use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{FromRawHandle, RawHandle};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessAsUserW, CreateProcessWithLogonW, DeleteProcThreadAttributeList,
    GetExitCodeProcess, InitializeProcThreadAttributeList, ResumeThread, TerminateProcess,
    UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, INFINITE, LOGON_WITH_PROFILE,
    LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION, STARTF_USESTDHANDLES, STARTUPINFOEXW,
    STARTUPINFOW,
};

use super::desktop::LaunchDesktop;
use super::identity::SandboxCredentials;
use super::job::KillOnCloseJob;
use super::token::create_restricted_token;
use super::WindowsRunRequest;
use crate::protocol::{
    RuntimeCapabilities, RuntimeMessage, StdioInputMessage, MAX_OUTPUT_CHUNK_BYTES,
    MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES,
};

const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x0002_0002;

#[derive(Debug, Serialize, Deserialize)]
struct RunnerRequest {
    command: Vec<String>,
    cwd: PathBuf,
    temp_dir: PathBuf,
    username: String,
    capability_sids: Vec<String>,
    max_output_bytes: usize,
    network_enabled: bool,
    proxy_url: Option<String>,
    allow_local_binding: bool,
    stdin_b64: Option<String>,
    stream_stdin: bool,
    env_overrides: BTreeMap<String, String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum RunnerInputFrame {
    Stdin { data_b64: String },
    StdinClose,
    Abort,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum RunnerEvent {
    Started { seq: u64, pid: u32 },
    Stdout { seq: u64, data_b64: String },
    Stderr { seq: u64, data_b64: String },
    Completed { seq: u64, exit_code: i32 },
    Error { seq: u64, message: String },
}

enum RunnerMessage {
    Started(u32),
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    Completed(i32),
    Error(String),
}

pub struct RunViaAccountContext<'a> {
    pub temp_dir: &'a Path,
    pub capability_sids: &'a [String],
    pub proxy_url: Option<String>,
    pub capabilities: RuntimeCapabilities,
    pub stdin_stream: Option<Receiver<StdioInputMessage>>,
    pub sender: &'a SyncSender<RuntimeMessage>,
}

pub fn run_via_account<F>(
    credentials: &SandboxCredentials,
    request: &WindowsRunRequest,
    context: RunViaAccountContext<'_>,
    verify_authorized_paths: F,
) -> Result<i32, String>
where
    F: FnOnce() -> Result<(), String>,
{
    let RunViaAccountContext {
        temp_dir,
        capability_sids,
        proxy_url,
        capabilities,
        stdin_stream,
        sender,
    } = context;
    let runner_request = RunnerRequest {
        command: request.command.clone(),
        cwd: request.cwd.clone(),
        temp_dir: temp_dir.to_path_buf(),
        username: credentials.username.clone(),
        capability_sids: capability_sids.to_vec(),
        max_output_bytes: request.max_output_bytes,
        network_enabled: request.network_enabled,
        proxy_url,
        allow_local_binding: request.allow_local_binding,
        stdin_b64: request
            .stdin
            .as_ref()
            .map(|value| BASE64_STANDARD.encode(value)),
        stream_stdin: stdin_stream.is_some(),
        env_overrides: request.env_overrides.clone(),
    };
    let executable = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(|error| format!("cannot resolve runtime executable: {error}"))?;
    let mut child_stdin = Pipe::new(/*parent_reads*/ false)?;
    let child_stdout = Pipe::new(/*parent_reads*/ true)?;
    let child_stderr = Pipe::new(/*parent_reads*/ true)?;
    let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
    startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = child_stdin.child;
    startup.hStdOutput = child_stdout.child;
    startup.hStdError = child_stderr.child;
    let mut process_info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let mut command_line = wide(format!("{} --windows-runner", quote_arg(&executable)));
    let username = wide(&credentials.username);
    let domain = wide(".");
    let password = wide(&credentials.password);
    let cwd = wide(executable.parent().unwrap_or(Path::new(r"C:\")));
    let mut environment = environment_block(restricted_environment(
        false,
        None,
        BTreeMap::new(),
        temp_dir,
        Some(&credentials.username),
    )?);
    let flags = CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
    let job = KillOnCloseJob::new()?;
    job.query_limits()?;
    // Audit W2: CreateProcessWithLogonW cannot accept PROC_THREAD_ATTRIBUTE_HANDLE_LIST
    // (only CreateProcessAsUserW supports the extended STARTUPINFOEXW attribute list).
    // We mitigate by ensuring the only inheritable handles at this point are the three
    // stdio pipe child ends -- Pipe::new already clears HANDLE_FLAG_INHERIT on the parent
    // side. The ACL mutex held by AclLease is non-inheritable (CreateMutexW with null
    // security attributes). The inner runner (run_restricted) then uses
    // CreateProcessAsUserW + PROC_THREAD_ATTRIBUTE_HANDLE_LIST to strictly confine what
    // the sandboxed command can see. Any future inheritable handle added to the runtime
    // before this spawn MUST clear HANDLE_FLAG_INHERIT to avoid leaking into the runner.
    let ok = unsafe {
        CreateProcessWithLogonW(
            username.as_ptr(),
            domain.as_ptr(),
            password.as_ptr(),
            LOGON_WITH_PROFILE,
            std::ptr::null(),
            command_line.as_mut_ptr(),
            flags,
            environment.as_mut_ptr().cast(),
            cwd.as_ptr(),
            &startup,
            &mut process_info,
        )
    };
    child_stdin.close_child();
    let child_stdout = child_stdout.into_parent_file_after_child_close();
    let mut child_stderr = child_stderr.into_parent_file_after_child_close();
    if ok == 0 {
        return Err(format!("CreateProcessWithLogonW failed: {}", unsafe {
            GetLastError()
        }));
    }
    let runner_process = OwnedHandle(process_info.hProcess);
    if let Err(error) = job.assign(runner_process.raw()) {
        unsafe {
            TerminateProcess(runner_process.raw(), 1);
            CloseHandle(process_info.hThread);
        }
        return Err(error);
    }
    if let Err(error) = verify_authorized_paths() {
        unsafe {
            TerminateProcess(runner_process.raw(), 1);
            CloseHandle(process_info.hThread);
        }
        return Err(error);
    }
    if unsafe { ResumeThread(process_info.hThread) } == u32::MAX {
        unsafe {
            CloseHandle(process_info.hThread);
        }
        return Err(format!("ResumeThread failed: {}", unsafe {
            GetLastError()
        }));
    }
    unsafe { CloseHandle(process_info.hThread) };
    let request_bytes = serde_json::to_vec(&runner_request)
        .map_err(|error| format!("cannot encode Windows runner request: {error}"))?;
    child_stdin
        .parent_file
        .write_all(&request_bytes)
        .and_then(|_| child_stdin.parent_file.write_all(b"\n"))
        .map_err(|error| format!("cannot send Windows runner request: {error}"))?;
    let input_finished = Arc::new(AtomicBool::new(false));
    let input_writer = match stdin_stream {
        Some(stream) => Some(spawn_runner_input_writer(
            child_stdin.into_parent_file_after_child_close(),
            stream,
            Arc::clone(&input_finished),
        )),
        None => {
            drop(child_stdin);
            None
        }
    };

    let stderr_reader =
        thread::spawn(move || read_capped(&mut child_stderr, MAX_RESPONSE_FRAME_BYTES));
    let mut reader = BufReader::new(child_stdout);
    let mut expected_seq = 0_u64;
    let mut started = false;
    let mut terminal: Result<i32, String> =
        Err("Windows runner closed before terminal".to_string());
    loop {
        let mut line = Vec::new();
        let count = reader
            .read_until(b'\n', &mut line)
            .map_err(|_| "cannot read Windows runner protocol".to_string())?;
        if count == 0 {
            break;
        }
        if line.len() > MAX_RESPONSE_FRAME_BYTES {
            terminal = Err("Windows runner protocol frame exceeds the size limit".to_string());
            break;
        }
        let event: RunnerEvent = serde_json::from_slice(&line)
            .map_err(|_| "invalid Windows runner protocol frame".to_string())?;
        let seq = match &event {
            RunnerEvent::Started { seq, .. }
            | RunnerEvent::Stdout { seq, .. }
            | RunnerEvent::Stderr { seq, .. }
            | RunnerEvent::Completed { seq, .. }
            | RunnerEvent::Error { seq, .. } => *seq,
        };
        if seq != expected_seq {
            terminal = Err("invalid Windows runner protocol sequence".to_string());
            break;
        }
        expected_seq += 1;
        match event {
            RunnerEvent::Started { pid, .. } if !started => {
                started = true;
                sender
                    .send(RuntimeMessage::Started {
                        pid: Some(pid),
                        capabilities: capabilities.clone(),
                    })
                    .map_err(|_| "protocol receiver disconnected".to_string())?;
            }
            RunnerEvent::Stdout { data_b64, .. } if started => {
                forward_runner_output(data_b64, sender, true)?;
            }
            RunnerEvent::Stderr { data_b64, .. } if started => {
                forward_runner_output(data_b64, sender, false)?;
            }
            RunnerEvent::Completed { exit_code, .. } if started => {
                terminal = Ok(exit_code);
                break;
            }
            RunnerEvent::Error { message, .. } => {
                terminal = Err(message);
                break;
            }
            _ => {
                terminal = Err("invalid Windows runner event ordering".to_string());
                break;
            }
        }
    }
    if terminal.is_err() {
        unsafe { TerminateProcess(runner_process.raw(), 1) };
    }
    input_finished.store(true, Ordering::Release);
    unsafe { WaitForSingleObject(runner_process.raw(), INFINITE) };
    let mut runner_exit = 0_u32;
    unsafe { GetExitCodeProcess(runner_process.raw(), &mut runner_exit) };
    let (_, stderr_truncated) = stderr_reader.join().unwrap_or_default();
    if let Some(writer) = input_writer {
        let _ = writer.join();
    }
    drop(job);
    if stderr_truncated {
        return Err("Windows sandbox runner diagnostics exceeded the size limit".to_string());
    }
    if terminal.is_ok() && runner_exit != 0 {
        return Err("Windows runner failed after terminal event".to_string());
    }
    terminal
}

pub fn runner_main() -> ! {
    let mut line = String::new();
    let request = BufReader::new(std::io::stdin())
        .read_line(&mut line)
        .and_then(|_| {
            serde_json::from_str::<RunnerRequest>(&line)
                .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
        });
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let result = match request {
        Ok(request) => run_restricted(request, &mut output),
        Err(_) => RunnerWriter::new(&mut output)
            .write(RunnerMessage::Error("invalid runner request".to_string())),
    };
    if let Err(error) = result {
        eprintln!("Windows runner failed: {error}");
        std::process::exit(1);
    }
    std::process::exit(0);
}

fn run_restricted<W: Write>(request: RunnerRequest, output: &mut W) -> Result<(), String> {
    let token = OwnedHandle(create_restricted_token(&request.capability_sids)?);
    // Keep the GUI-capable child off the user's interactive desktop. A failure
    // to create or ACL the private Desktop is a sandbox failure, never a
    // fallback to Winsta0\\Default.
    let desktop = LaunchDesktop::prepare()?;
    let child_stdin = Pipe::new(/*parent_reads*/ false)?;
    let child_stdout = Pipe::new(/*parent_reads*/ true)?;
    let child_stderr = Pipe::new(/*parent_reads*/ true)?;
    let inherited = vec![child_stdin.child, child_stdout.child, child_stderr.child];
    let mut attributes = ProcThreadAttributes::new()?;
    attributes.set_handle_list(&inherited)?;
    let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = child_stdin.child;
    startup.StartupInfo.hStdOutput = child_stdout.child;
    startup.StartupInfo.hStdError = child_stderr.child;
    startup.StartupInfo.lpDesktop = desktop.startup_info_desktop();
    startup.lpAttributeList = attributes.as_mut_ptr();
    let mut process_info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let mut command_line = wide(&command_line(&request.command)?);
    let cwd = wide(&request.cwd);
    if request.allow_local_binding {
        return Err(
            "Windows local binding requires the dedicated bind-capable sandbox identity"
                .to_string(),
        );
    }
    let mut environment = environment_block(restricted_environment(
        request.network_enabled,
        request.proxy_url.as_deref(),
        request.env_overrides,
        &request.temp_dir,
        Some(&request.username),
    )?);
    let flags = CREATE_NO_WINDOW
        | CREATE_SUSPENDED
        | CREATE_UNICODE_ENVIRONMENT
        | EXTENDED_STARTUPINFO_PRESENT;
    let ok = unsafe {
        CreateProcessAsUserW(
            token.raw(),
            std::ptr::null(),
            command_line.as_mut_ptr(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            1,
            flags,
            environment.as_mut_ptr().cast(),
            cwd.as_ptr(),
            &startup.StartupInfo,
            &mut process_info,
        )
    };
    let mut stdin = child_stdin.into_parent_file_after_child_close();
    let stdout = child_stdout.into_parent_file_after_child_close();
    let stderr = child_stderr.into_parent_file_after_child_close();
    if ok == 0 {
        return Err(format!("CreateProcessAsUserW failed: {}", unsafe {
            GetLastError()
        }));
    }
    let restricted_process = OwnedHandle(process_info.hProcess);
    // The runner itself is already in the outer kill-on-close Job; children
    // inherit that Job. Suspension closes the assignment/start race.
    if unsafe { ResumeThread(process_info.hThread) } == u32::MAX {
        unsafe {
            TerminateProcess(restricted_process.raw(), 1);
            CloseHandle(process_info.hThread);
        }
        return Err(format!("ResumeThread failed: {}", unsafe {
            GetLastError()
        }));
    }
    unsafe { CloseHandle(process_info.hThread) };
    let mut writer = RunnerWriter::new(output);
    writer.write(RunnerMessage::Started(process_info.dwProcessId))?;

    let budget = Arc::new(Mutex::new(request.max_output_bytes));
    let (sender, receiver) = mpsc::sync_channel(64);
    let (failure_sender, failure_receiver) = mpsc::channel();
    if let Some(encoded) = request.stdin_b64 {
        let value = BASE64_STANDARD
            .decode(encoded)
            .map_err(|_| "invalid runner stdin payload".to_string())?;
        thread::spawn(move || {
            let _ = stdin.write_all(&value);
        });
    } else if request.stream_stdin {
        spawn_restricted_input_writer(stdin, failure_sender.clone());
    } else {
        drop(stdin);
    }

    let stdout_reader = spawn_runner_reader(
        stdout,
        Arc::clone(&budget),
        sender.clone(),
        failure_sender.clone(),
        true,
    );
    let stderr_reader = spawn_runner_reader(stderr, budget, sender, failure_sender, false);
    let mut stream_failure = None;
    loop {
        while let Ok(message) = receiver.try_recv() {
            writer.write(message)?;
        }
        if let Ok(error) = failure_receiver.try_recv() {
            stream_failure = Some(error);
            unsafe { TerminateProcess(restricted_process.raw(), 1) };
            break;
        }
        match unsafe { WaitForSingleObject(restricted_process.raw(), 10) } {
            WAIT_OBJECT_0 => break,
            WAIT_TIMEOUT => {}
            _ => {
                stream_failure = Some("cannot wait for restricted child".to_string());
                unsafe { TerminateProcess(restricted_process.raw(), 1) };
                break;
            }
        }
    }
    unsafe { WaitForSingleObject(restricted_process.raw(), INFINITE) };
    let mut exit_code = 0_u32;
    unsafe { GetExitCodeProcess(restricted_process.raw(), &mut exit_code) };
    let _ = stdout_reader.join();
    let _ = stderr_reader.join();
    while let Ok(message) = receiver.try_recv() {
        writer.write(message)?;
    }
    if let Some(error) = stream_failure.or_else(|| failure_receiver.try_recv().ok()) {
        return writer.write(RunnerMessage::Error(error));
    }
    writer.write(RunnerMessage::Completed(exit_code as i32))
}

fn forward_runner_output(
    encoded: String,
    sender: &SyncSender<RuntimeMessage>,
    stdout: bool,
) -> Result<(), String> {
    let data = BASE64_STANDARD
        .decode(encoded)
        .map_err(|_| "invalid Windows runner output encoding".to_string())?;
    if data.len() > MAX_OUTPUT_CHUNK_BYTES {
        return Err("Windows runner output chunk exceeds the size limit".to_string());
    }
    let message = if stdout {
        RuntimeMessage::Stdout(data)
    } else {
        RuntimeMessage::Stderr(data)
    };
    sender
        .send(message)
        .map_err(|_| "protocol receiver disconnected".to_string())
}

fn spawn_runner_input_writer(
    mut writer: File,
    receiver: Receiver<StdioInputMessage>,
    finished: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while !finished.load(Ordering::Acquire) {
            let frame = match receiver.recv_timeout(Duration::from_millis(10)) {
                Ok(StdioInputMessage::Data(value)) => RunnerInputFrame::Stdin {
                    data_b64: BASE64_STANDARD.encode(value),
                },
                Ok(StdioInputMessage::Close) => RunnerInputFrame::StdinClose,
                Ok(StdioInputMessage::Abort) => RunnerInputFrame::Abort,
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => RunnerInputFrame::Abort,
            };
            let terminal = !matches!(frame, RunnerInputFrame::Stdin { .. });
            let mut encoded = match serde_json::to_vec(&frame) {
                Ok(value) => value,
                Err(_) => return,
            };
            encoded.push(b'\n');
            if encoded.len() > MAX_REQUEST_FRAME_BYTES
                || writer
                    .write_all(&encoded)
                    .and_then(|_| writer.flush())
                    .is_err()
            {
                return;
            }
            if terminal {
                return;
            }
        }
    })
}

fn spawn_restricted_input_writer(mut writer: File, failure_sender: mpsc::Sender<String>) {
    thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut reader = BufReader::new(stdin.lock());
        loop {
            let mut line = Vec::new();
            let count = match reader.read_until(b'\n', &mut line) {
                Ok(value) => value,
                Err(_) => {
                    let _ = failure_sender
                        .send("authenticated runner stdin could not be read".to_string());
                    return;
                }
            };
            if count == 0 || line.len() > MAX_REQUEST_FRAME_BYTES {
                let _ = failure_sender
                    .send("authenticated runner stdin closed unexpectedly".to_string());
                return;
            }
            let frame: RunnerInputFrame = match serde_json::from_slice(&line) {
                Ok(value) => value,
                Err(_) => {
                    let _ = failure_sender
                        .send("authenticated runner stdin frame is invalid".to_string());
                    return;
                }
            };
            match frame {
                RunnerInputFrame::Stdin { data_b64 } => {
                    let value = match BASE64_STANDARD.decode(data_b64) {
                        Ok(value)
                            if value.len() <= crate::protocol::MAX_STDIO_INPUT_FRAME_BYTES =>
                        {
                            value
                        }
                        _ => {
                            let _ = failure_sender
                                .send("authenticated runner stdin encoding is invalid".to_string());
                            return;
                        }
                    };
                    if writer
                        .write_all(&value)
                        .and_then(|_| writer.flush())
                        .is_err()
                    {
                        let _ = failure_sender
                            .send("restricted child stdin is unavailable".to_string());
                        return;
                    }
                }
                RunnerInputFrame::StdinClose => return,
                RunnerInputFrame::Abort => {
                    let _ = failure_sender
                        .send("authenticated runner stdin stream aborted".to_string());
                    return;
                }
            }
        }
    });
}

fn spawn_runner_reader(
    mut reader: impl Read + Send + 'static,
    budget: Arc<Mutex<usize>>,
    sender: SyncSender<RunnerMessage>,
    failure_sender: mpsc::Sender<String>,
    stdout: bool,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        while let Ok(count) = reader.read(&mut buffer) {
            if count == 0 {
                return;
            }
            let retained = {
                let mut remaining = budget
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                let retained = (*remaining).min(count);
                *remaining -= retained;
                retained
            };
            if retained > 0 {
                let message = if stdout {
                    RunnerMessage::Stdout(buffer[..retained].to_vec())
                } else {
                    RunnerMessage::Stderr(buffer[..retained].to_vec())
                };
                if sender.send(message).is_err() {
                    let _ = failure_sender.send("runner protocol writer disconnected".to_string());
                    return;
                }
            }
            if retained < count {
                let _ = failure_sender.send(
                    "OUTPUT_TRUNCATED: sandbox command output exceeded the configured limit"
                        .to_string(),
                );
                return;
            }
        }
        let _ = failure_sender.send("cannot read restricted child output".to_string());
    })
}

