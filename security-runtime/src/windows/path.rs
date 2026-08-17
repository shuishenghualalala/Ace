use std::ffi::OsString;
use std::fs;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::MetadataExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};

use windows_sys::Win32::Foundation::GetLastError;
use windows_sys::Win32::Storage::FileSystem::{
    GetFileInformationByHandle, GetLongPathNameW, BY_HANDLE_FILE_INFORMATION,
    FILE_ATTRIBUTE_REPARSE_POINT,
};

#[derive(Debug)]
pub struct PreparedPathPolicy {
    pub cwd: PathBuf,
    pub writable_roots: Vec<PathBuf>,
    pub readable_roots: Vec<PathBuf>,
    pub denied_roots: Vec<PathBuf>,
}

pub fn prepare_policy(
    cwd: &Path,
    writable_roots: &[PathBuf],
    readable_roots: &[PathBuf],
    denied_roots: &[PathBuf],
) -> Result<PreparedPathPolicy, String> {
    let cwd = canonical_directory(cwd, "working directory")?;
    let writable_roots = canonical_roots(writable_roots, "writable")?;
    let readable_roots = canonical_roots(readable_roots, "readable")?;
    let denied_roots = optional_canonical_roots(denied_roots)?;

    if !writable_roots
        .iter()
        .chain(&readable_roots)
        .any(|root| same_or_descendant(&cwd, root))
    {
        return Err(format!(
            "working directory is outside every authorized root: {}",
            cwd.display()
        ));
    }
    for writable in &writable_roots {
        for readable in &readable_roots {
            if same_or_descendant(writable, readable) {
                return Err(format!(
                    "writable root {} is inside read-only root {}",
                    writable.display(),
                    readable.display()
                ));
            }
        }
    }
    for denied in &denied_roots {
        if same_or_descendant(&cwd, denied) {
            return Err(format!(
                "working directory is inside denied root {}",
                denied.display()
            ));
        }
        for allowed in writable_roots.iter().chain(&readable_roots) {
            if same_or_descendant(allowed, denied) {
                return Err(format!(
                    "authorized root {} is inside denied root {}",
                    allowed.display(),
                    denied.display()
                ));
            }
        }
    }

    for root in writable_roots.iter().chain(&readable_roots) {
        inspect_tree(root)?;
    }

    Ok(PreparedPathPolicy {
        cwd,
        writable_roots,
        readable_roots,
        denied_roots,
    })
}

fn canonical_roots(paths: &[PathBuf], label: &str) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::with_capacity(paths.len());
    for path in paths {
        let canonical = canonical_directory(path, &format!("{label} root"))?;
        if !contains_path(&result, &canonical) {
            result.push(canonical);
        }
    }
    Ok(result)
}

fn optional_canonical_roots(paths: &[PathBuf]) -> Result<Vec<PathBuf>, String> {
    let mut result = Vec::with_capacity(paths.len());
    for path in paths {
        let path = lexical_local_absolute(path)?;
        reject_reparse_components(&path)?;
        let resolved = match canonical_local(&path) {
            Ok(path) => {
                let metadata = fs::metadata(&path).map_err(|error| {
                    format!("cannot inspect denied root {}: {error}", path.display())
                })?;
                if !metadata.is_dir() && !metadata.is_file() {
                    return Err(format!(
                        "denied root is not a filesystem object: {}",
                        path.display()
                    ));
                }
                reject_reparse_components(&path)?;
                path
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                reject_parent_components(&path)?;
                normalize_missing_path(&path)?
            }
            Err(error) => {
                return Err(format!(
                    "cannot resolve denied root {}: {error}",
                    path.display()
                ))
            }
        };
        if !contains_path(&result, &resolved) {
            result.push(resolved);
        }
    }
    Ok(result)
}

