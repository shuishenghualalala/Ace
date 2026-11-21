use rand::distributions::Alphanumeric;
use rand::{thread_rng, Rng};
use serde::{Deserialize, Serialize};
use std::ffi::OsStr;
use std::fs;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_ACCESS_DENIED, ERROR_LOGON_FAILURE,
    ERROR_PASSWORD_EXPIRED, ERROR_PASSWORD_MUST_CHANGE, HLOCAL,
};
use windows_sys::Win32::NetworkManagement::NetManagement::{
    NERR_Success, NERR_UserNotFound, NetApiBufferFree, NetUserAdd, NetUserDel, NetUserGetInfo,
    NetUserSetInfo, UF_ACCOUNTDISABLE, UF_DONT_EXPIRE_PASSWD, UF_PASSWD_CANT_CHANGE,
    UF_PASSWORD_EXPIRED, UF_SCRIPT, USER_INFO_1, USER_INFO_1003, USER_INFO_1008, USER_PRIV_USER,
};
use windows_sys::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN,
    CRYPT_INTEGER_BLOB,
};
use windows_sys::Win32::Security::EqualSid;
use windows_sys::Win32::Security::{
    LogonUserW, LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT,
};

const IDENTITY_VERSION: u16 = 3;
const IDENTITY_FILE: &str = "windows-sandbox-identity.json";
const REPAIR_JOURNAL_VERSION: u16 = 1;
const REPAIR_JOURNAL_FILE: &str = "windows-sandbox-repair.json";

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

#[derive(Debug, Serialize, Deserialize)]
struct RepairJournal {
    version: u16,
    offline: PendingAccountRepair,
    online: PendingAccountRepair,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    retired_direct_account: Option<RetiredDirectAccount>,
}

#[derive(Debug, Serialize, Deserialize)]
struct RetiredDirectAccount {
    username: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    sid: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct PendingAccountRepair {
    username: String,
    #[serde(default)]
    sid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    protected_password: Option<Vec<u8>>,
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

#[derive(Debug, PartialEq, Eq)]
pub enum SetupCheckError {
    NotReady(String),
    AccountRepairRequired(String),
}

#[derive(Debug, PartialEq, Eq)]
pub enum AccountHealthError {
    Missing,
    Disabled,
    PasswordExpired,
    DisabledAndPasswordExpired,
    Query(String),
}

impl std::fmt::Display for AccountHealthError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Missing => formatter.write_str("Windows sandbox account is missing"),
            Self::Disabled => formatter.write_str("Windows sandbox account is disabled"),
            Self::PasswordExpired => {
                formatter.write_str("Windows sandbox account password is expired")
            }
            Self::DisabledAndPasswordExpired => formatter
                .write_str("Windows sandbox account is disabled and its password is expired"),
            Self::Query(error) => {
                write!(formatter, "cannot inspect Windows sandbox account: {error}")
            }
        }
    }
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

/// Explicit elevated recovery for an installed Windows sandbox. Runtime and startup
/// preparation never call this path automatically.
pub fn repair(state_dir: &Path, expected_owner_sid: Option<&str>) -> Result<(), SetupError> {
    if let Some(expected_sid) = expected_owner_sid {
        let same_owner = setup_owner_matches(expected_sid).map_err(SetupError::Provision)?;
        if !same_owner {
            return Err(SetupError::OwnerMismatch);
        }
    }
    repair_installed(state_dir).map_err(SetupError::Provision)
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
    if repair_journal_exists(state_dir)? {
        return Err(
            "Windows sandbox repair is incomplete; retry repair from Security Center".to_string(),
        );
    }
    if let Some(bytes) = super::state::read_optional_file(&path)? {
        let offline = load(state_dir)?;
        let online = load_online(state_dir)?;
        ensure_account_usable(&offline.username).map_err(|error| error.to_string())?;
        ensure_account_usable(&online.username).map_err(|error| error.to_string())?;
        super::token::sid_string_for_account(&offline.username)?;
        super::token::sid_string_for_account(&online.username)?;
        super::wfp::install(&offline.username, &online.username)?;
        provision_direct_identity(&path, parse_identity(&bytes)?, false)?;
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
        Ok(stored) => provision_direct_identity(&path, stored, false).map(|_| ()),
        Err(error) => {
            let _ = fs::remove_file(path);
            delete_local_user(&username);
            delete_local_user(&online_username);
            Err(error)
        }
    }
}

