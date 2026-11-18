#![cfg(windows)]

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

struct RunOutcome {
    exit_code: Option<i64>,
    transcript: String,
}

/// Spawn the runtime, send one run request for `script`, and capture the
/// child's exit code plus everything it wrote to stdout/stderr.
fn run_script(
    state_dir: &Path,
    workspace: &Path,
    denied: &Path,
    script: String,
    direct_network: bool,
) -> RunOutcome {
    let command =
        std::env::var("ComSpec").unwrap_or_else(|_| r"C:\Windows\System32\cmd.exe".to_string());
    let argv = vec![
        command,
        "/v:on".into(),
        "/d".into(),
        "/s".into(),
        "/c".into(),
        script,
    ];
    run_command(state_dir, workspace, denied, argv, direct_network)
}

fn run_command(
    state_dir: &Path,
    workspace: &Path,
    denied: &Path,
    command: Vec<String>,
    direct_network: bool,
) -> RunOutcome {
    let token = "windows-native-test-token-longer-than-thirty-two";
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", token)
        .env("ACE_SECURITY_STATE_DIR", state_dir)
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
            "command": command,
            "cwd": workspace,
            "writable_roots": [workspace],
            "readonly_roots": [workspace.join(".git")],
            "denied_roots": [denied],
            "network_access": direct_network,
            "max_output_bytes": 65536,
            "stdin_b64": "cHJvbXB0DQo=",
            "env_overrides": {"CUSTOM_ENV": "custom"}
        }
    });
    writeln!(stdin, "{request}").unwrap();
    drop(stdin);
    let mut started = false;
    let mut exit_code = None;
    let mut transcript = String::new();
    loop {
        line.clear();
        if stdout.read_line(&mut line).unwrap() == 0 {
            break;
        }
        let event: serde_json::Value = serde_json::from_str(&line).unwrap();
        match event["type"].as_str() {
            Some("started") => {
                started = true;
                assert_eq!(event["capabilities"]["direct_network"], direct_network);
                assert_eq!(event["capabilities"]["managed_network"], false);
            }
            Some("stdout") | Some("stderr") => {
                if let Some(data) = event["data_b64"].as_str() {
                    use base64::Engine;
                    if let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(data) {
                        transcript.push_str(&String::from_utf8_lossy(&bytes));
                    }
                }
            }
            Some("completed") => {
                exit_code = event["exit_code"].as_i64();
                break;
            }
            Some("error") => panic!("runtime error: {event}"),
            _ => {}
        }
    }
    assert!(started, "child never started; transcript:\n{transcript}");
    assert!(
        child.wait().unwrap().success(),
        "runtime exited nonzero; transcript:\n{transcript}"
    );
    RunOutcome {
        exit_code,
        transcript,
    }
}

#[test]
fn dedicated_identity_writes_workspace_but_not_denied_or_protected_paths() {
    check_file_boundary(false);
}

#[test]
fn direct_network_connects_with_file_sandbox_applied() {
    check_file_boundary(true);
}

