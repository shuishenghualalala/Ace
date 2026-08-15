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
    std::fs::write(outside.path().join("host-secret"), "secret").unwrap();
    std::fs::write(read_only.path().join("readable"), "read-only").unwrap();
    std::os::unix::fs::symlink(outside.path(), workspace.path().join("outside-link")).unwrap();
    let script = format!(
        "test \"$(cat '{}/readable')\" = read-only || exit 30; printf allowed > allowed.txt; cat '{}/host-secret' >/dev/null 2>&1 && exit 31; printf denied > '{}/denied.txt' 2>/dev/null && exit 32; printf denied > '{}/not-writable' 2>/dev/null && exit 33; printf denied > .git/config 2>/dev/null && exit 34; mkdir .agents 2>/dev/null && exit 35; printf denied > outside-link/via-symlink 2>/dev/null && exit 36; exit 0",
        read_only.path().display(),
        outside.path().display(),
        outside.path().display(),
        read_only.path().display(),
    );
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "readable_roots": [read_only.path()],
        "denied_roots": [
            outside.path(),
            workspace.path().join(".git"),
            workspace.path().join(".agents")
        ],
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
    assert!(!read_only.path().join("not-writable").exists());
    assert!(!workspace.path().join(".git/config").exists());
    assert!(!workspace.path().join(".agents").exists());
    assert!(!outside.path().join("via-symlink").exists());
}

#[test]
fn mac_002_profile_parameters_handle_seatbelt_metacharacters() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let base = tempfile::tempdir().unwrap();
    let workspace = base.path().join("quoted\"\n(allow default)\n(require-any");
    std::fs::create_dir(&workspace).unwrap();
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", "printf escaped > result"],
        "cwd": &workspace,
        "writable_roots": [&workspace],
        "network_enabled": false
    }));
    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert_eq!(
        std::fs::read_to_string(workspace.join("result")).unwrap(),
        "escaped"
    );
}

#[test]
fn mac_004_005_007_managed_network_uses_proxy_and_blocks_raw_loopback() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    listener.set_nonblocking(true).unwrap();
    let server = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut reader = BufReader::new(stream.try_clone().unwrap());
                    let mut first_line = String::new();
                    let _ = reader.read_line(&mut first_line);
                    if first_line.starts_with("GET /through-proxy HTTP/") {
                        stream
                            .write_all(
                                b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nproxied",
                            )
                            .unwrap();
                        return true;
                    }
                    return false;
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => return false,
            }
        }
        false
    });
    let script = format!(
        "/usr/bin/nc -z -w 1 127.0.0.1 {port} >/dev/null 2>&1 && exit 51; test \"$(/usr/bin/curl --fail --silent --max-time 5 http://127.0.0.1:{port}/through-proxy)\" = proxied || exit 52"
    );
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "network_enabled": true,
        "network_rules": [{
            "host": "127.0.0.1",
            "port": port,
            "protocol": "http",
            "allow": true,
            "allow_private": true
        }],
        "max_output_bytes": 65536
    }));
    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert!(
        server.join().unwrap(),
        "request did not arrive through HTTP proxy"
    );
}

#[test]
fn mac_006_unix_socket_access_is_denied_when_no_exact_grant_is_supported() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let socket_dir = tempfile::tempdir().unwrap();
    let socket_path = socket_dir.path().join("host.sock");
    let listener = UnixListener::bind(&socket_path).unwrap();
    listener.set_nonblocking(true).unwrap();
    let script = format!(
        "printf x | /usr/bin/nc -w 1 -U '{}' >/dev/null 2>&1 && exit 61; exit 0",
        socket_path.display()
    );
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "readable_roots": [socket_dir.path()],
        "network_enabled": false
    }));
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert!(
        listener.accept().is_err(),
        "sandbox connected to an ungranted Unix socket"
    );
}

