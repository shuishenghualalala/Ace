pub mod bwrap;
pub mod bwrap_source;
pub mod proxy_routing;
pub mod seccomp;
pub mod wsl;

use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, ChildStderr, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use std::{collections::BTreeMap, io::Read, io::Write, thread};

pub use crate::protocol::{FilesystemGlobAccess, FilesystemGlobRule};
use crate::protocol::{
    RuntimeCapabilities, RuntimeMessage, StdioInputMessage, MAX_OUTPUT_CHUNK_BYTES,
};

const INNER_READY_TIMEOUT: Duration = Duration::from_secs(5);

fn set_child_resource_limits() -> std::io::Result<()> {
    const LIMITS: [(libc::c_int, libc::rlim_t); 4] = [
        (libc::RLIMIT_AS, 4 * 1024 * 1024 * 1024),
        (libc::RLIMIT_FSIZE, 2 * 1024 * 1024 * 1024),
        (libc::RLIMIT_NOFILE, 4096),
        (libc::RLIMIT_NPROC, 256),
    ];
    for (resource, value) in LIMITS {
        let limit = libc::rlimit {
            rlim_cur: value,
            rlim_max: value,
        };
        // SAFETY: called in Command::pre_exec after fork and before exec; the
        // structure is initialized and setrlimit is async-signal-safe.
        if unsafe { libc::setrlimit(resource as _, &limit) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

pub struct LinuxRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub filesystem_globs: Vec<FilesystemGlobRule>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub proxy_socket_dir: Option<PathBuf>,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub stdin_stream: Option<Receiver<StdioInputMessage>>,
    pub env_overrides: BTreeMap<String, String>,
}

pub struct LinuxRuntimeError {
    pub code: &'static str,
    pub message: String,
}

struct ManagedChild {
    child: Child,
    reaped: bool,
}

impl ManagedChild {
    fn new(child: Child) -> Self {
        Self {
            child,
            reaped: false,
        }
    }

    fn id(&self) -> u32 {
        self.child.id()
    }

    fn take_stdin(&mut self) -> Option<std::process::ChildStdin> {
        self.child.stdin.take()
    }

    fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        let status = self.child.try_wait()?;
        if status.is_some() {
            self.reaped = true;
        }
        Ok(status)
    }

    fn kill_and_wait(&mut self) -> Option<ExitStatus> {
        if self.reaped {
            return None;
        }
        let _ = self.child.kill();
        match self.child.wait() {
            Ok(status) => {
                self.reaped = true;
                Some(status)
            }
            Err(_) => None,
        }
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.child.kill();
            let _ = self.child.wait();
            self.reaped = true;
        }
    }
}

pub fn run(
    request: LinuxRunRequest,
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
        .map(|proxy| {
            proxy_routing::HostBridge::start(
                proxy.address(),
                proxy.authorization_header().to_string(),
            )
        })
        .transpose()
        .map_err(unavailable)?;
    let mut request = request;
    request.proxy_socket_dir = bridge.as_ref().map(|value| value.socket_dir.clone());
    let source = bwrap_source::locate(&request.cwd).map_err(unavailable)?;
    let mut plan = bwrap::build_args(&request).map_err(denied)?;
    let mut command = Command::new(source.executable());
    command.args(&plan.args);
    command
        .stdin(
            if request.stdin.is_some() || request.stdin_stream.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            },
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // The limits apply to bubblewrap and are inherited by every process in its
    // PID namespace, preventing an MCP child from escaping host process budgets.
    unsafe {
        command.pre_exec(set_child_resource_limits);
    }
    let mut child = command
        .spawn()
        .map_err(|error| unavailable(format!("failed to run bubblewrap: {error}")))?;
    plan.mark_spawned();
    let stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let mut child = ManagedChild::new(child);

    await_inner_readiness(&mut stderr, &mut child)?;

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

    let budget = Arc::new(Mutex::new(request.max_output_bytes));
    let (failure_sender, failure_receiver) = mpsc::channel();
    let input_finished = Arc::new(AtomicBool::new(false));
    let stdin_writer = if request.stdin.is_some() || request.stdin_stream.is_some() {
        let child_stdin = child.take_stdin().expect("piped stdin");
        Some(spawn_stdin_writer(
            child_stdin,
            request.stdin,
            request.stdin_stream,
            Arc::clone(&input_finished),
            failure_sender.clone(),
        ))
    } else {
        None
    };
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
            child.kill_and_wait();
            input_finished.store(true, Ordering::Release);
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            if let Some(writer) = stdin_writer {
                let _ = writer.join();
            }
            return Err(failure.into_error());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(error) => {
                child.kill_and_wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(unavailable(format!("cannot wait for bubblewrap: {error}")));
            }
        }
    };
    input_finished.store(true, Ordering::Release);
    let _ = stdout_reader.join();
    let _ = stderr_reader.join();
    if let Some(writer) = stdin_writer {
        let _ = writer.join();
    }
    if let Ok(failure) = failure_receiver.try_recv() {
        return Err(failure.into_error());
    }
    plan.cleanup().map_err(denied)?;
    sender
        .send(RuntimeMessage::Completed(status.code().unwrap_or(-1)))
        .map_err(|_| unavailable("protocol receiver disconnected"))
}

