mod profile;

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use rand::rngs::OsRng;
use rand::RngCore;

use crate::protocol::{
    RuntimeCapabilities, RuntimeMessage, StdioInputMessage, MAX_OUTPUT_CHUNK_BYTES,
};
use profile::{
    build_environment, build_invocation, build_probe_invocation, compile_profile,
    validate_requested_capabilities, NetworkAccess, ProfileInput, SeatbeltInvocation,
    SANDBOX_EXECUTABLE, SANDBOX_PROBE_EXECUTABLE,
};

const PRIVATE_TEMP_ROOT: &str = "/private/tmp";
static ACTIVE_PROCESS_GROUP: AtomicI32 = AtomicI32::new(0);

fn set_child_resource_limits() -> io::Result<()> {
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
        // structure is fully initialized and points to process-local memory.
        if unsafe { libc::setrlimit(resource as _, &limit) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}
static SIGNAL_SETUP: OnceLock<Result<(), String>> = OnceLock::new();

pub struct MacOsRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub stdin_stream: Option<Receiver<StdioInputMessage>>,
    pub env_overrides: BTreeMap<String, String>,
}

pub struct MacOsRuntimeError {
    pub code: &'static str,
    pub message: String,
}

struct SandboxPlan {
    invocation: SeatbeltInvocation,
    probe_invocation: SeatbeltInvocation,
    cwd: PathBuf,
    home: PrivateHome,
    environment: BTreeMap<String, String>,
}

struct PreparedRequest {
    command: Vec<String>,
    cwd: PathBuf,
    writable_roots: Vec<PathBuf>,
    readable_roots: Vec<PathBuf>,
    denied_roots: Vec<PathBuf>,
    home: PrivateHome,
}

struct PrivateHome {
    path: PathBuf,
    temp: PathBuf,
    cleaned: bool,
}

