"""Work route consumer lifecycle and compatibility contracts."""

import ast
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crew.features import FeatureGeneration, FeatureScope, ServiceRegistry
from crew.gateway.auth import AccountContext
from crew.gateway.routers.work import create_work_router as legacy_create_work_router
from crew.work.routes import create_work_router
from crew.work.service import WORK_SERVICE_KEY


class _Plugins:
    def __init__(self, registry: ServiceRegistry):
        self.registry = registry

    def acquire_service_lease(self, key, *, label="service-request", generation=None):
        return self.registry.acquire_lease(key, label=label, generation=generation)

    def resolve_service(self, key, default=None):
        return self.registry.get(key, default=default)


class _HistoryService:
    def __init__(self, marker: str):
        self.marker = marker

    def history(self, owner_account_id: str, *, include_archived: bool = False):
        return [{"marker": self.marker, "owner": owner_account_id}]


def _app(crew):
    app = FastAPI()

    @app.middleware("http")
    async def attach_account(request, call_next):
        request.state.account = AccountContext("A:work-owner", is_local=True)
        return await call_next(request)

    app.include_router(create_work_router(crew))
    return app


@pytest.mark.asyncio
async def test_work_route_is_unavailable_when_its_generation_drains():
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.work", 1))
    registry.register(scope, WORK_SERVICE_KEY, _HistoryService("old"))
    scope.activate()
    scope.begin_draining()
    crew = SimpleNamespace(plugins=_Plugins(registry), work_service=None)

    async with AsyncClient(transport=ASGITransport(app=_app(crew)), base_url="http://test") as client:
        response = await client.get("/api/work/history")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "Work 未启用"}
    await scope.dispose()


@pytest.mark.asyncio
async def test_work_route_selects_newest_active_generation():
    registry = ServiceRegistry()
    old_scope = FeatureScope(FeatureGeneration("product.work", 1))
    new_scope = FeatureScope(FeatureGeneration("product.work", 2))
    registry.register(old_scope, WORK_SERVICE_KEY, _HistoryService("old"))
    registry.register(new_scope, WORK_SERVICE_KEY, _HistoryService("new"))
    old_scope.activate()
    new_scope.activate()
    crew = SimpleNamespace(plugins=_Plugins(registry), work_service=None)

    async with AsyncClient(transport=ASGITransport(app=_app(crew)), base_url="http://test") as client:
        response = await client.get("/api/work/history")

    assert response.status_code == 200
    assert response.json()["entries"][0]["marker"] == "new"
    await old_scope.dispose()
    await new_scope.dispose()


@pytest.mark.asyncio
async def test_work_route_binding_fails_closed_instead_of_drifting_generation():
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.work", 1))
    registry.register(scope, WORK_SERVICE_KEY, _HistoryService("active"))
    scope.activate()
    crew = SimpleNamespace(plugins=_Plugins(registry), work_service=None)
    app = _app(crew)

    @app.middleware("http")
    async def bind_missing_generation(request, call_next):
        request.state.route_binding = SimpleNamespace(
            generation=FeatureGeneration("product.work", 99)
        )
        return await call_next(request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/work/history")

    assert response.status_code == 503
    await scope.dispose()


def test_legacy_work_router_is_only_a_compatibility_import():
    assert legacy_create_work_router is create_work_router
    source = ast.parse(Path("crew/work/routes.py").read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(source)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "crew.gateway.auth" not in imports
    assert "crew.gateway" not in imports