fn await_inner_readiness(
    stderr: &mut ChildStderr,
    child: &mut ManagedChild,
) -> Result<(), LinuxRuntimeError> {
    let fd = stderr.as_raw_fd();
    let original_flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if original_flags < 0
        || unsafe { libc::fcntl(fd, libc::F_SETFL, original_flags | libc::O_NONBLOCK) } < 0
    {
        child.kill_and_wait();
        return Err(denied("cannot monitor hardened inner-stage readiness"));
    }

    let deadline = Instant::now() + INNER_READY_TIMEOUT;
    let mut readiness = Vec::with_capacity(seccomp::INNER_READY_MARKER.len());
    loop {
        let mut buffer = [0_u8; 64];
        let remaining = seccomp::INNER_READY_MARKER.len() - readiness.len();
        match stderr.read(&mut buffer[..remaining]) {
            Ok(0) => {
                let status = child.try_wait().ok().flatten();
                restore_blocking(fd, original_flags);
                return Err(inner_readiness_error(
                    status.or_else(|| child.kill_and_wait()),
                    "bubblewrap did not reach the hardened inner stage",
                ));
            }
            Ok(count) => {
                readiness.extend_from_slice(&buffer[..count]);
                if !seccomp::INNER_READY_MARKER.starts_with(&readiness) {
                    let status = child.try_wait().ok().flatten();
                    restore_blocking(fd, original_flags);
                    return Err(inner_readiness_error(
                        status.or_else(|| child.kill_and_wait()),
                        "bubblewrap did not reach the hardened inner stage",
                    ));
                }
                if readiness == seccomp::INNER_READY_MARKER {
                    if !restore_blocking(fd, original_flags) {
                        child.kill_and_wait();
                        return Err(denied(
                            "cannot restore sandbox output after inner-stage readiness",
                        ));
                    }
                    return Ok(());
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => {
                restore_blocking(fd, original_flags);
                return Err(inner_readiness_error(
                    child.kill_and_wait(),
                    "cannot read hardened inner-stage readiness",
                ));
            }
        }

        if let Some(status) = child.try_wait().map_err(|error| {
            unavailable(format!(
                "cannot inspect bubblewrap during hardened setup: {error}"
            ))
        })? {
            restore_blocking(fd, original_flags);
            return Err(inner_readiness_error(
                Some(status),
                "bubblewrap exited before the hardened inner stage",
            ));
        }
        if Instant::now() >= deadline {
            let status = child.kill_and_wait();
            restore_blocking(fd, original_flags);
            return Err(inner_readiness_error(
                status,
                "bubblewrap did not reach the hardened inner stage before timeout",
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn restore_blocking(fd: i32, original_flags: i32) -> bool {
    (unsafe { libc::fcntl(fd, libc::F_SETFL, original_flags) }) >= 0
}

fn inner_readiness_error(
    status: Option<ExitStatus>,
    fallback: impl Into<String>,
) -> LinuxRuntimeError {
    if status.and_then(|value| value.code()) == Some(seccomp::INNER_SETUP_FAILURE_EXIT) {
        denied("inner no_new_privs/seccomp setup failed")
    } else {
        denied(fallback)
    }
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
    StdinFailed,
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
            Self::StdinFailed => LinuxRuntimeError {
                code: "runtime_protocol_mismatch",
                message: "authenticated sandbox stdin stream failed".to_string(),
            },
        }
    }
}

fn spawn_stdin_writer(
    mut writer: std::process::ChildStdin,
    once: Option<Vec<u8>>,
    stream: Option<Receiver<StdioInputMessage>>,
    finished: Arc<AtomicBool>,
    failure_sender: mpsc::Sender<StreamFailure>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        if let Some(value) = once {
            if writer.write_all(&value).is_err() {
                let _ = failure_sender.send(StreamFailure::StdinFailed);
            }
            return;
        }
        let Some(stream) = stream else {
            return;
        };
        while !finished.load(Ordering::Acquire) {
            match stream.recv_timeout(Duration::from_millis(10)) {
                Ok(StdioInputMessage::Data(value)) => {
                    if writer
                        .write_all(&value)
                        .and_then(|_| writer.flush())
                        .is_err()
                    {
                        let _ = failure_sender.send(StreamFailure::StdinFailed);
                        return;
                    }
                }
                Ok(StdioInputMessage::Close) => return,
                Ok(StdioInputMessage::Abort) => {
                    let _ = failure_sender.send(StreamFailure::StdinFailed);
                    return;
                }
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => {
                    if !finished.load(Ordering::Acquire) {
                        let _ = failure_sender.send(StreamFailure::StdinFailed);
                    }
                    return;
                }
            }
        }
    })
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
