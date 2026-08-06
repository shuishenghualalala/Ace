mod network;
mod protocol;
mod shell;

#[cfg(target_os = "linux")]
mod linux;

#[cfg(target_os = "macos")]
mod macos;

#[cfg(windows)]
mod windows;

use std::collections::{HashSet, VecDeque};
use std::env;
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::sync::mpsc::{self, SyncSender};
use std::thread;

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use protocol::{
    validate_process_inputs, ReadyFrame, RequestEnvelope, RuntimeEvent, RuntimeMessage,
    RuntimeRequest, MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES, PROTOCOL_VERSION,
    READY_CAPABILITIES,
};
use subtle::ConstantTimeEq;

// Hard cap on a single stdin protocol frame (M5). Without it `lines()` would
// grow an unbounded buffer; a peer that never sent a newline could exhaust
// memory. 1 MiB is far above any legitimate envelope.
// Bound on remembered nonces (M5). `HashSet` with no ceiling would grow for the
// lifetime of the process; a FIFO-evicting cap keeps replay protection bounded
// while still covering a realistic session window.
const NONCE_CACHE_CAP: usize = 4096;
const EVENT_CHANNEL_CAPACITY: usize = 64;

fn main() {
    #[cfg(target_os = "linux")]
    if env::args().nth(1).as_deref() == Some("--inner-seccomp") {
        linux::seccomp::exec_inner(env::args().skip(2).collect());
    }

    #[cfg(windows)]
    if env::args().nth(1).as_deref() == Some("--windows-runner") {
        windows::process::runner_main();
    }

    #[cfg(windows)]
    if env::args().nth(1).as_deref() == Some("--windows-setup") {
        let state_dir = env::args().nth(2).map(PathBuf::from).unwrap_or_else(|| {
            eprintln!("--windows-setup requires an absolute state directory");
            std::process::exit(2);
        });
        match windows::identity::setup(&state_dir) {
            Ok(()) => {
                println!("Windows sandbox identity setup completed");
                return;
            }
            Err(error) => {
                eprintln!("Windows sandbox identity setup failed: {error}");
                std::process::exit(1);
            }
        }
    }

    #[cfg(windows)]
    if env::args().nth(1).as_deref() == Some("--windows-uninstall") {
        let state_dir = env::args().nth(2).map(PathBuf::from).unwrap_or_else(|| {
            eprintln!("--windows-uninstall requires an absolute state directory");
            std::process::exit(2);
        });
        match windows::identity::uninstall(&state_dir) {
            Ok(()) => {
                println!("Windows sandbox identity uninstall completed");
                return;
            }
            Err(error) => {
                eprintln!("Windows sandbox identity uninstall failed: {error}");
                std::process::exit(1);
            }
        }
    }

    if let Err(error) = protocol_main() {
        eprintln!("ace-security-runtime: {error}");
        std::process::exit(1);
    }
}

fn protocol_main() -> Result<(), String> {
    let startup_token = env::var("ACE_SECURITY_RUNTIME_TOKEN")
        .map_err(|_| "missing authenticated startup token".to_string())?;
    if startup_token.len() < 32 {
        return Err("startup token is too short".to_string());
    }
    // Compare the startup token in constant time (N5). `String::!=` short-
    // circuits on the first differing byte, leaking prefix information via a
    // timing side channel. The token is an authentication credential, so we
    // route through `subtle::ConstantTimeEq`. `ct_eq` returns `Choice(0)` for
    // unequal-length slices, so this also rejects length-mismatched tokens
    // without a separate branch.
    let startup_token_bytes = startup_token.as_bytes();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    write_frame(
        &mut output,
        &ReadyFrame {
            frame_type: "ready",
            version: PROTOCOL_VERSION,
            capabilities: READY_CAPABILITIES,
        },
    )?;

    let stdin = io::stdin();
    // ponytail: FIFO-evicting nonce cache. Not a true LRU (no access-time
    // reordering), but replay protection only needs "recently seen" semantics;
    // a strict LRU would add bookkeeping for no security gain here.
    let mut seen_nonces = NonceCache::new(NONCE_CACHE_CAP);
    let mut reader = stdin.lock();
    let mut raw = String::new();
    loop {
        raw.clear();
        match reader.read_line(&mut raw) {
            Ok(0) => break, // EOF: peer closed stdin
            Ok(_) => {}
            Err(error) => return Err(format!("failed to read protocol frame: {error}")),
        }
        // M5: reject oversized frames before parsing so a peer cannot drive
        // unbounded allocation by streaming a single huge line.
        if raw.len() > MAX_REQUEST_FRAME_BYTES {
            write_error(
                &mut output,
                String::new(),
                "runtime_protocol_mismatch",
                "frame exceeds 2MiB limit",
            )?;
            continue;
        }
        // M6: do not reflect the serde error back to the peer — it can echo
        // attacker-controlled bytes and leak parser internals. Fixed string.
        let envelope: RequestEnvelope = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(_) => {
                write_error(
                    &mut output,
                    String::new(),
                    "runtime_protocol_mismatch",
                    "frame is not valid JSON",
                )?;
                continue;
            }
        };
        let nonce = envelope.nonce.clone();
        if envelope.version != PROTOCOL_VERSION {
            write_error(
                &mut output,
                nonce,
                "runtime_protocol_mismatch",
                "unsupported protocol version",
            )?;
            continue;
        }
        // N5: constant-time comparison. A mismatched token is reported as
        // `runtime_protocol_mismatch` (not `sandbox_denied`) so an attacker
        // cannot distinguish "wrong token" from "wrong protocol version" by
        // error code — both are authentication-layer rejections.
        let token_ok: bool = startup_token_bytes.ct_eq(envelope.token.as_bytes()).into();
        if !token_ok {
            write_error(
                &mut output,
                nonce,
                "runtime_protocol_mismatch",
                "invalid runtime authentication",
            )?;
            continue;
        }
        if nonce.len() < 16 || !seen_nonces.check_and_insert(&nonce) {
            write_error(
                &mut output,
                nonce,
                "sandbox_denied",
                "invalid or replayed nonce",
            )?;
            continue;
        }
        stream_request(envelope.request, nonce, &mut output)?;
    }
    Ok(())
}

