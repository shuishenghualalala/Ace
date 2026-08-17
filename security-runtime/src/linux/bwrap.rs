use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use globset::{GlobBuilder, GlobSet, GlobSetBuilder};

use super::LinuxRunRequest;

const PROTECTED_NAMES: &[&str] = &[".git", ".agents", ".crew"];
pub const MAX_DENY_READ_GLOB_MATCHES: usize = 8192;
const PLATFORM_READ_ROOTS: &[&str] = &[
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/nix/store",
    "/run/current-system/sw",
];

pub struct BwrapPlan {
    pub args: Vec<String>,
    synthetic_targets: Vec<PathBuf>,
    spawned: bool,
}

impl BwrapPlan {
    #[allow(dead_code)]
    pub fn mark_spawned(&mut self) {
        self.spawned = true;
    }

    pub fn cleanup(&mut self) -> Result<(), String> {
        if !self.spawned {
            return Ok(());
        }
        self.spawned = false;
        let mut failures = Vec::new();
        for target in self.synthetic_targets.iter().rev() {
            match std::fs::remove_dir(target) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => failures.push(format!("{}: {error}", target.display())),
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "cannot safely clean synthetic bwrap mount targets: {}",
                failures.join("; ")
            ))
        }
    }
}

impl Drop for BwrapPlan {
    fn drop(&mut self) {
        // bwrap may create mount targets below a writable bind. Remove only
        // still-empty synthetic directories; concurrent real content is preserved.
        let _ = self.cleanup();
    }
}

