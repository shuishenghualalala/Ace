"""HTTP client construction with proxy-environment errors made actionable."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

_BRACKETED_LOOPBACK_RE = re.compile(r"\[(::1|::)\](?::\d+)?")


def _normalize_no_proxy_environment() -> None:
    """httpx treats bracketed IPv6 loopback as a port; preserve the bypass intent."""
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if value and _BRACKETED_LOOPBACK_RE.search(value):
            os.environ[key] = _BRACKETED_LOOPBACK_RE.sub(r"\1", value)


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