pub fn run(
    request: MacOsRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), MacOsRuntimeError> {
    install_signal_cleanup().map_err(unavailable)?;
    verify_pinned_system_executable(Path::new(SANDBOX_EXECUTABLE)).map_err(unavailable)?;
    verify_pinned_system_executable(Path::new(SANDBOX_PROBE_EXECUTABLE)).map_err(unavailable)?;
    let network_protocols = request
        .network_rules
        .iter()
        .map(|rule| rule.protocol.clone())
        .collect::<Vec<_>>();
    validate_requested_capabilities(
        request.network_enabled,
        request.allow_local_binding,
        &network_protocols,
    )
    .map_err(denied)?;
    let policy =
        crate::network::NetworkPolicy::new(request.network_rules.clone()).map_err(network_error)?;
    let prepared = prepare_request(&request).map_err(denied)?;
    let proxy = if request.network_enabled {
        Some(crate::network::proxy::ProxyHandle::start(policy).map_err(network_error)?)
    } else {
        None
    };
    let proxy_endpoint = match proxy.as_ref() {
        Some(proxy) => {
            let address = proxy.address();
            if address.ip() != IpAddr::V4(Ipv4Addr::LOCALHOST) || address.port() == 0 {
                return Err(unavailable(
                    "managed proxy did not bind an exact IPv4 loopback endpoint",
                ));
            }
            Some((address.port(), proxy.proxy_url(address)))
        }
        None => None,
    };
    let mut plan = build_plan(prepared, proxy_endpoint, &request.env_overrides).map_err(denied)?;
    verify_profile(&plan).map_err(unavailable)?;

    let mut command = Command::new(plan.invocation.executable);
    command.args(&plan.invocation.arguments);
    command
        .current_dir(&plan.cwd)
        .env_clear()
        .envs(&plan.environment)
        .stdin(
            if request.stdin.is_some() || request.stdin_stream.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            },
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.process_group(0);
    // SAFETY: the closure performs only async-signal-safe setrlimit calls.
    unsafe {
        command.pre_exec(set_child_resource_limits);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = plan.home.cleanup();
            return Err(unavailable(format!(
                "failed to start macOS Seatbelt: {error}"
            )));
        }
    };
    let group = match i32::try_from(child.id()) {
        Ok(group) if group > 0 => group,
        _ => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = plan.home.cleanup();
            return Err(unavailable(
                "sandbox process id cannot identify a process group",
            ));
        }
    };
    if ACTIVE_PROCESS_GROUP
        .compare_exchange(0, group, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        let _ = terminate_process_group(child.id());
        let _ = child.kill();
        let _ = child.wait();
        let _ = plan.home.cleanup();
        return Err(unavailable("another macOS sandbox process group is active"));
    }
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            let _ = terminate_child_tree(&mut child);
            let _ = plan.home.cleanup();
            return Err(unavailable("macOS sandbox stdout pipe is unavailable"));
        }
    };
    let stderr = match child.stderr.take() {
        Some(stderr) => stderr,
        None => {
            drop(stdout);
            let _ = terminate_child_tree(&mut child);
            let _ = plan.home.cleanup();
            return Err(unavailable("macOS sandbox stderr pipe is unavailable"));
        }
    };
    let child_stdin = match request.stdin.is_some() || request.stdin_stream.is_some() {
        true => match child.stdin.take() {
            Some(stdin) => Some(stdin),
            None => {
                drop(stdout);
                drop(stderr);
                let _ = terminate_child_tree(&mut child);
                let _ = plan.home.cleanup();
                return Err(unavailable("macOS sandbox stdin pipe is unavailable"));
            }
        },
        false => None,
    };
    if sender
        .send(RuntimeMessage::Started {
            pid: Some(child.id()),
            capabilities: RuntimeCapabilities {
                backend: "macos_seatbelt",
                filesystem_sandbox: true,
                process_tree_cleanup: true,
                managed_network: request.network_enabled,
                system_bwrap: false,
                bundled_bwrap: false,
                wsl_version: None,
                local_binding_control: false,
                explicit_handle_inheritance: false,
                windows_restricted_token: false,
                windows_acl: false,
                windows_job: false,
                windows_wfp: false,
            },
        })
        .is_err()
    {
        let termination = terminate_child_tree(&mut child);
        let cleanup = plan.home.cleanup();
        if let Err(error) = termination {
            return Err(unavailable(error));
        }
        cleanup.map_err(unavailable)?;
        return Err(unavailable("protocol receiver disconnected"));
    }

    let budget = Arc::new(Mutex::new(request.max_output_bytes));
    let (failure_sender, failure_receiver) = mpsc::channel();
    let input_finished = Arc::new(AtomicBool::new(false));
    let stdin_writer = match child_stdin {
        Some(child_stdin) => Some(spawn_stdin_writer(
            child_stdin,
            request.stdin,
            request.stdin_stream,
            Arc::clone(&input_finished),
            failure_sender.clone(),
        )),
        None => None,
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
            if let Err(error) = terminate_child_tree(&mut child) {
                input_finished.store(true, Ordering::Release);
                let _ = plan.home.cleanup();
                return Err(unavailable(error));
            }
            input_finished.store(true, Ordering::Release);
            let stdout_join = stdout_reader.join();
            let stderr_join = stderr_reader.join();
            if let Some(writer) = stdin_writer {
                let _ = writer.join();
            }
            if stdout_join.is_err() || stderr_join.is_err() {
                let _ = plan.home.cleanup();
                return Err(unavailable("sandbox output reader terminated unexpectedly"));
            }
            plan.home.cleanup().map_err(unavailable)?;
            return Err(failure.into_error());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(error) => {
                if let Err(termination_error) = terminate_child_tree(&mut child) {
                    let _ = plan.home.cleanup();
                    return Err(unavailable(termination_error));
                }
                let stdout_join = stdout_reader.join();
                let stderr_join = stderr_reader.join();
                let cleanup = plan.home.cleanup();
                if stdout_join.is_err() || stderr_join.is_err() {
                    return Err(unavailable("sandbox output reader terminated unexpectedly"));
                }
                cleanup.map_err(unavailable)?;
                return Err(unavailable(format!(
                    "cannot wait for Seatbelt command: {error}"
                )));
            }
        }
    };
    input_finished.store(true, Ordering::Release);
    if let Err(error) = terminate_process_group(child.id()) {
        let cleanup = plan.home.cleanup();
        cleanup.map_err(unavailable)?;
        return Err(unavailable(error));
    }
    let stdout_join = stdout_reader.join();
    let stderr_join = stderr_reader.join();
    if let Some(writer) = stdin_writer {
        let _ = writer.join();
    }
    if stdout_join.is_err() || stderr_join.is_err() {
        let _ = plan.home.cleanup();
        return Err(unavailable("sandbox output reader terminated unexpectedly"));
    }
    plan.home.cleanup().map_err(unavailable)?;
    if let Ok(failure) = failure_receiver.try_recv() {
        return Err(failure.into_error());
    }
    sender
        .send(RuntimeMessage::Completed(status.code().unwrap_or(-1)))
        .map_err(|_| unavailable("protocol receiver disconnected"))
}

