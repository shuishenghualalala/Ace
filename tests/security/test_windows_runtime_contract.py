"""Windows source gates complement mandatory native target tests."""

from pathlib import Path


WINDOWS = Path(__file__).parents[2] / "security-runtime" / "src" / "windows"


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


def test_acl_changes_preserve_existing_dacl_and_have_crash_manifest():
    acl = _read("acl.rs")
    assert "GetNamedSecurityInfoW" in acl
    assert "SetEntriesInAclW" in acl
    assert "SetNamedSecurityInfoW" in acl
    assert "windows-acl-state.json" in acl
    assert "cleanup_stale" in acl
    assert "revoke_entry" in acl
    assert '".git", ".agents", ".crew"' in acl
