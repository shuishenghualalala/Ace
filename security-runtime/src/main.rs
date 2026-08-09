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
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use protocol::{
    validate_process_inputs_with_home_files, ReadyFrame, RequestEnvelope, RuntimeControl, RuntimeEvent,
    RuntimeMessage, RuntimeRequest, MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES,
    MAX_STDIN_BYTES, PROTOCOL_VERSION, READY_CAPABILITIES,
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

    // The interactive transport must keep reading control frames while the
    // managed child is running. A dedicated reader thread lets the worker
    // stream child output without blocking on the host's next write frame.
    let stdin = io::stdin();
    let (input_tx, input_rx) = mpsc::sync_channel::<InputMessage>(128);
    thread::spawn(move || {
        let mut raw = String::new();
        loop {
            raw.clear();
            match stdin.read_line(&mut raw) {
                Ok(0) => {
                    let _ = input_tx.send(InputMessage::Eof);
                    break;
                }
                Ok(_) => {}
                Err(_) => {
                    let _ = input_tx.send(InputMessage::Invalid);
                    break;
                }
            }
            if raw.len() > MAX_REQUEST_FRAME_BYTES {
                if input_tx.send(InputMessage::TooLarge).is_err() {
                    break;
                }
                continue;
            }
            let message = serde_json::from_str::<RequestEnvelope>(&raw)
                .map(|value| InputMessage::Frame(Box::new(value)))
                .unwrap_or(InputMessage::Invalid);
            if input_tx.send(message).is_err() {
                break;
            }
        }
    });
    // ponytail: FIFO-evicting nonce cache. Not a true LRU (no access-time
    // reordering), but replay protection only needs "recently seen" semantics;
    // a strict LRU would add bookkeeping for no security gain here.
    let mut seen_nonces = NonceCache::new(NONCE_CACHE_CAP);
    loop {
        let envelope = match input_rx.recv() {
            Ok(InputMessage::Frame(value)) => *value,
            Ok(InputMessage::Invalid) => {
                write_error(
                    &mut output,
                    String::new(),
                    "runtime_protocol_mismatch",
                    "frame is not valid JSON",
                )?;
                continue;
            }
            Ok(InputMessage::TooLarge) => {
                write_error(
                    &mut output,
                    String::new(),
                    "runtime_protocol_mismatch",
                    "frame exceeds 2MiB limit",
                )?;
                continue;
            }
            Ok(InputMessage::Eof) | Err(_) => break,
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
        match envelope.request {
            request @ RuntimeRequest::InteractiveOpen { .. } => {
                drop(output);
                stream_interactive_request(
                    request,
                    nonce,
                    &input_rx,
                    &startup_token,
                    &mut seen_nonces,
                    Arc::new(Mutex::new(io::BufWriter::new(io::stdout()))),
                )?;
                output = stdout.lock();
            }
            RuntimeRequest::InteractiveWrite { .. } | RuntimeRequest::InteractiveClose => {
                write_error(
                    &mut output,
                    nonce,
                    "sandbox_denied",
                    "interactive session is not open",
                )?;
            }
            request => stream_request(request, nonce, &mut output)?,
        }
    }
    Ok(())
}

enum InputMessage {
    Frame(Box<RequestEnvelope>),
    Invalid,
    TooLarge,
    Eof,
}

type SharedOutput = Arc<Mutex<io::BufWriter<io::Stdout>>>;

struct SharedOutputWriter(SharedOutput);

impl Write for SharedOutputWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| io::Error::other("native runtime output lock poisoned"))?
            .write(bytes)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.0
            .lock()
            .map_err(|_| io::Error::other("native runtime output lock poisoned"))?
            .flush()
    }
}

