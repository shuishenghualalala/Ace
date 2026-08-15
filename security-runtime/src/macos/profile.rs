use std::collections::{BTreeMap, BTreeSet};

pub const SANDBOX_EXECUTABLE: &str = "/usr/bin/sandbox-exec";
pub const SANDBOX_PROBE_EXECUTABLE: &str = "/usr/bin/true";

const MAX_PATH_DEFINITIONS: usize = 1024;
const FIXED_PATH: &str = "/usr/bin:/bin:/usr/sbin:/sbin";
const PLATFORM_READ_DIRECTORIES: &[&str] = &[
    "/System",
    "/Library/Apple",
    "/usr/bin",
    "/usr/lib",
    "/usr/share",
    "/bin",
    "/sbin",
    "/private/var/db/dyld",
];
const PLATFORM_READ_FILES: &[&str] = &[
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
    "/dev/zero",
    "/private/etc/hosts",
    "/private/etc/protocols",
    "/private/etc/resolv.conf",
    "/private/etc/services",
];

const BASE_POLICY: &str = include_str!("base_policy.sbpl");

const RESTRICTED_NETWORK_SUPPORT_POLICY: &str = "\
(allow system-socket
  (require-all
    (socket-domain AF_SYSTEM)
    (socket-protocol 2)))
