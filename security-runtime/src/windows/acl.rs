use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::c_void;
use std::fs;
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_ACCESS_DENIED, ERROR_SUCCESS, HLOCAL, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::{
    GetNamedSecurityInfoW, SetEntriesInAclW, SetNamedSecurityInfoW, EXPLICIT_ACCESS_W,
    TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    EqualSid, GetAce, ACCESS_ALLOWED_ACE, ACL, CONTAINER_INHERIT_ACE, DACL_SECURITY_INFORMATION,
    OBJECT_INHERIT_ACE,
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
    /// Best-effort grant on a path we do not own (system executables owned by
    /// TrustedInstaller). The restricted token is WRITE_RESTRICTED, so
    /// read/execute access already flows through the factory `Users: RX` ACE;
    /// the grant is defense-in-depth only and is never fatal at apply,
    /// revoke, or stale-cleanup time.
    #[serde(default)]
    optional: bool,
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
    manifest_written: bool,
    _mutex: AclMutex,
}

pub(super) struct AclMutex(isize);

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
            manifest_written: false,
            _mutex: mutex,
        };
        lease.plan_records(&account_sid, request)?;
        save_records(state_dir, &lease.records)?;
        lease.manifest_written = true;
        if let Err(error) = lease.apply_records() {
            drop(lease);
            return Err(error);
        }
        // Re-save so the recovery manifest only lists ACEs that were actually
        // applied; optional grants skipped above must never become stale
        // revoke entries the next prepare would fail on.
        save_records(state_dir, &lease.records)?;
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
            optional: true,
        });
        if let Some(command) = canonical_command_executable(request)? {
            self.records.push(AclRecord {
                path: command,
                sid: account_sid.to_string(),
                access: AclAccess::Read,
                synthetic: false,
                optional: true,
            });
        }
        for root in &request.readable_roots {
            self.records.push(AclRecord {
                path: canonical_existing(root)?,
                sid: account_sid.to_string(),
                access: AclAccess::Read,
                synthetic: false,
                optional: false,
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
                optional: false,
            });
            self.records.push(AclRecord {
                path: root.clone(),
                sid: self.capability_sids[index].clone(),
                access: AclAccess::Write,
                synthetic: false,
                optional: false,
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
                    optional: false,
                });
                self.records.push(AclRecord {
                    path: protected.clone(),
                    sid: sid.to_string(),
                    access: AclAccess::DenyWrite,
                    synthetic,
                    optional: false,
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
                optional: false,
            });
            for sid in &self.capability_sids {
                self.records.push(AclRecord {
                    path: root.clone(),
                    sid: sid.clone(),
                    access: AclAccess::Deny,
                    synthetic: false,
                    optional: false,
                });
            }
        }
        Ok(())
    }

    fn apply_records(&mut self) -> Result<(), String> {
        let records = std::mem::take(&mut self.records);
        let mut applied = Vec::with_capacity(records.len());
        for record in records {
            let result = LocalSid::from_string(&record.sid).and_then(|sid| {
                let (mode, mask) = match record.access {
                    AclAccess::Read => (SET_ACCESS, READ_MASK),
                    AclAccess::Write => (SET_ACCESS, WRITE_MASK),
                    AclAccess::DenyWrite => (DENY_ACCESS, DENY_WRITE_MASK),
                    AclAccess::Deny => (DENY_ACCESS, GENERIC_ALL_MASK),
                };
                apply_entry(&record.path, sid.as_ptr(), mode, mask)
                    .map_err(|error| error.to_string())
            });
            match result {
                Ok(()) => applied.push(record),
                // Optional grants target paths we do not own (TrustedInstaller
                // system binaries); when the host refuses the DACL write the
                // factory `Users: RX` ACE still covers read/execute because the
                // restricted token is WRITE_RESTRICTED. Skip the record so the
                // recovery manifest and Drop never try to revoke it.
                Err(_) if record.optional => {}
                Err(error) => {
                    self.records = applied;
                    return Err(error);
                }
            }
        }
        self.records = applied;
        Ok(())
    }
}

impl Drop for AclLease {
    fn drop(&mut self) {
        if self.manifest_written {
            let _ = cleanup_stale(&self.state_dir);
        }
        for path in self.synthetic_protected.iter().rev() {
            let _ = fs::remove_dir(path);
        }
    }
}

struct AclUpdateError {
    path: PathBuf,
    operation: &'static str,
    code: u32,
}

impl std::fmt::Display for AclUpdateError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "cannot {} ACL for {}: {}",
            self.operation,
            self.path.display(),
            self.code
        )
    }
}

