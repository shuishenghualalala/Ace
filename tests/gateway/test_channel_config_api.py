"""渠道配置 CRUD 与单渠道热重连 API 测试。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from crew.app import build_app
from crew.core.interfaces import Channel, MessageHandler
from crew.gateway import channel_config
from crew.gateway.platform_registry import PlatformConfig, PlatformEntry, platform_registry
from crew.gateway.routers.channels import _platform_error_kind
from crew.gateway.server import create_app
from crew.state.config import load_config, owner_overlay_config_path, resolve_writable_env_path, write_env_key

OWNER_A = "A:uid-a"


def test_weixin_network_error_is_classified_for_frontend():
    error = (
        "Cannot connect to host ilinkai.weixin.qq.com:443 ssl:default "
        "[nodename nor servname provided, or not known]"
    )
    assert _platform_error_kind("weixin", error) == "network"
    assert _platform_error_kind("weixin", "微信会话已过期") == ""
    assert _platform_error_kind("feishu", error) == ""


def _owner_overlay_path(_tmp_path: Path, owner_account_id: str = OWNER_A) -> Path:
    return owner_overlay_config_path(owner_account_id)


class DummyChannel(Channel):
    name = "testchat"
    starts: int = 0
    stops: int = 0
    stop_delay: float = 0.0
    started_configs: list[dict[str, Any]] = []

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    async def start(self, handler: MessageHandler) -> None:
        DummyChannel.starts += 1
        DummyChannel.started_configs.append(dict(self.config.extra))
        if self.config.extra.get("failStart"):
            raise RuntimeError(f"dummy failed with {self.config.extra.get('apiKey', '')}")

    async def stop(self) -> None:
        if DummyChannel.stop_delay:
            import asyncio

            await asyncio.sleep(DummyChannel.stop_delay)
        DummyChannel.stops += 1

    def status_detail(self) -> dict[str, Any]:
        return {
            "connect_url": self.config.extra.get("serverUrl", ""),
            "apiKey": self.config.extra.get("apiKey", ""),
            "last_error": self.config.extra.get("lastError", ""),
        }


class DummySecureChatChannel(Channel):
    name = "securechat"
    starts: int = 0

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    async def start(self, handler: MessageHandler) -> None:
        DummySecureChatChannel.starts += 1

    async def stop(self) -> None:
        pass


class DummyFeishuChannel(Channel):
    name = "feishu"
    starts: int = 0
    started_configs: list[dict[str, Any]] = []

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    async def start(self, handler: MessageHandler) -> None:
        DummyFeishuChannel.starts += 1
        DummyFeishuChannel.started_configs.append(dict(self.config.extra))

    async def stop(self) -> None:
        pass


def _dummy_securechat_entry() -> PlatformEntry:
    return PlatformEntry(
        name="securechat",
        label="SECURECHAT",
        adapter_factory=lambda config: DummySecureChatChannel(config),
        validate_config=lambda cfg: bool(
            cfg.extra.get("wsUrl") and cfg.extra.get("clientId") and cfg.extra.get("secretKey")
        ),
        is_connected=lambda cfg: bool(
            cfg.extra.get("wsUrl") and cfg.extra.get("clientId") and cfg.extra.get("secretKey")
        ),
    )


def _dummy_feishu_entry() -> PlatformEntry:
    return PlatformEntry(
        name="feishu",
        label="Feishu",
        adapter_factory=lambda config: DummyFeishuChannel(config),
        validate_config=lambda cfg: bool(cfg.extra.get("appId") and cfg.extra.get("appSecret")),
        is_connected=lambda cfg: bool(cfg.extra.get("appId") and cfg.extra.get("appSecret")),
    )


def _dummy_testchat_entry() -> PlatformEntry:
    return PlatformEntry(
        name="testchat",
        label="TESTCHAT",
        adapter_factory=lambda config: DummyChannel(config),
        validate_config=lambda cfg: bool(cfg.extra.get("serverUrl") and cfg.extra.get("apiKey")),
        is_connected=lambda cfg: bool(cfg.extra.get("serverUrl") and cfg.extra.get("apiKey")),
        optional_env=[{"name": "TESTCHAT_API_KEY", "secret": True}],
        env_enablement_fn=lambda: {"apiKey": os.getenv("TESTCHAT_API_KEY", "")} if os.getenv("TESTCHAT_API_KEY") else {},
    )


@pytest.fixture
def channel_api(tmp_path: Path):
    old_entries = list(platform_registry.all_entries())
    channel_config.PLATFORM_SECRET_ENV["testchat"] = {
        "apiKey": "TESTCHAT_API_KEY",
        "api_key": "TESTCHAT_API_KEY",
    }
    channel_config.PLATFORM_ACCOUNT_FIELDS["testchat"] = {
        "clientId",
        "client_id",
        "apiKey",
        "api_key",
    }
    channel_config.PLATFORM_ENV_FIELDS["testchat"] = {
        "serverUrl": "TESTCHAT_SERVER_URL",
        "server_url": "TESTCHAT_SERVER_URL",
    }
    channel_config.PLATFORM_SECRET_ENV["securechat"] = {
        "secretKey": "SECURECHAT_SECRET_KEY",
        "secret_key": "SECURECHAT_SECRET_KEY",
    }
    channel_config.PLATFORM_ACCOUNT_FIELDS["securechat"] = {
        "clientId",
        "client_id",
        "secretKey",
        "secret_key",
    }
    channel_config.PLATFORM_ENV_FIELDS["securechat"] = {
        "wsUrl": "SECURECHAT_WS_URL",
        "ws_url": "SECURECHAT_WS_URL",
        "clientId": "SECURECHAT_CLIENT_ID",
        "client_id": "SECURECHAT_CLIENT_ID",
    }
    DummyChannel.starts = 0
    DummyChannel.stops = 0
    DummyChannel.stop_delay = 0.0
    DummyChannel.started_configs = []
    DummyFeishuChannel.starts = 0
    DummyFeishuChannel.started_configs = []

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "db_path": str(tmp_path / "crew.db"),
                    "log_level": "WARNING",
                    "llm_trace": False,
                },
                "gateway": {"host": "127.0.0.1", "port": 8000, "admin_accounts": ["A:uid-a"]},
                "channels": {"testchat": {"enabled": False}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    os.environ["CREW_HOME"] = str(tmp_path / ".crew")
    os.environ.pop("TESTCHAT_API_KEY", None)
    cfg = load_config(config_path=str(config_yaml))
    crew = build_app(config=cfg, enable_team=False)
    platform_registry._entries.clear()
    platform_registry.register(_dummy_testchat_entry())
    app = create_app(crew)
    app.state.crew = crew
    yield app, config_yaml

    platform_registry._entries.clear()
    for entry in old_entries:
        platform_registry.register(entry)
    for mapping in (
        channel_config.PLATFORM_SECRET_ENV,
        channel_config.PLATFORM_ACCOUNT_FIELDS,
        channel_config.PLATFORM_ENV_FIELDS,
    ):
        mapping.pop("testchat", None)
        mapping.pop("securechat", None)
    for key in (
        "CREW_HOME",
        "TESTCHAT_API_KEY",
        "TESTCHAT_SERVER_URL",
        "SECURECHAT_SECRET_KEY",
        "SECURECHAT_WS_URL",
        "SECURECHAT_CLIENT_ID",
    ):
        os.environ.pop(key, None)


@pytest.mark.asyncio
async def test_save_platform_config_persists_without_secret(channel_api, auth_headers):
    app, config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws", "apiKey": "sk-secret"},
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["config"]["serverUrl"] == "wss://dummy.example/ws"
    assert "apiKey" not in body["config"]
    assert "sk-secret" not in resp.text
    env_text = (_owner_overlay_path(config_yaml.parent).parent / ".env").read_text(encoding="utf-8")
    assert "TESTCHAT_API_KEY=sk-secret" in env_text

    persisted = yaml.safe_load(_owner_overlay_path(config_yaml.parent).read_text(encoding="utf-8"))
    assert persisted["channels"]["testchat"]["enabled"] is True
    assert persisted["channels"]["testchat"]["serverUrl"] == "wss://dummy.example/ws"
    assert "apiKey" not in persisted["channels"]["testchat"]


@pytest.mark.asyncio
async def test_owner_env_secret_is_used_when_connecting_securechat(channel_api, auth_headers):
    app, config_yaml = channel_api
    old_entries = list(platform_registry.all_entries())
    DummySecureChatChannel.starts = 0
    securechat_entry = _dummy_securechat_entry()
    platform_registry._entries.clear()
    platform_registry.register(securechat_entry)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
            saved = await client.put(
                "/api/platforms/securechat/config",
                json={
                    "enabled": True,
                    "config": {"wsUrl": "wss://securechat.example/ws", "clientId": "client-1"},
                    "secrets": {"secretKey": "securechat-secret"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert "securechat-secret" not in saved.text
            connected = await client.post("/api/platforms/securechat/connect")
            status = await client.get("/api/platforms")
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)

    assert connected.status_code == 200, connected.text
    assert connected.json()["ok"] is True
    assert DummySecureChatChannel.starts == 1
    row = next(item for item in status.json() if item["name"] == "securechat")
    assert row["configured"] is True
    assert row["has_account"] is True
    assert "securechat-secret" not in status.text

    env_text = (_owner_overlay_path(config_yaml.parent).parent / ".env").read_text(encoding="utf-8")
    assert "SECURECHAT_SECRET_KEY=securechat-secret" in env_text


def _assert_channel_restores_on_gateway_startup(
    channel_api,
    auth_headers,
    *,
    entry_factory,
    platform: str,
    config_payload: dict,
    dummy_cls,
    extra_assert=None,
):
    """绑定 owner 的渠道在 gateway 重启后按 owner overlay 恢复运行。"""
    app, config_yaml = channel_api
    old_entries = list(platform_registry.all_entries())
    dummy_cls.starts = 0
    entry = entry_factory()
    platform_registry._entries.clear()
    platform_registry.register(entry)
    try:
        with TestClient(app) as client:
            saved = client.put(
                f"/api/platforms/{platform}/config",
                json=config_payload,
                headers=auth_headers,
            )
            assert saved.status_code == 200, saved.text
            connected = client.post(f"/api/platforms/{platform}/connect", headers=auth_headers)
            assert connected.status_code == 200, connected.text
            assert connected.json()["ok"] is True
            assert dummy_cls.starts == 1
            assert app.state.crew.channel_bindings.get_binding(platform) == "A:uid-a"

        cfg = load_config(config_path=str(config_yaml))
        crew = build_app(config=cfg, enable_team=False)
        platform_registry._entries.clear()
        platform_registry.register(entry)
        restarted = create_app(crew)
        restarted.state.crew = crew
        with TestClient(restarted) as client:
            restored = client.get("/api/platforms", headers=auth_headers)
    finally:
        platform_registry._entries.clear()
        for old in old_entries:
            platform_registry.register(old)

    assert restored.status_code == 200, restored.text
    row = next(item for item in restored.json() if item["name"] == platform)
    assert row["running"] is True
    assert row["has_account"] is True
    assert dummy_cls.starts == 2
    if extra_assert is not None:
        extra_assert()


def test_bound_owner_channel_restores_on_gateway_startup(channel_api, auth_headers):
    _assert_channel_restores_on_gateway_startup(
        channel_api,
        auth_headers,
        entry_factory=_dummy_securechat_entry,
        platform="securechat",
        config_payload={
            "enabled": True,
            "config": {"wsUrl": "wss://securechat.example/ws", "clientId": "client-1"},
            "secrets": {"secretKey": "securechat-secret"},
        },
        dummy_cls=DummySecureChatChannel,
    )


def test_bound_testchat_channel_restores_on_gateway_startup(channel_api, auth_headers):
    _assert_channel_restores_on_gateway_startup(
        channel_api,
        auth_headers,
        entry_factory=_dummy_testchat_entry,
        platform="testchat",
        config_payload={
            "enabled": True,
            "config": {"serverUrl": "wss://dummy.example/ws"},
            "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
        },
        dummy_cls=DummyChannel,
    )


def test_bound_owner_channel_wins_over_global_config_on_gateway_startup(channel_api, auth_headers):
    app, config_yaml = channel_api
    old_entries = list(platform_registry.all_entries())
    DummyChannel.starts = 0
    DummyChannel.started_configs = []
    testchat_entry = _dummy_testchat_entry()
    platform_registry._entries.clear()
    platform_registry.register(testchat_entry)
    try:
        with TestClient(app) as client:
            saved = client.put(
                "/api/platforms/testchat/config",
                json={
                    "enabled": True,
                    "config": {"serverUrl": "wss://owner.example/ws"},
                    "secrets": {"TESTCHAT_API_KEY": "owner-secret"},
                },
                headers=auth_headers,
            )
            assert saved.status_code == 200, saved.text
            connected = client.post("/api/platforms/testchat/connect", headers=auth_headers)
            assert connected.status_code == 200, connected.text

        data = yaml.safe_load(config_yaml.read_text(encoding="utf-8")) or {}
        data.setdefault("channels", {})["testchat"] = {
            "enabled": True,
            "serverUrl": "wss://global.example/ws",
            "apiKey": "global-secret",
        }
        config_yaml.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        os.environ["TESTCHAT_API_KEY"] = "process-secret"

        cfg = load_config(config_path=str(config_yaml))
        crew = build_app(config=cfg, enable_team=False)
        platform_registry._entries.clear()
        platform_registry.register(testchat_entry)
        restarted = create_app(crew)
        restarted.state.crew = crew
        with TestClient(restarted) as client:
            restored = client.get("/api/platforms", headers=auth_headers)
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)

    assert restored.status_code == 200, restored.text
    row = next(item for item in restored.json() if item["name"] == "testchat")
    assert row["running"] is True
    assert row["has_account"] is True
    assert DummyChannel.starts == 2
    assert DummyChannel.started_configs[-1]["serverUrl"] == "wss://owner.example/ws"
    assert DummyChannel.started_configs[-1]["apiKey"] == "owner-secret"


def test_platform_status_detail_redacts_owner_env_secret(channel_api, auth_headers):
    app, _config_yaml = channel_api
    with TestClient(app) as client:
        saved = client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {
                    "serverUrl": "wss://dummy.example/ws",
                    "lastError": "auth failed for detail-secret",
                },
                "secrets": {"TESTCHAT_API_KEY": "detail-secret"},
            },
            headers=auth_headers,
        )
        assert saved.status_code == 200, saved.text
        connected = client.post("/api/platforms/testchat/connect", headers=auth_headers)
        assert connected.status_code == 200, connected.text
        status = client.get("/api/platforms", headers=auth_headers)

    assert status.status_code == 200, status.text
    row = next(item for item in status.json() if item["name"] == "testchat")
    assert row["detail"]["apiKey"] == "***"
    assert row["detail"]["last_error"] == "auth failed for ***"
    assert "detail-secret" not in status.text


def test_bound_feishu_channel_restores_on_gateway_startup(channel_api, auth_headers):
    def _check_started_config():
        assert DummyFeishuChannel.started_configs[-1]["appId"] == "owner-app"
        assert DummyFeishuChannel.started_configs[-1]["appSecret"] == "owner-secret"

    _assert_channel_restores_on_gateway_startup(
        channel_api,
        auth_headers,
        entry_factory=_dummy_feishu_entry,
        platform="feishu",
        config_payload={
            "enabled": True,
            "config": {"appId": "owner-app"},
            "secrets": {"appSecret": "owner-secret"},
        },
        dummy_cls=DummyFeishuChannel,
        extra_assert=_check_started_config,
    )


@pytest.mark.asyncio
async def test_owner_env_secret_can_be_reused_when_completing_securechat_config(channel_api, auth_headers):
    app, config_yaml = channel_api
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    platform_registry.register(_dummy_securechat_entry())
    try:
        write_env_key(resolve_writable_env_path("A:uid-a"), "SECURECHAT_SECRET_KEY", "existing-secret", sync_process_env=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
            before = await client.get("/api/platforms/securechat/config")
            saved = await client.put(
                "/api/platforms/securechat/config",
                json={
                    "enabled": True,
                    "config": {"wsUrl": "wss://securechat.example/ws", "clientId": "client-1"},
                    "secrets": {},
                },
            )
            after = await client.get("/api/platforms")
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)

    assert before.status_code == 200
    assert before.json()["has_secret"]["SECURECHAT_SECRET_KEY"] is True
    assert before.json()["has_account"] is False
    assert saved.status_code == 200, saved.text
    row = next(item for item in after.json() if item["name"] == "securechat")
    assert row["configured"] is True
    assert row["has_account"] is True
    assert "existing-secret" not in saved.text

    env_text = (_owner_overlay_path(config_yaml.parent).parent / ".env").read_text(encoding="utf-8")
    assert "SECURECHAT_SECRET_KEY=existing-secret" in env_text


@pytest.mark.asyncio
async def test_local_owner_can_save_platform_config(channel_api, auth_headers):
    app, config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws"},
                "secrets": {"TESTCHAT_API_KEY": "sk-b"},
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    persisted = yaml.safe_load(_owner_overlay_path(config_yaml.parent).read_text(encoding="utf-8"))
    assert persisted["channels"]["testchat"]["enabled"] is True


@pytest.mark.asyncio
async def test_platform_config_is_not_visible_to_other_owner(channel_api, auth_headers, monkeypatch):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        saved = await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws"},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        assert saved.status_code == 200
        connected = await client.post("/api/platforms/testchat/connect")
        assert connected.status_code == 200
        logged_out = await client.post("/api/auth/logout")
        assert logged_out.status_code == 200

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "B:uid-b")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config_resp = await client.get("/api/platforms/testchat/config")
        list_resp = await client.get("/api/platforms")

    assert config_resp.status_code == 200
    config_body = config_resp.json()
    assert config_body["has_account"] is False
    assert not any(config_body["has_secret"].values())
    assert "serverUrl" not in config_body["config"]

    assert list_resp.status_code == 200
    row = next(item for item in list_resp.json() if item["name"] == "testchat")
    assert row["has_account"] is False
    assert row["running"] is False


@pytest.mark.asyncio
async def test_reconnect_platform_starts_only_target_channel(channel_api, auth_headers):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws"},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        resp = await client.post("/api/platforms/testchat/reconnect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"]["running"] is True
    assert DummyChannel.starts == 1
    assert DummyChannel.stops == 0


@pytest.mark.asyncio
async def test_disconnect_platform_stops_channel_without_deleting_account(channel_api, auth_headers):
    app, config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws"},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        assert (await client.post("/api/platforms/testchat/connect")).status_code == 200
        resp = await client.post("/api/platforms/testchat/disconnect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"]["running"] is False
    env_text = (_owner_overlay_path(config_yaml.parent).parent / ".env").read_text(encoding="utf-8")
    assert "TESTCHAT_API_KEY=sk-secret" in env_text
    persisted = yaml.safe_load(_owner_overlay_path(config_yaml.parent).read_text(encoding="utf-8"))
    assert persisted["channels"]["testchat"]["enabled"] is False
    assert persisted["channels"]["testchat"]["serverUrl"] == "wss://dummy.example/ws"


@pytest.mark.asyncio
async def test_delete_platform_account_clears_id_and_key_but_keeps_fixed_url(channel_api, auth_headers):
    app, config_yaml = channel_api
    owner_overlay = _owner_overlay_path(config_yaml.parent)
    owner_overlay.parent.mkdir(parents=True, exist_ok=True)
    owner_overlay.write_text(
        yaml.safe_dump(
            {
                "channels": {
                    "testchat": {
                        "enabled": True,
                        "serverUrl": "wss://fixed.example/ws",
                        "clientId": "legacy-client",
                        "apiKey": "legacy-yaml-secret",
                        "extra": {
                            "api_key": "nested-secret",
                            "client_id": "nested-client",
                            "keep": "kept",
                        },
                    }
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (owner_overlay.parent / ".env").write_text("TESTCHAT_API_KEY=sk-secret\n", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.post("/api/platforms/testchat/connect")
        resp = await client.delete("/api/platforms/testchat/account")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"]["running"] is False
    env_path = _owner_overlay_path(config_yaml.parent).parent / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    assert "TESTCHAT_API_KEY" not in env_text

    persisted = yaml.safe_load(_owner_overlay_path(config_yaml.parent).read_text(encoding="utf-8"))
    testchat = persisted["channels"]["testchat"]
    assert testchat["enabled"] is False
    assert testchat["serverUrl"] == "wss://fixed.example/ws"
    assert "clientId" not in testchat
    assert "apiKey" not in testchat
    assert testchat["extra"] == {"keep": "kept"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_request, expected_error",
    [
        (
            lambda client: client.post("/api/platforms/testchat/disconnect"),
            "正在重连",
        ),
        (
            lambda client: client.put(
                "/api/platforms/testchat/config",
                json={"enabled": True, "config": {"serverUrl": "wss://other.example/ws"}},
            ),
            None,
        ),
        (
            lambda client: client.delete("/api/platforms/testchat/account"),
            None,
        ),
    ],
    ids=["disconnect", "save-config", "delete-account"],
)
async def test_operation_rejects_during_reconnect(channel_api, auth_headers, blocked_request, expected_error):
    app, _config_yaml = channel_api
    DummyChannel.stop_delay = 0.05
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws"},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        assert (await client.post("/api/platforms/testchat/connect")).status_code == 200
        import asyncio

        reconnect = asyncio.create_task(client.post("/api/platforms/testchat/reconnect"))
        await asyncio.sleep(0)
        blocked = await blocked_request(client)
        reconnect_resp = await reconnect

    assert reconnect_resp.status_code == 200
    assert blocked.status_code == 409
    if expected_error is not None:
        assert expected_error in blocked.json()["error"]


@pytest.mark.asyncio
async def test_reconnect_error_is_redacted(channel_api, auth_headers):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://dummy.example/ws", "failStart": True},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        resp = await client.post("/api/platforms/testchat/reconnect")

    assert resp.status_code == 500
    assert "sk-secret" not in resp.text
    assert "***" in resp.text


@pytest.mark.asyncio
async def test_rejects_unknown_secret_env_field(channel_api, auth_headers):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    os.environ.pop("BAD_SECRET_ENV", None)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.put(
            "/api/platforms/testchat/config",
            json={"enabled": True, "config": {"serverUrl": "wss://dummy.example/ws"}, "secrets": {"BAD_SECRET_ENV": "bad"}},
        )
    assert resp.status_code == 400
    assert os.environ.get("BAD_SECRET_ENV") != "bad"


@pytest.mark.asyncio
async def test_platform_status_redacts_secret_values_in_urls(channel_api, auth_headers):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/platforms/testchat/config",
            json={
                "enabled": True,
                "config": {"serverUrl": "wss://user:pass@dummy.example/ws?token=url-secret"},
                "secrets": {"TESTCHAT_API_KEY": "sk-secret"},
            },
        )
        await client.post("/api/platforms/testchat/reconnect")
        resp = await client.get("/api/platforms")

    assert resp.status_code == 200
    text = resp.text
    assert "url-secret" not in text
    assert "pass@dummy" not in text
    assert "***" in text


@pytest.mark.asyncio
async def test_unknown_platform_returns_404(channel_api, auth_headers):
    app, _config_yaml = channel_api
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.put("/api/platforms/nope/config", json={})
    assert resp.status_code == 404
