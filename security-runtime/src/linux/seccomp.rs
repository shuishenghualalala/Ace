use std::collections::BTreeMap;
use std::io;
use std::os::unix::process::CommandExt;
use std::process::Command;

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use seccompiler::{
    apply_filter, BpfProgram, SeccompAction, SeccompCmpArgLen, SeccompCmpOp, SeccompCondition,
    SeccompFilter, SeccompRule, TargetArch,
};

pub const INNER_SETUP_FAILURE_EXIT: i32 = 252;
pub const INNER_READY_MARKER: &[u8] = b"ACE_INNER_SANDBOX_READY\n";

struct InnerArguments {
    command: Vec<String>,
    proxy_socket: Option<String>,
    allow_local_binding: bool,
    env_overrides: BTreeMap<String, String>,
}

pub(crate) fn set_parent_death_signal() -> io::Result<()> {
    let expected_parent = unsafe { libc::getppid() };
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::getppid() } != expected_parent {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            "parent exited during PDEATHSIG setup",
        ));
    }
    Ok(())
}

/// Inner bwrap stage: tighten the actual command after mount setup has completed.
pub fn exec_inner(arguments: Vec<String>) -> ! {
    let InnerArguments {
        command,
        proxy_socket,
        allow_local_binding,
        env_overrides,
    } = match parse_arguments(arguments) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("invalid inner-stage arguments: {error}");
            std::process::exit(INNER_SETUP_FAILURE_EXIT);
        }
    };
    if command.is_empty() {
        eprintln!("inner stage received an empty command");
        std::process::exit(INNER_SETUP_FAILURE_EXIT);
    }
    if let Err(error) = set_parent_death_signal() {
        eprintln!("failed to install parent-death signal: {error}");
        std::process::exit(INNER_SETUP_FAILURE_EXIT);
    }
    let mut inner_bridge = if let Some(ref socket) = proxy_socket {
        match super::proxy_routing::start_inner_bridge(std::path::Path::new(&socket)) {
            Ok(bridge) => Some(bridge),
            Err(error) => {
                eprintln!("failed to start inner proxy bridge: {error}");
                std::process::exit(INNER_SETUP_FAILURE_EXIT);
            }
        }
    } else {
        None
    };
    if let Err(error) = install(proxy_socket.is_some(), allow_local_binding) {
        if let Some(bridge) = inner_bridge.as_mut() {
            bridge.stop();
        }
        eprintln!("failed to install no_new_privs/seccomp: {error}");
        std::process::exit(INNER_SETUP_FAILURE_EXIT);
    }
    if let Err(error) = verify_active_hardening() {
        if let Some(bridge) = inner_bridge.as_mut() {
            bridge.stop();
        }
        eprintln!("failed to verify no_new_privs/seccomp/proc: {error}");
        std::process::exit(INNER_SETUP_FAILURE_EXIT);
    }
    for (name, value) in env_overrides {
        std::env::set_var(name, value);
    }
    std::env::set_var("ACE_SANDBOX", "linux-bwrap");
    if proxy_socket.is_some() {
        let proxy = super::proxy_routing::proxy_url();
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"] {
            std::env::set_var(name, &proxy);
        }
        std::env::set_var("NO_PROXY", "");
    }
    eprint!("{}", String::from_utf8_lossy(INNER_READY_MARKER));
    let error = Command::new(&command[0]).args(&command[1..]).exec();
    if let Some(bridge) = inner_bridge.as_mut() {
        bridge.stop();
    }
    eprintln!("failed to exec sandbox command: {error}");
    std::process::exit(127);
}

