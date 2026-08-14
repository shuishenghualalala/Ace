#![cfg(windows)]

use std::fs;
use std::os::windows::io::AsRawHandle;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[path = "../src/windows/job.rs"]
mod job;

#[test]
fn closing_job_terminates_assigned_process() {
    let mut child = Command::new("ping.exe")
        .args(["-n", "60", "127.0.0.1"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    thread::sleep(Duration::from_millis(100));
    assert!(child.try_wait().unwrap().is_none());

    let job = job::KillOnCloseJob::new().unwrap();
    job.assign(child.as_raw_handle() as isize).unwrap();
    drop(job);

    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline && child.try_wait().unwrap().is_none() {
        thread::sleep(Duration::from_millis(20));
    }
    assert!(
        child.try_wait().unwrap().is_some(),
        "assigned process survived closing a kill-on-close Job"
    );
}

#[test]
fn process_is_assigned_before_resume() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let process = fs::read_to_string(format!("{manifest}/src/windows/process.rs")).unwrap();
    assert!(
        process.find("job.assign").unwrap()
            < process.find("ResumeThread(process_info.hThread)").unwrap()
    );
}