fn apply_entry(path: &Path, sid: *mut c_void, mode: u32, mask: u32) -> Result<(), AclUpdateError> {
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
        return Err(AclUpdateError {
            path: path.to_path_buf(),
            operation: "read",
            code: status,
        });
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
        Err(AclUpdateError {
            path: path.to_path_buf(),
            operation: "update",
            code: result,
        })
    }
}

fn revoke_entry(path: &Path, sid: *mut c_void) -> Result<(), AclUpdateError> {
    apply_entry(path, sid, REVOKE_ACCESS, 0)
}

pub(crate) fn protect_legacy_state(state_dir: &Path) -> Result<(), String> {
    for name in [ACL_STATE_FILE, CAPABILITY_STATE_FILE, ACL_CLEANUP_LOG] {
        super::state::protect_optional_file(&state_dir.join(name))?;
    }
    super::state::protect_optional_file(&state_dir.join(ACL_STATE_FILE).with_extension("bak"))
}

pub(super) fn verify_recovery_state(state_dir: &Path) -> Result<(), String> {
    let log = state_dir.join(ACL_CLEANUP_LOG);
    let manifest = state_dir.join(ACL_STATE_FILE);
    if super::state::read_optional_file(&log)?.is_some()
        && super::state::read_optional_file(&manifest)?.is_none()
        && super::state::read_optional_file(&manifest.with_extension("bak"))?.is_none()
    {
        return Err("ACL cleanup failed and the recovery manifest is missing".to_string());
    }
    Ok(())
}

/// Recover interrupted leases under the same lock used by managed commands.
pub(crate) fn recover_stale(state_dir: &Path) -> Result<bool, String> {
    if !state_dir.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    let Some(_lock) = try_acquire_mutex(0)? else {
        return Ok(false);
    };
    cleanup_stale(state_dir)?;
    Ok(true)
}

fn cleanup_stale(state_dir: &Path) -> Result<(), String> {
    let path = state_dir.join(ACL_STATE_FILE);
    let backup = path.with_extension("bak");
    verify_recovery_state(state_dir)?;
    let bytes = match super::state::read_optional_file(&path)? {
        Some(bytes) => bytes,
        None => match super::state::read_optional_file(&backup)? {
            Some(bytes) => bytes,
            None => return Ok(()),
        },
    };
    let mut records: Vec<AclRecord> = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid stale ACL manifest: {error}"))?;
    let mut failures = Vec::new();
    for index in (0..records.len()).rev() {
        let record = &records[index];
        if let Err(error) = revoke_record(record) {
            failures.push(format!("{}: {error}", record.path.display()));
            continue;
        }
        records.remove(index);
        // The API has returned after processing inheritance. Until then keep
        // this entry durable even if the ACE on the root already disappeared.
        save_records(state_dir, &records)?;
    }
    let log = state_dir.join(ACL_CLEANUP_LOG);
    if !failures.is_empty() {
        let _ = super::state::write_file(&log, failures.join("\n").as_bytes());
        return Err(format!(
            "stale ACL cleanup failed for {} record(s); recovery manifest retained",
            failures.len()
        ));
    }
    // Remove an older backup first so interruption cannot resurrect it after
    // clearing the current manifest.
    remove_recovery_file(&backup)?;
    remove_recovery_file(&log)?;
    remove_recovery_file(&path)
}

fn revoke_record(record: &AclRecord) -> Result<(), String> {
    match fs::symlink_metadata(&record.path) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("cannot inspect ACL root: {error}")),
    }
    let sid = LocalSid::from_string(&record.sid)?;
    if let Err(error) = revoke_entry(&record.path, sid.as_ptr()) {
        // An older write-ahead manifest can describe a read grant that the
        // host lacked permission to install. Only accept Access Denied as a
        // completed revoke when every DACL in the recorded scope confirms
        // the SID is absent, including any inherited entries on descendants.
        let read_is_absent = error.code == ERROR_ACCESS_DENIED
            && matches!(record.access, AclAccess::Read)
            && scope_has_no_ace(&record.path, &sid).unwrap_or(false);
        if !read_is_absent {
            return Err(error.to_string());
        }
    }
    if record.synthetic {
        let _ = fs::remove_dir(&record.path);
    }
    Ok(())
}

fn scope_has_no_ace(path: &Path, sid: &LocalSid) -> Result<bool, String> {
    let mut pending = vec![path.to_path_buf()];
    while let Some(path) = pending.pop() {
        let metadata = fs::symlink_metadata(&path)
            .map_err(|error| format!("cannot inspect ACL recovery path: {error}"))?;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || !path_dacl_has_no_ace(&path, sid)?
        {
            return Ok(false);
        }
        if metadata.is_dir() {
            let children = fs::read_dir(&path)
                .map_err(|error| format!("cannot enumerate ACL recovery path: {error}"))?;
            for child in children {
                let child =
                    child.map_err(|error| format!("cannot inspect ACL recovery child: {error}"))?;
                pending.push(child.path());
            }
        }
    }
    Ok(true)
}

fn path_dacl_has_no_ace(path: &Path, sid: &LocalSid) -> Result<bool, String> {
    let path_wide = super::identity::wide(path.as_os_str());
    let mut descriptor = std::ptr::null_mut();
    let mut acl: *mut ACL = std::ptr::null_mut();
    let status = unsafe {
        GetNamedSecurityInfoW(
            path_wide.as_ptr(),
            1,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut acl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!("cannot inspect ACL recovery DACL: {status}"));
    }
    let result = unsafe { dacl_has_no_ace(acl, sid) };
    unsafe { LocalFree(descriptor as HLOCAL) };
    result
}

unsafe fn dacl_has_no_ace(acl: *mut ACL, sid: &LocalSid) -> Result<bool, String> {
    // A null DACL allows unrestricted access and cannot prove that recovery
    // removed the sandbox account's effective grant.
    let Some(acl) = acl.as_ref() else {
        return Ok(false);
    };
    for index in 0..u32::from(acl.AceCount) {
        let mut ace = std::ptr::null_mut();
        if GetAce(acl as *const ACL as *mut ACL, index, &mut ace) == 0 || ace.is_null() {
            return Err("cannot inspect ACL recovery ACE".to_string());
        }
        let ace = &*ace.cast::<ACCESS_ALLOWED_ACE>();
        // Standard allow/deny ACEs have the same SID offset. Fail safely on
        // any other ACE format instead of interpreting its SID layout.
        if !matches!(ace.Header.AceType, 0 | 1) {
            return Ok(false);
        }
        if EqualSid((&raw const ace.SidStart).cast_mut().cast(), sid.as_ptr()) != 0 {
            return Ok(false);
        }
    }
    Ok(true)
}

fn remove_recovery_file(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "cannot clear ACL recovery state {}: {error}",
            path.display()
        )),
    }
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

