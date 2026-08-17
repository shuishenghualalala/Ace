use rand::distributions::Alphanumeric;
use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::OsStr;
use std::fs;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_ACCESS_DENIED, HLOCAL,
};
use windows_sys::Win32::NetworkManagement::NetManagement::{
    NERR_Success, NERR_UserNotFound, NetUserAdd, NetUserDel, UF_DONT_EXPIRE_PASSWD,
    UF_PASSWD_CANT_CHANGE, UF_SCRIPT, USER_INFO_1, USER_PRIV_USER,
};
use windows_sys::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN,
    CRYPT_INTEGER_BLOB,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const IDENTITY_VERSION: u16 = 3;
const IDENTITY_FILE: &str = "windows-sandbox-identity.json";

#[derive(Debug, Serialize, Deserialize)]
struct StoredIdentity {
    version: u16,
    username: String,
    protected_password: Vec<u8>,
    online_username: String,
    online_protected_password: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct SandboxCredentials {
    pub username: String,
    pub password: String,
}

/// Installer-only elevated setup. Runtime never invokes this automatically.
pub fn setup(state_dir: &Path) -> Result<(), String> {
    super::path::validate_local_absolute(state_dir)?;
    require_elevated()?;
    validate_runtime_location()?;
    super::state::prepare_directory(state_dir)?;
    protect_legacy_identity(state_dir)?;
    super::acl::protect_legacy_state(state_dir)?;
    recover_identity_backup(state_dir)?;
    if super::state::read_optional_file(&identity_path(state_dir))?.is_some() {
        let offline = load(state_dir)?;
        let online = load_online(state_dir)?;
        super::token::sid_string_for_account(&offline.username)?;
        super::token::sid_string_for_account(&online.username)?;
        super::users::hide_sandbox_users(&[&offline.username, &online.username])?;
        super::wfp::install(&offline.username, &online.username)?;
        return Ok(());
    }
    let suffix: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(10)
        .map(char::from)
        .collect();
    let username = format!("AceSbOff_{suffix}");
    let online_username = format!("AceSbNet_{suffix}");
    let password: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(48)
        .map(char::from)
        .collect();
    create_local_user(&username, &password)?;
    let online_password: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(48)
        .map(char::from)
        .collect();
    if let Err(error) = create_local_user(&online_username, &online_password) {
        return Err(with_account_cleanup(error, &[&username]));
    }
    if let Err(error) = super::users::hide_sandbox_users(&[&username, &online_username]) {
        let account_cleanup = cleanup_accounts(&[&username, &online_username]);
        return Err(match account_cleanup {
            Ok(()) => error,
            Err(cleanup) => format!("{error}; account rollback failed: {cleanup}"),
        });
    }
    let result = (|| {
        let stored = StoredIdentity {
            version: IDENTITY_VERSION,
            username: username.clone(),
            protected_password: protect(password.as_bytes())?,
            online_username: online_username.clone(),
            online_protected_password: protect(online_password.as_bytes())?,
        };
        let bytes = serde_json::to_vec_pretty(&stored)
            .map_err(|error| format!("cannot serialize sandbox identity: {error}"))?;
        write_protected_identity(&identity_path(state_dir), &bytes)?;
        Ok(())
    })();
    if let Err(error) = result {
        let identity_cleanup = remove_optional_identity(&identity_path(state_dir));
        let visibility_cleanup = super::users::unhide_sandbox_users(&[&username, &online_username]);
        let account_cleanup = cleanup_accounts(&[&username, &online_username]);
        return Err(
            match (identity_cleanup, visibility_cleanup, account_cleanup) {
                (Ok(()), Ok(()), Ok(())) => error,
                (identity, visibility, accounts) => format!(
                    "{error}; setup rollback failed: identity={}; user_visibility={}; accounts={}",
                    result_label(identity),
                    result_label(visibility),
                    result_label(accounts)
                ),
            },
        );
    }
    if let Err(error) = super::wfp::install(&username, &online_username) {
        let wfp_cleanup = super::wfp::uninstall();
        let identity_cleanup = remove_optional_identity(&identity_path(state_dir));
        let visibility_cleanup = super::users::unhide_sandbox_users(&[&username, &online_username]);
        let account_cleanup = cleanup_accounts(&[&username, &online_username]);
        let error = match wfp_cleanup {
            Ok(()) => error,
            Err(cleanup) => format!("{error}; WFP rollback failed: {cleanup}"),
        };
        return Err(
            match (identity_cleanup, visibility_cleanup, account_cleanup) {
                (Ok(()), Ok(()), Ok(())) => error,
                (identity, visibility, accounts) => format!(
                    "{error}; setup rollback failed: identity={}; user_visibility={}; accounts={}",
                    result_label(identity),
                    result_label(visibility),
                    result_label(accounts)
                ),
            },
        );
    }
    Ok(())
}

pub fn load(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, false)
}

