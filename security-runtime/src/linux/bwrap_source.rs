use std::env;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

pub struct BwrapSource {
    executable: PathBuf,
    system: bool,
    _verified_file: Option<File>,
}

impl BwrapSource {
    pub fn executable(&self) -> &Path {
        &self.executable
    }

    pub fn is_system(&self) -> bool {
        self.system
    }
}

/// Select a real system bwrap outside the workspace, then a digest-pinned bundle.
pub fn locate(workspace: &Path) -> Result<BwrapSource, String> {
    if let Some(path) = find_system(workspace) {
        return Ok(BwrapSource {
            executable: path,
            system: true,
            _verified_file: None,
        });
    }
    let bundled = env::var_os("ACE_BUNDLED_BWRAP")
        .map(PathBuf::from)
        .ok_or_else(|| "no trusted system or bundled bubblewrap is available".to_string())?;
    let expected = env::var("ACE_BUNDLED_BWRAP_SHA256")
        .map_err(|_| "bundled bubblewrap has no pinned SHA-256".to_string())?;
    let canonical = bundled
        .canonicalize()
        .map_err(|error| format!("cannot resolve bundled bubblewrap: {error}"))?;
    if !is_executable(&canonical) {
        return Err("bundled bubblewrap is not executable".to_string());
    }
    let mut file = File::open(&canonical)
        .map_err(|error| format!("cannot open bundled bubblewrap: {error}"))?;
    verify_digest(&mut file, &expected)?;
    clear_close_on_exec(file.as_raw_fd())?;
    let executable = PathBuf::from(format!("/proc/self/fd/{}", file.as_raw_fd()));
    Ok(BwrapSource {
        executable,
        system: false,
        _verified_file: Some(file),
    })
}

fn find_system(workspace: &Path) -> Option<PathBuf> {
    let workspace = workspace.canonicalize().ok()?;
    env::split_paths(&env::var_os("PATH")?).find_map(|directory| {
        let candidate = directory.join("bwrap").canonicalize().ok()?;
        if candidate.starts_with(&workspace)
            || !is_executable(&candidate)
            || !is_trusted_system_candidate(&candidate)
        {
            return None;
        }
        // A bounded version probe rejects aliases or unrelated workspace binaries.
        probe_version(&candidate).then_some(candidate)
    })
}

fn probe_version(candidate: &Path) -> bool {
    let Ok(mut child) = std::process::Command::new(candidate)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(None) | Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

fn is_trusted_system_candidate(path: &Path) -> bool {
    for (index, component) in path.ancestors().enumerate() {
        let Ok(metadata) = std::fs::symlink_metadata(component) else {
            return false;
        };
        if metadata.file_type().is_symlink() || metadata.mode() & 0o022 != 0 || metadata.uid() != 0
        {
            return false;
        }
        if (index == 0 && !metadata.is_file()) || (index > 0 && !metadata.is_dir()) {
            return false;
        }
    }
    true
}

fn is_executable(path: &Path) -> bool {
    path.metadata()
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

fn verify_digest(file: &mut File, expected: &str) -> Result<(), String> {
    if expected.len() != 64 || !expected.bytes().all(|value| value.is_ascii_hexdigit()) {
        return Err("invalid bundled bubblewrap SHA-256".to_string());
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|error| format!("cannot seek bundled bubblewrap: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("cannot hash bundled bubblewrap: {error}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let actual = format!("{:x}", hasher.finalize());
    if actual.eq_ignore_ascii_case(expected) {
        Ok(())
    } else {
        Err(format!(
            "bundled bubblewrap digest mismatch: expected {expected}, got {actual}"
        ))
    }
}

fn clear_close_on_exec(fd: i32) -> Result<(), String> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } < 0 {
        return Err(format!(
            "cannot preserve bundled bubblewrap fd: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{is_trusted_system_candidate, probe_version, verify_digest};
    use sha2::{Digest, Sha256};
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn bundled_digest_is_exact() {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"trusted").unwrap();
        let expected = format!("{:x}", Sha256::digest(b"trusted"));
        assert!(verify_digest(&mut file, &expected).is_ok());
        assert!(verify_digest(&mut file, &"0".repeat(64)).is_err());
    }

    #[test]
    fn system_bwrap_candidate_requires_trusted_ownership_and_modes() {
        let system_binary = std::path::Path::new("/bin/true").canonicalize().unwrap();
        assert!(is_trusted_system_candidate(&system_binary));

        let temp = tempfile::tempdir().unwrap();
        let candidate = temp.path().join("bwrap");
        std::fs::copy("/bin/true", &candidate).unwrap();
        std::fs::set_permissions(&candidate, std::fs::Permissions::from_mode(0o777)).unwrap();

        assert!(!is_trusted_system_candidate(&candidate));
    }

    #[test]
    fn user_owned_executable_is_never_a_system_bwrap_candidate() {
        if unsafe { libc::geteuid() } == 0 {
            return;
        }
        let current_test_binary = std::env::current_exe().unwrap().canonicalize().unwrap();

        assert!(
            !is_trusted_system_candidate(&current_test_binary),
            "a caller-controlled executable must use the digest-pinned bundle path"
        );
    }

    #[test]
    fn system_bwrap_version_probe_is_bounded() {
        let temp = tempfile::tempdir().unwrap();
        let candidate = temp.path().join("bwrap");
        std::fs::write(&candidate, "#!/bin/sh\nexec /bin/sleep 60\n").unwrap();
        std::fs::set_permissions(&candidate, std::fs::Permissions::from_mode(0o700)).unwrap();
        let started = std::time::Instant::now();

        assert!(!probe_version(&candidate));
        assert!(started.elapsed() < std::time::Duration::from_secs(5));
    }
}
