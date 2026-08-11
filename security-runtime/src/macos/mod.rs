use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rand::RngCore;

use crate::protocol::{RuntimeCapabilities, RuntimeMessage, MAX_OUTPUT_CHUNK_BYTES};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const PROTECTED_NAMES: &[&str] = &[".git", ".agents", ".crew"];
static ACTIVE_PROCESS_GROUP: AtomicI32 = AtomicI32::new(0);
const PLATFORM_READ_ROOTS: &[&str] = &[
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "/private/etc",
    "/private/var/db",
    "/private/var/select",
    "/dev",
];

pub struct MacOsRunRequest {
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub readonly_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
    pub network_enabled: bool,
    pub network_rules: Vec<crate::protocol::NetworkRule>,
    pub allow_local_binding: bool,
    pub max_output_bytes: usize,
    pub stdin: Option<Vec<u8>>,
    pub env_overrides: BTreeMap<String, String>,
}

pub struct MacOsRuntimeError {
    pub code: &'static str,
    pub message: String,
}

struct SandboxPlan {
    profile: String,
    parameters: Vec<(String, String)>,
    cwd: PathBuf,
    home: PathBuf,
}

pub fn run(
    request: MacOsRunRequest,
    sender: &SyncSender<RuntimeMessage>,
) -> Result<(), MacOsRuntimeError> {
    install_signal_cleanup();
    if !Path::new(SANDBOX_EXEC).is_file() {
        return Err(unavailable("macOS Seatbelt launcher is unavailable"));
    }
    let policy =
        crate::network::NetworkPolicy::new(request.network_rules.clone()).map_err(network_error)?;
    let proxy = if request.network_enabled {
        Some(crate::network::proxy::ProxyHandle::start(policy).map_err(network_error)?)
    } else {
        None
    };
    let proxy_address = proxy.as_ref().map(|value| value.address());
    let plan = build_plan(&request, proxy_address.map(|value| value.port())).map_err(denied)?;

    let mut command = Command::new(SANDBOX_EXEC);
    command.arg("-p").arg(&plan.profile);
    for (name, value) in &plan.parameters {
        command.arg(format!("-D{name}={value}"));
    }
    command.args(&request.command);
    command
        .current_dir(&plan.cwd)
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .env("HOME", &plan.home)
        .env("TMPDIR", &plan.home)
        .env("ACE_SANDBOX", "macos-seatbelt")
        .envs(&request.env_overrides)
        .stdin(if request.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.process_group(0);
    if let Some(address) = proxy_address {
        let proxy_url = format!("http://{address}");
        command
            .env("HTTP_PROXY", &proxy_url)
            .env("HTTPS_PROXY", &proxy_url)
            .env("ALL_PROXY", &proxy_url)
            .env("NO_PROXY", "");
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            cleanup_home(&plan.home);
            return Err(unavailable(format!(
                "failed to start macOS Seatbelt: {error}"
            )));
        }
    };
    if let Ok(group) = i32::try_from(child.id()) {
        ACTIVE_PROCESS_GROUP.store(group, Ordering::Release);
    }
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
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
                local_binding_control: true,
                explicit_handle_inheritance: false,
                windows_restricted_token: false,
                windows_acl: false,
                windows_job: false,
                windows_wfp: false,
            },
        })
        .is_err()
    {
        terminate_child_tree(&mut child);
        cleanup_home(&plan.home);
        return Err(unavailable("protocol receiver disconnected"));
    }

    if let Some(stdin) = request.stdin {
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
            terminate_child_tree(&mut child);
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            cleanup_home(&plan.home);
            return Err(failure.into_error());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(error) => {
                terminate_child_tree(&mut child);
                cleanup_home(&plan.home);
                return Err(unavailable(format!(
                    "cannot wait for Seatbelt command: {error}"
                )));
            }
        }
    };
    terminate_process_group(child.id());
    let _ = stdout_reader.join();
    let _ = stderr_reader.join();
    cleanup_home(&plan.home);
    if let Ok(failure) = failure_receiver.try_recv() {
        return Err(failure.into_error());
    }
    sender
        .send(RuntimeMessage::Completed(status.code().unwrap_or(-1)))
        .map_err(|_| unavailable("protocol receiver disconnected"))
}

fn terminate_child_tree(child: &mut std::process::Child) {
    terminate_process_group(child.id());
    let _ = child.kill();
    let _ = child.wait();
}

fn terminate_process_group(pid: u32) {
    if let Ok(group) = i32::try_from(pid) {
        ACTIVE_PROCESS_GROUP
            .compare_exchange(group, 0, Ordering::AcqRel, Ordering::Acquire)
            .ok();
        unsafe {
            libc::kill(-group, libc::SIGKILL);
        }
    }
}