/// Build a Codex-shaped bwrap profile: empty root, explicit reads/writes, protected metadata.
pub fn build_args(request: &LinuxRunRequest) -> Result<BwrapPlan, String> {
    match (request.network_enabled, request.proxy_socket_dir.is_some()) {
        (true, false) => {
            return Err("managed network requires a private proxy bridge".to_string());
        }
        (false, true) => {
            return Err("offline sandbox cannot mount a proxy bridge".to_string());
        }
        _ => {}
    }
    let proxy_socket_dir = request
        .proxy_socket_dir
        .as_deref()
        .map(|path| {
            let canonical = path.canonicalize().map_err(|error| {
                format!(
                    "cannot resolve proxy bridge directory {}: {error}",
                    path.display()
                )
            })?;
            if !canonical.is_dir() {
                return Err(format!(
                    "proxy bridge directory is not a directory: {}",
                    path.display()
                ));
            }
            #[cfg(target_os = "linux")]
            {
                use std::os::unix::fs::FileTypeExt;

                let socket = canonical.join("proxy.sock");
                let metadata = std::fs::symlink_metadata(&socket).map_err(|error| {
                    format!(
                        "cannot inspect proxy bridge socket {}: {error}",
                        socket.display()
                    )
                })?;
                if !metadata.file_type().is_socket() {
                    return Err(format!(
                        "proxy bridge endpoint is not a Unix socket: {}",
                        socket.display()
                    ));
                }
            }
            Ok(canonical)
        })
        .transpose()?;
    let cwd = canonical_directory(&request.cwd)?;
    let executable = env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(|error| format!("cannot resolve runtime executable: {error}"))?;
    let writable = canonical_roots(&request.writable_roots)?;
    if writable.iter().any(|root| root.parent().is_none()) {
        return Err(
            "writable filesystem root is incompatible with the managed bwrap backend".to_string(),
        );
    }
    let readable = canonical_roots(&request.readable_roots)?;
    if !writable
        .iter()
        .chain(readable.iter())
        .any(|root| cwd.starts_with(root))
    {
        return Err("sandbox cwd must be inside an explicit authorized root".to_string());
    }
    let mut denied = canonical_or_missing_roots(&request.denied_roots)?;
    denied.extend(expand_deny_read_globs(request)?);
    denied.sort();
    denied.dedup();
    for root in &denied {
        if let Some(symlink) = first_writable_symlink_component(root, &writable) {
            return Err(format!(
                "cannot enforce deny-read path {} because it crosses writable symlink {}",
                root.display(),
                symlink.display()
            ));
        }
    }
    if denied.iter().any(|root| cwd.starts_with(root)) {
        return Err("sandbox cwd cannot be hidden by a deny root".to_string());
    }
    reject_overlapping_roots(&writable, &readable)?;

    let mut synthetic_targets = Vec::new();
    let mut args = vec![
        "--new-session".to_string(),
        "--die-with-parent".to_string(),
        "--rlimit".to_string(),
        "AS".to_string(),
        "4294967296".to_string(),
        "--rlimit".to_string(),
        "FSIZE".to_string(),
        "2147483648".to_string(),
        "--rlimit".to_string(),
        "NOFILE".to_string(),
        "4096".to_string(),
        "--rlimit".to_string(),
        "NPROC".to_string(),
        "256".to_string(),
        "--unshare-user".to_string(),
        "--uid".to_string(),
        "1000".to_string(),
        "--gid".to_string(),
        "1000".to_string(),
        "--unshare-pid".to_string(),
        "--unshare-ipc".to_string(),
        "--unshare-uts".to_string(),
        "--tmpfs".to_string(),
        "/".to_string(),
        "--dev".to_string(),
        "/dev".to_string(),
        "--tmpfs".to_string(),
        "/tmp".to_string(),
        "--tmpfs".to_string(),
        "/run".to_string(),
        "--dir".to_string(),
        "/tmp/ace-home".to_string(),
    ];
    // Network is always namespaced. Approved traffic reaches only the host proxy
    // through a private Unix socket bridge mounted below the masked /run.
    args.push("--unshare-net".to_string());
    args.extend(["--proc".to_string(), "/proc".to_string()]);

    let mut created_target_dirs = BTreeSet::from([
        PathBuf::from("/dev"),
        PathBuf::from("/proc"),
        PathBuf::from("/run"),
        PathBuf::from("/tmp"),
    ]);
    let mut visible_roots = Vec::new();
    for root in PLATFORM_READ_ROOTS
        .iter()
        .map(PathBuf::from)
        .filter(|path| path.exists())
    {
        append_bind(&mut args, &mut created_target_dirs, "--ro-bind", &root)?;
        visible_roots.push(root);
    }
    for root in &readable {
        append_bind(&mut args, &mut created_target_dirs, "--ro-bind", root)?;
        visible_roots.push(root.clone());
    }
    for root in &writable {
        append_bind(&mut args, &mut created_target_dirs, "--bind", root)?;
        visible_roots.push(root.clone());
    }

    // The helper re-enters itself inside the namespace before executing the
    // user command. Always overlay that exact binary read-only: development
    // builds can live below a writable workspace bind and must not let the
    // sandbox poison the helper used by a later launch.
    let executable_was_visible = is_visible(&executable, &visible_roots);
    append_bind(
        &mut args,
        &mut created_target_dirs,
        "--ro-bind",
        &executable,
    )?;
    if !executable_was_visible {
        visible_roots.push(executable.clone());
    }
    if let Some(command_path) = request.command.first().map(PathBuf::from) {
        if command_path.is_absolute() {
            match std::fs::symlink_metadata(&command_path) {
                Ok(_) => {
                    let canonical_command = command_path.canonicalize().map_err(|error| {
                        format!(
                            "cannot resolve sandbox executable {}: {error}",
                            command_path.display()
                        )
                    })?;
                    if !canonical_command.is_file() {
                        return Err(format!(
                            "sandbox executable is not a file: {}",
                            command_path.display()
                        ));
                    }
                    if !is_visible(&canonical_command, &visible_roots) {
                        append_bind(
                            &mut args,
                            &mut created_target_dirs,
                            "--ro-bind",
                            &canonical_command,
                        )?;
                        visible_roots.push(canonical_command);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(format!(
                        "cannot inspect sandbox executable {}: {error}",
                        command_path.display()
                    ));
                }
            }
        }
    }

    // Re-apply immutable project metadata after writable roots, so the narrow rule wins.
    for root in &writable {
        if !root.is_dir() {
            continue;
        }
        for name in PROTECTED_NAMES {
            let protected = root.join(name);
            match std::fs::symlink_metadata(&protected) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() {
                        return Err(format!(
                            "protected metadata path cannot be a symlink: {}",
                            protected.display()
                        ));
                    }
                    let value = path_string(&protected)?;
                    args.extend(["--ro-bind".to_string(), value.clone(), value]);
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    // Match Codex's missing-protected-entry shape: a read-only
                    // synthetic directory prevents creation through a writable root.
                    let value = path_string(&protected)?;
                    args.extend([
                        "--perms".to_string(),
                        "555".to_string(),
                        "--tmpfs".to_string(),
                        value.clone(),
                        "--remount-ro".to_string(),
                        value,
                    ]);
                    synthetic_targets.push(protected);
                }
                Err(error) => {
                    return Err(format!(
                        "cannot inspect protected metadata path {}: {error}",
                        protected.display()
                    ));
                }
            }
        }
    }
    // Deny entries are applied last and therefore cannot be upgraded by an
    // additional permission or by a narrower protected metadata mount.
    for root in &denied {
        if !is_visible(root, &visible_roots) {
            continue;
        }
        let value = path_string(root)?;
        if root.is_dir() || !root.exists() {
            if !root.exists() {
                register_synthetic_mount_targets(root, &writable, &mut synthetic_targets);
            }
            args.extend([
                "--perms".to_string(),
                "000".to_string(),
                "--tmpfs".to_string(),
                value,
            ]);
        } else {
            args.extend(["--ro-bind".to_string(), "/dev/null".to_string(), value]);
        }
    }
    if let Some(socket_dir) = &proxy_socket_dir {
        args.extend([
            "--ro-bind".to_string(),
            path_string(socket_dir)?,
            "/run/ace-network".to_string(),
        ]);
    }
    // Re-enter the exact helper already mounted above, then apply the inner seccomp stage.
    args.extend([
        "--chdir".to_string(),
        path_string(&cwd)?,
        "--clearenv".to_string(),
        "--setenv".to_string(),
        "PATH".to_string(),
        "/usr/local/bin:/usr/bin:/bin".to_string(),
        "--setenv".to_string(),
        "HOME".to_string(),
        "/tmp/ace-home".to_string(),
        "--setenv".to_string(),
        "TMPDIR".to_string(),
        "/tmp".to_string(),
    ]);
    args.extend([
        "--".to_string(),
        path_string(&executable)?,
        "--inner-seccomp".to_string(),
    ]);
    if request.network_enabled {
        args.extend([
            "--proxy-socket".to_string(),
            super::proxy_routing::INNER_SOCKET_PATH.to_string(),
        ]);
    }
    if request.allow_local_binding {
        args.push("--allow-local-binding".to_string());
    }
    if !request.env_overrides.is_empty() {
        let encoded = serde_json::to_vec(&request.env_overrides)
            .map(|value| BASE64_STANDARD.encode(value))
            .map_err(|_| "cannot encode environment overrides".to_string())?;
        args.extend(["--env-overrides-b64".to_string(), encoded]);
    }
    args.push("--".to_string());
    args.extend(request.command.clone());
    Ok(BwrapPlan {
        args,
        synthetic_targets,
        spawned: false,
    })
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