struct RunnerWriter<'a, W: Write> {
    output: &'a mut W,
    seq: u64,
    started: bool,
    terminal: bool,
}

impl<'a, W: Write> RunnerWriter<'a, W> {
    fn new(output: &'a mut W) -> Self {
        Self {
            output,
            seq: 0,
            started: false,
            terminal: false,
        }
    }

    fn write(&mut self, message: RunnerMessage) -> Result<(), String> {
        if self.terminal {
            return Err("runner emitted data after terminal".to_string());
        }
        let terminal = matches!(
            message,
            RunnerMessage::Completed(_) | RunnerMessage::Error(_)
        );
        let event = match message {
            RunnerMessage::Started(pid) if !self.started => {
                self.started = true;
                RunnerEvent::Started { seq: self.seq, pid }
            }
            RunnerMessage::Stdout(data) if self.started => RunnerEvent::Stdout {
                seq: self.seq,
                data_b64: BASE64_STANDARD.encode(data),
            },
            RunnerMessage::Stderr(data) if self.started => RunnerEvent::Stderr {
                seq: self.seq,
                data_b64: BASE64_STANDARD.encode(data),
            },
            RunnerMessage::Completed(exit_code) if self.started => RunnerEvent::Completed {
                seq: self.seq,
                exit_code,
            },
            RunnerMessage::Error(message) => RunnerEvent::Error {
                seq: self.seq,
                message,
            },
            _ => return Err("invalid runner event ordering".to_string()),
        };
        let mut frame =
            serde_json::to_vec(&event).map_err(|_| "cannot encode runner event".to_string())?;
        frame.push(b'\n');
        if frame.len() > MAX_RESPONSE_FRAME_BYTES {
            return Err("runner event exceeds the size limit".to_string());
        }
        self.output
            .write_all(&frame)
            .and_then(|_| self.output.flush())
            .map_err(|_| "cannot write runner event".to_string())?;
        self.seq += 1;
        self.terminal = terminal;
        Ok(())
    }
}

