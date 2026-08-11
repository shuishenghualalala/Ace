use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use rand::RngCore;

use super::LinuxRunRequest;

const PROTECTED_NAMES: &[&str] = &[".git", ".agents", ".crew"];
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
    home_staging: Option<PathBuf>,
}

impl Drop for BwrapPlan {
    fn drop(&mut self) {
        // bwrap may create mount targets below a writable bind. Remove only
        // still-empty synthetic directories; concurrent real content is preserved.
        for target in self.synthetic_targets.iter().rev() {
            let _ = std::fs::remove_dir(target);
        }
        if let Some(home_staging) = &self.home_staging {
            let _ = fs::remove_dir_all(home_staging);
        }
    }
}

/// Build a Codex-shaped bwrap profile: empty root, explicit reads/writes, protected metadata.
pub fn build_args(request: &LinuxRunRequest) -> Result<BwrapPlan, String> {
    let cwd = canonical_directory(&request.cwd)?;
    let executable = env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(|error| format!("cannot resolve runtime executable: {error}"))?;
    let writable = canonical_roots(&request.writable_roots)?;
    if !writable.iter().any(|root| cwd.starts_with(root)) {
        return Err("sandbox cwd must be inside an explicit writable root".to_string());
    }
    let readable = canonical_roots(&request.readable_roots)?;
    let readonly = protected_roots(&writable, &request.readonly_roots)?;
    let denied = canonical_or_missing_roots(&request.denied_roots)?;
    reject_overlapping_roots(&writable, &readable)?;

    let mut synthetic_targets = Vec::new();
    let home_staging = if request.home_files.is_empty() {
        None
    } else {
        Some(stage_home_files(&request.home_files)?)
    };
    let mut args = vec![
        "--new-session".to_string(),
        "--die-with-parent".to_string(),
        "--unshare-user".to_string(),
        "--unshare-pid".to_string(),
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
    if let Some(home_staging) = &home_staging {
        args.extend([
            "--ro-bind".to_string(),
            path_string(home_staging)?,
            "/tmp/ace-home".to_string(),
        ]);
    }

    // The helper re-enters itself inside the namespace before executing the
    // user command. If it lives outside a platform/approved root, expose only
    // that exact binary rather than its host directory.
    if !is_visible(&executable, &visible_roots) {
        append_bind(
            &mut args,
            &mut created_target_dirs,
            "--ro-bind",
            &executable,
        )?;
        visible_roots.push(executable.clone());
    }
    if let Some(command_path) = request.command.first().map(PathBuf::from) {
        if command_path.is_absolute()
            && command_path.exists()
            && !is_visible(&command_path, &visible_roots)
        {
            append_bind(
                &mut args,
                &mut created_target_dirs,
                "--ro-bind",
                &command_path,
            )?;
            visible_roots.push(command_path);
        }
    }

    // Re-apply immutable paths after writable roots, so the narrow rule wins.
    for protected in readonly {
        if protected.exists() {
            let value = path_string(&protected)?;
            args.extend(["--ro-bind".to_string(), value.clone(), value]);
        } else {
            // A read-only synthetic directory prevents creation through a writable root.
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
    }
    // Deny entries are applied last and therefore cannot be upgraded by an
    // additional permission or by a narrower protected metadata mount.
    for root in &denied {
        if !is_visible(root, &visible_roots) {
            continue;
        }
        let value = path_string(root)?;
        if root.is_dir() || !root.exists() {
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
    if let Some(socket_dir) = &request.proxy_socket_dir {
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
        home_staging,
    })
}

fn stage_home_files(files: &BTreeMap<String, Vec<u8>>) -> Result<PathBuf, String> {
    let mut suffix = [0_u8; 16];
    rand::thread_rng().fill_bytes(&mut suffix);
    let name = suffix
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let root = std::env::temp_dir().join(format!("ace-sandbox-home-files-{name}"));
    fs::create_dir(&root).map_err(|error| format!("cannot create projected HOME: {error}"))?;
    for (relative_path, content) in files {
        let destination = root.join(relative_path);
        if destination
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
        {
            let _ = fs::remove_dir_all(&root);
            return Err("projected HOME path escapes the staging root".to_string());
        }
        if let Some(parent) = destination.parent() {
            if let Err(error) = fs::create_dir_all(parent) {
                let _ = fs::remove_dir_all(&root);
                return Err(format!("cannot create projected HOME directory: {error}"));
            }
        }
        if let Err(error) = fs::write(&destination, content) {
            let _ = fs::remove_dir_all(&root);
            return Err(format!("cannot stage projected HOME file: {error}"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Err(error) = fs::set_permissions(&destination, fs::Permissions::from_mode(0o600))
            {
                let _ = fs::remove_dir_all(&root);
                return Err(format!("cannot restrict projected HOME file: {error}"));
            }
        }
    }
    Ok(root)
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
            return Err(format!("deny root must be absolute: {}", path.display()));
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
        let value = canonicalize_allow_missing(path)?;
        if !result.contains(&value) {
            result.push(value);
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

fn protected_roots(
    writable: &[PathBuf],
    explicit: &[PathBuf],
) -> Result<BTreeSet<PathBuf>, String> {
    let mut result = BTreeSet::new();
    for root in writable {
        for name in PROTECTED_NAMES {
            result.insert(root.join(name));
        }
    }
    for path in explicit {
        if std::fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            return Err(format!(
                "protected metadata path cannot be a symlink: {}",
                path.display()
            ));
        }
    }
    for root in canonical_or_missing_roots(explicit)? {
        if !writable.iter().any(|candidate| root.starts_with(candidate)) {
            return Err(format!(
                "read-only root must be inside an explicit writable root: {}",
                root.display()
            ));
        }
        result.insert(root);
    }
    for path in &result {
        if std::fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            return Err(format!(
                "protected metadata path cannot be a symlink: {}",
                path.display()
            ));
        }
    }
    Ok(result)
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
            readonly_roots: vec![temp.path().join(".git")],
            denied_roots: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: Default::default(),
            home_files: Default::default(),
        };
        let plan = build_args(&request).unwrap();
        assert!(plan.args.iter().any(|arg| arg == "--unshare-net"));
        assert!(plan.args.iter().any(|arg| arg == "--die-with-parent"));
        assert!(plan.args.iter().any(|arg| arg.ends_with(".git")));
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
            readonly_roots: vec![],
            denied_roots: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: Default::default(),
            home_files: Default::default(),
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
    fn cwd_must_already_be_inside_an_explicit_writable_root() {
        let workspace = tempfile::tempdir().unwrap();
        let request = LinuxRunRequest {
            command: vec!["/bin/true".to_string()],
            cwd: workspace.path().to_path_buf(),
            writable_roots: vec![],
            readable_roots: vec![],
            readonly_roots: vec![],
            denied_roots: vec![],
            network_enabled: false,
            network_rules: vec![],
            allow_local_binding: false,
            proxy_socket_dir: None,
            max_output_bytes: 1024,
            stdin: None,
            env_overrides: Default::default(),
            home_files: Default::default(),
        };

        let error = build_args(&request)
            .err()
            .expect("cwd must not auto-expand policy");

        assert!(error.contains("writable root"), "{error}");
    }
}