fn expand_deny_read_globs(request: &LinuxRunRequest) -> Result<Vec<PathBuf>, String> {
    let mut patterns_by_root = BTreeMap::<PathBuf, BTreeSet<String>>::new();
    for rule in &request.filesystem_globs {
        let root = canonical_glob_root(Path::new(&rule.root))?;
        let patterns = glob_pattern_variants(&rule.pattern)?;
        patterns_by_root.entry(root).or_default().extend(patterns);
    }

    let mut expanded = BTreeSet::new();
    for (root, patterns) in patterns_by_root {
        let mut builder = GlobSetBuilder::new();
        for pattern in patterns {
            let mut pattern_builder = GlobBuilder::new(&pattern);
            pattern_builder
                .literal_separator(true)
                .backslash_escape(false);
            let glob = pattern_builder.build().map_err(|error| {
                format!(
                    "deny-read glob pattern is invalid for {}: {error}",
                    root.display()
                )
            })?;
            builder.add(glob);
        }
        let matcher = builder.build().map_err(|error| {
            format!(
                "deny-read glob matcher failed for {}: {error}",
                root.display()
            )
        })?;
        collect_glob_matches(&root, &root, &matcher, &mut expanded)?;
    }
    Ok(expanded.into_iter().collect())
}

fn canonical_glob_root(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(format!(
            "deny-read glob root must be a normalized absolute path: {}",
            path.display()
        ));
    }
    let canonical = path.canonicalize().map_err(|error| {
        format!(
            "cannot resolve deny-read glob root {}: {error}",
            path.display()
        )
    })?;
    #[cfg(target_os = "linux")]
    if canonical != path {
        return Err(format!(
            "deny-read glob root identity changed: {}",
            path.display()
        ));
    }
    if !canonical.is_dir() {
        return Err(format!(
            "deny-read glob root is not a directory: {}",
            path.display()
        ));
    }
    Ok(canonical)
}

