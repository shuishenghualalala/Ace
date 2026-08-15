#![cfg(windows)]

use std::fs;

use windows_sys::Win32::Foundation::CloseHandle;
use windows_sys::Win32::Security::{
    GetTokenInformation, IsTokenRestricted, TokenRestrictedSids, TOKEN_GROUPS,
};

#[path = "../src/windows/token.rs"]
#[allow(dead_code)]
mod token;

#[test]
fn token_and_handle_contract_is_restricted() {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let token = fs::read_to_string(format!("{manifest}/src/windows/token.rs")).unwrap();
    let process = fs::read_to_string(format!("{manifest}/src/windows/process.rs")).unwrap();
    assert!(token.contains("CreateRestrictedToken"));
    assert!(token.contains("WRITE_RESTRICTED"));
    assert!(process.contains("PROC_THREAD_ATTRIBUTE_HANDLE_LIST"));
}

#[test]
fn restricted_token_uses_per_run_capability_logon_and_world_sids() {
    let handle = token::create_restricted_token(&[
        "S-1-5-21-123456789-234567890-345678901-456789012".to_string(),
    ])
    .unwrap();
    assert_ne!(unsafe { IsTokenRestricted(handle) }, 0);

    let mut size = 0;
    unsafe {
        GetTokenInformation(
            handle,
            TokenRestrictedSids,
            std::ptr::null_mut(),
            0,
            &mut size,
        )
    };
    assert!(size >= std::mem::size_of::<TOKEN_GROUPS>() as u32);
    let mut buffer = vec![0_u8; size as usize];
    assert_ne!(
        unsafe {
            GetTokenInformation(
                handle,
                TokenRestrictedSids,
                buffer.as_mut_ptr().cast(),
                size,
                &mut size,
            )
        },
        0
    );
    let groups = unsafe { &*(buffer.as_ptr() as *const TOKEN_GROUPS) };
    assert_eq!(groups.GroupCount, 3);
    unsafe { CloseHandle(handle) };
}