fn repair_installed(state_dir: &Path) -> Result<(), String> {
    if !state_dir.is_absolute() {
        return Err("security state directory must be absolute".to_string());
    }
    let _lock = super::acl::acquire_mutex()?;
    super::state::prepare_directory(state_dir)?;
    protect_legacy_identity(state_dir)?;
    super::acl::protect_legacy_state(state_dir)?;
    recover_identity_backup(state_dir)?;

    let identity_file = identity_path(state_dir);
    let bytes = super::state::read_optional_file(&identity_file)?.ok_or_else(|| {
        "Windows sandbox identity is not installed; use setup instead".to_string()
    })?;
    let mut stored = parse_identity(&bytes)?;
    let offline_sid = account_sid(&stored.username)?;
    let online_sid = account_sid(&stored.online_username)?;
    let journal_file = repair_journal_path(state_dir);
    let mut journal = match super::state::read_optional_file(&journal_file)? {
        Some(bytes) => {
            let journal: RepairJournal = serde_json::from_slice(&bytes)
                .map_err(|error| format!("Windows sandbox repair state is invalid: {error}"))?;
            validate_repair_journal(&journal, &stored, &offline_sid, &online_sid)?;
            journal
        }
        None => RepairJournal {
            version: REPAIR_JOURNAL_VERSION,
            offline: pending_account_repair(&stored.username, &offline_sid)?,
            online: pending_account_repair(&stored.online_username, &online_sid)?,
            retired_direct_account: unhealthy_direct_account(&stored)?,
        },
    };
    ensure_pending_for_unhealthy_account(&mut journal.offline)?;
    ensure_pending_for_unhealthy_account(&mut journal.online)?;
    write_repair_journal(&journal_file, &journal)?;

    if prepare_repair_account(&mut journal.offline)? {
        write_repair_journal(&journal_file, &journal)?;
    }
    if prepare_repair_account(&mut journal.online)? {
        write_repair_journal(&journal_file, &journal)?;
    }

    // Restore and verify the SID-scoped policy before re-enabling any sandbox account.
    // The offline account remains network-blocked and the online account remains proxy-only.
    super::wfp::install(&stored.username, &stored.online_username)?;
    restore_account(&journal.offline)?;
    restore_account(&journal.online)?;

    if let Some(password) = journal.offline.protected_password.as_deref() {
        stored.protected_password = password.to_vec();
    }
    if let Some(password) = journal.online.protected_password.as_deref() {
        stored.online_protected_password = password.to_vec();
    }
    stored = provision_direct_identity(&identity_file, stored, true)?;
    write_identity(&identity_file, &stored)?;

    super::identity::ensure_account_usable(&stored.username).map_err(|error| {
        format!("offline Windows sandbox account repair did not complete: {error}")
    })?;
    super::identity::ensure_account_usable(&stored.online_username).map_err(|error| {
        format!("online Windows sandbox account repair did not complete: {error}")
    })?;
    let direct = stored.direct.as_ref().ok_or_else(|| {
        "Windows sandbox direct network identity is missing after repair".to_string()
    })?;
    ensure_account_usable(&direct.username).map_err(|error| {
        format!("direct Windows sandbox account repair did not complete: {error}")
    })?;
    super::wfp::verify_installed(&stored.username, false)?;
    super::wfp::verify_installed(&stored.online_username, true)?;

    if let Some(retired) = journal.retired_direct_account.as_ref() {
        let is_current_direct = stored
            .direct
            .as_ref()
            .is_some_and(|account| account.username == retired.username);
        if !is_current_direct && valid_direct_account(&retired.username) {
            if let (Some(expected), Some(actual)) =
                (retired.sid.as_ref(), account_sid(&retired.username)?)
            {
                if expected == &actual {
                    delete_local_user_for_repair(&retired.username)?;
                }
            }
        }
    }

    fs::remove_file(&journal_file)
        .map_err(|error| format!("cannot finish Windows sandbox repair state: {error}"))?;
    Ok(())
}

