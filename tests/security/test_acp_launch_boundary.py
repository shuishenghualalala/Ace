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
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from crew.agent.external import process_lifecycle
from crew.security.context import SecurityContext
from crew.security.launch import current_process_launch, issue_process_launch, ProcessLaunch
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
from crew.security.service import ExecAuthorization


def _disabled_launch(tmp_path):
    return issue_process_launch(
        SecurityContext(
            os_user="test-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )


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


@pytest.mark.asyncio
async def test_codex_refuses_host_spawn_when_launch_missing(tmp_path, monkeypatch) -> None:
    from crew.agent.external import codex_adapter
    from crew.agent.external.codex_adapter import CodexAdapterError
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    monkeypatch.setattr(
        codex_adapter,
        "spawn_authorized_external_process",
        lambda *_args, **_kwargs: pytest.fail("missing authority reached Codex spawn"),
    )
    token = current_process_launch.set(None)
    try:
        events = codex_adapter.stream_codex_events(
            RuntimeExecutionRequest(
                executable_path=sys.executable,
                provider="codex",
                prompt="irrelevant",
                cwd=str(tmp_path),
            )
        )
        with pytest.raises(CodexAdapterError, match="缺少安全启动上下文"):
            async for _ in events:
                pass
    finally:
        current_process_launch.reset(token)


@pytest.mark.asyncio
async def test_claude_refuses_host_spawn_when_launch_missing(tmp_path, monkeypatch) -> None:
    from crew.agent.external import cli_adapter
    from crew.agent.external.cli_adapter import ExternalCliError
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    monkeypatch.setattr(
        cli_adapter,
        "spawn_authorized_external_process",
        lambda *_args, **_kwargs: pytest.fail("missing authority reached Claude spawn"),
    )
    token = current_process_launch.set(None)
    try:
        events = cli_adapter.stream_claude_events(
            RuntimeExecutionRequest(
                executable_path=sys.executable,
                provider="claude-code",
                prompt="irrelevant",
                cwd=str(tmp_path),
            )
        )
        with pytest.raises(ExternalCliError, match="缺少安全启动上下文"):
            async for _ in events:
                pass
    finally:
        current_process_launch.reset(token)


@pytest.mark.asyncio
async def test_authorized_external_spawn_rejects_stale_launch_before_process_creation(
    tmp_path,
    monkeypatch,
) -> None:
    starts: list[tuple[object, ...]] = []

    async def forbidden_spawn(*args, **kwargs):
        del kwargs
        starts.append(args)
        raise AssertionError("stale launch reached process creation")

    monkeypatch.setattr(process_lifecycle.asyncio, "create_subprocess_exec", forbidden_spawn)
    stale = replace(_disabled_launch(tmp_path), authority_digest="0" * 64)
    token = current_process_launch.set(stale)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await process_lifecycle.spawn_authorized_external_process(
                sys.executable,
                "-c",
                "print('must-not-run')",
                cwd=tmp_path,
            )
    finally:
        current_process_launch.reset(token)

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED
    assert starts == []


@pytest.mark.asyncio
async def test_authorized_external_spawn_rejects_unsafe_environment_before_process_creation(
    tmp_path,
    monkeypatch,
) -> None:
    starts: list[tuple[object, ...]] = []

    async def forbidden_spawn(*args, **kwargs):
        del kwargs
        starts.append(args)
        raise AssertionError("unsafe environment reached process creation")

    monkeypatch.setattr(process_lifecycle.asyncio, "create_subprocess_exec", forbidden_spawn)
    token = current_process_launch.set(_disabled_launch(tmp_path))
    try:
        with pytest.raises(
            process_lifecycle.ExternalProcessBoundaryError,
            match="LD_PRELOAD",
        ):
            await process_lifecycle.spawn_authorized_external_process(
                sys.executable,
                "-c",
                "print('must-not-run')",
                cwd=tmp_path,
                custom_env={"LD_PRELOAD": str(tmp_path / "attacker.so")},
            )
    finally:
        current_process_launch.reset(token)

    assert starts == []


@pytest.mark.asyncio
async def test_trusted_probe_does_not_inherit_ambient_credentials(
    tmp_path,
    monkeypatch,
) -> None:
    del tmp_path
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid")
    token = current_process_launch.set(None)
    try:
        result = await process_lifecycle.run_trusted_external_probe(
            sys.executable,
            "-c",
            (
                "import json,os;"
                "print(json.dumps({"
                "'ambient':os.getenv('OPENAI_API_KEY'),"
                "'proxy':os.getenv('HTTPS_PROXY'),"
                "'explicit':os.getenv('EXPLICIT_MARKER')"
                "}))"
            ),
            custom_env={"EXPLICIT_MARKER": "bound"},
            timeout=5,
        )
    finally:
        current_process_launch.reset(token)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "ambient": None,
        "proxy": None,
        "explicit": "bound",
    }


@pytest.mark.asyncio
async def test_trusted_probe_enforces_output_limit() -> None:
    token = current_process_launch.set(None)
    try:
        with pytest.raises(
            process_lifecycle.ExternalProcessOutputLimitError,
            match="output exceeds",
        ):
            await process_lifecycle.run_trusted_external_probe(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 8192); sys.stdout.flush()",
                timeout=5,
                max_output_bytes=1024,
            )
    finally:
        current_process_launch.reset(token)


