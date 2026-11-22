"""HTTP client construction with proxy-environment errors made actionable."""

from __future__ import annotations

import os
from typing import Any

import httpx


def _normalize_no_proxy_environment() -> None:
    """httpx treats bracketed IPv6 loopback as a port; preserve the bypass intent."""
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if value and ("[::1]" in value or "[::]" in value):
            os.environ[key] = value.replace("[::1]", "::1").replace("[::]", "::")


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build an httpx client, explaining malformed proxy environment values."""
    _normalize_no_proxy_environment()
    try:
        return httpx.AsyncClient(**kwargs)
    except httpx.InvalidURL as exc:
        raise RuntimeError(
            "HTTP 客户端初始化失败：请检查 NO_PROXY/HTTP_PROXY/HTTPS_PROXY 等代理环境变量；"
            "IPv6 地址不要写成带方括号的形式"
        ) from exc
