use std::os::windows::ffi::OsStrExt;

use windows_sys::Win32::Foundation::{GetLastError, LocalFree, ERROR_SUCCESS, HLOCAL};
use windows_sys::Win32::Security::Authorization::{
    SetEntriesInAclW, SetSecurityInfo, EXPLICIT_ACCESS_W, GRANT_ACCESS, SE_WINDOW_OBJECT,
    TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::DACL_SECURITY_INFORMATION;
use windows_sys::Win32::System::StationsAndDesktops::{
    CloseDesktop, CreateDesktopW, DESKTOP_CREATEMENU, DESKTOP_CREATEWINDOW, DESKTOP_DELETE,
    DESKTOP_ENUMERATE, DESKTOP_HOOKCONTROL, DESKTOP_JOURNALPLAYBACK, DESKTOP_JOURNALRECORD,
    DESKTOP_READOBJECTS, DESKTOP_READ_CONTROL, DESKTOP_SWITCHDESKTOP, DESKTOP_WRITEOBJECTS,
    DESKTOP_WRITE_DAC, DESKTOP_WRITE_OWNER,
};

use super::token::current_logon_sid_bytes;

const DESKTOP_ALL_ACCESS: u32 = DESKTOP_READOBJECTS
    | DESKTOP_CREATEWINDOW
    | DESKTOP_CREATEMENU
    | DESKTOP_HOOKCONTROL
    | DESKTOP_JOURNALRECORD
    | DESKTOP_JOURNALPLAYBACK
    | DESKTOP_ENUMERATE
    | DESKTOP_WRITEOBJECTS
    | DESKTOP_SWITCHDESKTOP
    | DESKTOP_DELETE
    | DESKTOP_READ_CONTROL
    | DESKTOP_WRITE_DAC
    | DESKTOP_WRITE_OWNER;

/// Keeps the private Desktop alive for the complete child process lifetime.
pub struct LaunchDesktop {
    _private: PrivateDesktop,
    startup_name: Vec<u16>,
}

impl LaunchDesktop {
    pub fn prepare() -> Result<Self, String> {
        let private = PrivateDesktop::create()?;
        let startup_name = wide(format!("Winsta0\\{}", private.name));
        Ok(Self {
            _private: private,
            startup_name,
        })
    }

    pub fn startup_info_desktop(&self) -> *mut u16 {
        self.startup_name.as_ptr() as *mut u16
    }
}

struct PrivateDesktop {
    handle: isize,
    name: String,
}

impl PrivateDesktop {
    fn create() -> Result<Self, String> {
        let name = format!("AceSandboxDesktop-{:x}", rand::random::<u128>());
        let name_wide = wide(&name);
        let handle = unsafe {
            CreateDesktopW(
                name_wide.as_ptr(),
                std::ptr::null(),
                std::ptr::null_mut(),
                0,
                DESKTOP_ALL_ACCESS,
                std::ptr::null_mut(),
            )
        };
        if handle == 0 {
            return Err(format!("CreateDesktopW failed: {}", unsafe {
                GetLastError()
            }));
        }
        if let Err(error) = unsafe { grant_access(handle) } {
            unsafe { CloseDesktop(handle) };
            return Err(error);
        }
        Ok(Self { handle, name })
    }
}

unsafe fn grant_access(handle: isize) -> Result<(), String> {
    let mut logon_sid = current_logon_sid_bytes()?;
    let entries = [EXPLICIT_ACCESS_W {
        grfAccessPermissions: DESKTOP_ALL_ACCESS,
        grfAccessMode: GRANT_ACCESS,
        grfInheritance: 0,
        Trustee: TRUSTEE_W {
            pMultipleTrustee: std::ptr::null_mut(),
            MultipleTrusteeOperation: 0,
            TrusteeForm: TRUSTEE_IS_SID,
            TrusteeType: TRUSTEE_IS_UNKNOWN,
            ptstrName: logon_sid.as_mut_ptr().cast::<u16>(),
        },
    }];
    let mut dacl = std::ptr::null_mut();
    let status = SetEntriesInAclW(
        entries.len() as u32,
        entries.as_ptr(),
        std::ptr::null_mut(),
        &mut dacl,
    );
    if status != ERROR_SUCCESS {
        return Err(format!("SetEntriesInAclW failed: {status}"));
    }
    let status = SetSecurityInfo(
        handle,
        SE_WINDOW_OBJECT,
        DACL_SECURITY_INFORMATION,
        std::ptr::null_mut(),
        std::ptr::null_mut(),
        dacl,
        std::ptr::null_mut(),
    );
    if !dacl.is_null() {
        LocalFree(dacl as HLOCAL);
    }
    if status != ERROR_SUCCESS {
        return Err(format!("SetSecurityInfo failed: {status}"));
    }
    Ok(())
}

impl Drop for PrivateDesktop {
    fn drop(&mut self) {
        if self.handle != 0 {
            unsafe { CloseDesktop(self.handle) };
        }
    }
}

fn wide(value: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
