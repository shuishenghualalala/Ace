#![cfg(windows)]

use std::fs;

#[test]
fn acl_contract_merges_and_revokes_only_ace_principals() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let acl = fs::read_to_string(format!("{manifest}/src/windows/acl.rs")).unwrap();
    assert!(acl.contains("GetSecurityInfo"));
    assert!(acl.contains("SetEntriesInAclW"));
    assert!(acl.contains("SetSecurityInfo"));
    assert!(acl.contains("CreateFileW"));
    assert!(acl.contains("REVOKE_ACCESS") || acl.contains("apply_entry(path, sid, 4, 0)"));
    assert!(!acl.contains("SetFileSecurityW"));
}
