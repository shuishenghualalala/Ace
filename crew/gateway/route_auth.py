"""Explicit authentication responsibility for every mounted Gateway route."""

from __future__ import annotations

from enum import StrEnum
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute

from crew.gateway.auth_policy import (
    INSTANCE_ONLY_AUTH_EXEMPT_EXACT,
    INTERNAL_BINDING_AUTH_EXEMPT_EXACT,
    PUBLIC_AUTH_EXEMPT_EXACT,
)


class RouteAuthResponsibility(StrEnum):
    """The boundary that owns authentication/authorization for a route."""

    INSTANCE_CHALLENGE = "public-instance-challenge"
    PUBLIC_CONTENT = "public-content"
    WEBHOOK_SIGNATURE = "public-webhook-signature"
    INTERNAL_BINDING = "short-lived-internal-binding"
    DESKTOP_LOGIN_BOOTSTRAP = "desktop-instance-login-bootstrap"
    DESKTOP_OWNER = "desktop-instance-and-owner"
    DESKTOP_ADMIN = "desktop-instance-and-admin"
    OWNER_RESOURCE_GUARD = "desktop-owner-resource-guard"


_PUBLIC_RESPONSIBILITIES = {
    ("GET", "/api/health"): RouteAuthResponsibility.INSTANCE_CHALLENGE,
    ("GET", "/api/auth/config"): RouteAuthResponsibility.PUBLIC_CONTENT,
    ("POST", "/api/feishu/events"): RouteAuthResponsibility.WEBHOOK_SIGNATURE,
    ("GET", "/api/scenarios"): RouteAuthResponsibility.PUBLIC_CONTENT,
    ("GET", "/api/scenarios/intro-lines"): RouteAuthResponsibility.PUBLIC_CONTENT,
    ("GET", "/api/scenarios/loading-status"): RouteAuthResponsibility.PUBLIC_CONTENT,
}
_PUBLIC_RESPONSIBILITY_PATHS = frozenset(path for _method, path in _PUBLIC_RESPONSIBILITIES)
if _PUBLIC_RESPONSIBILITY_PATHS != PUBLIC_AUTH_EXEMPT_EXACT:
    raise RuntimeError("public Gateway auth exemptions require exact route responsibilities")

_INTERNAL_BINDING_RESPONSIBILITIES = {
    ("POST", path): RouteAuthResponsibility.INTERNAL_BINDING
    for path in INTERNAL_BINDING_AUTH_EXEMPT_EXACT
}
_INSTANCE_ONLY_RESPONSIBILITIES = {
    ("POST", path): RouteAuthResponsibility.DESKTOP_LOGIN_BOOTSTRAP
    for path in INSTANCE_ONLY_AUTH_EXEMPT_EXACT
}

_ADMIN_ROUTES = frozenset({
    ("POST", "/api/runtimes/scan"),
    ("POST", "/api/runtimes/register"),
    ("POST", "/api/mcp/servers"),
    ("PUT", "/api/mcp/servers/{name}"),
    ("DELETE", "/api/mcp/servers/{name}"),
    ("POST", "/api/mcp/servers/{name}/reload"),
    ("GET", "/api/mcp/cua-driver/status"),
    ("POST", "/api/mcp/cua-driver/setup"),
    ("GET", "/api/mcp/cua-driver/setup/{task_id}"),
    ("POST", "/api/mcp/cua-driver/setup/{task_id}/cancel"),
    ("POST", "/api/plugins/install"),
    ("PUT", "/api/plugins/{plugin_key}/system-enabled"),
    ("DELETE", "/api/plugins/{plugin_key}"),
    ("PUT", "/api/skills/evolution"),
    ("POST", "/api/skills/{slug}/install"),
    ("DELETE", "/api/skills/{slug}"),
    ("DELETE", "/api/system/logs"),
})

_RESOURCE_GUARDED_ROUTES = frozenset({
    ("PUT", "/api/config/models/{model_id}"),
    ("DELETE", "/api/config/models/{model_id}"),
    ("GET", "/api/system/logs"),
})


