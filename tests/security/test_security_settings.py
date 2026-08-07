from crew.security.settings import (
    strict_security_enabled,
    websocket_transport_block_reason,
)


def test_security_defaults_strict_and_blocks_plaintext_websocket(monkeypatch):
    monkeypatch.delenv("ACE_STRICT_SECURITY", raising=False)

    assert strict_security_enabled() is True
    assert websocket_transport_block_reason("ws://channel.example/socket")
    assert websocket_transport_block_reason("wss://channel.example/socket") is None


def test_compatibility_mode_allows_plaintext_websocket(monkeypatch):
    monkeypatch.setenv("ACE_STRICT_SECURITY", "0")

    assert strict_security_enabled() is False
    assert websocket_transport_block_reason("ws://legacy.example/socket") is None