struct OwnedHandle(HANDLE);

impl OwnedHandle {
    fn raw(&self) -> HANDLE {
        self.0
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if self.0 != 0 {
            unsafe { CloseHandle(self.0) };
        }
    }
}

struct Pipe {
    parent_file: File,
    child: HANDLE,
}

impl Pipe {
    fn new(parent_reads: bool) -> Result<Self, String> {
        let mut read = 0;
        let mut write = 0;
        let security = windows_sys::Win32::Security::SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<windows_sys::Win32::Security::SECURITY_ATTRIBUTES>()
                as u32,
            lpSecurityDescriptor: std::ptr::null_mut(),
            bInheritHandle: 1,
        };
        if unsafe { CreatePipe(&mut read, &mut write, &security, 0) } == 0 {
            return Err(format!("CreatePipe failed: {}", unsafe { GetLastError() }));
        }
        let (parent, child) = if parent_reads {
            (read, write)
        } else {
            (write, read)
        };
        if unsafe { SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0) } == 0 {
            unsafe {
                CloseHandle(read);
                CloseHandle(write);
            }
            return Err(format!("SetHandleInformation failed: {}", unsafe {
                GetLastError()
            }));
        }
        let parent_file = unsafe { File::from_raw_handle(parent as RawHandle) };
        Ok(Self { parent_file, child })
    }

    fn close_child(&mut self) {
        if self.child != 0 {
            unsafe { CloseHandle(self.child) };
            self.child = 0;
        }
    }

    fn into_parent_file_after_child_close(mut self) -> File {
        self.close_child();
        let placeholder = File::open("NUL").expect("NUL must exist");
        std::mem::replace(&mut self.parent_file, placeholder)
    }
}

