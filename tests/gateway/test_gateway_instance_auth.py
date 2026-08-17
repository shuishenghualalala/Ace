"""Desktop/Gateway instance proof contract tests."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crew.gateway.instance_auth import (
    GATEWAY_INSTANCE_CHALLENGE_HEADER,
    GATEWAY_INSTANCE_DIRECTORY,
    GATEWAY_INSTANCE_KEY_FILENAME,
    configure_gateway_launch_key,
)
from crew.gateway.routers.misc import create_misc_router
from crew.gateway.windows_acl import protect_path as protect_windows_path

PROOF_CONTEXT = b"crew-gateway-instance-v1\x00"


@pytest.fixture
def health_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, Path]:
    crew_home = tmp_path / ".Crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    app = FastAPI()
    app.include_router(create_misc_router(SimpleNamespace()))
    return app, crew_home


def _write_key(crew_home: Path, encoded: bytes = b"11" * 32) -> Path:
    directory = crew_home / GATEWAY_INSTANCE_DIRECTORY
    directory.mkdir(parents=True, mode=0o700)
    if os.name == "nt":
        protect_windows_path(directory, directory=True)
    else:
        directory.chmod(0o700)
    key_file = directory / GATEWAY_INSTANCE_KEY_FILENAME
    key_file.write_bytes(encoded)
    if os.name == "nt":
        protect_windows_path(key_file, directory=False)
    else:
        key_file.chmod(0o600)
    return key_file


@pytest.mark.asyncio
async def test_health_without_challenge_remains_public_readiness(health_app):
    app, _ = health_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["service"] == "crew-gateway"


@pytest.mark.asyncio
async def test_health_reports_missing_windows_security_state(
    health_app,
    monkeypatch: pytest.MonkeyPatch,
):
    from crew.gateway.routers import misc

    helper = health_app[1].parent / "ace-security-runtime.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(misc, "packaged_runtime_argv", lambda: [str(helper)])
    monkeypatch.setattr(misc.platform, "system", lambda: "Windows")
    monkeypatch.delenv("ACE_SECURITY_STATE_DIR", raising=False)

    async with AsyncClient(transport=ASGITransport(app=health_app[0]), base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.json()["components"]["security_state"]["status"] == "failed"

    monkeypatch.setenv("ACE_SECURITY_STATE_DIR", str(health_app[1] / "security"))
    async with AsyncClient(transport=ASGITransport(app=health_app[0]), base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.json()["components"]["security_state"]["status"] == "ready"


@pytest.mark.asyncio
async def test_health_returns_domain_separated_instance_proof(health_app):
    app, crew_home = health_app
    encoded_key = b"23" * 32
    _write_key(crew_home, encoded_key)
    challenge = "ab" * 32
    expected = hmac.new(
        bytes.fromhex(encoded_key.decode("ascii")),
        PROOF_CONTEXT + challenge.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/health",
            headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: challenge},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["service"] == "crew-gateway"
    assert response.json()["instance_proof"] == expected


@pytest.mark.asyncio
async def test_managed_launch_key_supersedes_same_user_persistent_key(health_app):
    app, crew_home = health_app
    _write_key(crew_home, b"23" * 32)
    launch_key = bytes.fromhex("42" * 32)
    challenge = "ac" * 32
    expected = hmac.new(
        launch_key,
        PROOF_CONTEXT + challenge.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    persistent_proof = hmac.new(
        bytes.fromhex("23" * 32),
        PROOF_CONTEXT + challenge.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    configure_gateway_launch_key(launch_key)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/health",
                headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: challenge},
            )
    finally:
        configure_gateway_launch_key(None)

    assert response.json()["instance_proof"] == expected
    assert response.json()["instance_proof"] != persistent_proof


@pytest.mark.asyncio
async def test_health_challenge_fails_closed_without_secure_key(health_app):
    app, _ = health_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        malformed = await client.get(
            "/api/health",
            headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: "not-a-32-byte-hex-challenge"},
        )
        missing = await client.get(
            "/api/health",
            headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: "cd" * 32},
        )

    assert malformed.status_code == 401
    assert missing.status_code == 401
    assert malformed.json() == missing.json() == {
        "ok": False,
        "error": "gateway instance verification failed",
    }
    assert malformed.headers["content-length"] == missing.headers["content-length"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and symlink contract")
@pytest.mark.asyncio
async def test_health_rejects_wide_permissions_and_symlink_key(health_app):
    app, crew_home = health_app
    key_file = _write_key(crew_home)
    challenge_headers = {GATEWAY_INSTANCE_CHALLENGE_HEADER: "ef" * 32}

    key_file.chmod(0o644)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        wide_file = await client.get("/api/health", headers=challenge_headers)
    assert wide_file.status_code == 401

    key_file.unlink()
    target = crew_home / "not-the-instance-key"
    target.write_bytes(b"11" * 32)
    target.chmod(0o600)
    key_file.symlink_to(target)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        symlink = await client.get("/api/health", headers=challenge_headers)
    assert symlink.status_code == 401

    key_file.unlink()
    key_file.write_bytes(b"11" * 32)
    key_file.chmod(0o600)
    key_file.parent.chmod(0o755)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        wide_parent = await client.get("/api/health", headers=challenge_headers)
    assert wide_parent.status_code == 401
