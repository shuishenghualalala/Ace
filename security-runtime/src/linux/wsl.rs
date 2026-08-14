use std::fs;

/// Return Some(1/2) for WSL and None for native Linux.
pub fn detect() -> Option<u8> {
    let release = fs::read_to_string("/proc/sys/kernel/osrelease")
        .or_else(|_| fs::read_to_string("/proc/version"))
        .ok()?
        .to_ascii_lowercase();
    if !release.contains("microsoft") {
        return None;
    }
    Some(
        if release.contains("wsl2") || release.contains("microsoft-standard") {
            2
        } else {
            1
        },
    )
}