fn account_sid(username: &str) -> Result<Option<String>, String> {
    if local_user_flags(username)?.is_none() {
        return Ok(None);
    }
    super::token::sid_string_for_account(username).map(Some)
}

fn pending_account_repair(
    username: &str,
    sid: &Option<String>,
) -> Result<PendingAccountRepair, String> {
    let flags = local_user_flags(username)?;
    let sid = if flags.is_some() { sid.clone() } else { None };
    let protected_password = match flags {
        Some(flags) if flags & (UF_ACCOUNTDISABLE | UF_PASSWORD_EXPIRED) == 0 => None,
        _ => Some(protect(random_password().as_bytes())?),
    };
    Ok(PendingAccountRepair {
        username: username.to_string(),
        sid,
        protected_password,
    })
}

fn unhealthy_direct_account(
    stored: &StoredIdentity,
) -> Result<Option<RetiredDirectAccount>, String> {
    let Some(account) = stored.direct.as_ref() else {
        return Ok(None);
    };
    if !valid_direct_account(&account.username) {
        return Ok(None);
    }
    let sid = account_sid(&account.username)?;
    if direct_password(account).is_err() || ensure_account_usable(&account.username).is_err() {
        Ok(Some(RetiredDirectAccount {
            username: account.username.clone(),
            sid,
        }))
    } else {
        Ok(None)
    }
}

fn ensure_pending_for_unhealthy_account(account: &mut PendingAccountRepair) -> Result<(), String> {
    let flags = local_user_flags(&account.username)?;
    if flags.is_none_or(|flags| flags & (UF_ACCOUNTDISABLE | UF_PASSWORD_EXPIRED) != 0)
        && account.protected_password.is_none()
    {
        account.protected_password = Some(protect(random_password().as_bytes())?);
    }
    Ok(())
}

fn validate_repair_journal(
    journal: &RepairJournal,
    stored: &StoredIdentity,
    offline_sid: &Option<String>,
    online_sid: &Option<String>,
) -> Result<(), String> {
    let sid_changed = |expected: &Option<String>, actual: &Option<String>| matches!((expected, actual), (Some(expected), Some(actual)) if expected != actual);
    if journal.version != REPAIR_JOURNAL_VERSION
        || journal.offline.username != stored.username
        || sid_changed(&journal.offline.sid, offline_sid)
        || journal.online.username != stored.online_username
        || sid_changed(&journal.online.sid, online_sid)
    {
        return Err(
            "Windows sandbox repair state does not match the installed account identities"
                .to_string(),
        );
    }
    Ok(())
}

fn prepare_repair_account(account: &mut PendingAccountRepair) -> Result<bool, String> {
    let flags = local_user_flags(&account.username)?;
    let current_sid = account_sid(&account.username)?;
    match (&account.sid, current_sid) {
        (Some(expected), Some(actual)) if *expected == actual => {}
        (Some(_), None) => {
            let protected_password = account.protected_password.as_deref().ok_or_else(|| {
                format!(
                    "Windows sandbox account {} is missing its recovery credential",
                    account.username
                )
            })?;
            let password = decode_password(protected_password)?;
            create_local_user_with_flags(&account.username, &password, UF_ACCOUNTDISABLE)?;
            account.sid = account_sid(&account.username)?;
            if account.sid.is_none() {
                return Err(format!(
                    "cannot resolve the repaired Windows sandbox account {}",
                    account.username
                ));
            }
            return Ok(true);
        }
        (Some(_), Some(_)) => {
            return Err(format!(
                "Windows sandbox account {} changed identity during repair; no credentials were applied",
                account.username
            ));
        }
        (None, Some(actual)) => {
            account.sid = Some(actual);
            if flags.is_some_and(|flags| flags & (UF_ACCOUNTDISABLE | UF_PASSWORD_EXPIRED) != 0)
                && account.protected_password.is_none()
            {
                account.protected_password = Some(protect(random_password().as_bytes())?);
            }
            return Ok(true);
        }
        (None, None) => {
            let protected_password = account.protected_password.as_deref().ok_or_else(|| {
                format!(
                    "Windows sandbox account {} is missing its recovery credential",
                    account.username
                )
            })?;
            let password = decode_password(protected_password)?;
            create_local_user_with_flags(&account.username, &password, UF_ACCOUNTDISABLE)?;
            account.sid = account_sid(&account.username)?;
            if account.sid.is_none() {
                return Err(format!(
                    "cannot resolve the repaired Windows sandbox account {}",
                    account.username
                ));
            }
            return Ok(true);
        }
    }
    Ok(false)
}

