"""通用 MCP Server 管理 API（/api/mcp/servers）路由测试。

admin 鉴权 + CRUD 往返 + 持久化到临时 config.yaml + 单 server 重连 + 密钥脱敏。
用 tests/fixtures/echo_mcp_server.py 起真子进程验证端到端注册。
"""

from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

pytest.importorskip("mcp")

from crew.app import build_app
from crew.gateway.platform_registry import platform_registry
from crew.gateway.server import create_app
from crew.state.config import Config

_ECHO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "echo_mcp_server.py")
)

LOCAL_HEADERS: dict[str, str] = {}


def _restore_platform_entries(entries):
    platform_registry._entries.clear()
    for entry in entries:
        platform_registry.register(entry)


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """admin-only gateway，空 mcp_servers，config_path 指向临时 yaml。"""
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("llm:\n  active: default\nmcp_servers: {}\n", encoding="utf-8")
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        gateway_admin_accounts=["A:uid-a"],
        plugins_enabled=[],
        config_path=str(config_yaml),
    )
    cfg.mcp_servers = {}
    try:
        crew = build_app(config=cfg, enable_team=False)
        platform_registry._entries.clear()
        app = create_app(crew)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, crew, config_yaml
    finally:
        # 关掉可能起的 MCP worker，避免子进程泄漏
        try:
            if crew.mcp_manager is not None:
                await crew.mcp_manager.aclose()
        except Exception:
            pass
        _restore_platform_entries(old_entries)


def _echo_payload(name: str = "echo") -> dict:
    return {"name": name, "command": sys.executable, "args": [_ECHO]}


# ---- 本地访问 ----

async def test_list_servers_needs_no_identity_headers(api):
    client, _, _ = api
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 200


async def test_list_servers_local_owner_ok(api):
    client, _, _ = api
    resp = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_list_servers_admin_ok_empty(api):
    client, _, _ = api
    resp = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["servers"] == []


# ---- CRUD 往返 + 持久化 ----

async def test_create_server_registers_tools_and_persists(api):
    client, crew, config_yaml = api
    resp = await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is True
    srv = body["servers"][0]
    assert srv["name"] == "echo"
    # create 现为 fire-and-forget（后台连接），响应时可能尚未 connected。
    # 轮询 status 等后台 worker.start() 完成（echo server 连接快，2s 内）。
    import asyncio as _asyncio
    connected_srv = None
    for _ in range(20):
        await _asyncio.sleep(0.1)
        r = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
        for s in r.json()["servers"]:
            if s["name"] == "echo" and s["connected"]:
                connected_srv = s
                break
        if connected_srv:
            break
    assert connected_srv is not None, "echo server 未在 2s 内连上"
    assert "echo" in connected_srv["tools"]

    # 内存配置已更新
    assert "echo" in crew.config.mcp_servers
    # 持久化到 yaml
    import yaml as _yaml
    data = _yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert "echo" in data["mcp_servers"]


async def test_create_duplicate_returns_409(api):
    client, _, _ = api
    await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    resp = await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    assert resp.status_code == 409


async def test_create_invalid_name_rejected(api):
    client, _, _ = api
    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "bad name!", "command": "echo"},
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 400


async def test_create_missing_command_and_url_rejected(api):
    client, _, _ = api
    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "noop"},
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 400


async def test_update_server_reloads(api):
    client, _, _ = api
    await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    # 编辑：换个 args 仍指向 echo
    resp = await client.put(
        "/api/mcp/servers/echo",
        json={"command": sys.executable, "args": [_ECHO]},
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_update_nonexistent_returns_404(api):
    client, _, _ = api
    resp = await client.put(
        "/api/mcp/servers/nope",
        json={"command": "echo"},
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 404


async def test_delete_server_removes_and_persists(api):
    client, crew, config_yaml = api
    await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    resp = await client.delete("/api/mcp/servers/echo", headers=LOCAL_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["servers"] == []
    assert "echo" not in crew.config.mcp_servers
    import yaml as _yaml
    data = _yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert "echo" not in data.get("mcp_servers", {})


async def test_delete_nonexistent_returns_404(api):
    client, _, _ = api
    resp = await client.delete("/api/mcp/servers/nope", headers=LOCAL_HEADERS)
    assert resp.status_code == 404


# ---- 单 server 重连 ----

async def test_reload_server(api):
    client, _, _ = api
    await client.post("/api/mcp/servers", json=_echo_payload(), headers=LOCAL_HEADERS)
    resp = await client.post("/api/mcp/servers/echo/reload", headers=LOCAL_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_reload_nonexistent_returns_404(api):
    client, _, _ = api
    resp = await client.post("/api/mcp/servers/nope/reload", headers=LOCAL_HEADERS)
    assert resp.status_code == 404


# ---- 密钥脱敏 ----

async def test_secret_env_redacted_in_get(api):
    client, _, _ = api
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "name": "secret",
            "command": sys.executable,
            "args": [_ECHO],
            "env": {"API_KEY": "sk-supersecret", "PATH_EXTRA": "/usr/bin"},
        },
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 201
    # GET 返回脱敏
    resp = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    srv = next(s for s in resp.json()["servers"] if s["name"] == "secret")
    assert srv["config"]["env"]["API_KEY"] == "***"
    assert srv["config"]["env"]["PATH_EXTRA"] == "/usr/bin"
