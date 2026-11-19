use rand::distributions::Alphanumeric;
use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::OsStr;
use std::fs;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{GetLastError, LocalFree, ERROR_ACCESS_DENIED, HLOCAL};
use windows_sys::Win32::NetworkManagement::NetManagement::{
    NERR_Success, NetUserAdd, NetUserDel, UF_DONT_EXPIRE_PASSWD, UF_PASSWD_CANT_CHANGE, UF_SCRIPT,
    USER_INFO_1, USER_PRIV_USER,
};
use windows_sys::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN,
    CRYPT_INTEGER_BLOB,
};
use windows_sys::Win32::Security::EqualSid;

const IDENTITY_VERSION: u16 = 3;
const IDENTITY_FILE: &str = "windows-sandbox-identity.json";

#[derive(Debug, Serialize, Deserialize)]
struct StoredIdentity {
    version: u16,
    username: String,
    protected_password: Vec<u8>,
    online_username: String,
    online_protected_password: Vec<u8>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    direct: Option<StoredAccount>,
}

#[derive(Debug, Serialize, Deserialize)]
struct StoredAccount {
    username: String,
    protected_password: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct SandboxCredentials {
    pub username: String,
    pub password: String,
}

pub enum SetupError {
    OwnerMismatch,
    Provision(String),
}

/// Installer-only elevated setup. Runtime never invokes this automatically.
pub fn setup(state_dir: &Path, expected_owner_sid: Option<&str>) -> Result<(), SetupError> {
    if let Some(expected_sid) = expected_owner_sid {
        let same_owner = setup_owner_matches(expected_sid).map_err(SetupError::Provision)?;
        if !same_owner {
            return Err(SetupError::OwnerMismatch);
        }
    }
    provision(state_dir).map_err(SetupError::Provision)
}

fn provision(state_dir: &Path) -> Result<(), String> {
    if !state_dir.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    let _lock = super::acl::acquire_mutex()?;
    super::state::prepare_directory(state_dir)?;
    protect_legacy_identity(state_dir)?;
    super::acl::protect_legacy_state(state_dir)?;
    recover_identity_backup(state_dir)?;
    let path = identity_path(state_dir);
    if let Some(bytes) = super::state::read_optional_file(&path)? {
        let offline = load(state_dir)?;
        let online = load_online(state_dir)?;
        super::token::sid_string_for_account(&offline.username)?;
        super::token::sid_string_for_account(&online.username)?;
        super::wfp::install(&offline.username, &online.username)?;
        return provision_direct_identity(&path, parse_identity(&bytes)?);
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
        delete_local_user(&username);
        return Err(error);
    }
    let result = (|| {
        let stored = StoredIdentity {
            version: IDENTITY_VERSION,
            username: username.clone(),
            protected_password: protect(password.as_bytes())?,
            online_username: online_username.clone(),
            online_protected_password: protect(online_password.as_bytes())?,
            direct: None,
        };
        write_identity(&path, &stored)?;
        Ok(stored)
    })();
    let stored = result.and_then(|stored| {
        super::wfp::install(&username, &online_username)?;
        Ok(stored)
    });
    match stored {
        Ok(stored) => provision_direct_identity(&path, stored),
        Err(error) => {
            let _ = fs::remove_file(path);
            delete_local_user(&username);
            delete_local_user(&online_username);
            Err(error)
        }
    }
}

/// Installer preflight for an existing installation under the normal user.
pub fn check_setup(state_dir: &Path) -> Result<(), String> {
    if !state_dir.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    let offline = load(state_dir)?;
    let online = load_online(state_dir)?;
    load_direct(state_dir)?;
    super::wfp::verify_installed(&offline.username, false)?;
    super::wfp::verify_installed(&online.username, true)
}

fn setup_owner_matches(expected_sid: &str) -> Result<bool, String> {
    let expected = super::token::LocalSid::from_string(expected_sid)
        .map_err(|_| "invalid --expected-owner-sid value".to_string())?;
    let mut current = super::state::current_user_sid()?;
    Ok(unsafe { EqualSid(current.as_mut_ptr().cast(), expected.as_ptr()) } != 0)
}

fn provision_direct_identity(path: &Path, mut stored: StoredIdentity) -> Result<(), String> {
    if let Some(account) = stored.direct.as_ref() {
        if direct_password(account).is_ok() {
            return Ok(());
        }
        if !valid_direct_account(&account.username) {
            return Err("Windows sandbox direct network identity is invalid".to_string());
        }
    }
    let suffix: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(10)
        .map(char::from)
        .collect();
    let username = format!("AceSbDir_{suffix}");
    let password: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(48)
        .map(char::from)
        .collect();
    let protected_password = protect(password.as_bytes())?;
    create_local_user(&username, &password)?;
    let previous = stored.direct.replace(StoredAccount {
        username: username.clone(),
        protected_password,
    });
    if let Err(error) = write_identity(path, &stored) {
        // Publishing can succeed before removal of the old backup is attempted.
        // Avoid deleting the account if the new record already points to it.
        let published = super::state::read_file(path)
            .and_then(|bytes| parse_identity(&bytes))
            .ok()
            .and_then(|stored| stored.direct)
            .is_some_and(|account| account.username == username);
        if !published {
            delete_local_user(&username);
        }
        return Err(error);
    }
    if let Some(account) = previous {
        delete_local_user(&account.username);
    }
    Ok(())
}

pub fn load(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, false)
}