fn install_signal_cleanup() {
    unsafe {
        libc::signal(
            libc::SIGTERM,
            terminate_signal as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGINT,
            terminate_signal as *const () as libc::sighandler_t,
        );
    }
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

fn build_plan(request: &MacOsRunRequest, proxy_port: Option<u16>) -> Result<SandboxPlan, String> {
    if request.command.is_empty() {
        return Err("empty command".to_string());
    }
    let cwd = canonical_directory(&request.cwd)?;
    let writable = canonical_roots(&request.writable_roots)?;
    if !writable.iter().any(|root| cwd.starts_with(root)) {
        return Err("sandbox cwd must be inside an explicit writable root".to_string());
    }
    let readable = canonical_roots(&request.readable_roots)?;
    let readonly = canonical_readonly_roots(&request.readonly_roots, &writable)?;
    let denied = canonical_or_missing_roots(&request.denied_roots)?;
    let home = create_private_home()?;

    let mut parameters = Vec::new();
    let mut read_rules = Vec::new();
    for (index, root) in PLATFORM_READ_ROOTS.iter().enumerate() {
        push_subpath_rule(
            &mut parameters,
            &mut read_rules,
            "SYSTEM_READ",
            index,
            root,
            "allow file-read*",
        );
    }
    for (index, root) in readable.iter().enumerate() {
        push_subpath_rule(
            &mut parameters,
            &mut read_rules,
            "READABLE_ROOT",
            index,
            &path_string(root)?,
            "allow file-read*",
        );
    }
    let mut write_rules = Vec::new();
    for (index, root) in writable.iter().enumerate() {
        let value = path_string(root)?;
        push_subpath_rule(
            &mut parameters,
            &mut read_rules,
            "WRITABLE_READ_ROOT",
            index,
            &value,
            "allow file-read*",
        );
        push_subpath_rule(
            &mut parameters,
            &mut write_rules,
            "WRITABLE_ROOT",
            index,
            &value,
            "allow file-write*",
        );
    }
    push_subpath_rule(
        &mut parameters,
        &mut read_rules,
        "PRIVATE_HOME",
        0,
        &path_string(&home)?,
        "allow file-read*",
    );
    push_subpath_rule(
        &mut parameters,
        &mut write_rules,
        "PRIVATE_HOME_WRITE",
        0,
        &path_string(&home)?,
        "allow file-write*",
    );
    let mut traversal_roots = readable.clone();
    traversal_roots.extend(writable.iter().cloned());
    traversal_roots.push(home.clone());
    push_ancestor_metadata_rules(&mut parameters, &mut read_rules, &traversal_roots)?;

    if let Some(executable) = request.command.first().map(PathBuf::from) {
        if executable.is_absolute() {
            let executable = executable.canonicalize().map_err(|error| {
                format!(
                    "cannot resolve command executable {}: {error}",
                    executable.display()
                )
            })?;
            let value = path_string(&executable)?;
            parameters.push(("COMMAND_EXECUTABLE".to_string(), value));
            read_rules
                .push("(allow file-read* (literal (param \"COMMAND_EXECUTABLE\")))".to_string());
        }
    }

    let mut deny_rules = Vec::new();
    for (index, root) in readonly.iter().enumerate() {
        push_path_rule(
            &mut parameters,
            &mut deny_rules,
            "READONLY_ROOT",
            index,
            &path_string(root)?,
            "deny file-write*",
        );
    }
    for (index, root) in denied.iter().enumerate() {
        let value = path_string(root)?;
        push_path_rule(
            &mut parameters,
            &mut deny_rules,
            "DENIED_READ_ROOT",
            index,
            &value,
            "deny file-read*",
        );
        push_path_rule(
            &mut parameters,
            &mut deny_rules,
            "DENIED_WRITE_ROOT",
            index,
            &value,
            "deny file-write*",
        );
    }

    let network_rule = match proxy_port {
        Some(port) => format!(
            "(allow network-outbound (remote ip \"localhost:{port}\"))\n{}",
            if request.allow_local_binding {
                "(allow network-bind (local ip \"localhost:*\"))"
            } else {
                ""
            }
        ),
        None if request.allow_local_binding => {
            "(allow network-bind (local ip \"localhost:*\"))".to_string()
        }
        None => String::new(),
    };
    let profile = format!(
        "(version 1)\n(deny default)\n(import \"system.sb\")\n\
         (allow process*)\n(allow sysctl-read)\n(allow signal (target self))\n\
         {}\n{}\n{}\n{}\n",
        read_rules.join("\n"),
        write_rules.join("\n"),
        deny_rules.join("\n"),
        network_rule,
    );
    Ok(SandboxPlan {
        profile,
        parameters,
        cwd,
        home,
    })
}

fn push_subpath_rule(
    parameters: &mut Vec<(String, String)>,
    rules: &mut Vec<String>,
    prefix: &str,
    index: usize,
    value: &str,
    operation: &str,
) {
    let name = format!("{prefix}_{index}");
    parameters.push((name.clone(), value.to_string()));
    rules.push(format!("({operation} (subpath (param \"{name}\")))"));
}

fn push_ancestor_metadata_rules(
    parameters: &mut Vec<(String, String)>,
    rules: &mut Vec<String>,
    roots: &[PathBuf],
) -> Result<(), String> {
    let mut seen = BTreeSet::new();
    for root in roots {
        for ancestor in root
            .ancestors()
            .skip(1)
            .filter(|value| value.parent().is_some())
        {
            if !seen.insert(ancestor.to_path_buf()) {
                continue;
            }
            let name = format!("READABLE_ANCESTOR_{}", seen.len() - 1);
            parameters.push((name.clone(), path_string(ancestor)?));
            rules.push(format!(
                "(allow file-read-metadata (literal (param \"{name}\")))"
            ));
        }
    }
    Ok(())
}

fn push_path_rule(
    parameters: &mut Vec<(String, String)>,
    rules: &mut Vec<String>,
    prefix: &str,
    index: usize,
    value: &str,
    operation: &str,
) {
    let name = format!("{prefix}_{index}");
    parameters.push((name.clone(), value.to_string()));
    rules.push(format!("({operation} (literal (param \"{name}\")))"));
    rules.push(format!("({operation} (subpath (param \"{name}\")))"));
}

fn create_private_home() -> Result<PathBuf, String> {
    let mut random = [0_u8; 16];
    rand::thread_rng().fill_bytes(&mut random);
    let suffix = random
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let home = std::env::temp_dir().join(format!("ace-sandbox-home-{suffix}"));
    fs::create_dir(&home)
        .map_err(|error| format!("cannot create private sandbox home: {error}"))?;
    Ok(home.canonicalize().unwrap_or(home))
}

fn cleanup_home(home: &Path) {
    let _ = fs::remove_dir_all(home);
}

fn canonical_directory(path: &Path) -> Result<PathBuf, String> {
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve sandbox cwd {}: {error}", path.display()))?;
    if !canonical.is_dir() {
        return Err("sandbox cwd is not a directory".to_string());
    }
    Ok(canonical)
}

fn canonical_roots(paths: &[PathBuf]) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::new();
    for path in paths {
        let canonical = path.canonicalize().map_err(|error| {
            format!("cannot resolve permission root {}: {error}", path.display())
        })?;
        if !result.contains(&canonical) {
            result.push(canonical);
        }
    }
    Ok(result)
}