fn terminate_child_tree(child: &mut Child) -> Result<(), String> {
    let group_result = terminate_process_group(child.id());
    let kill_result = child.kill();
    let wait_result = child.wait();
    group_result?;
    if let Err(error) = kill_result {
        if error.kind() != std::io::ErrorKind::InvalidInput
            && error.raw_os_error() != Some(libc::ESRCH)
        {
            return Err(format!("cannot terminate Seatbelt command: {error}"));
        }
    }
    wait_result
        .map(|_| ())
        .map_err(|error| format!("cannot reap Seatbelt command: {error}"))
}

fn terminate_process_group(pid: u32) -> Result<(), String> {
    let group = i32::try_from(pid)
        .map_err(|_| "sandbox process id cannot identify a process group".to_string())?;
    let result = unsafe { libc::kill(-group, libc::SIGKILL) };
    let error = std::io::Error::last_os_error();
    if result == 0 || error.raw_os_error() == Some(libc::ESRCH) {
        ACTIVE_PROCESS_GROUP
            .compare_exchange(group, 0, Ordering::AcqRel, Ordering::Acquire)
            .ok();
        Ok(())
    } else {
        Err(format!("cannot terminate sandbox process group: {error}"))
    }
}

fn install_signal_cleanup() -> Result<(), String> {
    SIGNAL_SETUP.get_or_init(configure_signal_cleanup).clone()
}