fn install(network_proxy: bool, allow_local_binding: bool) -> Result<(), String> {
    let result = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
    if result != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let mut rules: BTreeMap<i64, Vec<SeccompRule>> = BTreeMap::new();
    // Always-denied syscalls: ptrace/proc-vm cross-memory access, io_uring (extra
    // attack surface), namespace-creation/entry + kernel-keyring/eBPF syscalls, and
    // mount-tree mutation syscalls. The bwrap user namespace grants CAP_SYS_ADMIN
    // *inside* that namespace, so the sandboxed command could otherwise remount the
    // read-only root, flip protected subpaths (`.git`/`.agents`/`.crew`) writable, or
    // overlay denied_roots with a fresh tmpfs--breaking spec §6.2/§8.1. Deny mount
    // family outright to keep the bwrap-established mount tree immutable.
    // clone is intentionally NOT denied outright (needed for fork/threads); a
    // namespace-flagged clone is bounded by the deny on unshare/setns.
    for syscall in [
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
        libc::SYS_io_uring_setup,
        libc::SYS_io_uring_enter,
        libc::SYS_io_uring_register,
        libc::SYS_unshare,
        libc::SYS_setns,
        libc::SYS_bpf,
        libc::SYS_keyctl,
        libc::SYS_add_key,
        libc::SYS_request_key,
        libc::SYS_mount,
        libc::SYS_umount2,
        libc::SYS_pivot_root,
        libc::SYS_open_tree,
        libc::SYS_move_mount,
        libc::SYS_fsopen,
        libc::SYS_fsconfig,
        libc::SYS_fsmount,
        libc::SYS_mount_setattr,
    ] {
        rules.insert(syscall, vec![]);
    }
    if network_proxy {
        // The command may create IP sockets only to reach the TCP bridge.
        // AF_UNIX socketpair remains available for process-local IPC, while
        // connectable AF_UNIX sockets are denied to avoid bypassing the bridge.
        let deny_non_ip_socket = SeccompRule::new(vec![
            SeccompCondition::new(
                0,
                SeccompCmpArgLen::Dword,
                SeccompCmpOp::Ne,
                libc::AF_INET as u64,
            )
            .map_err(|error| error.to_string())?,
            SeccompCondition::new(
                0,
                SeccompCmpArgLen::Dword,
                SeccompCmpOp::Ne,
                libc::AF_INET6 as u64,
            )
            .map_err(|error| error.to_string())?,
        ])
        .map_err(|error| error.to_string())?;
        let deny_non_unix_socketpair = SeccompRule::new(vec![SeccompCondition::new(
            0,
            SeccompCmpArgLen::Dword,
            SeccompCmpOp::Ne,
            libc::AF_UNIX as u64,
        )
        .map_err(|error| error.to_string())?])
        .map_err(|error| error.to_string())?;
        rules.insert(libc::SYS_socket, vec![deny_non_ip_socket]);
        rules.insert(libc::SYS_socketpair, vec![deny_non_unix_socketpair]);
    } else {
        // Offline tasks retain only AF_UNIX process-local IPC.
        let deny_non_unix = SeccompRule::new(vec![SeccompCondition::new(
            0,
            SeccompCmpArgLen::Dword,
            SeccompCmpOp::Ne,
            libc::AF_UNIX as u64,
        )
        .map_err(|error| error.to_string())?])
        .map_err(|error| error.to_string())?;
        rules.insert(libc::SYS_socket, vec![deny_non_unix.clone()]);
        rules.insert(libc::SYS_socketpair, vec![deny_non_unix]);
    }
    if !allow_local_binding {
        rules.insert(libc::SYS_bind, vec![]);
        rules.insert(libc::SYS_listen, vec![]);
    }
    let arch = if cfg!(target_arch = "x86_64") {
        TargetArch::x86_64
    } else if cfg!(target_arch = "aarch64") {
        TargetArch::aarch64
    } else {
        return Err("unsupported seccomp architecture".to_string());
    };
    let filter = SeccompFilter::new(
        rules,
        SeccompAction::Allow,
        SeccompAction::Errno(libc::EPERM as u32),
        arch,
    )
    .map_err(|error| error.to_string())?;
    let program: BpfProgram = filter
        .try_into()
        .map_err(|error: seccompiler::BackendError| error.to_string())?;
    apply_filter(&program).map_err(|error| error.to_string())
}

fn verify_active_hardening() -> Result<(), String> {
    let no_new_privs = unsafe { libc::prctl(libc::PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) };
    if no_new_privs != 1 {
        return Err(format!(
            "PR_GET_NO_NEW_PRIVS returned {no_new_privs}: {}",
            std::io::Error::last_os_error()
        ));
    }
    let seccomp_mode = unsafe { libc::prctl(libc::PR_GET_SECCOMP, 0, 0, 0, 0) };
    if seccomp_mode != 2 {
        return Err(format!("PR_GET_SECCOMP returned mode {seccomp_mode}"));
    }
    let status = std::fs::read_to_string("/proc/self/status")
        .map_err(|error| format!("cannot read fresh /proc status: {error}"))?;
    if !status.lines().any(|line| {
        line.strip_prefix("NoNewPrivs:")
            .is_some_and(|value| value.trim() == "1")
    }) {
        return Err("/proc status does not confirm NoNewPrivs=1".to_string());
    }
    if !status.lines().any(|line| {
        line.strip_prefix("Seccomp:")
            .is_some_and(|value| value.trim() == "2")
    }) {
        return Err("/proc status does not confirm seccomp filter mode".to_string());
    }
    for namespace in ["user", "pid", "net", "mnt", "ipc", "uts"] {
        std::fs::read_link(format!("/proc/self/ns/{namespace}"))
            .map_err(|error| format!("cannot inspect {namespace} namespace: {error}"))?;
    }
    Ok(())
}

fn parse_arguments(arguments: Vec<String>) -> Result<InnerArguments, String> {
    let split = arguments
        .iter()
        .position(|value| value == "--")
        .ok_or_else(|| "missing -- command separator".to_string())?;
    let mut proxy_socket = None;
    let mut allow_local_binding = false;
    let mut env_overrides = BTreeMap::new();
    let mut index = 0;
    while index < split {
        match arguments[index].as_str() {
            "--proxy-socket" => {
                index += 1;
                proxy_socket = Some(
                    arguments
                        .get(index)
                        .ok_or_else(|| "missing proxy socket".to_string())?
                        .clone(),
                );
            }
            "--allow-local-binding" => allow_local_binding = true,
            "--env-overrides-b64" => {
                index += 1;
                let encoded = arguments
                    .get(index)
                    .ok_or_else(|| "missing environment overrides".to_string())?;
                let decoded = BASE64_STANDARD
                    .decode(encoded)
                    .map_err(|_| "invalid environment overrides".to_string())?;
                env_overrides = serde_json::from_slice(&decoded)
                    .map_err(|_| "invalid environment overrides".to_string())?;
            }
            other => return Err(format!("unknown inner-stage option: {other}")),
        }
        index += 1;
    }
    let command = arguments.into_iter().skip(split + 1).collect();
    Ok(InnerArguments {
        command,
        proxy_socket,
        allow_local_binding,
        env_overrides,
    })
}
