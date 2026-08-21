from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from crew.security.launch import current_process_launch
from crew.security.runtime_client import (
    NativeRuntimeError,
    NativeRuntimeStdioProcess,
    RuntimeCapabilities,
    RuntimeErrorCode,
)
from crew.tools.mcp_client import (
    MCP_STDIO_INPUT_MAX_BYTES,
    MCP_STDIO_MAX_LIFETIME_SECONDS,
    MCP_STDIO_OUTPUT_MAX_BYTES,
    MCPClientManager,
    _MCPToolDescriptor,
    _ServerWorker,
    _native_stdio_transport,
    _sanitize_tool_descriptor,
    _stdio_env,
)
from crew.tools.registry import Registry


class _FakeMcpClient:
    def __init__(self, _transport, *, mode: str) -> None:
        assert mode == "auto"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakeNativeStream:
    def __init__(self) -> None:
        self.terminated = False

    async def terminate(self) -> None:
        self.terminated = True


def _managed_launch(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        managed=True,
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_id="session-a",
        task_id="task-a",
        profile=SimpleNamespace(
            filesystem=("workspace-only",),
            network=("approved-origin-only",),
        ),
    )


@pytest.mark.asyncio
async def test_malicious_child_is_routed_only_through_bound_native_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "mcp-secret-value-123"
    command = str(Path(sys.executable).resolve())
    config = {
        "command": command,
        "command_sha256": hashlib.sha256(Path(command).read_bytes()).hexdigest(),
        "args": ["malicious_mcp_server.py", "--attempt-host-spawn"],
        "cwd": str(tmp_path),
        "stdio_source": "local",
        "env": {
            "MCP_TOKEN": {
                "source": "local",
                "value": "@ace-secret:v1:bound-marker",
            },
            "SAFE_FLAG": {"source": "local", "value": "1"},
        },
    }
    launch = _managed_launch(tmp_path)
    captured: dict[str, object] = {}
    runtime = _FakeNativeStream()

    def resolve_secrets(name, cfg, *, sections):
        assert name == "malicious"
        assert sections == ("env",)
        assert cfg["env"]["MCP_TOKEN"]["value"].startswith("@ace-secret:")
        return {
            **cfg,
            "env": {
                **cfg["env"],
                "MCP_TOKEN": {"source": "local", "value": secret},
            },
        }

    def finalize(bound_launch, **kwargs):
        captured["launch"] = bound_launch
        captured.update(kwargs)
        return SimpleNamespace(
            snapshot=SimpleNamespace(helper_argv=("trusted-native-helper",))
        )

    class FakeRuntimeClient:
        def __init__(self, helper_argv) -> None:
            assert tuple(helper_argv) == ("trusted-native-helper",)

        async def open_authorized_stdio(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return runtime

    async def forbidden_host_spawn(*_args, **_kwargs):
        raise AssertionError("MCP child must never use host subprocess execution")

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-cross")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid")
    monkeypatch.setattr(
        "crew.tools.mcp_client.resolve_mcp_server_secrets",
        resolve_secrets,
    )
    monkeypatch.setattr(
        "crew.security.launch.finalize_process_launch",
        finalize,
    )
    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient",
        FakeRuntimeClient,
    )
    monkeypatch.setattr("mcp.Client", _FakeMcpClient, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_host_spawn)

    worker = _ServerWorker(
        "malicious",
        config,
        Registry(),
        process_launch=launch,
        working_directory=tmp_path,
    )
    async with AsyncExitStack() as stack:
        await worker._open(stack)

    assert captured["launch"] is launch
    assert captured["argv"] == (
        command,
        "malicious_mcp_server.py",
        "--attempt-host-spawn",
    )
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["environment"] == {
        "MCP_TOKEN": secret,
        "SAFE_FLAG": "1",
    }
    assert captured["credential_environment_names"] == frozenset({"MCP_TOKEN"})
    runtime_kwargs = captured["runtime_kwargs"]
    assert set(runtime_kwargs) == {
        "authorization",
        "env_overrides",
        "max_lifetime_seconds",
        "max_input_bytes",
        "max_output_bytes",
    }
    assert runtime_kwargs["max_lifetime_seconds"] == MCP_STDIO_MAX_LIFETIME_SECONDS
    assert runtime_kwargs["max_input_bytes"] == MCP_STDIO_INPUT_MAX_BYTES
    assert runtime_kwargs["max_output_bytes"] == MCP_STDIO_OUTPUT_MAX_BYTES
    assert "ambient-must-not-cross" not in runtime_kwargs["env_overrides"].values()
    assert runtime.terminated is True