impl Drop for Pipe {
    fn drop(&mut self) {
        self.close_child();
    }
}

struct ProcThreadAttributes {
    buffer: Vec<u8>,
    handles: Vec<HANDLE>,
}

impl ProcThreadAttributes {
    fn new() -> Result<Self, String> {
        let mut size = 0_usize;
        unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size) };
        if size == 0 {
            return Err(format!("cannot size process attributes: {}", unsafe {
                GetLastError()
            }));
        }
        let mut buffer = vec![0_u8; size];
        let pointer = buffer.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        if unsafe { InitializeProcThreadAttributeList(pointer, 1, 0, &mut size) } == 0 {
            return Err(format!(
                "cannot initialize process attributes: {}",
                unsafe { GetLastError() }
            ));
        }
        Ok(Self {
            buffer,
            handles: Vec::new(),
        })
    }

    fn as_mut_ptr(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.buffer.as_mut_ptr().cast()
    }

    fn set_handle_list(&mut self, handles: &[HANDLE]) -> Result<(), String> {
        self.handles = handles.to_vec();
        let pointer = self.as_mut_ptr();
        if unsafe {
            UpdateProcThreadAttribute(
                pointer,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                self.handles.as_mut_ptr().cast(),
                std::mem::size_of_val(self.handles.as_slice()),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(format!("cannot set inherited handle list: {}", unsafe {
                GetLastError()
            }));
        }
        Ok(())
    }
}

