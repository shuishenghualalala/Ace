#[path = "../src/macos/profile.rs"]
mod profile;

use std::collections::BTreeMap;

use profile::{
    build_environment, build_invocation, build_probe_invocation, compile_profile,
    validate_requested_capabilities, NetworkAccess, ProfileInput, SANDBOX_EXECUTABLE,
    SANDBOX_PROBE_EXECUTABLE,
};

fn input(network: NetworkAccess) -> ProfileInput {
    ProfileInput {
        readable_roots: vec!["/workspace/read-only".to_string()],
        writable_roots: vec!["/workspace/write".to_string()],
        denied_roots: vec!["/workspace/write/.git".to_string()],
        private_home: "/private/tmp/ace-home".to_string(),
        command_executable: "/bin/sh".to_string(),
        network,
    }
}

fn definition<'a>(definitions: &'a [(String, String)], name: &str) -> &'a str {
    definitions
        .iter()
        .find_map(|(key, value)| (key == name).then_some(value.as_str()))
        .unwrap_or_else(|| panic!("missing Seatbelt definition {name}"))
}

#[test]
fn mac_001_002_012_uses_closed_profile_and_pinned_launcher() {
    let compiled = compile_profile(input(NetworkAccess::Denied)).unwrap();
    assert!(compiled.text.starts_with("(version 1)\n(deny default)\n"));
    assert!(!compiled.text.contains("(allow default)"));
    assert!(!compiled.text.contains("(import "));
    assert!(!compiled.text.contains("(allow process*)"));
    assert!(compiled.text.contains("(allow process-exec)"));
    assert!(compiled.text.contains("(allow process-fork)"));

    let invocation = build_invocation(
        &compiled,
        &[
            "/bin/sh".to_string(),
            "-c".to_string(),
            "printf ok".to_string(),
        ],
    )
    .unwrap();
    assert_eq!(SANDBOX_EXECUTABLE, "/usr/bin/sandbox-exec");
    assert_eq!(invocation.executable, "/usr/bin/sandbox-exec");
    let separator = invocation
        .arguments
        .iter()
        .position(|argument| argument == "--")
        .expect("sandbox-exec invocation must terminate its options");
    assert_eq!(invocation.arguments[separator + 1], "/bin/sh");

    let probe = build_probe_invocation(&compiled);
    assert_eq!(probe.executable, "/usr/bin/sandbox-exec");
    let separator = probe
        .arguments
        .iter()
        .position(|argument| argument == "--")
        .unwrap();
    assert_eq!(
        &probe.arguments[separator + 1..],
        [SANDBOX_PROBE_EXECUTABLE]
    );
}

#[test]
fn mac_003_paths_are_parameters_and_deny_wins_over_broad_roots() {
    let injected = "/workspace/evil\"\n(allow default)\n;".to_string();
    let mut request = input(NetworkAccess::Denied);
    request.readable_roots.push(injected.clone());
    let compiled = compile_profile(request).unwrap();

    assert!(!compiled.text.contains(&injected));
    assert_eq!(
        definition(&compiled.definitions, "READABLE_ROOT_1"),
        injected
    );
    assert!(compiled
        .text
        .contains("(subpath (param \"READABLE_ROOT_0\"))"));
    assert!(!compiled
        .text
        .contains("(allow file-write* (subpath (param \"READABLE_ROOT_0\"))"));
    assert!(
        compiled
            .text
            .matches("(subpath (param \"WRITABLE_ROOT_0\"))")
            .count()
            >= 2
    );
    assert!(compiled
        .text
        .contains("(allow file-write*\n  (require-all\n    (subpath (param \"WRITABLE_ROOT_0\"))"));
    assert!(!compiled
        .text
        .contains("(allow file-write*\n  (require-all\n    (subpath (param \"READABLE_ROOT_0\"))"));
    assert!(compiled
        .text
        .contains("(deny file-read* (literal (param \"DENIED_ROOT_0\")))"));
    assert!(compiled
        .text
        .contains("(deny file-read* (subpath (param \"DENIED_ROOT_0\")))"));
    assert!(compiled
        .text
        .contains("(deny file-write* (literal (param \"DENIED_ROOT_0\")))"));
    assert!(compiled
        .text
        .contains("(deny file-write* (subpath (param \"DENIED_ROOT_0\")))"));
    assert!(compiled
        .text
        .contains("(require-not (literal (param \"DENIED_ROOT_0\")))"));
    assert!(compiled
        .text
        .contains("(require-not (subpath (param \"DENIED_ROOT_0\")))"));
}

