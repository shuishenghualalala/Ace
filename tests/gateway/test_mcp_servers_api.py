"""通用 MCP Server 管理 API（/api/mcp/servers）路由测试。

admin 鉴权 + CRUD 往返 + 持久化到临时 config.yaml + 单 server 重连 + 密钥脱敏。
用 tests/fixtures/echo_mcp_server.py 起真子进程验证端到端注册。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

pytest.importorskip("mcp")

from crew.app import build_app
from crew.channels.platform_registry import platform_registry
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
    # 显式批准 host stdio spawn（安全默认关闭，测试起真 echo 子进程需 opt-in）
    monkeypatch.setenv("ACE_ALLOW_HOST_MCP_STDIO", "1")
    from types import SimpleNamespace

    from crew.security.launch import current_process_launch
    from crew.security.models import PermissionProfile, PermissionProfileKind

    launch_token = current_process_launch.set(SimpleNamespace(
        managed=False,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
    ))
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
        current_process_launch.reset(launch_token)
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
    # 轮询 status 等后台 worker.start() 完成；Windows 上拉起子进程 + stdio 握手
    # 明显慢于 POSIX，窗口给到 10s 才能区分「慢」与「连不上」。
    import asyncio as _asyncio
    connected_srv = None
    for _ in range(100):
        await _asyncio.sleep(0.1)
        r = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
        for s in r.json()["servers"]:
            if s["name"] == "echo" and s["connected"]:
                connected_srv = s
                break
        if connected_srv:
            break
    assert connected_srv is not None, "echo server 未在 10s 内连上"
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

async def test_probe_remote_endpoint_without_persisting(api, monkeypatch):
    client, crew, _ = api
    import crew.gateway.routers.mcp_servers as mcp_router

    calls = []

    async def fake_probe(cfg):
        calls.append(cfg)
        return {"ok": True, "tools": ["ping"], "error": None}

    monkeypatch.setattr(mcp_router, "probe_remote_server", fake_probe)
    resp = await client.post(
        "/api/mcp/servers/probe",
        json={
            "url": "http://127.0.0.1:9/mcp",
            "transport": "http",
            "headers": {"X-Test": "1"},
        },
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "tools": ["ping"], "tool_count": 1, "elapsed_ms": 0, "error": None,
    }
    assert calls == [{
        "url": "http://127.0.0.1:9/mcp",
        "transport": "http",
        "headers": {"X-Test": "1"},
    }]
    assert crew.config.mcp_servers == {}

    rejected = await client.post(
        "/api/mcp/servers/probe", json={"command": "echo"}, headers=LOCAL_HEADERS,
    )
    assert rejected.status_code == 400
    assert "stdio" in rejected.json()["error"]


async def test_probe_merges_masked_secrets_from_saved_server(api, monkeypatch):
    client, _, _ = api
    import crew.gateway.routers.mcp_servers as mcp_router

    created = await client.post(
        "/api/mcp/servers",
        json={
            "name": "remote-secret",
            "url": "http://127.0.0.1:9/mcp?token=stored&mode=1",
            "transport": "http",
            "headers": {"Authorization": "Bearer stored", "X-Trace": "old"},
        },
        headers=LOCAL_HEADERS,
    )
    assert created.status_code == 201
    captured = {}

    async def fake_probe(cfg):
        captured.update(cfg)
        return {"ok": True, "tools": [], "tool_count": 0, "elapsed_ms": 1, "error": None}

    monkeypatch.setattr(mcp_router, "probe_remote_server", fake_probe)
    response = await client.post(
        "/api/mcp/servers/probe",
        json={
            "name": "remote-secret",
            "url": "http://127.0.0.1:9/mcp?token=***&mode=2",
            "transport": "http",
            "headers": {"Authorization": "***", "X-Trace": "new"},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 200
    assert captured["headers"] == {"Authorization": "Bearer stored", "X-Trace": "new"}
    assert captured["url"] == "http://127.0.0.1:9/mcp?token=stored&mode=2"


async def test_probe_limits_concurrent_probes(api, monkeypatch):
    client, _, _ = api
    import crew.gateway.routers.mcp_servers as mcp_router

    release = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def fake_probe(cfg):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        return {"ok": True, "tools": [], "error": None}

    monkeypatch.setattr(mcp_router, "probe_remote_server", fake_probe)
    payload = {"url": "http://127.0.0.1:9/mcp", "transport": "http"}
    tasks = [
        asyncio.create_task(client.post("/api/mcp/servers/probe", json=payload, headers=LOCAL_HEADERS))
        for _ in range(3)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(1.1)
    finally:
        release.set()
    responses = await asyncio.gather(*tasks)
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 200, 429]
    busy = next(response for response in responses if response.status_code == 429)
    assert busy.json()["code"] == "mcp_probe_busy"


async def test_invalid_http_urls_rejected_before_persist(api):
    client, crew, _ = api
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/mcp",
        "http://user:pass@example.com/mcp",
        "not-a-url",
    ):
        resp = await client.post(
            "/api/mcp/servers",
            json={"name": "bad-url", "url": url, "transport": "http"},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400, url
        assert "bad-url" not in (crew.config.mcp_servers or {})


async def test_secret_headers_and_url_query_redacted_in_get(api):
    client, _, config_yaml = api
    url = "http://127.0.0.1:1/mcp?token=abc123&mode=1"
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "name": "remote-secret",
            "url": url,
            "transport": "http",
            "headers": {"Authorization": "Bearer secret", "X-Trace": "visible"},
        },
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 201
    resp = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    srv = next(s for s in resp.json()["servers"] if s["name"] == "remote-secret")
    assert srv["config"]["headers"]["Authorization"] == "***"
    assert srv["config"]["headers"]["X-Trace"] == "visible"
    assert "token=***" in srv["config"]["url"]
    assert "mode=1" in srv["config"]["url"]
    persisted = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert persisted["mcp_servers"]["remote-secret"]["url"] == url
    await client.delete("/api/mcp/servers/remote-secret", headers=LOCAL_HEADERS)


async def test_update_masked_secret_preserves_original(api):
    client, crew, config_yaml = api
    created = await client.post(
        "/api/mcp/servers",
        json={
            "name": "secret-update",
            "command": sys.executable,
            "args": [_ECHO],
            "env": {"API_KEY": "sk-original", "VISIBLE": "old"},
        },
        headers=LOCAL_HEADERS,
    )
    assert created.status_code == 201
    updated = await client.put(
        "/api/mcp/servers/secret-update",
        json={
            "command": sys.executable,
            "args": [_ECHO],
            "env": {"API_KEY": "***", "VISIBLE": "new"},
        },
        headers=LOCAL_HEADERS,
    )
    assert updated.status_code == 200
    assert crew.config.mcp_servers["secret-update"]["env"] == {
        "API_KEY": "sk-original", "VISIBLE": "new",
    }
    persisted = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert persisted["mcp_servers"]["secret-update"]["env"]["API_KEY"] == "sk-original"


async def test_update_masked_url_query_preserves_secret(api):
    client, crew, config_yaml = api
    created = await client.post(
        "/api/mcp/servers",
        json={
            "name": "url-secret",
            "url": "http://127.0.0.1:9/mcp?token=stored&mode=1",
            "transport": "http",
        },
        headers=LOCAL_HEADERS,
    )
    assert created.status_code == 201
    updated = await client.put(
        "/api/mcp/servers/url-secret",
        json={
            "url": "http://127.0.0.1:9/mcp?token=***&mode=2",
            "transport": "http",
        },
        headers=LOCAL_HEADERS,
    )
    assert updated.status_code == 200
    assert crew.config.mcp_servers["url-secret"]["url"] == (
        "http://127.0.0.1:9/mcp?token=stored&mode=2"
    )
    persisted = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert persisted["mcp_servers"]["url-secret"]["url"].endswith("token=stored&mode=2")


async def test_list_caps_reported_tool_names(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    monkeypatch.setattr(crew.mcp_manager, "status", lambda: [{
        "name": "big",
        "transport": "http",
        "connected": True,
        "error": "",
        "tools": [f"tool-{index}" for index in range(60)],
        "config": {"url": "http://127.0.0.1:9/mcp", "transport": "http"},
    }])
    response = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    server = next(item for item in response.json()["servers"] if item["name"] == "big")
    assert len(server["tools"]) == 50
    assert server["tool_count"] == 60


async def test_list_tolerates_malformed_status_rows(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    monkeypatch.setattr(crew.mcp_manager, "status", lambda: [{
        "name": "broken",
        "transport": "http",
        "connected": False,
        "error": "",
        "tools": None,
        "config": "not-a-dict",
    }])
    response = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    server = next(item for item in response.json()["servers"] if item["name"] == "broken")
    assert server["tools"] == []
    assert server["tool_count"] == 0
    assert server["config"] == {"env": {}}


async def test_list_redacts_status_errors(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    monkeypatch.setattr(crew.mcp_manager, "status", lambda: [{
        "name": "leaky",
        "transport": "http",
        "connected": False,
        "error": "failed https://mcp.example.com/mcp?token=abc123 with sk-live-1234567890 and Authorization: Bearer header-secret",
        "tools": [],
        "config": {"url": "https://mcp.example.com/mcp", "transport": "http"},
    }])
    response = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    error = next(item for item in response.json()["servers"] if item["name"] == "leaky")["error"]
    assert "token=abc123" not in error
    assert "sk-live-1234567890" not in error
    assert "header-secret" not in error


async def test_create_manual_server_does_not_start_connection(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    started: list[str] = []

    async def fake_add(name, _config):
        started.append(name)
        return True

    monkeypatch.setattr(crew.mcp_manager, "add_server", fake_add)
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "manual-only",
            "transport": "stdio",
            "command": "definitely-not-a-real-binary",
            "auto_connect": False,
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 201
    assert started == []
    listed = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    server = next(item for item in listed.json()["servers"] if item["name"] == "manual-only")
    assert server["connected"] is False
    assert server["config"]["auto_connect"] is False


async def test_create_rejects_non_string_stdio_args(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={"name": "bad-args", "transport": "stdio", "command": "echo", "args": [{"x": 1}]},
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "args" in response.json()["error"]


async def test_create_rejects_header_newlines(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "bad-header",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Test": "ok\r\nInjected: yes"},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "换行" in response.json()["error"]


async def test_create_rejects_invalid_header_name(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "bad-header-name",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Bad Header": "value"},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "HTTP token" in response.json()["error"]


async def test_create_rejects_too_many_headers(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "many-headers",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {f"X-{index}": "value" for index in range(129)},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "数量" in response.json()["error"]


async def test_create_rejects_long_header_value(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "long-header",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Long": "a" * 8193},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "8192" in response.json()["error"]


async def test_update_to_manual_stops_the_worker(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    crew.config.add_mcp_server("swap", {"command": "echo", "transport": "stdio"})
    removed: list[str] = []
    registered: list[str] = []
    reloaded: list[str] = []

    async def fake_remove(name):
        removed.append(name)
        return True

    def fake_register(name, _cfg):
        registered.append(name)

    async def fake_reload(name, _cfg):
        reloaded.append(name)
        return True

    monkeypatch.setattr(crew.mcp_manager, "remove_server", fake_remove)
    monkeypatch.setattr(crew.mcp_manager, "register_pending", fake_register)
    monkeypatch.setattr(crew.mcp_manager, "reload_one", fake_reload)
    response = await client.put(
        "/api/mcp/servers/swap",
        json={"transport": "stdio", "command": "echo", "auto_connect": False},
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 200
    assert removed == ["swap"]
    assert registered == ["swap"]
    assert reloaded == []


async def test_create_rejects_invalid_env_key(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "bad-env",
            "transport": "stdio",
            "command": "echo",
            "env": {"BAD=KEY": "value"},
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "env" in response.json()["error"]


async def test_create_rejects_too_long_stdio_arg(api):
    client, _crew, _ = api
    response = await client.post(
        "/api/mcp/servers",
        json={
            "name": "long-arg",
            "transport": "stdio",
            "command": "echo",
            "args": ["a" * 8193],
        },
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 400
    assert "8192" in response.json()["error"]


async def test_probe_rejects_non_admin(api, monkeypatch):
    from crew.gateway.auth import AuthenticationError
    import crew.gateway.routers.mcp_servers as router

    def deny(*_args, **_kwargs):
        raise AuthenticationError("需要管理员权限")

    monkeypatch.setattr(router, "require_admin", deny)
    client, _crew, _ = api
    listed = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    assert listed.status_code == 403
    response = await client.post(
        "/api/mcp/servers/probe",
        json={"transport": "http", "url": "https://example.com/mcp"},
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 403
    assert response.json()["ok"] is False


async def test_list_marks_config_pending_while_worker_reconnects(api, monkeypatch):
    client, crew, _ = api
    assert crew.mcp_manager is not None
    crew.config.mcp_servers = {
        "pending": {"url": "http://127.0.0.1:9/new", "transport": "http"},
    }
    monkeypatch.setattr(crew.mcp_manager, "status", lambda: [{
        "name": "pending",
        "transport": "http",
        "connected": True,
        "error": "",
        "tools": [],
        "config": {"url": "http://127.0.0.1:9/old", "transport": "http"},
    }])
    response = await client.get("/api/mcp/servers", headers=LOCAL_HEADERS)
    server = next(item for item in response.json()["servers"] if item["name"] == "pending")
    assert server["config_pending"] is True
    assert server["config"]["url"] == "http://127.0.0.1:9/new"


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


# ---- 配置事务（S2）：持久化失败不得污染内存/磁盘/运行资源 ----
#
# 从真实 HTTP 入口注入磁盘写入失败（yaml.safe_dump / Path.replace），
# 核对业务字段 cfg.mcp_servers、raw_config、磁盘回读与运行管理器调用次数。


class _SpyMcpManager:
    """记录运行资源调用的假 manager：保存失败路径必须零调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self, _registry) -> None:
        self.calls.append("start")

    def register_pending(self, name, _cfg) -> None:
        self.calls.append(f"register_pending:{name}")

    async def add_server(self, name, _cfg) -> bool:
        self.calls.append(f"add_server:{name}")
        return True

    async def reload_one(self, name, _cfg=None) -> bool:
        self.calls.append(f"reload_one:{name}")
        return True

    async def remove_server(self, name) -> bool:
        self.calls.append(f"remove_server:{name}")
        return True

    def status(self) -> list[dict]:
        return []