pub fn load_online(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, true)
}

/// Elevated uninstall removes only stable Ace WFP objects and current Ace
/// sandbox accounts recorded in the identity file.
pub fn uninstall(state_dir: &Path) -> Result<(), String> {
    super::path::validate_local_absolute(state_dir)?;
    require_elevated()?;
    if state_dir.exists() {
        super::state::prepare_directory(state_dir)?;
        protect_legacy_identity(state_dir)?;
        super::acl::protect_legacy_state(state_dir)?;
    }
    recover_identity_backup(state_dir)?;
    let path = identity_path(state_dir);
    let usernames: Vec<String> = if super::state::read_optional_file(&path)?.is_some() {
        stored_usernames(state_dir)?
    } else {
        Vec::new()
    };
    for username in &usernames {
        if !is_sandbox_account_name(username) {
            return Err("sandbox identity contains a non-Ace account name".to_string());
        }
    }
    let username_refs = usernames.iter().map(String::as_str).collect::<Vec<_>>();
    // Remove the kernel network boundary before deleting or un-hiding the
    // accounts it names. If WFP teardown fails, leave the identity and users
    // intact so a later elevated recovery can retry with the same principals.
    super::wfp::uninstall()?;
    super::users::unhide_sandbox_users(&username_refs)?;
    cleanup_accounts(&username_refs)?;
    if !usernames.is_empty() {
        fs::remove_file(path)
            .map_err(|error| format!("cannot remove sandbox identity: {error}"))?;
    }
    Ok(())
}

fn load_named(state_dir: &Path, online: bool) -> Result<SandboxCredentials, String> {
    let bytes = super::state::read_file(&identity_path(state_dir))
        .map_err(|error| format!("Windows sandbox identity is not installed: {error}"))?;
    let stored: StoredIdentity = serde_json::from_slice(&bytes)
        .map_err(|error| format!("Windows sandbox identity is invalid: {error}"))?;
    if stored.version != IDENTITY_VERSION
        || !valid_offline_account(&stored.username)
        || !valid_online_account(&stored.online_username)
    {
        return Err("Windows sandbox identity version mismatch".to_string());
    }
    let protected_password = if online {
        &stored.online_protected_password
    } else {
        &stored.protected_password
    };
    let password = String::from_utf8(unprotect(protected_password)?)
        .map_err(|_| "Windows sandbox password is not valid UTF-8".to_string())?;
    Ok(SandboxCredentials {
        username: if online {
            stored.online_username
        } else {
            stored.username
        },
        password,
    })
}

/// Current account names only.
fn valid_offline_account(value: &str) -> bool {
    valid_generated_account(value, "AceSbOff_")
}

fn valid_online_account(value: &str) -> bool {
    valid_generated_account(value, "AceSbNet_")
}

fn valid_generated_account(value: &str, prefix: &str) -> bool {
    value.strip_prefix(prefix).is_some_and(|suffix| {
        suffix.len() == 10 && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
    })
}

