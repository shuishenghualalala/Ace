use std::os::windows::ffi::OsStrExt;

use windows_sys::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegCreateKeyExW, RegDeleteValueW, RegOpenKeyExW, RegSetValueExW,
    HKEY_LOCAL_MACHINE, KEY_SET_VALUE, KEY_WRITE, REG_DWORD, REG_OPTION_NON_VOLATILE,
};

const USERLIST_KEY_PATH: &str =
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList";

/// Hide only the generated Ace sandbox accounts from the interactive logon UI.
/// Any failure is returned so setup cannot report a partially hardened identity.
pub fn hide_sandbox_users(usernames: &[&str]) -> Result<(), String> {
    if usernames.is_empty() {
        return Ok(());
    }
    let key = create_userlist_key()?;
    for username in usernames {
        let name = wide(username);
        let hidden: u32 = 0;
        let status = unsafe {
            RegSetValueExW(
                key,
                name.as_ptr(),
                0,
                REG_DWORD,
                (&hidden as *const u32).cast(),
                std::mem::size_of::<u32>() as u32,
            )
        };
        if status != ERROR_SUCCESS {
            unsafe { RegCloseKey(key) };
            return Err(format!("RegSetValueExW(UserList) failed: {status}"));
        }
    }
    unsafe { RegCloseKey(key) };
    Ok(())
}

/// Remove only the generated Ace entries during uninstall. Missing keys or
/// values are already the desired state.
pub fn unhide_sandbox_users(usernames: &[&str]) -> Result<(), String> {
    if usernames.is_empty() {
        return Ok(());
    }
    let mut key = 0;
    let path = wide(USERLIST_KEY_PATH);
    let status = unsafe {
        RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            path.as_ptr(),
            0,
            KEY_SET_VALUE,
            &mut key,
        )
    };
    if status == ERROR_FILE_NOT_FOUND {
        return Ok(());
    }
    if status != ERROR_SUCCESS {
        return Err(format!("RegOpenKeyExW(UserList) failed: {status}"));
    }
    let mut failure = None;
    for username in usernames {
        let name = wide(username);
        let status = unsafe { RegDeleteValueW(key, name.as_ptr()) };
        if status != ERROR_SUCCESS && status != ERROR_FILE_NOT_FOUND {
            failure = Some(format!("RegDeleteValueW(UserList) failed: {status}"));
            break;
        }
    }
    unsafe { RegCloseKey(key) };
    failure.map_or(Ok(()), Err)
}

fn create_userlist_key() -> Result<isize, String> {
    let path = wide(USERLIST_KEY_PATH);
    let mut key = 0;
    let status = unsafe {
        RegCreateKeyExW(
            HKEY_LOCAL_MACHINE,
            path.as_ptr(),
            0,
            std::ptr::null_mut(),
            REG_OPTION_NON_VOLATILE,
            KEY_WRITE,
            std::ptr::null_mut(),
            &mut key,
            std::ptr::null_mut(),
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!("RegCreateKeyExW(UserList) failed: {status}"));
    }
    Ok(key)
}

fn wide(value: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