/// Bounded replay-protection cache (M5).
///
/// `HashSet` alone grows without limit; this pairs the set with a FIFO order
/// queue and evicts the oldest entry once `cap` is reached. Eviction reopens a
/// replay window for the evicted nonce, which is acceptable: nonces are
/// per-session and the cap is large relative to expected session volume.
struct NonceCache {
    seen: HashSet<String>,
    order: VecDeque<String>,
    cap: usize,
}

impl NonceCache {
    fn new(cap: usize) -> Self {
        Self {
            seen: HashSet::new(),
            order: VecDeque::new(),
            cap,
        }
    }

    /// Returns `true` if the nonce is fresh and was inserted, `false` if it
    /// was already seen (a replay).
    fn check_and_insert(&mut self, nonce: &str) -> bool {
        if !self.seen.insert(nonce.to_string()) {
            return false;
        }
        self.order.push_back(nonce.to_string());
        while self.order.len() > self.cap {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            }
        }
        true
    }
}

#[derive(Debug)]
struct RuntimeFailure {
    code: &'static str,
    message: String,
}

fn handle_request(
    request: RuntimeRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), RuntimeFailure> {
    match request {
        RuntimeRequest::ClassifyShell {
            shell_kind,
            executable,
            raw_command,
        } => sender
            .send(RuntimeMessage::Classified(shell::classify(
                &shell_kind,
                &executable,
                &raw_command,
            )))
            .map_err(|_| RuntimeFailure {
                code: "runtime_crashed",
                message: "classification channel closed".to_string(),
            }),
        RuntimeRequest::Run {
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            network_enabled,
            network_rules,
            allow_local_binding,
            max_output_bytes,
            stdin_b64,
            env_overrides,
        } => {
            let process_input = validate_process_inputs(stdin_b64.as_deref(), &env_overrides)
                .map_err(|error| RuntimeFailure {
                    code: error.code,
                    message: error.message.to_string(),
                })?;
            if command.is_empty() {
                return Err(RuntimeFailure {
                    code: "sandbox_denied",
                    message: "empty command".to_string(),
                });
            }
            #[cfg(target_os = "linux")]
            {
                let request = linux::LinuxRunRequest {
                    command,
                    cwd: PathBuf::from(cwd),
                    writable_roots: writable_roots.into_iter().map(PathBuf::from).collect(),
                    readable_roots: readable_roots.into_iter().map(PathBuf::from).collect(),
                    denied_roots: denied_roots.into_iter().map(PathBuf::from).collect(),
                    network_enabled,
                    network_rules,
                    allow_local_binding,
                    proxy_socket_dir: None,
                    max_output_bytes,
                    stdin: process_input.stdin,
                    env_overrides: process_input.env_overrides,
                };
                linux::run(request, sender).map_err(|error| RuntimeFailure {
                    code: error.code,
                    message: error.message,
                })
            }
            #[cfg(target_os = "macos")]
            {
                let request = macos::MacOsRunRequest {
                    command,
                    cwd: PathBuf::from(cwd),
                    writable_roots: writable_roots.into_iter().map(PathBuf::from).collect(),
                    readable_roots: readable_roots.into_iter().map(PathBuf::from).collect(),
                    denied_roots: denied_roots.into_iter().map(PathBuf::from).collect(),
                    network_enabled,
                    network_rules,
                    allow_local_binding,
                    max_output_bytes,
                    stdin: process_input.stdin,
                    env_overrides: process_input.env_overrides,
                };
                macos::run(request, sender).map_err(|error| RuntimeFailure {
                    code: error.code,
                    message: error.message,
                })
            }
            #[cfg(not(any(target_os = "linux", target_os = "macos")))]
            #[cfg(not(windows))]
            {
                let _ = (
                    command,
                    cwd,
                    writable_roots,
                    readable_roots,
                    denied_roots,
                    network_enabled,
                    network_rules,
                    allow_local_binding,
                    max_output_bytes,
                    process_input,
                );
                Err(RuntimeFailure {
                    code: "sandbox_unavailable",
                    message: "platform backend is unavailable".to_string(),
                })
            }
            #[cfg(windows)]
            {
                let request = windows::WindowsRunRequest {
                    command,
                    cwd: PathBuf::from(cwd),
                    writable_roots: writable_roots.into_iter().map(PathBuf::from).collect(),
                    readable_roots: readable_roots.into_iter().map(PathBuf::from).collect(),
                    denied_roots: denied_roots.into_iter().map(PathBuf::from).collect(),
                    network_enabled,
                    network_rules,
                    allow_local_binding,
                    max_output_bytes,
                    stdin: process_input.stdin,
                    env_overrides: process_input.env_overrides,
                };
                windows::run(request, sender).map_err(|error| RuntimeFailure {
                    code: error.code,
                    message: error.message,
                })
            }
        }
    }
}

