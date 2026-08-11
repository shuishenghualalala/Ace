use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::c_void;
use std::fs;
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_SUCCESS, HLOCAL};
use windows_sys::Win32::Security::Authorization::{
    GetNamedSecurityInfoW, SetEntriesInAclW, SetNamedSecurityInfoW, EXPLICIT_ACCESS_W,
    TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    ACL, CONTAINER_INHERIT_ACE, DACL_SECURITY_INFORMATION, OBJECT_INHERIT_ACE,
};
use windows_sys::Win32::Storage::FileSystem::{
    DELETE, FILE_ATTRIBUTE_REPARSE_POINT, FILE_GENERIC_EXECUTE, FILE_GENERIC_READ,
    FILE_GENERIC_WRITE,
};
use windows_sys::Win32::System::Threading::{
    CreateMutexW, ReleaseMutex, WaitForSingleObject, INFINITE,
};

use super::token::{sid_string_for_account, LocalSid};
use super::WindowsRunRequest;

const ACL_STATE_FILE: &str = "windows-acl-state.json";
const CAPABILITY_STATE_FILE: &str = "windows-capability-sids.json";
/// Written by AclLease::drop when ACE revocation fails; readiness checks for
/// its existence to detect ACE residue (audit M5).
pub const ACL_CLEANUP_LOG: &str = "windows-acl-cleanup.log";
const PROTECTED_NAMES: &[&str] = &[".git", ".agents", ".crew"];
const READ_MASK: u32 = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE;
const WRITE_MASK: u32 = FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE;
const DENY_WRITE_MASK: u32 = FILE_GENERIC_WRITE | DELETE | 0x40; // FILE_DELETE_CHILD
const GENERIC_ALL_MASK: u32 = 0x1000_0000;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_ABANDONED: u32 = 0x80;
// EXPLICIT_ACCESS_W.grfAccessMode is typed ACCESS_MODE (i32) in windows-sys, so the
// u32 constants below are cast to i32 at the assignment site in apply_entry.
const SET_ACCESS: u32 = 2;
const DENY_ACCESS: u32 = 3;
const REVOKE_ACCESS: u32 = 4;

#[derive(Debug, Serialize, Deserialize)]
struct AclRecord {
    path: PathBuf,
    sid: String,
    access: AclAccess,
    #[serde(default)]
    synthetic: bool,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
enum AclAccess {
    Read,
    Write,
    DenyWrite,
    Deny,
}

#[derive(Default, Serialize, Deserialize)]
struct CapabilityState {
    by_root: std::collections::BTreeMap<String, String>,
    readonly: Option<String>,
}

pub struct AclLease {
    state_dir: PathBuf,
    records: Vec<AclRecord>,
    capability_sids: Vec<String>,
    synthetic_protected: Vec<PathBuf>,
    _mutex: AclMutex,
}

struct AclMutex(isize);

impl Drop for AclMutex {
    fn drop(&mut self) {
        if self.0 != 0 {
            unsafe {
                ReleaseMutex(self.0);
                CloseHandle(self.0);
            }
        }
    }
}

impl AclLease {
    /// Reconcile stale sandbox ACEs, apply the exact task roots, and hold a
    /// cross-process mutex for the command lifetime so account ACLs cannot mix.
    pub fn prepare(
        state_dir: &Path,
        account: &str,
        request: &WindowsRunRequest,
    ) -> Result<Self, String> {
        let mutex = acquire_mutex()?;
        super::state::prepare_directory(state_dir)?;
        protect_legacy_state(state_dir)?;
        cleanup_stale(state_dir)?;
        let account_sid = sid_string_for_account(account)?;
        let mut capability_state = load_capabilities(state_dir)?;
        let mut capability_sids = Vec::new();
        for root in &request.writable_roots {
            let root = canonical_existing(root)?;
            let key = root.to_string_lossy().to_lowercase();
            let sid = capability_state
                .by_root
                .entry(key)
                .or_insert_with(random_capability_sid)
                .clone();
            capability_sids.push(sid);
        }
        if capability_sids.is_empty() {
            capability_sids.push(
                capability_state
                    .readonly
                    .get_or_insert_with(random_capability_sid)
                    .clone(),
            );
        }
        save_capabilities(state_dir, &capability_state)?;

        let mut lease = Self {
            state_dir: state_dir.to_path_buf(),
            records: Vec::new(),
            capability_sids,
            synthetic_protected: Vec::new(),
            _mutex: mutex,
        };
        lease.plan_records(&account_sid, request)?;
        save_records(state_dir, &lease.records)?;
        if let Err(error) = lease.apply_records() {
            drop(lease);
            return Err(error);
        }
        Ok(lease)
    }