fn restore_account(account: &PendingAccountRepair) -> Result<(), String> {
    let current_sid = account_sid(&account.username)?;
    if current_sid.as_ref() != account.sid.as_ref() || current_sid.is_none() {
        return Err(format!(
            "Windows sandbox account {} changed identity during repair; no credentials were applied",
            account.username
        ));
    }
    let Some(protected_password) = account.protected_password.as_deref() else {
        ensure_account_usable(&account.username).map_err(|error| error.to_string())?;
        return Ok(());
    };
    let password = decode_password(protected_password)?;
    let flags = local_user_flags(&account.username)?.ok_or_else(|| {
        format!(
            "Windows sandbox account {} disappeared during repair",
            account.username
        )
    })?;
    if flags & UF_ACCOUNTDISABLE != 0 {
        set_local_user_flags(&account.username, flags & !UF_ACCOUNTDISABLE)?;
    }

    match check_account_password(&account.username, &password)? {
        PasswordState::Valid => {}
        PasswordState::Expired => {
            let flags = local_user_flags(&account.username)?.ok_or_else(|| {
                format!(
                    "Windows sandbox account {} disappeared during repair",
                    account.username
                )
            })?;
            set_local_user_flags(&account.username, flags & !UF_PASSWORD_EXPIRED)?;
        }
        PasswordState::Invalid => set_local_user_password(&account.username, &password)?,
    }

    let flags = local_user_flags(&account.username)?.ok_or_else(|| {
        format!(
            "Windows sandbox account {} disappeared during repair",
            account.username
        )
    })?;
    let repaired_flags = flags & !(UF_ACCOUNTDISABLE | UF_PASSWORD_EXPIRED);
    if repaired_flags != flags {
        set_local_user_flags(&account.username, repaired_flags)?;
    }
    if check_account_password(&account.username, &password)? != PasswordState::Valid {
        return Err(format!(
            "Windows sandbox account {} did not accept its repaired credentials",
            account.username
        ));
    }
    ensure_account_usable(&account.username).map_err(|error| error.to_string())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PasswordState {
    Valid,
    Expired,
    Invalid,
}

fn check_account_password(username: &str, password: &str) -> Result<PasswordState, String> {
    let username = wide(username);
    let password = wide(password);
    let mut token = 0;
    let success = unsafe {
        LogonUserW(
            username.as_ptr(),
            std::ptr::null(),
            password.as_ptr(),
            LOGON32_LOGON_INTERACTIVE,
            LOGON32_PROVIDER_DEFAULT,
            &mut token,
        )
    };
    if success != 0 {
        unsafe { CloseHandle(token) };
        return Ok(PasswordState::Valid);
    }
    let error = unsafe { GetLastError() };
    if error == ERROR_LOGON_FAILURE {
        Ok(PasswordState::Invalid)
    } else if error == ERROR_PASSWORD_EXPIRED || error == ERROR_PASSWORD_MUST_CHANGE {
        Ok(PasswordState::Expired)
    } else {
        Err(format!(
            "cannot validate Windows sandbox credentials: error={error}"
        ))
    }
}

fn set_local_user_password(username: &str, password: &str) -> Result<(), String> {
    let username = wide(username);
    let mut password = wide(password);
    let info = USER_INFO_1003 {
        usri1003_password: password.as_mut_ptr(),
    };
    let status = unsafe {
        NetUserSetInfo(
            std::ptr::null(),
            username.as_ptr(),
            1003,
            (&info as *const USER_INFO_1003).cast_mut().cast(),
            std::ptr::null_mut(),
        )
    };
    if status == NERR_Success {
        Ok(())
    } else {
        Err(format!(
            "cannot reset Windows sandbox password: status={status}"
        ))
    }
}

fn set_local_user_flags(username: &str, flags: u32) -> Result<(), String> {
    let username = wide(username);
    let info = USER_INFO_1008 {
        usri1008_flags: flags,
    };
    let status = unsafe {
        NetUserSetInfo(
            std::ptr::null(),
            username.as_ptr(),
            1008,
            (&info as *const USER_INFO_1008).cast_mut().cast(),
            std::ptr::null_mut(),
        )
    };
    if status == NERR_Success {
        Ok(())
    } else {
        Err(format!(
            "cannot restore Windows sandbox account flags: status={status}"
        ))
    }
}

fn write_repair_journal(path: &Path, journal: &RepairJournal) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(journal)
        .map_err(|error| format!("cannot serialize Windows sandbox repair state: {error}"))?;
    super::state::write_file(path, &bytes)
}

