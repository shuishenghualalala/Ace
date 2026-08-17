#![cfg(target_os = "linux")]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use sha2::{Digest, Sha256};

#[test]
fn missing_bwrap_without_a_pinned_bundle_is_unavailable_before_started() {
    let workspace = tempfile::tempdir().unwrap();
    let token = "linux-missing-bwrap-token-longer-than-thirty-two";
    let mut child = Command::new(env!("CARGO_BIN_EXE_ace-security-runtime"))
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .env("PATH", "/nonexistent")
        .env_remove("ACE_BUNDLED_BWRAP")
        .env_remove("ACE_BUNDLED_BWRAP_SHA256")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""), "{line}");
    writeln!(
        stdin,
        "{}",
        serde_json::json!({
            "version": 2,
            "token": token,
            "nonce": "linux-missing-bwrap",
            "request": {
                "op": "run",
                "command": ["/bin/true"],
                "cwd": workspace.path(),
                "writable_roots": [workspace.path()]
            }
        })
    )
    .unwrap();
    drop(stdin);
    line.clear();
    stdout.read_line(&mut line).unwrap();

    assert!(line.contains("\"type\":\"error\""), "{line}");
    assert!(line.contains("\"code\":\"sandbox_unavailable\""), "{line}");
    assert!(!line.contains("\"type\":\"started\""), "{line}");
    assert!(child.wait().unwrap().success());
}

#[test]
fn hung_bwrap_setup_is_rejected_and_reaped_before_started() {
    let workspace = tempfile::tempdir().unwrap();
    let bundle = tempfile::tempdir().unwrap();
    let pid_file = bundle.path().join("bwrap.pid");
    let fake_bwrap = bundle.path().join("bwrap");
    std::fs::write(
        &fake_bwrap,
        format!(
            "#!/bin/sh\nprintf '%s' \"$$\" > '{}'\nexec /bin/sleep 60\n",
            pid_file.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&fake_bwrap, std::fs::Permissions::from_mode(0o700)).unwrap();
    let digest = format!("{:x}", Sha256::digest(std::fs::read(&fake_bwrap).unwrap()));

    let token = "linux-readiness-test-token-longer-than-thirty-two";
    let mut child = Command::new(env!("CARGO_BIN_EXE_ace-security-runtime"))
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .env("PATH", "/nonexistent")
        .env("ACE_BUNDLED_BWRAP", &fake_bwrap)
        .env("ACE_BUNDLED_BWRAP_SHA256", digest)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""), "{line}");
    writeln!(
        stdin,
        "{}",
        serde_json::json!({
            "version": 2,
            "token": token,
            "nonce": "linux-hung-bwrap-setup",
            "request": {
                "op": "run",
                "command": ["/bin/true"],
                "cwd": workspace.path(),
                "writable_roots": [workspace.path()]
            }
        })
    )
    .unwrap();
    drop(stdin);

    let (sender, receiver) = mpsc::sync_channel(1);
    let reader = std::thread::spawn(move || {
        let mut terminal = String::new();
        let _ = stdout.read_line(&mut terminal);
        let _ = sender.send(terminal);
    });
    let terminal = match receiver.recv_timeout(Duration::from_secs(8)) {
        Ok(value) => value,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            reap_fake_bwrap(&pid_file);
            let _ = reader.join();
            panic!("runtime did not bound bwrap readiness: {error}");
        }
    };

    assert!(terminal.contains("\"type\":\"error\""), "{terminal}");
    assert!(
        terminal.contains("\"code\":\"sandbox_denied\""),
        "{terminal}"
    );
    assert!(terminal.contains("hardened inner stage"), "{terminal}");
    assert!(!terminal.contains("\"type\":\"started\""), "{terminal}");
    assert!(child.wait().unwrap().success());
    let _ = reader.join();

    let pid: i32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    for _ in 0..100 {
        if unsafe { libc::kill(pid, 0) } != 0 {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    reap_fake_bwrap(&pid_file);
    panic!("timed-out bwrap process {pid} was not reaped");
}

#[test]
fn proc_mount_failure_is_terminal_instead_of_retrying_without_proc() {
    let workspace = tempfile::tempdir().unwrap();
    let bundle = tempfile::tempdir().unwrap();
    let fake_bwrap = bundle.path().join("bwrap");
    let invocation_log = bundle.path().join("invocations");
    std::fs::write(
        &fake_bwrap,
        format!(
            "#!/bin/sh\nprintf x >> '{}'\necho \"bwrap: Can't mount proc on /newroot/proc: Operation not permitted\" >&2\nexit 1\n",
            invocation_log.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&fake_bwrap, std::fs::Permissions::from_mode(0o700)).unwrap();
    let digest = format!("{:x}", Sha256::digest(std::fs::read(&fake_bwrap).unwrap()));

    let token = "linux-proc-failure-token-longer-than-thirty-two";
    let mut child = Command::new(env!("CARGO_BIN_EXE_ace-security-runtime"))
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .env("PATH", "/nonexistent")
        .env("ACE_BUNDLED_BWRAP", &fake_bwrap)
        .env("ACE_BUNDLED_BWRAP_SHA256", digest)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""), "{line}");
    writeln!(
        stdin,
        "{}",
        serde_json::json!({
            "version": 2,
            "token": token,
            "nonce": "linux-proc-mount-failure",
            "request": {
                "op": "run",
                "command": ["/bin/true"],
                "cwd": workspace.path(),
                "writable_roots": [workspace.path()]
            }
        })
    )
    .unwrap();
    drop(stdin);
    line.clear();
    stdout.read_line(&mut line).unwrap();

    assert!(line.contains("\"type\":\"error\""), "{line}");
    assert!(line.contains("\"code\":\"sandbox_denied\""), "{line}");
    assert!(!line.contains("\"type\":\"started\""), "{line}");
    assert!(child.wait().unwrap().success());
    assert_eq!(std::fs::read(&invocation_log).unwrap(), b"x");
}

