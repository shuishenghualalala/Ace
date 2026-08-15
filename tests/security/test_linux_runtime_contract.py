"""Cross-host Linux gates; native kernel behavior remains in Rust runner tests."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
LINUX = ROOT / "security-runtime" / "src" / "linux"


def _read(name: str) -> str:
    return (LINUX / name).read_text(encoding="utf-8")


def test_linux_never_weakens_full_disk_or_network_profiles() -> None:
    bwrap = _read("bwrap.rs")

    assert "writable filesystem root is incompatible" in bwrap
    assert "managed network requires a private proxy bridge" in bwrap
    assert "offline sandbox cannot mount a proxy bridge" in bwrap
    assert '"--unshare-net"' in bwrap
    assert '"--unshare-ipc"' in bwrap
    assert '"--unshare-uts"' in bwrap
    assert '"--proc".to_string(), "/proc".to_string()' in bwrap


def test_linux_started_event_follows_verified_inner_hardening() -> None:
    runtime = _read("mod.rs")
    seccomp = _read("seccomp.rs")

    assert runtime.index("await_inner_readiness") < runtime.index(
        "RuntimeMessage::Started"
    )
    assert "INNER_READY_TIMEOUT" in runtime
    assert "ManagedChild" in runtime
    assert "plan.cleanup().map_err(denied)?" in runtime
    assert "PR_GET_NO_NEW_PRIVS" in seccomp
    assert "PR_GET_SECCOMP" in seccomp
    assert 'read_to_string("/proc/self/status")' in seccomp
    for syscall in (
        "SYS_open_tree",
        "SYS_move_mount",
        "SYS_fsopen",
        "SYS_fsconfig",
        "SYS_fsmount",
        "SYS_mount_setattr",
    ):
        assert syscall in seccomp


def test_linux_glob_denies_are_bounded_and_planned_before_bwrap_spawn() -> None:
    runtime = _read("mod.rs")
    bwrap = _read("bwrap.rs")

    assert "MAX_DENY_READ_GLOB_MATCHES: usize = 8192" in bwrap
    assert "expand_deny_read_globs(request)?" in bwrap
    assert "first_writable_symlink_component" in bwrap
    assert runtime.index("bwrap::build_args") < runtime.index("Command::new")


def test_linux_bwrap_source_and_native_runner_are_mandatory() -> None:
    source = _read("bwrap_source.rs")
    workflow = (ROOT / ".github" / "workflows" / "security-linux.yml").read_text(
        encoding="utf-8"
    )
    native_tests = (
        ROOT / "security-runtime" / "tests" / "linux_fail_closed.rs"
    ).read_text(encoding="utf-8")

    assert "is_trusted_system_candidate" in source
    assert "metadata.uid() != 0" in source
    assert "ACE_BUNDLED_BWRAP_SHA256" in source
    assert "/proc/self/fd/" in source
    assert "sudo apt-get install -y bubblewrap" in workflow
    assert "ACE_REQUIRE_NATIVE_TESTS: '1'" in workflow
    assert '#![cfg(target_os = "linux")]' in native_tests
