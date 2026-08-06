use std::ffi::{c_void, OsStr};
use std::os::windows::ffi::OsStrExt;

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_SUCCESS, HANDLE, HLOCAL,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSidToSidW, SetEntriesInAclW, EXPLICIT_ACCESS_W,
    GRANT_ACCESS, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    AdjustTokenPrivileges, CopySid, CreateRestrictedToken, CreateWellKnownSid, GetLengthSid,
    GetTokenInformation, LookupAccountNameW, LookupPrivilegeValueW, SetTokenInformation,
    TokenDefaultDacl, TokenGroups, TokenUser, ACL, SE_PRIVILEGE_ENABLED, SID_AND_ATTRIBUTES,
    SID_NAME_USE, TOKEN_ADJUST_DEFAULT, TOKEN_ADJUST_PRIVILEGES, TOKEN_ASSIGN_PRIMARY,
    TOKEN_DEFAULT_DACL, TOKEN_DUPLICATE, TOKEN_PRIVILEGES, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

pub struct LocalSid(*mut c_void);

const DISABLE_MAX_PRIVILEGE: u32 = 0x01;
const LUA_TOKEN: u32 = 0x04;
const WRITE_RESTRICTED: u32 = 0x08;
const WIN_WORLD_SID: i32 = 1; // WELL_KNOWN_SID_TYPE::WinWorldSid -> Everyone
const GENERIC_ALL: u32 = 0x1000_0000;
const SE_GROUP_LOGON_ID: u32 = 0xC000_0000;

impl LocalSid {
    pub fn from_string(value: &str) -> Result<Self, String> {
        let mut pointer = std::ptr::null_mut();
        let wide = wide(value);
        if unsafe { ConvertStringSidToSidW(wide.as_ptr(), &mut pointer) } == 0 {
            return Err(format!("invalid capability SID: {}", unsafe {
                GetLastError()
            }));
        }
        Ok(Self(pointer))
    }

    pub fn as_ptr(&self) -> *mut c_void {
        self.0
    }
}

impl Drop for LocalSid {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { LocalFree(self.0 as HLOCAL) };
        }
    }
}

pub fn sid_string_for_account(account: &str) -> Result<String, String> {
    let account = wide(account);
    let mut sid_size = 0_u32;
    let mut domain_size = 0_u32;
    let mut use_type: SID_NAME_USE = 0;
    unsafe {
        LookupAccountNameW(
            std::ptr::null(),
            account.as_ptr(),
            std::ptr::null_mut(),
            &mut sid_size,
            std::ptr::null_mut(),
            &mut domain_size,
            &mut use_type,
        );
    }
    if sid_size == 0 {
        return Err(format!("cannot size sandbox account SID: {}", unsafe {
            GetLastError()
        }));
    }
    let mut sid = vec![0_u8; sid_size as usize];
    let mut domain = vec![0_u16; domain_size as usize];
    if unsafe {
        LookupAccountNameW(
            std::ptr::null(),
            account.as_ptr(),
            sid.as_mut_ptr().cast(),
            &mut sid_size,
            domain.as_mut_ptr(),
            &mut domain_size,
            &mut use_type,
        )
    } == 0
    {
        return Err(format!("cannot resolve sandbox account SID: {}", unsafe {
            GetLastError()
        }));
    }
    let mut string_pointer = std::ptr::null_mut();
    if unsafe { ConvertSidToStringSidW(sid.as_mut_ptr().cast(), &mut string_pointer) } == 0 {
        return Err(format!("cannot format sandbox account SID: {}", unsafe {
            GetLastError()
        }));
    }
    let value = unsafe {
        let mut length = 0;
        while *string_pointer.add(length) != 0 {
            length += 1;
        }
        String::from_utf16_lossy(std::slice::from_raw_parts(string_pointer, length))
    };
    unsafe { LocalFree(string_pointer as HLOCAL) };
    Ok(value)
}