fn configure_signal_cleanup() -> Result<(), String> {
    let handler = terminate_signal as *const () as libc::sighandler_t;
    let previous_term = unsafe { libc::signal(libc::SIGTERM, handler) };
    if previous_term == libc::SIG_ERR {
        return Err(format!(
            "cannot install SIGTERM cleanup: {}",
            std::io::Error::last_os_error()
        ));
    }
    let previous_int = unsafe { libc::signal(libc::SIGINT, handler) };
    if previous_int == libc::SIG_ERR {
        unsafe {
            libc::signal(libc::SIGTERM, previous_term);
        }
        return Err(format!(
            "cannot install SIGINT cleanup: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

extern "C" fn terminate_signal(signal: libc::c_int) {
    let group = ACTIVE_PROCESS_GROUP.swap(0, Ordering::AcqRel);
    if group > 0 {
        unsafe {
            libc::kill(-group, libc::SIGKILL);
        }
    }
    unsafe {
        libc::_exit(128 + signal);
    }
}

fn prepare_request(request: &MacOsRunRequest) -> Result<PreparedRequest, String> {
    let mut command = request.command.clone();
    let executable = command
        .first()
        .ok_or_else(|| "empty command".to_string())
        .map(PathBuf::from)?;
    let executable = canonical_executable(&executable)?;
    command[0] = path_string(&executable)?;

    let cwd = canonical_directory(&request.cwd, "sandbox cwd")?;
    let writable_roots = canonical_roots(&request.writable_roots, "writable root")?;
    let readable_roots = canonical_roots(&request.readable_roots, "readable root")?;
    let denied_roots = canonical_or_missing_roots(&request.denied_roots)?;
    if !writable_roots
        .iter()
        .chain(readable_roots.iter())
        .any(|root| path_is_within(&cwd, root))
    {
        return Err("sandbox cwd must be inside an explicit readable or writable root".to_string());
    }
    if denied_roots.iter().any(|root| path_is_within(&cwd, root)) {
        return Err("sandbox cwd overlaps an explicit denied root".to_string());
    }
    if denied_roots
        .iter()
        .any(|root| path_is_within(&executable, root))
    {
        return Err("command executable overlaps an explicit denied root".to_string());
    }

    let home = PrivateHome::create()?;
    if denied_roots
        .iter()
        .any(|root| path_is_within(&home.path, root))
    {
        return Err("private HOME overlaps an explicit denied root".to_string());
    }
    Ok(PreparedRequest {
        command,
        cwd,
        writable_roots,
        readable_roots,
        denied_roots,
        home,
    })
}

fn build_plan(
    prepared: PreparedRequest,
    proxy_endpoint: Option<(u16, String)>,
    env_overrides: &BTreeMap<String, String>,
) -> Result<SandboxPlan, String> {
    let home = path_string(&prepared.home.path)?;
    let temp = path_string(&prepared.home.temp)?;
    let proxy_port = proxy_endpoint.as_ref().map(|(port, _url)| *port);
    let mut environment = build_environment(&home, &temp, proxy_port, env_overrides)?;
    if let Some((_port, proxy_url)) = &proxy_endpoint {
        for name in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ] {
            environment.insert(name.to_string(), proxy_url.clone());
        }
    }
    let profile = compile_profile(ProfileInput {
        readable_roots: paths_to_strings(&prepared.readable_roots)?,
        writable_roots: paths_to_strings(&prepared.writable_roots)?,
        denied_roots: paths_to_strings(&prepared.denied_roots)?,
        private_home: home,
        command_executable: prepared.command[0].clone(),
        network: match proxy_port {
            Some(port) => NetworkAccess::ManagedProxy { port },
            None => NetworkAccess::Denied,
        },
    })?;
    let invocation = build_invocation(&profile, &prepared.command)?;
    let probe_invocation = build_probe_invocation(&profile);
    Ok(SandboxPlan {
        invocation,
        probe_invocation,
        cwd: prepared.cwd,
        home: prepared.home,
        environment,
    })
}

fn verify_profile(plan: &SandboxPlan) -> Result<(), String> {
    let status = Command::new(plan.probe_invocation.executable)
        .args(&plan.probe_invocation.arguments)
        .current_dir(&plan.cwd)
        .env_clear()
        .envs(&plan.environment)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("cannot validate macOS Seatbelt profile: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "macOS Seatbelt profile validation failed with status {}",
            status.code().unwrap_or(-1)
        ))
    }
}

fn verify_pinned_system_executable(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err(format!(
            "system helper path is not absolute: {}",
            path.display()
        ));
    }
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect system helper {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "system helper is not a pinned regular file: {}",
            path.display()
        ));
    }
    if metadata.uid() != 0 || metadata.mode() & 0o022 != 0 || metadata.mode() & 0o111 == 0 {
        return Err(format!(
            "system helper has unsafe ownership or permissions: {}",
            path.display()
        ));
    }
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve system helper {}: {error}", path.display()))?;
    if canonical != path {
        return Err(format!(
            "system helper resolved away from its pinned path: {}",
            path.display()
        ));
    }
    Ok(())
}

fn canonical_executable(path: &Path) -> Result<PathBuf, String> {
    validate_unambiguous_absolute_path(path, "command executable")?;
    let canonical = path.canonicalize().map_err(|error| {
        format!(
            "cannot resolve command executable {}: {error}",
            path.display()
        )
    })?;
    let metadata = canonical.metadata().map_err(|error| {
        format!(
            "cannot inspect command executable {}: {error}",
            canonical.display()
        )
    })?;
    if !metadata.is_file() || metadata.mode() & 0o111 == 0 {
        return Err(format!(
            "command executable is not an executable regular file: {}",
            canonical.display()
        ));
    }
    Ok(canonical)
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, String> {
    validate_unambiguous_absolute_path(path, label)?;
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve {label} {}: {error}", path.display()))?;
    if !canonical.is_dir() {
        return Err(format!(
            "{label} is not a directory: {}",
            canonical.display()
        ));
    }
    Ok(canonical)
}

fn canonical_roots(paths: &[PathBuf], label: &str) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::new();
    for path in paths {
        let canonical = canonical_directory(path, label)?;
        if !result.contains(&canonical) {
            result.push(canonical);
        }
    }
    Ok(result)
}