#[test]
fn killing_runtime_reaps_the_sandbox_process_tree() {
    let bwrap_available = Command::new("bwrap")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success());
    if !bwrap_available {
        assert!(
            std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() != Ok("1"),
            "native Linux runner requires bubblewrap"
        );
        return;
    }

    let workspace = tempfile::tempdir().unwrap();
    let token = "linux-process-cleanup-token-longer-than-thirty-two";
    let mut child = Command::new(env!("CARGO_BIN_EXE_ace-security-runtime"))
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""), "{line}");
    writeln!(
        stdin,
        "{}",
        serde_json::json!({
            "version": 2,
            "token": token,
            "nonce": "linux-parent-death-cleanup",
            "request": {
                "op": "run",
                "command": [
                    "/bin/sh",
                    "-c",
                    concat!(
                        "(trap '' HUP TERM; ",
                        "while :; do /bin/sleep 1; done) & ",
                        "/bin/sleep 60"
                    )
                ],
                "cwd": workspace.path(),
                "writable_roots": [workspace.path()]
            }
        })
    )
    .unwrap();
    drop(stdin);
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"started\""), "{line}");
    let started: serde_json::Value = serde_json::from_str(&line).unwrap();
    let bwrap_pid = started["pid"].as_i64().unwrap() as i32;
    let mut observed_processes = Vec::new();
    for _ in 0..500 {
        observed_processes = process_tree(bwrap_pid);
        if observed_processes.len() >= 2 {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        observed_processes.len() >= 2,
        "sandbox process tree was not observable below bwrap pid {bwrap_pid}"
    );

    child.kill().unwrap();
    let _ = child.wait();
    for _ in 0..500 {
        if observed_processes
            .iter()
            .all(|pid| unsafe { libc::kill(*pid, 0) } != 0)
        {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    for pid in &observed_processes {
        unsafe {
            libc::kill(*pid, libc::SIGKILL);
        }
    }
    panic!("sandbox processes survived runtime death: {observed_processes:?}");
}

fn process_tree(root: i32) -> Vec<i32> {
    let mut pending = vec![root];
    let mut result = Vec::new();
    while let Some(pid) = pending.pop() {
        if result.contains(&pid) {
            continue;
        }
        result.push(pid);
        if let Ok(children) = std::fs::read_to_string(format!("/proc/{pid}/task/{pid}/children")) {
            pending.extend(
                children
                    .split_whitespace()
                    .filter_map(|value| value.parse::<i32>().ok()),
            );
        }
    }
    result
}

fn reap_fake_bwrap(pid_file: &std::path::Path) {
    if let Ok(pid) = std::fs::read_to_string(pid_file).and_then(|value| {
        value
            .parse::<i32>()
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }) {
        unsafe {
            libc::kill(pid, libc::SIGKILL);
        }
    }
}
