#![cfg(target_os = "linux")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;

#[test]
fn native_linux_gate_is_explicit() {
    if Command::new("bwrap").arg("--version").status().is_err() {
        eprintln!("bwrap unavailable: release CI must provide system or bundled bwrap");
        return;
    }
    assert!(std::path::Path::new("/proc/self/ns/user").exists());
    assert!(std::path::Path::new("/proc/self/ns/pid").exists());
}

#[test]
fn native_fixture_reaches_authenticated_dispatch() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "classify_shell",
            "shell_kind": "bash",
            "executable": "/bin/bash",
            "raw_command": "pwd"
        }),
    );
    assert_eq!(events[0]["type"], "classified");
}

#[test]
fn stdin_environment_and_output_stream_through_hardened_child() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", "cat; printf ':%s' \"$CUSTOM_ENV\"; printf err >&2"],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "stdin_b64": BASE64_STANDARD.encode(b"prompt"),
            "env_overrides": {"CUSTOM_ENV": "custom"},
            "max_output_bytes": 1024
        }),
    );
    assert_eq!(events.first().unwrap()["type"], "started");
    assert_eq!(events.last().unwrap()["type"], "completed");
    let stdout = collect_stream(&events, "stdout");
    let stderr = collect_stream(&events, "stderr");
    assert_eq!(stdout, b"prompt:custom");
    assert_eq!(stderr, b"err");
}

#[test]
fn output_is_live_and_combined_limit_fails_closed() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", "printf early; sleep 0.2; printf late"],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "max_output_bytes": 1024
        }),
    );
    assert_eq!(events[0]["type"], "started");
    assert_eq!(events[1]["type"], "stdout");
    assert_eq!(events.last().unwrap()["type"], "completed");
    assert_eq!(collect_stream(&events, "stdout"), b"earlylate");

    let overflow = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", "printf 123456"],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "max_output_bytes": 5
        }),
    );
    assert_eq!(overflow.last().unwrap()["type"], "error");
    assert_eq!(overflow.last().unwrap()["code"], "output_truncated");
}

#[test]
fn managed_proxy_restricts_socket_families() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/usr/bin/python3",
                "-c",
                concat!(
                    "import socket,sys\n",
                    "try:\n socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n",
                    "except PermissionError:\n pass\n",
                    "else:\n sys.exit(1)\n",
                    "left,right=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)\n",
                    "left.sendall(b'ok')\n",
                    "sys.exit(0 if right.recv(2)==b'ok' else 2)\n"
                )
            ],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": true,
            "max_output_bytes": 1024
        }),
    );
    assert_eq!(events.last().unwrap()["type"], "completed");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
}

#[test]
fn direct_network_connects_with_file_sandbox_applied() {
    let workspace = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let blocked = outside.path().join("blocked");
    std::fs::create_dir(workspace.path().join(".git")).unwrap();
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port().to_string();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/usr/bin/python3",
                "-c",
                concat!(
                    "import pathlib,socket,sys\n",
                    "with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=2): pass\n",
                    "pathlib.Path('allowed').write_text('ok')\n",
                    "for target in (pathlib.Path('.git/blocked'), pathlib.Path(sys.argv[2])):\n",
                    " try: target.write_text('denied')\n",
                    " except OSError: pass\n",
                    " else: sys.exit(1)\n",
                ),
                port,
                blocked,
            ],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_access": true,
        }),
    );
    assert_eq!(events[0]["type"], "started", "{events:?}");
    assert_eq!(events[0]["capabilities"]["direct_network"], true);
    assert_eq!(events[0]["capabilities"]["managed_network"], false);
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert!(workspace.path().join("allowed").is_file());
    assert!(!workspace.path().join(".git/blocked").exists());
    assert!(!blocked.exists());
}

fn run_request(cwd: &std::path::Path, request: serde_json::Value) -> Vec<serde_json::Value> {
    let token = "linux-native-test-token-longer-than-thirty-two";
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
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
    // Each helper process serves one request, including when the test binary is PID 1.
    let nonce = "linux-native-single-request";
    let envelope = serde_json::json!({
        "version": 3,
        "token": token,
        "nonce": nonce,
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
        event["seq"].as_u64() == Some(index as u64) && event["nonce"].as_str() == Some(nonce)
    }));
    assert!(cwd.is_dir());
    events
}

fn collect_stream(events: &[serde_json::Value], stream: &str) -> Vec<u8> {
    events
        .iter()
        .filter(|event| event["type"] == stream)
        .flat_map(|event| {
            BASE64_STANDARD
                .decode(event["data_b64"].as_str().unwrap())
                .unwrap()
        })
        .collect()
}
