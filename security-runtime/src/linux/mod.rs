pub mod bwrap;
pub mod bwrap_source;
pub mod proxy_routing;
pub mod seccomp;
pub mod wsl;

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::{collections::BTreeMap, io::Read, io::Write, thread};

use crate::protocol::{
    RuntimeCapabilities, RuntimeControl, RuntimeMessage, MAX_OUTPUT_CHUNK_BYTES,
};

pub struct LinuxRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub readonly_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub proxy_socket_dir: Option<PathBuf>,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub env_overrides: BTreeMap<String, String>,
    pub home_files: BTreeMap<String, Vec<u8>>,
}

pub struct LinuxRuntimeError {
    pub code: &'static str,
    pub message: String,
}

pub fn run(
    request: LinuxRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), LinuxRuntimeError> {
    run_with_control(request, None, sender)
}

pub fn run_interactive(
    request: LinuxRunRequest,
    control_rx: Receiver<RuntimeControl>,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), LinuxRuntimeError> {
    run_with_control(request, Some(control_rx), sender)
}

fn run_with_control(
    request: LinuxRunRequest,
    control_rx: Option<Receiver<RuntimeControl>>,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), LinuxRuntimeError> {
    if wsl::detect() == Some(1) {
        return Err(unavailable(
            "WSL1 does not provide the required namespace boundary",
        ));
    }
    let policy =
        crate::network::NetworkPolicy::new(request.network_rules.clone()).map_err(network_error)?;
    let proxy = if request.network_enabled {
        Some(crate::network::proxy::ProxyHandle::start(policy).map_err(network_error)?)
    } else {
        None
    };
    let bridge = proxy
        .as_ref()
        .map(|proxy| proxy_routing::HostBridge::start(proxy.address()))
        .transpose()
        .map_err(unavailable)?;
    let mut request = request;
    request.proxy_socket_dir = bridge.as_ref().map(|value| value.socket_dir.clone());
    let source = bwrap_source::locate(&request.cwd).map_err(unavailable)?;
    let plan = bwrap::build_args(&request).map_err(denied)?;
    let mut command = Command::new(source.executable());
    command.args(&plan.args);
    command
        .stdin(if request.stdin.is_some() || control_rx.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| unavailable(format!("failed to run bubblewrap: {error}")))?;
    let stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");

    let mut readiness = vec![0; seccomp::INNER_READY_MARKER.len()];
    if stderr.read_exact(&mut readiness).is_err() || readiness != seccomp::INNER_READY_MARKER {
        let _ = child.kill();
        let status = child.wait().ok().and_then(|value| value.code());
        if status == Some(seccomp::INNER_SETUP_FAILURE_EXIT) {
            return Err(denied("inner no_new_privs/seccomp setup failed"));
        }
        return Err(denied("bubblewrap did not reach the hardened inner stage"));
    }

    sender
        .send(RuntimeMessage::Started {
            pid: Some(child.id()),
            capabilities: RuntimeCapabilities {
                backend: "linux_bwrap",
                filesystem_sandbox: true,
                process_tree_cleanup: true,
                managed_network: request.network_enabled,
                system_bwrap: source.is_system(),
                bundled_bwrap: !source.is_system(),
                wsl_version: wsl::detect(),
                local_binding_control: true,
                explicit_handle_inheritance: false,
                windows_restricted_token: false,
                windows_acl: false,
                windows_job: false,
                windows_wfp: false,
            },
        })
        .map_err(|_| unavailable("protocol receiver disconnected"))?;

    if let Some(control_rx) = control_rx {
        let mut child_stdin = child.stdin.take().expect("piped stdin");
        thread::spawn(move || {
            for control in control_rx {
                match control {
                    RuntimeControl::Write(data) => {
                        if child_stdin.write_all(&data).is_err() {
                            break;
                        }
                    }
                    RuntimeControl::Close => break,
                }
            }
        });
    } else if let Some(stdin) = request.stdin {
        let mut child_stdin = child.stdin.take().expect("piped stdin");
        thread::spawn(move || {
            let _ = child_stdin.write_all(&stdin);
        });
    }

    let budget = Arc::new(Mutex::new(request.max_output_bytes));
    let (failure_sender, failure_receiver) = mpsc::channel();
    let stdout_reader = spawn_reader(
        stdout,
        Arc::clone(&budget),
        sender.clone(),
        failure_sender.clone(),
        StreamKind::Stdout,
    );
    let stderr_reader = spawn_reader(
        stderr,
        budget,
        sender.clone(),
        failure_sender,
        StreamKind::Stderr,
    );

    let status = loop {
        if let Ok(failure) = failure_receiver.try_recv() {
            let _ = child.kill();
            let _ = child.wait();
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            return Err(failure.into_error());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(error) => return Err(unavailable(format!("cannot wait for bubblewrap: {error}"))),
        }
    };
    let _ = stdout_reader.join();
    let _ = stderr_reader.join();
    if let Ok(failure) = failure_receiver.try_recv() {
        return Err(failure.into_error());
    }
    sender
        .send(RuntimeMessage::Completed(status.code().unwrap_or(-1)))
        .map_err(|_| unavailable("protocol receiver disconnected"))
}

#[derive(Clone, Copy)]
enum StreamKind {
    Stdout,
    Stderr,
}

enum StreamFailure {
    OutputTruncated,
    ReadFailed,
    ReceiverDisconnected,
}

impl StreamFailure {
    fn into_error(self) -> LinuxRuntimeError {
        match self {
            Self::OutputTruncated => LinuxRuntimeError {
                code: "output_truncated",
                message: "sandbox output exceeded the configured limit".to_string(),
            },
            Self::ReadFailed => unavailable("cannot read sandbox output"),
            Self::ReceiverDisconnected => unavailable("protocol receiver disconnected"),
        }
    }
}

fn spawn_reader(
    mut reader: impl Read + Send + 'static,
    budget: Arc<Mutex<usize>>,
    sender: SyncSender<RuntimeMessage>,
    failure_sender: mpsc::Sender<StreamFailure>,
    stream: StreamKind,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut buffer = [0_u8; MAX_OUTPUT_CHUNK_BYTES];
        loop {
            let count = match reader.read(&mut buffer) {
                Ok(0) => return,
                Ok(count) => count,
                Err(_) => {
                    let _ = failure_sender.send(StreamFailure::ReadFailed);
                    return;
                }
            };
            let retained = {
                let mut remaining = budget
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                let retained = (*remaining).min(count);
                *remaining -= retained;
                retained
            };
            if retained > 0 {
                let message = match stream {
                    StreamKind::Stdout => RuntimeMessage::Stdout(buffer[..retained].to_vec()),
                    StreamKind::Stderr => RuntimeMessage::Stderr(buffer[..retained].to_vec()),
                };
                if sender.send(message).is_err() {
                    let _ = failure_sender.send(StreamFailure::ReceiverDisconnected);
                    return;
                }
            }
            if retained < count {
                let _ = failure_sender.send(StreamFailure::OutputTruncated);
                return;
            }
        }
    })
}

fn unavailable(message: impl Into<String>) -> LinuxRuntimeError {
    LinuxRuntimeError {
        code: "sandbox_unavailable",
        message: message.into(),
    }
}

fn denied(message: impl Into<String>) -> LinuxRuntimeError {
    LinuxRuntimeError {
        code: "sandbox_denied",
        message: message.into(),
    }
}

fn network_error(error: crate::network::policy::NetworkError) -> LinuxRuntimeError {
    LinuxRuntimeError {
        code: error.code.as_str(),
        message: error.message,
    }
}
