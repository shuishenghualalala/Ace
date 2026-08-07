#![cfg(windows)]

use std::path::PathBuf;

#[test]
fn native_windows_gate_requires_installed_fixture() {
    let required = std::env::var("ACE_REQUIRE_NATIVE_TESTS").as_deref() == Ok("1");
    let Some(state_dir) = std::env::var_os("ACE_WINDOWS_NATIVE_STATE_DIR") else {
        assert!(
            !required,
            "release gate requires an installed sandbox identity fixture"
        );
        return;
    };
    let state_dir = PathBuf::from(state_dir);
    assert!(state_dir.is_absolute());
    assert!(state_dir.join("windows-sandbox-identity.json").is_file());
}
