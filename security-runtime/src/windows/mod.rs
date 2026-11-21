pub mod acl;
pub mod identity;
pub mod job;
pub mod process;
pub mod readiness;
pub mod state;
pub mod token;
pub mod wfp;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::{env, fs};

use rand::RngCore;
use std::sync::mpsc::{Receiver, SyncSender};

use crate::protocol::{RuntimeCapabilities, RuntimeControl, RuntimeMessage};

pub struct WindowsRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub readonly_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub full_disk_read: bool,
    pub network_enabled: bool,
    pub network_access: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub env_overrides: BTreeMap<String, String>,
    pub home_files: BTreeMap<String, Vec<u8>>,
}

pub struct WindowsRuntimeError {
    pub code: &'static str,
    pub message: String,
}

pub fn run(
    request: WindowsRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), WindowsRuntimeError> {
    run_with_control(request, None, sender)
}

pub fn run_interactive(
    request: WindowsRunRequest,
    control_rx: Receiver<RuntimeControl>,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), WindowsRuntimeError> {
    run_with_control(request, Some(control_rx), sender)
}

fn run_with_control(
    mut request: WindowsRunRequest,
    control_rx: Option<Receiver<RuntimeControl>>,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), WindowsRuntimeError> {
    let state_dir = state_dir().map_err(|message| error("sandbox_unavailable", message))?;
    let readiness = readiness::probe(&state_dir);
    if !readiness.filesystem_sandbox {
        return Err(error("sandbox_unavailable", readiness.detail));
    }
    let credentials = if request.network_access {
        identity::load_direct(&state_dir)
    } else if request.network_enabled {
        identity::load_online(&state_dir)
    } else {
        identity::load(&state_dir)
    }
    .map_err(|message| error("sandbox_unavailable", message))?;
    identity::ensure_account_usable(&credentials.username)
        .map_err(|message| error("sandbox_unavailable", message.to_string()))?;
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
    let scratch =
        ScratchDirectory::create().map_err(|message| error("sandbox_unavailable", message))?;
    request.writable_roots.push(scratch.path().to_path_buf());
    let lease = acl::AclLease::prepare(&state_dir, &credentials.username, &request)
        .map_err(|message| error("sandbox_denied", message))?;
    if !request.network_access {
        wfp::verify_installed(&credentials.username, request.network_enabled)
            .map_err(|message| error("network_unavailable", message))?;
    }
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
        direct_network: request.network_access,
        full_disk_read: request.full_disk_read,
        system_bwrap: false,
        bundled_bwrap: false,
        wsl_version: None,
        local_binding_control: false,
        explicit_handle_inheritance: true,
        windows_restricted_token: true,
        windows_acl: true,
        windows_job: true,
        windows_wfp: !request.network_access,
    };
    let outcome = process::run_via_account(
        &credentials,
        &request,
        lease.capability_sids(),
        scratch.path(),
        capabilities,
        control_rx,
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

struct ScratchDirectory(PathBuf);

impl ScratchDirectory {
    fn create() -> Result<Self, String> {
        let mut random = [0_u8; 16];
        rand::thread_rng().fill_bytes(&mut random);
        let name: String = random.iter().map(|value| format!("{value:02x}")).collect();
        let directory = env::temp_dir().join(format!("ace-sandbox-{name}"));
        fs::create_dir(&directory)
            .map_err(|error| format!("cannot create Windows sandbox scratch directory: {error}"))?;
        Ok(Self(directory))
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for ScratchDirectory {
    fn drop(&mut self) {
        // The managed child is gone and the ACL lease has been released.
        let _ = fs::remove_dir_all(&self.0);
    }
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
