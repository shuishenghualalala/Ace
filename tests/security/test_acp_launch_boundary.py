"""H-2 regression: ACP must refuse host spawn when no ProcessLaunch is bound.

Every security-wired conversation compiles a ProcessLaunch in CrewApp.handle. A
Team member's envelope bypasses that, so ``current_process_launch`` resolves to
None inside its runtime. The ACP adapter previously treated ``None`` as "host
allowed" while every other exec path (execute_captured) refused — so a managed
conversation could still spawn on the host through ACP. It must fail closed,
matching execute_captured.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

import pytest

from crew.agent.external import process_lifecycle
from crew.security.context import SecurityContext
from crew.security.launch import current_process_launch, issue_process_launch
from crew.security.models import PermissionProfile, PermissionProfileKind
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode


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
        with pytest.raises(CodexAdapterError, match="context missing"):
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
        with pytest.raises(ExternalCliError, match="context missing"):
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
