"""Provider-neutral remote authentication contract tests."""

from __future__ import annotations

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway import auth
from crew.gateway.app import create_app
from crew.gateway.logout import LogoutCleanupError
from crew.gateway.platform_registry import platform_registry
from crew.gateway.routers import remote_auth
from crew.security.outbound import OutboundHttpResponse
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


def test_remote_session_is_bound_to_gateway_instance_and_unique_session(
    tmp_path,
    monkeypatch,
) -> None:
    first_home = tmp_path / "first"
    monkeypatch.setenv("CREW_HOME", str(first_home))
    first = auth.create_remote_session_token("email", "user@example.com", ttl_seconds=600)
    second = auth.create_remote_session_token("email", "user@example.com", ttl_seconds=600)
    encoded = first.split(".", 1)[0]
    payload = json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )

    assert first != second
    assert payload["aud"] == "ace-gateway-remote-session"
    assert payload["purpose"] == "owner-authentication"
    assert len(payload["sid"]) >= 24
    assert len(payload["instance"]) == 64

    monkeypatch.setenv("CREW_HOME", str(tmp_path / "second"))
    with pytest.raises(auth.AuthenticationError, match="登录会话无效"):
        auth.account_from_remote_session_token(
            first,
            Config(auth_mode="email"),
        )


def test_remote_session_rotation_revokes_existing_tokens(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    token = auth.create_remote_session_token(
        "email",
        "user@example.com",
        ttl_seconds=600,
    )

    auth.rotate_remote_session_signing_key()

    with pytest.raises(auth.AuthenticationError, match="登录会话无效"):
        auth.account_from_remote_session_token(token, Config(auth_mode="email"))


def test_remote_session_rotation_rolls_back_unverifiable_store_write(
    tmp_path,
    monkeypatch,
    _isolated_platform_secret_backend,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    token = auth.create_remote_session_token(
        "email",
        "user@example.com",
        ttl_seconds=600,
    )
    backend = _isolated_platform_secret_backend
    original_get = backend.get_password
    reads = 0

    def fail_verification(service: str, account: str):
        nonlocal reads
        reads += 1
        if reads == 2:
            return "corrupt-after-write"
        return original_get(service, account)

    monkeypatch.setattr(backend, "get_password", fail_verification)

    with pytest.raises(auth.AuthenticationError, match="无法轮换"):
        auth.rotate_remote_session_signing_key()

    assert (
        auth.account_from_remote_session_token(
            token,
            Config(auth_mode="email"),
        ).user_id
        == "user@example.com"
    )


def test_remote_session_creation_rolls_back_unverifiable_store_write(
    tmp_path,
    monkeypatch,
    _isolated_platform_secret_backend,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    backend = _isolated_platform_secret_backend
    before = dict(backend.values)
    original_get = backend.get_password
    reads = 0

    def fail_verification(service: str, account: str):
        nonlocal reads
        reads += 1
        if reads == 3:
            return "corrupt-after-write"
        return original_get(service, account)

    monkeypatch.setattr(backend, "get_password", fail_verification)

    with pytest.raises(auth.AuthenticationError, match="无法写入"):
        auth.create_remote_session_token(
            "email",
            "user@example.com",
            ttl_seconds=600,
        )

    assert backend.values == before
    assert not auth._session_key_path().exists()


def test_expired_remote_session_has_same_error_as_invalid(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    token = auth.create_remote_session_token(
        "email",
        "user@example.com",
        ttl_seconds=300,
    )
    monkeypatch.setattr(auth.time, "time", lambda: 1301)

    with pytest.raises(auth.AuthenticationError) as expired:
        auth.account_from_remote_session_token(token, Config(auth_mode="email"))
    with pytest.raises(auth.AuthenticationError) as invalid:
        auth.account_from_remote_session_token("invalid.token", Config(auth_mode="email"))

    assert str(expired.value) == str(invalid.value) == "登录会话无效"


@pytest.mark.asyncio
async def test_email_login_hides_session_store_failures(email_api, monkeypatch):
    def fail_session(*_args, **_kwargs):
        raise RuntimeError(r"C:\Users\owner\.crew\.auth\session.key token=must-not-leak")

    monkeypatch.setattr(remote_auth, "create_remote_session_token", fail_session)
    transport = ASGITransport(app=email_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"email": "user@example.com"},
        )

    assert response.status_code == 500
    assert response.json()["error"] == "无法创建登录会话"
    assert "session.key" not in response.text
    assert "must-not-leak" not in response.text


@pytest.mark.asyncio
async def test_email_logout_hides_cleanup_failure_details(
    email_api,
    monkeypatch,
    caplog,
):
    canary = "logout-secret-canary"

    async def fail_logout(_owner):
        raise LogoutCleanupError(
            rf"C:\private\credential.json access_token={canary}"
        )

    monkeypatch.setattr(
        email_api.state.crew.logout_coordinator,
        "logout",
        fail_logout,
    )
    transport = ASGITransport(app=email_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        logged_in = await client.post(
            "/api/auth/login",
            json={"email": "user@example.com"},
        )
        assert logged_in.status_code == 200
        response = await client.post("/api/auth/logout")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "released": False,
        "code": "LOGOUT_CLEANUP_FAILED",
        "error": "注销清理未完成",
    }
    assert canary not in response.text
    assert canary not in caplog.text
    assert "credential.json" not in response.text


@pytest.mark.asyncio
async def test_email_login_cookie_flags_follow_transport(email_api):
    transport = ASGITransport(app=email_api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"email": "user@example.com"},
        )

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" in cookie


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
async def test_remote_auth_uses_shared_pinned_http_client(monkeypatch):
    seen: dict[str, object] = {}

    class PinnedClient:
        def fetch(self, url: str, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return OutboundHttpResponse(
                final_url=url,
                status=200,
                headers={"content-type": "application/json"},
                body=b'{"ok":true}',
                content_type="application/json",
                charset="utf-8",
            )

    monkeypatch.setattr(remote_auth, "_REMOTE_AUTH_HTTP", PinnedClient(), raising=False)
    config = type(
        "RemoteConfig",
        (),
        {"auth_base_url": "https://auth.example", "auth_timeout_seconds": 5},
    )()

    status, payload = await remote_auth._post_json(
        config,
        "/auth/send-code",
        {"phoneNumber": "13800000000"},
    )

    assert (status, payload) == (200, {"ok": True})
    assert seen["url"] == "https://auth.example/auth/send-code"
    assert seen["kwargs"]["max_redirects"] == 0


def test_remote_auth_error_message_redacts_url_credentials() -> None:
    rendered = remote_auth._message(
        {
            "error": (
                "failed at https://user:password@example.test/path"
                "?access_token=query-secret"
            )
        },
        "fallback",
    )

    assert "password@" not in rendered
    assert "query-secret" not in rendered


def test_remote_auth_rejects_credential_bearing_base_url() -> None:
    config = type(
        "RemoteConfig",
        (),
        {
            "auth_base_url": (
                "https://auth.example/rpc?access_token=query-secret"
            )
        },
    )()

    assert remote_auth._remote_base_url(config) == ""


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
