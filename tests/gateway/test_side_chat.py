"""Session-side chat uses an owner-bound, committed transcript cut."""

from __future__ import annotations

import httpx
import pytest

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.app import create_app
from crew.state.config import Config
from crew.state.session_store import SessionEventType


@pytest.fixture
async def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_OFFLINE", "1")
    crew = build_app(config=Config(
        db_path=str(tmp_path / "side-chat.db"), cron_enabled=False, auth_mode="local",
    ), enable_team=False)
    app = create_app(crew)
    try:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 18000))
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            yield client, crew.session_store
    finally:
        await crew.shutdown()


def _contents(store, session_id, owner="local"):
    return [message.content for message in store.load(session_id, owner_account_id=owner)]


async def test_side_chat_uses_fixed_settled_cut_and_does_not_modify_parent(gateway):
    client, store = gateway
    parent_id = "parent"
    original = [Message.user("previous question"), Message.assistant("committed answer")]
    unfinished = Message.user("unfinished main turn")
    store.save(parent_id, original, "workspace-fixture", owner_account_id="local")
    store.update_agent_config(parent_id, lambda _: {
        "executor": "builtin", "model_profile_id": "source-model", "user_type": "external",
    }, owner_account_id="local")
    store.record_turn_event(parent_id, kind=SessionEventType.TURN_START, owner_account_id="local")
    store.save(parent_id, [*original, unfinished], owner_account_id="local")

    response = await client.post(f"/api/session/{parent_id}/side-chat", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    sid = result["session_id"]
    assert result == {"ok": True, "session_id": sid, "source_session_id": parent_id, "workspace_id": "workspace-fixture"}
    assert sid != parent_id
    assert _contents(store, sid) == [m.content for m in original]
    assert _contents(store, parent_id) == [m.content for m in [*original, unfinished]]
    child_config = store.get_agent_config(sid, owner_account_id="local")
    assert child_config is not None
    assert child_config["user_type"] == "external"
    assert child_config["model_profile_id"] == "source-model"

    store.record_turn_event(parent_id, kind=SessionEventType.TURN_END, owner_account_id="local")
    store.save(parent_id, [*original, unfinished, Message.assistant("new parent answer")], owner_account_id="local")
    store.save(sid, [*original, Message.user("independent side question")], owner_account_id="local")
    assert _contents(store, sid) == ["previous question", "committed answer", "independent side question"]
    assert _contents(store, parent_id)[-1] == "new parent answer"
    store.clear(parent_id, owner_account_id="local")
    assert _contents(store, sid) == ["previous question", "committed answer", "independent side question"]


async def test_side_chat_does_not_create_a_fork_for_unavailable_source(gateway):
    client, store = gateway
    history = [Message.user("existing question"), Message.assistant("existing answer")]
    store.save("foreign", history, owner_account_id="other-owner")
    store.save("internal::turn::worker", history, owner_account_id="local")
    store.ensure_session("empty", owner_account_id="local")
    store.save("external", history, owner_account_id="local")
    store.update_agent_config("external", lambda _: {"executor": "external"}, owner_account_id="local")
    store.save("custom", history, owner_account_id="local")
    store.update_agent_config(
        "custom", lambda _: {"executor": "builtin", "capability_profiles": ["restricted"]},
        owner_account_id="local",
    )
    assert (await client.post("/api/session/custom/side-chat", content="{}")).status_code in {415, 422}
    store.save("archived", history, owner_account_id="local")
    store.set_archived("archived", True, owner_account_id="local")
    for sid, status in [
        ("absent", 404), ("foreign", 404), ("internal::turn::worker", 404), ("archived", 404),
        ("empty", 409), ("external", 409), ("custom", 409),
    ]:
        response = await client.post(f"/api/session/{sid}/side-chat", json={})
        assert response.status_code == status, (sid, response.status_code, response.text)
    assert {row["session_id"] for row in store.list_sessions(owner_account_id="local", include_archived=True)} == {
        "archived", "custom", "empty", "external",
    }
    assert [row["session_id"] for row in store.list_sessions(owner_account_id="other-owner")] == ["foreign"]