def _break_safe_dump(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("injected safe_dump failure")

    monkeypatch.setattr(yaml, "safe_dump", _boom)


def _break_replace(monkeypatch, config_yaml: Path) -> None:
    original = Path.replace

    def _replace(self, target):
        if Path(target) == config_yaml:
            raise OSError("injected replace failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", _replace)


def _seed_server(crew, name: str, command: str) -> None:
    """不经 HTTP 直接种一个 server 到内存+磁盘（走遗留 set+persist 入口）。"""
    crew.config.set_mcp_server(name, {"command": command})
    crew.config.persist_mcp_servers()


def _assert_server_on_disk(config_yaml: Path, name: str, command: str | None) -> dict:
    data = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    servers = data.get("mcp_servers") or {}
    if command is None:
        assert name not in servers
    else:
        assert servers[name] == {"command": command}
    return data


async def test_create_persist_failure_keeps_memory_disk_and_runtime_untouched(api, monkeypatch):
    client, crew, config_yaml = api
    _seed_server(crew, "keep", "keep-cmd")
    spy = _SpyMcpManager()
    monkeypatch.setattr(crew, "mcp_manager", spy)
    _break_safe_dump(monkeypatch)

    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "fresh", "command": "new-cmd"},
        headers=LOCAL_HEADERS,
    )

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    # 业务字段：内存保持旧值
    assert "fresh" not in crew.config.mcp_servers
    assert crew.config.mcp_servers["keep"]["command"] == "keep-cmd"
    # 磁盘与 raw_config 保持旧值
    _assert_server_on_disk(config_yaml, "fresh", None)
    assert crew.config.mcp_servers["keep"]["command"] == "keep-cmd"
    assert crew.config.raw_config["mcp_servers"] == {"keep": {"command": "keep-cmd"}}
    # 运行管理器零调用
    assert spy.calls == []
    assert not config_yaml.with_suffix(config_yaml.suffix + ".tmp").exists()


