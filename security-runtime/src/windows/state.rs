//! Host-owned Windows security state objects.

use std::ffi::c_void;
use std::fs;
use std::io::Write;
use std::os::windows::fs::MetadataExt;
use std::os::windows::io::AsRawHandle;
use std::path::Path;

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_SUCCESS, HLOCAL};
use windows_sys::Win32::Security::Authorization::{
    GetNamedSecurityInfoW, SetEntriesInAclW, SetNamedSecurityInfoW, EXPLICIT_ACCESS_W,
    TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    AclSizeInformation, CreateWellKnownSid, EqualSid, GetAce, GetAclInformation, GetLengthSid,
    GetSecurityDescriptorControl, GetTokenInformation, TokenUser, ACCESS_ALLOWED_ACE, ACE_HEADER,
    ACL, ACL_SIZE_INFORMATION, CONTAINER_INHERIT_ACE, DACL_SECURITY_INFORMATION,
    OBJECT_INHERIT_ACE, OWNER_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION,
    SE_DACL_PROTECTED, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, FILE_ALL_ACCESS,
    FILE_ATTRIBUTE_REPARSE_POINT,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const SE_FILE_OBJECT: i32 = 1;
const WIN_LOCAL_SYSTEM_SID: i32 = 22;
const WIN_BUILTIN_ADMINISTRATORS_SID: i32 = 26;
const SET_ACCESS: i32 = 2;
const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;

/// Create the state directory used by identity and ACL persistence.
pub(crate) fn prepare_directory(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    reject_reparse_components(path)?;
    fs::create_dir_all(path)
        .map_err(|error| format!("cannot create security state directory: {error}"))?;
    reject_reparse_components(path)?;
    protect_host_object(path, true)
}

fn reject_reparse_components(path: &Path) -> Result<(), String> {
    for component in path.ancestors().collect::<Vec<_>>().into_iter().rev() {
        let metadata = match fs::symlink_metadata(component) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(format!(
                    "cannot inspect security state path {}: {error}",
                    component.display()
                ))
            }
        };
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!(
                "security state path cannot contain a reparse point: {}",
                component.display()
            ));
        }
    }
    Ok(())
}

pub(crate) fn protect_file(path: &Path) -> Result<(), String> {
    reject_reparse_components(path)?;
    reject_multiple_links(path)?;
    protect_host_object(path, false)
}

pub(crate) fn protect_optional_file(path: &Path) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(_) => protect_file(path),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "cannot inspect legacy security state {}: {error}",
            path.display()
        )),
    }
}

pub(crate) fn write_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "security state file has no parent directory".to_string())?;
    prepare_directory(parent)?;
    reject_existing_file(path)?;

    let temporary = path.with_extension("tmp");
    remove_stale_file(&temporary, "temporary")?;
    let mut temporary_file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| format!("cannot create security state temporary file: {error}"))?;
    if let Err(error) = protect_file(&temporary) {
        drop(temporary_file);
        let _ = fs::remove_file(&temporary);
        return Err(format!(
            "cannot protect security state temporary file before writing: {error}"
        ));
    }
    if let Err(error) = temporary_file
        .write_all(bytes)
        .and_then(|()| temporary_file.sync_all())
    {
        drop(temporary_file);
        let _ = fs::remove_file(&temporary);
        return Err(format!("cannot write security state: {error}"));
    }
    drop(temporary_file);

    let mut backup = None;
    if path.exists() {
        let backup_path = path.with_extension("bak");
        remove_stale_file(&backup_path, "backup")?;
        fs::rename(path, &backup_path)
            .map_err(|error| format!("cannot backup security state: {error}"))?;
        backup = Some(backup_path);
    }
    if let Err(error) = fs::rename(&temporary, path) {
        if let Some(backup_path) = &backup {
            let _ = fs::rename(backup_path, path);
        }
        return Err(format!("cannot publish security state: {error}"));
    }
    if let Err(error) = protect_file(path) {
        let _ = fs::remove_file(path);
        if let Some(backup_path) = &backup {
            let _ = fs::rename(backup_path, path);
        }
        return Err(format!(
            "cannot verify published security state protection: {error}"
        ));
    }
    if let Some(backup_path) = backup {
        fs::remove_file(backup_path)
            .map_err(|error| format!("cannot remove security state backup: {error}"))?;
    }
    Ok(())
}