fn glob_pattern_variants(pattern: &str) -> Result<BTreeSet<String>, String> {
    let parts = pattern.split('/').collect::<Vec<_>>();
    if pattern.is_empty()
        || pattern.contains('\0')
        || pattern.contains('\\')
        || pattern.contains(':')
        || pattern.contains('{')
        || pattern.contains('}')
        || parts.iter().any(|part| matches!(*part, "" | "." | ".."))
        || parts
            .iter()
            .any(|part| part.contains("**") && *part != "**")
    {
        return Err("deny-read glob pattern is not a safe relative pattern".to_string());
    }

    let mut in_class = false;
    let mut class_size = 0usize;
    for character in pattern.chars() {
        match character {
            '[' if !in_class => {
                in_class = true;
                class_size = 0;
            }
            '[' => return Err("deny-read glob pattern has a nested class".to_string()),
            ']' if in_class && class_size > 0 => in_class = false,
            ']' => return Err("deny-read glob pattern has an invalid class".to_string()),
            '/' if in_class => {
                return Err("deny-read glob pattern class contains a separator".to_string())
            }
            _ if in_class => class_size += 1,
            _ => {}
        }
    }
    if in_class {
        return Err("deny-read glob pattern has an unclosed class".to_string());
    }

    let mut variants = BTreeSet::from([pattern.to_string()]);
    if !pattern.starts_with("**/") {
        variants.insert(format!("**/{pattern}"));
    }
    let mut suffix = pattern;
    while let Some(stripped) = suffix.strip_prefix("**/") {
        variants.insert(stripped.to_string());
        suffix = stripped;
    }
    Ok(variants)
}

fn collect_glob_matches(
    search_root: &Path,
    directory: &Path,
    matcher: &GlobSet,
    expanded: &mut BTreeSet<PathBuf>,
) -> Result<(), String> {
    let entries = fs::read_dir(directory).map_err(|error| {
        format!(
            "cannot scan deny-read glob directory {}: {error}",
            directory.display()
        )
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            format!(
                "cannot scan deny-read glob directory {}: {error}",
                directory.display()
            )
        })?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|error| {
            format!(
                "cannot inspect deny-read glob candidate {}: {error}",
                path.display()
            )
        })?;
        let relative = path.strip_prefix(search_root).map_err(|_| {
            format!(
                "deny-read glob candidate escaped its root: {}",
                path.display()
            )
        })?;

        if (file_type.is_file() || file_type.is_symlink()) && matcher.is_match(relative) {
            let canonical = path.canonicalize().map_err(|error| {
                format!(
                    "deny-read glob match changed during expansion {}: {error}",
                    path.display()
                )
            })?;
            insert_glob_match(expanded, canonical, search_root)?;
            insert_glob_match(expanded, path.clone(), search_root)?;
        }
        if file_type.is_dir() {
            collect_glob_matches(search_root, &path, matcher, expanded)?;
        }
    }
    Ok(())
}

fn insert_glob_match(
    expanded: &mut BTreeSet<PathBuf>,
    path: PathBuf,
    search_root: &Path,
) -> Result<(), String> {
    expanded.insert(path);
    if expanded.len() > MAX_DENY_READ_GLOB_MATCHES {
        return Err(format!(
            "deny-read glob expansion for {} matched more than {MAX_DENY_READ_GLOB_MATCHES} paths",
            search_root.display()
        ));
    }
    Ok(())
}

fn first_writable_symlink_component(target: &Path, writable_roots: &[PathBuf]) -> Option<PathBuf> {
    let mut current = PathBuf::new();
    for component in target.components() {
        match component {
            Component::Prefix(prefix) => current.push(prefix.as_os_str()),
            Component::RootDir => current.push(Path::new("/")),
            Component::CurDir => continue,
            Component::ParentDir => {
                current.pop();
                continue;
            }
            Component::Normal(part) => current.push(part),
        }
        let Ok(metadata) = fs::symlink_metadata(&current) else {
            break;
        };
        if metadata.file_type().is_symlink()
            && writable_roots.iter().any(|root| current.starts_with(root))
        {
            return Some(current);
        }
    }
    None
}