impl Drop for ProcThreadAttributes {
    fn drop(&mut self) {
        unsafe { DeleteProcThreadAttributeList(self.as_mut_ptr()) };
    }
}

fn restricted_environment(
    network_enabled: bool,
    proxy_url: Option<&str>,
    env_overrides: BTreeMap<String, String>,
    temp_dir: &Path,
    username: Option<&str>,
) -> Result<BTreeMap<String, String>, String> {
    for name in env_overrides.keys() {
        if matches!(
            name.to_ascii_uppercase().as_str(),
            "ACE_SANDBOX"
                | "SYSTEMROOT"
                | "WINDIR"
                | "COMSPEC"
                | "PATH"
                | "TEMP"
                | "TMP"
                | "USERNAME"
                | "USERPROFILE"
                | "HOMEDRIVE"
                | "HOMEPATH"
        ) {
            return Err(format!(
                "Windows sandbox environment entry is reserved: {name}"
            ));
        }
    }
    let mut result = BTreeMap::new();
    for name in ["SystemRoot", "WINDIR", "ComSpec"] {
        if let Ok(value) = std::env::var(name) {
            result.insert(name.to_string(), value);
        }
    }
    let system_root = result
        .get("SystemRoot")
        .cloned()
        .unwrap_or_else(|| r"C:\Windows".to_string());
    result.insert(
        "PATH".to_string(),
        format!(r"{system_root}\System32;{system_root}"),
    );
    let temp = temp_dir.as_os_str().to_string_lossy().into_owned();
    result.insert("TEMP".to_string(), temp.clone());
    result.insert("TMP".to_string(), temp);
    if let Some(username) = username {
        result.insert("USERNAME".to_string(), username.to_string());
    }
    result.extend(env_overrides);
    result.insert(
        "ACE_SANDBOX".to_string(),
        "windows-sandbox-account".to_string(),
    );
    if network_enabled {
        let proxy = proxy_url
            .filter(|value| {
                value.starts_with("http://crew:")
                    && value.ends_with("@127.0.0.1:43119")
                    && !value.bytes().any(|byte| byte <= 0x20 || byte == 0x7f)
            })
            .ok_or_else(|| "managed proxy credential is unavailable".to_string())?
            .to_string();
        result.insert("HTTP_PROXY".to_string(), proxy.clone());
        result.insert("HTTPS_PROXY".to_string(), proxy.clone());
        result.insert("ALL_PROXY".to_string(), proxy);
        result.insert("NO_PROXY".to_string(), String::new());
    } else if proxy_url.is_some() {
        return Err("offline sandbox cannot receive proxy credentials".to_string());
    }
    Ok(result)
}

