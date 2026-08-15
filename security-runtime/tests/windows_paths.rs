#![cfg(windows)]

use std::fs;
use std::path::PathBuf;
use std::process::Command;

#[path = "../src/windows/path.rs"]
mod path;

#[test]
fn cwd_is_canonicalized_and_must_be_authorized() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    fs::create_dir(&workspace).unwrap();
    let lexical_cwd = workspace.join("nested").join("..");
    fs::create_dir(workspace.join("nested")).unwrap();

    let prepared =
        path::prepare_policy(&lexical_cwd, std::slice::from_ref(&workspace), &[], &[]).unwrap();

    assert_eq!(prepared.cwd, workspace);
    assert_eq!(prepared.writable_roots.len(), 1);
    assert!(prepared.readable_roots.is_empty());
    assert!(prepared.denied_roots.is_empty());
    assert!(path::prepare_policy(root.path(), &[workspace], &[], &[]).is_err());
}

#[test]
fn cwd_junction_is_rejected_without_falling_back_to_the_original_path() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("target");
    let junction = root.path().join("junction");
    fs::create_dir(&target).unwrap();
    let status = Command::new("cmd.exe")
        .args(["/d", "/c", "mklink", "/J"])
        .arg(&junction)
        .arg(&target)
        .status()
        .unwrap();
    assert!(status.success(), "junction setup failed");

    let error =
        path::prepare_policy(&junction, std::slice::from_ref(&junction), &[], &[]).unwrap_err();

    assert!(error.contains("reparse point"));
    fs::remove_dir(&junction).unwrap();
}

#[test]
fn writable_tree_rejects_hardlinks_and_nested_reparse_points() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    fs::create_dir(&workspace).unwrap();
    let outside = root.path().join("outside.txt");
    fs::write(&outside, b"outside").unwrap();
    fs::hard_link(&outside, workspace.join("hardlink.txt")).unwrap();

    let error =
        path::prepare_policy(&workspace, std::slice::from_ref(&workspace), &[], &[]).unwrap_err();

    assert!(error.contains("multiple hard links"));
}

#[test]
fn deny_precedence_rejects_reopened_descendants_but_allows_denied_children() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let denied_child = workspace.join("denied");
    let reopened_child = denied_child.join("reopened");
    fs::create_dir_all(&reopened_child).unwrap();

    path::prepare_policy(
        &workspace,
        std::slice::from_ref(&workspace),
        &[],
        std::slice::from_ref(&denied_child),
    )
    .unwrap();

    let error = path::prepare_policy(
        &reopened_child,
        std::slice::from_ref(&reopened_child),
        &[],
        &[denied_child],
    )
    .unwrap_err();
    assert!(error.contains("inside denied root"));

    let error = path::prepare_policy(
        &reopened_child,
        std::slice::from_ref(&reopened_child),
        std::slice::from_ref(&workspace),
        &[],
    )
    .unwrap_err();
    assert!(error.contains("inside read-only root"));
}

#[test]
fn unsupported_windows_path_namespaces_fail_closed() {
    for path in [
        PathBuf::from(r"C:relative"),
        PathBuf::from(r"\\server\share\path"),
        PathBuf::from(r"\\.\PIPE\ace"),
        PathBuf::from(r"\\?\C:\Windows"),
    ] {
        assert!(path::validate_local_absolute(&path).is_err());
    }
}

#[test]
fn missing_denied_roots_are_lexically_normalized() {
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("workspace");
    fs::create_dir(&workspace).unwrap();
    let denied = workspace.join("missing-parent").join("..").join("future");

    let prepared =
        path::prepare_policy(&workspace, std::slice::from_ref(&workspace), &[], &[denied]).unwrap();

    assert_eq!(prepared.denied_roots, vec![workspace.join("future")]);
}
