"""Process-wide security compatibility preference propagated by the desktop app."""

from __future__ import annotations

import os


def strict_security_enabled() -> bool:
    """Return True unless compatibility mode was explicitly enabled."""
    return os.getenv("ACE_STRICT_SECURITY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def websocket_transport_block_reason(url: str) -> str | None:
    """Block credential-bearing plaintext WebSockets only in strict mode."""
    if strict_security_enabled() and str(url).strip().lower().startswith("ws://"):
        return "严格安全约束要求渠道使用 wss://；可关闭严格约束后启用兼容模式"
    return None
