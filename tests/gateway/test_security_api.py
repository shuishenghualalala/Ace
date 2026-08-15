"""Gateway security API keeps approval authority in the Desktop main process."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message, ToolCall
from crew.gateway import auth as gateway_auth
from crew.gateway.auth import REMOTE_AUTH_COOKIE, create_remote_session_token
from crew.gateway.instance_auth import (
    verify_desktop_security_proof as real_verify_desktop_security_proof,
)
from crew.gateway.server import create_app
from crew.gateway.windows_acl import protect_path as protect_windows_path
from crew.security.actions import normalize_file_action
from crew.security.alerts import SecurityAlertKind
from crew.security.runtime_client import RuntimeCapabilities
from crew.state.config import Config

_IDENTITY_A = {"X-Crew-Staff-Code": "A", "X-Crew-Staff-Uid": "uid-a"}
_IDENTITY_B = {"X-Crew-Staff-Code": "B", "X-Crew-Staff-Uid": "uid-b"}
_KEY = bytes.fromhex("42" * 32)


def _write_instance_key(crew_home: Path) -> Path:
    key_dir = crew_home / ".gateway-instance"
    key_dir.mkdir(parents=True, mode=0o700)
    if os.name == "nt":
        protect_windows_path(key_dir, directory=True)
    key_file = key_dir / "gateway-instance.key"
    key_file.write_text(_KEY.hex(), encoding="ascii")
    if os.name == "nt":
        protect_windows_path(key_file, directory=False)
    else:
        key_file.chmod(0o600)
    return key_file


def _proof(method: str, path: str, body: bytes = b"", *, nonce: str | None = None) -> str:
    timestamp = int(time.time())
    nonce = nonce or secrets.token_hex(16)
    message = (
        b"crew-security-desktop-v1\x00"
        + f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode()
    )
    return f"{timestamp}:{nonce}:{hmac.new(_KEY, message, hashlib.sha256).hexdigest()}"


def _headers(method: str, path: str, body: bytes = b"", *, owner="a") -> dict[str, str]:
    identity = _IDENTITY_A if owner == "a" else _IDENTITY_B
    return {**identity, "X-Crew-Security-Proof": _proof(method, path, body)}


def _remote_cookies(user_id: str) -> dict[str, str]:
    return {
        REMOTE_AUTH_COOKIE: create_remote_session_token(
            "test",
            user_id,
            ttl_seconds=3600,
        )
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    crew_home = tmp_path / ".crew"
    _write_instance_key(crew_home)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), plugins_enabled=[]),
        enable_team=False,
    )
    # These API contract tests exercise the Desktop approval round trip. Model
    # that UI explicitly now that production correctly fails closed when no
    # live approval surface is registered.
    crew.security_service._approval_ui_available = lambda: True
    app = create_app(crew)
    app.state.crew = crew
    yield app
    crew.security_rules.close()
    crew.security_audit.close()
    crew.active_owner.close()


async def _post_json(client, path: str, payload: dict, *, owner="a"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return await client.post(
        path,
        content=body,
        headers={**_headers("POST", path, body, owner=owner), "content-type": "application/json"},
    )


async def _put_json(client, path: str, payload: dict, *, owner="a"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return await client.put(
        path,
        content=body,
        headers={**_headers("PUT", path, body, owner=owner), "content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_remote_owner_resource_matrix_is_owner_scoped(api):
    """Two authenticated remote owners must never see each other's stored resources."""
    crew = api.state.crew
    crew.config.auth_mode = "remote"
    crew.config.auth_provider_id = "test"
    owner_a = "test:user-a"
    owner_b = "test:user-b"

    crew.session_store.save("same", [Message.user("hello A")], owner_account_id=owner_a)
    crew.session_store.save("same", [Message.user("hello B")], owner_account_id=owner_b)
    workspace_a = crew.workspace_store.create("workspace A", owner_account_id=owner_a)
    workspace_b = crew.workspace_store.create("workspace B", owner_account_id=owner_b)
    task_a = crew.tasks.create_runtime(
        kind="team",
        session_id="same",
        title="task A",
        owner_account_id=owner_a,
    )
    task_b = crew.tasks.create_runtime(
        kind="team",
        session_id="same",
        title="task B",
        owner_account_id=owner_b,
    )
    cron_a = crew.cron_store.create(
        name="cron A",
        schedule="every 1h",
        query="ping",
        session_id="same",
        owner_account_id=owner_a,
    )
    cron_b = crew.cron_store.create(
        name="cron B",
        schedule="every 1h",
        query="ping",
        session_id="same",
        owner_account_id=owner_b,
    )
    site_a = crew.sites.store.upsert_site(
        owner=owner_a,
        workspace_id="default",
        session_id="same",
        name="site A",
        source_path=".",
        build_command="",
        output_directory="",
    )
    site_b = crew.sites.store.upsert_site(
        owner=owner_b,
        workspace_id="default",
        session_id="same",
        name="site B",
        source_path=".",
        build_command="",
        output_directory="",
    )
    crew._wiki_store.create_kb("kb-a", name="KB A", owner_account_id=owner_a)
    crew._wiki_store.create_kb("kb-b", name="KB B", owner_account_id=owner_b)
    work_a = crew.work_service.create_item(
        owner_account_id=owner_a,
        values={"title": "work A", "workspace_id": "default"},
    )
    work_b = crew.work_service.create_item(
        owner_account_id=owner_b,
        values={"title": "work B", "workspace_id": "default"},
    )
    # Per-owner plugin preferences: the only plugin surface that is account
    # scoped instead of global admin. Seed opposite values so any cross-owner
    # read or write leaks straight into the matrix assertions.
    crew.plugin_prefs.set_enabled(owner_a, "browser", True)
    crew.plugin_prefs.set_enabled(owner_b, "browser", False)

    transport = ASGITransport(app=api, client=("127.0.0.1", 32123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def get_as(user_id: str, path: str):
            return await client.get(
                path,
                cookies=_remote_cookies(user_id),
                headers={"X-Crew-Security-Proof": _proof("GET", path)},
            )

        async def put_as(user_id: str, path: str, payload: dict):
            body = json.dumps(payload, separators=(",", ":")).encode()
            return await client.put(
                path,
                content=body,
                cookies=_remote_cookies(user_id),
                headers={
                    "X-Crew-Security-Proof": _proof("PUT", path, body),
                    "content-type": "application/json",
                },
            )

        sessions_a = await get_as("user-a", "/api/sessions")
        assert [row["session_id"] for row in sessions_a.json()] == ["same"]
        assert (await get_as("user-a", "/api/session/same")).status_code == 200

        workspaces_a = await get_as("user-a", "/api/workspaces")
        assert [row["name"] for row in workspaces_a.json()].count("workspace A") == 1
        assert all(row["name"] != "workspace B" for row in workspaces_a.json())

        tasks_a = await get_as("user-a", "/api/tasks")
        assert [row["title"] for row in tasks_a.json()] == ["task A"]
        assert (await get_as("user-a", f"/api/tasks/{task_b['task_id']}")).status_code == 404

        cron_a_jobs = await get_as("user-a", "/api/cron/jobs")
        assert [row["name"] for row in cron_a_jobs.json()["jobs"]] == ["cron A"]
        assert (await get_as("user-a", f"/api/cron/jobs/{cron_b['id']}")).status_code == 404

        sites_a = await get_as("user-a", "/api/sites")
        assert [row["name"] for row in sites_a.json()["sites"]].count("site A") == 1
        assert all(row["name"] != "site B" for row in sites_a.json()["sites"])
        assert (await get_as("user-a", f"/api/sites/{site_b['id']}")).status_code == 404

        wiki_a = await get_as("user-a", "/api/wiki/kbs")
        wiki_a_ids = {row["id"] for row in wiki_a.json()["kbs"]}
        assert "kb-a" in wiki_a_ids
        assert "kb-b" not in wiki_a_ids

        work_items_a = await get_as("user-a", "/api/work/items")
        assert [row["title"] for row in work_items_a.json()["items"]] == ["work A"]
        assert (await get_as("user-a", f"/api/work/items/{work_b.item_id}")).status_code == 404

        # Differential resource errors: cross-owner and nonexistent lookups must
        # not become an existence oracle. The only permitted public difference
        # is echoing the caller-supplied ID, so compare normalized bodies.
        def _normalized(payload: dict, resource_id: str) -> dict:
            error = str(payload.get("error") or "").replace(resource_id, "<id>")
            return {**payload, "error": error}

        pairs = [
            (
                f"/api/tasks/{task_b['task_id']}",
                task_b["task_id"],
                "/api/tasks/task-does-not-exist",
                "task-does-not-exist",
            ),
            (
                f"/api/cron/jobs/{cron_b['id']}",
                cron_b["id"],
                "/api/cron/jobs/cron-does-not-exist",
                "cron-does-not-exist",
            ),
            (
                f"/api/sites/{site_b['id']}",
                site_b["id"],
                "/api/sites/site-does-not-exist",
                "site-does-not-exist",
            ),
            (
                f"/api/work/items/{work_b.item_id}",
                work_b.item_id,
                "/api/work/items/work-does-not-exist",
                "work-does-not-exist",
            ),
        ]
        for cross_path, cross_id, missing_path, missing_id in pairs:
            cross = await get_as("user-a", cross_path)
            missing = await get_as("user-a", missing_path)
            assert cross.status_code == missing.status_code == 404
            assert cross.json().get("ok") is False
            assert _normalized(cross.json(), cross_id) == _normalized(
                missing.json(),
                missing_id,
            )

        plugins_a = await get_as("user-a", "/api/plugins/states")
        browser_a = next(s for s in plugins_a.json() if s["key"] == "browser")
        # Gateway enforces one active owner lease at a time; releasing the
        # lease between identities tests handler isolation without weakening
        # that separate single-tenant assumption.
        assert crew.active_owner.release(owner_a)
        plugins_b = await get_as("user-b", "/api/plugins/states")
        browser_b = next(s for s in plugins_b.json() if s["key"] == "browser")
        assert browser_a["user_enabled"] is True
        assert browser_b["user_enabled"] is False
        assert crew.active_owner.release(owner_b)

        # Write isolation through the per-owner preference route: toggling one
        # owner's non-browser plugin must never rewrite the other owner's row.
        assert (await put_as("user-a", "/api/plugins/weixin-platform/enabled", {"enabled": False})).status_code == 200
        assert crew.active_owner.release(owner_a)
        assert (await put_as("user-b", "/api/plugins/weixin-platform/enabled", {"enabled": True})).status_code == 200
        assert crew.plugin_prefs.get_enabled(owner_a, "weixin-platform") is False
        assert crew.plugin_prefs.get_enabled(owner_b, "weixin-platform") is True
        assert crew.plugin_prefs.get_enabled(owner_a, "browser") is True
        assert crew.plugin_prefs.get_enabled(owner_b, "browser") is False

        assert crew.active_owner.release(owner_b)

        sessions_b = await get_as("user-b", "/api/sessions")
        assert [row["session_id"] for row in sessions_b.json()] == ["same"]
        assert (await get_as("user-b", "/api/session/same")).status_code == 200

        workspaces_b = await get_as("user-b", "/api/workspaces")
        assert [row["name"] for row in workspaces_b.json()].count("workspace B") == 1
        assert all(row["name"] != "workspace A" for row in workspaces_b.json())

        tasks_b = await get_as("user-b", "/api/tasks")
        assert [row["title"] for row in tasks_b.json()] == ["task B"]
        assert (await get_as("user-b", f"/api/tasks/{task_a['task_id']}")).status_code == 404

        cron_b_jobs = await get_as("user-b", "/api/cron/jobs")
        assert [row["name"] for row in cron_b_jobs.json()["jobs"]] == ["cron B"]
        assert (await get_as("user-b", f"/api/cron/jobs/{cron_a['id']}")).status_code == 404

        sites_b = await get_as("user-b", "/api/sites")
        assert [row["name"] for row in sites_b.json()["sites"]].count("site B") == 1
        assert all(row["name"] != "site A" for row in sites_b.json()["sites"])
        assert (await get_as("user-b", f"/api/sites/{site_a['id']}")).status_code == 404

        wiki_b = await get_as("user-b", "/api/wiki/kbs")
        wiki_b_ids = {row["id"] for row in wiki_b.json()["kbs"]}
        assert "kb-b" in wiki_b_ids
        assert "kb-a" not in wiki_b_ids

        work_items_b = await get_as("user-b", "/api/work/items")
        assert [row["title"] for row in work_items_b.json()["items"]] == ["work B"]
        assert (await get_as("user-b", f"/api/work/items/{work_a.item_id}")).status_code == 404

    assert workspace_a["id"] != workspace_b["id"]


@pytest.mark.asyncio
async def test_fake_execution_requires_main_process_proof_even_in_dev_mode(api, monkeypatch):
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        real_verify_desktop_security_proof,
    )
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/security/fake-executions",
            json={"session_id": "s1", "task_id": "task-a", "argv": ["echo", "safe"]},
            headers=_IDENTITY_A,
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_security_request_consumes_instance_proof_only_once(api, monkeypatch):
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        real_verify_desktop_security_proof,
    )
    path = "/api/security/audit/export"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_multipart_request_proof_binds_exact_wire_body(api, monkeypatch):
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        real_verify_desktop_security_proof,
    )
    path = "/api/wiki/upload"
    boundary = "ace-security-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "approved\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        **_headers("POST", path, body),
        "content-type": f"multipart/form-data; boundary={boundary}",
    }
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(path, content=body, headers=headers)
        rejected = await client.post(
            path,
            content=body.replace(b"approved", b"tampered"),
            headers={
                **_headers("POST", path, body),
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 401


@pytest.mark.asyncio
async def test_gateway_auth_rejects_request_body_over_global_bound(api, monkeypatch):
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        real_verify_desktop_security_proof,
    )
    monkeypatch.setattr(gateway_auth, "_MAX_AUTHENTICATED_REQUEST_BODY_BYTES", 8)
    path = "/api/security/fake-executions"
    body = b'{"too":"large"}'
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            path,
            content=body,
            headers={
                **_headers("POST", path, body),
                "content-type": "application/json",
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_fake_execution_requires_a_task_binding(api):
    path = "/api/security/fake-executions"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _post_json(
            client,
            path,
            {"session_id": "s1", "argv": ["echo", "safe"]},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_gateway_security_headers_cover_authentication_failures(api):
    api.state.crew.config.auth_mode = "remote"
    api.state.crew.config.auth_provider_id = "test"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sessions")

    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_environment_cannot_bypass_native_runtime_mode_gate(
    api,
    monkeypatch,
):
    monkeypatch.setenv("ACE_STRICT_SECURITY", "0")
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _put_json(
            client,
            "/api/security/mode",
            {"workspace_id": "default", "session_id": "s1", "mode": "auto_review"},
        )
    assert response.status_code == 409
    assert "live probe" in response.json()["detail"]


@pytest.mark.asyncio
async def test_strict_auto_review_requires_live_native_runtime(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _put_json(
            client,
            "/api/security/mode",
            {"workspace_id": "default", "session_id": "s1", "mode": "auto_review"},
        )

    assert response.status_code == 409
    assert "live probe" in response.json()["detail"]


@pytest.mark.asyncio
async def test_security_mode_schema_accepts_read_only_and_rejects_unknown_fields(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await _put_json(
            client,
            "/api/security/mode",
            {"workspace_id": "default", "session_id": "s1", "mode": "read_only"},
        )
        rejected = await _put_json(
            client,
            "/api/security/mode",
            {
                "workspace_id": "default",
                "session_id": "s1",
                "mode": "read_only",
                "sandbox": "disabled",
            },
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_full_access_requires_single_use_server_confirmation_nonce(api):
    challenge_path = "/api/security/full-access-challenge"
    mode_path = "/api/security/mode"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await _put_json(
            client,
            mode_path,
            {"workspace_id": "default", "session_id": "s1", "mode": "full_access"},
        )
        challenge = await client.get(
            f"{challenge_path}?workspace_id=default&session_id=s1",
            headers=_headers("GET", challenge_path),
        )
        nonce = challenge.json()["nonce"]
        accepted = await _put_json(
            client,
            mode_path,
            {
                "workspace_id": "default",
                "session_id": "s1",
                "mode": "full_access",
                "confirmation_nonce": nonce,
            },
        )
        replay = await _put_json(
            client,
            mode_path,
            {
                "workspace_id": "default",
                "session_id": "s1",
                "mode": "full_access",
                "confirmation_nonce": nonce,
            },
        )

    assert missing.status_code == 409
    assert challenge.status_code == 200
    assert isinstance(nonce, str) and len(nonce) >= 32
    assert accepted.status_code == 200
    assert replay.status_code == 409


@pytest.mark.asyncio
async def test_security_mode_switch_revokes_session_runtime_authority(api, monkeypatch):
    calls: list[tuple[str, str, str]] = []
    crew = api.state.crew

    monkeypatch.setattr(
        crew.dispatcher,
        "status",
        lambda *_args, **_kwargs: {"live": "idle"},
    )

    async def revoke_runtime_tools(owner: str, session: str) -> None:
        calls.append(("runtime_tools", owner, session))

    monkeypatch.setattr(
        crew.registry,
        "revoke_runtime_tool_session",
        revoke_runtime_tools,
    )

    from crew.tools.process_registry import process_registry

    monkeypatch.setattr(
        process_registry,
        "revoke_session",
        lambda owner, session, *, reason: calls.append((reason, owner, session)) or 1,
    )

    path = "/api/security/mode"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _put_json(
            client,
            path,
            {"workspace_id": "default", "session_id": "s1", "mode": "read_only"},
        )

    assert response.status_code == 200, response.text
    assert ("runtime_tools", "local", "s1") in calls
    assert ("SECURITY_MODE_CHANGED", "local", "s1") in calls


@pytest.mark.asyncio
async def test_fake_approval_round_trip_never_starts_a_process(api, monkeypatch):
    starts: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: starts.append((a, kw)))
    path = "/api/security/fake-executions"
    payload = {"session_id": "s1", "task_id": "task-a", "argv": ["echo", "safe"]}
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _post_json(client, path, payload)
        assert created.status_code == 200, created.text
        request = created.json()
        decision_path = f"/api/security/requests/{request['request_id']}/decision"
        decided = await _post_json(
            client,
            decision_path,
            {
                "session_id": "s1",
                "task_id": "task-a",
                "nonce": request["nonce"],
                "decision": "once",
            },
        )
    assert decided.json()["runtime"] == "fake"
    assert decided.json()["started_process"] is False
    assert starts == []


@pytest.mark.asyncio
async def test_decision_is_bound_to_nonce_and_owner(api):
    api.state.crew.config.auth_mode = "remote"
    api.state.crew.config.auth_provider_id = "test"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with (
        AsyncClient(
            transport=transport, base_url="http://test", cookies=_remote_cookies("uid-a")
        ) as client_a,
        AsyncClient(
            transport=transport, base_url="http://test", cookies=_remote_cookies("uid-b")
        ) as client_b,
    ):
        created = await _post_json(
            client_a,
            "/api/security/fake-executions",
            {"session_id": "s1", "task_id": "task-a", "argv": ["echo", "safe"]},
        )
        request = created.json()
        path = f"/api/security/requests/{request['request_id']}/decision"
        wrong_nonce = await _post_json(
            client_a,
            path,
            {
                "session_id": "s1",
                "task_id": "task-a",
                "nonce": "wrong",
                "decision": "once",
            },
        )
        other_owner = await _post_json(
            client_b,
            path,
            {
                "session_id": "s1",
                "task_id": "task-a",
                "nonce": request["nonce"],
                "decision": "once",
            },
            owner="b",
        )
    assert wrong_nonce.status_code == 409
    assert other_owner.status_code == 423


@pytest.mark.asyncio
async def test_replayed_decision_cannot_grant_twice(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _post_json(
            client,
            "/api/security/fake-executions",
            {"session_id": "s1", "task_id": "task-a", "argv": ["echo", "safe"]},
        )
        request = created.json()
        decision_path = f"/api/security/requests/{request['request_id']}/decision"
        payload = {
            "session_id": "s1",
            "task_id": "task-a",
            "nonce": request["nonce"],
            "decision": "once",
        }
        first = await _post_json(client, decision_path, payload)
        replay = await _post_json(client, decision_path, payload)

        audit_path = "/api/security/audit"
        audit = await client.get(
            audit_path
            + "?action_type=approval_decision&session_id=s1"
            + "&workspace_id=default&task_id=task-a",
            headers=_headers("GET", audit_path),
        )

    assert first.status_code == 200, first.text
    assert replay.status_code == 409, replay.text
    matching = [
        event
        for event in audit.json()["events"]
        if event.get("request_id") == request["request_id"]
        and event.get("decision") == "once"
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_fake_execution_cannot_create_reusable_authority(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for decision in ("session", "always"):
            created = await _post_json(
                client,
                "/api/security/fake-executions",
                {
                    "session_id": "s1",
                    "task_id": "task-a",
                    "argv": ["python", "-m", "pytest"],
                },
            )
            request = created.json()
            path = f"/api/security/requests/{request['request_id']}/decision"
            decided = await _post_json(
                client,
                path,
                {
                    "session_id": "s1",
                    "task_id": "task-a",
                    "nonce": request["nonce"],
                    "decision": decision,
                    **({"always_argv_prefix": ["python", "-m"]} if decision == "always" else {}),
                },
            )
            assert decided.status_code == 409
        rules_path = "/api/security/rules"
        rules = await client.get(rules_path, headers=_headers("GET", rules_path))
        audit_path = "/api/security/audit"
        audit = await client.get(audit_path, headers=_headers("GET", audit_path))
    assert rules.json()["rules"] == []
    assert "rule_created" not in {event["action_type"] for event in audit.json()["events"]}


@pytest.mark.asyncio
async def test_rule_status_audit_export_and_capabilities_stay_behind_desktop_proof(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        real_verify_desktop_security_proof,
    )
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/security/capabilities", headers=_IDENTITY_A)
        capabilities_path = "/api/security/capabilities"
        capabilities = await client.get(
            capabilities_path, headers=_headers("GET", capabilities_path)
        )
        export_path = "/api/security/audit/export"
        exported = await client.get(export_path, headers=_headers("GET", export_path))

    assert denied.status_code == 401
    assert capabilities.status_code == 200
    assert set(capabilities.json()) >= {
        "platform",
        "helper_present",
        "filesystem_sandbox",
        "managed_network",
        "detail",
    }
    assert exported.status_code == 200
    assert isinstance(exported.json()["jsonl"], str)


@pytest.mark.asyncio
async def test_audit_page_returns_owner_scoped_total(api, monkeypatch):
    captured: dict[str, object] = {}

    def query_page(**kwargs):
        captured.update(kwargs)
        return [
            SimpleNamespace(
                event_id="event-1",
                session_id="session-a",
                workspace_id="default",
            )
        ], 42

    monkeypatch.setattr(api.state.crew.security_audit, "query_page", query_page)
    monkeypatch.setattr(
        api.state.crew.session_store,
        "list_sessions",
        lambda **_kwargs: [
            {
                "session_id": "session-a",
                "title": "修复登录问题",
                "workspace_id": "default",
            }
        ],
    )
    path = (
        "/api/security/audit?limit=20&offset=40"
        "&action_type=approval_decision&decision=once&session_id=session-a"
        "&workspace_id=default&task_id=task-a&start_time=100&end_time=200"
        "&sort=oldest"
    )
    proof_path = "/api/security/audit"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", proof_path))

    assert response.status_code == 200
    assert response.json() == {
        "events": [
            {
                "event_id": "event-1",
                "session_id": "session-a",
                "session_title": "修复登录问题",
                "workspace_id": "default",
                "workspace_name": "默认工作空间",
                "workspace_root": "",
                "current_approval_mode": "request_approval",
            }
        ],
        "total": 42,
    }
    assert captured["limit"] == 20
    assert captured["offset"] == 40
    assert captured["action_type"] == "approval_decision"
    assert captured["decision"] == "once"
    assert captured["session_id"] == "session-a"
    assert captured["workspace_id"] == "default"
    assert captured["task_id"] == "task-a"
    assert captured["start_time"] == 100
    assert captured["end_time"] == 200
    assert captured["sort"] == "oldest"
    assert captured["owner_account_id"]


@pytest.mark.asyncio
async def test_audit_purge_is_owner_workspace_scoped_and_query_bounds_fail_closed(
    api,
    monkeypatch,
):
    captured: dict[str, object] = {}

    def purge_expired(**kwargs):
        captured.update(kwargs)
        return 3

    monkeypatch.setattr(api.state.crew.security_audit, "purge_expired", purge_expired)
    monkeypatch.setattr(api.state.crew.security_audit, "record", lambda event: None)
    purge_path = "/api/security/audit/purge-expired"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"{purge_path}?workspace_id=default",
            headers=_headers("POST", purge_path),
        )
        oversized_page = await client.get(
            "/api/security/audit?offset=1000001",
            headers=_headers("GET", "/api/security/audit"),
        )
        oversized_workspace = await client.post(
            f"{purge_path}?workspace_id={'a' * 201}",
            headers=_headers("POST", purge_path),
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": 3}
    assert captured["workspace_id"] == "default"
    assert captured["owner_account_id"]
    assert oversized_page.status_code == 422
    assert oversized_workspace.status_code == 422


@pytest.mark.asyncio
async def test_audit_page_recovers_old_action_detail_from_owner_session_history(
    api,
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "report.txt"
    action = normalize_file_action(target, "read")
    monkeypatch.setattr(
        api.state.crew.security_audit,
        "query_page",
        lambda **_kwargs: (
            [
                SimpleNamespace(
                    event_id="event-old",
                    session_id="session-a",
                    workspace_id="default",
                    task_id="task-a",
                    request_id="request-a",
                    normalized_action_hash=action.digest,
                    tool_name="file_read",
                    action_summary="",
                    action_detail="",
                )
            ],
            1,
        ),
    )
    monkeypatch.setattr(
        api.state.crew.session_store,
        "list_sessions",
        lambda **_kwargs: [
            {"session_id": "session-a", "title": "读取报告", "workspace_id": "default"}
        ],
    )
    monkeypatch.setattr(
        api.state.crew.session_store,
        "load",
        lambda *_args, **_kwargs: [
            Message.assistant(
                tool_calls=[
                    ToolCall(
                        id="wrong", name="file_read", arguments={"path": str(tmp_path / "other")}
                    ),
                    ToolCall(id="match", name="file_read", arguments={"path": str(target)}),
                ]
            )
        ],
    )
    path = "/api/security/audit"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    event = response.json()["events"][0]
    assert event["session_title"] == "读取报告"
    assert event["workspace_name"] == "默认工作空间"
    assert event["action_summary"] == f"读取文件：{target.resolve()}"
    assert event["action_detail"] == f"文件：{target.resolve()}\n操作：读取文件"
    assert "other" not in event["action_detail"]
    assert event["current_approval_mode"] == "request_approval"


@pytest.mark.asyncio
async def test_capabilities_do_not_trust_static_windows_identity(api, tmp_path, monkeypatch):
    state_dir = tmp_path / "security-state"
    state_dir.mkdir()
    (state_dir / "windows-sandbox-identity.json").write_text(
        '{"version":3}',
        encoding="utf-8",
    )
    invalid_helper = tmp_path / "ace-security-runtime.exe"
    invalid_helper.write_text("not an executable", encoding="utf-8")

    monkeypatch.setenv("ACE_SECURITY_STATE_DIR", str(state_dir.resolve()))
    monkeypatch.setattr("crew.gateway.routers.security.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: (str(Path(invalid_helper).resolve()),),
    )
    monkeypatch.setattr("crew.security.launch.runtime_source_stale", lambda *_args: False)

    path = "/api/security/capabilities"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    assert response.json()["filesystem_sandbox"] is False
    assert response.json()["managed_network"] is False


@pytest.mark.asyncio
async def test_capabilities_explain_when_windows_gateway_missed_state_directory(
    api, tmp_path, monkeypatch
):
    helper = tmp_path / "ace-security-runtime.exe"
    helper.write_bytes(b"fake helper")
    monkeypatch.delenv("ACE_SECURITY_STATE_DIR", raising=False)
    monkeypatch.setattr("crew.gateway.routers.security.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: (str(helper),),
    )
    monkeypatch.setattr("crew.security.launch.runtime_source_stale", lambda *_args: False)

    path = "/api/security/capabilities"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    assert response.json()["state_dir_configured"] is False
    assert "Gateway" in response.json()["detail"]
    assert "重启" in response.json()["detail"]


@pytest.mark.asyncio
async def test_capabilities_require_separate_live_filesystem_and_network_probes(
    api,
    tmp_path,
    monkeypatch,
):
    helper = tmp_path / "ace-security-runtime.exe"
    helper.write_bytes(b"fake helper")
    calls: list[bool] = []

    class FakeRuntimeClient:
        def __init__(self, helper_argv):
            assert helper_argv == (str(helper),)

        async def execute(self, **kwargs):
            network_enabled = kwargs["network_enabled"]
            calls.append(network_enabled)
            Path(kwargs["cwd"]).joinpath("probe-marker").write_text("ok", encoding="ascii")
            return SimpleNamespace(
                exit_code=1,
                capabilities=RuntimeCapabilities(
                    backend="windows_sandbox_account",
                    filesystem_sandbox=True,
                    process_tree_cleanup=True,
                    managed_network=network_enabled,
                    explicit_handle_inheritance=True,
                    windows_restricted_token=True,
                    windows_acl=True,
                    windows_job=True,
                    windows_wfp=network_enabled,
                ),
            )

    monkeypatch.setattr("crew.gateway.routers.security.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: (str(helper),),
    )
    monkeypatch.setattr("crew.security.launch.runtime_source_stale", lambda *_args: False)
    monkeypatch.setattr("crew.gateway.routers.security.NativeRuntimeClient", FakeRuntimeClient)

    path = "/api/security/capabilities"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    assert response.json()["filesystem_sandbox"] is True
    assert response.json()["managed_network"] is True
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_capabilities_probe_macos_runtime(api, tmp_path, monkeypatch):
    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"fake helper")
    calls: list[tuple[tuple[str, ...], bool]] = []

    class FakeRuntimeClient:
        def __init__(self, helper_argv):
            assert helper_argv == (str(helper),)

        async def execute(self, **kwargs):
            command = tuple(kwargs["command"])
            network_enabled = kwargs["network_enabled"]
            calls.append((command, network_enabled))
            assert command[:3] == (
                "/bin/sh",
                "-c",
                'printf ok > "$1"; cat "$2" >/dev/null',
            )
            Path(command[4]).write_text("ok", encoding="ascii")
            return SimpleNamespace(
                exit_code=1,
                capabilities=RuntimeCapabilities(
                    backend="macos_seatbelt",
                    filesystem_sandbox=True,
                    process_tree_cleanup=True,
                    managed_network=network_enabled,
                    local_binding_control=True,
                ),
            )

    monkeypatch.setattr("crew.gateway.routers.security.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: (str(helper),),
    )
    monkeypatch.setattr("crew.security.launch.runtime_source_stale", lambda *_args: False)
    monkeypatch.setattr("crew.gateway.routers.security.NativeRuntimeClient", FakeRuntimeClient)

    path = "/api/security/capabilities"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    assert response.json()["platform"] == "darwin"
    assert response.json()["filesystem_sandbox"] is True
    assert response.json()["managed_network"] is True
    assert response.json()["local_binding_control"] is True
    assert [network for _command, network in calls] == [False, True]


@pytest.mark.asyncio
async def test_capabilities_probe_rejects_runtime_startup_failure(api, tmp_path, monkeypatch):
    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"fake helper")

    class FakeRuntimeClient:
        def __init__(self, _helper_argv):
            pass

        async def execute(self, **_kwargs):
            return SimpleNamespace(
                exit_code=71,
                capabilities=RuntimeCapabilities(
                    backend="macos_seatbelt",
                    filesystem_sandbox=True,
                    process_tree_cleanup=True,
                    managed_network=False,
                ),
            )

    monkeypatch.setattr("crew.gateway.routers.security.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: (str(helper),),
    )
    monkeypatch.setattr("crew.security.launch.runtime_source_stale", lambda *_args: False)
    monkeypatch.setattr("crew.gateway.routers.security.NativeRuntimeClient", FakeRuntimeClient)

    path = "/api/security/capabilities"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 200
    assert response.json()["filesystem_sandbox"] is False
    assert response.json()["managed_network"] is False


@pytest.mark.asyncio
async def test_security_alert_stream_and_one_click_isolation(api):
    crew = api.state.crew
    registry = crew.security_alerts
    alert = None
    for _index in range(5):
        alert = registry.record(
            SecurityAlertKind.MANIFEST_MISMATCH,
            "local",
            "session-alert",
        )
    assert alert is not None

    path = "/api/security/alerts"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", path))
        assert response.status_code == 200
        body = response.json()
        assert body["admin"] is True
        assert [item["alert_id"] for item in body["alerts"]] == [alert.alert_id]

        action_path = f"/api/security/alerts/{alert.alert_id}/isolate"
        action = await client.post(
            action_path,
            headers=_headers("POST", action_path, b""),
        )
        assert action.status_code == 200
        assert action.json() == {"changed": True}
    assert registry.get(alert.alert_id).isolated is True


@pytest.mark.asyncio
async def test_security_alert_report_endpoint_aggregates_update_failure(api):
    path = "/api/security/alerts/report"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    created_alert = None
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _index in range(5):
            response = await _post_json(
                client,
                path,
                {
                    "kind": "update_signature_failure",
                    "detail": "SECRET=must-not-leak",
                    "session_id": "",
                    "task_id": "",
                },
            )
            assert response.status_code == 200
            body = response.json()
            if body["created"]:
                created_alert = body["alert"]
    assert created_alert is not None
    assert created_alert["kind"] == "update_signature_failure"
    assert "must-not-leak" not in created_alert["detail"]


def test_proof_one_time_nonce_rejects_replay(tmp_path, monkeypatch):
    """H-19: a verified Desktop proof is consumed; the same proof cannot be replayed
    within its TTL even for an identical request."""
    from crew.gateway.instance_auth import verify_desktop_security_proof

    crew_home = tmp_path / ".crew"
    _write_instance_key(crew_home)
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    body = b'{"mode":"request_approval"}'
    path = "/api/security/mode"
    proof = _proof("POST", path, body)
    # First verification passes and consumes the nonce.
    assert verify_desktop_security_proof(proof, method="POST", path=path, body=body) is True
    # Replay of the exact same proof is refused.
    assert verify_desktop_security_proof(proof, method="POST", path=path, body=body) is False