pub(crate) fn read_file(path: &Path) -> Result<Vec<u8>, String> {
    reject_reparse_components(path)?;
    reject_multiple_links(path)?;
    verify_protected_host_object(path)?;
    fs::read(path).map_err(|error| {
        format!(
            "cannot read protected security state {}: {error}",
            path.display()
        )
    })
}

pub(crate) fn read_optional_file(path: &Path) -> Result<Option<Vec<u8>>, String> {
    match fs::symlink_metadata(path) {
        Ok(_) => read_file(path).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "cannot inspect protected security state {}: {error}",
            path.display()
        )),
    }
}

fn reject_existing_file(path: &Path) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(format!(
                    "security state file cannot be a reparse point: {}",
                    path.display()
                ));
            }
            reject_multiple_links(path)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "cannot inspect security state file {}: {error}",
            path.display()
        )),
    }
}

fn reject_multiple_links(path: &Path) -> Result<(), String> {
    let file = fs::File::open(path).map_err(|error| {
        format!(
            "cannot open security state file {}: {error}",
            path.display()
        )
    })?;
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as isize, &mut information) } == 0 {
        return Err(format!(
            "cannot inspect security state file identity {}: {}",
            path.display(),
            unsafe { GetLastError() }
        ));
    }
    if information.nNumberOfLinks != 1 {
        return Err(format!(
            "security state file has multiple hard links: {}",
            path.display()
        ));
    }
    Ok(())
}

fn remove_stale_file(path: &Path, label: &str) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "cannot clear stale security state {label}: {error}"
        )),
    }
}

fn protect_host_object(path: &Path, container: bool) -> Result<(), String> {
    let sids = host_sids()?;
    let inheritance = if container {
        CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE
    } else {
        0
    };
    let entries = [
        trustee_entry(sids.user.as_ptr() as *mut c_void, inheritance),
        trustee_entry(sids.system.as_ptr() as *mut c_void, inheritance),
        trustee_entry(sids.admins.as_ptr() as *mut c_void, inheritance),
    ];
    let mut new_dacl: *mut ACL = std::ptr::null_mut();
    let merge = unsafe {
        SetEntriesInAclW(
            entries.len() as u32,
            entries.as_ptr(),
            std::ptr::null_mut(),
            &mut new_dacl,
        )
    };
    if merge != ERROR_SUCCESS {
        return Err(format!("SetEntriesInAclW failed: {merge}"));
    }
    let mut path_wide = super::identity::wide(path.as_os_str());
    let update = unsafe {
        SetNamedSecurityInfoW(
            path_wide.as_mut_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION
                | DACL_SECURITY_INFORMATION
                | PROTECTED_DACL_SECURITY_INFORMATION,
            sids.user.as_ptr() as *mut c_void,
            std::ptr::null_mut(),
            new_dacl,
            std::ptr::null_mut(),
        )
    };
    unsafe { LocalFree(new_dacl as HLOCAL) };
    if update != ERROR_SUCCESS {
        return Err(format!("SetNamedSecurityInfoW failed: {update}"));
    }
    verify_protected_host_object(path)
}

#[cfg(test)]
fn verify_host_only_dacl(path: &Path) -> Result<(), String> {
    verify_host_security(path, false)
}

fn verify_protected_host_object(path: &Path) -> Result<(), String> {
    verify_host_security(path, true)
}