#[test]
fn mac_006_009_unsupported_or_ambiguous_requests_fail_before_start() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let marker = workspace.path().join("must-not-run");
    let broken_deny = workspace.path().join("broken-deny");
    std::os::unix::fs::symlink(workspace.path().join("missing-target"), &broken_deny).unwrap();
    let cases = [
        serde_json::json!({
            "op": "run",
            "command": ["sh", "-c", format!("touch '{}'", marker.display())],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": false
        }),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", format!("touch '{}'", marker.display())],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": false,
            "allow_local_binding": true
        }),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", format!("touch '{}'", marker.display())],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": true,
            "network_rules": [{
                "host": "example.com",
                "port": 443,
                "protocol": "tcp",
                "allow": true
            }]
        }),
        serde_json::json!({
            "op": "run",
            "command": ["/bin/sh", "-c", format!("touch '{}'", marker.display())],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "denied_roots": [broken_deny],
            "network_enabled": false
        }),
    ];
    for request in cases {
        let events = run_request(request);
        assert_eq!(events.len(), 1, "{events:?}");
        assert_eq!(events[0]["type"], "error", "{events:?}");
        assert_eq!(events[0]["code"], "sandbox_denied", "{events:?}");
        assert!(!marker.exists(), "rejected request executed its command");
    }
}

#[test]
fn mac_003_cwd_may_be_read_only_but_must_be_explicitly_allowed() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let cwd = tempfile::tempdir().unwrap();
    let writable = tempfile::tempdir().unwrap();
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/pwd"],
        "cwd": cwd.path(),
        "writable_roots": [writable.path()],
        "readable_roots": [cwd.path()],
        "network_enabled": false
    }));
    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert_eq!(stdout_text(&events).trim(), cwd.path().to_str().unwrap());
}

#[test]
fn mac_009_private_home_is_isolated_reserved_and_removed() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let script = "mkdir \"$HOME/locked\"; chmod 000 \"$HOME/locked\"; printf '%s\\n%s\\n%s\\n' \"$HOME\" \"$TMPDIR\" \"$PWD\"; stat -f '%Lp' \"$HOME\"";
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "network_enabled": false
    }));
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    let output = stdout_text(&events);
    let lines = output.lines().collect::<Vec<_>>();
    assert_eq!(lines.len(), 4, "{output:?}");
    let home = Path::new(lines[0]);
    assert_eq!(Path::new(lines[1]), home.join("tmp"));
    assert_eq!(
        Path::new(lines[2]),
        workspace.path().canonicalize().unwrap()
    );
    assert_eq!(lines[3], "700");
    assert!(!home.exists(), "private HOME survived sandbox completion");

    let rejected = run_request(serde_json::json!({
        "op": "run",
        "command": ["/usr/bin/true"],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "network_enabled": false,
        "env_overrides": {"HOME": workspace.path()}
    }));
    assert_eq!(rejected.len(), 1, "{rejected:?}");
    assert_eq!(rejected[0]["type"], "error", "{rejected:?}");
    assert_eq!(rejected[0]["code"], "sandbox_denied", "{rejected:?}");
}

#[test]
fn mac_009_process_group_descendants_are_killed_on_completion() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let pid_file = workspace.path().join("child.pid");
    let script = format!(
        "/bin/sleep 30 & printf '%s' \"$!\" > '{}'",
        pid_file.display()
    );
    let events = run_request(serde_json::json!({
        "op": "run",
        "command": ["/bin/sh", "-c", script],
        "cwd": workspace.path(),
        "writable_roots": [workspace.path()],
        "network_enabled": false
    }));
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    let pid: i32 = std::fs::read_to_string(pid_file).unwrap().parse().unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while process_exists(pid) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert!(!process_exists(pid), "sandbox descendant {pid} survived");
}

#[test]
fn mac_012_parent_path_cannot_replace_pinned_sandbox_exec() {
    if !seatbelt_available() {
        assert!(
            !native_tests_required(),
            "release gate requires a host that permits macOS Seatbelt"
        );
        return;
    }
    let workspace = tempfile::tempdir().unwrap();
    let fake_bin = tempfile::tempdir().unwrap();
    let fake = fake_bin.path().join("sandbox-exec");
    std::fs::write(&fake, "#!/bin/sh\nexit 97\n").unwrap();
    let mut permissions = std::fs::metadata(&fake).unwrap().permissions();
    std::os::unix::fs::PermissionsExt::set_mode(&mut permissions, 0o700);
    std::fs::set_permissions(&fake, permissions).unwrap();
    let events = run_request_with_parent_env(
        serde_json::json!({
            "op": "run",
            "command": ["/usr/bin/true"],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": false
        }),
        &[("PATH", fake_bin.path().to_string_lossy().into_owned())],
    );
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
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
        "version": 2,
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
