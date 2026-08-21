#![cfg(windows)]

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};

#[test]
#[ignore = "requires an installed Windows sandbox fixture"]
fn dedicated_identity_writes_and_deletes_approved_paths_but_not_denied_or_protected_paths() {
    let state_dir = PathBuf::from(
        std::env::var_os("ACE_WINDOWS_NATIVE_STATE_DIR")
            .expect("ACE_WINDOWS_NATIVE_STATE_DIR must name an installed sandbox fixture"),
    );
    let workspace = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let readonly = tempfile::tempdir().unwrap();
    let readonly_file = readonly.path().join("readable.txt");
    std::fs::write(&readonly_file, "readable").unwrap();
    let denied = workspace.path().join("denied");
    std::fs::create_dir(&denied).unwrap();
    std::fs::write(denied.join("secret.txt"), "host-only").unwrap();
    std::fs::write(workspace.path().join("delete-me.txt"), "delete-me").unwrap();
    let outside_file = outside.path().join("delete-me.txt");
    std::fs::write(&outside_file, "delete-me").unwrap();
    std::fs::create_dir(workspace.path().join("delete-dir")).unwrap();
    std::fs::write(
        workspace.path().join("delete-dir").join("child.txt"),
        "delete-me",
    )
    .unwrap();
    std::fs::create_dir(workspace.path().join(".agents")).unwrap();
    std::fs::write(workspace.path().join(".agents/guard.txt"), "protected").unwrap();
    let command =
        std::env::var("ComSpec").unwrap_or_else(|_| r"C:\Windows\System32\cmd.exe".to_string());
    let script = vec![
        r#"set /p INPUT="#.to_string(),
        r#"if not "%INPUT%"=="prompt" exit /b 43"#.to_string(),
        r#"if not "%CUSTOM_ENV%"=="custom" exit /b 44"#.to_string(),
        r#"if not "%ACE_SANDBOX%"=="windows-sandbox-account" exit /b 56"#.to_string(),
        r#"if not exist "%TEMP%" exit /b 51"#.to_string(),
        r#"echo transient>"%TEMP%\child.txt""#.to_string(),
        "echo allowed>allowed.txt".to_string(),
        r#"del /q "delete-me.txt" >NUL 2>NUL"#.to_string(),
        r#"if exist "delete-me.txt" exit /b 48"#.to_string(),
        r#"rmdir /s /q "delete-dir" >NUL 2>NUL"#.to_string(),
        r#"if exist "delete-dir" exit /b 49"#.to_string(),
        format!(r#"del /q "{}" >NUL 2>NUL"#, outside_file.display()),
        format!(r#"if exist "{}" exit /b 50"#, outside_file.display()),
        format!(
            r#"(type "{}" >NUL 2>NUL && exit /b 41 || ver>NUL)"#,
            denied.join("secret.txt").display()
        ),
        format!(
            r#"(type "{}" >NUL 2>NUL && exit /b 45 || ver>NUL)"#,
            state_dir.join("windows-sandbox-identity.json").display()
        ),
        format!(
            r#"(type "{}" >NUL 2>NUL && exit /b 46 || ver>NUL)"#,
            state_dir.join("windows-capability-sids.json").display()
        ),
        format!(
            r#"(type "{}" >NUL 2>NUL && exit /b 47 || ver>NUL)"#,
            state_dir.join("windows-acl-state.json").display()
        ),
        format!(r#"type "{}" >NUL"#, readonly_file.display()),
        format!(
            r#"(echo denied>"{}" 2>NUL && exit /b 55 || ver>NUL)"#,
            readonly.path().join("forbidden.txt").display()
        ),
        "(move /y .agents .agents-moved >NUL 2>NUL && exit /b 52 || ver>NUL)".to_string(),
        "(del /q .agents\\guard.txt >NUL 2>NUL && exit /b 53 || ver>NUL)".to_string(),
        "(echo denied>.agents\\guard.txt && exit /b 54 || ver>NUL)".to_string(),
        "(echo denied>.git\\config && exit /b 42 || exit /b 0)".to_string(),
    ]
    .join(" && ");
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
        "version": 3,
        "token": token,
        "nonce": "windows-native-sandbox-nonce",
        "request": {
            "op": "run",
            "command": [command, "/d", "/s", "/c", script],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path(), outside.path()],
            "readable_roots": [readonly.path()],
            "denied_roots": [&denied],
            "network_enabled": false,
            "max_output_bytes": 65536,
            "stdin_b64": "cHJvbXB0DQo=",
            "env_overrides": {"CUSTOM_ENV": "custom"}
        }
    });
    writeln!(stdin, "{request}").unwrap();
    drop(stdin);
    let mut started = false;
    let mut capabilities = None;
    let mut exit_code = None;
    loop {
        line.clear();
        if stdout.read_line(&mut line).unwrap() == 0 {
            break;
        }
        let event: serde_json::Value = serde_json::from_str(&line).unwrap();
        match event["type"].as_str() {
            Some("started") => {
                started = true;
                capabilities = Some(event["capabilities"].clone());
            }
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
    let capabilities = capabilities.unwrap();
    assert_eq!(capabilities["windows_restricted_token"], true);
    assert_eq!(capabilities["windows_acl"], true);
    assert_eq!(capabilities["windows_job"], true);
    assert_eq!(capabilities["windows_wfp"], true);
    assert_eq!(capabilities["managed_network"], false);
    assert!(workspace.path().join("allowed.txt").exists());
    assert!(!outside_file.exists());
    assert!(!readonly.path().join("forbidden.txt").exists());
    assert!(!workspace.path().join(".git/config").exists());
    assert!(!workspace.path().join(".git").exists());
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(".agents/guard.txt")).unwrap(),
        "protected"
    );
    assert!(!workspace.path().join(".agents-moved").exists());
    assert_eq!(
        std::fs::read_to_string(denied.join("secret.txt")).unwrap(),
        "host-only"
    );
    assert!(!state_dir.join("windows-acl-state.json").exists());
    assert!(!state_dir.join("windows-acl-cleanup.log").exists());
    let runs = state_dir.join("windows-runs");
    assert!(runs.is_dir());
    assert_eq!(std::fs::read_dir(runs).unwrap().count(), 0);
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

#[test]
fn windows_gui_and_user_visibility_boundaries_are_fail_closed() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let process = fs::read_to_string(format!("{manifest}/src/windows/process.rs")).unwrap();
    let desktop = fs::read_to_string(format!("{manifest}/src/windows/desktop.rs")).unwrap();
    let users = fs::read_to_string(format!("{manifest}/src/windows/users.rs")).unwrap();

    assert!(process.contains("LaunchDesktop::prepare()"));
    assert!(process.contains("lpDesktop = desktop.startup_info_desktop()"));
    assert!(desktop.contains("CreateDesktopW"));
    assert!(desktop.contains("SetSecurityInfo"));
    assert!(desktop.contains("current_logon_sid_bytes"));
    assert!(users.contains("RegSetValueExW"));
    assert!(users.contains("RegDeleteValueW"));
    assert!(!process
        .lines()
        .any(|line| line.contains("lpDesktop") && line.contains("Winsta0")));
}
