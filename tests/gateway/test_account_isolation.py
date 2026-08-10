"""Gateway auth and account-owned data isolation tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app
from crew.state.config import Config


OWNER_A = "A:uid-a"
OWNER_B = "B:uid-b"
LOCAL_OWNER = "local"


@pytest.mark.asyncio
async def test_sessions_and_workspaces_use_local_owner(tmp_path):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    crew.session_store.save(
        "s-a",
        [Message.user("hello A")],
        workspace_id="default",
        owner_account_id=LOCAL_OWNER,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/sessions")
        assert [row["session_id"] for row in listed.json()] == ["s-a"]

        workspace = await client.post(
            "/api/workspaces",
            json={"name": "Same Name"},
        )
        assert workspace.status_code == 200
        workspaces = await client.get("/api/workspaces")
        assert [row["name"] for row in workspaces.json()].count("Same Name") == 1
        assert len(workspaces.json()) == 2


@pytest.mark.asyncio
async def test_dynamic_kanban_routes_use_local_owner(tmp_path):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    assert crew.dynamic_kanban is not None
    workflow_local = crew.dynamic_kanban.store.for_owner(LOCAL_OWNER).create_workflow(
        "same-kanban",
        "Local workflow",
    )
    workflow_b = crew.dynamic_kanban.store.for_owner(OWNER_B).create_workflow(
        "same-kanban",
        "B workflow",
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        board = await client.get("/api/dynamic-kanban/same-kanban/board")
        assert board.status_code == 200
        assert board.json()["workflow"]["id"] == workflow_local.id
        assert board.json()["workflow"]["id"] != workflow_b.id


def test_same_session_id_is_isolated_across_accounts(tmp_path):
    store = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    ).session_store

    store.save("same", [Message.user("hello A")], owner_account_id=OWNER_A)
    store.save("same", [Message.user("hello B")], owner_account_id=OWNER_B)
    store.set_status("same", "completed", "done A", owner_account_id=OWNER_A)
    store.set_status("same", "failed", "done B", owner_account_id=OWNER_B)

    assert [msg.content for msg in store.load("same", owner_account_id=OWNER_A)] == ["hello A"]
    assert [msg.content for msg in store.load("same", owner_account_id=OWNER_B)] == ["hello B"]
    assert [row["session_id"] for row in store.list_sessions(owner_account_id=OWNER_A)] == ["same"]
    assert [row["session_id"] for row in store.list_sessions(owner_account_id=OWNER_B)] == ["same"]
    assert store.get_status("same", owner_account_id=OWNER_A) == ("completed", "done A")
    assert store.get_status("same", owner_account_id=OWNER_B) == ("failed", "done B")

    store.clear("same", owner_account_id=OWNER_A)

    assert store.load("same", owner_account_id=OWNER_A) == []
    assert [msg.content for msg in store.load("same", owner_account_id=OWNER_B)] == ["hello B"]


def test_ws_same_session_id_keeps_local_owner_separate_from_stored_owner(tmp_path):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    crew.session_store.save("same", [Message.user("hello A")], owner_account_id=OWNER_A)
    client = TestClient(create_app(crew))

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_id": "same", "query": "hello"})
        # 首帧可能是 model_fallback 等 status，跳过直到业务 delta/final/error
        msg = ws.receive_json()
        for _ in range(10):
            if msg.get("kind") != "status":
                break
            msg = ws.receive_json()

    # 首帧可能是 status / task 进度帧，只要不是鉴权失败即可
    assert msg["kind"] in {"delta", "final", "error", "status", "task"}
    assert crew.session_store.session_belongs_to("same", LOCAL_OWNER)
    assert [msg.content for msg in crew.session_store.load("same", owner_account_id=OWNER_A)] == ["hello A"]


def test_local_owner_does_not_require_admin_account_config(tmp_path, caplog):
    build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)

    assert "gateway.admin_accounts 为空" not in caplog.text


@pytest.mark.asyncio
async def test_usage_tasks_and_cron_are_scoped_to_local_owner(tmp_path):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    crew.session_store.save("s-a", [Message.user("hello A")], owner_account_id=LOCAL_OWNER)
    crew.session_store.save("s-b", [Message.user("hello B")], owner_account_id=OWNER_B)
    task_a = crew.tasks.create_runtime(
        kind="team",
        session_id="s-a",
        title="task A",
        owner_account_id=LOCAL_OWNER,
    )
    task_b = crew.tasks.create_runtime(
        kind="team",
        session_id="s-b",
        title="task B",
        owner_account_id=OWNER_B,
    )
    cron_a = crew.cron_store.create(
        name="cron A",
        schedule="every 1h",
        query="ping",
        session_id="s-a",
        owner_account_id=LOCAL_OWNER,
    )
    cron_b = crew.cron_store.create(
        name="cron B",
        schedule="every 1h",
        query="ping",
        session_id="s-b",
        owner_account_id=OWNER_B,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        usage_a = await client.get("/api/usage")
        assert usage_a.json()["session_count"] == 1

        tasks_a = await client.get("/api/tasks")
        assert [row["title"] for row in tasks_a.json()] == ["task A"]

        cron_jobs_a = await client.get("/api/cron/jobs")
        assert [row["name"] for row in cron_jobs_a.json()["jobs"]] == ["cron A"]

        assert (await client.get(f"/api/tasks/{task_a['task_id']}")).status_code == 200
        assert (await client.get(f"/api/cron/jobs/{cron_a['id']}")).status_code == 200
        assert (await client.get(f"/api/tasks/{task_b['task_id']}")).status_code == 404
        assert (await client.get(f"/api/cron/jobs/{cron_b['id']}")).status_code == 404
