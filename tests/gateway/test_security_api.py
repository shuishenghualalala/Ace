"""Gateway security API keeps approval authority in the Desktop main process."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message, ToolCall
from crew.gateway.server import create_app
from crew.gateway.auth import REMOTE_AUTH_COOKIE, create_remote_session_token
from crew.security.actions import normalize_file_action
from crew.security.runtime_client import RuntimeCapabilities
from crew.state.config import Config

_IDENTITY_A = {"X-Crew-Staff-Code": "A", "X-Crew-Staff-Uid": "uid-a"}
_IDENTITY_B = {"X-Crew-Staff-Code": "B", "X-Crew-Staff-Uid": "uid-b"}
_KEY = bytes.fromhex("42" * 32)


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
    key_dir = crew_home / ".gateway-instance"
    key_dir.mkdir(parents=True, mode=0o700)
    key_file = key_dir / "gateway-instance.key"
    key_file.write_text(_KEY.hex(), encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), plugins_enabled=[]),
        enable_team=False,
    )
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
async def test_fake_execution_requires_main_process_proof_even_in_dev_mode(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/security/fake-executions",
            json={"session_id": "s1", "argv": ["echo", "safe"]},
            headers=_IDENTITY_A,
        )
    assert response.status_code == 403


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
async def test_conversation_mode_is_set_by_authenticated_desktop_runtime(
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
    assert response.status_code == 200
    assert response.json() == {"mode": "auto_review"}


@pytest.mark.asyncio
async def test_strict_auto_review_requires_live_native_runtime(api, monkeypatch):
    async def unavailable_runtime():
        return None, False, False, "darwin"

    monkeypatch.setattr(
        "crew.gateway.routers.security._live_filesystem_runtime",
        unavailable_runtime,
    )
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
async def test_full_access_selection_does_not_depend_on_native_runtime(api, monkeypatch):
    async def unavailable_runtime():
        raise AssertionError("full access must not probe the managed runtime")

    monkeypatch.setattr(
        "crew.gateway.routers.security._live_filesystem_runtime",
        unavailable_runtime,
    )
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _put_json(
            client,
            "/api/security/mode",
            {"workspace_id": "default", "session_id": "s1", "mode": "full_access"},
        )

    assert response.status_code == 200
    assert response.json() == {"mode": "full_access"}


@pytest.mark.asyncio
async def test_fake_approval_round_trip_never_starts_a_process(api, monkeypatch):
    starts: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: starts.append((a, kw)))
    path = "/api/security/fake-executions"
    payload = {"session_id": "s1", "argv": ["echo", "safe"]}
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
        AsyncClient(transport=transport, base_url="http://test", cookies=_remote_cookies("uid-a")) as client_a,
        AsyncClient(transport=transport, base_url="http://test", cookies=_remote_cookies("uid-b")) as client_b,
    ):
        created = await _post_json(
            client_a,
            "/api/security/fake-executions",
            {"session_id": "s1", "argv": ["echo", "safe"]},
        )
        request = created.json()
        path = f"/api/security/requests/{request['request_id']}/decision"
        wrong_nonce = await _post_json(
            client_a,
            path,
            {"session_id": "s1", "nonce": "wrong", "decision": "once"},
        )
        other_owner = await _post_json(
            client_b,
            path,
            {"session_id": "s1", "nonce": request["nonce"], "decision": "once"},
            owner="b",
        )
    assert wrong_nonce.status_code == 409
    assert other_owner.status_code == 423


@pytest.mark.asyncio
async def test_fake_execution_cannot_create_reusable_authority(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for decision in ("session", "always"):
            created = await _post_json(
                client,
                "/api/security/fake-executions",
                {"session_id": "s1", "argv": ["python", "-m", "pytest"]},
            )
            request = created.json()
            path = f"/api/security/requests/{request['request_id']}/decision"
            decided = await _post_json(
                client,
                path,
                {
                    "session_id": "s1",
                    "nonce": request["nonce"],
                    "decision": decision,
                    **(
                        {"always_argv_prefix": ["python", "-m"]}
                        if decision == "always"
                        else {}
                    ),
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
async def test_rule_status_audit_export_and_capabilities_stay_behind_desktop_proof(api):
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/security/capabilities", headers=_IDENTITY_A)
        capabilities_path = "/api/security/capabilities"
        capabilities = await client.get(
            capabilities_path, headers=_headers("GET", capabilities_path)
        )
        export_path = "/api/security/audit/export"
        exported = await client.get(export_path, headers=_headers("GET", export_path))

    assert denied.status_code == 403
    assert capabilities.status_code == 200
    assert set(capabilities.json()) >= {
        "platform", "helper_present", "filesystem_sandbox", "managed_network", "detail"
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
        "&action_type=approval_decision&decision=once&session_id=session-a&sort=oldest"
    )
    proof_path = "/api/security/audit"
    transport = ASGITransport(app=api, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_headers("GET", proof_path))

    assert response.status_code == 200
    assert response.json() == {
        "events": [{
            "event_id": "event-1",
            "session_id": "session-a",
            "session_title": "修复登录问题",
            "workspace_id": "default",
            "workspace_name": "默认工作空间",
            "workspace_root": "",
            "current_approval_mode": "request_approval",
        }],
        "total": 42,
    }
    assert captured["limit"] == 20
    assert captured["offset"] == 40
    assert captured["action_type"] == "approval_decision"
    assert captured["decision"] == "once"
    assert captured["session_id"] == "session-a"
    assert captured["sort"] == "oldest"
    assert captured["owner_account_id"]


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
                    ToolCall(id="wrong", name="file_read", arguments={"path": str(tmp_path / "other")}),
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
            assert kwargs["readonly_roots"] == (Path(kwargs["cwd"]) / ".git",)
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
                'printf ok > "$1"; printf changed > "$2" 2>/dev/null || true; cat "$3" >/dev/null',
            )
            assert kwargs["readonly_roots"] == (Path(command[5]).parent,)
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


def test_proof_one_time_nonce_rejects_replay(tmp_path, monkeypatch):
    """H-19: a verified Desktop proof is consumed; the same proof cannot be replayed
    within its TTL even for an identical request."""
    from crew.gateway.instance_auth import verify_desktop_security_proof

    crew_home = tmp_path / ".crew"
    key_dir = crew_home / ".gateway-instance"
    key_dir.mkdir(parents=True, mode=0o700)
    key_file = key_dir / "gateway-instance.key"
    key_file.write_text(_KEY.hex(), encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    body = b'{"mode":"request_approval"}'
    path = "/api/security/mode"
    proof = _proof("POST", path, body)
    # First verification passes and consumes the nonce.
    assert verify_desktop_security_proof(proof, method="POST", path=path, body=body) is True
    # Replay of the exact same proof is refused.
    assert verify_desktop_security_proof(proof, method="POST", path=path, body=body) is False