fn check_file_boundary(direct_network: bool) {
    let required = std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() == Ok("1");
    let Some(state_dir) = std::env::var_os("ACE_WINDOWS_NATIVE_STATE_DIR") else {
        assert!(
            !required,
            "release gate requires an installed sandbox identity fixture"
        );
        return;
    };
    let state_dir = PathBuf::from(state_dir);
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let network_probe = if direct_network {
        format!(
            "(curl.exe --noproxy * --max-time 4 -fsS http://127.0.0.1:{port}/ || exit /b 49) & "
        )
    } else {
        String::new()
    };
    let workspace = tempfile::tempdir().unwrap();
    let denied = tempfile::tempdir().unwrap();
    std::fs::create_dir(workspace.path().join(".git")).unwrap();
    std::fs::write(workspace.path().join(".git/config"), "original").unwrap();
    std::fs::write(denied.path().join("secret.txt"), "host-only").unwrap();

    // Diagnostic A: plain writable-root write with no stdin interaction.
    let write_only = run_script(
        &state_dir,
        workspace.path(),
        denied.path(),
        "echo A-START & echo allowed>allowed.txt & echo A-WROTE & dir /b & exit /b 0".to_string(),
        direct_network,
    );
    // Diagnostic B: stdin delivery + delayed expansion with unconditional
    // separators, so no conditional can silently skip a step.
    let stdin_probe = run_script(
        &state_dir,
        workspace.path(),
        denied.path(),
        "echo B-START & set /p INPUT= & echo B-GOT-[!INPUT!] & echo B-END & exit /b 0".to_string(),
        direct_network,
    );
    // Full chain: cmd.exe expands %VAR% for the whole compound line at parse
    // time, before `set /p` runs, so the prompt check must use delayed
    // expansion (`/v:on` + `!INPUT!`); %CUSTOM_ENV% is safe because it arrives
    // via the child environment and exists before the line is parsed.
    let script = format!(
        "{network_probe}set /p INPUT= && if not \"!INPUT!\"==\"prompt\" exit /b 43 && if not \"%CUSTOM_ENV%\"==\"custom\" exit /b 44 && echo allowed>allowed.txt && type .git\\config >NUL 2>NUL || exit /b 48 && (type \"{}\" >NUL 2>NUL && exit /b 41 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 45 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 46 || ver>NUL) && (type \"{}\" >NUL 2>NUL && exit /b 47 || ver>NUL) && (echo denied>.git\\config && exit /b 42 || exit /b 0)",
        denied.path().join("secret.txt").display(),
        state_dir.join("windows-sandbox-identity.json").display(),
        state_dir.join("windows-capability-sids.json").display(),
        state_dir.join("windows-acl-state.json").display(),
    );
    let server = direct_network.then(|| {
        use std::io::Read;
        use std::time::{Duration, Instant};
        listener.set_nonblocking(true).unwrap();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(10);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "sandbox never connected");
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("accept failed: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = [0; 256];
            let bytes = stream.read(&mut request).unwrap();
            assert!(request[..bytes].starts_with(b"GET / HTTP/"));
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
        })
    });
    let scratch = run_command(
        &state_dir,
        workspace.path(),
        denied.path(),
        vec![
            PathBuf::from(std::env::var_os("SystemRoot").unwrap_or_else(|| r"C:\Windows".into()))
                .join(r"System32\WindowsPowerShell\v1.0\powershell.exe")
                .to_string_lossy()
                .into_owned(),
            "-NoProfile".into(),
            "-Command".into(),
            concat!(
                "$ErrorActionPreference='Stop';",
                "if ($env:TEMP -ne $env:TMP -or $env:TEMP -ne $env:TMPDIR) {exit 60};",
                "$f=[IO.Path]::Combine($env:TEMP, 'ace-check.txt');",
                "[IO.File]::WriteAllText($f, 'scratch');",
                "if ([IO.File]::ReadAllText($f) -ne 'scratch') {exit 61};",
                "Write-Output ('ACE-SCRATCH=' + $env:TEMP)"
            )
            .into(),
        ],
        direct_network,
    );
    assert_eq!(scratch.exit_code, Some(0), "{}", scratch.transcript);
    let path = scratch
        .transcript
        .lines()
        .find_map(|line| line.strip_prefix("ACE-SCRATCH="))
        .unwrap_or_else(|| panic!("sandbox scratch transcript: {}", scratch.transcript));
    assert!(
        Path::new(path)
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("ace-sandbox-"),
        "{path}"
    );
    assert!(!Path::new(path).exists(), "scratch not removed: {path}");

    let full = run_script(
        &state_dir,
        workspace.path(),
        denied.path(),
        script,
        direct_network,
    );
    if let Some(server) = server {
        server.join().unwrap();
    }

    assert_eq!(
        write_only.exit_code,
        Some(0),
        "write-only transcript:\n{}",
        write_only.transcript
    );
    assert_eq!(
        stdin_probe.exit_code,
        Some(0),
        "stdin-probe transcript:\n{}",
        stdin_probe.transcript
    );
    assert_eq!(
        full.exit_code,
        Some(0),
        "full-chain transcript:\n{}\nwrite-only transcript:\n{}\nstdin-probe transcript:\n{}",
        full.transcript,
        write_only.transcript,
        stdin_probe.transcript
    );
    assert!(
        std::fs::read_to_string(workspace.path().join("allowed.txt")).is_ok(),
        "full-chain transcript:\n{}\nwrite-only transcript:\n{}",
        full.transcript,
        write_only.transcript
    );
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(".git/config")).unwrap(),
        "original"
    );
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
