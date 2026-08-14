use std::fs;
use std::path::Path;

use windows_sys::Win32::Foundation::CloseHandle;

use super::acl::{AclLease, ACL_CLEANUP_LOG};
use super::identity;
use super::WindowsRunRequest;

pub struct Readiness {
    pub filesystem_sandbox: bool,
    pub detail: String,
}

pub fn probe(state_dir: &Path) -> Readiness {
    if let Err(error) = super::state::prepare_directory(state_dir) {
        return not_ready(error);
    }
    if let Err(error) = super::acl::protect_legacy_state(state_dir) {
        return not_ready(error);
    }
    // M5: if a prior AclLease::drop logged ACE revoke failures, the sandbox
    // may have residue ACEs -- refuse to report ready until cleared.
    match super::state::read_optional_file(&state_dir.join(ACL_CLEANUP_LOG)) {
        Ok(Some(_)) => {
            return not_ready(
                "ACL cleanup failures detected (windows-acl-cleanup.log); \
                 re-run setup or manually clear stale ACEs"
                    .to_string(),
            )
        }
        Ok(None) => {}
        Err(error) => return not_ready(error),
    }

    let credentials = match identity::load(state_dir) {
        Ok(c) => c,
        Err(error) => return not_ready(error),
    };
    if let Err(error) = super::token::sid_string_for_account(&credentials.username) {
        return not_ready(error);
    }
    let online = match identity::load_online(state_dir) {
        Ok(c) => c,
        Err(error) => return not_ready(error),
    };
    if let Err(error) = super::token::sid_string_for_account(&online.username) {
        return not_ready(error);
    }
    match super::job::KillOnCloseJob::new() {
        Ok(job) => drop(job),
        Err(error) => return not_ready(error),
    }

    // W3: exercise the real ACL write/revoke cycle and restricted token
    // creation so readiness detects FFI/permission failures, not just
    // identity/SID resolution.
    if let Err(error) = probe_acl_and_token(state_dir, &credentials.username) {
        return not_ready(error);
    }

    Readiness {
        filesystem_sandbox: true,
        detail: "Windows sandbox identity, SID, Job, ACL lease, and restricted token are ready"
            .to_string(),
    }
}

fn not_ready(detail: String) -> Readiness {
    Readiness {
        filesystem_sandbox: false,
        detail,
    }
}

/// Run AclLease::prepare + drop in an isolated subdirectory, then create and
/// immediately close a restricted token. Catches real ACL/token failures that
/// a pure identity/SID check would miss (audit W3).
fn probe_acl_and_token(state_dir: &Path, account: &str) -> Result<(), String> {
    let probe_state = state_dir.join(".readiness-probe");
    let probe_writable = probe_state.join("writable");
    fs::create_dir_all(&probe_writable)
        .map_err(|e| format!("cannot create readiness probe dir: {e}"))?;
    let request = WindowsRunRequest {
        command: vec!["cmd.exe".to_string()],
        cwd: probe_writable.clone(),
        writable_roots: vec![probe_writable.clone()],
        readable_roots: Vec::new(),
        readonly_roots: vec![probe_writable.join(".git")],
        denied_roots: Vec::new(),
        full_disk_read: false,
        network_enabled: false,
        network_rules: Vec::new(),
        allow_local_binding: false,
        max_output_bytes: 4096,
        stdin: None,
        env_overrides: Default::default(),
        home_files: Default::default(),
    };
    let lease = AclLease::prepare(&probe_state, account, &request)?;
    let capability_sids = lease.capability_sids().to_vec();
    let token_handle = super::token::create_restricted_token(&capability_sids)?;
    unsafe { CloseHandle(token_handle) };
    // Drop the lease -- this revokes the ACEs it applied.
    drop(lease);
    // If drop logged failures, the probe dir will contain the cleanup log.
    let probe_failed = probe_state.join(ACL_CLEANUP_LOG).exists();
    let _ = fs::remove_dir_all(&probe_state);
    if probe_failed {
        return Err("ACL revoke failed during readiness probe (ACE residue possible)".to_string());
    }
    Ok(())
}
