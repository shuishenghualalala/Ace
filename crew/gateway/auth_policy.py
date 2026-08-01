"""Gateway authentication policy."""

from __future__ import annotations

AUTH_EXEMPT_EXACT = frozenset({
    "/api/health",
    "/api/auth/config",
    "/api/auth/send-code",
    "/api/auth/login",
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


def requires_gateway_auth(path: str) -> bool:
    """Return whether a request path must carry gateway auth."""

    return (
        path.startswith("/api/")
        and path not in AUTH_EXEMPT_EXACT
        and path not in INTERNAL_BINDING_AUTH_EXEMPT_EXACT
    )
