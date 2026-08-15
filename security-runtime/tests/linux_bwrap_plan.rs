//! Compile and exercise the Linux bwrap plan builder on every development host.
//!
//! Native namespace behavior still requires Linux, but this catches Rust/type
//! regressions in Linux-only code during the normal Windows test suite.

mod linux {
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    pub enum FilesystemGlobAccess {
        DenyRead,
    }

    #[allow(dead_code)]
    pub struct FilesystemGlobRule {
        pub root: String,
        pub pattern: String,
        pub access: FilesystemGlobAccess,
    }

    #[allow(dead_code)]
    pub struct LinuxRunRequest {
        pub command: Vec<String>,
        pub cwd: PathBuf,
        pub writable_roots: Vec<PathBuf>,
        pub readable_roots: Vec<PathBuf>,
        pub denied_roots: Vec<PathBuf>,
        pub filesystem_globs: Vec<FilesystemGlobRule>,
        pub network_enabled: bool,
        pub network_rules: Vec<()>,
        pub allow_local_binding: bool,
        pub proxy_socket_dir: Option<PathBuf>,
        pub max_output_bytes: usize,
        pub stdin: Option<Vec<u8>>,
        pub stdin_stream: Option<()>,
        pub env_overrides: BTreeMap<String, String>,
    }

    pub mod proxy_routing {
        pub const INNER_SOCKET_PATH: &str = "/run/ace-network/proxy.sock";
    }

    pub mod bwrap {
        include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/linux/bwrap.rs"));
    }
}

fn request(
    cwd: &std::path::Path,
    writable_roots: Vec<std::path::PathBuf>,
) -> linux::LinuxRunRequest {
    linux::LinuxRunRequest {
        command: vec!["true".to_string()],
        cwd: cwd.to_path_buf(),
        writable_roots,
        readable_roots: vec![],
        denied_roots: vec![],
        filesystem_globs: vec![],
        network_enabled: false,
        network_rules: vec![],
        allow_local_binding: false,
        proxy_socket_dir: None,
        max_output_bytes: 1024,
        stdin: None,
        stdin_stream: None,
        env_overrides: Default::default(),
    }
}

#[test]
fn full_filesystem_write_is_rejected_instead_of_weakening_the_backend() {
    let workspace = tempfile::tempdir().unwrap();
    let filesystem_root = workspace
        .path()
        .ancestors()
        .last()
        .expect("temporary path has a filesystem root")
        .to_path_buf();

    let error = linux::bwrap::build_args(&request(workspace.path(), vec![filesystem_root]))
        .err()
        .expect("a writable filesystem root must fail closed");

    assert!(error.contains("filesystem root"), "{error}");
}

#[test]
fn managed_network_without_a_private_proxy_bridge_is_rejected_during_planning() {
    let workspace = tempfile::tempdir().unwrap();
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.network_enabled = true;

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("managed network without its private bridge must fail closed");

    assert!(error.contains("proxy bridge"), "{error}");
}

#[test]
fn profile_isolates_host_ipc_and_uts_namespaces() {
    let workspace = tempfile::tempdir().unwrap();
    let plan = linux::bwrap::build_args(&request(
        workspace.path(),
        vec![workspace.path().to_path_buf()],
    ))
    .unwrap();

    assert!(plan.args.iter().any(|argument| argument == "--unshare-ipc"));
    assert!(plan.args.iter().any(|argument| argument == "--unshare-uts"));
}

#[test]
fn inner_runtime_helper_is_read_only_even_below_a_writable_root() {
    let helper = std::env::current_exe().unwrap().canonicalize().unwrap();
    let writable_root = helper.parent().unwrap().to_path_buf();
    let plan =
        linux::bwrap::build_args(&request(&writable_root, vec![writable_root.clone()])).unwrap();
    let helper_text = helper.to_string_lossy();

    assert!(
        plan.args.windows(3).any(|window| {
            window[0] == "--ro-bind" && window[1] == helper_text && window[2] == helper_text
        }),
        "the trusted inner helper must overlay any broader writable bind"
    );
}

#[test]
fn offline_profile_never_mounts_a_proxy_bridge() {
    let workspace = tempfile::tempdir().unwrap();
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.proxy_socket_dir = Some(workspace.path().to_path_buf());

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("offline profiles must not inherit managed-network resources");

    assert!(error.contains("offline sandbox"), "{error}");
}

#[test]
fn missing_deny_root_with_parent_traversal_is_rejected_before_mount_planning() {
    let workspace = tempfile::tempdir().unwrap();
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.denied_roots = vec![workspace.path().join("missing").join("..").join("secret")];

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("ambiguous missing deny roots must fail closed");

    assert!(error.contains("normalized absolute"), "{error}");
}

#[test]
fn cwd_hidden_by_a_deny_root_is_rejected_before_bwrap_spawn() {
    let workspace = tempfile::tempdir().unwrap();
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.denied_roots = vec![workspace.path().to_path_buf()];

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("bwrap must not discover an unusable cwd after spawn");

    assert!(error.contains("cwd"), "{error}");
    assert!(error.contains("deny root"), "{error}");
}

#[test]
fn managed_network_rejects_a_missing_proxy_mount_source() {
    let workspace = tempfile::tempdir().unwrap();
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.network_enabled = true;
    request.proxy_socket_dir = Some(workspace.path().join("missing-proxy-bridge"));

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("missing bind sources must be rejected during planning");

    assert!(error.contains("proxy bridge directory"), "{error}");
}