def test_stdio_environment_enforces_explicit_provenance_matches() -> None:
    assert _stdio_env(
        {"SAFE": {"source": "local", "value": "value"}},
        transport_source="local",
    ) == {"SAFE": "value"}
    assert _stdio_env({"SAFE": "legacy-local-value"}) == {
        "SAFE": "legacy-local-value"
    }
    with pytest.raises(ValueError, match="does not match"):
        _stdio_env(
            {"SAFE": {"source": "remote"}},
            transport_source="local",
        )
    with pytest.raises(ValueError, match="does not match"):
        _stdio_env(
            {"SAFE": {"source": "local", "value": "value"}},
            transport_source="remote",
        )
    with pytest.raises(ValueError, match="not allowed"):
        _stdio_env({"PATH": {"source": "local", "value": "attacker"}})


@pytest.mark.asyncio
async def test_malformed_stdio_environment_fails_before_spawn(
    tmp_path: Path,
) -> None:
    worker = _ServerWorker(
        "malformed",
        {
            "command": str(Path(sys.executable).resolve()),
            "env": ["NOT", "AN", "OBJECT"],
        },
        Registry(),
        process_launch=_managed_launch(tmp_path),
        working_directory=tmp_path,
    )

    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="env must be an object"):
            await worker._open(stack)


def test_env_dump_is_redacted_from_malicious_tool_metadata() -> None:
    secret = "top-secret-env-value"
    raw = SimpleNamespace(
        name="dump_env",
        description=f"environment MCP_TOKEN={secret}",
        input_schema={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "string",
                    "default": secret,
                }
            },
        },
    )

    sanitized = _sanitize_tool_descriptor(raw, secret_values=(secret,))

    rendered = json.dumps(
        {
            "description": sanitized.description,
            "schema": sanitized.input_schema,
        },
        sort_keys=True,
    )
    assert secret not in rendered
    assert "[REDACTED]" in rendered or "***" in rendered


class _MemoryStdin:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.frames.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _MemoryProcess:
    def __init__(self) -> None:
        self.stdin = _MemoryStdin()

    async def wait(self) -> int:
        return 0


class _FrameClient:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self.frames = list(frames)
        self.terminated = False

    async def _read_frame(self, _process, _timeout):
        return self.frames.pop(0)

    async def _terminate_tree(self, _process) -> None:
        self.terminated = True

    async def _validate_protocol_eof(self, _process, _deadline) -> None:
        return None


async def _empty_stderr() -> bytes:
    return b""


def _native_process(
    client: _FrameClient,
    process: _MemoryProcess,
    *,
    token: str = "t" * 48,
    nonce: str = "nonce-for-managed-mcp",
    max_input_bytes: int = 4096,
    max_output_bytes: int = 4096,
) -> NativeRuntimeStdioProcess:
    return NativeRuntimeStdioProcess(
        client=client,
        process=process,
        stderr_task=asyncio.create_task(_empty_stderr()),
        token=token,
        nonce=nonce,
        deadline=asyncio.get_running_loop().time() + 2,
        inactivity_timeout=None,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
        capabilities=RuntimeCapabilities(
            backend="fake",
            filesystem_sandbox=True,
            process_tree_cleanup=True,
            managed_network=False,
        ),
    )


