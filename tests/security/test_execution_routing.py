import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.security.launch import (
    ProcessLaunch,
    current_process_launch,
    execute_captured,
    host_stream_launch_block_reason,
    packaged_runtime_argv,
    runtime_source_stale,
)
from crew.security.models import FilesystemAccess, FilesystemEntry, PermissionProfile, PermissionProfileKind
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
from crew.tools.process_registry import ProcessRegistry


def _managed(tmp_path: Path) -> ProcessLaunch:
    runtime_skills = tmp_path / "runtime-skills"
    runtime_skills.mkdir(exist_ok=True)
    return ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        (str(tmp_path / "ace-security-runtime"),),
        (runtime_skills,),
    )


def test_host_stream_launch_policy_never_falls_back_from_managed_to_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ACE_STRICT_SECURITY", raising=False)
    token = current_process_launch.set(None)
    try:
        assert "missing" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_managed(tmp_path))
        assert "managed" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED)))
        assert host_stream_launch_block_reason() is None
        monkeypatch.setenv("ACE_STRICT_SECURITY", "0")
        current_process_launch.set(None)
        assert "missing" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_managed(tmp_path))
        assert "managed" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED)))
        assert host_stream_launch_block_reason() is None
    finally:
        current_process_launch.reset(token)


def test_packaged_runtime_honors_explicit_platform_artifact(monkeypatch, tmp_path):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    monkeypatch.setenv("ACE_SECURITY_RUNTIME", str(runtime))

    assert packaged_runtime_argv() == (str(runtime.resolve()),)


def test_runtime_source_stale_uses_manifest_next_to_selected_helper(tmp_path):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime.name,
                "binary_sha256": "not-used-by-source-check",
            }
        ),
        encoding="utf-8",
    )

    # Desktop staging manifests intentionally omit source_hash because the source tree
    # is not shipped with the application; they must not be reported as stale.
    assert runtime_source_stale(runtime) is None


