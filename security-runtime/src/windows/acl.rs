use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::c_void;
use std::fs;
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_SUCCESS, HLOCAL, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Security::Authorization::{
    GetSecurityInfo, SetEntriesInAclW, SetSecurityInfo, EXPLICIT_ACCESS_W, TRUSTEE_IS_SID,
    TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    AclSizeInformation, GetSecurityDescriptorDacl, IsValidAcl, MapGenericMask, ACCESS_ALLOWED_ACE,
    ACE_HEADER, ACL, ACL_SIZE_INFORMATION, CONTAINER_INHERIT_ACE, DACL_SECURITY_INFORMATION,
    GENERIC_MAPPING, OBJECT_INHERIT_ACE,
};
use windows_sys::Win32::Security::{EqualSid, GetAce, GetAclInformation};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, DELETE, FILE_ALL_ACCESS,
    FILE_APPEND_DATA, FILE_ATTRIBUTE_REPARSE_POINT, FILE_DELETE_CHILD, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_EXECUTE, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_WRITE_ATTRIBUTES, FILE_WRITE_DATA, FILE_WRITE_EA,
    OPEN_EXISTING, READ_CONTROL, WRITE_DAC,
};
use windows_sys::Win32::System::Threading::{
    CreateMutexW, ReleaseMutex, WaitForSingleObject, INFINITE,
};

use super::token::{sid_string_for_account, LocalSid};
use super::WindowsRunRequest;

const ACL_STATE_FILE: &str = "windows-acl-state.json";
const CAPABILITY_STATE_FILE: &str = "windows-capability-sids.json";
const ACL_STATE_VERSION: u16 = 2;
const RUNS_DIRECTORY: &str = "windows-runs";
/// Written by AclLease::drop when ACE revocation fails; readiness checks for
/// its existence to detect ACE residue (audit M5).
pub const ACL_CLEANUP_LOG: &str = "windows-acl-cleanup.log";
const PROTECTED_NAMES: &[&str] = &[".git", ".agents", ".crew"];
const READ_MASK: u32 = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE;
const WRITE_MASK: u32 = FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE;
// Do not grant FILE_DELETE_CHILD on the parent. Codex grants DELETE on the
// inheriting descendants so a protected child deny cannot be bypassed.
const STALE_DELETE_CHILD_MASK: u32 = FILE_DELETE_CHILD;
const GENERIC_WRITE_MASK: u32 = 0x4000_0000;
const INHERIT_ONLY_ACE: u8 = 0x08;
const INHERITED_ACE: u8 = 0x10;
const REQUIRED_WRITE_INHERIT_FLAGS: u8 = (CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE) as u8;
// Keep protected roots readable/executable while denying every write/delete
// path, matching Codex's write-only deny ACE.
const DENY_WRITE_MASK: u32 = FILE_GENERIC_WRITE
    | FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
    | GENERIC_WRITE_MASK
    | DELETE
    | FILE_DELETE_CHILD;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_ABANDONED: u32 = 0x80;
const SE_FILE_OBJECT: i32 = 1;
// EXPLICIT_ACCESS_W.grfAccessMode is typed ACCESS_MODE (i32) in windows-sys, so the
// u32 constants below are cast to i32 at the assignment site in apply_entry.
const SET_ACCESS: u32 = 2;
const DENY_ACCESS: u32 = 3;
const REVOKE_ACCESS: u32 = 4;

#[derive(Clone, Debug, Serialize, Deserialize)]
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
    DenyDeleteChild,
    DenyWrite,
    DenyAll,
    // Legacy manifests used one write-only deny variant.
    Deny,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct DaclSnapshot(Vec<u8>);

