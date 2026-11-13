"""Replay collaboration work from the owner-scoped execution journal."""

from types import SimpleNamespace

import pytest

from crew.core.types import Message, ToolCall
from crew.team.history_projection import team_internal_history_items


def execution():
    turn = "session::turn::req"
    return {
        "session_id": turn,
        "team_snapshot": {"leader_id": "crew::builtin", "members": [
            {"member_id": "crew::builtin", "name": "Crew"}, {"member_id": "agent-k", "name": "kimi"},
        ]},
        "units": [{"id": "a", "title": "整理材料"}, {"id": "b", "title": "检查证据"}],
        "attempts": [
            {"unit_id": "a", "planned_member": "agent-k", "actual_executor": "agent-k", "state": "completed",
             "started_at": 10, "ended_at": 20, "result": {"text": "完整成果" + "甲" * 1400}},
            {"unit_id": "b", "planned_member": "agent-k", "actual_executor": "agent-k", "state": "completed",
             "started_at": 21, "ended_at": 30, "result": {"text": "第二项成果"}},
        ],
    }


def fake_crew(events=None):
    class Store:
        def for_session(self, owner, session_id):
            assert owner == "owner" and session_id == "session"
            return [execution()]

    team = SimpleNamespace(_collaboration_execution_store=Store(), event_history_for_session=lambda *a, **k: events or [])
    return SimpleNamespace(team=team)


@pytest.mark.asyncio
async def test_journal_replays_assignments_and_full_results_with_scoped_process():
    children = [("session::turn::req::agent-k", [
        Message(role="assistant", content="先检查材料", timestamp=15, thinking="检查来源",
                tool_calls=[ToolCall(id="tool-a", name="read", arguments={"path": "a"})]),
        Message(role="assistant", content="再检查证据", timestamp=25,
                tool_calls=[ToolCall(id="tool-b", name="read", arguments={"path": "b"})]),
        Message(role="assistant", content="其他回合内容", timestamp=35),
    ])]
    items = await team_internal_history_items(fake_crew(), "session", children, "owner")
    assert [item["event_type"] for item in items] == ["team_assign", "team_submit", "team_assign", "team_submit"]
    assert items[0]["agent_name"] == "Crew" and items[0]["mention_to"] == ["agent-k"]
    assert items[1]["agent_name"] == "kimi" and len(items[1]["content"]) > 1200
    assert items[1]["process_text"] == "先检查材料" and items[1]["thinking"] == "检查来源"
    assert [call["id"] for call in items[1]["tool_calls"]] == ["tool-a"]
    assert items[3]["process_text"] == "再检查证据"
    assert [call["id"] for call in items[3]["tool_calls"]] == ["tool-b"]


@pytest.mark.asyncio
async def test_journal_enriches_existing_events_without_duplicate_cards():
    events = [
        {"role": "team_internal", "event_type": "team_assign", "node_id": "a", "request_id": "req",
         "source_session_id": "session::turn::req::leader", "timestamp": 10.1, "content": "已有派活"},
        {"role": "team_internal", "event_type": "team_submit", "node_id": "a", "request_id": "req",
         "source_session_id": "session::turn::req::agent-k", "timestamp": 19.9, "content": "截断内容"},
    ]
    items = await team_internal_history_items(fake_crew(events), "session", [], "owner")
    assert len(items) == 4
    assert sum(item["node_id"] == "a" and item["event_type"] == "team_assign" for item in items) == 1
    assert next(item for item in items if item["node_id"] == "a" and item["event_type"] == "team_submit")["content"].endswith("甲" * 1400)


@pytest.mark.asyncio
async def test_failed_attempt_is_not_presented_as_a_successful_agent_result():
    snapshot = execution()
    snapshot["attempts"] = [{"unit_id": "a", "planned_member": "agent-k", "state": "failed",
                             "started_at": 10, "ended_at": 12, "error": "启动失败"}]
    crew = SimpleNamespace(team=SimpleNamespace(_collaboration_execution_store=SimpleNamespace(for_session=lambda *_: [snapshot])))
    items = await team_internal_history_items(crew, "session", [], "owner")
    assert [item["event_type"] for item in items] == ["team_assign", "team_decision"]
    assert "启动失败" in items[1]["content"]
    assert all(item.get("agent_name") != "kimi" for item in items)


def test_session_history_endpoint_places_agent_work_before_the_final_answer():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from crew.gateway.auth import AccountContext
    from crew.gateway.routers.sessions import create_sessions_router

    class SessionStore:
        def session_belongs_to(self, session_id, owner_account_id):
            return session_id == "session" and owner_account_id == "owner"

        def get_agent_config(self, *_args, **_kwargs):
            return {"executor": "team"}

        def load(self, *_args, **_kwargs):
            return []

        def load_child_sessions(self, *_args, **_kwargs):
            return [("session::turn::req::agent-k", [Message(role="assistant", content="先检查材料", timestamp=15)])]

    class Tasks:
        def list_tasks(self, *_args, **_kwargs):
            return [{"kind": "agent_turn", "session_id": "session", "request_id": "req",
                     "detail": "用户需求", "result": "最终答复", "created_at": 1, "updated_at": 40}]

    crew = fake_crew()
    crew.session_store = SessionStore()
    crew.tasks = Tasks()
    app = FastAPI()

    @app.middleware("http")
    async def attach_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="owner")
        return await call_next(request)

    app.include_router(create_sessions_router(crew, dispatcher=None))
    response = TestClient(app).get("/api/session/session")
    assert response.status_code == 200
    items = response.json()
    assert [item.get("event_type", item["role"]) for item in items] == [
        "user", "team_assign", "team_submit", "team_assign", "team_submit", "assistant",
    ]
    assert items[2]["process_text"] == "先检查材料"
    assert items[-1]["content"] == "最终答复"