/// Build a restricted token for the sandbox runner.
///
/// Audit W1: restricting SIDs must include not just the capability SIDs but
/// also the logon SID, Everyone, and the token-user SID, and the token must
/// carry a permissive default DACL so the sandboxed process can create
/// pipes/IPC objects without ACCESS_DENIED under real UAC. This mirrors the
/// Codex `create_workspace_write_token_with_caps_and_user_from` path (the
/// runner runs as the dedicated sandbox account, i.e. the elevated backend).
pub fn create_restricted_token(capability_sids: &[String]) -> Result<HANDLE, String> {
    if capability_sids.is_empty() {
        return Err("restricted token requires at least one capability SID".to_string());
    }
    let local_sids = capability_sids
        .iter()
        .map(|value| LocalSid::from_string(value))
        .collect::<Result<Vec<_>, _>>()?;
    let access = TOKEN_DUPLICATE
        | TOKEN_ASSIGN_PRIMARY
        | TOKEN_QUERY
        | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_PRIVILEGES;
    let mut base = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), access, &mut base) } == 0 {
        return Err(format!("OpenProcessToken failed: {}", unsafe {
            GetLastError()
        }));
    }
    // Gather SIDs needed for both the restricting list and the default DACL.
    // All three are owned by their Vec and must outlive CreateRestrictedToken.
    let mut user_sid = match get_user_sid_bytes(base) {
        Ok(v) => v,
        Err(error) => {
            unsafe { CloseHandle(base) };
            return Err(error);
        }
    };
    let mut logon_sid = match get_logon_sid_bytes(base) {
        Ok(v) => v,
        Err(error) => {
            unsafe { CloseHandle(base) };
            return Err(error);
        }
    };
    let mut everyone = match world_sid() {
        Ok(v) => v,
        Err(error) => {
            unsafe { CloseHandle(base) };
            return Err(error);
        }
    };
    let psid_user = user_sid.as_mut_ptr() as *mut c_void;
    let psid_logon = logon_sid.as_mut_ptr() as *mut c_void;
    let psid_everyone = everyone.as_mut_ptr() as *mut c_void;

    // Restricting SIDs order: capabilities..., user SID, logon, Everyone
    // (Codex token.rs create_token_with_caps_from, elevated path).
    let mut entries: Vec<SID_AND_ATTRIBUTES> = Vec::with_capacity(local_sids.len() + 3);
    for sid in &local_sids {
        entries.push(SID_AND_ATTRIBUTES {
            Sid: sid.as_ptr(),
            Attributes: 0,
        });
    }
    entries.push(SID_AND_ATTRIBUTES {
        Sid: psid_user,
        Attributes: 0,
    });
    entries.push(SID_AND_ATTRIBUTES {
        Sid: psid_logon,
        Attributes: 0,
    });
    entries.push(SID_AND_ATTRIBUTES {
        Sid: psid_everyone,
        Attributes: 0,
    });

    let mut restricted = 0;
    let ok = unsafe {
        CreateRestrictedToken(
            base,
            DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
            0,
            std::ptr::null(),
            0,
            std::ptr::null(),
            entries.len() as u32,
            entries.as_mut_ptr(),
            &mut restricted,
        )
    };
    unsafe { CloseHandle(base) };
    if ok == 0 {
        return Err(format!("CreateRestrictedToken failed: {}", unsafe {
            GetLastError()
        }));
    }
    // Default DACL grants GENERIC_ALL to logon, Everyone, and the capability
    // SIDs so sandboxed processes can create pipes/IPC objects (audit W1).
    let mut dacl_sids: Vec<*mut c_void> = Vec::with_capacity(local_sids.len() + 2);
    dacl_sids.push(psid_logon);
    dacl_sids.push(psid_everyone);
    for sid in &local_sids {
        dacl_sids.push(sid.as_ptr());
    }
    if let Err(error) = set_default_dacl(restricted, &dacl_sids) {
        unsafe { CloseHandle(restricted) };
        return Err(error);
    }
    if let Err(error) = enable_traverse_privilege(restricted) {
        unsafe { CloseHandle(restricted) };
        return Err(error);
    }
    Ok(restricted)
}