pub fn load_online(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, true)
}

pub fn load_direct(state_dir: &Path) -> Result<SandboxCredentials, String> {
    let stored = read_identity(state_dir)?;
    let account = stored.direct.ok_or_else(|| {
        "Windows direct network requires sandbox setup or repair from Ace Security Center"
            .to_string()
    })?;
    let password = direct_password(&account)?;
    Ok(SandboxCredentials {
        username: account.username,
        password,
    })
}

fn direct_password(account: &StoredAccount) -> Result<String, String> {
    if !valid_direct_account(&account.username) {
        return Err("Windows sandbox direct network identity is invalid".to_string());
    }
    super::token::sid_string_for_account(&account.username)?;
    decode_password(&account.protected_password)
}

/// Elevated uninstall removes only stable Ace WFP objects and current Ace
/// sandbox accounts recorded in the identity file.
pub fn uninstall(state_dir: &Path) -> Result<(), String> {
    if !state_dir.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    let _lock = super::acl::acquire_mutex()?;
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
    super::wfp::uninstall()?;
    for username in &usernames {
        if is_sandbox_account_name(username) && username.len() <= 64 {
            delete_local_user(username);
        }
    }
    if !usernames.is_empty() {
        fs::remove_file(path)
            .map_err(|error| format!("cannot remove sandbox identity: {error}"))?;
    }
    Ok(())
}

fn load_named(state_dir: &Path, online: bool) -> Result<SandboxCredentials, String> {
    let stored = read_identity(state_dir)?;
    let protected_password = if online {
        &stored.online_protected_password
    } else {
        &stored.protected_password
    };
    let password = decode_password(protected_password)?;
    Ok(SandboxCredentials {
        username: if online {
            stored.online_username
        } else {
            stored.username
        },
        password,
    })
}

fn read_identity(state_dir: &Path) -> Result<StoredIdentity, String> {
    let bytes = super::state::read_file(&identity_path(state_dir))
        .map_err(|error| format!("Windows sandbox identity is not installed: {error}"))?;
    parse_identity(&bytes)
}

fn parse_identity(bytes: &[u8]) -> Result<StoredIdentity, String> {
    let stored: StoredIdentity = serde_json::from_slice(bytes)
        .map_err(|error| format!("Windows sandbox identity is invalid: {error}"))?;
    if stored.version != IDENTITY_VERSION
        || !valid_offline_account(&stored.username)
        || !valid_online_account(&stored.online_username)
    {
        return Err("Windows sandbox identity version mismatch".to_string());
    }
    Ok(stored)
}

fn decode_password(protected_password: &[u8]) -> Result<String, String> {
    String::from_utf8(unprotect(protected_password)?)
        .map_err(|_| "Windows sandbox password is not valid UTF-8".to_string())
}

