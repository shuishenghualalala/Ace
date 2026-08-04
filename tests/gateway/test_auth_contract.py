"""Gateway 鉴权契约测试。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

from crew.app import build_app
from crew.gateway.channel_manager import ChannelManager
from crew.gateway.platform_registry import platform_registry
from crew.gateway.routers.channels import create_channels_router
from crew.gateway.server import create_app
from crew.state.config import Config, load_config


def _restore_platform_entries(entries):
    platform_registry._entries.clear()
    for entry in entries:
        platform_registry.register(entry)


def _feishu_webhook_app(channel, *, dev_mode: bool = False) -> FastAPI:
    manager = ChannelManager()
    manager.register(channel)
    crew = SimpleNamespace(
        active_owner=SimpleNamespace(
            current=lambda: SimpleNamespace(owner_account_id="A:uid-a"),
        ),
        config=SimpleNamespace(gateway_dev_mode=dev_mode),
    )
    app = FastAPI()
    app.include_router(create_channels_router(crew, None, manager))
    return app


@pytest.fixture
def api(tmp_path, monkeypatch):
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        gateway_admin_accounts=["local"],
        plugins_enabled=[],
    )
    try:
        crew = build_app(config=cfg, enable_team=False)
        platform_registry._entries.clear()
        app = create_app(crew)
        app.state.crew = crew
        yield app
    finally:
        _restore_platform_entries(old_entries)


@pytest.fixture
def api_dev(tmp_path, monkeypatch):
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        gateway_dev_mode=True,
        gateway_dev_account="dev:dev",
        plugins_enabled=[],
    )
    try:
        crew = build_app(config=cfg, enable_team=False)
        platform_registry._entries.clear()
        app = create_app(crew)
        app.state.crew = crew
        yield app
    finally:
        _restore_platform_entries(old_entries)


@pytest.mark.asyncio
async def test_api_rejects_non_loopback_request(api):
    transport = ASGITransport(app=api, client=("10.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")

    assert resp.status_code == 401


def test_legacy_gateway_auth_config_is_ignored(tmp_path, monkeypatch):
    """旧静态 Token/认证地址不得重新进入当前 Gateway 配置契约。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "gateway:\n  auth_token: old-token\n  auth_base_url: https://old.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "old-env-token")

    config = load_config(config_path=config_path)

    assert not hasattr(config, "gateway_auth_token")
    # 旧 gateway.auth_base_url 与 GATEWAY_AUTH_TOKEN 仍被忽略；新的认证地址
    # 只从顶层 auth.remote 或 CREW_AUTH_BASE_URL 读取。
    assert config.auth_base_url == ""