@pytest.mark.parametrize("inject", ["safe_dump", "replace"])
async def test_update_persist_failure_keeps_memory_disk_and_runtime_untouched(
    api, monkeypatch, inject
):
    client, crew, config_yaml = api
    _seed_server(crew, "echo", "old-cmd")
    spy = _SpyMcpManager()
    monkeypatch.setattr(crew, "mcp_manager", spy)
    if inject == "safe_dump":
        _break_safe_dump(monkeypatch)
    else:
        _break_replace(monkeypatch, config_yaml)

    resp = await client.put(
        "/api/mcp/servers/echo",
        json={"command": "new-cmd"},
        headers=LOCAL_HEADERS,
    )

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert crew.config.mcp_servers["echo"]["command"] == "old-cmd"
    _assert_server_on_disk(config_yaml, "echo", "old-cmd")
    assert crew.config.raw_config["mcp_servers"] == {"echo": {"command": "old-cmd"}}
    assert spy.calls == []
    assert not config_yaml.with_suffix(config_yaml.suffix + ".tmp").exists()


async def test_delete_persist_failure_keeps_memory_disk_and_runtime_untouched(api, monkeypatch):
    client, crew, config_yaml = api
    _seed_server(crew, "echo", "old-cmd")
    spy = _SpyMcpManager()
    monkeypatch.setattr(crew, "mcp_manager", spy)
    _break_safe_dump(monkeypatch)

    resp = await client.delete("/api/mcp/servers/echo", headers=LOCAL_HEADERS)

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert crew.config.mcp_servers["echo"]["command"] == "old-cmd"
    _assert_server_on_disk(config_yaml, "echo", "old-cmd")
    assert crew.config.raw_config["mcp_servers"] == {"echo": {"command": "old-cmd"}}
    assert spy.calls == []
    assert not config_yaml.with_suffix(config_yaml.suffix + ".tmp").exists()