    pub fn capability_sids(&self) -> &[String] {
        &self.capability_sids
    }

    fn plan_records(
        &mut self,
        account_sid: &str,
        request: &WindowsRunRequest,
    ) -> Result<(), String> {
        let executable = std::env::current_exe()
            .and_then(|path| path.canonicalize())
            .map_err(|error| format!("cannot resolve runtime executable: {error}"))?;
        self.records.push(AclRecord {
            path: executable,
            sid: account_sid.to_string(),
            access: AclAccess::Read,
            synthetic: false,
        });
        if let Some(command) = canonical_command_executable(request)? {
            self.records.push(AclRecord {
                path: command,
                sid: account_sid.to_string(),
                access: AclAccess::Read,
                synthetic: false,
            });
        }
        for root in &request.readable_roots {
            self.records.push(AclRecord {
                path: canonical_existing(root)?,
                sid: account_sid.to_string(),
                access: AclAccess::Read,
                synthetic: false,
            });
        }
        let writable = request
            .writable_roots
            .iter()
            .map(|root| canonical_existing(root))
            .collect::<Result<Vec<_>, _>>()?;
        for (index, root) in writable.iter().enumerate() {
            self.records.push(AclRecord {
                path: root.clone(),
                sid: account_sid.to_string(),
                access: AclAccess::Write,
                synthetic: false,
            });
            self.records.push(AclRecord {
                path: root.clone(),
                sid: self.capability_sids[index].clone(),
                access: AclAccess::Write,
                synthetic: false,
            });
        }
        for (protected, writable_index) in readonly_targets(&writable, &request.readonly_roots)? {
            if !protected.exists() {
                fs::create_dir(&protected).map_err(|error| {
                    format!(
                        "cannot create protected ACL mount point {}: {error}",
                        protected.display()
                    )
                })?;
                self.synthetic_protected.push(protected.clone());
            }
            let synthetic = self.synthetic_protected.contains(&protected);
            for sid in [account_sid, self.capability_sids[writable_index].as_str()] {
                self.records.push(AclRecord {
                    path: protected.clone(),
                    sid: sid.to_string(),
                    access: AclAccess::Read,
                    synthetic,
                });
                self.records.push(AclRecord {
                    path: protected.clone(),
                    sid: sid.to_string(),
                    access: AclAccess::DenyWrite,
                    synthetic,
                });
            }
        }
        for root in &request.denied_roots {
            // SQLite sidecars (WAL/SHM/journal) are created lazily. A missing
            // optional sidecar is not an ACL failure; required writable and
            // readable roots above still use canonical_existing and remain
            // fail-closed. Existing deny roots receive the same ACL records.
            let Some(root) = canonical_optional(root)? else {
                continue;
            };
            self.records.push(AclRecord {
                path: root.clone(),
                sid: account_sid.to_string(),
                access: AclAccess::Deny,
                synthetic: false,
            });
            for sid in &self.capability_sids {
                self.records.push(AclRecord {
                    path: root.clone(),
                    sid: sid.clone(),
                    access: AclAccess::Deny,
                    synthetic: false,
                });
            }
        }
        Ok(())
    }