(allow mach-lookup
  (global-name \"com.apple.bsd.dirhelper\")
  (global-name \"com.apple.system.opendirectoryd.membership\")
  (global-name \"com.apple.SecurityServer\")
  (global-name \"com.apple.networkd\")
  (global-name \"com.apple.ocspd\")
  (global-name \"com.apple.trustd.agent\")
  (global-name \"com.apple.SystemConfiguration.DNSConfiguration\")
  (global-name \"com.apple.SystemConfiguration.configd\"))
(allow sysctl-read (sysctl-name-regex #\"^net.routetable\"))
";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NetworkAccess {
    Denied,
    ManagedProxy { port: u16 },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProfileInput {
    pub readable_roots: Vec<String>,
    pub writable_roots: Vec<String>,
    pub denied_roots: Vec<String>,
    pub private_home: String,
    pub command_executable: String,
    pub network: NetworkAccess,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompiledProfile {
    pub text: String,
    pub definitions: Vec<(String, String)>,
    command_executable: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SeatbeltInvocation {
    pub executable: &'static str,
    pub arguments: Vec<String>,
}

pub fn validate_requested_capabilities(
    network_enabled: bool,
    allow_local_binding: bool,
    network_protocols: &[String],
) -> Result<(), String> {
    if allow_local_binding {
        return Err(
            "macOS Seatbelt local binding is unsupported without port-scoped grants".to_string(),
        );
    }
    if !network_enabled && !network_protocols.is_empty() {
        return Err("network rules require managed network enforcement".to_string());
    }
    if let Some(protocol) = network_protocols
        .iter()
        .find(|protocol| !matches!(protocol.as_str(), "http" | "https"))
    {
        return Err(format!(
            "macOS managed proxy does not support {protocol} network rules"
        ));
    }
    Ok(())
}

pub fn compile_profile(input: ProfileInput) -> Result<CompiledProfile, String> {
    validate_absolute_path(&input.private_home, "private HOME")?;
    validate_absolute_path(&input.command_executable, "command executable")?;

    let readable_roots = validate_and_dedupe_paths(input.readable_roots, "readable root")?;
    let writable_roots = validate_and_dedupe_paths(input.writable_roots, "writable root")?;
    let denied_roots = validate_and_dedupe_paths(input.denied_roots, "denied root")?;
    let definition_count = readable_roots
        .len()
        .checked_add(writable_roots.len())
        .and_then(|count| count.checked_add(denied_roots.len()))
        .and_then(|count| count.checked_add(PLATFORM_READ_DIRECTORIES.len()))
        .and_then(|count| count.checked_add(PLATFORM_READ_FILES.len()))
        .and_then(|count| count.checked_add(2))
        .ok_or_else(|| "too many macOS Seatbelt path definitions".to_string())?;
    if definition_count > MAX_PATH_DEFINITIONS {
        return Err("too many macOS Seatbelt path definitions".to_string());
    }

    let mut definitions = denied_roots
        .iter()
        .enumerate()
        .map(|(index, path)| (format!("DENIED_ROOT_{index}"), path.clone()))
        .collect::<Vec<_>>();
    let denied_names = (0..denied_roots.len())
        .map(|index| format!("DENIED_ROOT_{index}"))
        .collect::<Vec<_>>();
    let mut rules = Vec::new();

    for (index, path) in PLATFORM_READ_DIRECTORIES.iter().enumerate() {
        let name = format!("PLATFORM_READ_DIR_{index}");
        definitions.push((name.clone(), (*path).to_string()));
        rules.push(allow_rule(
            "file-read*",
            &format!("(subpath (param \"{name}\"))"),
            &denied_names,
        ));
    }
    for (index, path) in PLATFORM_READ_FILES.iter().enumerate() {
        let name = format!("PLATFORM_READ_FILE_{index}");
        definitions.push((name.clone(), (*path).to_string()));
        rules.push(allow_rule(
            "file-read*",
            &format!("(literal (param \"{name}\"))"),
            &denied_names,
        ));
        if *path == "/dev/null" {
            rules.push(allow_rule(
                "file-write-data",
                &format!("(literal (param \"{name}\"))"),
                &denied_names,
            ));
        }
    }
    for (index, path) in readable_roots.iter().enumerate() {
        let name = format!("READABLE_ROOT_{index}");
        definitions.push((name.clone(), path.clone()));
        rules.push(allow_rule(
            "file-read*",
            &format!("(subpath (param \"{name}\"))"),
            &denied_names,
        ));
    }
    for (index, path) in writable_roots.iter().enumerate() {
        let name = format!("WRITABLE_ROOT_{index}");
        definitions.push((name.clone(), path.clone()));
        let filter = format!("(subpath (param \"{name}\"))");
        rules.push(allow_rule("file-read*", &filter, &denied_names));
        rules.push(allow_rule("file-write*", &filter, &denied_names));
    }

    definitions.push(("PRIVATE_HOME".to_string(), input.private_home));
    rules.push(allow_rule(
        "file-read*",
        "(subpath (param \"PRIVATE_HOME\"))",
        &denied_names,
    ));
    rules.push(allow_rule(
        "file-write*",
        "(subpath (param \"PRIVATE_HOME\"))",
        &denied_names,
    ));

    definitions.push((
        "COMMAND_EXECUTABLE".to_string(),
        input.command_executable.clone(),
    ));
    rules.push(allow_rule(
        "file-read*",
        "(literal (param \"COMMAND_EXECUTABLE\"))",
        &denied_names,
    ));

    for denied_name in &denied_names {
        rules.push(format!(
            "(deny file-read* (literal (param \"{denied_name}\")))"
        ));
        rules.push(format!(
            "(deny file-read* (subpath (param \"{denied_name}\")))"
        ));
        rules.push(format!(
            "(deny file-write* (literal (param \"{denied_name}\")))"
        ));
        rules.push(format!(
            "(deny file-write* (subpath (param \"{denied_name}\")))"
        ));
    }

    let network_policy = match input.network {
        NetworkAccess::Denied => String::new(),
        NetworkAccess::ManagedProxy { port: 0 } => {
            return Err("managed proxy port must be non-zero".to_string());
        }
        // DNS and destination resolution happen in the host-side proxy. The
        // sandbox receives no port-53 or blanket network rule.
        NetworkAccess::ManagedProxy { port } => format!(
            "{RESTRICTED_NETWORK_SUPPORT_POLICY}\
(allow network-outbound (remote ip \"127.0.0.1:{port}\"))\n"
        ),
    };

    let mut text = BASE_POLICY.replace("\r\n", "\n");
    text.push_str(&rules.join("\n"));
    text.push('\n');
    text.push_str(&network_policy);
    Ok(CompiledProfile {
        text,
        definitions,
        command_executable: input.command_executable,
    })
}

pub fn build_invocation(
    profile: &CompiledProfile,
    command: &[String],
) -> Result<SeatbeltInvocation, String> {
    let executable = command.first().ok_or_else(|| "empty command".to_string())?;
    validate_absolute_path(executable, "command executable")?;
    if executable != &profile.command_executable {
        return Err("command executable changed after Seatbelt profile compilation".to_string());
    }
    if command.iter().any(|argument| argument.contains('\0')) {
        return Err("command contains a NUL byte".to_string());
    }
    Ok(invocation_for_command(profile, command))
}

pub fn build_probe_invocation(profile: &CompiledProfile) -> SeatbeltInvocation {
    invocation_for_command(profile, &[SANDBOX_PROBE_EXECUTABLE.to_string()])
}

fn invocation_for_command(profile: &CompiledProfile, command: &[String]) -> SeatbeltInvocation {
    let mut arguments = Vec::with_capacity(profile.definitions.len() + command.len() + 3);
    arguments.push("-p".to_string());
    arguments.push(profile.text.clone());
    arguments.extend(
        profile
            .definitions
            .iter()
            .map(|(name, value)| format!("-D{name}={value}")),
    );
    arguments.push("--".to_string());
    arguments.extend(command.iter().cloned());
    SeatbeltInvocation {
        executable: SANDBOX_EXECUTABLE,
        arguments,
    }
}

pub fn build_environment(
    home: &str,
    temp: &str,
    proxy_port: Option<u16>,
    overrides: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, String>, String> {
    validate_absolute_path(home, "private HOME")?;
    validate_absolute_path(temp, "private TMPDIR")?;
    if !is_strict_descendant(temp, home) {
        return Err("private TMPDIR must be inside private HOME".to_string());
    }

    let mut environment = BTreeMap::new();
    for (name, value) in overrides {
        if !valid_environment_name(name) || value.contains('\0') {
            return Err("invalid macOS sandbox environment entry".to_string());
        }
        let normalized = name.to_ascii_uppercase();
        if matches!(
            normalized.as_str(),
            "HOME"
                | "TMPDIR"
                | "PATH"
                | "ACE_SANDBOX"
                | "HTTP_PROXY"
                | "HTTPS_PROXY"
                | "ALL_PROXY"
                | "NO_PROXY"
        ) || normalized.starts_with("DYLD_")
            || normalized.starts_with("__XPC_")
        {
            return Err(format!(
                "macOS sandbox environment entry {name} is reserved"
            ));
        }
        environment.insert(name.clone(), value.clone());
    }

    environment.insert("PATH".to_string(), FIXED_PATH.to_string());
    environment.insert("HOME".to_string(), home.to_string());
    environment.insert("TMPDIR".to_string(), temp.to_string());
    environment.insert("ACE_SANDBOX".to_string(), "macos-seatbelt".to_string());
    if let Some(port) = proxy_port {
        if port == 0 {
            return Err("managed proxy port must be non-zero".to_string());
        }
        let proxy_url = format!("http://127.0.0.1:{port}");
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
        environment.insert("NO_PROXY".to_string(), String::new());
        environment.insert("no_proxy".to_string(), String::new());
    }
    Ok(environment)
}

fn allow_rule(operation: &str, filter: &str, denied_names: &[String]) -> String {
    if denied_names.is_empty() {
        return format!("(allow {operation} {filter})");
    }
    let exclusions = denied_names
        .iter()
        .flat_map(|name| {
            [
                format!("    (require-not (literal (param \"{name}\")))"),
                format!("    (require-not (subpath (param \"{name}\")))"),
            ]
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!("(allow {operation}\n  (require-all\n    {filter}\n{exclusions}))")
}

fn validate_and_dedupe_paths(paths: Vec<String>, label: &str) -> Result<Vec<String>, String> {
    let mut seen = BTreeSet::new();
    let mut result = Vec::new();
    for path in paths {
        validate_absolute_path(&path, label)?;
        if seen.insert(path.clone()) {
            result.push(path);
        }
    }
    Ok(result)
}

fn validate_absolute_path(path: &str, label: &str) -> Result<(), String> {
    if path.is_empty() || !path.starts_with('/') || path.contains('\0') {
        return Err(format!("{label} must be an absolute NUL-free path"));
    }
    if path.len() > 1 && (path.ends_with('/') || path[1..].contains("//")) {
        return Err(format!("{label} must be normalized"));
    }
    if path
        .split('/')
        .skip(1)
        .any(|component| matches!(component, "." | ".."))
    {
        return Err(format!("{label} must not contain traversal components"));
    }
    Ok(())
}

fn is_strict_descendant(path: &str, root: &str) -> bool {
    if root == "/" {
        path != "/"
    } else {
        path.strip_prefix(root)
            .is_some_and(|tail| tail.starts_with('/'))
    }
}

fn valid_environment_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}