#[derive(Clone, Debug, Serialize, Deserialize)]
struct AclRestoreRecord {
    path: PathBuf,
    dacl: DaclSnapshot,
    synthetic: bool,
    #[serde(default)]
    identity: Option<FileIdentity>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct CleanupPath {
    path: PathBuf,
    recursive: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct AclManifest {
    version: u16,
    restores: Vec<AclRestoreRecord>,
    cleanup_paths: Vec<CleanupPath>,
}

pub struct AclLease {
    state_dir: PathBuf,
    records: Vec<AclRecord>,
    restores: Vec<AclRestoreRecord>,
    pins: Vec<PinnedObject>,
    capability_sids: Vec<String>,
    synthetic_protected: Vec<PathBuf>,
    cleanup_paths: Vec<CleanupPath>,
    temp_dir: PathBuf,
    cleanup_attempted: bool,
    _mutex: AclMutex,
}

struct PinnedObject {
    path: PathBuf,
    handle: isize,
    volume_serial: u32,
    file_index_high: u32,
    file_index_low: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct FileIdentity {
    volume_serial: u32,
    file_index_high: u32,
    file_index_low: u32,
}

impl PinnedObject {
    fn open(path: &Path) -> Result<Self, String> {
        super::path::reject_reparse_components(path)?;
        let path_wide = super::identity::wide(path.as_os_str());
        let handle = unsafe {
            CreateFileW(
                path_wide.as_ptr(),
                READ_CONTROL | WRITE_DAC,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                0,
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(format!(
                "cannot pin ACL target {}: {}",
                path.display(),
                unsafe { GetLastError() }
            ));
        }
        let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe { GetFileInformationByHandle(handle, &mut information) } == 0 {
            let error = unsafe { GetLastError() };
            unsafe { CloseHandle(handle) };
            return Err(format!(
                "cannot inspect pinned ACL target {}: {error}",
                path.display()
            ));
        }
        if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            unsafe { CloseHandle(handle) };
            return Err(format!(
                "ACL target cannot be a reparse point: {}",
                path.display()
            ));
        }
        Ok(Self {
            path: path.to_path_buf(),
            handle,
            volume_serial: information.dwVolumeSerialNumber,
            file_index_high: information.nFileIndexHigh,
            file_index_low: information.nFileIndexLow,
        })
    }

    fn verify_path_identity(&self) -> Result<(), String> {
        let current = Self::open(&self.path)?;
        if current.identity() != self.identity() {
            return Err(format!(
                "ACL target identity changed after authorization: {}",
                self.path.display()
            ));
        }
        Ok(())
    }

    fn identity(&self) -> FileIdentity {
        FileIdentity {
            volume_serial: self.volume_serial,
            file_index_high: self.file_index_high,
            file_index_low: self.file_index_low,
        }
    }
}

impl Drop for PinnedObject {
    fn drop(&mut self) {
        if self.handle != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.handle) };
        }
    }
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
        let mut capability_sids = request
            .writable_roots
            .iter()
            .map(|_| random_capability_sid())
            .collect::<Vec<_>>();
        if capability_sids.is_empty() {
            capability_sids.push(random_capability_sid());
        }
        let (run_dir, temp_dir) = create_run_directories(state_dir)?;

