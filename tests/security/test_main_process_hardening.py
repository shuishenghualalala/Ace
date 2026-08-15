from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from crew.process_hardening import (
    ProcessHardener,
    ProcessHardeningError,
    sanitize_injection_environment,
)

ROOT = Path(__file__).resolve().parents[2]


class RecordingOperations:
    def __init__(self, *, fail: str = "") -> None:
        self.calls: list[str] = []
        self.fail = fail

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise OSError(f"{name} rejected")

    def disable_core_dumps(self) -> None:
        self._record("disable_core_dumps")

    def disable_linux_dumpability(self) -> None:
        self._record("disable_linux_dumpability")

    def enable_linux_no_new_privs(self) -> None:
        self._record("enable_linux_no_new_privs")

    def deny_macos_debug_attach(self) -> None:
        self._record("deny_macos_debug_attach")

    def secure_windows_dll_search(self) -> None:
        self._record("secure_windows_dll_search")

    def configure_windows_error_mode(self) -> None:
        self._record("configure_windows_error_mode")

    def disable_windows_standard_handle_inheritance(self) -> None:
        self._record("disable_windows_standard_handle_inheritance")


def test_injection_environment_is_removed_without_blocking_explicit_child_env() -> None:
    environment = {
        "PATH": "/safe/bin",
        "ACE_SECURITY_MODE": "managed",
        "LD_PRELOAD": "/tmp/inject.so",
        "ld_audit": "/tmp/audit.so",
        "LD_LIBRARY_PATH": "/tmp/libs",
        "LD_LIBRARY_PATH_64": "/tmp/libs64",
        "LD_PROFILE": "/tmp/profile-output",
        "DYLD_INSERT_LIBRARIES": "/tmp/inject.dylib",
        "DyLd_Custom_Hook": "unsafe",
        "PYTHONINSPECT": "1",
        "PYTHONSTARTUP": "/tmp/start.py",
        "PYTHONPATH": "/tmp/modules",
        "PYTHONHOME": "/tmp/python",
        "NODE_OPTIONS": "--require=/tmp/hook.js",
        "NODE_PATH": "/tmp/node_modules",
        "NODE_EXTRA_CA_CERTS": "/tmp/attacker-ca.pem",
        "ELECTRON_RUN_AS_NODE": "1",
        "ELECTRON_NO_ASAR": "1",
        "OPENSSL_CONF": "/tmp/openssl.cnf",
        "PSModulePath": "C:\\attacker\\modules",
        "COR_ENABLE_PROFILING": "1",
        "CORECLR_PROFILER_PATH": "C:\\attacker\\profiler.dll",
        "DOTNET_STARTUP_HOOKS": "C:\\attacker\\startup-hook.dll",
        "COMPlus_ReadyToRun": "0",
        "JAVA_TOOL_OPTIONS": "-javaagent:C:\\attacker\\agent.jar",
    }

    removed = sanitize_injection_environment(environment)

    assert set(removed) == {
        "LD_PRELOAD",
        "ld_audit",
        "LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH_64",
        "LD_PROFILE",
        "DYLD_INSERT_LIBRARIES",
        "DyLd_Custom_Hook",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "ELECTRON_RUN_AS_NODE",
        "ELECTRON_NO_ASAR",
        "OPENSSL_CONF",
        "PSModulePath",
        "COR_ENABLE_PROFILING",
        "CORECLR_PROFILER_PATH",
        "DOTNET_STARTUP_HOOKS",
        "COMPlus_ReadyToRun",
        "JAVA_TOOL_OPTIONS",
    }
    assert environment == {"PATH": "/safe/bin", "ACE_SECURITY_MODE": "managed"}

    child_environment = dict(environment)
    child_environment["LD_PRELOAD"] = "/trusted/skill-only.so"
    assert "LD_PRELOAD" not in environment
    assert child_environment["LD_PRELOAD"] == "/trusted/skill-only.so"


def test_linux_hardening_is_fail_closed_and_idempotent() -> None:
    environment = {"NODE_OPTIONS": "--inspect"}
    operations = RecordingOperations()
    messages: list[str] = []
    hardener = ProcessHardener(
        environ=environment,
        platform_name="linux",
        os_name="posix",
        operations=operations,
        warning_sink=messages.append,
    )

    first = hardener.apply("managed-background-bridge")
    environment["PYTHONSTARTUP"] = "/tmp/late.py"
    second = hardener.apply("managed-background-bridge")

    assert operations.calls == [
        "disable_core_dumps",
        "disable_linux_dumpability",
        "enable_linux_no_new_privs",
    ]
    assert first.platform == "linux"
    assert first.role == "managed-background-bridge"
    assert first.applied == (
        "rlimit_core=0",
        "pr_set_dumpable=0",
        "pr_set_no_new_privs=1",
    )
    assert second.applied == first.applied
    assert "NODE_OPTIONS" not in environment
    assert "PYTHONSTARTUP" not in environment
    assert any("NODE_OPTIONS" in message for message in messages)
    assert any("PYTHONSTARTUP" in message for message in messages)


