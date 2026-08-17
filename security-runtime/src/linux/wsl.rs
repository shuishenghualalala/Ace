use std::fs;

/// Return Some(1/2) for WSL and None for native Linux.
pub fn detect() -> Option<u8> {
    let release = fs::read_to_string("/proc/sys/kernel/osrelease")
        .or_else(|_| fs::read_to_string("/proc/version"))
        .ok()?
        .to_ascii_lowercase();
    classify_release(&release)
}

fn classify_release(release: &str) -> Option<u8> {
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

#[cfg(test)]
mod tests {
    use super::classify_release;

    #[test]
    fn classifies_native_wsl1_and_wsl2_without_a_fallback_path() {
        assert_eq!(
            classify_release("5.15.90.1-microsoft-standard-WSL2"),
            Some(2)
        );
        assert_eq!(classify_release("4.4.0-Microsoft"), Some(1));
        assert_eq!(classify_release("6.8.0-generic"), None);
    }
}
