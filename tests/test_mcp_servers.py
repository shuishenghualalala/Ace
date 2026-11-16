"""MCPClientManager 运行时管理能力测试：status / add_server / remove_server / reload_one。

用 tests/fixtures/echo_mcp_server.py 起真 stdio 子进程，验证增量增删与单 server 重连
不影响其他 server。
"""

import os
import sys

import pytest

pytest.importorskip("mcp")

from crew.core.types import ToolCall
from crew.tools.mcp_client import MCPClientManager
from crew.tools.registry import Registry

_ECHO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "fixtures", "echo_mcp_server.py")
)


@pytest.fixture(autouse=True)
def _allow_host_stdio(monkeypatch):
    """显式批准 host stdio spawn：安全默认关闭后，起真子进程的测试需显式 opt-in。

    与 tests/test_mcp.py 的 stdio 用例同一 idioms：env 开关 + 非 managed launch。
    """
    from types import SimpleNamespace

    from crew.security.launch import current_process_launch
    from crew.security.models import PermissionProfile, PermissionProfileKind

    monkeypatch.setenv("ACE_ALLOW_HOST_MCP_STDIO", "1")
    token = current_process_launch.set(SimpleNamespace(
        managed=False,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
    ))
    try:
        yield
    finally:
        current_process_launch.reset(token)


def _echo_cfg(name: str = "echo") -> dict:
    return {"command": sys.executable, "args": [_ECHO]}


# ---- status：未启动时反映配置 ----

async def test_status_lists_configured_servers_before_start():
    mgr = MCPClientManager({"echo": _echo_cfg()})
    try:
        rows = mgr.status()
        assert len(rows) == 1
        assert rows[0]["name"] == "echo"
        assert rows[0]["connected"] is False
        assert rows[0]["transport"] == "stdio"
        assert rows[0]["tools"] == []
    finally:
        await mgr.aclose()


async def test_status_reflects_connected_worker_and_tools():
    reg = Registry()
    mgr = MCPClientManager({"echo": _echo_cfg()})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        rows = mgr.status()
        assert rows[0]["connected"] is True
        assert "echo" in rows[0]["tools"]
    finally:
        await mgr.aclose()


# ---- add_server：增量新增不打断已有 worker ----

async def test_add_server_connects_and_registers_tools():
    reg = Registry()
    mgr = MCPClientManager({})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        ok = await mgr.add_server("echo", _echo_cfg())
        assert ok is True
        assert "echo__echo" in reg.names()
        rows = mgr.status()
        assert any(r["name"] == "echo" and r["connected"] for r in rows)
        # 调用一次确认工具真的可用
        res = await reg.execute(ToolCall("1", "echo__echo", {"text": "hi"}))
        assert "echo: hi" in res.content
    finally:
        await mgr.aclose()


async def test_add_server_does_not_disturb_existing_worker():
    reg = Registry()
    mgr = MCPClientManager({"a": _echo_cfg("a")})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        ok = await mgr.add_server("b", _echo_cfg("b"))
        assert ok is True
        # a 仍然连接且可调用（未被断开）
        res = await reg.execute(ToolCall("1", "a__echo", {"text": "x"}))
        assert "echo: x" in res.content
        assert "b__echo" in reg.names()
    finally:
        await mgr.aclose()


async def test_add_server_duplicate_returns_false():
    reg = Registry()
    mgr = MCPClientManager({"echo": _echo_cfg()})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        ok = await mgr.add_server("echo", _echo_cfg())
        assert ok is False
    finally:
        await mgr.aclose()


# ---- remove_server：注销工具 + 不影响其他 ----

async def test_remove_server_unregisters_tools_and_keeps_others():
    reg = Registry()
    mgr = MCPClientManager({"a": _echo_cfg("a"), "b": _echo_cfg("b")})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        ok = await mgr.remove_server("a")
        assert ok is True
        assert "a__echo" not in reg.names()
        assert "b__echo" in reg.names()
        # b 仍可调用
        res = await reg.execute(ToolCall("1", "b__echo", {"text": "y"}))
        assert "echo: y" in res.content
    finally:
        await mgr.aclose()


async def test_remove_nonexistent_returns_false():
    reg = Registry()
    mgr = MCPClientManager({})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        assert await mgr.remove_server("nope") is False
    finally:
        await mgr.aclose()


# ---- reload_one：单 server 重连，其他不动 ----

async def test_reload_one_reconnects_single_server():
    reg = Registry()
    mgr = MCPClientManager({"a": _echo_cfg("a"), "b": _echo_cfg("b")})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        ok = await mgr.reload_one("a")
        assert ok is True
        assert "a__echo" in reg.names()
        assert "b__echo" in reg.names()
        # 重连后 a 仍可调用
        res = await reg.execute(ToolCall("1", "a__echo", {"text": "z"}))
        assert "echo: z" in res.content
    finally:
        await mgr.aclose()


async def test_reload_one_nonexistent_returns_false():
    reg = Registry()
    mgr = MCPClientManager({})
    await mgr.start(reg)
    await mgr.await_started()
    try:
        assert await mgr.reload_one("nope") is False
    finally:
        await mgr.aclose()