@pytest.mark.parametrize("role", ["cli", "gateway"])
def test_linux_host_roles_record_no_new_privs_skip_for_approved_elevation(
    role: str,
) -> None:
    operations = RecordingOperations()
    messages: list[str] = []
    hardener = ProcessHardener(
        environ={},
        platform_name="linux",
        os_name="posix",
        operations=operations,
        warning_sink=messages.append,
    )

    report = hardener.apply(role)

    assert operations.calls == ["disable_core_dumps", "disable_linux_dumpability"]
    assert report.applied == ("rlimit_core=0", "pr_set_dumpable=0")
    assert any(
        "PR_SET_NO_NEW_PRIVS" in message
        and "SKIPPED" in message
        and "approved elevated descendants" in message
        for message in messages
    )


def test_critical_native_failure_is_logged_without_environment_values() -> None:
    operations = RecordingOperations(fail="disable_linux_dumpability")
    messages: list[str] = []
    hardener = ProcessHardener(
        environ={"LD_PRELOAD": "/secret/inject.so"},
        platform_name="linux",
        os_name="posix",
        operations=operations,
        warning_sink=messages.append,
    )

    with pytest.raises(ProcessHardeningError, match="pr_set_dumpable=0"):
        hardener.apply("gateway")

    combined = "\n".join(messages)
    assert "FAIL-CLOSED" in combined
    assert "LD_PRELOAD" in combined
    assert "/secret/inject.so" not in combined


def test_environment_cleanup_failure_is_fail_closed_before_native_calls() -> None:
    class UndeletableEnvironment(dict[str, str]):
        def __delitem__(self, key: str) -> None:
            raise OSError(f"cannot delete {key}")

    operations = RecordingOperations()
    messages: list[str] = []
    hardener = ProcessHardener(
        environ=UndeletableEnvironment({"NODE_OPTIONS": "--inspect"}),
        platform_name="linux",
        os_name="posix",
        operations=operations,
        warning_sink=messages.append,
    )

    with pytest.raises(ProcessHardeningError, match="environment sanitization"):
        hardener.apply("gateway")

    assert operations.calls == []
    assert any("FAIL-CLOSED" in message and "environment-sanitization" in message for message in messages)
    assert all("--inspect" not in message for message in messages)


def test_environment_cleanup_verifies_that_the_hook_is_absent() -> None:
    class StickyEnvironment(dict[str, str]):
        def __delitem__(self, key: str) -> None:
            return

    operations = RecordingOperations()
    hardener = ProcessHardener(
        environ=StickyEnvironment({"NODE_OPTIONS": "--inspect"}),
        platform_name="linux",
        os_name="posix",
        operations=operations,
        warning_sink=lambda _message: None,
    )

    with pytest.raises(ProcessHardeningError, match="environment sanitization"):
        hardener.apply("gateway")

    assert operations.calls == []


def test_macos_deny_attach_failure_is_fail_closed() -> None:
    operations = RecordingOperations(fail="deny_macos_debug_attach")
    messages: list[str] = []
    hardener = ProcessHardener(
        environ={},
        platform_name="darwin",
        os_name="posix",
        operations=operations,
        warning_sink=messages.append,
    )

    with pytest.raises(ProcessHardeningError, match="PT_DENY_ATTACH"):
        hardener.apply("gateway")

    assert operations.calls == ["disable_core_dumps", "deny_macos_debug_attach"]
    assert any("PT_DENY_ATTACH" in message and "FAIL-CLOSED" in message for message in messages)


def test_windows_hardening_applies_dll_error_and_handle_controls() -> None:
    operations = RecordingOperations()
    hardener = ProcessHardener(
        environ={},
        platform_name="win32",
        os_name="nt",
        operations=operations,
        warning_sink=lambda _message: None,
    )

    report = hardener.apply("gateway")

    assert operations.calls == [
        "secure_windows_dll_search",
        "configure_windows_error_mode",
        "disable_windows_standard_handle_inheritance",
    ]
    assert report.applied == (
        "secure_dll_search",
        "noninteractive_error_mode",
        "noninheritable_standard_handles",
    )


def test_unknown_platform_fails_closed() -> None:
    hardener = ProcessHardener(
        environ={},
        platform_name="plan9",
        os_name="unknown",
        operations=RecordingOperations(),
        warning_sink=lambda _message: None,
    )

    with pytest.raises(ProcessHardeningError, match="unsupported platform"):
        hardener.apply("gateway")