        let mut lease = Self {
            state_dir: state_dir.to_path_buf(),
            records: Vec::new(),
            restores: Vec::new(),
            pins: Vec::new(),
            capability_sids,
            synthetic_protected: Vec::new(),
            cleanup_paths: vec![CleanupPath {
                path: run_dir,
                recursive: true,
            }],
            temp_dir,
            cleanup_attempted: false,
            _mutex: mutex,
        };
        if let Err(error) = lease
            .plan_records(&account_sid, request)
            .and_then(|()| lease.save_manifest())
            .and_then(|()| lease.apply_records())
        {
            let cleanup = lease.cleanup_internal();
            return Err(match cleanup {
                Ok(()) => error,
                Err(cleanup) => format!("{error}; ACL rollback also failed: {cleanup}"),
            });
        }
        Ok(lease)
    }

    pub fn capability_sids(&self) -> &[String] {
        &self.capability_sids
    }

    pub fn temp_dir(&self) -> &Path {
        &self.temp_dir
    }

    pub fn verify_pins(&self) -> Result<(), String> {
        for pin in &self.pins {
            pin.verify_path_identity()?;
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<(), String> {
        self.cleanup_internal()
    }

    fn plan_records(
        &mut self,
        account_sid: &str,
        request: &WindowsRunRequest,
    ) -> Result<(), String> {
        self.pin_path(&request.cwd)?;
        let executable = std::env::current_exe()
            .and_then(|path| path.canonicalize())
            .map_err(|error| format!("cannot resolve runtime executable: {error}"))?;
        super::path::reject_reparse_components(&executable)?;
        self.push_record(&executable, account_sid, AclAccess::Read, false)?;
        for sid in self.capability_sids.clone() {
            self.push_record(&executable, &sid, AclAccess::Read, false)?;
        }
        for root in &request.readable_roots {
            let root = canonical_existing(root)?;
            self.push_record(&root, account_sid, AclAccess::Read, false)?;
            self.push_record(&root, account_sid, AclAccess::DenyWrite, false)?;
            for sid in self.capability_sids.clone() {
                self.push_record(&root, &sid, AclAccess::Read, false)?;
                self.push_record(&root, &sid, AclAccess::DenyWrite, false)?;
            }
        }
        for (index, root) in request.writable_roots.iter().enumerate() {
            let root = canonical_existing(root)?;
            self.push_record(&root, account_sid, AclAccess::Write, false)?;
            self.push_record(&root, account_sid, AclAccess::DenyDeleteChild, false)?;
            let capability_sid = self.capability_sids[index].clone();
            self.push_record(&root, &capability_sid, AclAccess::Write, false)?;
            self.push_record(&root, &capability_sid, AclAccess::DenyDeleteChild, false)?;
            for name in PROTECTED_NAMES {
                let protected = root.join(name);
                reject_reparse_point(&protected)?;
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
                reject_reparse_point(&protected)?;
                if synthetic {
                    self.cleanup_paths.push(CleanupPath {
                        path: protected.clone(),
                        recursive: false,
                    });
                }
                self.push_record(&protected, account_sid, AclAccess::DenyWrite, synthetic)?;
                self.push_record(&protected, &capability_sid, AclAccess::DenyWrite, synthetic)?;
            }
        }
        let temp_dir = self.temp_dir.clone();
        self.push_record(&temp_dir, account_sid, AclAccess::Write, true)?;
        for sid in self.capability_sids.clone() {
            self.push_record(&temp_dir, &sid, AclAccess::Write, true)?;
        }
        for root in &request.denied_roots {
            // SQLite sidecars (WAL/SHM/journal) are created lazily. A missing
            // optional sidecar is not an ACL failure; required writable and
            // readable roots above still use canonical_existing and remain
            // fail-closed. Existing deny roots receive the same ACL records.
            let Some(root) = canonical_optional(root)? else {
                continue;
            };
            self.push_record(&root, account_sid, AclAccess::DenyAll, false)?;
            for sid in self.capability_sids.clone() {
                self.push_record(&root, &sid, AclAccess::DenyAll, false)?;
            }
        }
        Ok(())
    }

    fn push_record(
        &mut self,
        path: &Path,
        sid: &str,
        access: AclAccess,
        synthetic: bool,
    ) -> Result<(), String> {
        if !self
            .restores
            .iter()
            .any(|record| same_path(&record.path, path))
        {
            self.pin_path(path)?;
            let handle = self
                .pins
                .iter()
                .find(|pin| same_path(&pin.path, path))
                .map(|pin| pin.handle)
                .ok_or_else(|| format!("ACL target was not pinned: {}", path.display()))?;
            self.restores.push(AclRestoreRecord {
                path: path.to_path_buf(),
                dacl: snapshot_dacl_handle(handle, path)?,
                synthetic,
                identity: self
                    .pins
                    .iter()
                    .find(|pin| same_path(&pin.path, path))
                    .map(PinnedObject::identity),
            });
        }
        self.records.push(AclRecord {
            path: path.to_path_buf(),
            sid: sid.to_string(),
            access,
            synthetic,
        });
        Ok(())
    }

    fn pin_path(&mut self, path: &Path) -> Result<(), String> {
        if !self.pins.iter().any(|pin| same_path(&pin.path, path)) {
            self.pins.push(PinnedObject::open(path)?);
        }
        Ok(())
    }

    fn save_manifest(&self) -> Result<(), String> {
        save_manifest(
            &self.state_dir,
            &AclManifest {
                version: ACL_STATE_VERSION,
                restores: self.restores.clone(),
                cleanup_paths: self.cleanup_paths.clone(),
            },
        )
    }

    fn apply_records(&self) -> Result<(), String> {
        let mut refreshed_entries: Vec<(PathBuf, String)> = Vec::new();
        for record in &self.records {
            let sid = LocalSid::from_string(&record.sid)?;
            let pin = self
                .pins
                .iter()
                .find(|pin| same_path(&pin.path, &record.path))
                .ok_or_else(|| format!("ACL target was not pinned: {}", record.path.display()))?;
            let (mode, mask) = match record.access {
                AclAccess::Read => (SET_ACCESS, READ_MASK),
                AclAccess::Write => (SET_ACCESS, WRITE_MASK),
                AclAccess::DenyDeleteChild => (DENY_ACCESS, FILE_DELETE_CHILD),
                AclAccess::Deny | AclAccess::DenyWrite => (DENY_ACCESS, DENY_WRITE_MASK),
                AclAccess::DenyAll => (DENY_ACCESS, FILE_ALL_ACCESS),
            };
            if matches!(record.access, AclAccess::Write)
                && !refreshed_entries.contains(&(record.path.clone(), record.sid.clone()))
            {
                ensure_write_allow_ace_handle(pin.handle, &record.path, sid.as_ptr()).map_err(
                    |error| {
                        format!(
                            "cannot reconcile write ACL for {} (synthetic={}): {error}",
                            record.path.display(),
                            record.synthetic
                        )
                    },
                )?;
                refreshed_entries.push((record.path.clone(), record.sid.clone()));
                continue;
            }
            apply_entry_handle(pin.handle, &record.path, sid.as_ptr(), mode, mask).map_err(
                |error| {
                    format!(
                        "cannot apply {:?} ACL for {} (synthetic={}): {error}",
                        record.access,
                        record.path.display(),
                        record.synthetic
                    )
                },
            )?;
        }
        Ok(())
    }

    fn cleanup_internal(&mut self) -> Result<(), String> {
        if self.cleanup_attempted {
            return Ok(());
        }
        self.cleanup_attempted = true;
        let mut failures = Vec::new();
        if self.restores.is_empty() {
            // Compatibility for an in-memory lease created from the previous
            // revoke-only representation (and for its regression tests).
            for record in self.records.iter().rev() {
                match LocalSid::from_string(&record.sid) {
                    Ok(sid) => {
                        if let Err(error) = revoke_entry(&record.path, sid.as_ptr()) {
                            failures.push(format!("{}: {error}", record.path.display()));
                        }
                    }
                    Err(error) => failures.push(format!("{}: {error}", record.path.display())),
                }
            }
        } else {
            for record in self.restores.iter().rev() {
                let Some(pin) = self
                    .pins
                    .iter()
                    .find(|pin| same_path(&pin.path, &record.path))
                else {
                    failures.push(format!(
                        "{}: ACL restore target is not pinned",
                        record.path.display()
                    ));
                    continue;
                };
                if let Err(error) = restore_dacl_handle(pin.handle, &record.path, &record.dacl) {
                    failures.push(format!("{}: {error}", record.path.display()));
                }
            }
        }
        self.pins.clear();
        cleanup_paths(&self.cleanup_paths, &mut failures);

        let manifest = self.state_dir.join(ACL_STATE_FILE);
        let log_path = self.state_dir.join(ACL_CLEANUP_LOG);
        if failures.is_empty() {
            remove_optional_file(&manifest)
                .and_then(|()| remove_optional_file(&log_path))
                .map_err(|error| format!("cannot clear ACL recovery state: {error}"))?;
            Ok(())
        } else {
            let detail = failures.join("\n");
            let _ = super::state::write_file(&log_path, detail.as_bytes());
            Err(format!(
                "ACL restoration failed for {} object(s); recovery manifest retained",
                failures.len()
            ))
        }
    }
}

fn ensure_write_allow_ace_handle(
    handle: isize,
    path: &Path,
    sid: *mut c_void,
) -> Result<(), String> {
    let mut descriptor = std::ptr::null_mut();
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let status = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut dacl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!("cannot read ACL for {}: {status}", path.display()));
    }

    let needs_refresh = unsafe { write_allow_needs_refresh(dacl, sid) };
    if !needs_refresh {
        unsafe { LocalFree(descriptor as HLOCAL) };
        return Ok(());
    }

    let mut entry: EXPLICIT_ACCESS_W = unsafe { std::mem::zeroed() };
    entry.grfAccessPermissions = WRITE_MASK;
    entry.grfAccessMode = SET_ACCESS as i32;
    entry.grfInheritance = CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE;
    entry.Trustee = TRUSTEE_W {
        pMultipleTrustee: std::ptr::null_mut(),
        MultipleTrusteeOperation: 0,
        TrusteeForm: TRUSTEE_IS_SID,
        TrusteeType: TRUSTEE_IS_UNKNOWN,
        ptstrName: sid.cast(),
    };
    let mut new_acl: *mut ACL = std::ptr::null_mut();
    let merge = unsafe { SetEntriesInAclW(1, &entry, dacl, &mut new_acl) };
    let update = if merge == ERROR_SUCCESS {
        unsafe {
            SetSecurityInfo(
                handle,
                SE_FILE_OBJECT,
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
    if update == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(format!(
            "cannot update ACL for {}: {update}",
            path.display()
        ))
    }
}

unsafe fn write_allow_needs_refresh(dacl: *mut ACL, sid: *mut c_void) -> bool {
    if dacl.is_null() {
        return true;
    }
    let mut info: ACL_SIZE_INFORMATION = std::mem::zeroed();
    if GetAclInformation(
        dacl,
        (&mut info as *mut ACL_SIZE_INFORMATION).cast(),
        std::mem::size_of::<ACL_SIZE_INFORMATION>() as u32,
        AclSizeInformation,
    ) == 0
    {
        return true;
    }
    let mut has_write = false;
    let mapping = GENERIC_MAPPING {
        GenericRead: FILE_GENERIC_READ,
        GenericWrite: FILE_GENERIC_WRITE,
        GenericExecute: FILE_GENERIC_EXECUTE,
        GenericAll: FILE_ALL_ACCESS,
    };
    for index in 0..info.AceCount {
        let mut ace_ptr: *mut c_void = std::ptr::null_mut();
        if GetAce(dacl, index, &mut ace_ptr) == 0 || ace_ptr.is_null() {
            continue;
        }
        let header = &*(ace_ptr as *const ACE_HEADER);
        if header.AceType != 0 || header.AceFlags & INHERIT_ONLY_ACE != 0 {
            continue;
        }
        let ace = &*(ace_ptr as *const ACCESS_ALLOWED_ACE);
        let sid_ptr = std::ptr::addr_of!(ace.SidStart) as *mut c_void;
        if EqualSid(sid_ptr, sid) == 0 {
            continue;
        }
        let mut mask = ace.Mask;
        MapGenericMask(&mut mask, &mapping);
        // A direct allow ACE without both inheritance bits only makes the
        // root writable. Existing files/dirs then still lack DELETE, which
        // is exactly the approved-external-delete failure this reconciler
        // must repair on upgrades from the old ACL writer.
        if header.AceFlags & REQUIRED_WRITE_INHERIT_FLAGS != REQUIRED_WRITE_INHERIT_FLAGS {
            return true;
        }
        if (mask & WRITE_MASK) == WRITE_MASK {
            has_write = true;
        }
        if (header.AceFlags & INHERITED_ACE == 0) && (mask & STALE_DELETE_CHILD_MASK) != 0 {
            return true;
        }
    }
    !has_write
}

#[cfg(test)]
fn snapshot_dacl(path: &Path) -> Result<DaclSnapshot, String> {
    let pin = PinnedObject::open(path)?;
    snapshot_dacl_handle(pin.handle, path)
}

fn snapshot_dacl_handle(handle: isize, path: &Path) -> Result<DaclSnapshot, String> {
    let mut descriptor = std::ptr::null_mut();
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let status = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut dacl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!(
            "cannot snapshot ACL for {}: {status}",
            path.display()
        ));
    }
    let result = (|| {
        let mut present = 0;
        let mut defaulted = 0;
        if unsafe { GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted) }
            == 0
        {
            return Err(format!(
                "cannot inspect DACL presence for {}: {}",
                path.display(),
                unsafe { GetLastError() }
            ));
        }
        if present == 0 || dacl.is_null() {
            return Err(format!(
                "refusing to modify absent or null DACL for {}",
                path.display()
            ));
        }
        let mut info: ACL_SIZE_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe {
            GetAclInformation(
                dacl,
                (&mut info as *mut ACL_SIZE_INFORMATION).cast(),
                std::mem::size_of::<ACL_SIZE_INFORMATION>() as u32,
                AclSizeInformation,
            )
        } == 0
        {
            return Err(format!(
                "cannot size DACL for {}: {}",
                path.display(),
                unsafe { GetLastError() }
            ));
        }
        if info.AclBytesInUse < std::mem::size_of::<ACL>() as u32 {
            return Err(format!("invalid DACL size for {}", path.display()));
        }
        let bytes = unsafe {
            std::slice::from_raw_parts(dacl.cast::<u8>(), info.AclBytesInUse as usize).to_vec()
        };
        Ok(DaclSnapshot(bytes))
    })();
    unsafe { LocalFree(descriptor as HLOCAL) };
    result
}

