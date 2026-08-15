"""Gateway authentication policy."""

from __future__ import annotations

PUBLIC_AUTH_EXEMPT_EXACT = frozenset({
    "/api/health",
    "/api/auth/config",
    "/api/feishu/events",
    "/api/scenarios",
    "/api/scenarios/intro-lines",
    "/api/scenarios/loading-status",
})

# 外部 Runtime/MCP 子进程只能持有一次性短期 interaction binding，不能持有 Desktop 登录身份。
# 这些路由在 interaction_bridge 内自行校验 loopback、binding Owner 与 Active Owner 状态。
INTERNAL_BINDING_AUTH_EXEMPT_EXACT = frozenset({
    "/api/internal/interactions/ask",
    "/api/internal/team/mention",
    "/api/internal/team/plan/create",
    "/api/internal/team/plan/read",
    "/api/internal/team/plan/update",
})

# Login bootstrap is not owner-authenticated yet, but it still must prove that
# the caller is the paired Desktop before credentials leave the main process.
INSTANCE_ONLY_AUTH_EXEMPT_EXACT = frozenset({
    "/api/auth/send-code",
    "/api/auth/login",
})

# Compatibility export: these paths are exempt from owner-session auth, not
# necessarily from paired Desktop instance authentication.
AUTH_EXEMPT_EXACT = (
    PUBLIC_AUTH_EXEMPT_EXACT
    | INTERNAL_BINDING_AUTH_EXEMPT_EXACT
    | INSTANCE_ONLY_AUTH_EXEMPT_EXACT
)


def requires_gateway_auth(path: str) -> bool:
    """Return whether a request path must carry owner-session authentication."""

    return (
        path.startswith("/api/")
        and path not in AUTH_EXEMPT_EXACT
    )


def requires_gateway_instance_auth(path: str) -> bool:
    """Return whether a request must prove the paired Desktop instance."""

    return (
        path.startswith("/api/")
        and path not in PUBLIC_AUTH_EXEMPT_EXACT
        and path not in INTERNAL_BINDING_AUTH_EXEMPT_EXACT
    )