fn repair_journal_exists(state_dir: &Path) -> Result<bool, String> {
    super::state::read_optional_file(&repair_journal_path(state_dir)).map(|value| value.is_some())
}

fn repair_journal_path(state_dir: &Path) -> PathBuf {
    state_dir.join(REPAIR_JOURNAL_FILE)
}

fn random_password() -> String {
    thread_rng()
        .sample_iter(&Alphanumeric)
        .take(48)
        .map(char::from)
        .collect()
}

/// Installer preflight for an existing installation under the normal user.
pub fn check_setup(state_dir: &Path) -> Result<(), SetupCheckError> {
    if !state_dir.is_absolute() {
        return Err(SetupCheckError::NotReady(
            "security state directory must be absolute".to_string(),
        ));
    }
    if repair_journal_exists(state_dir).map_err(SetupCheckError::NotReady)? {
        return Err(SetupCheckError::AccountRepairRequired(
            "Windows sandbox repair is incomplete; retry repair from Security Center".to_string(),
        ));
    }
    let offline = load(state_dir).map_err(SetupCheckError::NotReady)?;
    let online = load_online(state_dir).map_err(SetupCheckError::NotReady)?;
    for credentials in [&offline, &online] {
        match ensure_account_usable(&credentials.username) {
            Ok(()) => {}
            Err(AccountHealthError::Query(error)) => {
                return Err(SetupCheckError::NotReady(error));
            }
            Err(error) => {
                return Err(SetupCheckError::AccountRepairRequired(error.to_string()));
            }
        }
    }
    let direct = load_direct(state_dir).map_err(SetupCheckError::NotReady)?;
    match ensure_account_usable(&direct.username) {
        Ok(()) => {}
        Err(AccountHealthError::Query(error)) => {
            return Err(SetupCheckError::NotReady(error));
        }
        Err(error) => {
            return Err(SetupCheckError::AccountRepairRequired(error.to_string()));
        }
    }
    super::token::sid_string_for_account(&offline.username).map_err(SetupCheckError::NotReady)?;
    super::token::sid_string_for_account(&online.username).map_err(SetupCheckError::NotReady)?;
    super::token::sid_string_for_account(&direct.username).map_err(SetupCheckError::NotReady)?;
    super::wfp::verify_installed(&offline.username, false).map_err(SetupCheckError::NotReady)?;
    super::wfp::verify_installed(&online.username, true).map_err(SetupCheckError::NotReady)
}

pub fn ensure_account_usable(username: &str) -> Result<(), AccountHealthError> {
    let flags = local_user_flags(username).map_err(AccountHealthError::Query)?;
    account_health_from_flags(flags)
}

fn account_health_from_flags(flags: Option<u32>) -> Result<(), AccountHealthError> {
    let Some(flags) = flags else {
        return Err(AccountHealthError::Missing);
    };
    let disabled = flags & UF_ACCOUNTDISABLE != 0;
    let expired = flags & UF_PASSWORD_EXPIRED != 0;
    match (disabled, expired) {
        (true, true) => Err(AccountHealthError::DisabledAndPasswordExpired),
        (true, false) => Err(AccountHealthError::Disabled),
        (false, true) => Err(AccountHealthError::PasswordExpired),
        (false, false) => Ok(()),
    }
}