fn canonical_or_missing_roots(paths: &[PathBuf]) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::new();
    for path in paths {
        let value = if path.exists() {
            path.canonicalize()
                .map_err(|error| format!("cannot resolve deny root {}: {error}", path.display()))?
        } else if path.is_absolute()
            && !path
                .components()
                .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
        {
            canonicalize_missing_path(path)?
        } else {
            return Err(format!(
                "missing deny root must be a normalized absolute path: {}",
                path.display()
            ));
        };
        if !result.contains(&value) {
            result.push(value);
        }
    }
    Ok(result)
}

fn canonicalize_missing_path(path: &Path) -> Result<PathBuf, String> {
    let mut ancestor = path.to_path_buf();
    let mut suffix = Vec::<OsString>::new();
    loop {
        match std::fs::symlink_metadata(&ancestor) {
            Ok(_) => {
                let mut resolved = ancestor.canonicalize().map_err(|error| {
                    format!(
                        "cannot resolve existing ancestor of deny root {}: {error}",
                        path.display()
                    )
                })?;
                if !suffix.is_empty() && !resolved.is_dir() {
                    return Err(format!(
                        "existing ancestor of deny root is not a directory: {}",
                        ancestor.display()
                    ));
                }
                for component in suffix.into_iter().rev() {
                    resolved.push(component);
                }
                return Ok(resolved);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = ancestor.file_name().ok_or_else(|| {
                    format!(
                        "cannot find an existing ancestor for deny root {}",
                        path.display()
                    )
                })?;
                suffix.push(name.to_os_string());
                ancestor = ancestor
                    .parent()
                    .ok_or_else(|| {
                        format!(
                            "cannot find an existing ancestor for deny root {}",
                            path.display()
                        )
                    })?
                    .to_path_buf();
            }
            Err(error) => {
                return Err(format!(
                    "cannot inspect deny root {}: {error}",
                    path.display()
                ));
            }
        }
    }
}

fn reject_overlapping_roots(writable: &[PathBuf], readable: &[PathBuf]) -> Result<(), String> {
    for write in writable {
        for read in readable {
            if write.starts_with(read) || read.starts_with(write) {
                return Err(format!(
                    "overlapping read/write permission roots are ambiguous: {} and {}",
                    write.display(),
                    read.display()
                ));
            }
        }
    }
    Ok(())
}

fn register_synthetic_mount_targets(
    target: &Path,
    writable_roots: &[PathBuf],
    synthetic_targets: &mut Vec<PathBuf>,
) {
    let Some(writable_root) = writable_roots
        .iter()
        .filter(|root| target.starts_with(root))
        .max_by_key(|root| root.components().count())
    else {
        return;
    };
    let mut missing = Vec::new();
    let mut cursor = target;
    while cursor != writable_root && !cursor.exists() {
        missing.push(cursor.to_path_buf());
        let Some(parent) = cursor.parent() else {
            break;
        };
        cursor = parent;
    }
    missing.reverse();
    for path in missing {
        if !synthetic_targets.contains(&path) {
            synthetic_targets.push(path);
        }
    }
}

fn append_bind(
    args: &mut Vec<String>,
    created_target_dirs: &mut BTreeSet<PathBuf>,
    operation: &str,
    path: &Path,
) -> Result<(), String> {
    let mut parents = path
        .parent()
        .into_iter()
        .flat_map(Path::ancestors)
        .filter(|parent| parent.parent().is_some())
        .map(Path::to_path_buf)
        .collect::<Vec<_>>();
    parents.reverse();
    for parent in parents {
        if created_target_dirs.insert(parent.clone()) {
            args.extend(["--dir".to_string(), path_string(&parent)?]);
        }
    }
    let value = path_string(path)?;
    args.extend([operation.to_string(), value.clone(), value]);
    Ok(())
}

fn is_visible(path: &Path, visible_roots: &[PathBuf]) -> bool {
    visible_roots
        .iter()
        .any(|root| path.starts_with(root) || root.starts_with(path))
}

fn path_string(path: &Path) -> Result<String, String> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| format!("path is not valid UTF-8: {}", path.display()))
}