#[cfg(test)]
fn restore_dacl(path: &Path, snapshot: &DaclSnapshot) -> Result<(), String> {
    let pin = PinnedObject::open(path)?;
    restore_dacl_handle(pin.handle, path, snapshot)
}

fn restore_dacl_handle(handle: isize, path: &Path, snapshot: &DaclSnapshot) -> Result<(), String> {
    if snapshot.0.len() < std::mem::size_of::<ACL>() {
        return Err(format!("invalid saved DACL for {}", path.display()));
    }
    let acl = snapshot.0.as_ptr() as *mut ACL;
    if unsafe { IsValidAcl(acl) } == 0 {
        return Err(format!("saved DACL is invalid for {}", path.display()));
    }
    let status = unsafe {
        SetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            acl,
            std::ptr::null_mut(),
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!(
            "cannot restore DACL for {}: {status}",
            path.display()
        ));
    }
    Ok(())
}

impl Drop for AclLease {
    fn drop(&mut self) {
        let _ = self.cleanup_internal();
    }
}

fn apply_entry(path: &Path, sid: *mut c_void, mode: u32, mask: u32) -> Result<(), String> {
    let pin = PinnedObject::open(path)?;
    apply_entry_handle(pin.handle, path, sid, mode, mask)
}

fn apply_entry_handle(
    handle: isize,
    path: &Path,
    sid: *mut c_void,
    mode: u32,
    mask: u32,
) -> Result<(), String> {
    let mut descriptor = std::ptr::null_mut();
    let mut old_acl: *mut ACL = std::ptr::null_mut();
    let status = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
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
            SetSecurityInfo(
                handle,
                SE_FILE_OBJECT,
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

pub(crate) fn recover_stale(state_dir: &Path) -> Result<(), String> {
    let _mutex = acquire_mutex()?;
    cleanup_stale(state_dir)
}

fn cleanup_stale(state_dir: &Path) -> Result<(), String> {
    let path = state_dir.join(ACL_STATE_FILE);
    let Some(bytes) = super::state::read_optional_file(&path)? else {
        return Ok(());
    };
    let mut failures = Vec::new();
    if let Ok(manifest) = serde_json::from_slice::<AclManifest>(&bytes) {
        if manifest.version != ACL_STATE_VERSION {
            return Err(format!(
                "unsupported stale ACL manifest version {}",
                manifest.version
            ));
        }
        restore_records(&manifest.restores, &mut failures);
        cleanup_paths(&manifest.cleanup_paths, &mut failures);
    } else {
        let records: Vec<AclRecord> = serde_json::from_slice(&bytes)
            .map_err(|error| format!("invalid stale ACL manifest: {error}"))?;
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
                if let Err(error) = remove_optional_directory(&record.path, false) {
                    failures.push(format!("{}: {error}", record.path.display()));
                }
            }
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
    remove_optional_file(&path)
        .and_then(|()| remove_optional_file(&state_dir.join(ACL_CLEANUP_LOG)))
        .map_err(|error| format!("cannot clear stale ACL recovery state: {error}"))
}

fn restore_records(records: &[AclRestoreRecord], failures: &mut Vec<String>) {
    for record in records.iter().rev() {
        if !record.path.exists() {
            failures.push(format!(
                "{}: ACL restore target is missing",
                record.path.display()
            ));
            continue;
        }
        let Some(expected_identity) = record.identity else {
            failures.push(format!(
                "{}: ACL restore target has no saved identity",
                record.path.display()
            ));
            continue;
        };
        let pin = match PinnedObject::open(&record.path) {
            Ok(pin) => pin,
            Err(error) => {
                failures.push(format!("{}: {error}", record.path.display()));
                continue;
            }
        };
        if pin.identity() != expected_identity {
            failures.push(format!(
                "{}: ACL restore target identity changed",
                record.path.display()
            ));
            continue;
        }
        if let Err(error) = restore_dacl_handle(pin.handle, &record.path, &record.dacl) {
            failures.push(format!("{}: {error}", record.path.display()));
        }
    }
}

fn cleanup_paths(paths: &[CleanupPath], failures: &mut Vec<String>) {
    for cleanup in paths.iter().rev() {
        if let Err(error) = remove_optional_directory(&cleanup.path, cleanup.recursive) {
            failures.push(format!("{}: {error}", cleanup.path.display()));
        }
    }
}

fn remove_optional_directory(path: &Path, recursive: bool) -> Result<(), String> {
    let result = if recursive {
        fs::remove_dir_all(path)
    } else {
        fs::remove_dir(path)
    };
    match result {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("cannot remove cleanup directory: {error}")),
    }
}

fn remove_optional_file(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

fn save_manifest(state_dir: &Path, manifest: &AclManifest) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(manifest)
        .map_err(|error| format!("cannot serialize ACL recovery manifest: {error}"))?;
    super::state::write_file(&state_dir.join(ACL_STATE_FILE), &bytes)
        .map_err(|error| format!("cannot persist ACL recovery manifest: {error}"))
}

// Legacy serializer retained solely for stale-manifest recovery tests.
#[cfg(test)]
fn save_records(state_dir: &Path, records: &[AclRecord]) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(records)
        .map_err(|error| format!("cannot serialize ACL manifest: {error}"))?;
    super::state::write_file(&state_dir.join(ACL_STATE_FILE), &bytes)
        .map_err(|error| format!("cannot persist ACL manifest: {error}"))
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
    super::path::reject_reparse_components(path)?;
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve ACL root {}: {error}", path.display()))?;
    super::path::reject_reparse_components(&canonical)?;
    Ok(canonical)
}