@pytest.mark.asyncio
async def test_duplex_input_frames_are_authenticated_and_sequenced() -> None:
    token = "k" * 48
    nonce = "nonce-for-managed-mcp"
    process = _MemoryProcess()
    stream = _native_process(_FrameClient([]), process, token=token, nonce=nonce)

    await stream.send(b'{"jsonrpc":"2.0"}\n')
    await stream.close_stdin()

    data_frame = json.loads(process.stdin.frames[0])
    close_frame = json.loads(process.stdin.frames[1])
    assert data_frame["seq"] == 0
    assert close_frame["seq"] == 1
    assert close_frame["type"] == "stdin_close"
    canonical = (
        b"ace-runtime-stdio-v1\x00"
        + nonce.encode()
        + b"\x00"
        + b"0"
        + b"\x00stdin\x00"
        + data_frame["data_b64"].encode()
    )
    expected = hmac.new(token.encode(), canonical, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(data_frame["mac"], expected)
    assert base64.b64decode(data_frame["data_b64"]) == b'{"jsonrpc":"2.0"}\n'


@pytest.mark.asyncio
async def test_replayed_or_mismatched_output_frame_terminates_runtime() -> None:
    nonce = "nonce-for-managed-mcp"
    frames = [
        {
            "version": 3,
            "nonce": nonce,
            "seq": 1,
            "type": "stdout",
            "data_b64": base64.b64encode(b"first").decode(),
        },
        {
            "version": 3,
            "nonce": nonce,
            "seq": 1,
            "type": "stdout",
            "data_b64": base64.b64encode(b"replay").decode(),
        },
    ]
    client = _FrameClient(frames)
    stream = _native_process(client, _MemoryProcess(), nonce=nonce)

    assert await stream.receive() == ("stdout", b"first")
    with pytest.raises(NativeRuntimeError) as caught:
        await stream.receive()

    assert caught.value.code is RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH
    assert client.terminated is True


@pytest.mark.asyncio
async def test_task_provenance_mismatch_and_session_revoke_block_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    manager = MCPClientManager(
        {"scoped": {"command": str(Path(sys.executable).resolve())}}
    )
    await manager.start(registry)
    launch = _managed_launch(tmp_path)

    async def fake_start(self, *, register_tools=True):
        del register_tools
        self._tools = [
            _MCPToolDescriptor(
                name="mutate",
                description="",
                input_schema={"type": "object", "properties": {}},
            )
        ]
        self._ready.set()
        self._task = asyncio.create_task(asyncio.Event().wait())
        return True

    monkeypatch.setattr(_ServerWorker, "start", fake_start)
    monkeypatch.setattr(
        "crew.security.launch.validate_process_launch",
        lambda _launch, **_kwargs: None,
    )
    lease = await manager.prepare_runtime_tools(
        process_launch=launch,
        cwd=tmp_path,
    )
    assert lease is not None
    worker = manager._scoped_workers[
        ("owner-a", "workspace-a", "session-a", "task-a", "scoped")
    ]
    worker.invoke = AsyncMock(return_value="called")
    handler = manager._make_scoped_handler("scoped", "mutate")

    mismatched = SimpleNamespace(**vars(launch))
    mismatched.task_id = "task-b"
    token = current_process_launch.set(mismatched)
    try:
        result = await handler({})
    finally:
        current_process_launch.reset(token)
    assert "revoked" in result
    worker.invoke.assert_not_awaited()

    await manager.revoke_session("owner-a", "session-a")
    token = current_process_launch.set(launch)
    try:
        result = await handler({})
    finally:
        current_process_launch.reset(token)
    assert "revoked" in result
    worker.invoke.assert_not_awaited()
    await manager.aclose()


class _AdversarialTransportRuntime:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.closed = asyncio.Event()
        self.terminated = False

    async def receive(self):
        if self.outcome == "wait":
            await self.closed.wait()
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED,
                "closed",
            )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def send(self, _data: bytes) -> None:
        return None

    async def close_stdin(self) -> None:
        self.closed.set()

    async def terminate(self) -> None:
        self.terminated = True
        self.closed.set()


@pytest.mark.asyncio
async def test_malicious_child_crash_is_terminal_and_sanitized() -> None:
    runtime = _AdversarialTransportRuntime(("completed", 9))

    async with _native_stdio_transport(runtime) as (read_stream, _write_stream):
        failure = await read_stream.receive()
        assert isinstance(failure, RuntimeError)
        assert "exited unexpectedly" in str(failure)

    assert runtime.terminated is True


@pytest.mark.asyncio
async def test_malicious_child_timeout_terminates_native_transport() -> None:
    import anyio

    runtime = _AdversarialTransportRuntime(
        NativeRuntimeError(
            RuntimeErrorCode.TIMEOUT,
            "secret child detail",
        )
    )

    async with _native_stdio_transport(runtime) as (read_stream, _write_stream):
        with pytest.raises(anyio.EndOfStream):
            await read_stream.receive()

    assert runtime.terminated is True


@pytest.mark.asyncio
async def test_cancelled_mcp_transport_terminates_waiting_child() -> None:
    runtime = _AdversarialTransportRuntime("wait")
    entered = asyncio.Event()

    async def use_transport() -> None:
        async with _native_stdio_transport(runtime):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use_transport())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.terminated is True
