"""外部 runtime 注册接口字段校验测试（G9）。"""

from __future__ import annotations

import sys

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.routers import runtimes as runtimes_router
from crew.gateway.server import create_app
from crew.security.launch import current_process_launch
from crew.state.config import Config


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    # 显式关闭 dev_mode，避免 loopback 被劫持成 dev:dev 并自动 admin
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            gateway_dev_mode=False,
            gateway_admin_accounts=["A:uid-a"],
        ),
        enable_team=False,
    )
    return create_app(crew)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_error_fragment",
    [
        # 完全无关的 payload → 400，并指出缺失字段
        ({"foo": "bar"}, "id"),
        # id 存在但类型错误 → 400
        ({"id": 123, "type": "claude", "provider": "anthropic"}, None),
        # 只有部分必填字段（缺 provider）→ 400
        ({"id": "rt-1", "type": "claude"}, "provider"),
    ],
    ids=["missing-fields", "wrong-typed-field", "partial-missing"],
)
async def test_register_invalid_payload_returns_400(api, auth_headers, payload, expected_error_fragment):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/runtimes/register", json=payload)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    if expected_error_fragment is not None:
        assert expected_error_fragment in resp.json()["error"]


@pytest.mark.asyncio
async def test_register_valid_passes(api, auth_headers):
    """合法 payload 通过校验并写入（200）。"""
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/runtimes/register",
            json={"id": "rt-valid", "type": "claude", "provider": "anthropic"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "rt-valid"
    assert data["display_badge"] == "A"


@pytest.mark.asyncio
async def test_scan_allows_authenticated_non_admin(api, monkeypatch):
    calls = 0

    async def fake_discover():
        nonlocal calls
        calls += 1
        launch = current_process_launch.get()
        assert launch is not None
        assert launch.managed is False
        return []

    monkeypatch.setattr(runtimes_router, "discover_local_runtimes", fake_discover)
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runtimes/scan")

    assert resp.status_code == 200
    assert resp.json() == []
    assert calls == 1


@pytest.mark.asyncio
async def test_scan_requires_login(api, monkeypatch):
    async def fake_discover():
        raise AssertionError("未登录请求不应启动 Runtime 探测")

    monkeypatch.setattr(runtimes_router, "discover_local_runtimes", fake_discover)
    transport = ASGITransport(app=api, client=("10.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runtimes/scan")

    assert resp.status_code == 401
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_admin_scan_remains_supported(api, auth_headers, monkeypatch):
    calls = 0

    async def fake_discover():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(runtimes_router, "discover_local_runtimes", fake_discover)
    transport = ASGITransport(app=api)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        resp = await client.post("/api/runtimes/scan")

    assert resp.status_code == 200
    assert calls == 1


@pytest.mark.asyncio
async def test_delete_runtime_removes_unused_registered_record(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        registered = await client.post(
            "/api/runtimes/register",
            json={"id": "runtime-e2e", "type": "e2e", "provider": "e2e"},
        )
        assert registered.status_code == 200

        deleted = await client.delete("/api/runtimes/runtime-e2e")

        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}
        assert all(runtime["id"] != "runtime-e2e" for runtime in (await client.get("/api/runtimes")).json())


@pytest.mark.asyncio
async def test_delete_runtime_rejects_record_used_by_agent(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await _register_ready_runtime(client)
        agent = await client.post(
            "/api/external-agents",
            json={"name": "保留的外援", "runtime_id": "rt-models", "model": "model-a"},
        )
        assert agent.status_code == 200

        deleted = await client.delete("/api/runtimes/rt-models")

        assert deleted.status_code == 409
        assert "请先删除对应智能体" in deleted.json()["error"]
        assert any(runtime["id"] == "rt-models" for runtime in (await client.get("/api/runtimes")).json())


async def _register_ready_runtime(client: AsyncClient) -> None:
    response = await client.post(
        "/api/runtimes/register",
        json={
            "id": "rt-models",
            "type": "codex",
            "provider": "codex",
            "protocol": "cli",
            "executable_path": sys.executable,
            "metadata": {
                "availability_status": "ready",
                "models": [
                    {"id": "model-a", "label": "Model A", "default": True},
                    {"id": "model-b", "label": "Model B", "default": False},
                ],
                "default_model_id": "model-a",
            },
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_error"),
    [
        ("", "请选择模型"),
        ("model-unknown", "所选模型不属于当前运行时"),
    ],
)
async def test_create_external_agent_requires_catalog_model(api, auth_headers, model, expected_error):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await _register_ready_runtime(client)
        response = await client.post(
            "/api/external-agents",
            json={"name": "测试智能体", "runtime_id": "rt-models", "model": model},
        )

    assert response.status_code == 400
    assert response.json()["error"] == expected_error


@pytest.mark.asyncio
async def test_create_external_agent_persists_selected_catalog_model(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await _register_ready_runtime(client)
        response = await client.post(
            "/api/external-agents",
            json={"name": "测试智能体", "runtime_id": "rt-models", "model": "model-b"},
        )

    assert response.status_code == 200
    assert response.json()["model"] == "model-b"
    assert response.json()["display_badge"] == "X"
    assert response.json()["profile"]["model"]["binding_status"] == "valid"


@pytest.mark.asyncio
async def test_external_agent_and_team_api_are_owner_private_but_runtime_is_shared(api, auth_headers, monkeypatch):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as owner_a_client:
        await _register_ready_runtime(owner_a_client)
        agent_a_response = await owner_a_client.post(
            "/api/external-agents",
            json={"name": "Agent A", "runtime_id": "rt-models", "model": "model-a"},
        )
        assert agent_a_response.status_code == 200
        agent_a = agent_a_response.json()
        team_a_response = await owner_a_client.post(
            "/api/external-teams",
            json={
                "name": "Team A",
                "leader_agent_id": agent_a["id"],
                "members": [{"agent_id": agent_a["id"], "role": "Leader"}],
            },
        )
        assert team_a_response.status_code == 200
        assert team_a_response.json()["display_badge"] == "T"
        assert team_a_response.json()["members"][0]["display_badge"] == "X"
        assert (await owner_a_client.post("/api/auth/logout")).status_code == 200

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "B:uid-b")
    async with AsyncClient(transport=transport, base_url="http://test") as owner_b_client:
        assert (await owner_b_client.get("/api/external-agents")).json() == []
        assert (await owner_b_client.get("/api/external-teams")).json() == []
        assert (await owner_b_client.delete(f"/api/external-agents/{agent_a['id']}")).status_code == 404
        assert (await owner_b_client.delete(f"/api/external-teams/{team_a_response.json()['id']}")).status_code == 404
        cross_team = await owner_b_client.post(
            "/api/external-teams",
            json={
                "name": "Cross Owner Team",
                "leader_agent_id": agent_a["id"],
                "members": [{"agent_id": agent_a["id"], "role": "Leader"}],
            },
        )
        assert cross_team.status_code == 404
        agent_b = await owner_b_client.post(
            "/api/external-agents",
            json={"name": "Agent B", "runtime_id": "rt-models", "model": "model-b"},
        )
        # Owner B can bind its private Agent to the Runtime registered by Owner A:
        # Runtime remains global while Agent/Team ownership stays private.
        assert agent_b.status_code == 200
        assert [item["name"] for item in (await owner_b_client.get("/api/external-agents")).json()] == ["Agent B"]
        team_b = await owner_b_client.post(
            "/api/external-teams",
            json={
                "name": "Team B",
                "leader_agent_id": agent_b.json()["id"],
                "members": [{"agent_id": agent_b.json()["id"], "role": "Leader"}],
            },
        )
        assert team_b.status_code == 200
        assert (await owner_b_client.delete(f"/api/external-teams/{team_b.json()['id']}")).status_code == 200
        assert (await owner_b_client.delete(f"/api/external-agents/{agent_b.json()['id']}")).status_code == 200
        assert (await owner_b_client.get("/api/external-agents")).json() == []
        assert (await owner_b_client.get("/api/external-teams")).json() == []
        assert (await owner_b_client.post("/api/auth/logout")).status_code == 200

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    async with AsyncClient(transport=transport, base_url="http://test") as owner_a_client:
        assert [item["name"] for item in (await owner_a_client.get("/api/external-agents")).json()] == ["Agent A"]
        assert [item["name"] for item in (await owner_a_client.get("/api/external-teams")).json()] == ["Team A"]