/// Current account names only.
fn valid_offline_account(value: &str) -> bool {
    value.starts_with("AceSbOff_") && value.len() <= 64
}

fn valid_online_account(value: &str) -> bool {
    value.starts_with("AceSbNet_") && value.len() <= 64
}

fn valid_direct_account(value: &str) -> bool {
    value.strip_prefix("AceSbDir_").is_some_and(|suffix| {
        suffix.len() == 10 && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
    })
}

/// Guards ``delete_local_user`` so only Ace sandbox accounts are ever removed,
/// even if the identity file is corrupt.
fn is_sandbox_account_name(value: &str) -> bool {
    value.starts_with("AceSb")
}

fn stored_usernames_from_value(value: &serde_json::Value) -> Result<Vec<String>, String> {
    let mut names = Vec::new();
    if let Some(s) = value.get("username").and_then(|v| v.as_str()) {
        names.push(s.to_string());
    }
    if let Some(s) = value.get("online_username").and_then(|v| v.as_str()) {
        names.push(s.to_string());
    }
    if let Some(account) = value["direct"]["username"]
        .as_str()
        .filter(|name| valid_direct_account(name))
    {
        names.push(account.to_string());
    }
    Ok(names)
}

fn stored_usernames(state_dir: &Path) -> Result<Vec<String>, String> {
    let bytes = super::state::read_file(&identity_path(state_dir))
        .map_err(|error| format!("Windows sandbox identity is not installed: {error}"))?;
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("Windows sandbox identity is invalid: {error}"))?;
    stored_usernames_from_value(&value)
}

fn delete_local_user(username: &str) {
    let username = wide(username);
    unsafe { NetUserDel(std::ptr::null(), username.as_ptr()) };
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

fn write_identity(path: &Path, stored: &StoredIdentity) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(stored)
        .map_err(|error| format!("cannot serialize sandbox identity: {error}"))?;
    super::state::write_file(path, &bytes)
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
    fn setup_check_leaves_missing_state_untouched() {
        let parent = tempfile::tempdir().unwrap();
        let state = parent.path().join("not-installed");
        assert!(check_setup(&state).is_err());
        assert!(!state.exists());
    }

    #[test]
    fn mismatched_setup_owner_does_not_create_security_state() {
        let parent = tempfile::tempdir().unwrap();
        let state = parent.path().join("untouched");
        assert!(matches!(
            setup(&state, Some("S-1-1-0")),
            Err(SetupError::OwnerMismatch)
        ));
        assert!(!state.exists());
    }

    #[test]
    fn malformed_setup_owner_does_not_create_security_state() {
        let parent = tempfile::tempdir().unwrap();
        let state = parent.path().join("untouched");
        assert!(matches!(
            setup(&state, Some("invalid")),
            Err(SetupError::Provision(error)) if error.contains("invalid --expected-owner-sid value")
        ));
        assert!(!state.exists());
    }

    #[test]
    fn legacy_v3_file_keeps_restricted_identities_and_has_no_direct_identity() {
        let existing = serde_json::json!({
            "version": 3,
            "username": "AceSbOff_0123456789",
            "protected_password": [],
            "online_username": "AceSbNet_0123456789",
            "online_protected_password": [],
        });
        let stored = parse_identity(&serde_json::to_vec(&existing).unwrap()).unwrap();
        assert_eq!(stored.username, "AceSbOff_0123456789");
        assert_eq!(stored.online_username, "AceSbNet_0123456789");
        assert!(stored.direct.is_none());
        assert_eq!(stored_usernames_from_value(&existing).unwrap().len(), 2);
    }

    #[test]
    fn uninstall_enumerates_only_valid_new_direct_identity_names() {
        let value = |name: &str| serde_json::json!({ "direct": { "username": name } });
        assert_eq!(
            stored_usernames_from_value(&value("AceSbDir_0123456789")).unwrap(),
            ["AceSbDir_0123456789"],
        );
        assert!(stored_usernames_from_value(&value("Administrator"))
            .unwrap()
            .is_empty());
    }
}