fn enable_traverse_privilege(token: HANDLE) -> Result<(), String> {
    let mut luid = unsafe { std::mem::zeroed() };
    if unsafe {
        LookupPrivilegeValueW(
            std::ptr::null(),
            wide("SeChangeNotifyPrivilege").as_ptr(),
            &mut luid,
        )
    } == 0
    {
        return Err(format!("LookupPrivilegeValueW failed: {}", unsafe {
            GetLastError()
        }));
    }
    let mut privileges: TOKEN_PRIVILEGES = unsafe { std::mem::zeroed() };
    privileges.PrivilegeCount = 1;
    privileges.Privileges[0].Luid = luid;
    privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    // Audit M3: AdjustTokenPrivileges returns nonzero even when not all
    // privileges were assigned; only GetLastError()==0 means full success.
    // Call GetLastError in the same unsafe block to avoid a stale error.
    let (ok, last_error) = unsafe {
        let ok = AdjustTokenPrivileges(
            token,
            0,
            &privileges,
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        );
        (ok, GetLastError())
    };
    if ok == 0 {
        return Err(format!("AdjustTokenPrivileges failed: {last_error}"));
    }
    if last_error != 0 {
        return Err(format!(
            "AdjustTokenPrivileges did not assign all privileges (GetLastError={last_error})"
        ));
    }
    Ok(())
}

/// Set a permissive default DACL on the token so sandboxed processes can
/// create pipes/IPC objects without ACCESS_DENIED (audit W1).
fn set_default_dacl(token: HANDLE, sids: &[*mut c_void]) -> Result<(), String> {
    if sids.is_empty() {
        return Ok(());
    }
    let entries: Vec<EXPLICIT_ACCESS_W> = sids
        .iter()
        .map(|sid| EXPLICIT_ACCESS_W {
            grfAccessPermissions: GENERIC_ALL,
            grfAccessMode: GRANT_ACCESS,
            grfInheritance: 0,
            Trustee: TRUSTEE_W {
                pMultipleTrustee: std::ptr::null_mut(),
                MultipleTrusteeOperation: 0,
                TrusteeForm: TRUSTEE_IS_SID,
                TrusteeType: TRUSTEE_IS_UNKNOWN,
                ptstrName: *sid as *mut u16,
            },
        })
        .collect();
    let mut new_dacl: *mut ACL = std::ptr::null_mut();
    let res = unsafe {
        SetEntriesInAclW(
            entries.len() as u32,
            entries.as_ptr(),
            std::ptr::null_mut(),
            &mut new_dacl,
        )
    };
    if res != ERROR_SUCCESS {
        return Err(format!("SetEntriesInAclW failed: {res}"));
    }
    let mut info = TOKEN_DEFAULT_DACL {
        DefaultDacl: new_dacl,
    };
    let ok = unsafe {
        SetTokenInformation(
            token,
            TokenDefaultDacl,
            &mut info as *mut _ as *mut c_void,
            std::mem::size_of::<TOKEN_DEFAULT_DACL>() as u32,
        )
    };
    unsafe { LocalFree(new_dacl as HLOCAL) };
    if ok == 0 {
        return Err(format!(
            "SetTokenInformation(TokenDefaultDacl) failed: {}",
            unsafe { GetLastError() }
        ));
    }
    Ok(())
}

/// Build the Everyone (World) SID via CreateWellKnownSid (Codex token.rs:109-128).
fn world_sid() -> Result<Vec<u8>, String> {
    let mut size: u32 = 0;
    unsafe {
        CreateWellKnownSid(
            WIN_WORLD_SID,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if size == 0 {
        return Err(format!("CreateWellKnownSid sizing failed: {}", unsafe {
            GetLastError()
        }));
    }
    let mut buf = vec![0_u8; size as usize];
    if unsafe {
        CreateWellKnownSid(
            WIN_WORLD_SID,
            std::ptr::null_mut(),
            buf.as_mut_ptr() as *mut c_void,
            &mut size,
        )
    } == 0
    {
        return Err(format!("CreateWellKnownSid failed: {}", unsafe {
            GetLastError()
        }));
    }
    Ok(buf)
}

/// Extract the token-user SID bytes from a token (Codex token.rs:279-317).
fn get_user_sid_bytes(token: HANDLE) -> Result<Vec<u8>, String> {
    let mut needed: u32 = 0;
    unsafe { GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(format!("TokenUser size query returned 0: {}", unsafe {
            GetLastError()
        }));
    }
    let mut buf = vec![0_u8; needed as usize];
    let ok = unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            buf.as_mut_ptr() as *mut c_void,
            needed,
            &mut needed,
        )
    };
    if ok == 0 || (needed as usize) < std::mem::size_of::<TOKEN_USER>() {
        return Err(format!(
            "GetTokenInformation(TokenUser) failed: {}",
            unsafe { GetLastError() }
        ));
    }
    let token_user: TOKEN_USER =
        unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const TOKEN_USER) };
    let sid_len = unsafe { GetLengthSid(token_user.User.Sid) };
    if sid_len == 0 {
        return Err(format!("GetLengthSid(TokenUser) failed: {}", unsafe {
            GetLastError()
        }));
    }
    let mut out = vec![0_u8; sid_len as usize];
    if unsafe {
        CopySid(
            sid_len,
            out.as_mut_ptr() as *mut c_void,
            token_user.User.Sid,
        )
    } == 0
    {
        return Err(format!("CopySid(TokenUser) failed: {}", unsafe {
            GetLastError()
        }));
    }
    Ok(out)
}

