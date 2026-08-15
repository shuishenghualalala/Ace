"""Protocol and fail-closed tests for the native runtime client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemGlobEntry,
    NetworkAccess,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import (
    NativeRuntimeClient,
    NativeRuntimeError,
    RuntimeCapabilities,
    RuntimeErrorCode,
    ShellVerdict,
    set_runtime_diagnostic_auditor,
)
from crew.security.snapshot import issue_authorization_snapshot

_FAKE_HELPER = r'''
import base64, json, os, sys, time
mode = sys.argv[1]
version = 999 if mode == "bad-ready" else 2
ready = {
    "type":"ready",
    "version":version,
    "capabilities":["deny_read_glob_v1", "stdin_once", "stream_output", "duplex_stdio_v1"],
}
if mode == "missing-ready-capability":
    ready["capabilities"] = ["stdin_once"]
elif mode == "legacy-ready":
    ready["capabilities"] = ["stdin_once", "stream_output"]
elif mode == "missing-duplex":
    ready["capabilities"] = ["deny_read_glob_v1", "stdin_once", "stream_output"]
print(json.dumps(ready), flush=True)
if mode == "hang":
    time.sleep(30)
    raise SystemExit
line = sys.stdin.readline()
if not line:
    raise SystemExit(2)
request = json.loads(line)
if request["token"] != os.environ["ACE_SECURITY_RUNTIME_TOKEN"]:
    raise SystemExit(3)
if request["request"].get("op") == "classify_shell":
    classification = {
        "shell_kind": request["request"]["shell_kind"],
        "raw_command": request["request"]["raw_command"],
        "parsed_commands": [["git", "status"]],
        "canonical_digest": "a" * 64,
        "verdict": "allow_read_only" if mode == "classify-ok" else "invented",
        "reason": "test",
    }
    print(json.dumps({
        "version": 2, "nonce": request["nonce"], "seq": 0,
        "type": "classified", "classification": classification,
    }), flush=True)
    raise SystemExit
if mode == "crash":
    raise SystemExit(4)
if mode == "assert-request":
    payload = request["request"]
    if base64.b64decode(payload["stdin_b64"]) != b"\x00prompt\xff":
        raise SystemExit(5)
    if payload["env_overrides"] != {"CODEX_API_KEY": "secret"}:
        raise SystemExit(6)
if mode == "assert-glob-request":
    if request["request"].get("filesystem_globs") != [{
        "access": "deny_read",
        "pattern": "**/*.pem",
        "root": request["request"]["cwd"],
    }]:
        raise SystemExit(9)
if mode == "assert-no-stdin" and "stdin_b64" in request["request"]:
    raise SystemExit(7)
if mode == "assert-empty-stdin" and request["request"].get("stdin_b64") != "":
    raise SystemExit(8)
if mode == "stderr-flood":
    sys.stderr.write("x" * (256 * 1024))
    sys.stderr.flush()
nonce = "wrong" if mode == "bad-nonce" else request["nonce"]
capabilities = {
    "backend": "windows_sandbox_account" if mode.startswith("windows-") else "fake",
    "filesystem_sandbox": mode != "missing-capability",
    "process_tree_cleanup": True,
    "managed_network": mode in {"network-ok", "windows-network-ok"},
    "local_binding_control": mode == "local-binding-ok",
    "explicit_handle_inheritance": mode in {"windows-ok", "windows-network-ok"},
    "windows_restricted_token": mode in {"windows-ok", "windows-network-ok"},
    "windows_acl": mode in {"windows-ok", "windows-network-ok"},
    "windows_job": mode in {"windows-ok", "windows-network-ok"},
    "windows_wfp": mode == "windows-network-ok",
}
frames = [
    {
        "version": 2,
        "nonce": nonce,
        "seq": 0,
        "type": "started",
        "pid": 123,
        "capabilities": capabilities,
    },
    {
        "version": 2,
        "nonce": nonce,
        "seq": 1,
        "type": "stdout",
        "data_b64": base64.b64encode(b"sandboxed").decode(),
    },
    {
        "version": 2,
        "nonce": nonce,
        "seq": 2,
        "type": "stderr",
        "data_b64": base64.b64encode(b"notice").decode(),
    },
    {
        "version": 2,
        "nonce": nonce,
        "seq": 3,
        "type": "completed",
        "exit_code": 0,
    }
]
if mode == "output-before-start":
    frames = frames[1:]
elif mode == "bad-sequence":
    frames[1]["seq"] = 2
elif mode == "invalid-base64":
    frames[1]["data_b64"] = "***"
elif mode == "oversized-chunk":
    frames[1]["data_b64"] = base64.b64encode(b"x" * (64 * 1024 + 1)).decode()
elif mode == "oversized-frame":
    frames[1]["padding"] = "x" * (128 * 1024)
elif mode == "unknown-event":
    frames[1]["type"] = "progress"
elif mode == "invalid-exit-code":
    frames[3]["exit_code"] = {}
elif mode == "premature-eof":
    frames = frames[:1]
elif mode == "extra-after-terminal":
    frames.append({
        "version": 2,
        "nonce": nonce,
        "seq": 4,
        "type": "completed",
        "exit_code": 0,
    })
elif mode == "error-before-start":
    frames = [{
        "version": 2,
        "nonce": nonce,
        "seq": 0,
        "type": "error",
        "code": "sandbox_denied",
        "message": "sandbox denied execution",
    }]
elif mode == "repeated-output":
    frames.insert(2, {
        "version": 2,
        "nonce": nonce,
        "seq": 2,
        "type": "stdout",
        "data_b64": base64.b64encode(b"-again").decode(),
    })
    frames[3]["seq"] = 3
    frames[4]["seq"] = 4
for frame in frames:
    print(json.dumps(frame), flush=True)
    if mode == "slow-stream":
        time.sleep(0.06)
'''


class _ProtocolTestRuntimeClient(NativeRuntimeClient):
    """Bypass package integrity only for the in-process Python protocol fixture."""

    async def _spawn(
        self,
        token: str,
        *,
        expected_helper_digest: str | None = None,
    ) -> asyncio.subprocess.Process:
        with (
            patch("crew.security.launch.verify_helper_integrity", lambda _path: None),
            patch("crew.security.launch.trusted_helper_environment", lambda _path: {}),
        ):
            return await super()._spawn(
                token,
                expected_helper_digest=expected_helper_digest,
            )


def _helper(tmp_path: Path, mode: str) -> NativeRuntimeClient:
    script = tmp_path / "fake_runtime.py"
    script.write_text(_FAKE_HELPER, encoding="utf-8")
    return _ProtocolTestRuntimeClient(
        (sys.executable, str(script), mode),
        startup_timeout=0.5,
    )


def _authorized_request(
    tmp_path: Path,
    profile: PermissionProfile,
    *,
    additional_permissions: AdditionalPermissionProfile | None = None,
    trusted_readable_roots: tuple[Path, ...] = (),
    environment: dict[str, str] | None = None,
    **request_options,
) -> tuple[NativeRuntimeClient, ExecutionRequest]:
    env = dict(environment or {})
    action = normalize_exec_action(("test",), tmp_path)
    helper = tmp_path / "authorized-runtime"
    helper_argv: tuple[str, ...] = ()
    if profile.kind is PermissionProfileKind.MANAGED:
        helper.write_bytes(b"runtime")
        helper.with_name("runtime-manifest.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "binary_name": helper.name,
                    "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        helper_argv = (str(helper),)
    authorization = issue_authorization_snapshot(
        context=SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        action=action,
        profile=profile,
        additional_permissions=additional_permissions or AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment=env,
        helper_argv=helper_argv,
        trusted_readable_roots=trusted_readable_roots,
    )
    runtime_argv = helper_argv or (str(tmp_path / "unused-runtime"),)
    return NativeRuntimeClient(runtime_argv), ExecutionRequest(
        authorization_snapshot=authorization,
        env_overrides=env,
        **request_options,
    )


@pytest.mark.asyncio
async def test_large_helper_stderr_is_drained_without_deadlock(tmp_path):
    result = await _helper(tmp_path, "stderr-flood").execute(
        command=("ignored",), cwd=tmp_path, timeout=2
    )
    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
async def test_runtime_diagnostic_auditor_receives_ready_and_failure(tmp_path):
    events: list[dict] = []
    set_runtime_diagnostic_auditor(lambda **fields: events.append(fields))
    try:
        result = await _helper(tmp_path, "ok").execute(
            command=("ignored",),
            cwd=tmp_path,
            timeout=1,
        )
        assert result.stdout == "sandboxed"
        ready = [event for event in events if event["status"] == "ready"]
        assert ready
        assert ready[0]["version"] == "2"
        assert "duplex_stdio_v1" in ready[0]["capabilities"]

        with pytest.raises(NativeRuntimeError):
            await _helper(tmp_path, "missing-ready-capability").execute(
                command=("ignored",),
                cwd=tmp_path,
                timeout=1,
            )
        failed = [event for event in events if event["status"] == "failed"]
        assert failed
        assert failed[-1]["failure_code"]
    finally:
        set_runtime_diagnostic_auditor(None)


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("bad-ready", "runtime_protocol_mismatch"),
        ("missing-ready-capability", "runtime_protocol_mismatch"),
        ("crash", "runtime_crashed"),
        ("missing-capability", "sandbox_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_failure_matrix_records_stable_code(
    tmp_path,
    mode,
    expected_code,
):
    events: list[dict] = []
    set_runtime_diagnostic_auditor(lambda **fields: events.append(fields))
    try:
        with pytest.raises(NativeRuntimeError):
            await _helper(tmp_path, mode).execute(
                command=("ignored",),
                cwd=tmp_path,
                timeout=1,
            )
        failed = [event for event in events if event["status"] == "failed"]
        assert failed
        assert failed[-1]["failure_code"] == expected_code
    finally:
        set_runtime_diagnostic_auditor(None)


@pytest.mark.asyncio
async def test_classify_shell_emits_ready_and_failure_diagnostics(tmp_path):
    events: list[dict] = []
    set_runtime_diagnostic_auditor(lambda **fields: events.append(fields))
    try:
        result = await _helper(tmp_path, "classify-ok").classify_shell(
            shell_kind="bash",
            executable=str(Path(sys.executable).resolve(strict=True)),
            raw_command="git status",
            timeout=1,
        )
        assert result.verdict is ShellVerdict.ALLOW_READ_ONLY
        assert any(event["status"] == "ready" for event in events)

        ask = await _helper(tmp_path, "hang").classify_shell(
            shell_kind="bash",
            executable=str(Path(sys.executable).resolve(strict=True)),
            raw_command="git status",
            timeout=1,
        )
        assert ask.verdict is ShellVerdict.ASK
        failed = [event for event in events if event["status"] == "failed"]
        assert failed
        assert failed[-1]["failure_code"] == "classifier_unavailable"
    finally:
        set_runtime_diagnostic_auditor(None)


@pytest.mark.asyncio
async def test_helper_integrity_failure_is_recorded_as_diagnostic(tmp_path):
    script = tmp_path / "fake_runtime.py"
    script.write_text(_FAKE_HELPER, encoding="utf-8")
    events: list[dict] = []
    set_runtime_diagnostic_auditor(lambda **fields: events.append(fields))
    try:
        client = NativeRuntimeClient(
            (sys.executable, str(script), "ok"),
            startup_timeout=0.5,
        )
        with pytest.raises(NativeRuntimeError) as exc_info:
            await client.execute(command=("ignored",), cwd=tmp_path, timeout=1)
        assert exc_info.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
        failed = [event for event in events if event["status"] == "failed"]
        assert failed
        assert failed[-1]["failure_code"] == "sandbox_unavailable"
    finally:
        set_runtime_diagnostic_auditor(None)


@pytest.mark.asyncio
async def test_native_helper_does_not_inherit_ambient_user_environment(
    tmp_path,
    monkeypatch,
):
    captured: dict[str, str] = {}
    original_spawn = asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        return await original_spawn(*args, **kwargs)

    for name, value in {
        "PATH": "C:\\attacker",
        "HOME": "C:\\attacker-home",
        "HTTPS_PROXY": "http://attacker.invalid",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "LD_PRELOAD": "/attacker/hook.so",
        "PYTHONSTARTUP": "C:\\attacker\\startup.py",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)

    await _helper(tmp_path, "ok").execute(
        command=("ignored",),
        cwd=tmp_path,
        timeout=1,
    )

    assert captured["ACE_SECURITY_RUNTIME_TOKEN"]
    for name in (
        "PATH",
        "HOME",
        "HTTPS_PROXY",
        "AWS_SECRET_ACCESS_KEY",
        "LD_PRELOAD",
        "PYTHONSTARTUP",
    ):
        assert name not in captured


@pytest.mark.asyncio
async def test_shell_classifier_accepts_only_valid_read_only_contract(tmp_path):
    result = await _helper(tmp_path, "classify-ok").classify_shell(
        shell_kind="bash",
        executable=str(Path(sys.executable).resolve(strict=True)),
        raw_command="git status",
        timeout=1,
    )
    assert result.verdict is ShellVerdict.ALLOW_READ_ONLY
    assert result.parsed_commands == (("git", "status"),)
    assert result.canonical_digest == "a" * 64


@pytest.mark.asyncio
async def test_unknown_classifier_verdict_fails_closed_to_ask(tmp_path):
    result = await _helper(tmp_path, "classify-unknown").classify_shell(
        shell_kind="powershell",
        executable="pwsh",
        raw_command="Get-ChildItem",
        timeout=1,
    )
    assert result.verdict is ShellVerdict.ASK


@pytest.mark.asyncio
async def test_capabilities_are_enforced_for_the_requested_profile(tmp_path):
    # Offline fake backend does not require network-specific capabilities.
    await _helper(tmp_path, "ok").execute(command=("ignored",), cwd=tmp_path, timeout=1)

    with pytest.raises(NativeRuntimeError) as network_error:
        await _helper(tmp_path, "ok").execute(
            command=("ignored",), cwd=tmp_path, network_enabled=True, timeout=1
        )
    assert network_error.value.code is RuntimeErrorCode.NETWORK_UNAVAILABLE

    await _helper(tmp_path, "network-ok").execute(
        command=("ignored",), cwd=tmp_path, network_enabled=True, timeout=1
    )
    with pytest.raises(NativeRuntimeError) as binding_error:
        await _helper(tmp_path, "network-ok").execute(
            command=("ignored",), cwd=tmp_path, allow_local_binding=True, timeout=1
        )
    assert binding_error.value.code is RuntimeErrorCode.NETWORK_UNAVAILABLE


@pytest.mark.asyncio
async def test_duplex_capability_failure_preserves_fail_closed_error(tmp_path):
    runtime = _helper(tmp_path, "missing-duplex")
    command = (str(tmp_path / "mcp-server"),)
    authorization = issue_authorization_snapshot(
        context=SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        action=normalize_exec_action(command, tmp_path),
        profile=PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(
                FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),
            ),
        ),
        additional_permissions=AdditionalPermissionProfile(),
        argv=command,
        cwd=tmp_path,
        environment={},
        helper_argv=runtime._helper_argv,
    )

    with pytest.raises(NativeRuntimeError) as failure:
        await runtime.open_authorized_stdio(
            authorization=authorization,
            env_overrides={},
            max_lifetime_seconds=1,
            max_input_bytes=1024,
            max_output_bytes=1024,
        )

    assert failure.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert "authenticated duplex stdio" in str(failure.value)


@pytest.mark.asyncio
async def test_windows_capabilities_require_token_acl_job_handles_and_wfp(tmp_path):
    with pytest.raises(NativeRuntimeError) as missing_windows:
        await _helper(tmp_path, "windows-missing").execute(
            command=("ignored",), cwd=tmp_path, timeout=1
        )
    assert missing_windows.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE

    await _helper(tmp_path, "windows-ok").execute(
        command=("ignored",), cwd=tmp_path, timeout=1
    )
    with pytest.raises(NativeRuntimeError) as missing_wfp:
        await _helper(tmp_path, "windows-ok").execute(
            command=("ignored",), cwd=tmp_path, network_enabled=True, timeout=1
        )
    assert missing_wfp.value.code is RuntimeErrorCode.NETWORK_UNAVAILABLE
    await _helper(tmp_path, "windows-network-ok").execute(
        command=("ignored",), cwd=tmp_path, network_enabled=True, timeout=1
    )


def test_real_windows_backend_name_is_recognised():
    """The native Windows helper reports ``windows_sandbox_account`` (not ``windows``).

    A bare ``backend == 'windows'`` compare silently skipped the real helper's
    capability gate; this pins the family membership so a future regression is
    caught at the unit level instead of only by an integration matrix.
    """
    real = RuntimeCapabilities(
        backend="windows_sandbox_account",
        filesystem_sandbox=True,
        process_tree_cleanup=True,
        managed_network=True,
    )
    assert real.is_windows_backend
    legacy = RuntimeCapabilities(
        backend="windows",
        filesystem_sandbox=True,
        process_tree_cleanup=True,
        managed_network=False,
    )
    assert legacy.is_windows_backend
    other = RuntimeCapabilities(
        backend="linux_bwrap",
        filesystem_sandbox=True,
        process_tree_cleanup=True,
        managed_network=False,
    )
    assert not other.is_windows_backend


@pytest.mark.asyncio
async def test_verified_protocol_returns_sandbox_result(tmp_path):
    result = await _helper(tmp_path, "ok").execute(
        command=("ignored",), cwd=tmp_path, timeout=1
    )
    assert result.stdout == "sandboxed"
    assert result.stderr == "notice"
    assert result.exit_code == 0
    assert result.capabilities.backend == "fake"


@pytest.mark.asyncio
async def test_runtime_client_derives_every_execution_fact_from_one_signed_snapshot(
    tmp_path,
    monkeypatch,
):
    from crew.security.actions import normalize_exec_action
    from crew.security.context import SecurityContext
    from crew.security.models import AdditionalPermissionProfile
    from crew.security.snapshot import issue_authorization_snapshot

    helper = tmp_path / "runtime"
    helper.write_bytes(b"runtime")
    readable = tmp_path / "readable"
    writable = tmp_path / "writable"
    denied = tmp_path / "denied"
    readable.mkdir()
    writable.mkdir()
    action = normalize_exec_action(("python", "-V"), tmp_path)
    profile = PermissionProfile(
        PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(readable, FilesystemAccess.READ),
            FilesystemEntry(writable, FilesystemAccess.READ_WRITE),
            FilesystemEntry(denied, FilesystemAccess.DENY, escalatable=False),
        ),
        filesystem_globs=(FilesystemGlobEntry(tmp_path, "**/*.pem"),),
        network_entries=(NetworkEntry("example.com", 443, "https"),),
    )
    environment = {"SAFE": "1"}
    signed = issue_authorization_snapshot(
        context=SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        action=action,
        profile=profile,
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment=environment,
        helper_argv=(str(helper),),
    )
    client = NativeRuntimeClient((str(helper),))
    captured = {}

    async def record(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(client, "execute", record)
    result = await client.execute_authorized(
        authorization=signed,
        env_overrides=environment,
        timeout=12.5,
        max_output_bytes=1234,
    )

    assert result.exit_code == 0
    assert captured["command"] == action.argv
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["readable_roots"] == (readable.resolve(),)
    assert captured["writable_roots"] == (writable.resolve(),)
    assert captured["denied_roots"] == (denied.resolve(),)
    assert captured["filesystem_globs"] == (
        {
            "access": "deny_read",
            "pattern": "**/*.pem",
            "root": str(tmp_path.resolve()),
        },
    )
    assert captured["network_rules"] == (
        {
            "host": "example.com",
            "port": 443,
            "protocol": "https",
            "allow": True,
            "allow_private": False,
            "escalatable": True,
        },
    )
    assert captured["env_overrides"] == environment
    assert captured["_use_exact_authorized_paths"] is True

    with pytest.raises(NativeRuntimeError, match="replay"):
        await client.execute_authorized(
            authorization=signed,
            env_overrides=environment,
        )

    with pytest.raises(NativeRuntimeError) as changed_env:
        await client.execute_authorized(
            authorization=signed,
            env_overrides={"SAFE": "tampered"},
        )
    assert changed_env.value.code is RuntimeErrorCode.SANDBOX_DENIED

    mismatched_client = NativeRuntimeClient((str(tmp_path / "different-runtime"),))
    with pytest.raises(NativeRuntimeError) as changed_helper:
        await mismatched_client.execute_authorized(
            authorization=signed,
            env_overrides=environment,
        )
    assert changed_helper.value.code is RuntimeErrorCode.SANDBOX_DENIED


@pytest.mark.asyncio
async def test_authorized_runtime_freezes_environment_before_snapshot_consumption(
    tmp_path,
    monkeypatch,
):
    runtime, request = _authorized_request(
        tmp_path,
        PermissionProfile(PermissionProfileKind.MANAGED),
        environment={"SAFE": "1"},
    )
    environment = request.env_overrides
    assert isinstance(environment, dict)
    entered = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def record(**kwargs):
        entered.set()
        await release.wait()
        captured.update(kwargs)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "execute", record)
    task = asyncio.create_task(
        runtime.execute_authorized(
            authorization=request.authorization_snapshot,
            env_overrides=environment,
        )
    )
    await entered.wait()
    environment["SAFE"] = "tampered"
    environment["INJECTED"] = "after-verification"
    release.set()
    await task

    assert captured["env_overrides"] == {"SAFE": "1"}
    assert captured["env_overrides"] is not environment


@pytest.mark.asyncio
async def test_authorized_runtime_rejects_root_identity_change_before_execution(
    tmp_path,
    monkeypatch,
):
    readable = tmp_path / "readable"
    readable.mkdir()
    runtime, request = _authorized_request(
        tmp_path,
        PermissionProfile(PermissionProfileKind.MANAGED),
        trusted_readable_roots=(readable,),
    )
    original_resolve = Path.resolve

    def redirected_resolve(path, *, strict=False):
        if path == readable:
            return tmp_path.parent
        return original_resolve(path, strict=strict)

    reached_execution = False

    async def forbidden_execute(**_kwargs):
        nonlocal reached_execution
        reached_execution = True
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    monkeypatch.setattr(runtime, "execute", forbidden_execute)

    with pytest.raises(NativeRuntimeError, match="path|路径|identity"):
        await runtime.execute_authorized(
            authorization=request.authorization_snapshot,
            env_overrides=request.env_overrides or {},
        )

    assert not reached_execution


@pytest.mark.asyncio
async def test_authorized_runtime_rejects_glob_root_identity_change_before_execution(
    tmp_path,
    monkeypatch,
):
    glob_root = tmp_path / "glob-root"
    glob_root.mkdir()
    runtime, request = _authorized_request(
        tmp_path,
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem_globs=(FilesystemGlobEntry(glob_root, "**/*.pem"),),
        ),
    )
    original_resolve = Path.resolve

    def redirected_resolve(path, *, strict=False):
        if path == glob_root:
            return tmp_path.parent
        return original_resolve(path, strict=strict)

    reached_execution = False

    async def forbidden_execute(**_kwargs):
        nonlocal reached_execution
        reached_execution = True
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    monkeypatch.setattr(runtime, "execute", forbidden_execute)

    with pytest.raises(NativeRuntimeError, match="glob root|identity"):
        await runtime.execute_authorized(
            authorization=request.authorization_snapshot,
            env_overrides=request.env_overrides or {},
        )

    assert not reached_execution


@pytest.mark.asyncio
async def test_broker_forwards_the_same_snapshot_without_recompiling_authority(tmp_path):
    from crew.security.actions import normalize_exec_action
    from crew.security.context import SecurityContext
    from crew.security.models import AdditionalPermissionProfile
    from crew.security.snapshot import issue_authorization_snapshot

    helper = tmp_path / "runtime"
    helper.write_bytes(b"runtime")
    action = normalize_exec_action(("python", "-V"), tmp_path)
    environment = {"SAFE": "1"}
    signed = issue_authorization_snapshot(
        context=SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        action=action,
        profile=PermissionProfile(PermissionProfileKind.MANAGED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment=environment,
        helper_argv=(str(helper),),
    )

    class SnapshotOnlyRuntime:
        async def execute(self, **_kwargs):
            raise AssertionError("broker recompiled loose execution fields")

        async def execute_authorized(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = SnapshotOnlyRuntime()
    result = await SecurityExecutionBroker(runtime).execute(  # type: ignore[arg-type]
        ExecutionRequest(
            authorization_snapshot=signed,
            command=("attacker", "--expanded"),
            cwd=tmp_path.parent,
            permission_profile=PermissionProfile(
                PermissionProfileKind.MANAGED,
                filesystem=(
                    FilesystemEntry(tmp_path.parent, FilesystemAccess.READ_WRITE),
                ),
            ),
            additional_permissions=AdditionalPermissionProfile(
                filesystem=(
                    FilesystemEntry(tmp_path.parent, FilesystemAccess.READ_WRITE),
                ),
            ),
            trusted_readable_roots=(tmp_path.parent,),
            env_overrides=environment,
            timeout_seconds=7.5,
            max_output_bytes=321,
        )
    )

    assert result == "result"
    assert runtime.kwargs["authorization"] is signed
    assert runtime.kwargs["env_overrides"] == environment
    assert runtime.kwargs["timeout"] == 7.5
    assert runtime.kwargs["max_output_bytes"] == 321


@pytest.mark.asyncio
async def test_broker_rejects_legacy_unbound_execution_fields(tmp_path):
    class NeverRuntime:
        async def execute(self, **_kwargs):
            raise AssertionError("unbound execution fields reached the native runtime")

        async def execute_authorized(self, **_kwargs):
            raise AssertionError("missing snapshot reached the authorized runtime path")

    with pytest.raises(ValueError, match="authorization snapshot"):
        await SecurityExecutionBroker(NeverRuntime()).execute(  # type: ignore[arg-type]
            ExecutionRequest(
                command=("python", "-V"),
                cwd=tmp_path,
                permission_profile=PermissionProfile(PermissionProfileKind.MANAGED),
            )
        )


@pytest.mark.asyncio
async def test_authorized_runtime_rechecks_snapshot_helper_digest_at_spawn(
    tmp_path,
    monkeypatch,
):
    runtime, request = _authorized_request(
        tmp_path,
        PermissionProfile(PermissionProfileKind.MANAGED),
    )
    helper = Path(runtime._helper_argv[0])
    execute = runtime.execute

    async def swap_then_execute(**kwargs):
        helper.write_bytes(b"replaced-after-snapshot-verification")
        return await execute(**kwargs)

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("replaced helper reached process creation")

    monkeypatch.setattr(runtime, "execute", swap_then_execute)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)

    with pytest.raises(NativeRuntimeError) as caught:
        await SecurityExecutionBroker(runtime).execute(request)

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["bad-ready", "missing-ready-capability", "legacy-ready", "bad-nonce"],
)
async def test_protocol_mismatch_fails_closed(tmp_path, mode):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, mode).execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "output-before-start",
        "bad-sequence",
        "invalid-base64",
        "oversized-chunk",
        "oversized-frame",
        "unknown-event",
        "invalid-exit-code",
        "premature-eof",
        "extra-after-terminal",
    ],
)
async def test_invalid_event_stream_fails_closed(tmp_path, mode):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, mode).execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH


@pytest.mark.asyncio
async def test_error_is_valid_terminal_before_started(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "error-before-start").execute(
            command=("ignored",), cwd=tmp_path, timeout=1
        )
    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


@pytest.mark.asyncio
async def test_aggregate_output_limit_fails_closed(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "ok").execute(
            command=("ignored",), cwd=tmp_path, timeout=1, max_output_bytes=5
        )
    assert caught.value.code is RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH


@pytest.mark.asyncio
async def test_timeout_covers_the_whole_event_stream(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "slow-stream").execute(
            command=("ignored",), cwd=tmp_path, timeout=0.1
        )
    assert caught.value.code is RuntimeErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_request_carries_binary_stdin_and_environment_overrides(tmp_path):
    result = await _helper(tmp_path, "assert-request").execute(
        command=("ignored",),
        cwd=tmp_path,
        stdin=b"\x00prompt\xff",
        env_overrides={"CODEX_API_KEY": "secret"},
        timeout=1,
    )
    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
async def test_request_carries_canonical_deny_read_glob_rules(tmp_path):
    result = await _helper(tmp_path, "assert-glob-request").execute(
        command=("ignored",),
        cwd=tmp_path,
        filesystem_globs=(
            {
                "access": "deny_read",
                "pattern": "**/*.pem",
                "root": str(tmp_path.resolve()),
            },
        ),
        timeout=1,
    )

    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "stdin"),
    [("assert-no-stdin", None), ("assert-empty-stdin", b"")],
)
async def test_request_distinguishes_absent_and_empty_stdin(tmp_path, mode, stdin):
    await _helper(tmp_path, mode).execute(
        command=("ignored",), cwd=tmp_path, stdin=stdin, timeout=1
    )


@pytest.mark.asyncio
async def test_activity_callbacks_fire_once_without_output_data(tmp_path):
    started = []
    streams = []
    result = await _helper(tmp_path, "repeated-output").execute(
        command=("ignored",),
        cwd=tmp_path,
        timeout=1,
        on_started=started.append,
        on_output=streams.append,
    )
    assert result.stdout == "sandboxed-again"
    assert started == [123]
    assert streams == ["stdout", "stderr"]


@pytest.mark.asyncio
async def test_callback_failure_does_not_change_command_result(tmp_path):
    def fail(_value):
        raise RuntimeError("metrics unavailable")

    result = await _helper(tmp_path, "ok").execute(
        command=("ignored",),
        cwd=tmp_path,
        timeout=1,
        on_started=fail,
        on_output=fail,
    )
    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdin", "env_overrides"),
    [
        (b"x" * (1024 * 1024 + 1), {}),
        (None, {"INVALID-NAME": "value"}),
        (None, {"VALID_NAME": "nul\x00value"}),
        (None, {"HTTP_PROXY": "http://attacker"}),
        (None, {"ACE_SANDBOX": "attacker"}),
        (None, {"ACE_SECURITY_RUNTIME_TOKEN": "attacker"}),
        (None, {"PATH": "/attacker/bin"}),
        (None, {"LD_PRELOAD": "/attacker/hook.so"}),
        (None, {"BASH_ENV": "/attacker/startup"}),
        (None, {"SAFE_NAME": "one", "safe_name": "two"}),
        (None, {"LARGE": "x" * (256 * 1024)}),
    ],
    ids=[
        "stdin-too-large",
        "invalid-env-name",
        "env-nul",
        "proxy-env-reserved",
        "sandbox-marker-reserved",
        "runtime-env-reserved",
        "path-env-reserved",
        "loader-env-reserved",
        "startup-env-reserved",
        "case-folded-env-duplicate",
        "env-too-large",
    ],
)
async def test_invalid_stdin_or_environment_is_rejected_before_spawn(
    tmp_path, stdin, env_overrides
):
    client = NativeRuntimeClient((str(tmp_path / "must-not-spawn"),))
    with pytest.raises(ValueError):
        await client.execute(
            command=("ignored",),
            cwd=tmp_path,
            stdin=stdin,
            env_overrides=env_overrides,
            timeout=1,
        )


@pytest.mark.asyncio
async def test_invalid_deny_read_glob_is_rejected_before_helper_spawn(
    tmp_path,
    monkeypatch,
):
    client = NativeRuntimeClient((str(tmp_path / "must-not-spawn"),))
    reached_spawn = False

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal reached_spawn
        reached_spawn = True
        raise AssertionError("invalid glob reached helper process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    with pytest.raises(ValueError, match="glob"):
        await client.execute(
            command=("ignored",),
            cwd=tmp_path,
            filesystem_globs=(
                {
                    "access": "deny_read",
                    "pattern": "[unterminated",
                    "root": str(tmp_path.resolve()),
                },
            ),
            timeout=1,
        )

    assert not reached_spawn


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_spawn(tmp_path):
    client = NativeRuntimeClient((str(tmp_path / "must-not-spawn"),))
    with pytest.raises(ValueError, match="request exceeds"):
        await client.execute(
            command=("x" * (2 * 1024 * 1024),),
            cwd=tmp_path,
            timeout=1,
        )


@pytest.mark.asyncio
async def test_missing_capability_never_falls_back(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "missing-capability").execute(
            command=("ignored",), cwd=tmp_path, timeout=1
        )
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_crash_is_stable_error(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "crash").execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.RUNTIME_CRASHED


@pytest.mark.asyncio
async def test_missing_helper_is_unavailable(tmp_path):
    client = NativeRuntimeClient((str(tmp_path / "absent-runtime"),))
    with pytest.raises(NativeRuntimeError) as caught:
        await client.execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_helper_digest_mismatch_fails_closed(tmp_path):
    # 一个真实存在的二进制 + 一份 binary_sha256 不匹配的 manifest 必须失败关闭，
    # 不能 spawn。这把 Python 侧的完整性校验从"零"提升到与 Desktop sha256 gate 一致。
    binary = tmp_path / "fake-runtime"
    binary.write_bytes(b"not-the-real-runtime")
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        '{"schema": 2, "binary_sha256": "deadbeef", "binary_name": "fake-runtime"}',
        encoding="utf-8",
    )
    client = NativeRuntimeClient((str(binary),))
    with pytest.raises(NativeRuntimeError) as caught:
        await client.execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_legacy_manifest_fails_closed(tmp_path):
    binary = tmp_path / "fake-runtime"
    binary.write_bytes(b"not-the-real-runtime")
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        '{"schema_version": 1, "files": [{"name": "fake-runtime"}]}',
        encoding="utf-8",
    )
    client = NativeRuntimeClient((str(binary),))
    with pytest.raises(NativeRuntimeError) as caught:
        await client.execute(command=("ignored",), cwd=tmp_path, timeout=1)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_timeout_kills_helper_tree(tmp_path):
    with pytest.raises(NativeRuntimeError) as caught:
        await _helper(tmp_path, "hang").execute(command=("ignored",), cwd=tmp_path, timeout=0.1)
    assert caught.value.code is RuntimeErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_broker_preserves_read_write_deny_and_network_semantics(tmp_path, monkeypatch):
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(tmp_path / "write", FilesystemAccess.READ_WRITE),
            FilesystemEntry(tmp_path / "read", FilesystemAccess.READ),
            FilesystemEntry(tmp_path / "deny", FilesystemAccess.DENY, escalatable=False),
        ),
        network=NetworkPolicy.RESTRICTED,
    )
    runtime, request = _authorized_request(
        tmp_path,
        profile,
        trusted_readable_roots=(tmp_path / "runtime-skills",),
    )
    captured = {}

    async def record(**kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(runtime, "execute", record)
    result = await SecurityExecutionBroker(runtime).execute(request)

    assert result == "result"
    assert captured["writable_roots"] == (tmp_path / "write",)
    assert captured["readable_roots"] == (
        tmp_path / "read",
        tmp_path / "runtime-skills",
    )
    assert captured["denied_roots"] == (tmp_path / "deny",)
    assert captured["network_enabled"] is False


@pytest.mark.asyncio
async def test_broker_does_not_forward_runtime_owned_protected_read_roots(
    tmp_path,
    monkeypatch,
):
    """Missing .git/.agents/.crew guards are enforced by each runtime, not resolved as host reads."""
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),
            FilesystemEntry(
                tmp_path / ".agents",
                FilesystemAccess.READ,
                escalatable=False,
            ),
        ),
    )
    runtime, request = _authorized_request(
        tmp_path,
        profile,
        trusted_readable_roots=(tmp_path / "runtime-skills",),
    )
    captured = {}

    async def record(**kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(runtime, "execute", record)
    await SecurityExecutionBroker(runtime).execute(request)

    assert captured["writable_roots"] == (tmp_path,)
    assert captured["readable_roots"] == ()


@pytest.mark.asyncio
async def test_broker_passes_only_exact_network_amendments(tmp_path, monkeypatch):
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        network_entries=(
            NetworkEntry("Example.COM.", 443, "https"),
            NetworkEntry(
                "blocked.example", 80, "http", NetworkAccess.DENY, escalatable=False
            ),
        ),
    )
    runtime, request = _authorized_request(tmp_path, profile)
    captured = {}

    async def record(**kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(runtime, "execute", record)
    await SecurityExecutionBroker(runtime).execute(request)

    assert captured["network_enabled"] is True
    assert captured["network_rules"] == (
        {
            "host": "blocked.example",
            "port": 80,
            "protocol": "http",
            "allow": False,
            "allow_private": False,
            "escalatable": False,
        },
        {
            "host": "example.com",
            "port": 443,
            "protocol": "https",
            "allow": True,
            "allow_private": False,
            "escalatable": True,
        },
    )


@pytest.mark.asyncio
async def test_broker_passes_process_data_and_activity_callbacks_once(tmp_path, monkeypatch):
    profile = PermissionProfile(kind=PermissionProfileKind.MANAGED)
    started = [].append
    output = [].append
    runtime, request = _authorized_request(
        tmp_path,
        profile,
        environment={"API_KEY": "secret"},
        stdin=b"prompt",
        timeout_seconds=12.5,
        max_output_bytes=1234,
    )
    captured = {}
    calls = 0

    async def record(**kwargs):
        nonlocal calls
        calls += 1
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(runtime, "execute", record)
    result = await SecurityExecutionBroker(runtime).execute(
        request,
        on_started=started,
        on_output=output,
    )

    assert result == "result"
    assert calls == 1
    assert captured["stdin"] == b"prompt"
    assert captured["env_overrides"] == {"API_KEY": "secret"}
    assert captured["timeout"] == 12.5
    assert captured["max_output_bytes"] == 1234
    assert captured["on_started"] is started
    assert captured["on_output"] is output


@pytest.mark.asyncio
async def test_broker_refuses_disabled_profile_without_spawning(tmp_path):
    runtime, request = _authorized_request(
        tmp_path,
        PermissionProfile(kind=PermissionProfileKind.DISABLED),
    )
    with pytest.raises(NativeRuntimeError) as caught:
        await SecurityExecutionBroker(runtime).execute(request)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED
