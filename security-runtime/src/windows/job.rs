use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_LIMIT_PROCESS_TIME,
};

pub const DEFAULT_ACTIVE_PROCESS_LIMIT: u32 = 64;
pub const DEFAULT_PROCESS_MEMORY_LIMIT: usize = 1024 * 1024 * 1024;
pub const DEFAULT_JOB_MEMORY_LIMIT: usize = 2 * 1024 * 1024 * 1024;
pub const DEFAULT_PROCESS_USER_TIME_LIMIT_100NS: i64 = 30 * 60 * 10_000_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct JobLimits {
    pub active_process_limit: u32,
    pub process_memory_limit: usize,
    pub job_memory_limit: usize,
    pub process_user_time_limit_100ns: i64,
}

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
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_PROCESS_TIME;
        limits.BasicLimitInformation.ActiveProcessLimit = DEFAULT_ACTIVE_PROCESS_LIMIT;
        limits.BasicLimitInformation.PerProcessUserTimeLimit =
            DEFAULT_PROCESS_USER_TIME_LIMIT_100NS;
        limits.ProcessMemoryLimit = DEFAULT_PROCESS_MEMORY_LIMIT;
        limits.JobMemoryLimit = DEFAULT_JOB_MEMORY_LIMIT;
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

    pub fn query_limits(&self) -> Result<JobLimits, String> {
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe {
            QueryInformationJobObject(
                self.0,
                JobObjectExtendedLimitInformation,
                (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(format!("QueryInformationJobObject failed: {}", unsafe {
                GetLastError()
            }));
        }
        let required = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_PROCESS_TIME;
        if limits.BasicLimitInformation.LimitFlags & required != required {
            return Err("Job Object resource-limit flags were not retained".to_string());
        }
        Ok(JobLimits {
            active_process_limit: limits.BasicLimitInformation.ActiveProcessLimit,
            process_memory_limit: limits.ProcessMemoryLimit,
            job_memory_limit: limits.JobMemoryLimit,
            process_user_time_limit_100ns: limits.BasicLimitInformation.PerProcessUserTimeLimit,
        })
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
