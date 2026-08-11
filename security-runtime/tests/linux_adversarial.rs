#![cfg(target_os = "linux")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

fn native_tests_required() -> bool {
    std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() == Ok("1")
}

#[test]
fn managed_profile_blocks_outside_and_protected_writes() {
    if Command::new("bwrap")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_err()
    {
        assert!(!native_tests_required(), "release gate requires bubblewrap");
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    std::fs::create_dir(workspace.path().join(".git")).unwrap();
    std::fs::write(workspace.path().join(".git/config"), "original").unwrap();
    std::fs::write(outside.path().join("host-secret"), "secret").unwrap();
    let script = format!(
        "echo allowed > allowed.txt && test \"$(cat .git/config)\" = original && ! test -r '{}/host-secret' && ! echo denied > '{}/denied.txt' && ! echo denied > .git/config",
        outside.path().display(),
        outside.path().display()
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
        "allowed\n"
    );
    assert!(!outside.path().join("denied.txt").exists());
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(".git/config")).unwrap(),
        "original"
    );
}

fn run_request(request: serde_json::Value) -> Vec<serde_json::Value> {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "native-test-token-longer-than-thirty-two-bytes";
    let mut child = Command::new(binary)
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
    assert!(line.contains("\"stdin_once\""));
    let envelope = serde_json::json!({
        "version": 2,
        "token": token,
        "nonce": format!("linux-adversarial-{}", std::process::id()),
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
    assert!(events.iter().enumerate().all(|(index, event)| {
        event["seq"].as_u64() == Some(index as u64)
            && event["nonce"]
                .as_str()
                .is_some_and(|nonce| nonce.starts_with("linux-adversarial-"))
    }));
    events
}