def route_auth_responsibility(
    method: str,
    path: str,
    *,
    default: RouteAuthResponsibility = RouteAuthResponsibility.DESKTOP_OWNER,
) -> RouteAuthResponsibility:
    """Resolve one route from explicit exceptions and its router default."""

    key = (method.upper(), path)
    explicit = _PUBLIC_RESPONSIBILITIES.get(key)
    if explicit is not None:
        return explicit
    if path in PUBLIC_AUTH_EXEMPT_EXACT:
        raise RuntimeError(
            f"public Gateway path lacks a method-specific auth responsibility: {key!r}"
        )
    explicit = _INTERNAL_BINDING_RESPONSIBILITIES.get(key)
    if explicit is not None:
        return explicit
    if path in INTERNAL_BINDING_AUTH_EXEMPT_EXACT:
        raise RuntimeError(
            f"internal binding path lacks a method-specific auth responsibility: {key!r}"
        )
    explicit = _INSTANCE_ONLY_RESPONSIBILITIES.get(key)
    if explicit is not None:
        return explicit
    if path in INSTANCE_ONLY_AUTH_EXEMPT_EXACT:
        raise RuntimeError(
            f"login bootstrap path lacks a method-specific auth responsibility: {key!r}"
        )
    if key in _ADMIN_ROUTES:
        return RouteAuthResponsibility.DESKTOP_ADMIN
    if key in _RESOURCE_GUARDED_ROUTES:
        return RouteAuthResponsibility.OWNER_RESOURCE_GUARD
    return default


def include_router_with_auth(
    app: FastAPI,
    router: APIRouter,
    *,
    prefix: str = "",
    default: RouteAuthResponsibility = RouteAuthResponsibility.DESKTOP_OWNER,
) -> None:
    """Annotate a router and mount it with an explicit inherited responsibility."""

    for route in router.routes:
        if isinstance(route, APIRoute):
            mounted_path = f"{prefix}{route.path}"
            responsibilities = {
                method: route_auth_responsibility(
                    method,
                    mounted_path,
                    default=default,
                )
                for method in sorted(route.methods)
            }
        elif isinstance(route, APIWebSocketRoute):
            mounted_path = f"{prefix}{route.path}"
            responsibilities = {
                "WS": route_auth_responsibility(
                    "WS",
                    mounted_path,
                    default=default,
                )
            }
        else:
            continue
        setattr(route, "gateway_mounted_path", mounted_path)
        setattr(route, "gateway_auth_responsibilities", responsibilities)
    app.include_router(router, prefix=prefix)


def route_responsibilities(route: Any) -> dict[str, RouteAuthResponsibility]:
    value = getattr(route, "gateway_auth_responsibilities", None)
    return dict(value) if isinstance(value, dict) else {}


def iter_gateway_routes(app: FastAPI) -> Iterator[tuple[str, Any]]:
    """Yield mounted API/WS routes across FastAPI's nested router wrappers."""

    def walk(routes: list[Any], inherited_prefix: str = "") -> Iterator[tuple[str, Any]]:
        for route in routes:
            if isinstance(route, (APIRoute, APIWebSocketRoute)):
                mounted = str(
                    getattr(route, "gateway_mounted_path", "")
                    or f"{inherited_prefix}{route.path}"
                )
                if mounted.startswith("/api/") or mounted.startswith("/ws"):
                    yield mounted, route
                continue
            original = getattr(route, "original_router", None)
            nested_routes = getattr(original, "routes", None)
            if isinstance(nested_routes, list):
                context = getattr(route, "include_context", None)
                nested_prefix = f"{inherited_prefix}{getattr(context, 'prefix', '')}"
                yield from walk(nested_routes, nested_prefix)

    yield from walk(app.routes)


def declared_public_paths() -> frozenset[str]:
    """Expose the exact exception closure for route-table tests."""

    return (
        PUBLIC_AUTH_EXEMPT_EXACT
        | INTERNAL_BINDING_AUTH_EXEMPT_EXACT
        | INSTANCE_ONLY_AUTH_EXEMPT_EXACT
    )


def declared_admin_routes() -> frozenset[tuple[str, str]]:
    """Expose the complete global/admin route contract for negative tests."""

    return _ADMIN_ROUTES


__all__ = [
    "RouteAuthResponsibility",
    "declared_admin_routes",
    "declared_public_paths",
    "include_router_with_auth",
    "iter_gateway_routes",
    "route_auth_responsibility",
    "route_responsibilities",
]
