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
fn stdin_environment_and_output_stream_through_hardened_child() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/bin/sh",
                "-c",
                "cat; printf ':%s:%s' \"$CUSTOM_ENV\" \"$ACE_SANDBOX\"; printf err >&2"
            ],
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
    assert_eq!(stdout, b"prompt:custom:linux-bwrap");
    assert_eq!(stderr, b"err");
}

#[test]
fn deny_read_glob_masks_matching_content_in_the_native_child() {
    let workspace = tempfile::tempdir().unwrap();
    std::fs::write(workspace.path().join("public.txt"), "public").unwrap();
    std::fs::write(workspace.path().join("secret.pem"), "secret").unwrap();
    let root = workspace.path().canonicalize().unwrap();
    let events = run_request(
        &root,
        serde_json::json!({
            "op": "run",
            "command": [
                "/bin/sh",
                "-c",
                "test \"$(cat public.txt)\" = public && test -z \"$(cat secret.pem)\""
            ],
            "cwd": &root,
            "writable_roots": [&root],
            "filesystem_globs": [{
                "root": &root,
                "pattern": "**/*.pem",
                "access": "deny_read"
            }],
            "max_output_bytes": 1024
        }),
    );

    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["type"], "completed", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
    assert!(!collect_stream(&events, "stdout")
        .windows(b"secret".len())
        .any(|window| window == b"secret"));
}

#[test]
fn deny_read_glob_overflow_rejects_before_started_or_command_spawn() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path().canonicalize().unwrap();
    for index in 0..=8192 {
        std::fs::File::create(root.join(format!("secret-{index:04}.pem"))).unwrap();
    }
    let marker = root.join("must-not-exist");
    let events = run_request(
        &root,
        serde_json::json!({
            "op": "run",
            "command": ["/usr/bin/touch", &marker],
            "cwd": &root,
            "writable_roots": [&root],
            "filesystem_globs": [{
                "root": &root,
                "pattern": "*.pem",
                "access": "deny_read"
            }],
            "max_output_bytes": 1024
        }),
    );

    assert_eq!(events.len(), 1, "{events:?}");
    assert_eq!(events[0]["type"], "error", "{events:?}");
    assert_eq!(events[0]["code"], "sandbox_denied", "{events:?}");
    assert!(
        !marker.exists(),
        "command spawned before glob overflow denial"
    );
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
fn started_capabilities_follow_verified_kernel_boundaries() {
    let workspace = tempfile::tempdir().unwrap();
    let host_pid_namespace = std::fs::read_link("/proc/self/ns/pid")
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let host_user_namespace = std::fs::read_link("/proc/self/ns/user")
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let host_network_namespace = std::fs::read_link("/proc/self/ns/net")
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let host_ipc_namespace = std::fs::read_link("/proc/self/ns/ipc")
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let host_uts_namespace = std::fs::read_link("/proc/self/ns/uts")
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/bin/sh",
                "-c",
                concat!(
                    "test \"$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)\" = 1 && ",
                    "test \"$(awk '/^Seccomp:/ {print $2}' /proc/self/status)\" = 2 && ",
                    "test -r /proc/1/status && ",
                    "test \"$(readlink /proc/self/ns/pid)\" != \"$1\" && ",
                    "test \"$(readlink /proc/self/ns/user)\" != \"$2\" && ",
                    "test \"$(readlink /proc/self/ns/net)\" != \"$3\" && ",
                    "test \"$(readlink /proc/self/ns/ipc)\" != \"$4\" && ",
                    "test \"$(readlink /proc/self/ns/uts)\" != \"$5\""
                ),
                "ace-linux-contract",
                host_pid_namespace,
                host_user_namespace,
                host_network_namespace,
                host_ipc_namespace,
                host_uts_namespace
            ],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": false,
            "max_output_bytes": 1024
        }),
    );

    let started = events.first().unwrap();
    assert_eq!(started["type"], "started", "{events:?}");
    assert_eq!(started["capabilities"]["backend"], "linux_bwrap");
    assert_eq!(started["capabilities"]["filesystem_sandbox"], true);
    assert_eq!(started["capabilities"]["process_tree_cleanup"], true);
    assert_eq!(started["capabilities"]["managed_network"], false);
    assert_eq!(started["capabilities"]["local_binding_control"], true);
    assert_ne!(
        started["capabilities"]["system_bwrap"],
        started["capabilities"]["bundled_bwrap"]
    );
    assert_eq!(events.last().unwrap()["type"], "completed", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
}

#[test]
fn sandbox_command_inherits_parent_death_signal() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/usr/bin/python3",
                "-c",
                "import ctypes,sys; libc=ctypes.CDLL(None); value=ctypes.c_int(); libc.prctl(2,ctypes.byref(value),0,0,0); sys.exit(0 if value.value==9 else 1)"
            ],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "max_output_bytes": 1024
        }),
    );
    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["type"], "completed", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
}

#[test]
fn seccomp_blocks_namespace_and_mount_management_syscalls() {
    let workspace = tempfile::tempdir().unwrap();
    let events = run_request(
        workspace.path(),
        serde_json::json!({
            "op": "run",
            "command": [
                "/usr/bin/python3",
                "-c",
                concat!(
                    "import ctypes,errno,sys\n",
                    "libc=ctypes.CDLL(None,use_errno=True)\n",
                    "checks=[]\n",
                    "for call in (lambda: libc.unshare(0x00020000),",
                    "lambda: libc.mount(None,b'/tmp',None,0,None),",
                    "lambda: libc.syscall(430,b'tmpfs',0),",
                    "lambda: libc.syscall(429,-1,b'',-1,b'',0)):\n",
                    " ctypes.set_errno(0)\n",
                    " result=call()\n",
                    " checks.append((result,ctypes.get_errno()))\n",
                    "sys.exit(0 if all(result==-1 and error==errno.EPERM ",
                    "for result,error in checks) else 1)\n"
                )
            ],
            "cwd": workspace.path(),
            "writable_roots": [workspace.path()],
            "network_enabled": false,
            "max_output_bytes": 1024
        }),
    );

    assert_eq!(events.first().unwrap()["type"], "started", "{events:?}");
    assert_eq!(events.last().unwrap()["type"], "completed", "{events:?}");
    assert_eq!(events.last().unwrap()["exit_code"], 0, "{events:?}");
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
    assert!(line.contains("\"deny_read_glob_v1\""));
    let envelope = serde_json::json!({
        "version": 3,
        "token": token,
        "nonce": format!("linux-native-{}", std::process::id()),
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
                .is_some_and(|nonce| nonce.starts_with("linux-native-"))
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