fn verify_host_security(path: &Path, require_protected_owner: bool) -> Result<(), String> {
    let sids = host_sids()?;
    let expected = [
        sids.user.as_ptr() as *mut c_void,
        sids.system.as_ptr() as *mut c_void,
        sids.admins.as_ptr() as *mut c_void,
    ];
    let path_wide = super::identity::wide(path.as_os_str());
    let mut owner: *mut c_void = std::ptr::null_mut();
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut descriptor: *mut c_void = std::ptr::null_mut();
    let status = unsafe {
        GetNamedSecurityInfoW(
            path_wide.as_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            std::ptr::null_mut(),
            &mut dacl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!(
            "cannot read security state DACL for {}: {status}",
            path.display()
        ));
    }
    let result = (|| {
        if require_protected_owner {
            if owner.is_null() || unsafe { EqualSid(owner, expected[0]) } == 0 {
                return Err(format!(
                    "security state owner mismatch for {}",
                    path.display()
                ));
            }
            let mut control = 0;
            let mut revision = 0;
            if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0
                || control & SE_DACL_PROTECTED == 0
            {
                return Err(format!(
                    "security state DACL is not protected for {}",
                    path.display()
                ));
            }
        }
        if dacl.is_null() {
            return Err(format!(
                "security state DACL is null for {}",
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
                "cannot inspect security state DACL for {}",
                path.display()
            ));
        }
        if info.AceCount != expected.len() as u32 {
            return Err(format!(
                "security state DACL has unexpected principals for {}",
                path.display()
            ));
        }
        let mut seen = [false; 3];
        for index in 0..info.AceCount {
            let mut ace_ptr: *mut c_void = std::ptr::null_mut();
            if unsafe { GetAce(dacl, index, &mut ace_ptr) } == 0 {
                return Err(format!(
                    "cannot read security state ACE for {}",
                    path.display()
                ));
            }
            let header = unsafe { &*(ace_ptr as *const ACE_HEADER) };
            if header.AceType != ACCESS_ALLOWED_ACE_TYPE {
                return Err(format!(
                    "security state DACL contains a non-allow ACE for {}",
                    path.display()
                ));
            }
            let ace = unsafe { &*(ace_ptr as *const ACCESS_ALLOWED_ACE) };
            if ace.Mask != FILE_ALL_ACCESS {
                return Err(format!(
                    "security state ACE has an unexpected mask for {}",
                    path.display()
                ));
            }
            let sid = std::ptr::addr_of!(ace.SidStart) as *mut c_void;
            let Some(position) = expected
                .iter()
                .position(|candidate| unsafe { EqualSid(sid, *candidate) } != 0)
            else {
                return Err(format!(
                    "security state DACL contains an unexpected SID for {}",
                    path.display()
                ));
            };
            if seen[position] {
                return Err(format!(
                    "security state DACL contains a duplicate SID for {}",
                    path.display()
                ));
            }
            seen[position] = true;
        }
        Ok(())
    })();
    unsafe { LocalFree(descriptor as HLOCAL) };
    result
}

struct HostSids {
    user: Vec<u8>,
    system: Vec<u8>,
    admins: Vec<u8>,
}

fn host_sids() -> Result<HostSids, String> {
    Ok(HostSids {
        user: current_user_sid()?,
        system: well_known_sid(WIN_LOCAL_SYSTEM_SID)?,
        admins: well_known_sid(WIN_BUILTIN_ADMINISTRATORS_SID)?,
    })
}

fn trustee_entry(sid: *mut c_void, inheritance: u32) -> EXPLICIT_ACCESS_W {
    let mut explicit: EXPLICIT_ACCESS_W = unsafe { std::mem::zeroed() };
    explicit.grfAccessPermissions = FILE_ALL_ACCESS;
    explicit.grfAccessMode = SET_ACCESS;
    explicit.grfInheritance = inheritance;
    explicit.Trustee = TRUSTEE_W {
        pMultipleTrustee: std::ptr::null_mut(),
        MultipleTrusteeOperation: 0,
        TrusteeForm: TRUSTEE_IS_SID,
        TrusteeType: TRUSTEE_IS_UNKNOWN,
        ptstrName: sid as *mut u16,
    };
    explicit
}

