"""Owner-scoped HTTP controls for continuable subagents."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.agent.subagent.feature import _create_subagent_router


class _Service:
    def __init__(self):
        self.messages = []
        self.interrupts = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"status": "queued", "subagent_id": kwargs["subagent_id"], "state": "ready"}

    def interrupt_agent(self, **kwargs):
        self.interrupts.append(kwargs)
        return True


class _Host:
    def __init__(self, service):
        self.plugins = None
        self.subagent_runtime = service


def _client(service):
    app = FastAPI()
    app.include_router(_create_subagent_router(_Host(service)))
    return TestClient(app)


def test_message_route_requires_session_and_preserves_owner_boundary():
    service = _Service()
    client = _client(service)

    missing_session = client.post(
        "/api/subagents/child-1/message",
        json={"message": "继续"},
    )
    assert missing_session.status_code == 400

    response = client.post(
        "/api/subagents/child-1/message?session_id=parent-1",
        json={"message": "继续", "trigger_turn": False},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "queued",
        "subagent_id": "child-1",
        "state": "ready",
    }
    assert service.messages[0]["parent_session_id"] == "parent-1"
    assert service.messages[0]["owner_account_id"] == "local"
    assert service.messages[0]["trigger_turn"] is False


def test_interrupt_route_delegates_to_runtime():
    service = _Service()
    client = _client(service)

    response = client.post(
        "/api/subagents/child-1/interrupt?session_id=parent-1",
        json={"message": "停止"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "interrupted",
        "subagent_id": "child-1",
    }
    assert service.interrupts[0]["parent_session_id"] == "parent-1"
    assert service.interrupts[0]["message"] == "停止"
