"""Route Registry ownership, gateway assembly and runtime gate behavior."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

from crew.features import (
    FeatureGeneration,
    FeatureScope,
    RouteConflictError,
    RouteContribution,
    RouteRegistry,
    RouteUnavailableError,
)
from crew.gateway.app import make_route_gate


def _active_scope(feature_id: str, sequence: int = 1) -> FeatureScope:
    scope = FeatureScope(FeatureGeneration(feature_id, sequence))
    scope.activate()
    return scope


def _router(path: str = "/status") -> APIRouter:
    router = APIRouter()

    @router.get(path)
    def status() -> dict:
        return {"ok": True}

    return router


def test_contribution_validates_identity_and_prefix() -> None:
    contribution = RouteContribution(" plugin.browser ", _router(), prefix="/api/plugins/browser/")
    assert contribution.contribution_id == "plugin.browser"
    assert contribution.prefix == "/api/plugins/browser"

    with pytest.raises(ValueError, match="must not be empty"):
        RouteContribution(" ", _router())
    with pytest.raises(ValueError, match="whitespace"):
        RouteContribution("plugin browser", _router())
    with pytest.raises(ValueError, match="start with '/'"):
        RouteContribution("plugin.browser", _router(), prefix="api")
    with pytest.raises(TypeError, match="must not be None"):
        RouteContribution("plugin.browser", None)


def test_registry_rejects_unrelated_contribution_owner() -> None:
    registry = RouteRegistry()
    registry.register(
        _active_scope("first"), RouteContribution("plugin.browser", _router())
    )

    with pytest.raises(RouteConflictError, match="first@g1"):
        registry.register(
            _active_scope("second"), RouteContribution("plugin.browser", _router())
        )


async def test_new_generation_stages_then_replaces_contribution_atomically() -> None:
    registry = RouteRegistry()
    old_scope = _active_scope("browser", 1)
    registry.register(old_scope, RouteContribution("plugin.browser", _router()))

    new_scope = FeatureScope(FeatureGeneration("browser", 2))
    registry.register(new_scope, RouteContribution("plugin.browser", _router()))

    assert registry.resolve("plugin.browser").generation.key == "browser@g1"
    new_scope.activate()
    assert registry.resolve("plugin.browser").generation.key == "browser@g2"

    await old_scope.dispose()
    assert registry.resolve("plugin.browser").generation.key == "browser@g2"


async def test_route_acquire_lease_binds_the_selected_generation() -> None:
    registry = RouteRegistry()
    old_scope = _active_scope("browser", 1)
    registry.register(old_scope, RouteContribution("plugin.browser", _router()))

    new_scope = FeatureScope(FeatureGeneration("browser", 2))
    registry.register(new_scope, RouteContribution("plugin.browser", _router()))

    old_binding, old_lease = registry.acquire_lease("plugin.browser", label="old")
    assert old_binding.generation.key == "browser@g1"

    new_scope.activate()
    new_binding, new_lease = registry.acquire_lease("plugin.browser", label="new")
    assert new_binding.generation.key == "browser@g2"

    # The old request keeps its exact generation even after replacement becomes
    # visible; releasing it must not affect the new generation's lease.
    assert old_binding.generation.key != new_binding.generation.key
    old_lease.release()
    new_lease.release()
    await old_scope.dispose()
    await new_scope.dispose()


async def test_scope_release_makes_contribution_unavailable() -> None:
    registry = RouteRegistry()
    scope = _active_scope("browser")
    registry.register(scope, RouteContribution("plugin.browser", _router()))

    assert registry.is_available("plugin.browser") is True
    assert registry.contribution_ids() == ("plugin.browser",)

    await scope.dispose()
    assert registry.is_available("plugin.browser") is False
    assert registry.contribution_ids() == ()
    with pytest.raises(RouteUnavailableError, match="plugin.browser"):
        registry.resolve("plugin.browser")


def test_bindings_follow_deterministic_order() -> None:
    registry = RouteRegistry()
    registry.register(_active_scope("b"), RouteContribution("plugin.b", _router()))
    registry.register(_active_scope("a"), RouteContribution("plugin.a", _router()))

    assert [b.contribution.contribution_id for b in registry.bindings()] == [
        "plugin.a",
        "plugin.b",
    ]


def _gated_app(registry: RouteRegistry, contribution_id: str, router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(
        router,
        prefix="/api/plugins/browser",
        dependencies=[Depends(make_route_gate(registry, contribution_id))],
    )
    return app


def test_gate_passes_while_contribution_active() -> None:
    registry = RouteRegistry()
    registry.register(
        _active_scope("browser"), RouteContribution("plugin.browser", _router())
    )
    client = TestClient(_gated_app(registry, "plugin.browser", _router()))

    assert client.get("/api/plugins/browser/status").json() == {"ok": True}


async def test_gate_returns_capability_unavailable_after_scope_release() -> None:
    registry = RouteRegistry()
    scope = _active_scope("browser")
    registry.register(scope, RouteContribution("plugin.browser", _router()))
    client = TestClient(_gated_app(registry, "plugin.browser", _router()))

    assert client.get("/api/plugins/browser/status").status_code == 200

    await scope.dispose()
    response = client.get("/api/plugins/browser/status")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["code"] == "capability_unavailable"


@pytest.mark.asyncio
async def test_route_gate_drains_in_flight_request_before_scope_cleanup() -> None:
    registry = RouteRegistry()
    scope = _active_scope("browser")
    started = asyncio.Event()
    release = asyncio.Event()
    closed = 0

    async def handler(request: Request) -> dict[str, bool]:
        nonlocal closed
        started.set()
        await release.wait()
        return {"ok": True}

    router = APIRouter()

    @router.get("/status")
    async def status(request: Request):
        return await handler(request)

    def dispose() -> None:
        nonlocal closed
        closed += 1

    scope.register(dispose, label="resource:route-owner")
    registry.register(scope, RouteContribution("plugin.browser", router))
    app = _gated_app(registry, "plugin.browser", router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/api/plugins/browser/status"))
        await started.wait()
        scope.begin_draining()
        assert registry.acquire_lease("plugin.browser", label="late") is None
        stopping = asyncio.create_task(scope.stop(timeout_seconds=None))
        await asyncio.sleep(0)
        assert not stopping.done()
        assert closed == 0

        release.set()
        response = await request
        await stopping

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert closed == 1


async def test_legacy_plugin_router_lands_in_route_registry(tmp_path):
    """旧 register(ctx) 目录插件的 API Router 经桥接进入 Route Registry，卸载即摘除。"""
    from crew.plugins.manager import PluginManager
    from crew.tools.registry import Registry

    plugin_dir = tmp_path / "route_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: route-plugin\nkind: standalone\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(
        """
from fastapi import APIRouter


def register(ctx):
    router = APIRouter()

    @router.get("/status")
    def status():
        return {"ok": True}

    ctx.register_api_router(router)
""",
        encoding="utf-8",
    )

    plugins = PluginManager(registry=Registry())
    plugins.discover_and_load([tmp_path], enabled=["route-plugin"])

    routes = plugins.feature_runtime.routes
    assert routes.is_available("plugin:route-plugin")
    binding = routes.resolve("plugin:route-plugin")
    assert binding.contribution.prefix == "/api/plugins/route-plugin"

    assert await plugins.unload_plugin_async("route-plugin") is True
    assert not routes.is_available("plugin:route-plugin")