fn canonical_optional(path: &Path) -> Result<Option<PathBuf>, String> {
    super::path::reject_reparse_components(path)?;
    match path.canonicalize() {
        Ok(path) => {
            super::path::reject_reparse_components(&path)?;
            Ok(Some(path))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "cannot resolve ACL root {}: {error}",
            path.display()
        )),
    }
}

fn create_run_directories(state_dir: &Path) -> Result<(PathBuf, PathBuf), String> {
    let runs = state_dir.join(RUNS_DIRECTORY);
    super::state::prepare_directory(&runs)?;
    for _ in 0..16 {
        let run_dir = runs.join(format!(
            "{:08x}-{:032x}",
            std::process::id(),
            thread_rng().gen::<u128>()
        ));
        match fs::create_dir(&run_dir) {
            Ok(()) => {
                if let Err(error) = super::state::prepare_directory(&run_dir) {
                    let _ = fs::remove_dir(&run_dir);
                    return Err(error);
                }
                let temp_dir = run_dir.join("temp");
                if let Err(error) = super::state::prepare_directory(&temp_dir) {
                    let _ = fs::remove_dir_all(&run_dir);
                    return Err(error);
                }
                return Ok((run_dir, temp_dir));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "cannot create per-run Windows sandbox directory: {error}"
                ))
            }
        }
    }
    Err("cannot allocate a unique per-run Windows sandbox directory".to_string())
}

