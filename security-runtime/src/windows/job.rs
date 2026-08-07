use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

pub struct KillOnCloseJob(HANDLE);

impl KillOnCloseJob {
    pub fn new() -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null_mut(), std::ptr::null()) };
        if handle == 0 {
            return Err(format!("CreateJobObjectW failed: {}", unsafe {
                GetLastError()
            }));
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            unsafe { CloseHandle(handle) };
            return Err(format!("SetInformationJobObject failed: {}", unsafe {
                GetLastError()
            }));
        }
        Ok(Self(handle))
    }

    pub fn assign(&self, process: HANDLE) -> Result<(), String> {
        if unsafe { AssignProcessToJobObject(self.0, process) } == 0 {
            return Err(format!("AssignProcessToJobObject failed: {}", unsafe {
                GetLastError()
            }));
        }
        Ok(())
    }
}

impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        if self.0 != 0 {
            unsafe { CloseHandle(self.0) };
        }
    }
}