async def test_update_failure_then_retry_applies_change_without_losing_updates(api, monkeypatch):
    client, crew, config_yaml = api
    _seed_server(crew, "echo", "v1")
    _seed_server(crew, "other", "stable")
    spy = _SpyMcpManager()
    monkeypatch.setattr(crew, "mcp_manager", spy)

    real_safe_dump = yaml.safe_dump
    state = {"calls": 0}

    def flaky_safe_dump(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("injected safe_dump failure")
        return real_safe_dump(*args, **kwargs)

    monkeypatch.setattr(yaml, "safe_dump", flaky_safe_dump)

    first = await client.put(
        "/api/mcp/servers/echo", json={"command": "v2"}, headers=LOCAL_HEADERS
    )
    assert first.status_code == 500
    assert crew.config.mcp_servers["echo"]["command"] == "v1"
    assert crew.config.mcp_servers["other"]["command"] == "stable"
    assert spy.calls == []

    second = await client.put(
        "/api/mcp/servers/echo", json={"command": "v2"}, headers=LOCAL_HEADERS
    )
    assert second.status_code == 200
    assert crew.config.mcp_servers["echo"]["command"] == "v2"
    assert crew.config.mcp_servers["other"]["command"] == "stable"
    # stdio payload 经 _validate_server_payload 规范化会带 args=[]，只核对 command
    disk = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    assert disk["mcp_servers"]["echo"]["command"] == "v2"
    assert disk["mcp_servers"]["other"] == {"command": "stable"}
    # 重试成功后才允许运行资源操作（后台 reload；让出事件循环使其落地）
    import asyncio as _asyncio

    for _ in range(5):
        await _asyncio.sleep(0)
    assert "reload_one:echo" in spy.calls


async def test_delete_saved_but_runtime_remove_failure_reports_honestly(api, monkeypatch):
    """持久化成功 + 运行资源操作失败：磁盘与内存保持新值，错误如实上报。"""
    client, crew, config_yaml = api
    _seed_server(crew, "echo", "cmd")
    spy = _SpyMcpManager()

    async def _boom(name):
        spy.calls.append(f"remove_server:{name}")
        raise RuntimeError("worker stop failed")

    spy.remove_server = _boom
    monkeypatch.setattr(crew, "mcp_manager", spy)

    resp = await client.delete("/api/mcp/servers/echo", headers=LOCAL_HEADERS)

    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert "配置已保存" in body["error"]
    # _ensure_mgr_started 先触发一次 start，随后才移除运行实例
    assert spy.calls == ["start", "remove_server:echo"]
    # 配置（磁盘+内存）保持"已删除"的新值，不得回滚伪装成保存失败
    assert "echo" not in crew.config.mcp_servers
    _assert_server_on_disk(config_yaml, "echo", None)
    assert "echo" not in crew.config.raw_config.get("mcp_servers", {})
