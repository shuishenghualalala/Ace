"""Source-level gates supplement, but never replace, native platform tests."""

from pathlib import Path


RUNTIME = Path(__file__).parents[2] / "security-runtime" / "src"


def test_protocol_has_no_network_listener_and_rejects_replay():
    source = (RUNTIME / "main.rs").read_text(encoding="utf-8")
    assert "TcpListener" not in source
    assert "seen_nonces" in source
    assert "ACE_SECURITY_RUNTIME_TOKEN" in source


def test_linux_profile_uses_codex_shaped_boundaries():
    source = (RUNTIME / "linux" / "bwrap.rs").read_text(encoding="utf-8")
    for required in (
        '"--ro-bind"',
        '"--unshare-user"',
        '"--unshare-pid"',
        '"--unshare-net"',
        '"--die-with-parent"',
        '"--proc"',
        '"--remount-ro"',
    ):
        assert required in source
    for protected in ('".git"', '".agents"', '".crew"'):
        assert protected in source
    assert '"--tmpfs".to_string(),\n        "/".to_string(),' in source
    assert (
        '"--ro-bind".to_string(),\n        "/".to_string(),\n        "/".to_string(),'
        not in source
    )
    assert "writable.push(cwd.clone())" not in source
    assert "sandbox cwd must be inside an explicit writable root" in source


def test_linux_hardening_and_bundle_verification_are_not_optional_fallbacks():
    seccomp = (RUNTIME / "linux" / "seccomp.rs").read_text(encoding="utf-8")
    source = (RUNTIME / "linux" / "bwrap_source.rs").read_text(encoding="utf-8")
    assert "PR_SET_NO_NEW_PRIVS" in seccomp
    assert "SYS_ptrace" in seccomp
    assert "AF_UNIX" in seccomp
    assert "ACE_BUNDLED_BWRAP_SHA256" in source
    assert "/proc/self/fd/" in source
    assert 'arg("--version")' in source


def test_macos_profile_uses_seatbelt_and_exact_managed_proxy_route():
    source = (RUNTIME / "macos" / "mod.rs").read_text(encoding="utf-8")
    assert 'const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec"' in source
    assert "(deny default)" in source
    assert "READONLY_ROOT" in source
    assert "DENIED_READ_ROOT" in source
    assert '"deny file-write*"' in source
    assert "allow file-write*" in source
    assert 'remote ip \\"localhost:{port}\\"' in source
    assert 'backend: "macos_seatbelt"' in source
    assert '.env_clear()' in source
    assert "ACTIVE_PROCESS_GROUP" in source
    assert "terminate_signal" in source
