"""Authorization contract for global MCP server mutations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from crew.gateway.auth import AccountContext
from crew.gateway.routers.mcp_servers import create_mcp_servers_router
from crew.security.audit import SQLiteSecurityAudit


class _Config:
    def __init__(self) -> None:
        self.gateway_admin_accounts = ["email:admin@example.com"]
        self.mcp_servers: dict[str, dict[str, Any]] = {
            "existing": {
                "command": "dummy",
                "env": {
                    "API_TOKEN": "super-secret-value",
                    "VISIBLE": "plain",
                },
            }
        }
        self.mutations: list[tuple[str, str]] = []

    def set_mcp_server(self, name: str, config: dict[str, Any]) -> None:
        self.mutations.append(("set", name))
        self.mcp_servers[name] = config

    def remove_mcp_server(self, name: str) -> None:
        self.mutations.append(("remove", name))
        self.mcp_servers.pop(name, None)

    def persist_mcp_servers(self) -> None:
        self.mutations.append(("persist", ""))


class _Manager:
    _registry = object()

    def __init__(self, config: _Config) -> None:
        self.config = config
        self.calls: list[tuple[str, str]] = []

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "transport": "stdio",
                "connected": False,
                "error": "",
                "tools": [],
                "config": server,
            }
            for name, server in self.config.mcp_servers.items()
        ]

    def register_pending(self, name: str, _config: dict[str, Any]) -> None:
        self.calls.append(("pending", name))

    async def add_server(self, name: str, _config: dict[str, Any]) -> None:
        self.calls.append(("add", name))

    async def reload_one(self, name: str, _config: dict[str, Any] | None = None) -> bool:
        self.calls.append(("reload", name))
        return True

    async def quiesce_server(self, name: str) -> bool:
        self.calls.append(("quiesce", name))
        return name in self.config.mcp_servers

    async def remove_server(self, name: str) -> None:
        self.calls.append(("remove", name))


@pytest.fixture
def mcp_auth_app(tmp_path: Path):
    config = _Config()
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    manager = _Manager(config)
    crew = SimpleNamespace(config=config, mcp_manager=manager, security_audit=audit)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated_account(request: Request, call_next):
        owner = request.headers.get("X-Test-Owner", "email:user@example.com")
        request.state.account = AccountContext(owner_account_id=owner)
        return await call_next(request)

    app.include_router(create_mcp_servers_router(crew))
    try:
        yield app, crew
    finally:
        audit.close()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/api/mcp/servers",
            {
                "name": "blocked",
                "command": "dummy",
                "env": {"API_TOKEN": "must-not-enter-audit"},
            },
        ),
        (
            "PUT",
            "/api/mcp/servers/existing",
            {"command": "dummy", "env": {"API_TOKEN": "must-not-enter-audit"}},
        ),
        ("DELETE", "/api/mcp/servers/existing", None),
        ("POST", "/api/mcp/servers/existing/reload", None),
    ],
)
@pytest.mark.asyncio
async def test_non_admin_cannot_mutate_global_mcp_servers_and_denial_is_audited(
    mcp_auth_app,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    app, crew = mcp_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            json=payload,
            headers={"X-Test-Owner": "email:user@example.com"},
        )

    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "需要管理员权限"}
    assert crew.config.mutations == []
    assert crew.mcp_manager.calls == []

    records = crew.security_audit.query(owner_account_id="email:user@example.com")
    assert len(records) == 1
    event = records[0]
    assert event.action_type == "mcp_server_admin_denied"
    assert event.decision == "deny"
    assert event.decision_source == "gateway_admin_policy"
    assert event.stable_error_code == "gateway_admin_required"
    assert event.tool_name == "gateway:mcp_servers"
    assert len(event.normalized_action_hash) == 64
    assert method in event.action_detail
    assert "must-not-enter-audit" not in event.action_detail


@pytest.mark.asyncio
async def test_non_admin_can_list_mcp_servers_with_env_redacted(mcp_auth_app) -> None:
    app, _crew = mcp_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/mcp/servers",
            headers={"X-Test-Owner": "email:user@example.com"},
        )

    assert response.status_code == 200
    config = response.json()["servers"][0]["config"]
    assert config["env"] == {"API_TOKEN": "***", "VISIBLE": "plain"}


@pytest.mark.asyncio
async def test_configured_admin_reaches_mcp_mutation_validation(mcp_auth_app) -> None:
    app, crew = mcp_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mcp/servers",
            json={"name": "bad name", "command": "dummy"},
            headers={"X-Test-Owner": "email:admin@example.com"},
        )

    assert response.status_code == 400
    assert crew.security_audit.query(owner_account_id="email:admin@example.com") == []


@pytest.mark.asyncio
async def test_admin_mcp_mutations_have_durable_digest_audit_and_reload_terminal_state(
    mcp_auth_app,
) -> None:
    app, crew = mcp_auth_app
    transport = ASGITransport(app=app)
    headers = {"X-Test-Owner": "email:admin@example.com"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mcp/servers",
            json={
                "name": "remote",
                "url": "https://mcp.example.invalid/rpc",
                "transport": "http",
                "headers": {"Authorization": "Bearer secret-canary"},
            },
            headers=headers,
        )
        assert response.status_code == 201

        response = await client.post(
            "/api/mcp/servers/remote/reload",
            headers=headers,
        )
        assert response.status_code == 200
        await asyncio.sleep(0)

    records = crew.security_audit.query(owner_account_id="email:admin@example.com")
    mutation_records = [
        event for event in records if event.action_type == "mcp_server_admin_action"
    ]
    assert any("outcome=succeeded" in event.action_detail for event in mutation_records)
    assert any("outcome=reload_succeeded" in event.action_detail for event in mutation_records)
    assert all("secret-canary" not in event.action_detail for event in mutation_records)
    assert all(len(event.normalized_action_hash) == 64 for event in mutation_records)


@pytest.mark.asyncio
async def test_admin_mcp_remove_failure_restores_config_and_is_audited(
    mcp_auth_app,
    monkeypatch,
) -> None:
    app, crew = mcp_auth_app

    async def fail_remove(_name: str) -> bool:
        raise RuntimeError(r"C:\\private\\mcp\access_token=remove-secret")

    monkeypatch.setattr(crew.mcp_manager, "remove_server", fail_remove)
    transport = ASGITransport(app=app)
    headers = {"X-Test-Owner": "email:admin@example.com"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/api/mcp/servers/existing",
            headers=headers,
        )

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "MCP 资源回收失败"}
    assert "existing" in crew.config.mcp_servers
    assert "remove-secret" not in response.text
    records = crew.security_audit.query(owner_account_id="email:admin@example.com")
    assert any(
        event.action_type == "mcp_server_admin_action"
        and "action=remove" in event.action_detail
        and "outcome=remove_failed" in event.action_detail
        and event.stable_error_code == "mcp_remove_failed"
        for event in records
    )
