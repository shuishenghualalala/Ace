#![cfg(windows)]

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};

#[test]
fn dedicated_identity_writes_workspace_but_not_denied_or_protected_paths() {
    let required = std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() == Ok("1");
    let Some(state_dir) = std::env::var_os("ACE_WINDOWS_NATIVE_STATE_DIR") else {
        assert!(
            !required,
            "release gate requires an installed sandbox identity fixture"
        );
        return;
    };
    let state_dir = PathBuf::from(state_dir);
    let workspace = tempfile::tempdir().unwrap();
    let denied = tempfile::tempdir().unwrap();
    std::fs::create_dir(workspace.path().join(".git")).unwrap();
    std::fs::write(workspace.path().join(".git/config"), "original").unwrap();
    std::fs::write(denied.path().join("secret.txt"), "host-only").unwrap();
    let command =
        std::env::var("ComSpec").unwrap_or_else(|_| r"C:\Windows\System32\cmd.exe".to_string());
    let script = format!(
        "set /p INPUT= && if not \"%INPUT%\"==\"prompt\" exit /b 43 && if not \"%CUSTOM_ENV%\"==\"custom\" exit /b 44 && echo allowed>allowed.txt && type .git\\config >NUL 2>NUL || exit /b 48 && (type \"{}\" >NUL 2>NUL && exit /b 41 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 45 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 46 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 47 || ver>NUL) && (echo denied>.git\\config && exit /b 42 || exit /b 0)",
        denied.path().join("secret.txt").display(),
        state_dir.join("windows-sandbox-identity.json").display(),
        state_dir.join("windows-capability-sids.json").display(),
        state_dir.join("windows-acl-state.json").display(),
    );
    let token = "windows-native-test-token-longer-than-thirty-two";
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .env("ACE_SECURITY_STATE_DIR", &state_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    let request = serde_json::json!({
        "version": 2,
        "token": token,
        "nonce": "windows-native-sandbox-nonce",
        "request": {
            "op": "run",
            "command": [command, "/d", "/s", "/c", script],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "readonly_roots": [workspace.path().join(".git")],
            "denied_roots": [denied.path()],
            "network_enabled": false,
            "max_output_bytes": 65536,
            "stdin_b64": "cHJvbXB0DQo=",
            "env_overrides": {"CUSTOM_ENV": "custom"}
        }
    });
    writeln!(stdin, "{request}").unwrap();
    drop(stdin);
    let mut started = false;
    let mut exit_code = None;
    loop {
        line.clear();
        if stdout.read_line(&mut line).unwrap() == 0 {
            break;
        }
        let event: serde_json::Value = serde_json::from_str(&line).unwrap();
        match event["type"].as_str() {
            Some("started") => started = true,
            Some("completed") => {
                exit_code = event["exit_code"].as_i64();
                break;
            }
            Some("error") => panic!("runtime error: {event}"),
            _ => {}
        }
    }
    assert!(started);
    assert_eq!(exit_code, Some(0));
    assert!(workspace.path().join("allowed.txt").exists());
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(".git/config")).unwrap(),
        "original"
    );
    assert!(child.wait().unwrap().success());
}

#[test]
fn runner_protocol_is_streaming_and_child_only() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let process = fs::read_to_string(format!("{manifest}/src/windows/process.rs")).unwrap();
    assert!(process.contains("stdin_b64"));
    assert!(process.contains("env_overrides"));
    assert!(process.contains("RunnerEvent"));
    assert!(process.contains("CreateProcessAsUserW"));
    assert!(process.contains("RuntimeMessage::Stdout"));
    assert!(!process.contains("RunnerResponse"));
}
