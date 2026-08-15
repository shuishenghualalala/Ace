"""CUA Driver MCP 一键安装服务测试。

全部使用 mock 避免真实下载/安装。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crew.security.context import SecurityContext
from crew.security.launch import issue_process_launch
from crew.security.models import PermissionProfile, PermissionProfileKind
from crew.security.outbound import OutboundHttpResponse
from crew.security.runtime_client import NativeRuntimeError
from crew.tools import cua_setup
from crew.tools.cua_setup import (
    CuaDriverSetupService,
    SetupTask,
    _clean_system_env,
    _download_verified_installer,
    _is_at_spi_installed,
    task_to_dict,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def service() -> CuaDriverSetupService:
    return CuaDriverSetupService()

@pytest.fixture
def admin_launch(tmp_path: Path):
    return issue_process_launch(
        SecurityContext(
            os_user="test-user",
            owner_account_id="email:admin@example.com",
            workspace_id="gateway",
            workspace_root=tmp_path,
            session_id="cua-setup",
            request_id="request",
            task_id="",
            cwd=tmp_path,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )


@pytest.fixture
def mock_crew(tmp_path: Path) -> Any:
    """构造一个最小 Crew 对象，包含 config/registry/reload_mcp_manager。"""
    config = SimpleNamespace(
        mcp_servers={},
        config_path=str(tmp_path / "config.yaml"),
    )
    config.set_mcp_server = MagicMock()
    config.persist_mcp_servers = MagicMock(return_value=Path(config.config_path))

    registry = SimpleNamespace(names=lambda: [])

    crew = SimpleNamespace(
        config=config,
        registry=registry,
        reload_mcp_manager=AsyncMock(),
    )
    return crew


async def test_at_spi_probe_uses_clean_system_env(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundled")
    with patch("crew.tools.cua_setup._run_command", new_callable=AsyncMock) as run:
        run.return_value = ""

        assert await _is_at_spi_installed() is True

    assert "LD_LIBRARY_PATH" not in run.await_args.kwargs["env"]


async def test_cua_subprocess_environment_drops_secrets_and_proxy_overrides(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VLM_API_KEY", "must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")

    env = _clean_system_env()

    assert "VLM_API_KEY" not in env
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert env.get("PATH") == os.environ.get("PATH")


def _write_config(path: Path) -> None:
    path.write_text(
        "llm:\n  active: default\nmcp_servers:\n  bocha:\n    command: echo\n",
        encoding="utf-8",
    )


async def test_status_when_not_installed(service: CuaDriverSetupService):
    with patch("crew.tools.cua_setup._find_cua_binary", return_value=None):
        result = await service.status(SimpleNamespace(names=lambda: []))
    assert result["installed"] is False
    assert result["daemon_running"] is False
    assert result["mcp_enabled"] is False


async def test_unverified_remote_installer_is_always_refused(
    service: CuaDriverSetupService,
    monkeypatch,
):
    monkeypatch.setenv("ACE_STRICT_SECURITY", "0")
    monkeypatch.setenv("ACE_CUA_BINARY_SHA256_LINUX", "a" * 64)
    monkeypatch.delenv("ACE_CUA_INSTALL_SHA256_LINUX", raising=False)
    with patch("crew.tools.cua_setup._find_cua_binary", return_value=None):
        task = SimpleNamespace(add_log=MagicMock())
        with pytest.raises(RuntimeError, match="SHA-256"):
            await service._ensure_binary(task, "linux", False)


async def test_cua_command_refuses_missing_admin_launch_context(
    monkeypatch,
) -> None:
    spawn_calls: list[object] = []
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        lambda *_args, **_kwargs: spawn_calls.append(object()),
    )

    with pytest.raises(NativeRuntimeError, match="launch context missing"):
        await cua_setup._run_command(
            [sys.executable, "-c", "print('must not run')"],
            timeout=1,
        )

    assert spawn_calls == []


async def test_verified_installer_uses_shared_pinned_http_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = b"Write-Output safe"
    expected = hashlib.sha256(payload).hexdigest()
    seen: dict[str, object] = {}

    class PinnedClient:
        def fetch(self, url: str, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return OutboundHttpResponse(
                final_url=url,
                status=200,
                headers={},
                body=payload,
                content_type="text/plain",
                charset="utf-8",
            )

    monkeypatch.setattr(cua_setup, "_CUA_HTTP", PinnedClient(), raising=False)
    target = tmp_path / "install.ps1"

    _download_verified_installer(
        "https://cua.ai/driver/install.ps1",
        target,
        expected,
    )

    assert target.read_bytes() == payload
    assert seen["url"] == "https://cua.ai/driver/install.ps1"
    assert seen["kwargs"] == {
        "method": "GET",
        "headers": {"User-Agent": "Crew"},
        "timeout": 30.0,
        "max_bytes": 4 * 1024 * 1024,
        "max_redirects": 0,
    }


async def test_cua_binary_digest_is_verified_before_execution(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "cua-driver"
    binary.write_bytes(b"trusted binary")
    expected = hashlib.sha256(binary.read_bytes()).hexdigest()

    cua_setup._verify_cua_binary(str(binary), expected)
    binary.write_bytes(b"attacker replacement")
    with pytest.raises(RuntimeError, match="完整性"):
        cua_setup._verify_cua_binary(str(binary), expected)


async def test_status_when_installed_and_tools_present(
    service: CuaDriverSetupService,
    monkeypatch,
):
    registry = SimpleNamespace(names=lambda: ["cua-driver__list_windows", "terminal__run"])
    monkeypatch.setenv("ACE_CUA_BINARY_SHA256_LINUX", "a" * 64)

    async def fake_run(cmd, timeout, env=None):
        if "--version" in cmd:
            return "cua-driver 0.1.0"
        return "running"

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch("crew.tools.cua_setup._find_cua_binary", return_value="/bin/cua-driver"),
        patch("crew.tools.cua_setup._verify_cua_binary"),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
    ):
        result = await service.status(registry)

    assert result["installed"] is True
    assert result["version"] == "cua-driver 0.1.0"
    assert result["daemon_running"] is True
    assert result["mcp_enabled"] is True
    assert "cua-driver__list_windows" in result["tools_registered"]


async def test_status_verification_error_redacts_binary_path(
    service: CuaDriverSetupService,
    monkeypatch,
):
    registry = SimpleNamespace(names=lambda: [])
    monkeypatch.setenv("ACE_CUA_BINARY_SHA256_LINUX", "a" * 64)

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch(
            "crew.tools.cua_setup._find_cua_binary",
            return_value="/secret/home/user/cua-driver",
        ),
        patch(
            "crew.tools.cua_setup._verify_cua_binary",
            side_effect=ValueError(
                r"/secret/home/user/cua-driver: ACCESS_TOKEN=must-not-leak"
            ),
        ),
    ):
        result = await service.status(registry)

    assert result["verification_error"] == "驱动验证失败"
    payload = json.dumps(result, ensure_ascii=False)
    assert "must-not-leak" not in payload
    assert "/secret/home" not in result["verification_error"]


async def test_start_setup_detects_unsupported_platform(
    service: CuaDriverSetupService,
    mock_crew: Any,
    admin_launch,
):
    with patch("crew.tools.cua_setup._detect_platform", return_value="freebsd"):
        task = service.start_setup(crew=mock_crew, process_launch=admin_launch)
        # 等待后台任务结束
        await asyncio.sleep(0.1)
        assert task.status == "failed"
        assert "不支持的操作系统" in task.error


async def test_cua_task_status_redacts_host_paths_and_bounds_messages() -> None:
    task = SetupTask(task_id="task_1", platform="linux")
    task.add_log(r"installer failed at C:\Users\alice\secret-token.txt")
    task.update_step("install", "failed", r"/home/alice/private/config.yaml")
    task.finish("failed", r"open C:\Users\alice\secret-token.txt: permission denied")

    payload = task_to_dict(task)
    assert "alice" not in json.dumps(payload, ensure_ascii=False)
    assert "secret-token" not in json.dumps(payload, ensure_ascii=False)
    assert payload["error"] == "CUA Driver 安装失败"
    assert payload["steps"][0]["message"] == "CUA Driver 状态已隐藏"


async def test_full_setup_flow_linux(
    service: CuaDriverSetupService,
    mock_crew: Any,
    tmp_path: Path,
    admin_launch,
    monkeypatch,
):
    _write_config(Path(mock_crew.config.config_path))
    installer = b"#!/bin/sh\nexit 0\n"
    monkeypatch.setenv(
        "ACE_CUA_INSTALL_SHA256_LINUX",
        hashlib.sha256(installer).hexdigest(),
    )
    monkeypatch.setenv("ACE_CUA_BINARY_SHA256_LINUX", "b" * 64)

    async def fake_run(cmd, timeout, env=None):
        if "--version" in cmd:
            return "cua-driver 0.1.0"
        if "status" in cmd:
            return "running"
        return ""

    async def fake_stream(cmd, *, timeout, stdout_cb, stderr_cb, env=None):
        if stdout_cb:
            stdout_cb("installing...")
        if stderr_cb:
            stderr_cb("")

    registry_tools = ["cua-driver__list_windows"]
    mock_crew.registry.names = lambda: registry_tools

    def fake_download(_url, target, _checksum):
        target.write_bytes(installer)

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch("crew.tools.cua_setup._find_cua_binary", side_effect=[None, "/home/user/.local/bin/cua-driver"]),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
        patch("crew.tools.cua_setup._run_command_streaming", side_effect=fake_stream),
        patch("crew.tools.cua_setup._is_at_spi_installed", return_value=True),
        patch("crew.tools.cua_setup._download_verified_installer", side_effect=fake_download),
        patch("crew.tools.cua_setup._verify_cua_binary"),
    ):
        task = service.start_setup(crew=mock_crew, process_launch=admin_launch)
        # 等待最多 5 秒
        for _ in range(50):
            if task.status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.1)

    assert task.status == "success", f"task failed: {task.error}\nlog: {task.log}"
    mock_crew.config.set_mcp_server.assert_called_once()
    persisted_server = mock_crew.config.set_mcp_server.call_args.args[1]
    assert persisted_server["command"] == "/home/user/.local/bin/cua-driver"
    assert persisted_server["command_sha256"] == "b" * 64
    mock_crew.config.persist_mcp_servers.assert_called_once()
    mock_crew.reload_mcp_manager.assert_awaited_once()

    # 检查响应序列化
    payload = task_to_dict(task)
    assert payload["status"] == "success"
    assert any(s["name"] == "update_config" and s["status"] == "success" for s in payload["steps"])


async def test_reload_failure_rolls_back_persisted_cua_mcp_config(
    service: CuaDriverSetupService,
    mock_crew: Any,
    admin_launch,
    monkeypatch,
):
    monkeypatch.setenv("ACE_CUA_BINARY_SHA256_LINUX", "c" * 64)
    original = {"existing": {"command": "safe"}}
    mock_crew.config.mcp_servers = {
        name: dict(value) for name, value in original.items()
    }
    mock_crew.config.set_mcp_server = lambda name, value: (
        mock_crew.config.mcp_servers.__setitem__(name, value)
    )
    mock_crew.config.persist_mcp_servers = MagicMock()
    mock_crew.reload_mcp_manager = AsyncMock(
        side_effect=[RuntimeError("reload failed"), None]
    )

    async def fake_run(_cmd, timeout, env=None):
        del timeout, env
        return "cua-driver 0.1.0"

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch.object(
            service,
            "_ensure_binary",
            new_callable=AsyncMock,
            return_value="/trusted/cua-driver",
        ),
        patch.object(service, "_install_linux_deps", new_callable=AsyncMock),
        patch("crew.tools.cua_setup._verify_cua_binary"),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
    ):
        task = service.start_setup(
            crew=mock_crew,
            start_daemon=False,
            process_launch=admin_launch,
        )
        for _ in range(50):
            if task.status in {"success", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)

    assert task.status == "failed"
    assert mock_crew.config.mcp_servers == original
    assert mock_crew.config.persist_mcp_servers.call_count == 2
    assert mock_crew.reload_mcp_manager.await_count == 2


async def test_cancel_task(
    service: CuaDriverSetupService,
    mock_crew: Any,
    admin_launch,
):
    # 用一个不会立即结束的任务
    async def slow_install(*args, **kwargs):
        await asyncio.sleep(10)

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch.object(service, "_do_setup", side_effect=slow_install),
    ):
        task = service.start_setup(crew=mock_crew, process_launch=admin_launch)
        # 确保任务已经 running
        await asyncio.sleep(0.05)
        ok = await service.cancel_task(task.task_id)
        assert ok is True
        assert task.status == "cancelled"


async def test_concurrent_global_setup_is_rejected(
    service: CuaDriverSetupService,
    mock_crew: Any,
    admin_launch,
):
    async def slow_install(*_args, **_kwargs):
        await asyncio.sleep(10)

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch.object(service, "_do_setup", side_effect=slow_install),
    ):
        first = service.start_setup(
            crew=mock_crew,
            process_launch=admin_launch,
        )
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="正在运行"):
            service.start_setup(
                crew=mock_crew,
                process_launch=admin_launch,
            )
        assert await service.cancel_task(first.task_id) is True


async def test_task_not_found(service: CuaDriverSetupService):
    assert service.get_task("not-exist") is None
    ok = await service.cancel_task("not-exist")
    assert ok is False
