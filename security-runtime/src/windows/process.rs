use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{FromRawHandle, RawHandle};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;

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

use super::identity::SandboxCredentials;
use super::job::KillOnCloseJob;
use super::token::create_restricted_token;
use super::WindowsRunRequest;
use crate::protocol::{
    RuntimeCapabilities, RuntimeControl, RuntimeMessage, MAX_OUTPUT_CHUNK_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
};

const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x0002_0002;

#[derive(Debug, Serialize, Deserialize)]
struct RunnerRequest {
    command: Vec<String>,
    cwd: PathBuf,
    capability_sids: Vec<String>,
    max_output_bytes: usize,
    network_enabled: bool,
    allow_local_binding: bool,
    stdin_b64: Option<String>,
    env_overrides: BTreeMap<String, String>,
    home_files: BTreeMap<String, String>,
    full_disk_read: bool,
    host_home: Option<PathBuf>,
    interactive: bool,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum RunnerControl {
    Write { data_b64: String },
    Close,
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

pub fn run_via_account(
    credentials: &SandboxCredentials,
    request: &WindowsRunRequest,
    capability_sids: &[String],
    capabilities: RuntimeCapabilities,
    control_rx: Option<Receiver<RuntimeControl>>,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), String> {
    let runner_request = RunnerRequest {
        command: request.command.clone(),
        cwd: request.cwd.clone(),
        capability_sids: capability_sids.to_vec(),
        max_output_bytes: request.max_output_bytes,
        network_enabled: request.network_enabled,
        allow_local_binding: request.allow_local_binding,
        stdin_b64: request
            .stdin
            .as_ref()
            .map(|value| BASE64_STANDARD.encode(value)),
        env_overrides: request.env_overrides.clone(),
        home_files: request
            .home_files
            .iter()
            .map(|(path, content)| (path.clone(), BASE64_STANDARD.encode(content)))
            .collect(),
        full_disk_read: request.full_disk_read,
        host_home: if request.full_disk_read && request.home_files.is_empty() {
            host_home()
        } else {
            None
        },
        interactive: control_rx.is_some(),
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
    let mut environment = environment_block(restricted_environment(false, BTreeMap::new()));
    let flags = CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
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
    let job = KillOnCloseJob::new()?;
    if let Err(error) = job.assign(process_info.hProcess) {
        unsafe {
            TerminateProcess(process_info.hProcess, 1);
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
        }
        return Err(error);
    }
    if unsafe { ResumeThread(process_info.hThread) } == u32::MAX {
        unsafe {
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
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
    if let Some(control_rx) = control_rx {
        let mut child_stdin = child_stdin.parent_file;
        thread::spawn(move || {
            for control in control_rx {
                let message = match control {
                    RuntimeControl::Write(data) => RunnerControl::Write {
                        data_b64: BASE64_STANDARD.encode(data),
                    },
                    RuntimeControl::Close => RunnerControl::Close,
                };
                let Ok(mut encoded) = serde_json::to_vec(&message) else {
                    break;
                };
                encoded.push(b'\n');
                if child_stdin.write_all(&encoded).is_err() {
                    break;
                }
                if matches!(message, RunnerControl::Close) {
                    break;
                }
            }
        });
    } else {
        drop(child_stdin);
    }

    let stderr_reader =
        thread::spawn(move || read_capped(&mut child_stderr, MAX_RESPONSE_FRAME_BYTES));
    let mut reader = BufReader::new(child_stdout);
    let mut expected_seq = 0_u64;
    let mut started = false;
    let mut terminal: Result<(), String> = Err("Windows runner closed before terminal".to_string());
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
                sender
                    .send(RuntimeMessage::Completed(exit_code))
                    .map_err(|_| "protocol receiver disconnected".to_string())?;
                terminal = Ok(());
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
        unsafe { TerminateProcess(process_info.hProcess, 1) };
    }
    unsafe { WaitForSingleObject(process_info.hProcess, INFINITE) };
    let mut runner_exit = 0_u32;
    unsafe {
        GetExitCodeProcess(process_info.hProcess, &mut runner_exit);
        CloseHandle(process_info.hProcess);
    }
    let (_, stderr_truncated) = stderr_reader.join().unwrap_or_default();
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
    let mut reader = BufReader::new(std::io::stdin());
    let mut line = String::new();
    let request = reader.read_line(&mut line).and_then(|_| {
        serde_json::from_str::<RunnerRequest>(&line)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    });
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let result = match request {
        Ok(request) => run_restricted(request, &mut output, reader),
        Err(_) => RunnerWriter::new(&mut output)
            .write(RunnerMessage::Error("invalid runner request".to_string())),
    };
    if let Err(error) = result {
        eprintln!("Windows runner failed: {error}");
        std::process::exit(1);
    }
    std::process::exit(0);
}

fn run_restricted<W: Write, R: BufRead + Send + 'static>(
    request: RunnerRequest,
    output: &mut W,
    reader: R,
) -> Result<(), String> {
    let token = create_restricted_token(&request.capability_sids)?;
    let staged_home = stage_home_files(&request.home_files)?;
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
    let mut child_environment =
        restricted_environment(request.network_enabled, request.env_overrides);
    if let Some(home) = &staged_home {
        child_environment.insert("HOME".to_string(), home.0.to_string_lossy().to_string());
        child_environment.insert(
            "USERPROFILE".to_string(),
            home.0.to_string_lossy().to_string(),
        );
        child_environment.insert(
            "APPDATA".to_string(),
            home.0
                .join("AppData")
                .join("Roaming")
                .to_string_lossy()
                .to_string(),
        );
    } else if request.full_disk_read {
        if let Some(home) = request.host_home.as_ref().filter(|path| path.is_absolute()) {
            let value = home.to_string_lossy().to_string();
            child_environment.insert("HOME".to_string(), value.clone());
            child_environment.insert("USERPROFILE".to_string(), value);
            child_environment.insert(
                "APPDATA".to_string(),
                home.join("AppData")
                    .join("Roaming")
                    .to_string_lossy()
                    .to_string(),
            );
        }
    }
    let mut environment = environment_block(child_environment);
    let flags = CREATE_NO_WINDOW
        | CREATE_SUSPENDED
        | CREATE_UNICODE_ENVIRONMENT
        | EXTENDED_STARTUPINFO_PRESENT;
    let ok = unsafe {
        CreateProcessAsUserW(
            token,
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
    unsafe { CloseHandle(token) };
    let mut stdin = child_stdin.into_parent_file_after_child_close();
    let stdout = child_stdout.into_parent_file_after_child_close();
    let stderr = child_stderr.into_parent_file_after_child_close();
    if ok == 0 {
        return Err(format!("CreateProcessAsUserW failed: {}", unsafe {
            GetLastError()
        }));
    }
    // The runner itself is already in the outer kill-on-close Job; children
    // inherit that Job. Suspension closes the assignment/start race.
    if unsafe { ResumeThread(process_info.hThread) } == u32::MAX {
        unsafe {
            TerminateProcess(process_info.hProcess, 1);
            CloseHandle(process_info.hThread);
            CloseHandle(process_info.hProcess);
        }
        return Err(format!("ResumeThread failed: {}", unsafe {
            GetLastError()
        }));
    }
    unsafe { CloseHandle(process_info.hThread) };
    let mut writer = RunnerWriter::new(output);
    writer.write(RunnerMessage::Started(process_info.dwProcessId))?;

    let control_rx = if request.interactive {
        let (control_tx, control_rx) = mpsc::channel();
        thread::spawn(move || {
            let mut reader = reader;
            let mut line = Vec::new();
            loop {
                line.clear();
                match reader.read_until(b'\n', &mut line) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(_) => break,
                }
                if line.len() > MAX_RESPONSE_FRAME_BYTES {
                    break;
                }
                let Ok(control) = serde_json::from_slice::<RunnerControl>(&line) else {
                    break;
                };
                let close = matches!(control, RunnerControl::Close);
                if control_tx.send(control).is_err() {
                    break;
                }
                if close {
                    break;
                }
            }
        });
        Some(control_rx)
    } else {
        None
    };

    if let Some(control_rx) = control_rx {
        thread::spawn(move || {
            let mut stdin = stdin;
            for control in control_rx {
                match control {
                    RunnerControl::Write { data_b64 } => {
                        let Ok(data) = BASE64_STANDARD.decode(data_b64) else {
                            break;
                        };
                        if stdin.write_all(&data).is_err() {
                            break;
                        }
                    }
                    RunnerControl::Close => break,
                }
            }
        });
    } else if let Some(encoded) = request.stdin_b64 {
        let value = BASE64_STANDARD
            .decode(encoded)
            .map_err(|_| "invalid runner stdin payload".to_string())?;
        thread::spawn(move || {
            let _ = stdin.write_all(&value);
        });
    } else {
        drop(stdin);
    }

    let budget = Arc::new(Mutex::new(request.max_output_bytes));
    let (sender, receiver) = mpsc::sync_channel(64);
    let (failure_sender, failure_receiver) = mpsc::channel();
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
            unsafe { TerminateProcess(process_info.hProcess, 1) };
            break;
        }
        match unsafe { WaitForSingleObject(process_info.hProcess, 10) } {
            WAIT_OBJECT_0 => break,
            WAIT_TIMEOUT => {}
            _ => {
                stream_failure = Some("cannot wait for restricted child".to_string());
                unsafe { TerminateProcess(process_info.hProcess, 1) };
                break;
            }
        }
    }
    unsafe { WaitForSingleObject(process_info.hProcess, INFINITE) };
    let mut exit_code = 0_u32;
    unsafe {
        GetExitCodeProcess(process_info.hProcess, &mut exit_code);
        CloseHandle(process_info.hProcess);
    }
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

fn host_home() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
}

struct StagedHome(PathBuf);

impl Drop for StagedHome {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn stage_home_files(files: &BTreeMap<String, String>) -> Result<Option<StagedHome>, String> {
    if files.is_empty() {
        return Ok(None);
    }
    let mut suffix = [0_u8; 16];
    rand::thread_rng().fill_bytes(&mut suffix);
    let name = suffix
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let root = std::env::temp_dir().join(format!("ace-sandbox-home-files-{name}"));
    fs::create_dir(&root).map_err(|error| format!("cannot create projected HOME: {error}"))?;
    for (relative_path, encoded) in files {
        let components: Vec<&str> = relative_path.split('/').collect();
        if relative_path.is_empty()
            || relative_path.starts_with('/')
            || relative_path.contains('\\')
            || relative_path.contains(':')
            || components
                .iter()
                .any(|part| part.is_empty() || *part == "." || *part == "..")
        {
            let _ = fs::remove_dir_all(&root);
            return Err("projected HOME path must be relative".to_string());
        }
        let destination = root.join(relative_path);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("cannot create projected HOME directory: {error}"))?;
        }
        let content = BASE64_STANDARD
            .decode(encoded)
            .map_err(|_| "invalid projected HOME file encoding".to_string())?;
        fs::write(&destination, content)
            .map_err(|error| format!("cannot stage projected HOME file: {error}"))?;
    }
    Ok(Some(StagedHome(root)))
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
    env_overrides: BTreeMap<String, String>,
) -> BTreeMap<String, String> {
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
    result.extend(env_overrides);
    if network_enabled {
        result.extend(crate::network::managed_proxy_environment(
            "http://127.0.0.1:43119",
        ));
    }
    result
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
