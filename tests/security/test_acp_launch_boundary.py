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
from crew.security.models import FilesystemAccess, FilesystemEntry, PermissionProfile, PermissionProfileKind


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
    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
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
    }.intersection(env_overrides)