fn same_path(left: &Path, right: &Path) -> bool {
    left.as_os_str()
        .to_string_lossy()
        .eq_ignore_ascii_case(&right.as_os_str().to_string_lossy())
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
    fn stale_snapshot_manifest_restores_acl_before_readiness() {
        let state = tempfile::tempdir().unwrap();
        super::super::state::prepare_directory(state.path()).unwrap();
        let target = state.path().join("target");
        fs::create_dir(&target).unwrap();
        let original = snapshot_dacl(&target).unwrap();
        let identity = PinnedObject::open(&target).unwrap().identity();
        let sid = LocalSid::from_string(&random_capability_sid()).unwrap();
        apply_entry(&target, sid.as_ptr(), SET_ACCESS, WRITE_MASK).unwrap();
        assert_ne!(snapshot_dacl(&target).unwrap(), original);
        save_manifest(
            state.path(),
            &AclManifest {
                version: ACL_STATE_VERSION,
                restores: vec![AclRestoreRecord {
                    path: target.clone(),
                    dacl: original.clone(),
                    synthetic: false,
                    identity: Some(identity),
                }],
                cleanup_paths: Vec::new(),
            },
        )
        .unwrap();

        recover_stale(state.path()).unwrap();

        assert_eq!(snapshot_dacl(&target).unwrap(), original);
        assert!(!state.path().join(ACL_STATE_FILE).exists());
    }

    #[test]
    fn stale_snapshot_manifest_rejects_a_replaced_target() {
        let state = tempfile::tempdir().unwrap();
        super::super::state::prepare_directory(state.path()).unwrap();
        let target = state.path().join("target");
        let displaced = state.path().join("displaced");
        fs::create_dir(&target).unwrap();
        let original = snapshot_dacl(&target).unwrap();
        let pin = PinnedObject::open(&target).unwrap();
        let identity = pin.identity();
        drop(pin);
        let capability = LocalSid::from_string(&random_capability_sid()).unwrap();
        apply_entry(&target, capability.as_ptr(), SET_ACCESS, WRITE_MASK).unwrap();
        fs::rename(&target, &displaced).unwrap();
        fs::create_dir(&target).unwrap();
        let replacement_sid = LocalSid::from_string(&random_capability_sid()).unwrap();
        apply_entry(&target, replacement_sid.as_ptr(), SET_ACCESS, READ_MASK).unwrap();
        let replacement = snapshot_dacl(&target).unwrap();
        save_manifest(
            state.path(),
            &AclManifest {
                version: ACL_STATE_VERSION,
                restores: vec![AclRestoreRecord {
                    path: target.clone(),
                    dacl: original,
                    synthetic: false,
                    identity: Some(identity),
                }],
                cleanup_paths: Vec::new(),
            },
        )
        .unwrap();

        let error = recover_stale(state.path()).unwrap_err();

        assert!(error.contains("stale ACL cleanup failed"));
        let cleanup_log = String::from_utf8(
            super::super::state::read_file(&state.path().join(ACL_CLEANUP_LOG)).unwrap(),
        )
        .unwrap();
        assert!(cleanup_log.contains("identity"));
        assert_eq!(snapshot_dacl(&target).unwrap(), replacement);
        assert!(state.path().join(ACL_STATE_FILE).exists());
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
            restores: Vec::new(),
            pins: Vec::new(),
            capability_sids: Vec::new(),
            synthetic_protected: Vec::new(),
            cleanup_paths: Vec::new(),
            temp_dir: state.path().join("temp"),
            cleanup_attempted: false,
            _mutex: AclMutex(0),
        };

        drop(lease);

        assert!(state.path().join(ACL_STATE_FILE).exists());
        assert!(state.path().join(ACL_CLEANUP_LOG).exists());
    }

    #[test]
    fn dacl_snapshot_round_trip_restores_the_exact_original_acl() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("target");
        fs::create_dir(&target).unwrap();
        let original = snapshot_dacl(&target).unwrap();
        let sid = LocalSid::from_string(&random_capability_sid()).unwrap();

        apply_entry(&target, sid.as_ptr(), SET_ACCESS, WRITE_MASK).unwrap();
        assert_ne!(snapshot_dacl(&target).unwrap(), original);
        restore_dacl(&target, &original).unwrap();

        assert_eq!(snapshot_dacl(&target).unwrap(), original);
    }

    #[test]
    fn parent_dacl_restore_removes_inherited_capability_from_existing_child() {
        let root = tempfile::tempdir().unwrap();
        let parent = root.path().join("parent");
        let child = parent.join("child.txt");
        fs::create_dir(&parent).unwrap();
        fs::write(&child, b"child").unwrap();
        let parent_original = snapshot_dacl(&parent).unwrap();
        let child_original = snapshot_dacl(&child).unwrap();
        let sid = LocalSid::from_string(&random_capability_sid()).unwrap();

        apply_entry(&parent, sid.as_ptr(), SET_ACCESS, WRITE_MASK).unwrap();
        assert_ne!(snapshot_dacl(&child).unwrap(), child_original);
        restore_dacl(&parent, &parent_original).unwrap();

        assert_eq!(snapshot_dacl(&child).unwrap(), child_original);
    }

    #[test]
    fn pinned_acl_target_detects_a_rename_before_launch() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("target");
        let renamed = root.path().join("renamed");
        fs::create_dir(&target).unwrap();

        let pin = PinnedObject::open(&target).unwrap();
        fs::rename(&target, &renamed).unwrap();
        assert!(pin.verify_path_identity().is_err());
        drop(pin);
    }
}
