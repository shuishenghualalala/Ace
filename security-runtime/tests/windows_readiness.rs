#![cfg(windows)]

use std::path::PathBuf;

#[test]
#[ignore = "requires an installed Windows sandbox fixture"]
fn native_windows_gate_requires_installed_fixture() {
    let state_dir = PathBuf::from(
        std::env::var_os("ACE_WINDOWS_NATIVE_STATE_DIR")
            .expect("ACE_WINDOWS_NATIVE_STATE_DIR must name an installed sandbox fixture"),
    );
    assert!(state_dir.is_absolute());
    assert!(state_dir.join("windows-sandbox-identity.json").is_file());
}