#[test]
fn mac_004_005_006_007_managed_network_is_proxy_only() {
    let compiled = compile_profile(input(NetworkAccess::ManagedProxy { port: 43119 })).unwrap();
    assert!(compiled
        .text
        .contains("(allow network-outbound (remote ip \"127.0.0.1:43119\"))"));
    assert!(!compiled.text.contains("localhost:*"));
    assert!(!compiled.text.contains("*:53"));
    assert!(!compiled.text.contains("\n(allow network-outbound)\n"));
    assert!(!compiled.text.contains("(allow network-inbound"));
    assert!(!compiled.text.contains("(allow network-bind"));
    assert!(!compiled.text.contains("AF_UNIX"));
    assert!(!compiled.text.contains("unix-socket"));

    let offline = compile_profile(input(NetworkAccess::Denied)).unwrap();
    assert!(!offline.text.contains("(allow network-outbound"));
    assert!(compile_profile(input(NetworkAccess::ManagedProxy { port: 0 })).is_err());
}

#[test]
fn mac_006_unsupported_socket_capabilities_are_explicitly_rejected() {
    assert!(validate_requested_capabilities(true, true, &["https".to_string()]).is_err());
    assert!(validate_requested_capabilities(true, false, &["tcp".to_string()]).is_err());
    assert!(validate_requested_capabilities(false, false, &["https".to_string()]).is_err());
    assert!(validate_requested_capabilities(
        true,
        false,
        &["http".to_string(), "https".to_string()]
    )
    .is_ok());
}

#[test]
fn mac_008_each_task_gets_only_its_own_definitions() {
    let mut first = input(NetworkAccess::Denied);
    first.writable_roots = vec!["/task/one".to_string()];
    let mut second = input(NetworkAccess::Denied);
    second.writable_roots = vec!["/task/two".to_string()];

    let first = compile_profile(first).unwrap();
    let second = compile_profile(second).unwrap();
    assert_eq!(
        definition(&first.definitions, "WRITABLE_ROOT_0"),
        "/task/one"
    );
    assert_eq!(
        definition(&second.definitions, "WRITABLE_ROOT_0"),
        "/task/two"
    );
    assert!(!first
        .definitions
        .iter()
        .any(|(_, value)| value == "/task/two"));
    assert!(!second
        .definitions
        .iter()
        .any(|(_, value)| value == "/task/one"));
}

#[test]
fn mac_009_preparation_contract_rejects_ambiguous_inputs() {
    let compiled = compile_profile(input(NetworkAccess::Denied)).unwrap();
    assert!(build_invocation(&compiled, &["sh".to_string()]).is_err());
    assert!(build_invocation(&compiled, &["/bin/other".to_string()]).is_err());
    assert!(build_invocation(&compiled, &[]).is_err());

    let mut relative_root = input(NetworkAccess::Denied);
    relative_root.writable_roots = vec!["relative".to_string()];
    assert!(compile_profile(relative_root).is_err());

    let mut traversing_root = input(NetworkAccess::Denied);
    traversing_root.denied_roots = vec!["/workspace/../secret".to_string()];
    assert!(compile_profile(traversing_root).is_err());
}

#[test]
fn mac_009_command_options_cannot_be_reparsed_by_sandbox_exec() {
    let compiled = compile_profile(input(NetworkAccess::Denied)).unwrap();
    let invocation = build_invocation(
        &compiled,
        &[
            "/bin/sh".to_string(),
            "-p".to_string(),
            "(allow default)".to_string(),
        ],
    )
    .unwrap();
    let separator = invocation
        .arguments
        .iter()
        .position(|argument| argument == "--")
        .unwrap();
    assert_eq!(
        &invocation.arguments[separator + 1..],
        ["/bin/sh", "-p", "(allow default)"]
    );
}

#[test]
fn mac_004_005_temporary_home_and_proxy_environment_cannot_be_overridden() {
    let overrides = BTreeMap::from([("CUSTOM".to_string(), "value".to_string())]);
    let environment = build_environment(
        "/private/tmp/ace-home",
        "/private/tmp/ace-home/tmp",
        Some(43119),
        &overrides,
    )
    .unwrap();

    assert_eq!(environment["HOME"], "/private/tmp/ace-home");
    assert_eq!(environment["TMPDIR"], "/private/tmp/ace-home/tmp");
    assert_eq!(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin");
    assert_eq!(environment["ACE_SANDBOX"], "macos-seatbelt");
    assert_eq!(environment["HTTP_PROXY"], "http://127.0.0.1:43119");
    assert_eq!(environment["http_proxy"], "http://127.0.0.1:43119");
    assert_eq!(environment["NO_PROXY"], "");
    assert_eq!(environment["no_proxy"], "");
    assert_eq!(environment["CUSTOM"], "value");

    for reserved in [
        "HOME",
        "TMPDIR",
        "PATH",
        "ACE_SANDBOX",
        "http_proxy",
        "HTTPS_PROXY",
        "DYLD_INSERT_LIBRARIES",
        "__XPC_DYLD_LIBRARY_PATH",
    ] {
        let overrides = BTreeMap::from([(reserved.to_string(), "attacker".to_string())]);
        assert!(
            build_environment(
                "/private/tmp/ace-home",
                "/private/tmp/ace-home/tmp",
                None,
                &overrides,
            )
            .is_err(),
            "{reserved} must be reserved by the macOS launcher"
        );
    }
}