    fn apply_records(&self) -> Result<(), String> {
        for record in &self.records {
            let sid = LocalSid::from_string(&record.sid)?;
            let (mode, mask) = match record.access {
                AclAccess::Read => (SET_ACCESS, READ_MASK),
                AclAccess::Write => (SET_ACCESS, WRITE_MASK),
                AclAccess::DenyWrite => (DENY_ACCESS, DENY_WRITE_MASK),
                AclAccess::Deny => (DENY_ACCESS, GENERIC_ALL_MASK),
            };
            apply_entry(&record.path, sid.as_ptr(), mode, mask)?;
        }
        Ok(())
    }
}

impl Drop for AclLease {
    fn drop(&mut self) {
        // M5: collect ACE revoke failures instead of silently swallowing them.
        let mut revoke_failures: Vec<String> = Vec::new();
        for record in self.records.iter().rev() {
            match LocalSid::from_string(&record.sid) {
                Ok(sid) => {
                    if let Err(error) = revoke_entry(&record.path, sid.as_ptr()) {
                        revoke_failures.push(format!("{}: {}", record.path.display(), error));
                    }
                }
                Err(error) => {
                    revoke_failures.push(format!("{}: {}", record.path.display(), error));
                }
            }
        }
        for path in self.synthetic_protected.iter().rev() {
            let _ = fs::remove_dir(path);
        }
        // Persist revoke failures so readiness can flag ACE residue; clear on success.
        let log_path = self.state_dir.join(ACL_CLEANUP_LOG);
        if revoke_failures.is_empty() {
            let _ = fs::remove_file(self.state_dir.join(ACL_STATE_FILE));
            let _ = fs::remove_file(&log_path);
        } else {
            // Keep the recovery manifest as the fail-closed source of truth. The
            // human-readable log is supplemental and may itself fail to persist.
            let _ = super::state::write_file(&log_path, revoke_failures.join("\n").as_bytes());
        }
    }
}

fn apply_entry(path: &Path, sid: *mut c_void, mode: u32, mask: u32) -> Result<(), String> {
    let mut path_wide = super::identity::wide(path.as_os_str());
    let mut descriptor = std::ptr::null_mut();
    let mut old_acl: *mut ACL = std::ptr::null_mut();
    let status = unsafe {
        GetNamedSecurityInfoW(
            path_wide.as_ptr(),
            1,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut old_acl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!("cannot read ACL for {}: {status}", path.display()));
    }
    let mut entry: EXPLICIT_ACCESS_W = unsafe { std::mem::zeroed() };
    entry.grfAccessPermissions = mask;
    entry.grfAccessMode = mode as i32;
    entry.grfInheritance = CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE;
    entry.Trustee = TRUSTEE_W {
        pMultipleTrustee: std::ptr::null_mut(),
        MultipleTrusteeOperation: 0,
        TrusteeForm: TRUSTEE_IS_SID,
        TrusteeType: TRUSTEE_IS_UNKNOWN,
        ptstrName: sid.cast(),
    };
    let mut new_acl: *mut ACL = std::ptr::null_mut();
    let merge = unsafe { SetEntriesInAclW(1, &entry, old_acl, &mut new_acl) };
    let result = if merge == ERROR_SUCCESS {
        unsafe {
            SetNamedSecurityInfoW(
                path_wide.as_mut_ptr(),
                1,
                DACL_SECURITY_INFORMATION,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                new_acl,
                std::ptr::null_mut(),
            )
        }
    } else {
        merge
    };
    unsafe {
        if !new_acl.is_null() {
            LocalFree(new_acl as HLOCAL);
        }
        if !descriptor.is_null() {
            LocalFree(descriptor as HLOCAL);
        }
    }
    if result == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(format!(
            "cannot update ACL for {}: {result}",
            path.display()
        ))
    }
}

fn revoke_entry(path: &Path, sid: *mut c_void) -> Result<(), String> {
    apply_entry(path, sid, REVOKE_ACCESS, 0)
}

pub(crate) fn protect_legacy_state(state_dir: &Path) -> Result<(), String> {
    for name in [ACL_STATE_FILE, CAPABILITY_STATE_FILE, ACL_CLEANUP_LOG] {
        super::state::protect_optional_file(&state_dir.join(name))?;
    }
    Ok(())
}