fn canonical_or_missing_roots(paths: &[PathBuf]) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::new();
    for path in paths {
        if !path.is_absolute() {
            return Err(format!(
                "permission root must be absolute: {}",
                path.display()
            ));
        }
        if path
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
        {
            return Err(format!(
                "permission root cannot contain '..': {}",
                path.display()
            ));
        }
        let canonical = canonicalize_allow_missing(path)?;
        if !result.contains(&canonical) {
            result.push(canonical);
        }
    }
    Ok(result)
}

fn canonicalize_allow_missing(path: &Path) -> Result<PathBuf, String> {
    if path.symlink_metadata().is_ok() {
        return path.canonicalize().map_err(|error| {
            format!("cannot resolve permission root {}: {error}", path.display())
        });
    }
    let mut ancestor = path;
    let mut suffix = Vec::new();
    while ancestor.symlink_metadata().is_err() {
        let name = ancestor.file_name().ok_or_else(|| {
            format!(
                "cannot resolve permission root ancestor: {}",
                path.display()
            )
        })?;
        suffix.push(name.to_os_string());
        ancestor = ancestor.parent().ok_or_else(|| {
            format!(
                "cannot resolve permission root ancestor: {}",
                path.display()
            )
        })?;
    }
    let mut canonical = ancestor
        .canonicalize()
        .map_err(|error| format!("cannot resolve permission root {}: {error}", path.display()))?;
    for name in suffix.iter().rev() {
        canonical.push(name);
    }
    Ok(canonical)
}

