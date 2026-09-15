"""通用 MCP Server 管理 API（/api/mcp/servers）路由测试。

admin 鉴权 + CRUD 往返 + 持久化到临时 config.yaml + 单 server 重连 + 密钥脱敏。
用 tests/fixtures/echo_mcp_server.py 起真子进程验证端到端注册。
"""

from __future__ import annotations

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
    crew.mcp_manager = spy
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
    crew.mcp_manager = spy
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
    crew.mcp_manager = spy
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
    crew.mcp_manager = spy

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
    crew.mcp_manager = spy

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
