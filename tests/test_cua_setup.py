"""CUA Driver MCP 一键安装服务测试。

全部使用 mock 避免真实下载/安装。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crew.tools.cua_setup import CuaDriverSetupService, task_to_dict

pytestmark = pytest.mark.asyncio


@pytest.fixture
def service() -> CuaDriverSetupService:
    return CuaDriverSetupService()


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


def _write_config(path: Path) -> None:
    path.write_text(
        "llm:\n  active: default\nmcp_servers:\n  sample-server:\n    command: echo\n",
        encoding="utf-8",
    )


async def test_status_when_not_installed(service: CuaDriverSetupService):
    with patch("crew.tools.cua_setup._find_cua_binary", return_value=None):
        result = await service.status(SimpleNamespace(names=lambda: []))
    assert result["installed"] is False
    assert result["daemon_running"] is False
    assert result["mcp_enabled"] is False


async def test_status_when_installed_and_tools_present(service: CuaDriverSetupService):
    registry = SimpleNamespace(names=lambda: ["cua-driver__list_windows", "terminal__run"])

    async def fake_run(cmd, timeout, env=None):
        if "--version" in cmd:
            return "cua-driver 0.1.0"
        return "running"

    with (
        patch("crew.tools.cua_setup._find_cua_binary", return_value="/bin/cua-driver"),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
    ):
        result = await service.status(registry)

    assert result["installed"] is True
    assert result["version"] == "cua-driver 0.1.0"
    assert result["daemon_running"] is True
    assert result["mcp_enabled"] is True
    assert "cua-driver__list_windows" in result["tools_registered"]


async def test_start_setup_detects_unsupported_platform(
    service: CuaDriverSetupService, mock_crew: Any
):
    with patch("crew.tools.cua_setup._detect_platform", return_value="freebsd"):
        task = service.start_setup(crew=mock_crew)
        # 等待后台任务结束
        await asyncio.sleep(0.1)
        assert task.status == "failed"
        assert "不支持的操作系统" in task.error


async def test_full_setup_flow_linux(
    service: CuaDriverSetupService, mock_crew: Any, tmp_path: Path
):
    _write_config(Path(mock_crew.config.config_path))

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

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch(
            "crew.tools.cua_setup._find_cua_binary",
            side_effect=[None, "/home/user/.local/bin/cua-driver"],
        ),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
        patch("crew.tools.cua_setup._run_command_streaming", side_effect=fake_stream),
        patch("crew.tools.cua_setup._is_at_spi_installed", return_value=True),
    ):
        task = service.start_setup(crew=mock_crew)
        # 等待最多 5 秒
        for _ in range(50):
            if task.status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.1)

    assert task.status == "success", f"task failed: {task.error}\nlog: {task.log}"
    mock_crew.config.set_mcp_server.assert_called_once()
    mock_crew.config.persist_mcp_servers.assert_called_once()
    mock_crew.reload_mcp_manager.assert_awaited_once()

    # 检查响应序列化
    payload = task_to_dict(task)
    assert payload["status"] == "success"
    assert any(s["name"] == "update_config" and s["status"] == "success" for s in payload["steps"])


async def test_full_setup_flow_macos(
    service: CuaDriverSetupService, mock_crew: Any, tmp_path: Path
):
    _write_config(Path(mock_crew.config.config_path))
    streamed_commands: list[list[str]] = []
    command_calls: list[list[str]] = []
    status_calls = 0

    async def fake_run(cmd, timeout, env=None):
        nonlocal status_calls
        command_calls.append(cmd)
        if "--version" in cmd:
            return "cua-driver 0.14.1"
        if "status" in cmd:
            status_calls += 1
            return "stopped" if status_calls == 1 else "running"
        return ""

    async def fake_stream(cmd, *, timeout, stdout_cb, stderr_cb, env=None):
        streamed_commands.append(cmd)
        if stdout_cb:
            stdout_cb("installing macOS app...")

    binary = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"
    registry_tools = ["cua-driver__list_windows"]
    mock_crew.registry.names = lambda: registry_tools

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="macos"),
        patch("crew.tools.cua_setup._find_cua_binary", side_effect=[None, binary]),
        patch(
            "crew.tools.cua_setup._find_cua_app",
            side_effect=[None, "/Applications/CuaDriver.app", "/Applications/CuaDriver.app"],
        ),
        patch("crew.tools.cua_setup._run_command", side_effect=fake_run),
        patch("crew.tools.cua_setup._run_command_streaming", side_effect=fake_stream),
    ):
        task = service.start_setup(crew=mock_crew)
        for _ in range(50):
            if task.status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.1)

    assert task.status == "success", f"task failed: {task.error}\nlog: {task.log}"
    assert streamed_commands == [
        [
            "/bin/bash",
            "-c",
            "curl -fsSL https://cua.ai/driver/install.sh | /bin/bash",
        ]
    ]
    assert ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"] in command_calls
    mock_crew.config.set_mcp_server.assert_called_once_with(
        "cua-driver",
        {"command": binary, "args": ["mcp"], "env": {}},
    )
    assert any("辅助功能权限" in line for line in task.log)


async def test_cancel_task(service: CuaDriverSetupService, mock_crew: Any):
    # 用一个不会立即结束的任务
    async def slow_install(*args, **kwargs):
        await asyncio.sleep(10)

    with (
        patch("crew.tools.cua_setup._detect_platform", return_value="linux"),
        patch.object(service, "_do_setup", side_effect=slow_install),
    ):
        task = service.start_setup(crew=mock_crew)
        # 确保任务已经 running
        await asyncio.sleep(0.05)
        ok = await service.cancel_task(task.task_id)
        assert ok is True
        assert task.status == "cancelled"


async def test_task_not_found(service: CuaDriverSetupService):
    assert service.get_task("not-exist") is None
    ok = await service.cancel_task("not-exist")
    assert ok is False