fn environment_block(values: BTreeMap<String, String>) -> Vec<u16> {
    let mut result = Vec::new();
    for (key, value) in values {
        result.extend(wide(format!("{key}={value}")));
    }
    result.push(0);
    result
}

fn command_line(argv: &[String]) -> Result<String, String> {
    if argv.is_empty() {
        return Err("empty Windows command".to_string());
    }
    Ok(argv.iter().map(quote_arg).collect::<Vec<_>>().join(" "))
}

fn quote_arg(value: impl AsRef<OsStr>) -> String {
    let value = value.as_ref().to_string_lossy();
    if !value.is_empty()
        && !value
            .chars()
            .any(|character| character.is_whitespace() || character == '"')
    {
        return value.into_owned();
    }
    let mut result = String::from("\"");
    let mut slashes = 0;
    for character in value.chars() {
        if character == '\\' {
            slashes += 1;
        } else if character == '"' {
            result.push_str(&"\\".repeat(slashes * 2 + 1));
            result.push('"');
            slashes = 0;
        } else {
            result.push_str(&"\\".repeat(slashes));
            slashes = 0;
            result.push(character);
        }
    }
    result.push_str(&"\\".repeat(slashes * 2));
    result.push('"');
    result
}

fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

fn read_capped(reader: &mut impl Read, limit: usize) -> (Vec<u8>, bool) {
    let mut result = Vec::new();
    let mut truncated = false;
    let mut buffer = [0_u8; 8192];
    while let Ok(count) = reader.read(&mut buffer) {
        if count == 0 {
            break;
        }
        let retain = limit.saturating_sub(result.len()).min(count);
        result.extend_from_slice(&buffer[..retain]);
        truncated |= retain < count;
    }
    (result, truncated)
}
