"""Provider-neutral remote authentication contract tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.app import create_app
from crew.gateway.platform_registry import platform_registry
from crew.state.config import Config, load_config


@pytest.fixture
def remote_api(tmp_path, monkeypatch):
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        # 显式 remote 必须优先于开发启动旁路，才能在 npm run dev 下联调登录。
        gateway_dev_mode=True,
        auth_mode="remote",
        auth_provider_id="example",
        auth_base_url="https://auth.example",
        plugins_enabled=[],
    )
    try:
        crew = build_app(config=config, enable_team=False)
        platform_registry._entries.clear()
        api = create_app(crew)
        api.state.crew = crew
        yield api
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)


@pytest.fixture
def email_api(tmp_path, monkeypatch):
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        auth_mode="email",
        plugins_enabled=[],
    )
    try:
        crew = build_app(config=config, enable_team=False)
        platform_registry._entries.clear()
        api = create_app(crew)
        api.state.crew = crew
        yield api
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)


@pytest.mark.asyncio
async def test_remote_mode_requires_login(remote_api):
    transport = ASGITransport(app=remote_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/api/auth/config")
        sessions = await client.get("/api/sessions")

    assert config.status_code == 200
    assert config.json() == {
        "ok": True,
        "mode": "remote",
        "configured": True,
        "providerId": "example",
    }
    assert sessions.status_code == 401


@pytest.mark.asyncio
async def test_email_mode_normalizes_email_and_scopes_owner(email_api):
    transport = ASGITransport(app=email_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/api/auth/config")
        before = await client.get("/api/sessions")
        logged_in = await client.post(
            "/api/auth/login",
            json={"email": "  Tenant.User@Example.COM  "},
        )
        sessions = await client.get("/api/sessions")
        owner = email_api.state.crew.active_owner.current()
        logged_out = await client.post("/api/auth/logout")
        after_logout = await client.get("/api/sessions")

    assert config.json() == {
        "ok": True,
        "mode": "email",
        "configured": True,
        "providerId": "email",
    }
    assert before.status_code == 401
    assert logged_in.status_code == 200
    assert logged_in.json()["user"] == {
        "userId": "tenant.user@example.com",
        "email": "tenant.user@example.com",
        "phoneNumber": "",
    }
    assert sessions.status_code == 200
    assert owner is not None
    assert owner.owner_account_id == "email:tenant.user@example.com"
    assert logged_out.status_code == 200
    assert after_logout.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["", "missing-at.example.com", "a@b", "a b@example.com"])
async def test_email_mode_rejects_invalid_email(email_api, email):
    transport = ASGITransport(app=email_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/auth/login", json={"email": email})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_remote_login_routes_reject_non_loopback_client(remote_api):
    transport = ASGITransport(app=remote_api, client=("203.0.113.10", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/api/auth/config")
        login = await client.post(
            "/api/auth/login",
            json={"phoneNumber": "13800000000", "code": "123456"},
        )

    assert config.status_code == 401
    assert login.status_code == 401


@pytest.mark.asyncio
async def test_remote_login_returns_generic_user_and_scopes_owner(remote_api, monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_post(_config, path: str, body: dict):
        calls.append((path, body))
        if path == "/auth/send-code":
            return 200, {"ok": True, "message": "sent"}
        return 200, {
            "ok": True,
            "user": {
                "userId": "user-123",
                "phoneNumber": "13800000000",
                "displayName": "Example User",
            },
        }

    monkeypatch.setattr("crew.gateway.routers.remote_auth._post_json", fake_post)
    transport = ASGITransport(app=remote_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sent = await client.post(
            "/api/auth/send-code",
            json={"phoneNumber": "13800000000"},
        )
        logged_in = await client.post(
            "/api/auth/login",
            json={"phoneNumber": "13800000000", "code": "123456"},
        )
        sessions = await client.get("/api/sessions")
        owner = remote_api.state.crew.active_owner.current()
        logged_out = await client.post("/api/auth/logout")
        after_logout = await client.get("/api/sessions")

    assert sent.status_code == 200
    assert logged_in.status_code == 200
    assert logged_in.json() == {
        "ok": True,
        "user": {
            "userId": "user-123",
            "phoneNumber": "13800000000",
            "displayName": "Example User",
        },
    }
    assert sessions.status_code == 200
    assert "13800000000" not in logged_in.headers.get("set-cookie", "")
    assert owner is not None
    assert owner.owner_account_id == "example:user-123"
    assert logged_out.status_code == 200
    assert after_logout.status_code == 401
    assert calls == [
        ("/auth/send-code", {"phoneNumber": "13800000000"}),
        ("/auth/login-by-code", {"phoneNumber": "13800000000", "code": "123456"}),
    ]


@pytest.mark.asyncio
async def test_remote_login_rejects_response_without_user_id(remote_api, monkeypatch):
    async def fake_post(_config, _path: str, _body: dict):
        return 200, {"ok": True, "user": {"phoneNumber": "13800000000"}}

    monkeypatch.setattr("crew.gateway.routers.remote_auth._post_json", fake_post)
    transport = ASGITransport(app=remote_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"phoneNumber": "13800000000", "code": "123456"},
        )

    assert response.status_code == 502
    assert response.json()["error"] == "认证服务未返回 userId"


def test_auth_base_url_environment_override(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "auth:\n"
        "  mode: remote\n"
        "  remote:\n"
        "    provider_id: example\n"
        "    base_url: https://xxxxx.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREW_AUTH_BASE_URL", "https://login.example")

    config = load_config(config_path=config_path)

    assert config.auth_mode == "remote"
    assert config.auth_provider_id == "example"
    assert config.auth_base_url == "https://login.example"


def test_email_auth_mode_loads_from_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("auth:\n  mode: email\n", encoding="utf-8")

    config = load_config(config_path=config_path)

    assert config.auth_mode == "email"