fn well_known_sid(kind: i32) -> Result<Vec<u8>, String> {
    let mut size = 0;
    unsafe {
        CreateWellKnownSid(kind, std::ptr::null_mut(), std::ptr::null_mut(), &mut size);
    }
    if size == 0 {
        return Err(format!("CreateWellKnownSid({kind}) sizing failed"));
    }
    let mut buffer = vec![0_u8; size as usize];
    if unsafe {
        CreateWellKnownSid(
            kind,
            std::ptr::null_mut(),
            buffer.as_mut_ptr().cast(),
            &mut size,
        )
    } == 0
    {
        return Err(format!("CreateWellKnownSid({kind}) failed: {}", unsafe {
            GetLastError()
        }));
    }
    Ok(buffer)
}

fn current_user_sid() -> Result<Vec<u8>, String> {
    let mut token = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(format!("OpenProcessToken failed: {}", unsafe {
            GetLastError()
        }));
    }
    let result = (|| {
        let mut size = 0;
        unsafe {
            GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut size);
        }
        if size == 0 {
            return Err("GetTokenInformation sizing failed".to_string());
        }
        let mut buffer = vec![0_u8; size as usize];
        if unsafe {
            GetTokenInformation(
                token,
                TokenUser,
                buffer.as_mut_ptr().cast(),
                size,
                &mut size,
            )
        } == 0
        {
            return Err(format!("GetTokenInformation failed: {}", unsafe {
                GetLastError()
            }));
        }
        let user = unsafe { &*(buffer.as_ptr() as *const TOKEN_USER) };
        if user.User.Sid.is_null() {
            return Err("token user SID is null".to_string());
        }
        let length = unsafe { GetLengthSid(user.User.Sid) } as usize;
        let mut sid = vec![0_u8; length];
        unsafe {
            std::ptr::copy_nonoverlapping(user.User.Sid as *const u8, sid.as_mut_ptr(), length);
        }
        Ok(sid)
    })();
    unsafe { CloseHandle(token) };
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn state_directory_rejects_a_junction() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("target");
        let junction = root.path().join("state");
        fs::create_dir(&target).unwrap();
        let status = Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&junction)
            .arg(&target)
            .status()
            .unwrap();
        assert!(status.success(), "junction setup failed");

        let error = prepare_directory(&junction).unwrap_err();

        assert!(error.contains("reparse point"));
        fs::remove_dir(&junction).unwrap();
    }

    #[test]
    fn state_directory_protects_itself_and_inherited_children() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");

        prepare_directory(&state).unwrap();
        verify_host_only_dacl(&state).unwrap();

        let manifest = state.join("manifest.json");
        fs::write(&manifest, b"{}").unwrap();
        verify_host_only_dacl(&manifest).unwrap();
        protect_optional_file(&manifest).unwrap();
        verify_protected_host_object(&manifest).unwrap();
    }

    #[test]
    fn protected_writer_rejects_a_hardlinked_destination() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        prepare_directory(&state).unwrap();
        let outside = root.path().join("outside.json");
        fs::write(&outside, b"outside").unwrap();
        let manifest = state.join("manifest.json");
        fs::hard_link(&outside, &manifest).unwrap();

        let error = write_file(&manifest, b"replacement").unwrap_err();

        assert!(error.contains("multiple hard links"));
        assert_eq!(fs::read(&outside).unwrap(), b"outside");
    }

    #[test]
    fn protected_reader_rejects_a_hardlinked_state_file() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        prepare_directory(&state).unwrap();
        let outside = root.path().join("outside.json");
        fs::write(&outside, b"outside").unwrap();
        let manifest = state.join("manifest.json");
        fs::hard_link(&outside, &manifest).unwrap();

        let error = read_file(&manifest).unwrap_err();

        assert!(error.contains("multiple hard links"));
    }

    #[test]
    fn protected_writer_replaces_existing_state_and_remains_readable() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        prepare_directory(&state).unwrap();
        let manifest = state.join("manifest.json");

        write_file(&manifest, b"first").unwrap();
        write_file(&manifest, b"second").unwrap();

        assert_eq!(read_file(&manifest).unwrap(), b"second");
        assert!(!manifest.with_extension("tmp").exists());
        assert!(!manifest.with_extension("bak").exists());
    }
}