def test_managed_background_command_is_protocol_data_not_host_argv(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    monkeypatch.setattr(
        registry,
        "spawn_local",
        lambda *args, **kwargs: pytest.fail("managed execution used host spawn_local"),
    )

    result = registry.spawn_security("echo secret", launch=_managed(tmp_path), cwd=str(tmp_path))

    assert result == "session"
    assert captured["payload"]["command"][-1].endswith("echo secret")
    assert captured["payload"]["helper_argv"] == [str(tmp_path / "ace-security-runtime")]
    assert captured["payload"]["readable_roots"] == [str(tmp_path / "runtime-skills")]


def test_managed_background_receives_minimal_runtime_env(
    tmp_path, monkeypatch
):
    registry = ProcessRegistry()
    captured = {}

    monkeypatch.setattr(
        "crew.state.home.managed_runtime_env_overrides",
        lambda **kwargs: {"CREW_RUNTIME_HOME": str(tmp_path / "runtime"), "PYTHONUTF8": "1"},
    )
    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    result = registry.spawn_security(
        "python unified_search.py",
        launch=_managed(tmp_path),
        cwd=str(tmp_path),
        owner_account_id="owner-a",
        session_key="session-a",
    )

    assert result == "session"
    assert captured["payload"]["env_overrides"] == {
        "CREW_RUNTIME_HOME": str(tmp_path / "runtime"),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    assert captured["kwargs"]["secret_values"] == ()


@pytest.mark.asyncio
async def test_background_bridge_forwards_explicit_env_overrides(tmp_path, monkeypatch):
    from crew.security import background_runner

    captured = {}

    class _Runtime:
        def __init__(self, helper_argv):
            captured["helper_argv"] = helper_argv

        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(background_runner, "NativeRuntimeClient", _Runtime)
    exit_code = await background_runner._run(
        {
            "helper_argv": ["runtime"],
            "command": ["python", "skill.py"],
            "cwd": str(tmp_path),
            "env_overrides": {"CREW_SEARCH_TICKET": "one-time-ticket-9876"},
        }
    )

    assert exit_code == 0
    assert captured["env_overrides"] == {
        "CREW_SEARCH_TICKET": "one-time-ticket-9876"
    }


@pytest.mark.asyncio
async def test_managed_captured_failure_never_falls_back_to_host(tmp_path, monkeypatch):
    launch = _managed(tmp_path)
    token = current_process_launch.set(launch)
    spawned_argv = None

    async def host_spawn(*args, **kwargs):
        nonlocal spawned_argv
        spawned_argv = args
        raise FileNotFoundError(args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await execute_captured(("ignored",), cwd=tmp_path, timeout=0.1)
    finally:
        current_process_launch.reset(token)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert spawned_argv == launch.helper_argv
    assert "ignored" not in spawned_argv


@pytest.mark.asyncio
async def test_managed_captured_passes_process_data_without_host_spawn(tmp_path, monkeypatch):
    launch = _managed(tmp_path)
    captured = {}
    started = [].append
    output = [].append

    async def managed_execute(self, request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    async def host_spawn(*args, **kwargs):
        raise AssertionError("managed execution must not use host spawn")

    monkeypatch.setattr(
        "crew.security.broker.SecurityExecutionBroker.execute",
        managed_execute,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    token = current_process_launch.set(launch)
    try:
        result = await execute_captured(
            ("ignored",),
            cwd=tmp_path,
            timeout=3.5,
            stdin=b"prompt",
            env_overrides={"API_KEY": "secret"},
            max_output_bytes=1234,
            on_started=started,
            on_output=output,
        )
    finally:
        current_process_launch.reset(token)

    assert result.stdout == "ok"
    assert captured["request"].stdin == b"prompt"
    assert captured["request"].env_overrides == {"API_KEY": "secret"}
    assert captured["request"].timeout_seconds == 3.5
    assert captured["request"].max_output_bytes == 1234
    assert captured["request"].trusted_readable_roots == (tmp_path / "runtime-skills",)
    assert captured["kwargs"] == {"on_started": started, "on_output": output}


@pytest.mark.asyncio
async def test_captured_execution_without_launch_context_refuses_host(tmp_path, monkeypatch):
    """A missing launch contextvar (e.g. a non-inheriting task) must NOT host-exec.

    Regression guard for the fail-open defect: ``execute_captured`` used to fall back
    to ``create_subprocess_exec`` when the contextvar was unset, conflating "full
    access" with "lost security context". It must now fail closed.
    """
    token = current_process_launch.set(None)

    async def host_spawn(*args, **kwargs):
        raise AssertionError("host subprocess must not start without a launch decision")

    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await execute_captured(("echo", "secret"), cwd=tmp_path, timeout=0.1)
    finally:
        current_process_launch.reset(token)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


def test_managed_acp_is_explicitly_unavailable_not_host_fallback() -> None:
    source = Path("crew/agent/external/acp_adapter.py").read_text(encoding="utf-8")
    assert "managed 模式拒绝宿主启动" in source
    assert "host_stream_launch_block_reason" in source


def test_sensitive_env_values_and_precise_redaction() -> None:
    """H-3 unit: sensitive-key env values are extracted and exact-redacted (always-on)."""
    from crew.tools.redact import redact_secret_values, sensitive_env_values

    values = sensitive_env_values(
        {
            "DB_PASSWORD": "Hunter2-Blue-77!",
            "CREW_SEARCH_TICKET": "one-time-ticket-9876",
            "PATH": "/usr/bin",
            "API_KEY": "k",
            "USER": "bob",
            "AUTHOR": "Jane Doe",
            "AUTHORITY": "x",
        }
    )
    assert "Hunter2-Blue-77!" in values
    assert "one-time-ticket-9876" in values
    assert "/usr/bin" not in values  # PATH is not a secret key
    assert "k" not in values  # too short (<4)
    assert "Jane Doe" not in values  # AUTHOR must not be over-redacted (substring 'auth')
    assert "x" not in values  # AUTHORITY likewise
    assert redact_secret_values(f"echo {values[0]}", values) == "echo ***REDACTED***"
    # non-matching text passes through untouched
    assert redact_secret_values("plain text", values) == "plain text"


def test_private_output_truncation_never_exposes_a_partial_secret() -> None:
    secret = "ABCDEFGHIJ"
    # A rolling buffer may begin in the middle of a value, while a complete
    # occurrence may independently cross the nominal visible-output boundary.
    output = "DEFGHIJxx" + secret + " safe"

    result = ProcessRegistry._redact_private_output(
        output,
        (secret,),
        truncated=True,
        max_output_chars=16,
    )

    assert "DEFGHIJ" not in result
    assert secret not in result
    assert result.endswith(" safe")


@pytest.mark.asyncio
async def test_cua_run_command_host_executes_without_launch_context(tmp_path, monkeypatch):
    """Regression (review #1): cua_setup._run_command is installer-lifecycle (gateway
    setup router runs with current_process_launch unset). It must host-execute and NOT
    route through execute_captured's fail-closed (which would 500 the install/status flow)."""
    from crew.tools.cua_setup import _run_command

    token = current_process_launch.set(None)  # gateway/router context: no conversation launch

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"cua-driver v1.2.3\n", b"")

    async def fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    try:
        out = await _run_command(["cua-driver", "--version"], timeout=5)
    finally:
        current_process_launch.reset(token)
    assert "v1.2.3" in out


@pytest.mark.asyncio
async def test_captured_execution_redacts_sensitive_env_value(tmp_path, monkeypatch):
    """H-3: a sensitive env value echoed by the child is precise-redacted even when it
    has no generic-secret shape (sk-/=/auth), and cannot be disabled via CREW_REDACT_SECRETS."""
    secret = "Hunter2-Blue-77!"  # no shape the generic regex matches
    monkeypatch.setenv("CREW_REDACT_SECRETS", "false")  # must NOT disable this boundary
    disabled_launch = ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))

    class _FakeProc:
        pid = 123
        returncode = 0
        stdin = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(f"connected with {secret}\n".encode("utf-8"))
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    token = current_process_launch.set(disabled_launch)
    try:
        result = await execute_captured(
            ("dbcli",), cwd=tmp_path, timeout=1.0, env={"DB_PASSWORD": secret, "PATH": "/bin"}
        )
    finally:
        current_process_launch.reset(token)
    assert secret not in result.stdout
    assert "REDACTED" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(("stdin", "expected"), [(None, "0"), (b"prompt", "6")])
async def test_host_captured_uses_one_shot_stdin_and_reports_activity(tmp_path, stdin, expected):
    secret = "Split-Secret-9876"
    script = (
        "import os,sys;"
        "data=sys.stdin.buffer.read();"
        "value=os.environ['API_KEY'];"
        "sys.stdout.write(str(len(data))+':'+value[:7]);sys.stdout.flush();"
        "sys.stdout.write(value[7:]);"
        "sys.stderr.write('notice')"
    )
    env = dict(os.environ, API_KEY=secret)
    started = []
    streams = []
    token = current_process_launch.set(
        ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))
    )
    try:
        result = await execute_captured(
            (sys.executable, "-c", script),
            cwd=tmp_path,
            timeout=2,
            env=env,
            stdin=stdin,
            on_started=started.append,
            on_output=streams.append,
        )
    finally:
        current_process_launch.reset(token)

    assert result.returncode == 0
    assert result.stdout.startswith(f"{expected}:")
    assert secret not in result.stdout
    assert "REDACTED" in result.stdout
    assert result.stderr == "notice"
    assert len(started) == 1
    assert set(streams) == {"stdout", "stderr"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_host_captured_timeout_and_cancel_terminate_process_tree(
    tmp_path, monkeypatch, cancel
):
    from crew.security import process_lifecycle

    terminated = 0
    real_terminate = process_lifecycle.terminate_process_tree
    real_spawn = asyncio.create_subprocess_exec

    if cancel:
        first_spawn = True

        async def delayed_first_spawn(*args, **kwargs):
            nonlocal first_spawn
            if first_spawn:
                first_spawn = False
                await asyncio.sleep(0.1)
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(
            "crew.security.launch.asyncio.create_subprocess_exec",
            delayed_first_spawn,
        )

    async def record_terminate(process):
        nonlocal terminated
        terminated += 1
        await real_terminate(process)

    monkeypatch.setattr("crew.security.launch.terminate_process_tree", record_terminate)
    token = current_process_launch.set(
        ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))
    )
    try:
        task = asyncio.create_task(
            execute_captured(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.05 if not cancel else 30,
            )
        )
        if cancel:
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(asyncio.TimeoutError):
                await task
    finally:
        current_process_launch.reset(token)

    assert terminated == 1
