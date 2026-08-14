"""Process-wide security settings loaded from the application config."""

from __future__ import annotations

_configured_security_enabled = False


def configure_security(*, enabled: bool) -> None:
    """Publish the trusted config value for low-level callers."""
    global _configured_security_enabled
    _configured_security_enabled = bool(enabled)


def strict_security_enabled() -> bool:
    """Return the trusted application-config security state."""
    return _configured_security_enabled


def websocket_transport_block_reason(url: str) -> str | None:
    """Block credential-bearing plaintext WebSockets only in strict mode."""
    if strict_security_enabled() and str(url).strip().lower().startswith("ws://"):
        return "严格安全约束要求渠道使用 wss://；可关闭严格约束后启用兼容模式"
    return None