fn stream_interactive_request(
    request: RuntimeRequest,
    nonce: String,
    input_rx: &Receiver<InputMessage>,
    startup_token: &str,
    seen_nonces: &mut NonceCache,
    output: SharedOutput,
) -> Result<(), String> {
    let (sender, receiver) = mpsc::sync_channel(EVENT_CHANNEL_CAPACITY);
    let (control_tx, control_rx) = mpsc::channel();
    let worker = thread::spawn(move || execute_interactive_request(request, control_rx, sender));
    let mut output_writer = SharedOutputWriter(output.clone());
    let mut writer = EventWriter::new(&mut output_writer, nonce);
    let mut close_sent = false;

    loop {
        while let Ok(message) = receiver.try_recv() {
            writer.write_message(message)?;
            if writer.terminal {
                break;
            }
        }
        if writer.terminal {
            break;
        }
        match input_rx.recv_timeout(std::time::Duration::from_millis(10)) {
            Ok(InputMessage::Frame(envelope)) => {
                if !authenticate_frame(&envelope, startup_token, seen_nonces, &mut writer)? {
                    break;
                }
                match envelope.request {
                    RuntimeRequest::InteractiveWrite { data_b64 } => {
                        let data = match BASE64_STANDARD.decode(data_b64) {
                            Ok(data) => data,
                            Err(_) => {
                                writer.write_message(RuntimeMessage::Error {
                                    code: "runtime_protocol_mismatch",
                                    message: "invalid interactive stdin payload".to_string(),
                                })?;
                                break;
                            }
                        };
                        if data.len() > MAX_STDIN_BYTES {
                            writer.write_message(RuntimeMessage::Error {
                                code: "sandbox_denied",
                                message: "interactive stdin payload exceeds the size limit"
                                    .to_string(),
                            })?;
                            break;
                        }
                        control_tx
                            .send(RuntimeControl::Write(data))
                            .map_err(|_| "interactive worker closed".to_string())?;
                    }
                    RuntimeRequest::InteractiveClose => {
                        if !close_sent {
                            control_tx
                                .send(RuntimeControl::Close)
                                .map_err(|_| "interactive worker closed".to_string())?;
                            close_sent = true;
                        }
                    }
                    _ => {
                        writer.write_message(RuntimeMessage::Error {
                            code: "sandbox_denied",
                            message: "interactive session accepts only write or close frames"
                                .to_string(),
                        })?;
                        break;
                    }
                }
            }
            Ok(InputMessage::Invalid) => {
                writer.write_message(RuntimeMessage::Error {
                    code: "runtime_protocol_mismatch",
                    message: "frame is not valid JSON".to_string(),
                })?;
                break;
            }
            Ok(InputMessage::TooLarge) => {
                writer.write_message(RuntimeMessage::Error {
                    code: "runtime_protocol_mismatch",
                    message: "frame exceeds 2MiB limit".to_string(),
                })?;
                break;
            }
            Ok(InputMessage::Eof) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                if !close_sent {
                    let _ = control_tx.send(RuntimeControl::Close);
                }
                break;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
    }

    drop(control_tx);
    while let Ok(message) = receiver.recv() {
        writer.write_message(message)?;
        if writer.terminal {
            break;
        }
    }
    let _ = worker.join();
    Ok(())
}

fn authenticate_frame(
    envelope: &RequestEnvelope,
    startup_token: &str,
    seen_nonces: &mut NonceCache,
    writer: &mut EventWriter<'_, SharedOutputWriter>,
) -> Result<bool, String> {
    if envelope.version != PROTOCOL_VERSION {
        writer.write_message(RuntimeMessage::Error {
            code: "runtime_protocol_mismatch",
            message: "unsupported protocol version".to_string(),
        })?;
        return Ok(false);
    }
    if !bool::from(startup_token.as_bytes().ct_eq(envelope.token.as_bytes())) {
        writer.write_message(RuntimeMessage::Error {
            code: "runtime_protocol_mismatch",
            message: "invalid runtime authentication".to_string(),
        })?;
        return Ok(false);
    }
    if envelope.nonce.len() < 16 || !seen_nonces.check_and_insert(&envelope.nonce) {
        writer.write_message(RuntimeMessage::Error {
            code: "sandbox_denied",
            message: "invalid or replayed nonce".to_string(),
        })?;
        return Ok(false);
    }
    Ok(true)
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

fn execute_interactive_request(
    request: RuntimeRequest,
    control_rx: Receiver<RuntimeControl>,
    sender: SyncSender<RuntimeMessage>,
) {
    let result = match request {
        RuntimeRequest::InteractiveOpen {
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            network_enabled,
            network_rules,
            allow_local_binding,
            max_output_bytes,
            env_overrides,
            home_files,
        } => {
            let process_input = validate_process_inputs_with_home_files(
                None,
                &env_overrides,
                &home_files,
            );
            let process_input = match process_input {
                Ok(value) => value,
                Err(error) => {
                    let _ = sender.send(RuntimeMessage::Error {
                        code: error.code,
                        message: error.message.to_string(),
                    });
                    return;
                }
            };
            if command.is_empty() {
                Err(RuntimeFailure {
                    code: "sandbox_denied",
                    message: "empty command".to_string(),
                })
            } else {
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
                        stdin: None,
                        env_overrides: process_input.env_overrides,
                        home_files: process_input.home_files,
                    };
                    linux::run_interactive(request, control_rx, &sender).map_err(|error| {
                        RuntimeFailure {
                            code: error.code,
                            message: error.message,
                        }
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
                        stdin: None,
                        env_overrides: process_input.env_overrides,
                        home_files: process_input.home_files,
                    };
                    macos::run_interactive(request, control_rx, &sender).map_err(|error| {
                        RuntimeFailure {
                            code: error.code,
                            message: error.message,
                        }
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
                        stdin: None,
                        env_overrides: process_input.env_overrides,
                        home_files: process_input.home_files,
                    };
                    windows::run_interactive(request, control_rx, &sender).map_err(|error| {
                        RuntimeFailure {
                            code: error.code,
                            message: error.message,
                        }
                    })
                }
                #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
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
                        env_overrides,
                        control_rx,
                    );
                    Err(RuntimeFailure {
                        code: "sandbox_unavailable",
                        message: "platform backend is unavailable".to_string(),
                    })
                }
            }
        }
        _ => Err(RuntimeFailure {
            code: "sandbox_denied",
            message: "interactive request must open a session".to_string(),
        }),
    };
    if let Err(error) = result {
        let _ = sender.send(RuntimeMessage::Error {
            code: error.code,
            message: error.message,
        });
    }
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
            home_files,
        } => {
            let process_input = validate_process_inputs_with_home_files(
                stdin_b64.as_deref(),
                &env_overrides,
                &home_files,
            )
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
                    home_files: process_input.home_files,
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
                    home_files: process_input.home_files,
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
                    home_files: process_input.home_files,
                };
                windows::run(request, sender).map_err(|error| RuntimeFailure {
                    code: error.code,
                    message: error.message,
                })
            }
        }
        RuntimeRequest::InteractiveOpen { .. }
        | RuntimeRequest::InteractiveWrite { .. }
        | RuntimeRequest::InteractiveClose => Err(RuntimeFailure {
            code: "sandbox_denied",
            message: "interactive request must open a session".to_string(),
        }),
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
