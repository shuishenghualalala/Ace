from crew.security.settings import (
    configure_security,
    strict_security_enabled,
    websocket_transport_block_reason,
)


def test_security_defaults_disabled_and_allows_plaintext_websocket():
    configure_security(enabled=False)

    assert strict_security_enabled() is False
    assert websocket_transport_block_reason("ws://channel.example/socket") is None
    assert websocket_transport_block_reason("wss://channel.example/socket") is None


def test_configured_security_blocks_plaintext_websocket():
    configure_security(enabled=True)

    assert strict_security_enabled() is True
    assert websocket_transport_block_reason("ws://channel.example/socket")


def test_disabled_security_allows_plaintext_websocket():
    configure_security(enabled=False)

    assert strict_security_enabled() is False
    assert websocket_transport_block_reason("ws://legacy.example/socket") is None