fn canonical_or_missing_roots(paths: &[PathBuf]) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::new();
    for path in paths {
        validate_unambiguous_absolute_path(path, "denied root")?;
        let canonical = canonicalize_allow_missing(path)?;
        if !result.contains(&canonical) {
            result.push(canonical);
        }
    }
    Ok(result)
}

fn canonicalize_allow_missing(path: &Path) -> Result<PathBuf, String> {
    let mut cursor = path;
    let mut missing = Vec::new();
    loop {
        match cursor.canonicalize() {
            Ok(mut canonical) => {
                for component in missing.iter().rev() {
                    canonical.push(component);
                }
                return Ok(canonical);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                match fs::symlink_metadata(cursor) {
                    Ok(_) => {
                        return Err(format!(
                            "denied root contains an unresolvable filesystem entry: {}",
                            cursor.display()
                        ));
                    }
                    Err(metadata_error)
                        if metadata_error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(metadata_error) => {
                        return Err(format!(
                            "cannot inspect denied root {}: {metadata_error}",
                            cursor.display()
                        ));
                    }
                }
                let name = cursor
                    .file_name()
                    .ok_or_else(|| format!("cannot resolve denied root {}", path.display()))?;
                missing.push(name.to_os_string());
                cursor = cursor
                    .parent()
                    .ok_or_else(|| format!("cannot resolve denied root {}", path.display()))?;
            }
            Err(error) => {
                return Err(format!(
                    "cannot resolve denied root {}: {error}",
                    path.display()
                ));
            }
        }
    }
}

fn validate_unambiguous_absolute_path(path: &Path, label: &str) -> Result<(), String> {
    if !path.is_absolute() {
        return Err(format!("{label} must be absolute: {}", path.display()));
    }
    if path
        .components()
        .any(|component| matches!(component, Component::ParentDir | Component::CurDir))
    {
        return Err(format!(
            "{label} cannot contain '.' or '..': {}",
            path.display()
        ));
    }
    Ok(())
}

fn path_is_within(path: &Path, root: &Path) -> bool {
    path.starts_with(root)
}

fn paths_to_strings(paths: &[PathBuf]) -> Result<Vec<String>, String> {
    paths.iter().map(|path| path_string(path)).collect()
}

fn path_string(path: &Path) -> Result<String, String> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| format!("sandbox path is not valid UTF-8: {}", path.display()))
}