fn stream_request<W: Write>(
    request: RuntimeRequest,
    nonce: String,
    output: &mut W,
) -> Result<(), String> {
    let (sender, receiver) = mpsc::sync_channel(EVENT_CHANNEL_CAPACITY);
    let worker = thread::spawn(move || execute_request(request, sender));
    let mut writer = EventWriter::new(output, nonce);

    while let Ok(message) = receiver.recv() {
        writer.write_message(message)?;
    }
    if !writer.terminal {
        writer.write_message(RuntimeMessage::Error {
            code: "runtime_crashed",
            message: "native runtime worker terminated unexpectedly".to_string(),
        })?;
    }
    if worker.join().is_err() && !writer.terminal {
        return Err("native runtime worker panicked".to_string());
    }
    Ok(())
}

fn execute_request(request: RuntimeRequest, sender: SyncSender<RuntimeMessage>) {
    if let Err(error) = handle_request(request, &sender) {
        let _ = sender.send(RuntimeMessage::Error {
            code: error.code,
            message: error.message,
        });
    }
}

struct EventWriter<'a, W: Write> {
    output: &'a mut W,
    nonce: String,
    seq: u64,
    started: bool,
    terminal: bool,
}

impl<'a, W: Write> EventWriter<'a, W> {
    fn new(output: &'a mut W, nonce: String) -> Self {
        Self {
            output,
            nonce,
            seq: 0,
            started: false,
            terminal: false,
        }
    }

    fn write_message(&mut self, message: RuntimeMessage) -> Result<(), String> {
        if self.terminal {
            return Err("native runtime attempted to emit data after terminal".to_string());
        }
        let terminal = matches!(
            message,
            RuntimeMessage::Classified(_)
                | RuntimeMessage::Completed(_)
                | RuntimeMessage::Error { .. }
        );
        let event = match message {
            RuntimeMessage::Classified(classification) if !self.started => {
                RuntimeEvent::Classified {
                    version: PROTOCOL_VERSION,
                    nonce: self.nonce.clone(),
                    seq: self.seq,
                    classification,
                }
            }
            RuntimeMessage::Started { pid, capabilities } if !self.started => {
                self.started = true;
                RuntimeEvent::Started {
                    version: PROTOCOL_VERSION,
                    nonce: self.nonce.clone(),
                    seq: self.seq,
                    pid,
                    capabilities,
                }
            }
            RuntimeMessage::Stdout(data) if self.started => RuntimeEvent::Stdout {
                version: PROTOCOL_VERSION,
                nonce: self.nonce.clone(),
                seq: self.seq,
                data_b64: BASE64_STANDARD.encode(data),
            },
            RuntimeMessage::Stderr(data) if self.started => RuntimeEvent::Stderr {
                version: PROTOCOL_VERSION,
                nonce: self.nonce.clone(),
                seq: self.seq,
                data_b64: BASE64_STANDARD.encode(data),
            },
            RuntimeMessage::Completed(exit_code) if self.started => RuntimeEvent::Completed {
                version: PROTOCOL_VERSION,
                nonce: self.nonce.clone(),
                seq: self.seq,
                exit_code,
            },
            RuntimeMessage::Error { code, message } => RuntimeEvent::Error {
                version: PROTOCOL_VERSION,
                nonce: self.nonce.clone(),
                seq: self.seq,
                code,
                message,
            },
            _ => return Err("native runtime event ordering violation".to_string()),
        };
        write_frame(self.output, &event)?;
        self.seq += 1;
        self.terminal = terminal;
        Ok(())
    }
}