def test_python_production_entries_harden_before_application_imports() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    application_module = (ROOT / "crew/app.py").read_text(encoding="utf-8")
    cli_main = (ROOT / "crew/cli/main.py").read_text(encoding="utf-8")
    cli_package_main = (ROOT / "crew/cli/__main__.py").read_text(encoding="utf-8")
    cli_entrypoint = (ROOT / "crew/cli/entrypoint.py").read_text(encoding="utf-8")
    weixin_entrypoint = (ROOT / "crew/cli/weixin_login.py").read_text(encoding="utf-8")
    gateway_package = (ROOT / "crew/gateway/__init__.py").read_text(encoding="utf-8")
    gateway_server = (ROOT / "crew/gateway/server.py").read_text(encoding="utf-8")
    process_registry = (ROOT / "crew/tools/process_registry.py").read_text(encoding="utf-8")
    desktop_main = (ROOT / "desktop/src/main/index.ts").read_text(encoding="utf-8")
    docker_pack = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")
    mac_pack = (ROOT / "deb-package/pack_mac.sh").read_text(encoding="utf-8")

    assert 'crew = "crew.cli.entrypoint:main"' in pyproject
    assert "harden_main_process" not in application_module
    assert "from crew.cli.entrypoint import main" in cli_package_main
    assert cli_entrypoint.index('harden_main_process("cli")') < cli_entrypoint.index(
        "from crew.cli.main import main as cli_main"
    )
    assert cli_main.index('harden_main_process("cli")') < cli_main.index("import asyncio")
    assert weixin_entrypoint.index('harden_main_process("cli-weixin-login")') < (
        weixin_entrypoint.index("import asyncio")
    )
    assert "from crew.gateway." not in gateway_package
    assert gateway_server.index('harden_main_process("gateway")') < gateway_server.index(
        "from crew.gateway.app import create_app, run"
    )
    assert process_registry.index('harden_main_process("managed-background-bridge")') < (
        process_registry.index("from crew.security.background_runner import main")
    )
    assert "['-m', 'crew.gateway.server']" in desktop_main
    gateway_spawn = desktop_main.index("['-m', 'crew.gateway.server']")
    assert "managedGateway = spawn(" in desktop_main[max(0, gateway_spawn - 100) : gateway_spawn]
    assert "hardenedChildProcessOptions(" in desktop_main[gateway_spawn : gateway_spawn + 250]
    assert "const logDirectory = path.dirname(file);" in desktop_main
    assert "ensurePrivateUpdateDirectory(logDirectory);" in desktop_main
    assert "fs.constants.O_NOFOLLOW" in desktop_main
    assert "    crew/gateway/server.py" in docker_pack
    assert "    crew/gateway/server.py" in mac_pack


def test_console_entrypoint_cleans_the_live_process_environment() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONSTARTUP": str(ROOT / "untrusted-startup.py"),
            "NODE_OPTIONS": "--require=untrusted.js",
            "ELECTRON_RUN_AS_NODE": "1",
        }
    )
    code = """
import json
import os
import crew.cli.entrypoint
print(json.dumps({
    "pythonstartup": os.environ.get("PYTHONSTARTUP"),
    "node_options": os.environ.get("NODE_OPTIONS"),
    "electron_run_as_node": os.environ.get("ELECTRON_RUN_AS_NODE"),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert json.loads(completed.stdout.strip()) == {
        "pythonstartup": None,
        "node_options": None,
        "electron_run_as_node": None,
    }
    assert "PYTHONSTARTUP" in completed.stderr
    assert "untrusted-startup.py" not in completed.stderr


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX runner evidence")
def test_posix_runner_reports_real_process_hardening_state() -> None:
    if sys.platform == "linux":
        probe = """
import ctypes
import json
import resource
from crew.process_hardening import harden_main_process
harden_main_process("managed-background-bridge")
libc = ctypes.CDLL(None, use_errno=True)
print(json.dumps({
    "core": list(resource.getrlimit(resource.RLIMIT_CORE)),
    "dumpable": libc.prctl(3, 0, 0, 0, 0),
    "no_new_privs": libc.prctl(39, 0, 0, 0, 0),
}))
"""
    else:
        probe = """
import json
import resource
from crew.process_hardening import harden_main_process
harden_main_process("managed-background-bridge")
print(json.dumps({"core": list(resource.getrlimit(resource.RLIMIT_CORE))}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    state = json.loads(completed.stdout.strip())

    assert state["core"] == [0, 0]
    if sys.platform == "linux":
        assert state["dumpable"] == 0
        assert state["no_new_privs"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runner evidence")
def test_windows_runner_reports_native_controls_and_noninheritable_handles() -> None:
    probe = """
import json
import msvcrt
import os
import sys
import crew.cli.entrypoint
from crew.process_hardening import harden_main_process
report = harden_main_process("verification")
handles = []
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if stream is None:
        continue
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (OSError, ValueError):
        continue
    handles.append(os.get_handle_inheritable(handle))
print(json.dumps({"applied": list(report.applied), "inheritable": handles}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    state = json.loads(completed.stdout.strip())

    assert state["applied"] == [
        "secure_dll_search",
        "noninteractive_error_mode",
        "noninheritable_standard_handles",
    ]
    assert state["inheritable"]
    assert not any(state["inheritable"])


def test_sandbox_skill_adds_ld_preload_only_to_its_explicit_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.skills.xlsx.scripts.office import soffice

    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.setattr(soffice, "_needs_shim", lambda: True)
    monkeypatch.setattr(soffice, "_ensure_shim", lambda: Path("/trusted/lo_socket_shim.so"))

    child_environment = soffice.get_soffice_env()

    assert "LD_PRELOAD" not in os.environ
    assert Path(child_environment["LD_PRELOAD"]) == Path("/trusted/lo_socket_shim.so")
