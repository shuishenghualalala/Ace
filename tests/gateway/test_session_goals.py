"""Goal controls, persisted scope, and model context through the actual Gateway."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Event

import pytest
from starlette.testclient import TestClient

from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse
from crew.features.hooks import hook_registry
from crew.gateway.app import create_app
from crew.state.config import Config


@pytest.fixture
def goal_gateway(tmp_path, monkeypatch, unused_tcp_port_factory):
    monkeypatch.setenv("CREW_OFFLINE", "1")

    @contextmanager
    def open_gateway(provider):
        monkeypatch.setattr("crew.app.build_provider", lambda _cfg: provider)
        ready = Event()

        def on_ready(_event, _context):
            ready.set()

        crew = build_app(config=Config(
            db_path=str(tmp_path / "goal-sessions.db"), cron_enabled=False, title_auto=False,
            auth_mode="local", health_port=unused_tcp_port_factory(),
        ), enable_team=False)
        hook_registry.register("gateway:startup", on_ready)
        try:
            with TestClient(create_app(crew), client=("127.0.0.1", 17000)) as client:
                assert ready.wait(15), "Gateway did not become ready"
                yield client, crew
        finally:
            hook_registry.unregister("gateway:startup", on_ready)

    return open_gateway


def _send(client, sid, text, **options):
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"session_id": sid, "query": text, **options})
        for _ in range(30):
            frame = socket.receive_json()
            if frame.get("is_final"):
                assert frame["body"]["outcome"]["status"] == "completed", frame
                return
    pytest.fail("No terminal chat frame")


def _model():
    return FakeProvider([ChatResponse(text="goal fixture reply", finish_reason="stop")] * 4)


def _set(client, path, version, objective):
    return client.post(path, json={"action": "set", "version": version, "objective": objective})


def test_goal_round_trip_controls_prompt_and_survives_restart(goal_gateway):
    provider = _model()
    objective = "Audit the synthetic invoice reference 896A"
    path = "/api/session/goal-main/goal"
    with goal_gateway(provider) as (client, _crew):
        assert client.post("/api/session/goal-main/ensure", json={}).status_code == 200
        assert client.post("/api/session/unrelated/ensure", json={}).status_code == 200
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.json()["goal"] is None
        applied = _set(client, path, initial.json()["version"], objective)
        assert applied.status_code == 200
        assert applied.json()["goal"] == {"objective": objective, "status": "active"}
        assert provider.calls == []

        _send(client, "goal-main", "Report the next step")
        assert any(objective in message.content for message in provider.calls[0])
        assert client.get(path).json() == applied.json()
        _send(client, "unrelated", "Another conversation")
        assert all(objective not in message.content for message in provider.calls[1])

        paused = client.post(path, json={"action": "pause", "version": applied.json()["version"]})
        assert paused.status_code == 200
        assert paused.json()["goal"]["status"] == "paused"
        stale = client.post(path, json={"action": "complete", "version": applied.json()["version"]})
        assert stale.status_code == 409
        assert client.get(path).json() == paused.json()
        _send(client, "goal-main", "Answer without goal context")
        assert all(objective not in message.content for message in provider.calls[2])

    after_restart = _model()
    with goal_gateway(after_restart) as (client, _crew):
        assert client.get(path).json() == paused.json()
        resumed = client.post(path, json={"action": "resume", "version": paused.json()["version"]})
        assert resumed.status_code == 200
        assert resumed.json()["goal"]["status"] == "active"
        _send(client, "goal-main", "Continue here")
        assert any(objective in message.content for message in after_restart.calls[0])
        assert any("Answer without goal context" == message.content for message in after_restart.calls[0])


def test_goal_not_owned_or_replaced_is_not_mutated(goal_gateway):
    with goal_gateway(_model()) as (client, crew):
        path = "/api/session/target/goal"
        crew.session_store.ensure_session("target", owner_account_id="another-owner")
        assert client.get(path).status_code == 404
        assert _set(client, path, "before", "Do not create a foreign goal").status_code == 404
        assert not crew.session_store.session_belongs_to("target", owner_account_id="local")

        assert client.post("/api/session/target/ensure", json={}).status_code == 200
        old_version = client.get(path).json()["version"]
        applied = _set(client, path, old_version, "Original target")
        assert applied.status_code == 200
        crew.session_store.clear("target", owner_account_id="local")
        assert client.post("/api/session/target/ensure", json={}).status_code == 200
        assert client.get(path).json()["goal"] is None
        assert _set(client, path, old_version, "Stale target").status_code == 409
        assert client.get(path).json()["goal"] is None
        assert crew.session_store.session_belongs_to("target", owner_account_id="another-owner")
