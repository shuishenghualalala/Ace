"""HTTP client construction with proxy-environment errors made actionable."""

from __future__ import annotations

from typing import Any

import httpx


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build an httpx client, explaining malformed proxy environment values."""
    try:
        return httpx.AsyncClient(**kwargs)
    except httpx.InvalidURL as exc:
        raise RuntimeError(
            "HTTP 客户端初始化失败：请检查 NO_PROXY/HTTP_PROXY/HTTPS_PROXY 等代理环境变量；"
            "IPv6 地址不要写成带方括号的形式"
        ) from exc
