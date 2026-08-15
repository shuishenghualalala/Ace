"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


class _TestCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


@pytest.fixture(autouse=True)
def _isolated_platform_secret_backend(monkeypatch) -> _TestCredentialBackend:
    """Never let tests write developer credentials to the host OS keyring."""
    from crew.security.secret_store import PlatformSecretStore

    backend = _TestCredentialBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    return backend


@pytest.fixture(autouse=True)
def _gateway_transport_boundary_test_defaults(monkeypatch) -> None:
    """Keep domain tests focused; boundary tests replace these stubs explicitly."""

    from crew.gateway import app as gateway_app
    from crew.gateway import auth as gateway_auth

    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        lambda _proof, **_kwargs: True,
    )
    monkeypatch.setattr(
        gateway_auth,
        "require_trusted_request_origin",
        lambda _origin, _config=None: None,
    )
    monkeypatch.setattr(
        gateway_app,
        "require_trusted_request_origin",
        lambda _origin, _config=None: None,
    )


@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """Use the historical test owner without sending identity headers."""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


@pytest.fixture
def send_ws_json() -> Callable[[Any, dict[str, Any]], None]:
    """Send one valid, strictly sequenced Gateway WebSocket test frame."""

    sequences: dict[Any, int] = {}
    nonce_counter = 0

    def send(socket: Any, payload: dict[str, Any]) -> None:
        nonlocal nonce_counter
        sequence = sequences.get(socket, 0) + 1
        sequences[socket] = sequence
        nonce_counter += 1
        socket.send_json(
            {
                **payload,
                "protocol_version": 1,
                "client_sequence": sequence,
                "nonce": f"test-nonce-{nonce_counter:016d}",
            }
        )

    return send