/// Extract the logon SID from the token's group list (Codex token.rs:194-277).
///
/// Falls back to the linked (elevated) token if the logon SID is not present
/// on the current token. Returns owned bytes so the caller controls the lifetime.
fn get_logon_sid_bytes(token: HANDLE) -> Result<Vec<u8>, String> {
    if let Some(v) = scan_token_groups_for_logon(token) {
        return Ok(v);
    }
    // Fallback: try the linked token (e.g. elevated token paired with a UAC filtered token).
    const TOKEN_LINKED_TOKEN_CLASS: i32 = 19; // TokenLinkedToken
    #[repr(C)]
    struct TokenLinkedToken {
        linked_token: HANDLE,
    }
    let mut needed: u32 = 0;
    unsafe {
        GetTokenInformation(
            token,
            TOKEN_LINKED_TOKEN_CLASS,
            std::ptr::null_mut(),
            0,
            &mut needed,
        )
    };
    if needed >= std::mem::size_of::<TokenLinkedToken>() as u32 {
        let mut buf = vec![0_u8; needed as usize];
        let ok = unsafe {
            GetTokenInformation(
                token,
                TOKEN_LINKED_TOKEN_CLASS,
                buf.as_mut_ptr() as *mut c_void,
                needed,
                &mut needed,
            )
        };
        if ok != 0 {
            let lt: TokenLinkedToken =
                unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const TokenLinkedToken) };
            if lt.linked_token != 0 {
                let res = scan_token_groups_for_logon(lt.linked_token);
                unsafe { CloseHandle(lt.linked_token) };
                if let Some(v) = res {
                    return Ok(v);
                }
            }
        }
    }
    Err("logon SID not present on token".to_string())
}

/// Scan TOKEN_GROUPS for a group flagged SE_GROUP_LOGON_ID and copy its SID.
fn scan_token_groups_for_logon(token: HANDLE) -> Option<Vec<u8>> {
    let mut needed: u32 = 0;
    unsafe { GetTokenInformation(token, TokenGroups, std::ptr::null_mut(), 0, &mut needed) };
    if needed == 0 {
        return None;
    }
    let mut buf = vec![0_u8; needed as usize];
    let ok = unsafe {
        GetTokenInformation(
            token,
            TokenGroups,
            buf.as_mut_ptr() as *mut c_void,
            needed,
            &mut needed,
        )
    };
    if ok == 0 || (needed as usize) < std::mem::size_of::<u32>() {
        return None;
    }
    let groups = buf.as_ptr() as *const windows_sys::Win32::Security::TOKEN_GROUPS;
    let group_count = unsafe { (*groups).GroupCount } as usize;
    let groups_base = unsafe { std::ptr::addr_of!((*groups).Groups) } as *const SID_AND_ATTRIBUTES;
    for i in 0..group_count {
        let entry = unsafe { std::ptr::read_unaligned(groups_base.add(i)) };
        if (entry.Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID {
            let sid = entry.Sid;
            let sid_len = unsafe { GetLengthSid(sid) };
            if sid_len == 0 {
                return None;
            }
            let mut out = vec![0_u8; sid_len as usize];
            if unsafe { CopySid(sid_len, out.as_mut_ptr() as *mut c_void, sid) } == 0 {
                return None;
            }
            return Some(out);
        }
    }
    None
}

fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