fn cleanup_stale(state_dir: &Path) -> Result<(), String> {
    let path = state_dir.join(ACL_STATE_FILE);
    let Some(bytes) = super::state::read_optional_file(&path)? else {
        return Ok(());
    };
    let records: Vec<AclRecord> = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid stale ACL manifest: {error}"))?;
    let mut failures = Vec::new();
    for record in records.iter().rev() {
        if !record.path.exists() {
            continue;
        }
        let sid = match LocalSid::from_string(&record.sid) {
            Ok(sid) => sid,
            Err(error) => {
                failures.push(format!("{}: {error}", record.path.display()));
                continue;
            }
        };
        if let Err(error) = revoke_entry(&record.path, sid.as_ptr()) {
            failures.push(format!("{}: {error}", record.path.display()));
            continue;
        }
        if record.synthetic {
            let _ = fs::remove_dir(&record.path);
        }
    }
    if !failures.is_empty() {
        let _ = super::state::write_file(
            &state_dir.join(ACL_CLEANUP_LOG),
            failures.join("\n").as_bytes(),
        );
        return Err(format!(
            "stale ACL cleanup failed for {} record(s); recovery manifest retained",
            failures.len()
        ));
    }
    fs::remove_file(path).map_err(|error| format!("cannot clear stale ACL manifest: {error}"))
}

fn save_records(state_dir: &Path, records: &[AclRecord]) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(records)
        .map_err(|error| format!("cannot serialize ACL manifest: {error}"))?;
    super::state::write_file(&state_dir.join(ACL_STATE_FILE), &bytes)
        .map_err(|error| format!("cannot persist ACL manifest: {error}"))
}

fn load_capabilities(state_dir: &Path) -> Result<CapabilityState, String> {
    match super::state::read_optional_file(&state_dir.join(CAPABILITY_STATE_FILE))? {
        Some(bytes) => serde_json::from_slice(&bytes)
            .map_err(|error| format!("invalid capability state: {error}")),
        None => Ok(Default::default()),
    }
}

fn save_capabilities(state_dir: &Path, state: &CapabilityState) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(state)
        .map_err(|error| format!("cannot serialize capability state: {error}"))?;
    let path = state_dir.join(CAPABILITY_STATE_FILE);
    super::state::write_file(&path, &bytes)
        .map_err(|error| format!("cannot persist capability state: {error}"))
}

fn random_capability_sid() -> String {
    let mut rng = thread_rng();
    format!(
        "S-1-5-21-{}-{}-{}-{}",
        rng.gen::<u32>(),
        rng.gen::<u32>(),
        rng.gen::<u32>(),
        rng.gen::<u32>()
    )
}

fn canonical_existing(path: &Path) -> Result<PathBuf, String> {
    path.canonicalize()
        .map_err(|error| format!("cannot resolve ACL root {}: {error}", path.display()))
}

fn canonical_command_executable(request: &WindowsRunRequest) -> Result<Option<PathBuf>, String> {
    let Some(path) = request.command.first().map(Path::new) else {
        return Ok(None);
    };
    if !path.is_absolute() {
        return Ok(None);
    }
    canonical_existing(path).map(Some)
}

fn canonical_optional(path: &Path) -> Result<Option<PathBuf>, String> {
    match path.canonicalize() {
        Ok(path) => Ok(Some(path)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "cannot resolve ACL root {}: {error}",
            path.display()
        )),
    }
}

fn reject_reparse_point(path: &Path) -> Result<(), String> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!(
                "protected metadata cannot be a reparse point: {}",
                path.display()
            ));
        }
    }
    Ok(())
}

fn readonly_targets(
    writable: &[PathBuf],
    explicit: &[PathBuf],
) -> Result<std::collections::BTreeMap<PathBuf, usize>, String> {
    let mut targets = std::collections::BTreeMap::new();
    for (index, root) in writable.iter().enumerate() {
        for name in PROTECTED_NAMES {
            targets.insert(root.join(name), index);
        }
    }
    for path in explicit {
        reject_reparse_point(path)?;
        let root = match canonical_optional(path)? {
            Some(root) => root,
            None if path.is_absolute()
                && !path
                    .components()
                    .any(|part| matches!(part, std::path::Component::ParentDir)) =>
            {
                path.clone()
            }
            None => {
                return Err(format!(
                    "read-only root must be absolute and cannot contain '..': {}",
                    path.display()
                ));
            }
        };
        let Some((index, _)) = writable
            .iter()
            .enumerate()
            .filter(|(_, candidate)| root.starts_with(candidate))
            .max_by_key(|(_, candidate)| candidate.components().count())
        else {
            return Err(format!(
                "read-only root must be inside an explicit writable root: {}",
                root.display()
            ));
        };
        targets.insert(root, index);
    }
    for path in targets.keys() {
        reject_reparse_point(path)?;
    }
    Ok(targets)
}

