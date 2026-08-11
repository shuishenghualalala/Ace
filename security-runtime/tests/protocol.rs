use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

#[test]
fn protocol_contract_is_versioned_and_tcp_free() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let protocol = fs::read_to_string(format!("{manifest}/src/protocol.rs")).unwrap();
    let main = fs::read_to_string(format!("{manifest}/src/main.rs")).unwrap();
    assert!(protocol.contains("PROTOCOL_VERSION: u16 = 2"));
    assert!(protocol.contains("MAX_REQUEST_FRAME_BYTES"));
    assert!(protocol.contains("MAX_STDIN_BYTES"));
    assert!(protocol.contains("MAX_ENV_BYTES"));
    assert!(protocol.contains("MAX_OUTPUT_CHUNK_BYTES"));
    assert!(!protocol.contains("ResponseEnvelope"));
    assert!(main.contains("ACE_SECURITY_RUNTIME_TOKEN"));
    assert!(main.contains("seen_nonces"));
    assert!(main.contains("sync_channel"));
    assert!(!main.contains("TcpListener"));
}

#[test]
fn startup_token_and_nonce_replay_are_enforced() {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "a".repeat(48);
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", &token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""));
    assert!(line.contains("\"version\":2"));
    assert!(line.contains("\"stdin_once\""));
    assert!(line.contains("\"stream_output\""));
    assert!(line.contains("\"readonly_roots\""));

    let request = serde_json::json!({
        "version": 2,
        "token": token,
        "nonce": "nonce-longer-than-sixteen",
        "request": {"op": "run", "command": [], "cwd": "."}
    });
    writeln!(stdin, "{request}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(line.contains("empty command"));
    writeln!(stdin, "{request}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(line.contains("replayed nonce"));
    drop(stdin);
    assert!(child.wait().unwrap().success());
}

// N5 regression: a wrong startup token must be rejected as
// `runtime_protocol_mismatch` (not `sandbox_denied`) so an attacker cannot
// distinguish "wrong token" from "wrong protocol version" by error code. The
// comparison must also be constant-time, but that is not directly observable
// here; the error-code assertion is the observable proxy.
#[test]
fn token_mismatch_returns_protocol_mismatch() {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "a".repeat(48);
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", &token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""));

    let request = serde_json::json!({
        "version": 2,
        "token": "b".repeat(48),
        "nonce": "nonce-longer-than-sixteen",
        "request": {"op": "run", "command": [], "cwd": "."}
    });
    writeln!(stdin, "{request}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(
        line.contains("runtime_protocol_mismatch"),
        "expected runtime_protocol_mismatch, got: {line}"
    );
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(
        !line.contains("sandbox_denied"),
        "token mismatch must not leak sandbox_denied, got: {line}"
    );
    drop(stdin);
    assert!(child.wait().unwrap().success());
}

// M5 regression: a single frame larger than 2 MiB must be rejected before
// parsing, with the fixed runtime_protocol_mismatch code, rather than driving
// unbounded allocation.
#[test]
fn oversized_frame_is_rejected() {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "a".repeat(48);
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", &token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""));

    // One byte over the 2 MiB cap, terminated by a newline so read_line returns.
    let huge = "x".repeat(2 * 1024 * 1024 + 1);
    writeln!(stdin, "{huge}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(
        line.contains("runtime_protocol_mismatch"),
        "expected runtime_protocol_mismatch, got: {line}"
    );
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(
        line.contains("frame exceeds 2MiB limit"),
        "expected limit message, got: {line}"
    );
    drop(stdin);
    assert!(child.wait().unwrap().success());
}

#[test]
fn invalid_process_inputs_are_rejected_before_command_handling() {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "a".repeat(48);
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", &token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();

    let invalid_stdin = serde_json::json!({
        "version": 2,
        "token": token,
        "nonce": "invalid-stdin-nonce-long",
        "request": {
            "op": "run",
            "command": [],
            "cwd": ".",
            "stdin_b64": "***"
        }
    });
    writeln!(stdin, "{invalid_stdin}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("sandbox_denied"));
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(line.contains("invalid stdin payload"));
    assert!(!line.contains("***"));

    let reserved_environment = serde_json::json!({
        "version": 2,
        "token": token,
        "nonce": "invalid-environment-nonce",
        "request": {
            "op": "run",
            "command": [],
            "cwd": ".",
            "env_overrides": {"http_proxy": "attacker-controlled"}
        }
    });
    writeln!(stdin, "{reserved_environment}").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("sandbox_denied"));
    assert!(line.contains("\"type\":\"error\""));
    assert!(line.contains("\"seq\":0"));
    assert!(line.contains("disallowed environment entry"));
    assert!(!line.contains("attacker-controlled"));

    drop(stdin);
    assert!(child.wait().unwrap().success());
}

// M6 regression: a malformed JSON frame must get the fixed string
// "frame is not valid JSON", never a reflected serde error that could echo
// attacker-controlled bytes or parser internals.
#[test]
fn malformed_json_returns_fixed_message() {
    let binary = env!("CARGO_BIN_EXE_ace-security-runtime");
    let token = "a".repeat(48);
    let mut child = Command::new(binary)
        .env("ACE_SECURITY_RUNTIME_TOKEN", &token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains("\"type\":\"ready\""));

    // A malformed payload whose serde error would otherwise mention the
    // specific parse position.
    writeln!(stdin, "{{ this is not json").unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(
        line.contains("frame is not valid JSON"),
        "expected fixed message, got: {line}"
    );
    // Must not reflect serde's own diagnostic.
    assert!(
        !line.contains("expected"),
        "serde error leaked into response: {line}"
    );
    drop(stdin);
    assert!(child.wait().unwrap().success());
}