fn canonical_readonly_roots(
    paths: &[PathBuf],
    writable: &[PathBuf],
) -> Result<Vec<PathBuf>, String> {
    let mut roots = Vec::new();
    for root in writable {
        for name in PROTECTED_NAMES {
            roots.push(root.join(name));
        }
    }
    for path in paths {
        if std::fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            return Err(format!(
                "read-only root cannot be a symlink: {}",
                path.display()
            ));
        }
    }
    for root in canonical_or_missing_roots(paths)? {
        if !writable.iter().any(|candidate| root.starts_with(candidate)) {
            return Err(format!(
                "read-only root must be inside an explicit writable root: {}",
                root.display()
            ));
        }
        if !roots.contains(&root) {
            roots.push(root);
        }
    }
    for root in &roots {
        if std::fs::symlink_metadata(root).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            return Err(format!(
                "read-only root cannot be a symlink: {}",
                root.display()
            ));
        }
    }
    Ok(roots)
}

fn path_string(path: &Path) -> Result<String, String> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| format!("sandbox path is not valid UTF-8: {}", path.display()))
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
    fn into_error(self) -> MacOsRuntimeError {
        match self {
            Self::OutputTruncated => MacOsRuntimeError {
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

#[cfg(test)]
mod tests {
    use super::{build_plan, MacOsRunRequest};
    use std::collections::BTreeMap;

    fn request(workspace: &std::path::Path) -> MacOsRunRequest {
        MacOsRunRequest {
            command: vec!["/bin/sh".to_string(), "-c".to_string(), "true".to_string()],
            cwd: workspace.to_path_buf(),
            writable_roots: vec![workspace.to_path_buf()],
            readable_roots: vec![],
            readonly_roots: vec![workspace.join(".agents")],
            denied_roots: vec![workspace.join(".git")],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: BTreeMap::new(),
        }
    }

    #[test]
    fn profile_uses_parameters_for_user_paths_and_denies_protected_roots() {
        let workspace = tempfile::tempdir().unwrap();
        let plan = build_plan(&request(workspace.path()), None).unwrap();
        assert!(!plan.profile.contains(workspace.path().to_str().unwrap()));
        assert!(plan.profile.contains("READONLY_ROOT_0"));
        assert!(plan.profile.contains("DENIED_READ_ROOT_0"));
        assert!(plan.profile.contains("DENIED_WRITE_ROOT_0"));
        assert!(plan.profile.contains("deny file-read*"));
        assert!(plan.profile.contains("deny file-write*"));
        assert!(plan
            .profile
            .contains("deny file-write* (literal (param \"READONLY_ROOT_0\"))"));
        assert!(!plan
            .profile
            .contains("deny file-read* (literal (param \"READONLY_ROOT_0\"))"));
        assert!(!plan.profile.contains("allow network-outbound"));
    }

    #[test]
    fn readable_roots_allow_only_ancestor_metadata_for_path_traversal() {
        let workspace = tempfile::tempdir().unwrap();
        let runtime = tempfile::tempdir().unwrap();
        let nested = runtime.path().join("node/lib");
        std::fs::create_dir_all(&nested).unwrap();
        let mut value = request(workspace.path());
        value.readable_roots = vec![nested];

        let plan = build_plan(&value, None).unwrap();

        assert!(plan
            .profile
            .contains("allow file-read-metadata (literal (param \"READABLE_ANCESTOR_0\"))"));
        assert!(!plan
            .profile
            .contains("allow file-read* (subpath (param \"READABLE_ANCESTOR_0\"))"));
    }

    #[test]
    fn readonly_root_must_be_inside_a_writable_root() {
        let workspace = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let mut value = request(workspace.path());
        value.readonly_roots = vec![outside.path().to_path_buf()];

        let error = build_plan(&value, None)
            .err()
            .expect("read-only root outside writable roots must fail");

        assert!(error.contains("read-only root must be inside"), "{error}");
    }

    #[test]
    fn network_profile_only_allows_the_managed_proxy_port() {
        let workspace = tempfile::tempdir().unwrap();
        let mut value = request(workspace.path());
        value.network_enabled = true;
        let plan = build_plan(&value, Some(43119)).unwrap();
        assert!(plan.profile.contains("localhost:43119"));
        assert!(!plan.profile.contains("allow network-inbound"));
        assert!(!plan.profile.contains("localhost:*"));
    }

    #[test]
    fn local_binding_is_explicit() {
        let workspace = tempfile::tempdir().unwrap();
        let mut value = request(workspace.path());
        value.allow_local_binding = true;
        let plan = build_plan(&value, None).unwrap();
        assert!(plan.profile.contains("allow network-bind"));
    }
}
