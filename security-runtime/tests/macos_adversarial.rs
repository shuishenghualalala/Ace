#![cfg(target_os = "macos")]

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

fn native_tests_required() -> bool {
    std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() == Ok("1")
}

fn seatbelt_available() -> bool {
    Command::new("/usr/bin/sandbox-exec")
        .args(["-p", "(version 1) (allow default)", "/usr/bin/true"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

#[test]
fn mac_001_002_003_010_managed_profile_enforces_read_write_and_deny_precedence() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let read_only = tempfile::tempdir().unwrap();
    std::fs::create_dir(workspace.path().join(".git")).unwrap();
    std::fs::write(workspace.path().join(".git/config"), "original").unwrap();
    std::fs::write(outside.path().join("host-secret"), "secret").unwrap();
    std::fs::write(read_only.path().join("readable"), "read-only").unwrap();
    std::os::unix::fs::symlink(outside.path(), workspace.path().join("outside-link")).unwrap();
    let script = format!(
        "printf allowed > allowed.txt; test \"$(cat .git/config)\" = original || exit 34; cat '{}/host-secret' >/dev/null 2>&1 && exit 31; printf denied > '{}/denied.txt' 2>/dev/null && exit 32; printf denied > .git/config 2>/dev/null && exit 33; exit 0",
        outside.path().display(),
        outside.path().display(),
        read_only.path().display(),
    );
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "readonly_roots": [workspace.path().join(".git")],
        "denied_roots": [outside.path()],
        "network_enabled": false,
        "max_output_bytes": 65536
    }));
    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["type"], "completed", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("allowed.txt")).unwrap(),
        "allowed"
    );
    assert!(!outside.path().join("denied.txt").exists());
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(".git/config")).unwrap(),
        "original"
    );
}

fn run_request(request: serde_json::Value) -> Vec<serde_json::Value> {
    run_request_with_parent_env(request, &[])
}

fn run_request_with_parent_env(
    request: serde_json::Value,
    parent_env: &[(&str, String)],
) -> Vec<serde_json::Value> {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "native-test-token-longer-than-thirty-two-bytes";
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .envs(parent_env.iter().cloned())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"stdin_once\""));
    let envelope = serde_json::json!({
        "version": 3,
        "token": token,
        "nonce": format!("macos-adversarial-{}", std::process::id()),
        "request": request,
    });
    writeln!(stdin, "{envelope}").unwrap();
    drop(stdin);

    let mut events = Vec::new();
    loop {
        line.clear();
        if stdout.read_line(&mut line).unwrap() == 0 {
            break;
        }
        let event: serde_json::Value = serde_json::from_str(&line).unwrap();
        let terminal = matches!(event["type"].as_str(), Some("completed" | "error"));
        events.push(event);
        if terminal {
            break;
        }
    }
    assert!(child.wait().unwrap().success());
    events
}

fn stdout_text(events: &[serde_json::Value]) -> String {
    let bytes = events
        .iter()
        .filter(|event| event["type"] == "stdout")
        .flat_map(|event| {
            BASE64_STANDARD
                .decode(event["data_b64"].as_str().unwrap())
                .unwrap()
        })
        .collect::<Vec<_>>();
    String::from_utf8(bytes).unwrap()
}

fn process_exists(pid: i32) -> bool {
    let result = unsafe { libc::kill(pid, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}