fn lexical_local_absolute(path: &Path) -> Result<PathBuf, String> {
    validate_local_absolute(path)?;
    let mut components = path.components();
    let drive = match components.next() {
        Some(Component::Prefix(prefix)) => match prefix.kind() {
            Prefix::Disk(drive) => drive,
            _ => unreachable!("validate_local_absolute accepts disk prefixes only"),
        },
        _ => unreachable!("validate_local_absolute requires a prefix"),
    };
    if !matches!(components.next(), Some(Component::RootDir)) {
        unreachable!("validate_local_absolute requires a root component");
    }
    let mut suffix = Vec::new();
    for component in components {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if suffix.pop().is_none() {
                    return Err(format!(
                        "Windows sandbox path escapes its drive root: {}",
                        path.display()
                    ));
                }
            }
            Component::Normal(value) => {
                let value = value.to_string_lossy();
                if value.contains(':') || value.contains('*') || value.contains('?') {
                    return Err(format!(
                        "Windows sandbox rejects alternate streams and wildcard names: {}",
                        path.display()
                    ));
                }
                suffix.push(OsString::from(value.as_ref()));
            }
            _ => {
                return Err(format!(
                    "Windows sandbox path has an unsupported component: {}",
                    path.display()
                ))
            }
        }
    }
    let mut normalized = PathBuf::from(format!("{}:\\", drive as char));
    normalized.extend(suffix);
    Ok(normalized)
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, String> {
    validate_local_absolute(path)?;
    reject_reparse_components(path)?;
    let canonical = canonical_local(path)
        .map_err(|error| format!("cannot resolve {label} {}: {error}", path.display()))?;
    reject_reparse_components(&canonical)?;
    let metadata = fs::metadata(&canonical)
        .map_err(|error| format!("cannot inspect {label} {}: {error}", canonical.display()))?;
    if !metadata.is_dir() {
        return Err(format!(
            "{label} is not a directory: {}",
            canonical.display()
        ));
    }
    Ok(canonical)
}

fn canonical_local(path: &Path) -> Result<PathBuf, std::io::Error> {
    let canonical = long_path(&path.canonicalize()?)?;
    let rendered = canonical.as_os_str().to_string_lossy();
    if let Some(local) = rendered.strip_prefix(r"\\?\") {
        if local.as_bytes().get(1) == Some(&b':') {
            return Ok(PathBuf::from(local));
        }
    }
    Ok(canonical)
}

fn long_path(path: &Path) -> Result<PathBuf, std::io::Error> {
    let input: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut capacity = 260_u32;
    loop {
        let mut output = vec![0_u16; capacity as usize];
        let length = unsafe { GetLongPathNameW(input.as_ptr(), output.as_mut_ptr(), capacity) };
        if length == 0 {
            return Err(std::io::Error::last_os_error());
        }
        if length < capacity {
            return Ok(PathBuf::from(OsString::from_wide(
                &output[..length as usize],
            )));
        }
        let next = length.saturating_add(1);
        if next <= capacity {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "Windows long path length overflow",
            ));
        }
        capacity = next;
    }
}

pub(crate) fn validate_local_absolute(path: &Path) -> Result<(), String> {
    if path.as_os_str().is_empty() || path.as_os_str().to_string_lossy().contains('\0') {
        return Err("Windows sandbox path is empty or contains NUL".to_string());
    }
    let mut components = path.components();
    let prefix = match components.next() {
        Some(Component::Prefix(prefix)) => prefix.kind(),
        _ => {
            return Err(format!(
                "Windows sandbox path must be drive-absolute: {}",
                path.display()
            ))
        }
    };
    if !matches!(prefix, Prefix::Disk(_)) || !matches!(components.next(), Some(Component::RootDir))
    {
        return Err(format!(
            "Windows sandbox rejects drive-relative, UNC, and device paths: {}",
            path.display()
        ));
    }
    for component in components {
        if let Component::Normal(value) = component {
            let value = value.to_string_lossy();
            if value.contains(':') || value.contains('*') || value.contains('?') {
                return Err(format!(
                    "Windows sandbox rejects alternate streams and wildcard names: {}",
                    path.display()
                ));
            }
        }
    }
    Ok(())
}

