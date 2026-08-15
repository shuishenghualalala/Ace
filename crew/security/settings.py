"""Process-wide production security invariants."""

from __future__ import annotations


def strict_security_enabled() -> bool:
    """Security controls are mandatory and cannot be disabled by environment."""
    return True


def websocket_transport_block_reason(url: str) -> str | None:
    """Block credential-bearing plaintext WebSockets."""
    if str(url).strip().lower().startswith("ws://"):
        return "安全约束要求渠道使用 wss://"
    return None
