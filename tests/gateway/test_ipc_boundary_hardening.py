"""Negative contracts for the Desktop/Gateway HTTP and WebSocket boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway import auth as gateway_auth
from crew.gateway.auth import (
    AuthenticationError,
    DESKTOP_REQUEST_ORIGIN,
    account_from_remote_session_token,
    create_remote_session_token,
    require_trusted_request_origin as real_require_trusted_request_origin,
    revoke_remote_owner_sessions,
)
from crew.gateway.auth_policy import (
    INTERNAL_BINDING_AUTH_EXEMPT_EXACT,
    requires_gateway_auth,
    requires_gateway_instance_auth,
)
from crew.gateway.platform_registry import platform_registry
from crew.gateway.route_auth import (
    RouteAuthResponsibility,
    declared_admin_routes,
    declared_public_paths,
    iter_gateway_routes,
    route_auth_responsibility,
    route_responsibilities,
)
from crew.gateway.server import create_app
from crew.state.config import Config


@pytest.fixture
def api(tmp_path, monkeypatch):
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            gateway_admin_accounts=["local"],
            plugins_enabled=[],
        ),
        enable_team=False,
    )
    platform_registry._entries.clear()
    app = create_app(crew)
    app.state.crew = crew
    try:
        yield app
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)


def _require_paired_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        lambda proof, **_kwargs: proof == "paired-desktop",
    )


@pytest.mark.asyncio
async def test_malicious_loopback_process_cannot_call_protected_rest(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.get("/api/sessions")
        forged = await client.get(
            "/api/sessions",
            headers={"X-Crew-Security-Proof": "forged-local-process"},
        )
        accepted = await client.get(
            "/api/sessions",
            headers={"X-Crew-Security-Proof": "paired-desktop"},
        )

    assert rejected.status_code == 401
    assert forged.status_code == 401
    assert accepted.status_code == 200


def test_malicious_loopback_process_cannot_open_websocket(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    with TestClient(api) as client:
        with pytest.raises(Exception) as rejected:  # noqa: BLE001 - client backend type varies
            with client.websocket_connect("/ws") as socket:
                socket.receive_json()
        assert getattr(rejected.value, "code", None) == 4401

        with pytest.raises(Exception) as forged:  # noqa: BLE001 - client backend type varies
            with client.websocket_connect(
                "/ws",
                headers={"X-Crew-Security-Proof": "forged-local-process"},
            ) as socket:
                socket.receive_json()
        assert getattr(forged.value, "code", None) == 4401

        with client.websocket_connect(
            "/ws",
            headers={"X-Crew-Security-Proof": "paired-desktop"},
        ):
            pass


def test_late_mounted_websocket_inherits_global_instance_auth(api, monkeypatch):
    _require_paired_proof(monkeypatch)

    @api.websocket("/ws/plugin-probe")
    async def plugin_probe(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"owner": socket.state.account.owner_account_id})

    with TestClient(api) as client:
        with pytest.raises(Exception) as rejected:  # noqa: BLE001 - client backend type varies
            with client.websocket_connect("/ws/plugin-probe") as socket:
                socket.receive_json()
        assert getattr(rejected.value, "code", None) == 4401

        with client.websocket_connect(
            "/ws/plugin-probe",
            headers={"X-Crew-Security-Proof": "paired-desktop"},
        ) as socket:
            assert socket.receive_json() == {"owner": "local"}


@pytest.mark.asyncio
async def test_cookie_mutation_rejects_missing_and_spoofed_origin(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    monkeypatch.setattr(
        gateway_auth,
        "require_trusted_request_origin",
        real_require_trusted_request_origin,
    )
    crew = api.state.crew
    crew.config.auth_mode = "email"
    token = create_remote_session_token(
        "email",
        "owner@example.com",
        ttl_seconds=600,
    )
    common = {
        "Cookie": f"crew_auth_session={token}",
        "X-Crew-Security-Proof": "paired-desktop",
    }
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/api/cron/jobs/missing/pause", headers=common)
        spoofed = await client.post(
            "/api/cron/jobs/missing/pause",
            headers={**common, "Origin": "https://evil.example"},
        )
        trusted = await client.post(
            "/api/cron/jobs/missing/pause",
            headers={**common, "Origin": DESKTOP_REQUEST_ORIGIN},
        )

    assert missing.status_code == 401
    assert spoofed.status_code == 401
    assert trusted.status_code != 401


def test_remote_session_rotates_and_server_revocation_blocks_replay(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    config = SimpleNamespace(
        auth_mode="email",
        auth_provider_id="email",
        gateway_dev_mode=False,
    )
    first = create_remote_session_token("email", "rotate@example.com", ttl_seconds=600)
    assert account_from_remote_session_token(first, config).user_id == "rotate@example.com"

    second = create_remote_session_token("email", "rotate@example.com", ttl_seconds=600)
    assert second != first
    with pytest.raises(AuthenticationError, match="登录会话无效"):
        account_from_remote_session_token(first, config)

    assert revoke_remote_owner_sessions("email:rotate@example.com") == 1
    with pytest.raises(AuthenticationError, match="登录会话无效"):
        account_from_remote_session_token(second, config)


@pytest.mark.asyncio
async def test_logout_revokes_cookie_before_old_cookie_can_replay(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    crew = api.state.crew
    crew.config.auth_mode = "email"
    token = create_remote_session_token(
        "email",
        "logout@example.com",
        ttl_seconds=600,
    )
    cookie = f"crew_auth_session={token}"
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        active = await client.get(
            "/api/sessions",
            headers={
                "Cookie": cookie,
                "X-Crew-Security-Proof": "paired-desktop",
            },
        )
        logged_out = await client.post(
            "/api/auth/logout",
            headers={
                "Cookie": cookie,
                "Origin": DESKTOP_REQUEST_ORIGIN,
                "X-Crew-Security-Proof": "paired-desktop",
            },
        )
        replayed = await client.get(
            "/api/sessions",
            headers={
                "Cookie": cookie,
                "X-Crew-Security-Proof": "paired-desktop",
            },
        )

    assert active.status_code == 200
    assert logged_out.status_code == 200
    assert replayed.status_code == 401


def test_route_table_has_explicit_auth_responsibility(api):
    gateway_routes = list(iter_gateway_routes(api))
    assert gateway_routes
    assert all(route_responsibilities(route) for _path, route in gateway_routes)

    actual_responsibilities = {
        (method, path): responsibility
        for path, route in gateway_routes
        for method, responsibility in route_responsibilities(route).items()
    }
    assert declared_admin_routes() <= actual_responsibilities.keys()
    assert all(
        actual_responsibilities[key] is RouteAuthResponsibility.DESKTOP_ADMIN
        for key in declared_admin_routes()
    )
    actual_paths = {path for path, _route in gateway_routes}
    assert declared_public_paths() <= actual_paths
    for path, route in gateway_routes:
        for method, responsibility in route_responsibilities(route).items():
            if path in INTERNAL_BINDING_AUTH_EXEMPT_EXACT:
                assert responsibility is RouteAuthResponsibility.INTERNAL_BINDING
            elif requires_gateway_auth(path):
                assert responsibility in {
                    RouteAuthResponsibility.DESKTOP_OWNER,
                    RouteAuthResponsibility.DESKTOP_ADMIN,
                    RouteAuthResponsibility.OWNER_RESOURCE_GUARD,
                }
            elif requires_gateway_instance_auth(path):
                assert responsibility is RouteAuthResponsibility.DESKTOP_LOGIN_BOOTSTRAP


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/health"),
        ("GET", "/api/internal/interactions/ask"),
        ("GET", "/api/auth/login"),
    ],
)
def test_auth_exception_route_drift_fails_closed(method, path):
    with pytest.raises(RuntimeError, match="method-specific auth responsibility"):
        route_auth_responsibility(method, path)


@pytest.mark.asyncio
async def test_every_protected_rest_route_rejects_missing_authentication(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    cases = [
        (method, path.replace("{name}", "missing").replace("{task_id}", "missing"))
        for path, route in iter_gateway_routes(api)
        for method in route_responsibilities(route)
        if method != "WS" and requires_gateway_auth(path)
    ]
    assert len(cases) > 150

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in sorted(cases):
            response = await client.request(
                method,
                path,
                json={} if method in {"POST", "PUT", "PATCH"} else None,
            )
            assert response.status_code == 401, (method, path, response.status_code)


@pytest.mark.asyncio
async def test_low_privilege_owner_cannot_call_any_global_admin_route(
    api,
    monkeypatch,
):
    _require_paired_proof(monkeypatch)
    crew = api.state.crew
    crew.config.auth_mode = "email"
    crew.config.gateway_admin_accounts = ["email:admin@example.com"]
    token = create_remote_session_token(
        "email",
        "member@example.com",
        ttl_seconds=600,
    )
    headers = {
        "Cookie": f"crew_auth_session={token}",
        "Origin": DESKTOP_REQUEST_ORIGIN,
        "X-Crew-Security-Proof": "paired-desktop",
    }
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.request(
                method,
                path.replace("{name}", "missing")
                .replace("{task_id}", "missing")
                .replace("{plugin_key}", "missing")
                .replace("{slug}", "missing"),
                headers=headers,
                **({"json": {}} if method in {"POST", "PUT", "PATCH"} else {}),
            )
            for method, path in sorted(declared_admin_routes())
        ]

    assert all(response.status_code == 403 for response in responses)
    _, audited = api.state.crew.security_audit.query_page(
        owner_account_id="email:member@example.com",
        action_type="gateway_authorization",
        decision="deny",
        limit=100,
    )
    assert audited >= len(declared_admin_routes())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_production_rejects_non_loopback_bind_configuration(api, host):
    crew = api.state.crew
    crew.config.gateway_host = host

    with pytest.raises(RuntimeError, match="loopback"):
        create_app(crew)


@pytest.mark.asyncio
async def test_protected_route_denial_is_durably_audited_without_sensitive_echo(api, monkeypatch):
    _require_paired_proof(monkeypatch)
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/sessions?secret=do-not-audit",
            headers={"Authorization": "Bearer do-not-audit"},
        )

    assert response.status_code == 401
    records, total = api.state.crew.security_audit.query_page(
        owner_account_id="unauthenticated",
        action_type="gateway_authorization",
        decision="deny",
    )
    assert total >= 1
    event = records[0]
    assert event.decision_source == "gateway-auth-middleware"
    assert event.stable_error_code == "HTTP_401"
    assert event.action_detail == "GET /api/sessions denied with HTTP 401"
    assert "do-not-audit" not in event.action_detail


@pytest.mark.asyncio
async def test_protected_route_denial_fails_closed_when_audit_is_unavailable(
    api,
    monkeypatch,
):
    _require_paired_proof(monkeypatch)

    def unavailable(_event):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(api.state.crew.security_audit, "record", unavailable)
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sessions")

    assert response.status_code == 503
    assert response.json()["code"] == "SECURITY_AUDIT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_unauthenticated_denial_audit_flood_is_bounded(api, monkeypatch):
    events = []

    def record(event):
        events.append(event)
        return event.event_id

    monkeypatch.setattr(api.state.crew.security_audit, "record", record)
    _require_paired_proof(monkeypatch)
    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(33):
            response = await client.get("/api/sessions")

    assert response.status_code == 401
    assert len(events) == 32
