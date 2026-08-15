from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from crew.gateway.auth import AccountContext
from crew.gateway.routers import mcp_setup
from crew.security.audit import SQLiteSecurityAudit
from crew.security.launch import validate_process_launch
from crew.security.models import PermissionProfileKind, SandboxablePreference


class _SetupService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.task = SimpleNamespace(
            task_id="task-safe",
            platform="windows",
            status="running",
            started_at=1.0,
            finished_at=None,
            steps=[],
            log=[],
            error=None,
        )

    async def status(self, _registry):
        self.calls.append("status")
        return {"ok": True}

    def start_setup(self, *, process_launch, **_kwargs):
        validate_process_launch(process_launch)
        assert process_launch.profile.kind is PermissionProfileKind.DISABLED
        assert process_launch.sandbox_preference is SandboxablePreference.FORBID
        assert process_launch.sandboxed is False
        assert process_launch.sandbox_system_surface == "cua-driver-admin"
        self.calls.append("setup")
        return self.task

    def get_task(self, _task_id: str):
        self.calls.append("task_status")
        return self.task

    async def cancel_task(self, _task_id: str) -> bool:
        self.calls.append("cancel")
        return True


@pytest.fixture
def cua_auth_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _SetupService()
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    config = SimpleNamespace(
        gateway_admin_accounts=["email:admin@example.com"],
    )
    crew = SimpleNamespace(
        config=config,
        registry=SimpleNamespace(),
        security_audit=audit,
    )
    monkeypatch.setattr(mcp_setup, "_cua_setup_service", service)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated_account(request: Request, call_next):
        request.state.account = AccountContext(
            owner_account_id=request.headers.get(
                "X-Test-Owner",
                "email:user@example.com",
            )
        )
        return await call_next(request)

    app.include_router(mcp_setup.create_mcp_setup_router(crew))
    try:
        yield app, crew, service
    finally:
        audit.close()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/mcp/cua-driver/status", None),
        ("POST", "/api/mcp/cua-driver/setup", {}),
        ("GET", "/api/mcp/cua-driver/setup/task-safe", None),
        ("POST", "/api/mcp/cua-driver/setup/task-safe/cancel", None),
    ],
)
@pytest.mark.asyncio
async def test_non_admin_cannot_access_global_cua_setup_surface(
    cua_auth_app,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    app, crew, service = cua_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            json=payload,
            headers={"X-Test-Owner": "email:user@example.com"},
        )

    assert response.status_code == 403
    assert service.calls == []
    records = crew.security_audit.query(
        owner_account_id="email:user@example.com"
    )
    assert len(records) == 1
    assert records[0].action_type == "cua_setup_admin_denied"
    assert records[0].stable_error_code == "gateway_admin_required"


@pytest.mark.asyncio
async def test_configured_admin_receives_explicit_cua_process_authority(
    cua_auth_app,
) -> None:
    app, _crew, service = cua_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mcp/cua-driver/setup",
            json={"start_daemon": False},
            headers={"X-Test-Owner": "email:admin@example.com"},
        )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-safe"
    assert service.calls == ["setup"]
    records = _crew.security_audit.query(
        owner_account_id="email:admin@example.com"
    )
    assert len(records) == 1
    assert records[0].action_type == "cua_setup_admin_action"
    assert records[0].decision == "allow"


@pytest.mark.asyncio
async def test_cua_mutation_fails_closed_when_audit_is_unavailable(
    cua_auth_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, crew, service = cua_auth_app
    monkeypatch.setattr(
        crew.security_audit,
        "record",
        lambda _event: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mcp/cua-driver/setup",
            json={},
            headers={"X-Test-Owner": "email:admin@example.com"},
        )

    assert response.status_code == 503
    assert service.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"start_daemon": "false"},
        {"force_reinstall": 1},
        {"unknown": True},
    ],
)
@pytest.mark.asyncio
async def test_cua_setup_rejects_ambiguous_or_unknown_fields(
    cua_auth_app,
    payload: dict,
) -> None:
    app, _crew, service = cua_auth_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mcp/cua-driver/setup",
            json=payload,
            headers={"X-Test-Owner": "email:admin@example.com"},
        )

    assert response.status_code == 400
    assert service.calls == []