/// Canonicalize an absolute path that may not exist yet by resolving the
/// nearest existing ancestor and re-appending the missing suffix. Matches
/// the macOS/Linux `canonicalize_allow_missing`: on Windows `canonicalize`
/// returns verbatim `\\?\` paths, so falling back to the raw path would
/// never satisfy `starts_with` against canonicalized writable roots.
fn canonical_allow_missing(path: &Path) -> Result<PathBuf, String> {
    let mut ancestor = path;
    let mut suffix = Vec::new();
    loop {
        match ancestor.canonicalize() {
            Ok(mut canonical) => {
                for name in suffix.iter().rev() {
                    canonical.push(name);
                }
                return Ok(canonical);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = ancestor.file_name().ok_or_else(|| {
                    format!("cannot resolve ACL root ancestor: {}", path.display())
                })?;
                suffix.push(name.to_os_string());
                ancestor = ancestor.parent().ok_or_else(|| {
                    format!("cannot resolve ACL root ancestor: {}", path.display())
                })?;
            }
            Err(error) => {
                return Err(format!(
                    "cannot resolve ACL root {}: {error}",
                    path.display()
                ));
            }
        }
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
        if root.is_dir() {
            for name in PROTECTED_NAMES {
                targets.insert(root.join(name), index);
            }
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
                canonical_allow_missing(path)?
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

pub(super) fn acquire_mutex() -> Result<AclMutex, String> {
    try_acquire_mutex(INFINITE)?.ok_or_else(|| "ACL mutex wait timed out".to_string())
}

fn try_acquire_mutex(wait_ms: u32) -> Result<Option<AclMutex>, String> {
    acquire_named_mutex(r"Local\AceWindowsSandboxAcl", wait_ms)
}

fn acquire_named_mutex(name: &str, wait_ms: u32) -> Result<Option<AclMutex>, String> {
    let name = super::identity::wide(name);
    let handle = unsafe { CreateMutexW(std::ptr::null_mut(), 0, name.as_ptr()) };
    if handle == 0 {
        return Err(format!("cannot create ACL mutex: {}", unsafe {
            GetLastError()
        }));
    }
    let wait = unsafe { WaitForSingleObject(handle, wait_ms) };
    if wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED {
        return Ok(Some(AclMutex(handle)));
    }
    unsafe { CloseHandle(handle) };
    if wait == WAIT_TIMEOUT {
        Ok(None)
    } else {
        Err(format!("cannot acquire ACL mutex: {wait}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonblocking_recovery_lock_reports_another_thread_as_busy() {
        let name = format!(r"Local\AceWindowsSandboxAclTest-{}", std::process::id());
        let lock = acquire_named_mutex(&name, 0).unwrap().unwrap();
        assert!(
            std::thread::spawn(move || acquire_named_mutex(&name, 0).unwrap().is_none())
                .join()
                .unwrap()
        );
        drop(lock);
    }

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
            full_disk_read: false,
            network_enabled: false,
            network_access: false,
            network_rules: vec![],
            allow_local_binding: false,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: Default::default(),
            home_files: Default::default(),
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
    fn writable_file_does_not_get_project_metadata_children() {
        let workspace = tempfile::tempdir().unwrap();
        let file = workspace.path().join("approved.txt");
        fs::write(&file, "approved").unwrap();

        let roots = vec![file.canonicalize().unwrap()];
        assert!(readonly_targets(&roots, &[]).unwrap().is_empty());
    }

    #[test]
    fn missing_readonly_root_inside_writable_root_is_accepted() {
        // Readiness-probe regression: the writable root arrives canonicalized
        // (verbatim `\\?\` on Windows) while an explicit read-only root that
        // does not exist yet must still resolve inside it.
        let writable = tempfile::tempdir().unwrap();
        let missing = writable.path().join("probe").join(".git");

        let targets =
            readonly_targets(&[writable.path().canonicalize().unwrap()], &[missing]).unwrap();

        let resolved = writable
            .path()
            .canonicalize()
            .unwrap()
            .join("probe")
            .join(".git");
        assert!(targets.contains_key(&resolved));
    }

    #[test]
    fn scope_verification_detects_a_grant_below_a_clean_root() {
        let dir = tempfile::tempdir().unwrap();
        let child = dir.path().join("child.txt");
        fs::write(&child, "test").unwrap();
        let sid = LocalSid::from_string("S-1-5-21-1-2-3-4").unwrap();
        assert!(scope_has_no_ace(dir.path(), &sid).unwrap());

        apply_entry(&child, sid.as_ptr(), SET_ACCESS, READ_MASK).unwrap_or_else(|error| {
            panic!("{error}");
        });

        assert!(path_dacl_has_no_ace(dir.path(), &sid).unwrap());
        assert!(!scope_has_no_ace(dir.path(), &sid).unwrap());
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
                optional: false,
            }],
        )
        .unwrap();

        let error = cleanup_stale(state.path()).unwrap_err();

        assert!(error.contains("stale ACL"));
        assert!(state.path().join(ACL_STATE_FILE).exists());
        assert!(state.path().join(ACL_CLEANUP_LOG).exists());
    }

    #[test]
    fn recovery_from_legacy_backup_checkpoints_completed_records() {
        let state = tempfile::tempdir().unwrap();
        let target = state.path().join("present");
        fs::create_dir(&target).unwrap();
        let blocked = AclRecord {
            path: target,
            sid: "not-a-windows-sid".to_string(),
            access: AclAccess::Write,
            synthetic: false,
            optional: false,
        };
        let completed = AclRecord {
            path: state.path().join("removed"),
            sid: "S-1-5-21-1-2-3-4".to_string(),
            access: AclAccess::Read,
            synthetic: false,
            optional: false,
        };
        let manifest = state.path().join(ACL_STATE_FILE);
        let backup = manifest.with_extension("bak");
        super::super::state::write_file(
            &backup,
            &serde_json::to_vec(&[blocked, completed]).unwrap(),
        )
        .unwrap();

        assert!(cleanup_stale(state.path()).is_err());

        let records: Vec<AclRecord> =
            serde_json::from_slice(&super::super::state::read_file(&manifest).unwrap()).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].path, state.path().join("present"));
        assert!(backup.exists());

        fs::remove_dir(&records[0].path).unwrap();
        cleanup_stale(state.path()).unwrap();

        assert!(!manifest.exists());
        assert!(!backup.exists());
        assert!(!state.path().join(ACL_CLEANUP_LOG).exists());
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
            optional: false,
        }];
        save_records(state.path(), &records).unwrap();
        let lease = AclLease {
            state_dir: state.path().to_path_buf(),
            records,
            capability_sids: Vec::new(),
            synthetic_protected: Vec::new(),
            manifest_written: true,
            _mutex: AclMutex(0),
        };

        drop(lease);

        assert!(state.path().join(ACL_STATE_FILE).exists());
        assert!(state.path().join(ACL_CLEANUP_LOG).exists());
    }
}
