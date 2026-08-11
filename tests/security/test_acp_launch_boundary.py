"""H-2 regression: ACP must refuse host spawn when no ProcessLaunch is bound.

Every security-wired conversation compiles a ProcessLaunch in CrewApp.handle. A
Team member's envelope bypasses that, so ``current_process_launch`` resolves to
None inside its runtime. The ACP adapter previously treated ``None`` as "host
allowed" while every other exec path (execute_captured) refused — so a managed
conversation could still spawn on the host through ACP. It must fail closed,
matching execute_captured.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.security.launch import ProcessLaunch, current_process_launch
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode


@pytest.mark.asyncio
async def test_acp_refuses_host_spawn_when_launch_missing() -> None:
    from crew.agent.external import acp_adapter
    from crew.agent.external.acp_adapter import AcpAdapterError

    token = current_process_launch.set(None)
    try:
        # The refusal happens before the config is read, so a bare object suffices.
        agen = acp_adapter.stream_acp_events("irrelevant", object())  # type: ignore[arg-type]
        with pytest.raises(AcpAdapterError, match="缺少安全启动上下文"):
            async for _ in agen:
                pass
    finally:
        current_process_launch.reset(token)


class _FakeManagedAcpSession:
    def __init__(self) -> None:
        self.process = SimpleNamespace(pid=4242, returncode=0)
        self.stderr_lines: list[str] = []
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False

    async def write(self, data: bytes) -> None:
        message = json.loads(data)
        method = message.get("method")
        request_id = message.get("id")
        if method == "session/prompt":
            await self._frames.put(json.dumps({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "managed ok"},
                    }
                },
            }).encode() + b"\n")
            result = {"stopReason": "end_turn"}
        elif method == "session/new":
            result = {"sessionId": "managed-session"}
        else:
            result = {"ok": True}
        await self._frames.put(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }).encode() + b"\n")

    async def read_chunk(self) -> bytes | None:
        return await self._frames.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._frames.put(None)

    async def abort(self) -> None:
        await self.close()


class _BrokenManagedAcpSession(_FakeManagedAcpSession):
    async def read_chunk(self) -> bytes | None:
        raise NativeRuntimeError(
            RuntimeErrorCode.RUNTIME_CRASHED,
            "native runtime closed the protocol stream",
        )


class _FakeManagedLineSession:
    def __init__(self) -> None:
        self.process = SimpleNamespace(pid=5252, returncode=None)
        self.stderr_lines: list[str] = []
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.writes: list[bytes] = []
        self._closed = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        for raw_line in data.splitlines():
            message = json.loads(raw_line)
            if message.get("method") == "initialize":
                await self._put({"id": message["id"], "result": {}})
                continue
            if message.get("method") == "thread/start":
                await self._put({
                    "id": message["id"],
                    "result": {"thread": {"id": "managed-thread"}},
                })
                continue
            if message.get("method") == "turn/start":
                await self._put({
                    "id": message["id"],
                    "result": {"turn": {"id": "managed-turn"}},
                })
                await self._put({
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "managed-thread",
                        "turnId": "managed-turn",
                        "delta": "codex managed",
                    },
                })
                await self._put({
                    "method": "turn/completed",
                    "params": {
                        "threadId": "managed-thread",
                        "turnId": "managed-turn",
                        "turn": {"status": "completed"},
                    },
                })
                continue
            if message.get("type") == "user":
                await self._put({"type": "system", "session_id": "managed-session"})
                await self._put({
                    "type": "stream_event",
                    "session_id": "managed-session",
                    "event": {
                        "delta": {"type": "text_delta", "text": "claude managed"},
                    },
                })
                await self._put({
                    "type": "result",
                    "session_id": "managed-session",
                    "subtype": "success",
                    "is_error": False,
                })

    async def _put(self, payload: dict) -> None:
        await self._frames.put(json.dumps(payload).encode() + b"\n")

    async def read_chunk(self) -> bytes | None:
        return await self._frames.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.process.returncode = 0
            await self._frames.put(None)

    async def abort(self) -> None:
        await self.close()


def _managed_launch(tmp_path: Path) -> ProcessLaunch:
    return ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
        external_security_enabled=True,
    )


@pytest.mark.asyncio
async def test_managed_external_interactive_compiles_scoped_projection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external.runtime_adapter import (
        RuntimeExecutionRequest,
        open_managed_external_interactive,
    )
    from crew.security.broker import SecurityExecutionBroker

    owner_home = tmp_path / "owner-home"
    credential = owner_home / ".codex" / "auth.json"
    credential.parent.mkdir(parents=True)
    credential.write_text('{"token":"test"}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    async def open_interactive(self, request):
        captured["request"] = request
        return "managed-session"

    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setattr(SecurityExecutionBroker, "open_interactive", open_interactive)
    token = current_process_launch.set(_managed_launch(workspace))
    try:
        result = await open_managed_external_interactive(
            RuntimeExecutionRequest(
                executable_path=sys.executable,
                provider="codex",
                prompt="work",
                cwd=str(workspace),
                credential_home_paths=(".codex/auth.json",),
                network_endpoints=("https://api.example.test/v1",),
                custom_env={"API_KEY": "secret", "HOME": "/host/home"},
            ),
            (sys.executable, "app-server", "--listen", "stdio://"),
        )
    finally:
        current_process_launch.reset(token)

    assert result == "managed-session"
    request = captured["request"]
    assert request.home_files == {".codex/auth.json": b'{"token":"test"}'}
    assert request.env_overrides["API_KEY"] == "secret"
    assert not {
        "PATH",
        "HOME",
        "TMPDIR",
        "PWD",
        "OLDPWD",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }.intersection(request.env_overrides)
    assert request.additional_permissions.network[0].host == "api.example.test"


@pytest.mark.asyncio
async def test_managed_codex_app_server_uses_native_interactive_transport(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external import codex_adapter
    from crew.agent.external.codex_adapter import stream_codex_events
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    session = _FakeManagedLineSession()
    captured = {}

    async def open_managed(request, command):
        captured["request"] = request
        captured["command"] = command
        return session

    monkeypatch.setattr(codex_adapter, "open_managed_external_interactive", open_managed)
    token = current_process_launch.set(_managed_launch(tmp_path))
    try:
        events = [
            event
            async for event in stream_codex_events(RuntimeExecutionRequest(
                executable_path=sys.executable,
                provider="codex",
                prompt="work",
                cwd=str(tmp_path),
                timeout=2,
            ))
        ]
    finally:
        current_process_launch.reset(token)

    assert [event.text for event in events if event.kind == "text"] == ["codex managed"]
    assert captured["command"][1:] == ("app-server", "--listen", "stdio://")
    assert session.writes, "Codex must send protocol frames through the native session"


@pytest.mark.asyncio
async def test_managed_claude_stream_json_uses_native_interactive_transport(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external import cli_adapter
    from crew.agent.external.cli_adapter import stream_claude_events
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    session = _FakeManagedLineSession()
    captured = {}

    async def open_managed(request, command):
        captured["request"] = request
        captured["command"] = command
        return session

    monkeypatch.setattr(cli_adapter, "open_managed_external_interactive", open_managed)
    token = current_process_launch.set(_managed_launch(tmp_path))
    try:
        events = [
            event
            async for event in stream_claude_events(RuntimeExecutionRequest(
                executable_path=sys.executable,
                provider="claude-code",
                prompt="work",
                cwd=str(tmp_path),
                timeout=2,
            ))
        ]
    finally:
        current_process_launch.reset(token)

    assert [event.text for event in events if event.kind == "text"] == ["claude managed"]
    assert captured["command"][1:5] == (
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
    )
    assert session.writes, "Claude must send stream-json frames through the native session"


@pytest.mark.asyncio
async def test_managed_acp_uses_native_broker_and_preserves_stdio_protocol(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external.acp_adapter import AcpAdapterConfig, stream_acp_events
    from crew.security.broker import SecurityExecutionBroker

    captured = {}
    session = _FakeManagedAcpSession()

    async def open_interactive(self, request):
        captured["request"] = request
        return session

    monkeypatch.setattr(SecurityExecutionBroker, "open_interactive", open_interactive)
    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
        external_security_enabled=True,
    )
    token = current_process_launch.set(launch)
    try:
        events = [
            event async for event in stream_acp_events(
                "hello",
                AcpAdapterConfig(
                    executable_path=sys.executable,
                    cwd=str(tmp_path),
                    timeout=2,
                ),
            )
        ]
    finally:
        current_process_launch.reset(token)

    assert [event.text for event in events if event.kind == "text"] == ["managed ok"]
    assert captured["request"].command[0] == str(Path(sys.executable).resolve())
    assert captured["request"].permission_profile.kind is PermissionProfileKind.MANAGED


@pytest.mark.asyncio
async def test_managed_acp_preserves_native_stream_failure_detail(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external.acp_adapter import AcpAdapterConfig, AcpAdapterError, stream_acp_events
    from crew.security.broker import SecurityExecutionBroker

    session = _BrokenManagedAcpSession()

    async def open_interactive(self, request):
        return session

    monkeypatch.setattr(SecurityExecutionBroker, "open_interactive", open_interactive)
    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
        external_security_enabled=True,
    )
    token = current_process_launch.set(launch)
    try:
        with pytest.raises(AcpAdapterError, match="native runtime closed the protocol stream"):
            async for _ in stream_acp_events(
                "hello",
                AcpAdapterConfig(
                    executable_path=sys.executable,
                    cwd=str(tmp_path),
                    timeout=2,
                ),
            ):
                pass
    finally:
        current_process_launch.reset(token)


@pytest.mark.asyncio
async def test_managed_acp_forwards_system_callback_permission(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external.acp_adapter import AcpAdapterConfig, stream_acp_events
    from crew.security.broker import SecurityExecutionBroker

    captured = {}
    session = _FakeManagedAcpSession()

    async def open_interactive(self, request):
        captured["request"] = request
        return session

    monkeypatch.setattr(SecurityExecutionBroker, "open_interactive", open_interactive)
    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
        external_security_enabled=True,
    )
    additional_permissions = AdditionalPermissionProfile(
        network=(NetworkEntry("127.0.0.1", 8123, "http"),),
    )
    token = current_process_launch.set(launch)
    try:
        events = [
            event
            async for event in stream_acp_events(
                "hello",
                AcpAdapterConfig(
                    executable_path=sys.executable,
                    cwd=str(tmp_path),
                    additional_permissions=additional_permissions,
                    timeout=2,
                ),
            )
        ]
    finally:
        current_process_launch.reset(token)

    assert [event.text for event in events if event.kind == "text"] == ["managed ok"]
    assert captured["request"].additional_permissions is additional_permissions


@pytest.mark.asyncio
async def test_managed_acp_does_not_override_native_runtime_environment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.agent.external.acp_adapter import AcpAdapterConfig, stream_acp_events
    from crew.security.broker import SecurityExecutionBroker

    captured = {}
    session = _FakeManagedAcpSession()

    async def open_interactive(self, request):
        captured["request"] = request
        return session

    monkeypatch.setattr(SecurityExecutionBroker, "open_interactive", open_interactive)
    monkeypatch.setenv("PATH", "/host/path")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("HTTP_PROXY", "http://host-proxy")
    monkeypatch.setenv("ACE_SECURITY_RUNTIME_TOKEN", "host-token")
    monkeypatch.setenv("ACE_BUNDLED_RUNTIME", "host-runtime")
    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
        external_security_enabled=True,
    )
    token = current_process_launch.set(launch)
    try:
        events = [
            event
            async for event in stream_acp_events(
                "hello",
                AcpAdapterConfig(
                    executable_path=sys.executable,
                    cwd=str(tmp_path),
                    custom_env={"PROVIDER_API_KEY": "secret", "PATH": "/custom/path"},
                    timeout=2,
                ),
            )
        ]
    finally:
        current_process_launch.reset(token)

    assert [event.text for event in events if event.kind == "text"] == ["managed ok"]
    env_overrides = captured["request"].env_overrides
    assert env_overrides["PROVIDER_API_KEY"] == "secret"
    assert not {
        "PATH",
        "HOME",
        "TMPDIR",
        "PWD",
        "OLDPWD",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "ACE_SECURITY_RUNTIME_TOKEN",
        "ACE_BUNDLED_RUNTIME",
    }.intersection(env_overrides)