def test_external_process_boundary_never_resolves_bare_commands_from_path(
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / ("agent.cmd" if os.name == "nt" else "agent")
    executable.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(
        process_lifecycle.ExternalProcessBoundaryError,
        match="absolute discovered path",
    ):
        process_lifecycle.resolve_external_executable("agent")


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


class _AllowNetworkApprovalService:
    """测试桩：外部智能体联网审批一律放行（本文件的用例只验证传输层，不覆盖审批决策）。"""

    @staticmethod
    def authorize_exec_action(*_args, **_kwargs) -> ExecAuthorization:
        return ExecAuthorization(True)


def _managed_launch(tmp_path: Path) -> ProcessLaunch:
    # 外部智能体联网需要审批上下文（provider 网段 overlay 经 approval_service 授权），
    # 缺 security_context / approval_service 时 cli_adapter 直接抛 SANDBOX_UNAVAILABLE。
    # 合并后的安全语义：启动决策必须由 issue_process_launch 签发（裸构造无权威 MAC），
    # 且 managed 启动必须携带真实存在的 helper 可执行文件。
    runtime = tmp_path / "native-runtime"
    if not runtime.exists():
        runtime.write_bytes(b"test-runtime")
        runtime.with_name("runtime-manifest.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "binary_name": runtime.name,
                    "binary_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    context = SecurityContext(
        os_user="os-acp",
        owner_account_id="owner-acp",
        workspace_id="workspace-acp",
        workspace_root=tmp_path,
        session_id="session-acp",
        request_id="request-acp",
        task_id="task-acp",
        cwd=tmp_path,
    )
    return issue_process_launch(
        context,
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        helper_argv=(str(runtime),),
        external_security_enabled=True,
        security_context=context,
        approval_service=_AllowNetworkApprovalService(),
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
    monkeypatch.setenv("USERPROFILE", str(owner_home))
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
async def test_managed_codex_app_server_routes_to_brokered_one_shot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """managed 启动决策下 Codex 走 broker 一次性执行（含联网审批 overlay），
    不再使用交互式 app-server 传输（f3aca1b 起的新契约）。"""
    from crew.agent.external import cli_adapter, codex_adapter
    from crew.agent.external.codex_adapter import stream_codex_events
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    captured = {}

    async def fake_run_external_cli(config):
        captured["config"] = config
        return "codex managed"

    async def open_managed(*_args, **_kwargs):
        raise AssertionError("managed 路径不应再使用交互式传输")

    monkeypatch.setattr(cli_adapter, "run_external_cli", fake_run_external_cli)
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
    assert captured["config"].provider == "codex"
    assert captured["config"].prompt == "work"


@pytest.mark.asyncio
async def test_managed_claude_stream_json_routes_to_brokered_one_shot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """managed 启动决策下 Claude 回合走 broker 一次性执行（含联网审批 overlay），
    不再使用交互式 stream-json 传输（f3aca1b 起的新契约）。"""
    from crew.agent.external import cli_adapter
    from crew.agent.external.cli_adapter import stream_claude_events
    from crew.agent.external.runtime_adapter import RuntimeExecutionRequest

    captured = {}

    async def fake_run_external_cli(config):
        captured["config"] = config
        return "claude managed"

    async def open_managed(*_args, **_kwargs):
        raise AssertionError("managed 路径不应再使用交互式传输")

    monkeypatch.setattr(cli_adapter, "run_external_cli", fake_run_external_cli)
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
    assert captured["config"].provider == "claude"
    assert captured["config"].prompt == "work"


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
    launch = _managed_launch(tmp_path)
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
    launch = _managed_launch(tmp_path)
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
    launch = _managed_launch(tmp_path)
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
    launch = _managed_launch(tmp_path)
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