/// Guards ``delete_local_user`` so only Ace sandbox accounts are ever removed,
/// even if the identity file is corrupt.
fn is_sandbox_account_name(value: &str) -> bool {
    valid_offline_account(value) || valid_online_account(value)
}

fn stored_usernames(state_dir: &Path) -> Result<Vec<String>, String> {
    let bytes = super::state::read_file(&identity_path(state_dir))
        .map_err(|error| format!("Windows sandbox identity is not installed: {error}"))?;
    let stored: StoredIdentity = serde_json::from_slice(&bytes)
        .map_err(|error| format!("Windows sandbox identity is invalid: {error}"))?;
    if stored.version != IDENTITY_VERSION
        || !valid_offline_account(&stored.username)
        || !valid_online_account(&stored.online_username)
    {
        return Err("Windows sandbox identity version mismatch".to_string());
    }
    Ok(vec![stored.username, stored.online_username])
}

fn delete_local_user(username: &str) -> Result<(), String> {
    let username = wide(username);
    let status = unsafe { NetUserDel(std::ptr::null(), username.as_ptr()) };
    if status == NERR_Success || status == NERR_UserNotFound {
        Ok(())
    } else {
        Err(format!(
            "cannot remove Windows sandbox account: status={status}"
        ))
    }
}

fn cleanup_accounts(usernames: &[&str]) -> Result<(), String> {
    let mut failures = Vec::new();
    for username in usernames {
        if let Err(error) = delete_local_user(username) {
            failures.push(error);
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}

fn with_account_cleanup(error: String, usernames: &[&str]) -> String {
    match cleanup_accounts(usernames) {
        Ok(()) => error,
        Err(cleanup) => format!("{error}; account rollback failed: {cleanup}"),
    }
}

fn remove_optional_identity(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("cannot remove sandbox identity state: {error}")),
    }
}

fn result_label(result: Result<(), String>) -> String {
    match result {
        Ok(()) => "ok".to_string(),
        Err(error) => error,
    }
}

fn create_local_user(username: &str, password: &str) -> Result<(), String> {
    let mut username = wide(username);
    let mut password = wide(password);
    let mut info = USER_INFO_1 {
        usri1_name: username.as_mut_ptr(),
        usri1_password: password.as_mut_ptr(),
        usri1_password_age: 0,
        usri1_priv: USER_PRIV_USER,
        usri1_home_dir: std::ptr::null_mut(),
        usri1_comment: std::ptr::null_mut(),
        usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_PASSWD_CANT_CHANGE,
        usri1_script_path: std::ptr::null_mut(),
    };
    let mut parameter_error = 0_u32;
    let status = unsafe {
        NetUserAdd(
            std::ptr::null(),
            1,
            (&mut info as *mut USER_INFO_1).cast(),
            &mut parameter_error,
        )
    };
    if status == NERR_Success {
        Ok(())
    } else if status == ERROR_ACCESS_DENIED {
        Err("Windows sandbox setup requires an explicitly approved elevated installer".to_string())
    } else {
        Err(format!(
            "cannot create Windows sandbox account: status={status}, parameter={parameter_error}"
        ))
    }
}

fn require_elevated() -> Result<(), String> {
    let mut token = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(format!(
            "cannot inspect Windows setup elevation: {}",
            unsafe { GetLastError() }
        ));
    }
    let mut elevation: TOKEN_ELEVATION = unsafe { std::mem::zeroed() };
    let mut size = 0;
    let ok = unsafe {
        GetTokenInformation(
            token,
            TokenElevation,
            (&mut elevation as *mut TOKEN_ELEVATION).cast(),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut size,
        )
    };
    let last_error = if ok == 0 {
        unsafe { GetLastError() }
    } else {
        0
    };
    unsafe { CloseHandle(token) };
    if ok == 0 {
        return Err(format!(
            "cannot inspect Windows setup elevation: {}",
            last_error
        ));
    }
    if elevation.TokenIsElevated == 0 {
        return Err(
            "Windows sandbox setup requires an explicitly approved elevated installer".to_string(),
        );
    }
    Ok(())
}

