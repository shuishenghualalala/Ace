#![cfg(windows)]

use std::path::PathBuf;
use std::process::Command;

#[test]
fn mismatched_setup_owner_has_distinct_exit_without_creating_state() {
    let parent = tempfile::tempdir().unwrap();
    let state_dir = parent.path().join("security");
    let status = Command::new(env!("CARGO_BIN_EXE_ace-security-runtime"))
        .arg("--windows-setup")
        .arg(&state_dir)
        .args(["--expected-owner-sid", "S-1-1-0"])
        .status()
        .unwrap();

    assert_eq!(status.code(), Some(4));
    assert!(!state_dir.exists());
}

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