#[cfg(test)]
mod tests {
    #[cfg(target_os = "linux")]
    use super::super::{FilesystemGlobAccess, FilesystemGlobRule};
    use super::build_args;
    use crate::linux::LinuxRunRequest;

    #[test]
    fn profile_has_namespaces_and_protected_metadata() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir(temp.path().join(".git")).unwrap();
        let request = LinuxRunRequest {
            command: vec!["true".to_string()],
            cwd: temp.path().to_path_buf(),
            writable_roots: vec![temp.path().to_path_buf()],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };
        let plan = build_args(&request).unwrap();
        assert!(plan.args.iter().any(|arg| arg == "--unshare-net"));
        assert!(plan
            .args
            .windows(2)
            .any(|window| window == ["--uid", "1000"]));
        assert!(plan
            .args
            .windows(2)
            .any(|window| window == ["--gid", "1000"]));
        assert!(plan.args.iter().any(|arg| arg == "--die-with-parent"));
        assert!(plan.args.iter().any(|arg| arg.ends_with(".git")));
        assert!(plan
            .args
            .windows(3)
            .any(|window| window == ["--rlimit", "AS", "4294967296"]));
        assert!(plan
            .args
            .windows(3)
            .any(|window| window == ["--rlimit", "NPROC", "256"]));
    }

    #[test]
    fn user_argv_is_appended_byte_for_byte_after_the_bwrap_separator() {
        let workspace = tempfile::tempdir().unwrap();
        let command = vec![
            "-looks-like-a-bwrap-option".to_string(),
            "--bind".to_string(),
            "/outside".to_string(),
        ];
        let request = LinuxRunRequest {
            command: command.clone(),
            cwd: workspace.path().to_path_buf(),
            writable_roots: vec![workspace.path().to_path_buf()],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();
        let separators = plan
            .args
            .iter()
            .enumerate()
            .filter_map(|(index, arg)| (arg == "--").then_some(index))
            .collect::<Vec<_>>();

        // The first separator ends bwrap options. The second ends the fixed
        // inner-seccomp wrapper options. User argv follows both unchanged.
        assert_eq!(separators.len(), 2);
        assert_eq!(&plan.args[separators[1] + 1..], command.as_slice());
        assert!(separators[0] < separators[1]);
    }

    #[test]
    fn profile_starts_from_empty_root_without_mounting_host_root() {
        let parent = tempfile::tempdir().unwrap();
        let workspace = parent.path().join("workspace");
        let host_secret = parent.path().join("host-secret");
        std::fs::create_dir(&workspace).unwrap();
        std::fs::write(&host_secret, "secret").unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.clone(),
            writable_roots: vec![workspace],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();

        assert!(plan
            .args
            .windows(2)
            .any(|window| window == ["--tmpfs", "/"]));
        assert!(!plan
            .args
            .windows(3)
            .any(|window| window == ["--ro-bind", "/", "/"]));
        assert!(!plan
            .args
            .iter()
            .any(|arg| arg == host_secret.to_str().unwrap()));
    }

    #[test]
    fn cwd_must_already_be_inside_an_explicit_authorized_root() {
        let workspace = tempfile::tempdir().unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.path().to_path_buf(),
            writable_roots: vec![],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let error = build_args(&request)
            .err()
            .expect("cwd must not auto-expand policy");

        assert!(error.contains("authorized root"), "{error}");
    }

    #[test]
    fn read_only_cwd_is_mounted_without_becoming_writable() {
        let workspace = tempfile::tempdir().unwrap();
        let root = workspace.path().canonicalize().unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: root.clone(),
            writable_roots: vec![],
            readable_roots: vec![root.clone()],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();
        let root = root.to_string_lossy().into_owned();
        assert!(plan
            .args
            .windows(3)
            .any(|window| { window[0] == "--ro-bind" && window[1] == root && window[2] == root }));
        assert!(!plan
            .args
            .windows(3)
            .any(|window| { window[0] == "--bind" && window[1] == root && window[2] == root }));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn missing_deny_root_is_resolved_through_existing_symlink_ancestors() {
        use std::os::unix::fs::symlink;

        let workspace = tempfile::tempdir().unwrap();
        let real = workspace.path().join("real");
        let link = workspace.path().join("link");
        std::fs::create_dir(&real).unwrap();
        symlink(&real, &link).unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.path().to_path_buf(),
            writable_roots: vec![workspace.path().to_path_buf()],
            readable_roots: vec![],
            denied_roots: vec![link.join("missing")],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();
        let resolved = real.join("missing").to_string_lossy().into_owned();
        let unresolved = link.join("missing").to_string_lossy().into_owned();

        assert!(
            plan.args.iter().any(|arg| arg == &resolved),
            "{:?}",
            plan.args
        );
        assert!(
            !plan.args.iter().any(|arg| arg == &unresolved),
            "{:?}",
            plan.args
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn absolute_command_symlink_binds_only_its_canonical_target() {
        use std::os::unix::fs::symlink;

        let workspace = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let executable = outside.path().join("tool");
        std::fs::write(&executable, b"tool").unwrap();
        let link = workspace.path().join("tool");
        symlink(&executable, &link).unwrap();
        let request = LinuxRunRequest {
            command: vec![link.to_string_lossy().into_owned()],
            cwd: workspace.path().to_path_buf(),
            writable_roots: vec![workspace.path().to_path_buf()],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();
        let canonical = executable
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .into_owned();

        assert!(
            plan.args.iter().any(|arg| arg == &canonical),
            "{:?}",
            plan.args
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn deny_read_glob_masks_the_canonical_target_of_a_read_only_symlink() {
        use std::os::unix::fs::symlink;

        let parent = tempfile::tempdir().unwrap();
        let workspace = parent.path().join("workspace");
        let glob_root = parent.path().join("glob-root");
        let target_root = parent.path().join("target-root");
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::create_dir_all(&glob_root).unwrap();
        std::fs::create_dir_all(&target_root).unwrap();
        let target = target_root.join("secret.txt");
        std::fs::write(&target, "secret").unwrap();
        symlink(&target, glob_root.join("alias.pem")).unwrap();
        let glob_root = glob_root.canonicalize().unwrap();
        let target = target.canonicalize().unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.clone(),
            writable_roots: vec![workspace],
            readable_roots: vec![glob_root.clone(), target_root],
            denied_roots: vec![],
            filesystem_globs: vec![FilesystemGlobRule {
                root: glob_root.to_string_lossy().into_owned(),
                pattern: "*.pem".to_string(),
                access: FilesystemGlobAccess::DenyRead,
            }],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let plan = build_args(&request).unwrap();
        let target = target.to_string_lossy();

        assert!(
            plan.args.windows(3).any(|window| {
                window[0] == "--ro-bind" && window[1] == "/dev/null" && window[2] == target
            }),
            "{:?}",
            plan.args
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn deny_read_glob_rejects_a_symlink_mutable_through_a_writable_root() {
        use std::os::unix::fs::symlink;

        let workspace = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let target = outside.path().join("secret.txt");
        std::fs::write(&target, "secret").unwrap();
        symlink(&target, workspace.path().join("alias.pem")).unwrap();
        let root = workspace.path().canonicalize().unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: root.clone(),
            writable_roots: vec![root.clone()],
            readable_roots: vec![],
            denied_roots: vec![],
            filesystem_globs: vec![FilesystemGlobRule {
                root: root.to_string_lossy().into_owned(),
                pattern: "*.pem".to_string(),
                access: FilesystemGlobAccess::DenyRead,
            }],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let error = build_args(&request)
            .err()
            .expect("a mutable symlink would make a startup-time mask racy");

        assert!(error.contains("writable symlink"), "{error}");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn deny_read_glob_rejects_a_noncanonical_root() {
        use std::os::unix::fs::symlink;

        let parent = tempfile::tempdir().unwrap();
        let workspace = parent.path().join("workspace");
        let real_root = parent.path().join("real-root");
        let linked_root = parent.path().join("linked-root");
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::create_dir_all(&real_root).unwrap();
        symlink(&real_root, &linked_root).unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.clone(),
            writable_roots: vec![workspace],
            readable_roots: vec![real_root],
            denied_roots: vec![],
            filesystem_globs: vec![FilesystemGlobRule {
                root: linked_root.to_string_lossy().into_owned(),
                pattern: "*.pem".to_string(),
                access: FilesystemGlobAccess::DenyRead,
            }],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            stdin_stream: None,
            env_overrides: Default::default(),
        };

        let error = build_args(&request)
            .err()
            .expect("native expansion must not retarget a signed root");

        assert!(error.contains("identity changed"), "{error}");
    }
}