pub(crate) fn validate_runtime_location() -> Result<(), String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("cannot locate runtime: {error}"))?;
    super::path::reject_reparse_components(&executable)?;
    let executable = executable
        .canonicalize()
        .map_err(|error| format!("cannot resolve runtime location: {error}"))?;
    super::path::reject_reparse_components(&executable)?;
    if !executable.is_file() {
        return Err("Windows runtime location is not a regular file".to_string());
    }
    Ok(())
}

fn protect(bytes: &[u8]) -> Result<Vec<u8>, String> {
    crypt(bytes, true)
}

// SECURITY NOTE (audit M-4): LOCAL_MACHINE means any local principal that can read the
// identity file + call CryptUnprotectData can recover both sandbox-account passwords.
// Both protect and unprotect now pass CRYPTPROTECT_LOCAL_MACHINE symmetrically (the
// blob carries the machine binding either way, but the explicit flag makes the intent
// clear and avoids relying on auto-detection).
// Mitigation (H-12): ``setup`` now layers a PROTECTED_DACL on identity_path() via
// ``protect_identity_file`` — only the installing user / SYSTEM / Administrators can
// read it. The child sandbox never loads this file; the trusted host resolves the
// credentials before launch. Inherited Everyone/Users and both sandbox-account ACEs
// are absent. DACL application is fail-closed. Real Windows multi-user validation of
// the complete state directory remains a release gate.

fn unprotect(bytes: &[u8]) -> Result<Vec<u8>, String> {
    crypt(bytes, false)
}

fn crypt(bytes: &[u8], protect: bool) -> Result<Vec<u8>, String> {
    let input = CRYPT_INTEGER_BLOB {
        cbData: bytes.len() as u32,
        pbData: bytes.as_ptr() as *mut u8,
    };
    let mut output: CRYPT_INTEGER_BLOB = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        if protect {
            CryptProtectData(
                &input,
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE,
                &mut output,
            )
        } else {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE,
                &mut output,
            )
        }
    };
    if ok == 0 {
        return Err(format!("DPAPI operation failed: {}", unsafe {
            GetLastError()
        }));
    }
    let result = unsafe {
        let value = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
        LocalFree(output.pbData as HLOCAL);
        value
    };
    Ok(result)
}

fn write_protected_identity(path: &Path, bytes: &[u8]) -> Result<(), String> {
    super::state::write_file(path, bytes)
}

fn protect_legacy_identity(state_dir: &Path) -> Result<(), String> {
    let path = identity_path(state_dir);
    for candidate in [&path, &path.with_extension("bak")] {
        super::state::protect_optional_file(candidate)?;
    }
    Ok(())
}

fn recover_identity_backup(state_dir: &Path) -> Result<(), String> {
    let path = identity_path(state_dir);
    let backup = path.with_extension("bak");
    if super::state::read_optional_file(&path)?.is_none()
        && super::state::read_optional_file(&backup)?.is_some()
    {
        fs::rename(backup, &path)
            .map_err(|error| format!("cannot recover sandbox identity backup: {error}"))?;
        super::state::protect_file(&path)?;
    }
    Ok(())
}

fn identity_path(state_dir: &Path) -> PathBuf {
    state_dir.join(IDENTITY_FILE)
}

pub fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stored_account_names_match_only_installer_generated_names() {
        assert!(valid_offline_account("AceSbOff_Ab3dE7xYz9"));
        assert!(valid_online_account("AceSbNet_Ab3dE7xYz9"));
        for invalid in [
            "AceSbOff_",
            "AceSbNet_",
            "AceSbOff_other\\account",
            "AceSbNet_other account",
            "AceSbOff_Ab3dE7xYz90",
            "Other_Ab3dE7xYz9",
        ] {
            assert!(!is_sandbox_account_name(invalid), "{invalid}");
        }
    }
}