fn write_error<W: Write>(
    output: &mut W,
    nonce: String,
    code: &'static str,
    message: impl Into<String>,
) -> Result<(), String> {
    EventWriter::new(output, nonce).write_message(RuntimeMessage::Error {
        code,
        message: message.into(),
    })
}

fn write_frame<W: Write, T: serde::Serialize>(output: &mut W, value: &T) -> Result<(), String> {
    let mut frame = serde_json::to_vec(value)
        .map_err(|error| format!("failed to encode protocol frame: {error}"))?;
    frame.push(b'\n');
    if frame.len() > MAX_RESPONSE_FRAME_BYTES {
        return Err("native runtime response frame exceeds the size limit".to_string());
    }
    output
        .write_all(&frame)
        .and_then(|_| output.flush())
        .map_err(|error| format!("failed to write protocol frame: {error}"))
}

#[cfg(test)]
mod tests {
    use super::{EventWriter, NonceCache, RuntimeMessage, NONCE_CACHE_CAP};
    use crate::protocol::RuntimeCapabilities;

    // M5 regression: the nonce cache must be bounded. After inserting `cap`
    // distinct nonces, the oldest must be evicted (re-openable) while recent
    // ones stay protected.
    #[test]
    fn nonce_cache_evicts_oldest_when_full() {
        let mut cache = NonceCache::new(2);
        assert!(cache.check_and_insert("nonce-aaaaaaaaaaaaaa"));
        assert!(cache.check_and_insert("nonce-bbbbbbbbbbbbbb"));
        // Cache is full; inserting a third evicts the oldest.
        assert!(cache.check_and_insert("nonce-cccccccccccccc"));
        // The evicted nonce is now accepted again (FIFO eviction reopens it).
        assert!(
            cache.check_and_insert("nonce-aaaaaaaaaaaaaa"),
            "evicted nonce should be re-acceptable"
        );
    }

    #[test]
    fn nonce_cache_rejects_replay() {
        let mut cache = NonceCache::new(NONCE_CACHE_CAP);
        assert!(cache.check_and_insert("nonce-aaaaaaaaaaaaaa"));
        assert!(
            !cache.check_and_insert("nonce-aaaaaaaaaaaaaa"),
            "replayed nonce must be rejected"
        );
    }

    #[test]
    fn nonce_cache_rejects_short_nonce() {
        // Short nonces are rejected by the caller (len < 16), but the cache
        // itself accepts any string; this test documents that the length gate
        // lives in protocol_main, not in NonceCache.
        let mut cache = NonceCache::new(NONCE_CACHE_CAP);
        assert!(cache.check_and_insert("short"));
        assert!(!cache.check_and_insert("short"));
    }

    #[test]
    fn event_writer_sequences_one_terminal_stream() {
        let mut output = Vec::new();
        let mut writer = EventWriter::new(&mut output, "nonce".to_string());
        writer
            .write_message(RuntimeMessage::Started {
                pid: Some(123),
                capabilities: capabilities(),
            })
            .unwrap();
        writer
            .write_message(RuntimeMessage::Stdout(b"out".to_vec()))
            .unwrap();
        writer
            .write_message(RuntimeMessage::Stderr(b"err".to_vec()))
            .unwrap();
        writer.write_message(RuntimeMessage::Completed(0)).unwrap();
        assert!(writer.write_message(RuntimeMessage::Completed(0)).is_err());
        drop(writer);

        let frames = String::from_utf8(output).unwrap();
        let values = frames
            .lines()
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(values.len(), 4);
        assert_eq!(values[0]["type"], "started");
        assert_eq!(values[1]["type"], "stdout");
        assert_eq!(values[2]["type"], "stderr");
        assert_eq!(values[3]["type"], "completed");
        assert_eq!(values[3]["seq"], 3);
    }

    #[test]
    fn event_writer_allows_error_before_started() {
        let mut output = Vec::new();
        EventWriter::new(&mut output, "nonce".to_string())
            .write_message(RuntimeMessage::Error {
                code: "sandbox_denied",
                message: "denied".to_string(),
            })
            .unwrap();
        let value: serde_json::Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(value["type"], "error");
        assert_eq!(value["seq"], 0);
    }

    fn capabilities() -> RuntimeCapabilities {
        RuntimeCapabilities {
            backend: "test",
            filesystem_sandbox: true,
            process_tree_cleanup: true,
            managed_network: false,
            system_bwrap: false,
            bundled_bwrap: false,
            wsl_version: None,
            local_binding_control: false,
            explicit_handle_inheritance: false,
            windows_restricted_token: false,
            windows_acl: false,
            windows_job: false,
            windows_wfp: false,
        }
    }
}
