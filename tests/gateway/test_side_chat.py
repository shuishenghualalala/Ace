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
        await crew.startup(start_cron=False)
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


@pytest.mark.parametrize(
    "binding",
    [None, {"model_profile_id": "source-model", "user_type": "external"}],
    ids=["desktop-first-send", "explicit-model-and-role-ceiling"],
)
async def test_side_chat_from_desktop_authoring_does_not_inherit_the_profile(gateway, binding):
    client, store = gateway
    parent_id = "desktop-parent"
    workspace_id = "desktop-workspace"
    path = f"/api/session/{parent_id}"
    if binding:
        store.ensure_session(parent_id, workspace_id=workspace_id, owner_account_id="local")
        store.set_agent_config(parent_id, {"executor": "builtin", **binding}, owner_account_id="local")
    prepared = await client.post(f"{path}/ensure", json={
        "workspace_id": workspace_id, "title": "Desktop chat", "prepare_builtin_authoring": True,
    })
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["builtin_authoring_prepared"] is True
    parent_config = store.get_agent_config(parent_id, owner_account_id="local")
    assert parent_config is not None
    assert parent_config["capability_profiles"] == ["blueprint.authoring"]
    store.save(parent_id, [Message.user("question"), Message.assistant("answer")], workspace_id,
               owner_account_id="local")

    response = await client.post(f"{path}/side-chat", json={})
    assert response.status_code == 200, response.text
    child_id = response.json()["session_id"]
    assert child_id != parent_id
    assert response.json() == {
        "ok": True, "session_id": child_id, "source_session_id": parent_id, "workspace_id": workspace_id,
    }
    child_config = store.get_agent_config(child_id, owner_account_id="local")
    assert child_config is not None
    assert {k: v for k, v in child_config.items() if k not in {"_created_at", "_updated_at"}} == {
        "executor": "builtin", **(binding or {}),
    }
    assert store.get_agent_config(parent_id, owner_account_id="local") == parent_config
    assert _contents(store, parent_id) == _contents(store, child_id) == ["question", "answer"]


async def test_side_chat_does_not_create_a_fork_for_unavailable_source(gateway):
    client, store = gateway
    history = [Message.user("existing question"), Message.assistant("existing answer")]
    store.save("foreign", history, owner_account_id="other-owner")
    store.save("internal::turn::worker", history, owner_account_id="local")
    store.ensure_session("empty", owner_account_id="local")
    store.save("external", history, owner_account_id="local")
    store.update_agent_config("external", lambda _: {"executor": "external"}, owner_account_id="local")
    builtin_authoring = {"executor": "builtin", "capability_profiles": ["blueprint.authoring"]}
    unsupported = {
        "custom": {"executor": "builtin", "capability_profiles": ["restricted"]},
        "mixed": {"executor": "builtin", "capability_profiles": ["blueprint.authoring", "restricted"]},
        "sites": {"executor": "builtin", "capability_profiles": ["sites.authoring"]},
        "preset": {**builtin_authoring, "preset_agent_type": "custom"},
        "explicit-permissions": {**builtin_authoring, "permission_mode": "full_access"},
        "external-authoring": {**builtin_authoring, "executor": "external"},
    }
    for sid, config in unsupported.items():
        store.save(sid, history, owner_account_id="local")
        store.set_agent_config(sid, config, owner_account_id="local")
    assert (await client.post("/api/session/custom/side-chat", content="{}")).status_code in {415, 422}
    store.save("archived", history, owner_account_id="local")
    store.set_archived("archived", True, owner_account_id="local")
    for sid, status in [
        ("absent", 404), ("foreign", 404), ("internal::turn::worker", 404), ("archived", 404),
        ("empty", 409), ("external", 409), *((sid, 409) for sid in unsupported),
    ]:
        response = await client.post(f"/api/session/{sid}/side-chat", json={})
        assert response.status_code == status, (sid, response.status_code, response.text)
    assert {row["session_id"] for row in store.list_sessions(owner_account_id="local", include_archived=True)} == {
        "archived", "empty", "external", *unsupported,
    }
    assert [row["session_id"] for row in store.list_sessions(owner_account_id="other-owner")] == ["foreign"]