impl PrivateHome {
    fn create() -> Result<Self, String> {
        let base = canonical_directory(Path::new(PRIVATE_TEMP_ROOT), "private HOME base")?;
        if base != Path::new(PRIVATE_TEMP_ROOT) {
            return Err("private HOME base resolved away from /private/tmp".to_string());
        }
        for _ in 0..16 {
            let mut random = [0_u8; 24];
            OsRng
                .try_fill_bytes(&mut random)
                .map_err(|error| format!("cannot generate private HOME name: {error}"))?;
            let suffix = random
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            let path = base.join(format!("ace-sandbox-home-{suffix}"));
            let create = fs::DirBuilder::new().mode(0o700).create(&path);
            match create {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    return Err(format!("cannot create private sandbox HOME: {error}"));
                }
            }
            if let Err(error) = fs::set_permissions(&path, fs::Permissions::from_mode(0o700)) {
                let _ = fs::remove_dir(&path);
                return Err(format!("cannot secure private sandbox HOME: {error}"));
            }
            let canonical = match path.canonicalize() {
                Ok(canonical) if canonical == path => canonical,
                Ok(_) => {
                    let _ = fs::remove_dir(&path);
                    return Err("private sandbox HOME resolved to an unexpected path".to_string());
                }
                Err(error) => {
                    let _ = fs::remove_dir(&path);
                    return Err(format!("cannot resolve private sandbox HOME: {error}"));
                }
            };
            if let Err(error) = verify_private_directory(&canonical) {
                let _ = fs::remove_dir_all(&canonical);
                return Err(error);
            }
            let temp = canonical.join("tmp");
            if let Err(error) = fs::DirBuilder::new().mode(0o700).create(&temp) {
                let _ = fs::remove_dir_all(&canonical);
                return Err(format!("cannot create private sandbox TMPDIR: {error}"));
            }
            if let Err(error) = fs::set_permissions(&temp, fs::Permissions::from_mode(0o700)) {
                let _ = fs::remove_dir_all(&canonical);
                return Err(format!("cannot secure private sandbox TMPDIR: {error}"));
            }
            if let Err(error) = verify_private_directory(&temp) {
                let _ = fs::remove_dir_all(&canonical);
                return Err(error);
            }
            return Ok(Self {
                path: canonical,
                temp,
                cleaned: false,
            });
        }
        Err("cannot allocate a unique private sandbox HOME".to_string())
    }

    fn cleanup(&mut self) -> Result<(), String> {
        if self.cleaned {
            return Ok(());
        }
        if self.path.parent() != Some(Path::new(PRIVATE_TEMP_ROOT))
            || !self
                .path
                .file_name()
                .is_some_and(|name| name.to_string_lossy().starts_with("ace-sandbox-home-"))
        {
            return Err("refusing to clean an invalid private sandbox HOME path".to_string());
        }
        match fs::symlink_metadata(&self.path) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                fs::remove_file(&self.path)
                    .map_err(|error| format!("cannot remove private sandbox HOME: {error}"))?;
            }
            Ok(_) => {
                make_tree_removable(&self.path)?;
                fs::remove_dir_all(&self.path)
                    .map_err(|error| format!("cannot remove private sandbox HOME: {error}"))?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(format!("cannot inspect private sandbox HOME: {error}"));
            }
        }
        self.cleaned = true;
        Ok(())
    }
}

impl Drop for PrivateHome {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn verify_private_directory(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect private sandbox directory: {error}"))?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o777 != 0o700
    {
        return Err(format!(
            "private sandbox directory has unsafe metadata: {}",
            path.display()
        ));
    }
    Ok(())
}

fn make_tree_removable(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect private sandbox entry: {error}"))?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    if metadata.is_dir() {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot secure private sandbox entry for cleanup: {error}"))?;
        let entries = fs::read_dir(path)
            .map_err(|error| format!("cannot enumerate private sandbox HOME: {error}"))?;
        for entry in entries {
            let entry =
                entry.map_err(|error| format!("cannot enumerate private sandbox HOME: {error}"))?;
            make_tree_removable(&entry.path())?;
        }
    }
    Ok(())
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
    fn into_error(self) -> MacOsRuntimeError {
        match self {
            Self::OutputTruncated => MacOsRuntimeError {
                code: "output_truncated",
                message: "sandbox output exceeded the configured limit".to_string(),
            },
            Self::ReadFailed => unavailable("cannot read sandbox output"),
            Self::ReceiverDisconnected => unavailable("protocol receiver disconnected"),
            Self::StdinFailed => MacOsRuntimeError {
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

fn unavailable(message: impl Into<String>) -> MacOsRuntimeError {
    MacOsRuntimeError {
        code: "sandbox_unavailable",
        message: message.into(),
    }
}

fn denied(message: impl Into<String>) -> MacOsRuntimeError {
    MacOsRuntimeError {
        code: "sandbox_denied",
        message: message.into(),
    }
}

fn network_error(error: crate::network::policy::NetworkError) -> MacOsRuntimeError {
    MacOsRuntimeError {
        code: error.code.as_str(),
        message: error.message,
    }
}