fn local_user_flags(username: &str) -> Result<Option<u32>, String> {
    let mut username = wide(username);
    let mut buffer = std::ptr::null_mut();
    let status = unsafe { NetUserGetInfo(std::ptr::null(), username.as_mut_ptr(), 1, &mut buffer) };
    if status == NERR_UserNotFound {
        return Ok(None);
    }
    if status != NERR_Success {
        return Err(format!("NetUserGetInfo returned status {status}"));
    }
    let flags = unsafe { (*buffer.cast::<USER_INFO_1>()).usri1_flags };
    unsafe { NetApiBufferFree(buffer.cast()) };
    Ok(Some(flags))
}

fn setup_owner_matches(expected_sid: &str) -> Result<bool, String> {
    let expected = super::token::LocalSid::from_string(expected_sid)
        .map_err(|_| "invalid --expected-owner-sid value".to_string())?;
    let mut current = super::state::current_user_sid()?;
    Ok(unsafe { EqualSid(current.as_mut_ptr().cast(), expected.as_ptr()) } != 0)
}

fn provision_direct_identity(
    path: &Path,
    mut stored: StoredIdentity,
    repair_unusable: bool,
) -> Result<StoredIdentity, String> {
    if let Some(account) = stored.direct.as_ref() {
        if direct_password(account).is_ok() {
            match ensure_account_usable(&account.username) {
                Ok(()) => return Ok(stored),
                Err(AccountHealthError::Disabled) if repair_unusable => {
                    let flags = local_user_flags(&account.username)?.ok_or_else(|| {
                        format!(
                            "Windows sandbox direct account {} disappeared",
                            account.username
                        )
                    })?;
                    set_local_user_flags(&account.username, flags & !UF_ACCOUNTDISABLE)?;
                    if ensure_account_usable(&account.username).is_ok() {
                        return Ok(stored);
                    }
                }
                Err(error) if !repair_unusable => return Err(error.to_string()),
                Err(_) => {}
            }
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
    create_local_user_with_flags(
        &username,
        &password,
        if repair_unusable {
            UF_ACCOUNTDISABLE
        } else {
            0
        },
    )?;
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
    if repair_unusable {
        let flags = local_user_flags(&username)?
            .ok_or_else(|| format!("repaired direct account {username} disappeared"))?;
        set_local_user_flags(&username, flags & !UF_ACCOUNTDISABLE)?;
        ensure_account_usable(&username).map_err(|error| error.to_string())?;
    }
    if let Some(account) = previous {
        if repair_unusable {
            delete_local_user_for_repair(&account.username)?;
        } else {
            delete_local_user(&account.username);
        }
    }
    Ok(stored)
}

pub fn load(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, false)
}

pub fn load_online(state_dir: &Path) -> Result<SandboxCredentials, String> {
    load_named(state_dir, true)
}

pub fn load_direct(state_dir: &Path) -> Result<SandboxCredentials, String> {
    ensure_no_repair_in_progress(state_dir)?;
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
    let identity_bytes = super::state::read_optional_file(&path)?;
    let (usernames, (offline_account, online_account)) = if let Some(bytes) = identity_bytes {
        let value: serde_json::Value = serde_json::from_slice(&bytes)
            .map_err(|error| format!("Windows sandbox identity is invalid: {error}"))?;
        (
            stored_usernames_from_value(&value)?,
            stored_wfp_accounts_from_value(&value),
        )
    } else {
        (Vec::new(), (None, None))
    };
    let repair_journal = super::state::read_optional_file(&repair_journal_path(state_dir))?;
    if usernames.is_empty() && repair_journal.is_some() {
        return Err(
            "Windows sandbox repair is incomplete but its identity file is missing; restore the protected identity before uninstalling".to_string(),
        );
    }
    super::wfp::uninstall(offline_account.as_deref(), online_account.as_deref())?;
    for username in &usernames {
        if is_sandbox_account_name(username) && username.len() <= 64 {
            delete_local_user(username);
        }
    }
    if !usernames.is_empty() {
        fs::remove_file(path)
            .map_err(|error| format!("cannot remove sandbox identity: {error}"))?;
    }
    if repair_journal.is_some() {
        fs::remove_file(repair_journal_path(state_dir))
            .map_err(|error| format!("cannot remove Windows sandbox repair state: {error}"))?;
    }
    Ok(())
}

fn load_named(state_dir: &Path, online: bool) -> Result<SandboxCredentials, String> {
    ensure_no_repair_in_progress(state_dir)?;
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

fn ensure_no_repair_in_progress(state_dir: &Path) -> Result<(), String> {
    if repair_journal_exists(state_dir)? {
        return Err(
            "Windows sandbox repair is incomplete; retry repair from Security Center".to_string(),
        );
    }
    Ok(())
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

fn stored_wfp_accounts_from_value(value: &serde_json::Value) -> (Option<String>, Option<String>) {
    let offline = value
        .get("username")
        .and_then(|value| value.as_str())
        .filter(|name| valid_offline_account(name))
        .map(str::to_string);
    let online = value
        .get("online_username")
        .and_then(|value| value.as_str())
        .filter(|name| valid_online_account(name))
        .map(str::to_string);
    (offline, online)
}

fn delete_local_user(username: &str) {
    let username = wide(username);
    unsafe { NetUserDel(std::ptr::null(), username.as_ptr()) };
}

fn delete_local_user_for_repair(username: &str) -> Result<(), String> {
    if !valid_direct_account(username) {
        return Err("refusing to remove an unrecognized direct sandbox account".to_string());
    }
    delete_local_user(username);
    if local_user_flags(username)?.is_some() {
        return Err(format!(
            "cannot remove retired direct sandbox account {username}; repair remains pending"
        ));
    }
    Ok(())
}

fn create_local_user(username: &str, password: &str) -> Result<(), String> {
    create_local_user_with_flags(username, password, 0)
}

fn create_local_user_with_flags(
    username: &str,
    password: &str,
    additional_flags: u32,
) -> Result<(), String> {
    let mut username = wide(username);
    let mut password = wide(password);
    let mut info = USER_INFO_1 {
        usri1_name: username.as_mut_ptr(),
        usri1_password: password.as_mut_ptr(),
        usri1_password_age: 0,
        usri1_priv: USER_PRIV_USER,
        usri1_home_dir: std::ptr::null_mut(),
        usri1_comment: std::ptr::null_mut(),
        usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_PASSWD_CANT_CHANGE | additional_flags,
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
    fn explicit_repair_without_installed_identity_does_not_create_accounts_or_journal() {
        let parent = tempfile::tempdir().unwrap();
        let state = parent.path().join("not-installed");
        assert!(matches!(
            repair(&state, None),
            Err(SetupError::Provision(message)) if message.contains("identity is not installed")
        ));
        assert!(!identity_path(&state).exists());
        assert!(!repair_journal_path(&state).exists());
    }

    #[test]
    fn account_health_rejects_missing_disabled_and_expired_accounts() {
        assert_eq!(
            account_health_from_flags(None),
            Err(AccountHealthError::Missing)
        );
        assert_eq!(
            account_health_from_flags(Some(UF_ACCOUNTDISABLE)),
            Err(AccountHealthError::Disabled)
        );
        assert_eq!(
            account_health_from_flags(Some(UF_PASSWORD_EXPIRED)),
            Err(AccountHealthError::PasswordExpired)
        );
        assert_eq!(
            account_health_from_flags(Some(UF_ACCOUNTDISABLE | UF_PASSWORD_EXPIRED)),
            Err(AccountHealthError::DisabledAndPasswordExpired)
        );
        assert_eq!(account_health_from_flags(Some(0)), Ok(()));
        assert_eq!(
            account_health_from_flags(Some(UF_DONT_EXPIRE_PASSWD)),
            Ok(())
        );
    }

    #[test]
    fn local_user_flag_query_reports_a_missing_local_account() {
        let suffix: String = thread_rng()
            .sample_iter(&Alphanumeric)
            .take(12)
            .map(char::from)
            .collect();
        let username = format!("AceProbe{suffix}");
        assert_eq!(local_user_flags(&username).unwrap(), None);
    }

    #[test]
    fn setup_check_classifies_a_missing_sandbox_account_before_sid_lookup() {
        let parent = tempfile::tempdir().unwrap();
        let suffix: String = thread_rng()
            .sample_iter(&Alphanumeric)
            .take(10)
            .map(char::from)
            .collect();
        let stored = StoredIdentity {
            version: IDENTITY_VERSION,
            username: format!("AceSbOff_{suffix}"),
            protected_password: protect(b"offline-test-password").unwrap(),
            online_username: format!("AceSbNet_{suffix}"),
            online_protected_password: protect(b"online-test-password").unwrap(),
            direct: None,
        };
        write_identity(&identity_path(parent.path()), &stored).unwrap();

        assert!(matches!(
            check_setup(parent.path()),
            Err(SetupCheckError::AccountRepairRequired(message))
                if message == "Windows sandbox account is missing"
        ));
    }

    #[test]
    fn incomplete_repair_blocks_setup_checks_and_credential_loading() {
        let parent = tempfile::tempdir().unwrap();
        let suffix: String = thread_rng()
            .sample_iter(&Alphanumeric)
            .take(10)
            .map(char::from)
            .collect();
        let stored = StoredIdentity {
            version: IDENTITY_VERSION,
            username: format!("AceSbOff_{suffix}"),
            protected_password: protect(b"offline-test-password").unwrap(),
            online_username: format!("AceSbNet_{suffix}"),
            online_protected_password: protect(b"online-test-password").unwrap(),
            direct: None,
        };
        write_identity(&identity_path(parent.path()), &stored).unwrap();
        write_repair_journal(
            &repair_journal_path(parent.path()),
            &RepairJournal {
                version: REPAIR_JOURNAL_VERSION,
                offline: PendingAccountRepair {
                    username: stored.username.clone(),
                    sid: None,
                    protected_password: Some(protect(b"pending-offline-password").unwrap()),
                },
                online: PendingAccountRepair {
                    username: stored.online_username.clone(),
                    sid: None,
                    protected_password: Some(protect(b"pending-online-password").unwrap()),
                },
                retired_direct_account: None,
            },
        )
        .unwrap();

        assert!(load(parent.path())
            .unwrap_err()
            .contains("repair is incomplete"));
        assert!(matches!(
            check_setup(parent.path()),
            Err(SetupCheckError::AccountRepairRequired(message))
                if message.contains("repair is incomplete")
        ));
    }

    #[test]
    fn repair_journal_rejects_an_account_sid_change() {
        let stored = StoredIdentity {
            version: IDENTITY_VERSION,
            username: "AceSbOff_0123456789".to_string(),
            protected_password: Vec::new(),
            online_username: "AceSbNet_0123456789".to_string(),
            online_protected_password: Vec::new(),
            direct: None,
        };
        let journal = RepairJournal {
            version: REPAIR_JOURNAL_VERSION,
            offline: PendingAccountRepair {
                username: stored.username.clone(),
                sid: Some("S-1-5-21-100".to_string()),
                protected_password: None,
            },
            online: PendingAccountRepair {
                username: stored.online_username.clone(),
                sid: Some("S-1-5-21-200".to_string()),
                protected_password: None,
            },
            retired_direct_account: None,
        };
        assert!(validate_repair_journal(
            &journal,
            &stored,
            &Some("S-1-5-21-101".to_string()),
            &Some("S-1-5-21-200".to_string()),
        )
        .is_err());
    }

    #[test]
    fn missing_account_repair_uses_a_protected_pending_credential() {
        let account = pending_account_repair("AceSbOff_0123456789", &None).unwrap();
        let protected = account.protected_password.unwrap();
        let password = decode_password(&protected).unwrap();
        assert_eq!(account.sid, None);
        assert_eq!(password.len(), 48);
        assert_ne!(protected, password.as_bytes());
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

    #[test]
    fn uninstall_scopes_wfp_cleanup_to_valid_offline_and_online_accounts() {
        let identity = serde_json::json!({
            "username": "AceSbOff_0123456789",
            "online_username": "AceSbNet_0123456789",
            "direct": { "username": "AceSbDir_0123456789" },
        });
        assert_eq!(
            stored_wfp_accounts_from_value(&identity),
            (
                Some("AceSbOff_0123456789".to_string()),
                Some("AceSbNet_0123456789".to_string()),
            ),
        );

        let invalid = serde_json::json!({
            "username": "Administrator",
            "online_username": "AceSbOff_0123456789",
            "direct": { "username": "AceSbDir_0123456789" },
        });
        assert_eq!(stored_wfp_accounts_from_value(&invalid), (None, None));
    }
}
