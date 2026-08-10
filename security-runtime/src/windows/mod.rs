pub mod acl;
pub mod identity;
pub mod job;
pub mod process;
pub mod readiness;
pub mod state;
pub mod token;
pub mod wfp;

use std::collections::BTreeMap;
use std::env;
use std::path::PathBuf;
use std::sync::mpsc::SyncSender;

use crate::protocol::{RuntimeCapabilities, RuntimeMessage};

pub struct WindowsRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub readonly_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub env_overrides: BTreeMap<String, String>,
}

pub struct WindowsRuntimeError {
    pub code: &'static str,
    pub message: String,
}

pub fn run(
    request: WindowsRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), WindowsRuntimeError> {
    let state_dir = state_dir().map_err(|message| error("sandbox_unavailable", message))?;
    let readiness = readiness::probe(&state_dir);
    if !readiness.filesystem_sandbox {
        return Err(error("sandbox_unavailable", readiness.detail));
    }
    if request.network_enabled {
        wfp::verify_installed().map_err(|message| error("network_unavailable", message))?;
    }
    let credentials = if request.network_enabled {
        identity::load_online(&state_dir)
    } else {
        identity::load(&state_dir)
    }
    .map_err(|message| error("sandbox_unavailable", message))?;
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
    let policy =
        crate::network::NetworkPolicy::new(request.network_rules.clone()).map_err(network_error)?;
    let proxy = if request.network_enabled {
        Some(
            crate::network::proxy::ProxyHandle::start_on(policy, managed_proxy_port())
                .map_err(network_error)?,
        )
    } else {
        None
    };
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
        windows_wfp: request.network_enabled,
    };
    let outcome = process::run_via_account(
        &credentials,
        &request,
        lease.capability_sids(),
        capabilities,
        sender,
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
    drop(lease);
    outcome?;
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
    if !configured.is_absolute() {
        return Err("Windows security state directory must be absolute".to_string());
    }
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
