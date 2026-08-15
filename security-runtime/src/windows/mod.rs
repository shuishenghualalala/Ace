pub mod acl;
mod desktop;
pub mod identity;
pub mod job;
pub mod path;
pub mod process;
pub mod readiness;
pub mod state;
pub mod token;
mod users;
pub mod wfp;

use std::collections::BTreeMap;
use std::env;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, SyncSender};

use crate::protocol::{RuntimeCapabilities, RuntimeMessage, StdioInputMessage};

pub struct WindowsRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub stdin_stream: Option<Receiver<StdioInputMessage>>,
    pub env_overrides: BTreeMap<String, String>,
}

pub struct WindowsRuntimeError {
    pub code: &'static str,
    pub message: String,
}

pub fn run(
    mut request: WindowsRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), WindowsRuntimeError> {
    if request.allow_local_binding {
        return Err(error(
            "sandbox_denied",
            "Windows local binding is unavailable without a dedicated bind-capable identity",
        ));
    }
    let prepared = path::prepare_policy(
        &request.cwd,
        &request.writable_roots,
        &request.readable_roots,
        &request.denied_roots,
    )
    .map_err(|message| error("sandbox_denied", message))?;
    request.cwd = prepared.cwd;
    request.writable_roots = prepared.writable_roots;
    request.readable_roots = prepared.readable_roots;
    request.denied_roots = prepared.denied_roots;

    identity::validate_runtime_location()
        .map_err(|message| error("sandbox_unavailable", message))?;
    let state_dir = state_dir().map_err(|message| error("sandbox_unavailable", message))?;
    let readiness = readiness::probe(&state_dir);
    if !readiness.filesystem_sandbox {
        return Err(error("sandbox_unavailable", readiness.detail));
    }
    let offline =
        identity::load(&state_dir).map_err(|message| error("sandbox_unavailable", message))?;
    let online = identity::load_online(&state_dir)
        .map_err(|message| error("sandbox_unavailable", message))?;
    wfp::verify_installed(&offline.username, &online.username)
        .map_err(|message| error("network_unavailable", message))?;
    let credentials = if request.network_enabled {
        &online
    } else {
        &offline
    };
    let policy =
        crate::network::NetworkPolicy::new(request.network_rules.clone()).map_err(network_error)?;
    // H-20 (SEC-P2-003): the cross-process ACL mutex acquired by ``AclLease``
    // is the serialization boundary for managed runs. We start the proxy
    // *after* acquiring it and drop the proxy *before* releasing it (see the
    // explicit drops below). That ordering proves the proxy bound to the fixed
    // loopback port (43119) always belongs to the currently-running sandbox: a
    // second managed-network run cannot bind the port until this run's sandbox
    // has exited, so cross-task proxy/policy leakage is structurally impossible
    // even if this proxy's listener thread died mid-run. The port is runtime-
    // chosen (``managed_proxy_port``), never accepted from the model/request.
    // True parallelism (WIN-NET-001) stays deferred — it needs per-run dynamic
    // ports + runtime WFP filters; serialization keeps the single-port design
    // safe until then.
    let lease = acl::AclLease::prepare(&state_dir, &credentials.username, &request)
        .map_err(|message| error("sandbox_denied", message))?;
    if let Err(identity_error) = lease.verify_pins() {
        let cleanup = lease.finish();
        return Err(match cleanup {
            Ok(()) => error("sandbox_denied", identity_error),
            Err(cleanup) => error(
                "sandbox_denied",
                format!("{identity_error}; ACL cleanup also failed: {cleanup}"),
            ),
        });
    }
    let proxy = if request.network_enabled {
        match crate::network::proxy::ProxyHandle::start_on(policy, managed_proxy_port()) {
            Ok(proxy) => Some(proxy),
            Err(proxy_error) => {
                let cleanup = lease.finish();
                return Err(match cleanup {
                    Ok(()) => network_error(proxy_error),
                    Err(cleanup) => error(
                        "sandbox_denied",
                        format!(
                            "{}; ACL cleanup also failed: {cleanup}",
                            proxy_error.message
                        ),
                    ),
                });
            }
        }
    } else {
        None
    };
    if let Err(identity_error) = lease.verify_pins() {
        drop(proxy);
        let cleanup = lease.finish();
        return Err(match cleanup {
            Ok(()) => error("sandbox_denied", identity_error),
            Err(cleanup) => error(
                "sandbox_denied",
                format!("{identity_error}; ACL cleanup also failed: {cleanup}"),
            ),
        });
    }
    let capabilities = RuntimeCapabilities {
        backend: "windows_sandbox_account",
        filesystem_sandbox: true,
        process_tree_cleanup: true,
        managed_network: request.network_enabled,
        system_bwrap: false,
        bundled_bwrap: false,
        wsl_version: None,
        local_binding_control: false,
        explicit_handle_inheritance: true,
        windows_restricted_token: true,
        windows_acl: true,
        windows_job: true,
        windows_wfp: true,
    };
    let proxy_url = proxy.as_ref().map(|proxy| {
        proxy.proxy_url(SocketAddr::from((
            Ipv4Addr::LOCALHOST,
            managed_proxy_port(),
        )))
    });
    let stdin_stream = request.stdin_stream.take();
    let outcome = process::run_via_account(
        credentials,
        &request,
        process::RunViaAccountContext {
            temp_dir: lease.temp_dir(),
            capability_sids: lease.capability_sids(),
            proxy_url,
            capabilities,
            stdin_stream,
            sender,
        },
        || lease.verify_pins(),
    )
    .map_err(|message| {
        if message.starts_with("OUTPUT_TRUNCATED:") {
            error("output_truncated", message)
        } else {
            error("sandbox_denied", message)
        }
    });
    // Drop the proxy before the lease: the loopback port must be free before
    // the mutex releases, or the next managed run spuriously fails to bind.
    // Both drops run on every path (Ok and Err).
    drop(proxy);
    let cleanup = lease
        .finish()
        .map_err(|message| error("sandbox_denied", message));
    let exit_code = match (outcome, cleanup) {
        (Ok(exit_code), Ok(())) => exit_code,
        (Err(process_error), Ok(())) => return Err(process_error),
        (Ok(_), Err(cleanup)) => return Err(cleanup),
        (Err(process_error), Err(cleanup)) => {
            return Err(error(
                "sandbox_denied",
                format!(
                    "{}; cleanup failure: {}",
                    process_error.message, cleanup.message
                ),
            ))
        }
    };
    sender
        .send(RuntimeMessage::Completed(exit_code))
        .map_err(|_| error("sandbox_denied", "protocol receiver disconnected"))?;
    Ok(())
}

fn managed_proxy_port() -> u16 {
    // Fixed loopback port the WFP filter permits for the online sandbox
    // account. Runtime-chosen; never derived from the request or model.
    43119
}

fn state_dir() -> Result<PathBuf, String> {
    let configured = env::var_os("ACE_SECURITY_STATE_DIR")
        .map(PathBuf::from)
        .ok_or_else(|| "ACE_SECURITY_STATE_DIR is not configured".to_string())?;
    path::validate_local_absolute(&configured)?;
    Ok(configured)
}

fn error(code: &'static str, message: impl Into<String>) -> WindowsRuntimeError {
    WindowsRuntimeError {
        code,
        message: message.into(),
    }
}

fn network_error(error: crate::network::policy::NetworkError) -> WindowsRuntimeError {
    WindowsRuntimeError {
        code: error.code.as_str(),
        message: error.message,
    }
}