#[test]
fn dropping_an_unspawned_plan_never_removes_a_concurrently_created_path() {
    let workspace = tempfile::tempdir().unwrap();
    let plan = linux::bwrap::build_args(&request(
        workspace.path(),
        vec![workspace.path().to_path_buf()],
    ))
    .unwrap();
    let protected = workspace.path().join(".git");
    std::fs::create_dir(&protected).unwrap();

    drop(plan);

    assert!(
        protected.is_dir(),
        "planning alone must never claim ownership of host paths"
    );
}

#[test]
fn synthetic_target_cleanup_reports_content_instead_of_silently_succeeding() {
    let workspace = tempfile::tempdir().unwrap();
    let mut plan = linux::bwrap::build_args(&request(
        workspace.path(),
        vec![workspace.path().to_path_buf()],
    ))
    .unwrap();
    plan.mark_spawned();
    let protected = workspace.path().join(".git");
    std::fs::create_dir(&protected).unwrap();
    std::fs::write(protected.join("unexpected"), "content").unwrap();

    let error = plan
        .cleanup()
        .expect_err("non-empty synthetic targets must be a terminal cleanup failure");

    assert!(error.contains(".git"), "{error}");
    assert!(protected.join("unexpected").is_file());
}

#[test]
fn missing_deny_mount_targets_are_removed_after_a_spawned_plan() {
    let workspace = tempfile::tempdir().unwrap();
    let denied_parent = workspace.path().join("missing-parent");
    let denied = denied_parent.join("blocked");
    let mut request = request(workspace.path(), vec![workspace.path().to_path_buf()]);
    request.denied_roots = vec![denied.clone()];
    let mut plan = linux::bwrap::build_args(&request).unwrap();
    plan.mark_spawned();

    // bubblewrap creates mount targets below writable binds on the host. Model
    // that side effect so cleanup must remove every synthetic component, not
    // just protected .git/.agents/.crew targets.
    std::fs::create_dir_all(&denied).unwrap();
    plan.cleanup().unwrap();

    assert!(!denied.exists());
    assert!(!denied_parent.exists());
}

#[test]
fn deny_read_glob_masks_root_and_nested_matches() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path().canonicalize().unwrap();
    let nested_dir = root.join("nested");
    std::fs::create_dir(&nested_dir).unwrap();
    let root_match = root.join("root.pem");
    let nested_match = nested_dir.join("nested.pem");
    std::fs::write(&root_match, "root secret").unwrap();
    std::fs::write(&nested_match, "nested secret").unwrap();
    let mut request = request(&root, vec![root.clone()]);
    request.filesystem_globs = vec![linux::FilesystemGlobRule {
        root: root.to_string_lossy().into_owned(),
        pattern: "**/*.pem".to_string(),
        access: linux::FilesystemGlobAccess::DenyRead,
    }];

    let plan = linux::bwrap::build_args(&request).unwrap();
    for blocked in [root_match, nested_match] {
        let blocked = blocked.to_string_lossy();
        assert!(
            plan.args.windows(3).any(|window| {
                window[0] == "--ro-bind" && window[1] == "/dev/null" && window[2] == blocked
            }),
            "missing deny mask for {blocked}: {:?}",
            plan.args
        );
    }
}

#[test]
fn deny_read_glob_expansion_accepts_8192_and_rejects_8193_before_spawn() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path().canonicalize().unwrap();
    for index in 0..linux::bwrap::MAX_DENY_READ_GLOB_MATCHES {
        std::fs::File::create(root.join(format!("secret-{index:04}.pem"))).unwrap();
    }
    let mut request = request(&root, vec![root.clone()]);
    request.filesystem_globs = vec![linux::FilesystemGlobRule {
        root: root.to_string_lossy().into_owned(),
        pattern: "*.pem".to_string(),
        access: linux::FilesystemGlobAccess::DenyRead,
    }];

    let plan = linux::bwrap::build_args(&request)
        .expect("exactly 8192 expanded deny roots must remain enforceable");
    let last_allowed = root
        .join(format!(
            "secret-{:04}.pem",
            linux::bwrap::MAX_DENY_READ_GLOB_MATCHES - 1
        ))
        .to_string_lossy()
        .into_owned();
    assert!(plan.args.iter().any(|argument| argument == &last_allowed));
    drop(plan);

    std::fs::File::create(root.join(format!(
        "secret-{:04}.pem",
        linux::bwrap::MAX_DENY_READ_GLOB_MATCHES
    )))
    .unwrap();
    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("8193 glob matches must stop planning before bwrap can spawn");

    assert!(
        error.contains("more than 8192 paths"),
        "unexpected overflow error: {error}"
    );
}

#[test]
fn invalid_deny_read_glob_pattern_is_rejected_during_planning() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path().canonicalize().unwrap();
    let mut request = request(&root, vec![root.clone()]);
    request.filesystem_globs = vec![linux::FilesystemGlobRule {
        root: root.to_string_lossy().into_owned(),
        pattern: "[unterminated".to_string(),
        access: linux::FilesystemGlobAccess::DenyRead,
    }];

    let error = linux::bwrap::build_args(&request)
        .err()
        .expect("invalid glob syntax must fail before bwrap spawn");

    assert!(error.contains("unclosed class"), "{error}");
}