pub(crate) fn reject_reparse_components(path: &Path) -> Result<(), String> {
    for component in path.ancestors().collect::<Vec<_>>().into_iter().rev() {
        let metadata = match fs::symlink_metadata(component) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(format!(
                    "cannot inspect Windows sandbox path {}: {error}",
                    component.display()
                ))
            }
        };
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!(
                "Windows sandbox path cannot contain a reparse point: {}",
                component.display()
            ));
        }
    }
    Ok(())
}

fn reject_parent_components(path: &Path) -> Result<(), String> {
    let mut parent = path.parent();
    while let Some(candidate) = parent {
        match fs::symlink_metadata(candidate) {
            Ok(_) => return reject_reparse_components(candidate),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                parent = candidate.parent();
            }
            Err(error) => {
                return Err(format!(
                    "cannot inspect denied-root parent {}: {error}",
                    candidate.display()
                ))
            }
        }
    }
    Err(format!(
        "denied root has no inspectable parent: {}",
        path.display()
    ))
}

fn normalize_missing_path(path: &Path) -> Result<PathBuf, String> {
    let mut existing = path.to_path_buf();
    let mut missing = Vec::new();
    loop {
        match fs::symlink_metadata(&existing) {
            Ok(metadata) => {
                if !metadata.is_dir() {
                    return Err(format!(
                        "denied-root parent is not a directory: {}",
                        existing.display()
                    ));
                }
                reject_reparse_components(&existing)?;
                let mut normalized = canonical_local(&existing)
                    .map_err(|error| format!("cannot resolve denied-root parent: {error}"))?;
                for component in missing.iter().rev() {
                    normalized.push(component);
                }
                return Ok(normalized);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = existing.file_name().ok_or_else(|| {
                    format!("denied root has no inspectable parent: {}", path.display())
                })?;
                missing.push(name.to_os_string());
                existing = existing
                    .parent()
                    .ok_or_else(|| {
                        format!("denied root has no inspectable parent: {}", path.display())
                    })?
                    .to_path_buf();
            }
            Err(error) => {
                return Err(format!(
                    "cannot inspect denied-root parent {}: {error}",
                    existing.display()
                ));
            }
        }
    }
}

fn inspect_tree(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect authorized tree {}: {error}", path.display()))?;
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(format!(
            "authorized tree cannot contain a reparse point: {}",
            path.display()
        ));
    }
    if metadata.is_file() {
        return reject_multiple_links(path);
    }
    if !metadata.is_dir() {
        return Err(format!(
            "authorized tree contains an unsupported object: {}",
            path.display()
        ));
    }
    let entries = fs::read_dir(path).map_err(|error| {
        format!(
            "cannot enumerate authorized tree {}: {error}",
            path.display()
        )
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            format!(
                "cannot enumerate authorized tree entry under {}: {error}",
                path.display()
            )
        })?;
        inspect_tree(&entry.path())?;
    }
    Ok(())
}

fn reject_multiple_links(path: &Path) -> Result<(), String> {
    let file = fs::File::open(path)
        .map_err(|error| format!("cannot open authorized file {}: {error}", path.display()))?;
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as isize, &mut information) } == 0 {
        return Err(format!(
            "cannot inspect authorized file identity {}: {}",
            path.display(),
            unsafe { GetLastError() }
        ));
    }
    if information.nNumberOfLinks != 1 {
        return Err(format!(
            "authorized file has multiple hard links: {}",
            path.display()
        ));
    }
    Ok(())
}

fn contains_path(paths: &[PathBuf], candidate: &Path) -> bool {
    paths
        .iter()
        .any(|path| normalized(path) == normalized(candidate))
}

fn same_or_descendant(path: &Path, root: &Path) -> bool {
    let path = normalized(path);
    let root = normalized(root);
    path == root
        || path
            .strip_prefix(&root)
            .is_some_and(|suffix| suffix.starts_with('\\'))
}

fn normalized(path: &Path) -> String {
    path.as_os_str()
        .to_string_lossy()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}
