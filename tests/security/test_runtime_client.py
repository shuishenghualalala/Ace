"""Protocol and fail-closed tests for the native runtime client."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crew.security.runtime_client import (
    NativeRuntimeClient,
    NativeRuntimeError,
    RuntimeCapabilities,
    RuntimeErrorCode,
    ShellVerdict,
)
from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    NetworkAccess,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
)


_FAKE_HELPER = r'''
import base64, json, os, sys, time
mode = sys.argv[1]
version = 999 if mode == "bad-ready" else 2
ready = {"type":"ready", "version":version, "capabilities":["stdin_once", "stream_output", "readonly_roots"]}
if mode == "missing-ready-capability":
    ready["capabilities"] = ["stdin_once"]
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
payload = request["request"]
if mode == "assert-request":
    if base64.b64decode(payload["stdin_b64"]) != b"\x00prompt\xff":
        raise SystemExit(5)
    if payload["env_overrides"] != {"CODEX_API_KEY": "secret"}:
        raise SystemExit(6)
    if payload["readonly_roots"] != [os.path.join(payload["cwd"], ".agents")]:
        raise SystemExit(9)
if mode == "assert-home-files":
    if payload["home_files"] != {".agent/auth": base64.b64encode(b"token").decode()}:
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


_INTERACTIVE_HELPER = r'''
import base64, json, sys

print(json.dumps({
    "type": "ready",
    "version": 2,
    "capabilities": ["stdin_once", "stream_output", "stdin_bidirectional", "readonly_roots"],
}), flush=True)
open_request = json.loads(sys.stdin.readline())
print(json.dumps({
    "version": 2,
    "nonce": open_request["nonce"],
    "seq": 0,
    "type": "started",
    "pid": 123,
    "capabilities": {
        "backend": "fake",
        "filesystem_sandbox": True,
        "process_tree_cleanup": True,
        "managed_network": False,
    },
}), flush=True)
seq = 1
for line in sys.stdin:
    request = json.loads(line)["request"]
    if request["op"] == "interactive_write":
        data = base64.b64decode(request["data_b64"])
        print(json.dumps({
            "version": 2,
            "nonce": open_request["nonce"],
            "seq": seq,
            "type": "stdout",
            "data_b64": base64.b64encode(data).decode(),
        }), flush=True)
        seq += 1
    elif request["op"] == "interactive_close":
        print(json.dumps({
            "version": 2,
            "nonce": open_request["nonce"],
            "seq": seq,
            "type": "completed",
            "exit_code": 0,
        }), flush=True)
        break
'''


def _helper(tmp_path: Path, mode: str) -> NativeRuntimeClient:
    script = tmp_path / "fake_runtime.py"
    script.write_text(_FAKE_HELPER, encoding="utf-8")
    return NativeRuntimeClient((sys.executable, str(script), mode), startup_timeout=0.5)


def _interactive_helper(tmp_path: Path) -> NativeRuntimeClient:
    script = tmp_path / "fake_interactive_runtime.py"
    script.write_text(_INTERACTIVE_HELPER, encoding="utf-8")
    return NativeRuntimeClient((sys.executable, str(script)), startup_timeout=0.5)


@pytest.mark.asyncio
async def test_interactive_session_forwards_bidirectional_stdio(tmp_path):
    session = await _interactive_helper(tmp_path).open_interactive(
        command=("ignored",),
        cwd=tmp_path,
        timeout=1,
    )
    await session.write(b"hello\n")
    assert await session.read_chunk() == b"hello\n"

    await session.close_child_stdin()
    assert await session.read_chunk() is None
    await session.close()


@pytest.mark.asyncio
async def test_large_helper_stderr_is_drained_without_deadlock(tmp_path):
    result = await _helper(tmp_path, "stderr-flood").execute(
        command=("ignored",), cwd=tmp_path, timeout=2
    )
    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
async def test_shell_classifier_accepts_only_valid_read_only_contract(tmp_path):
    result = await _helper(tmp_path, "classify-ok").classify_shell(
        shell_kind="bash",
        executable="/bin/bash",
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
@pytest.mark.parametrize("mode", ["bad-ready", "missing-ready-capability", "bad-nonce"])
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
        readonly_roots=(tmp_path / ".agents",),
        stdin=b"\x00prompt\xff",
        env_overrides={"CODEX_API_KEY": "secret"},
        timeout=1,
    )
    assert result.stdout == "sandboxed"


@pytest.mark.asyncio
async def test_request_carries_projected_home_files(tmp_path):
    result = await _helper(tmp_path, "assert-home-files").execute(
        command=("ignored",),
        cwd=tmp_path,
        home_files={".agent/auth": b"token"},
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
        (None, {"PATH": "/tmp/attacker"}),
        (None, {"ACE_SECURITY_RUNTIME_TOKEN": "attacker"}),
        (None, {"LARGE": "x" * (256 * 1024)}),
    ],
    ids=[
        "stdin-too-large",
        "invalid-env-name",
        "env-nul",
        "proxy-env-reserved",
        "sandbox-env-reserved",
        "runtime-env-reserved",
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
async def test_broker_preserves_read_write_deny_and_network_semantics(tmp_path):
    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(tmp_path / "write", FilesystemAccess.READ_WRITE),
            FilesystemEntry(tmp_path / "read", FilesystemAccess.READ),
            FilesystemEntry(tmp_path / "deny", FilesystemAccess.DENY, escalatable=False),
        ),
        network=NetworkPolicy.RESTRICTED,
    )
    result = await broker.execute(
        ExecutionRequest(
            command=("test",),
            cwd=tmp_path,
            permission_profile=profile,
            trusted_readable_roots=(tmp_path / "runtime-skills",),
        )
    )
    assert result == "result"
    assert runtime.kwargs["writable_roots"] == [tmp_path / "write"]
    assert runtime.kwargs["readable_roots"] == [
        tmp_path / "runtime-skills",
        tmp_path / "read",
    ]
    assert runtime.kwargs["denied_roots"] == [tmp_path / "deny"]
    assert runtime.kwargs["readonly_roots"] == []
    assert runtime.kwargs["network_enabled"] is False


@pytest.mark.asyncio
async def test_broker_derives_readonly_roots_for_host_python_venv_entrypoint(tmp_path):
    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    environment = tmp_path / "external-agent" / "venv"
    interpreter = environment / "bin" / "python"
    entrypoint = environment / "bin" / "external-agent"
    base_python = tmp_path / "python-base" / "bin" / "python3.12"
    base_stdlib = tmp_path / "python-base" / "lib" / "python3.12"
    interpreter.parent.mkdir(parents=True)
    base_python.parent.mkdir(parents=True)
    base_stdlib.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    interpreter.symlink_to(base_python)
    (environment / "pyvenv.cfg").write_text(
        f"home = {base_python.parent}\n", encoding="utf-8"
    )
    entrypoint.write_text(f"#!{interpreter}\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),),
    )

    await broker.execute(
        ExecutionRequest(
            command=(str(entrypoint), "--version"),
            cwd=workspace,
            permission_profile=profile,
        )
    )

    assert runtime.kwargs["readable_roots"] == [
        environment.resolve(),
        (tmp_path / "python-base" / "lib").resolve(),
        base_stdlib.resolve(),
    ]


@pytest.mark.asyncio
async def test_broker_does_not_derive_venv_roots_for_workspace_script(tmp_path):
    workspace = tmp_path / "workspace"
    environment = tmp_path / "external-agent" / "venv"
    workspace.mkdir()
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "python").write_text("", encoding="utf-8")
    (environment / "pyvenv.cfg").write_text(
        f"home = {tmp_path / 'python-base' / 'bin'}\n", encoding="utf-8"
    )
    entrypoint = workspace / "entrypoint"
    entrypoint.write_text(f"#!{environment / 'bin' / 'python'}\n", encoding="utf-8")

    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),),
    )
    await broker.execute(
        ExecutionRequest(
            command=(str(entrypoint),),
            cwd=workspace,
            permission_profile=profile,
        )
    )

    assert runtime.kwargs["readable_roots"] == []


@pytest.mark.asyncio
async def test_broker_does_not_forward_runtime_owned_protected_read_roots(tmp_path):
    """Missing .git/.agents/.crew guards are enforced by each runtime, not resolved as host reads."""

    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),
            FilesystemEntry(tmp_path / ".agents", FilesystemAccess.READ, escalatable=False),
        ),
    )
    await broker.execute(
        ExecutionRequest(
            command=("test",),
            cwd=tmp_path,
            permission_profile=profile,
            trusted_readable_roots=(tmp_path / "runtime-skills",),
        )
    )
    assert runtime.kwargs["writable_roots"] == [tmp_path]
    assert runtime.kwargs["readable_roots"] == []


@pytest.mark.asyncio
async def test_broker_forwards_immutable_read_roots_to_the_native_runtime(tmp_path):
    """Missing metadata guards use the native read-only carve-out contract."""

    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
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

    await broker.execute(
        ExecutionRequest(
            command=("test",),
            cwd=tmp_path,
            permission_profile=profile,
            trusted_readable_roots=(tmp_path / "runtime-skills",),
        )
    )

    assert runtime.kwargs["writable_roots"] == [tmp_path]
    assert runtime.kwargs["readable_roots"] == []
    assert runtime.kwargs["readonly_roots"] == [tmp_path / ".agents"]


@pytest.mark.asyncio
async def test_broker_carves_workspace_from_protected_runtime_home(tmp_path):
    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime_home = tmp_path / "runtime-home"
    workspace = runtime_home / "accounts" / "owner" / "task_workspaces" / "default"
    workspace.mkdir(parents=True)
    database = runtime_home / "crew.db"
    database.write_text("protected", encoding="utf-8")
    runtime = RecordingRuntime()
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),
            FilesystemEntry(runtime_home, FilesystemAccess.DENY, escalatable=False),
            FilesystemEntry(database, FilesystemAccess.DENY, escalatable=False),
        ),
    )

    await SecurityExecutionBroker(runtime).execute(  # type: ignore[arg-type]
        ExecutionRequest(command=("test",), cwd=workspace, permission_profile=profile)
    )

    assert runtime.kwargs["writable_roots"] == [workspace]
    assert runtime.kwargs["denied_roots"] == [database]


@pytest.mark.asyncio
async def test_broker_passes_only_exact_network_amendments(tmp_path):
    class RecordingRuntime:
        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        network_entries=(
            NetworkEntry("Example.COM.", 443, "https"),
            NetworkEntry(
                "blocked.example", 80, "http", NetworkAccess.DENY, escalatable=False
            ),
        ),
    )
    await broker.execute(ExecutionRequest(command=("test",), cwd=tmp_path, permission_profile=profile))

    assert runtime.kwargs["network_enabled"] is True
    assert runtime.kwargs["network_rules"] == [
        {
            "host": "example.com",
            "port": 443,
            "protocol": "https",
            "allow": True,
            "allow_private": False,
            "escalatable": True,
        },
        {
            "host": "blocked.example",
            "port": 80,
            "protocol": "http",
            "allow": False,
            "allow_private": False,
            "escalatable": False,
        },
    ]


@pytest.mark.asyncio
async def test_broker_passes_process_data_and_activity_callbacks_once(tmp_path):
    class RecordingRuntime:
        calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            self.kwargs = kwargs
            return "result"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    profile = PermissionProfile(kind=PermissionProfileKind.MANAGED)
    started = [].append
    output = [].append
    request = ExecutionRequest(
        command=("test",),
        cwd=tmp_path,
        permission_profile=profile,
        stdin=b"prompt",
        env_overrides={"API_KEY": "secret"},
        timeout_seconds=12.5,
        max_output_bytes=1234,
    )

    result = await broker.execute(request, on_started=started, on_output=output)

    assert result == "result"
    assert runtime.calls == 1
    assert runtime.kwargs["stdin"] == b"prompt"
    assert runtime.kwargs["env_overrides"] == {"API_KEY": "secret"}
    assert runtime.kwargs["timeout"] == 12.5
    assert runtime.kwargs["max_output_bytes"] == 1234
    assert runtime.kwargs["on_started"] is started
    assert runtime.kwargs["on_output"] is output


@pytest.mark.asyncio
async def test_broker_merges_system_callback_network_permission(tmp_path):
    class RecordingRuntime:
        async def open_interactive(self, **kwargs):
            self.kwargs = kwargs
            return "session"

    runtime = RecordingRuntime()
    broker = SecurityExecutionBroker(runtime)  # type: ignore[arg-type]
    request = ExecutionRequest(
        command=("test",),
        cwd=tmp_path,
        permission_profile=PermissionProfile(PermissionProfileKind.MANAGED),
        additional_permissions=AdditionalPermissionProfile(
            network=(
                NetworkEntry(
                    "127.0.0.1",
                    8123,
                    "http",
                    NetworkAccess.ALLOW,
                    allow_private=True,
                    escalatable=False,
                ),
            ),
        ),
    )

    assert await broker.open_interactive(request) == "session"
    assert runtime.kwargs["network_enabled"] is True
    assert runtime.kwargs["network_rules"] == [
        {
            "host": "127.0.0.1",
            "port": 8123,
            "protocol": "http",
            "allow": True,
            "allow_private": True,
            "escalatable": False,
        }
    ]


@pytest.mark.asyncio
async def test_broker_refuses_disabled_profile_without_spawning(tmp_path):
    class NeverRuntime:
        async def execute(self, **kwargs):
            raise AssertionError("disabled profile must not enter managed runtime")

    broker = SecurityExecutionBroker(NeverRuntime())  # type: ignore[arg-type]
    profile = PermissionProfile(kind=PermissionProfileKind.DISABLED)
    with pytest.raises(ValueError, match="outside the managed security broker"):
        await broker.execute(
            ExecutionRequest(command=("test",), cwd=tmp_path, permission_profile=profile)
        )
