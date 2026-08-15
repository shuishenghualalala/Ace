"""Windows source gates complement mandatory native target tests."""

from pathlib import Path


WINDOWS = Path(__file__).parents[2] / "security-runtime" / "src" / "windows"
WINDOWS_TESTS = Path(__file__).parents[2] / "security-runtime" / "tests"


def _read(name: str) -> str:
    return (WINDOWS / name).read_text(encoding="utf-8")


def test_windows_uses_dedicated_identity_and_dpapi_not_current_user_only():
    identity = _read("identity.rs")
    process = _read("process.rs")
    assert "NetUserAdd" in identity
    assert "CryptProtectData" in identity
    assert "CryptUnprotectData" in identity
    assert "CreateProcessWithLogonW" in process
    assert "--windows-runner" in process


def test_sandbox_accounts_cannot_read_the_machine_dpapi_identity_file():
    identity = _read("identity.rs")
    state = _read("state.rs")

    assert "offline_sid" not in identity
    assert "online_sid" not in identity
    assert "read_write = FILE_GENERIC_READ | FILE_GENERIC_WRITE" not in identity
    assert "super::state::prepare_directory(state_dir)" in identity
    assert "super::state::read_file(&identity_path(state_dir))" in identity
    assert "super::state::write_file(path, bytes)" in identity
    atomic_write = state[state.index("pub(crate) fn write_file") : state.index(
        "pub(crate) fn read_file"
    )]
    assert ".create_new(true)" in atomic_write
    assert "fs::write(&temporary" not in atomic_write
    assert atomic_write.index("protect_file(&temporary)") < atomic_write.index("write_all")


def test_windows_setup_reprotects_the_legacy_identity_before_reading_it():
    identity = _read("identity.rs")
    setup = identity[identity.index("pub fn setup") : identity.index("pub fn load")]

    assert "protect_legacy_identity(state_dir)?" in setup
    assert setup.index("protect_legacy_identity(state_dir)?") < setup.index(
        "read_optional_file(&identity_path(state_dir))"
    )


def test_windows_security_state_family_uses_one_protected_object_seam():
    state = _read("state.rs")
    acl = _read("acl.rs")
    identity = _read("identity.rs")
    readiness = _read("readiness.rs")

    assert "PROTECTED_DACL_SECURITY_INFORMATION" in state
    assert "GetNamedSecurityInfoW" in state
    assert "GetFileInformationByHandle" in state
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in state
    assert "super::state::read_optional_file" in acl
    assert "super::state::write_file" in acl
    assert "pub(crate) fn protect_legacy_state" in acl
    assert "super::acl::protect_legacy_state(state_dir)?" in identity
    assert "super::acl::protect_legacy_state(state_dir)" in readiness


def test_restricted_command_has_explicit_handles_and_job_cleanup():
    token = _read("token.rs")
    process = _read("process.rs")
    job = _read("job.rs")
    assert "CreateRestrictedToken" in token
    assert "DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED" in token
    assert "PROC_THREAD_ATTRIBUTE_HANDLE_LIST" in process
    assert "CREATE_SUSPENDED" in process
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in job
    assert "AssignProcessToJobObject" in job


def test_acl_targets_are_reverified_after_runner_creation_before_resume():
    process = _read("process.rs")

    assert "verify_authorized_paths" in process
    assert process.index("job.assign") < process.index("verify_authorized_paths()")
    assert process.index("verify_authorized_paths()") < process.index(
        "ResumeThread(process_info.hThread)"
    )


def test_acl_changes_preserve_existing_dacl_and_have_crash_manifest():
    acl = _read("acl.rs")
    assert "GetSecurityInfo" in acl
    assert "SetEntriesInAclW" in acl
    assert "SetSecurityInfo" in acl
    assert "CreateFileW" in acl
    assert "windows-acl-state.json" in acl
    assert "cleanup_stale" in acl
    assert "revoke_entry" in acl
    assert '".git", ".agents", ".crew"' in acl


def test_acl_enforces_read_only_and_protected_child_delete_precedence():
    acl = _read("acl.rs")

    assert "DenyDeleteChild" in acl
    readable = acl[acl.index("for root in &request.readable_roots") :]
    readable = readable[: readable.index("for (index, root) in request.writable_roots")]
    assert "AclAccess::DenyWrite" in readable
    writable = acl[acl.index("for (index, root) in request.writable_roots") :]
    writable = writable[: writable.index("for root in &request.denied_roots")]
    assert "AclAccess::DenyDeleteChild" in writable


def test_windows_run_prepares_canonical_paths_and_never_falls_back_to_raw_cwd():
    windows = _read("mod.rs")
    paths = _read("path.rs")

    assert "path::prepare_policy" in windows
    assert "request.cwd = prepared.cwd" in windows
    assert "reject_reparse_components" in paths
    assert "multiple hard links" in paths
    assert "unwrap_or(request.cwd)" not in windows
    assert "unwrap_or_else(|_| request.cwd" not in windows


def test_windows_run_revalidates_the_runtime_location_before_launch():
    windows = _read("mod.rs")

    assert "identity::validate_runtime_location()" in windows
    assert windows.index("identity::validate_runtime_location()") < windows.index(
        "process::run_via_account"
    )


def test_wfp_is_mandatory_for_offline_and_online_accounts():
    windows = _read("mod.rs")
    wfp = _read("wfp.rs")

    assert "verify_installed(&offline.username, &online.username)" in windows
    assert "if request.network_enabled {\n        wfp::verify_installed" not in windows
    assert "FWPM_CONDITION_ALE_USER_ID" in wfp
    assert "FWPM_CONDITION_IP_REMOTE_PORT" in wfp
    assert "FWP_CONDITION_FLAG_IS_LOOPBACK" in wfp
    assert "verify_provider_and_sublayer" in wfp


def test_wfp_does_not_permit_ipv6_when_the_proxy_listens_only_on_ipv4():
    wfp = _read("wfp.rs")

    assert "(ONLINE_PERMIT_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6)" not in wfp
    assert "verify_filter_absent(engine.handle, &ONLINE_PERMIT_V6)" in wfp


def test_windows_uses_per_run_capabilities_temp_and_reported_acl_cleanup():
    windows = _read("mod.rs")
    acl = _read("acl.rs")
    process = _read("process.rs")

    assert "load_capabilities" not in acl
    assert "save_capabilities" not in acl
    assert "create_run_directories" in acl
    assert "temp_dir" in process
    assert '"TEMP"' in process
    assert '"TMP"' in process
    assert '"USERNAME"' in process
    assert "environment entry is reserved" in process
    assert "lease.finish()" in windows
    assert "restore_dacl" in acl


def test_windows_job_has_enforced_resource_limits():
    job = _read("job.rs")

    assert "JOB_OBJECT_LIMIT_ACTIVE_PROCESS" in job
    assert "JOB_OBJECT_LIMIT_PROCESS_MEMORY" in job
    assert "JOB_OBJECT_LIMIT_JOB_MEMORY" in job
    assert "JOB_OBJECT_LIMIT_PROCESS_TIME" in job
    assert "QueryInformationJobObject" in job


def test_windows_setup_is_explicitly_elevated_and_wfp_failure_is_fatal():
    identity = _read("identity.rs")
    setup = identity[identity.index("pub fn setup") : identity.index("pub fn load")]

    assert "require_elevated()?" in setup
    assert setup.index("require_elevated()?") < setup.index("prepare_directory")
    assert "validate_runtime_location()?" in setup
    assert setup.index("validate_runtime_location()?") < setup.index("prepare_directory")
    assert "super::wfp::install" in setup
    assert "if let Err(error) = super::wfp::install" in setup
    assert "super::wfp::uninstall()" in setup
    assert "match (identity_cleanup, visibility_cleanup, account_cleanup)" in setup


def test_native_identity_tests_are_reported_ignored_without_a_fixture():
    sandbox = (WINDOWS_TESTS / "windows_sandbox.rs").read_text(encoding="utf-8")
    readiness = (WINDOWS_TESTS / "windows_readiness.rs").read_text(encoding="utf-8")

    assert '#[ignore = "requires an installed Windows sandbox fixture"]' in sandbox
    assert '#[ignore = "requires an installed Windows sandbox fixture"]' in readiness
    assert "ACE_REQUIRE_NATIVE_TESTS" not in sandbox
    assert "ACE_REQUIRE_NATIVE_TESTS" not in readiness
