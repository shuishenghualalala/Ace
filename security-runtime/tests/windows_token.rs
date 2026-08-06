#![cfg(windows)]

use std::fs;

#[test]
fn token_and_handle_contract_is_restricted() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let token = fs::read_to_string(format!("{manifest}/src/windows/token.rs")).unwrap();
    let process = fs::read_to_string(format!("{manifest}/src/windows/process.rs")).unwrap();
    assert!(token.contains("CreateRestrictedToken"));
    assert!(token.contains("WRITE_RESTRICTED"));
    assert!(process.contains("PROC_THREAD_ATTRIBUTE_HANDLE_LIST"));
}