fn acquire_mutex() -> Result<AclMutex, String> {
    let name = super::identity::wide("Local\\AceWindowsSandboxAcl");
    let handle = unsafe { CreateMutexW(std::ptr::null_mut(), 0, name.as_ptr()) };
    if handle == 0 {
        return Err(format!("cannot create ACL mutex: {}", unsafe {
            GetLastError()
        }));
    }
    let wait = unsafe { WaitForSingleObject(handle, INFINITE) };
    if wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED {
        Ok(AclMutex(handle))
    } else {
        unsafe { CloseHandle(handle) };
        Err(format!("cannot acquire ACL mutex: {wait}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_optional_root_is_skipped() {
        let path = PathBuf::from(format!(
            r"C:\\ace-security-runtime-missing-{}",
            std::process::id()
        ));
        assert_eq!(
            canonical_optional(&path).expect("not a permission error"),
            None
        );
    }

    #[test]
    fn absolute_command_executable_is_part_of_the_acl_plan() {
        let directory = tempfile::tempdir().unwrap();
        let executable = directory.path().join("tool.exe");
        fs::write(&executable, b"test").unwrap();
        let request = WindowsRunRequest {
            command: vec![executable.to_string_lossy().into_owned()],
            cwd: directory.path().to_path_buf(),
            writable_roots: vec![],
            readable_roots: vec![],
            readonly_roots: vec![],
            denied_roots: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: Default::default(),
        };

        assert_eq!(
            canonical_command_executable(&request).unwrap(),
            Some(executable.canonicalize().unwrap()),
        );
    }

    #[test]
    fn readonly_root_outside_writable_roots_is_rejected() {
        let writable = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();

        let error = readonly_targets(
            &[writable.path().canonicalize().unwrap()],
            &[outside.path().to_path_buf()],
        )
        .unwrap_err();

        assert!(error.contains("read-only root must be inside"), "{error}");
    }

    #[test]
    fn stale_cleanup_failure_keeps_recovery_manifest() {
        let state = tempfile::tempdir().unwrap();
        let target = state.path().join("still-present");
        fs::create_dir(&target).unwrap();
        save_records(
            state.path(),
            &[AclRecord {
                path: target,
                sid: "not-a-windows-sid".to_string(),
                access: AclAccess::Write,
                synthetic: false,
            }],
        )
        .unwrap();

        let error = cleanup_stale(state.path()).unwrap_err();

        assert!(error.contains("stale ACL"));
        assert!(state.path().join(ACL_STATE_FILE).exists());
        assert!(state.path().join(ACL_CLEANUP_LOG).exists());
    }

    #[test]
    fn lease_drop_keeps_recovery_manifest_when_sid_cannot_be_resolved() {
        let state = tempfile::tempdir().unwrap();
        let target = state.path().join("still-present");
        fs::create_dir(&target).unwrap();
        let records = vec![AclRecord {
            path: target,
            sid: "not-a-windows-sid".to_string(),
            access: AclAccess::Write,
            synthetic: false,
        }];
        save_records(state.path(), &records).unwrap();
        let lease = AclLease {
            state_dir: state.path().to_path_buf(),
            records,
            capability_sids: Vec::new(),
            synthetic_protected: Vec::new(),
            _mutex: AclMutex(0),
        };

        drop(lease);

        assert!(state.path().join(ACL_STATE_FILE).exists());
        assert!(state.path().join(ACL_CLEANUP_LOG).exists());
    }
}