@pytest.mark.asyncio
async def test_loopback_request_uses_local_owner_without_identity_headers(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")

    assert resp.status_code == 200
    assert api.state.crew.active_owner.current().owner_account_id == "local"


@pytest.mark.asyncio
async def test_repeated_loopback_requests_share_local_owner(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/sessions")
        second = await client.get("/api/sessions")

    assert first.status_code == 200
    assert second.status_code == 200
    assert api.state.crew.active_owner.current().owner_account_id == "local"


@pytest.mark.asyncio
async def test_explicit_logout_releases_local_owner_and_next_request_reclaims_it(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/sessions")).status_code == 200
        logged_out = await client.post("/api/auth/logout")
        assert api.state.crew.active_owner.current() is None
        next_owner = await client.get("/api/sessions")

    assert logged_out.status_code == 200
    assert logged_out.json()["released"] is True
    assert next_owner.status_code == 200


@pytest.mark.asyncio
async def test_logout_without_active_owner_is_idempotent_and_does_not_claim(api):
    transport = ASGITransport(app=api)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json()["released"] is True
    assert api.state.crew.active_owner.current() is None


def test_websocket_disconnect_does_not_release_active_owner(api):
    with TestClient(api) as client:
        assert client.get("/api/sessions").status_code == 200
        with client.websocket_connect("/ws"):
            pass
        assert api.state.crew.active_owner.current().owner_account_id == "local"


def test_gateway_restart_completes_pending_logout_before_readiness(api):
    crew = api.state.crew
    crew.active_owner.claim("A:uid-a")
    assert crew.active_owner.prepare_restart_logout("A:uid-a") is True

    with TestClient(api) as client:
        assert client.get("/api/health").status_code == 200
        assert crew.active_owner.current() is None
        assert crew.active_owner.pending_restart_logout() is None


def test_login_required_mode_releases_legacy_local_owner_on_startup(api):
    crew = api.state.crew
    crew.config.auth_mode = "email"
    crew.active_owner.claim("local")

    with TestClient(api) as client:
        assert client.get("/api/health").status_code == 200
        assert crew.active_owner.current() is None


def test_health_is_ready_while_business_requests_wait_for_deferred_startup(api):
    crew = api.state.crew
    startup_entered = threading.Event()
    startup_release = threading.Event()

    async def blocked_startup() -> None:
        startup_entered.set()
        while not startup_release.is_set():
            await asyncio.sleep(0.01)

    crew.startup = blocked_startup
    with TestClient(api) as client, ThreadPoolExecutor(max_workers=1) as pool:
        assert startup_entered.wait(timeout=1)
        assert client.get("/api/health").status_code == 200

        business_request = pool.submit(client.get, "/api/scenarios")
        deadline = time.monotonic() + 1
        while not business_request.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert business_request.running()
        time.sleep(0.05)
        assert not business_request.done()
        startup_release.set()
        assert business_request.result(timeout=1).status_code == 200


def test_deferred_startup_does_not_connect_channels_without_active_owner(api):
    crew = api.state.crew
    starts: list[object] = []

    async def record_start(handler) -> None:
        starts.append(handler)

    crew.logout_coordinator._channel_manager.start_all = record_start
    with TestClient(api) as client:
        # A non-health API crosses the startup fence, so this observes the
        # completed deferred startup rather than racing it.
        assert client.get("/api/scenarios").status_code == 200
        assert crew.active_owner.current() is None

    assert starts == []


def test_health_reports_cron_start_failure_without_blocking_gateway(api):
    crew = api.state.crew

    class FailingCronService:
        is_running = False
        start_error = "scheduler unavailable"

        async def start(self) -> None:
            raise RuntimeError(self.start_error)

        async def stop(self) -> None:
            return None

    crew.cron_service = FailingCronService()
    with TestClient(api) as client:
        assert client.get("/api/scenarios").status_code == 200
        health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["components"]["cron"] == {
        "status": "failed",
        "message": "定时任务启动失败，请查看 Gateway 日志",
    }


def test_health_reports_deferred_startup_failure_without_losing_readiness(api):
    async def failing_startup() -> None:
        raise RuntimeError("mcp unavailable")

    api.state.crew.startup = failing_startup
    with TestClient(api) as client:
        business = client.get("/api/scenarios")
        health = client.get("/api/health")

    assert business.status_code == 503
    assert health.status_code == 200
    assert health.json()["components"]["startup"] == {
        "status": "failed",
        "message": "运行环境组件初始化失败，请查看 Gateway 日志",
    }


@pytest.mark.asyncio
async def test_health_is_exempt_but_feishu_requires_active_owner_before_body_parse(api):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/health")
        feishu = await client.post(
            "/api/feishu/events",
            content=b"this is deliberately not json",
            headers={"Content-Type": "application/json"},
        )

    assert health.status_code == 200
    assert feishu.status_code == 503
    assert feishu.json() == {
        "ok": False,
        "error": "Gateway 未登录，飞书渠道已断开",
        "code": "LOGIN_REQUIRED",
    }


@pytest.mark.asyncio
async def test_feishu_webhook_requires_verification_token_before_body_parse():
    channel = SimpleNamespace(
        name="feishu",
        settings=SimpleNamespace(verification_token=""),
        ingress_available=lambda owner: True,
    )
    app = _feishu_webhook_app(channel)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/feishu/events",
            content=b"not json and must not be parsed",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "FEISHU_WEBHOOK_TOKEN_REQUIRED"


@pytest.mark.asyncio
async def test_feishu_webhook_200_means_event_was_enqueued():
    accepted = []
    channel = SimpleNamespace(
        name="feishu",
        settings=SimpleNamespace(verification_token="expected"),
        ingress_available=lambda owner: owner == "A:uid-a",
        verify_webhook=lambda payload, allow_missing_token=False: (
            payload.get("header", {}).get("token") == "expected"
        ),
        challenge_response=lambda payload: None,
        enqueue_webhook_event=lambda payload: (accepted.append(payload) or "accepted"),
    )
    app = _feishu_webhook_app(channel)
    payload = {
        "header": {"token": "expected"},
        "event": {"message": {}, "sender": {}},
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/feishu/events", json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert accepted == [payload]


@pytest.mark.asyncio
async def test_local_owner_can_use_admin_routes(api):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runtimes/register",
            json={"id": "rt-denied", "type": "claude", "provider": "anthropic"},
        )

    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_dev_mode_loopback_no_headers_passes(api_dev):
    transport = ASGITransport(app=api_dev, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dev_mode_uses_configured_local_owner(api_dev):
    transport = ASGITransport(app=api_dev, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")

    assert resp.status_code == 200
    assert api_dev.state.crew.active_owner.current().owner_account_id == "dev:dev"


def test_dev_mode_websocket_uses_configured_local_owner(api_dev):
    with TestClient(api_dev) as client:
        with client.websocket_connect("/ws"):
            lease = api_dev.state.crew.active_owner.current()
            assert lease is not None
            assert lease.owner_account_id == "dev:dev"


@pytest.mark.asyncio
async def test_dev_mode_non_loopback_still_rejected(api_dev):
    transport = ASGITransport(app=api_dev, client=("10.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dev_mode_admin_routes_allowed(api_dev):
    transport = ASGITransport(app=api_dev, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runtimes/register",
            json={"id": "rt-dev", "type": "claude", "provider": "anthropic"},
        )

    # dev 账号即 admin：不应被 403 拦截（其余业务校验失败也无所谓，只验证鉴权放行）。
    assert resp.status_code != 403
