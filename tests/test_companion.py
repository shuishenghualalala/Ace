"""Companion domain invariants and persistence."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from crew.companion import CompanionService, CompanionStore
from crew.core.mocks import InMemoryWorkspaceStore
from crew.core.types import Message
from crew.gateway.routers.companion import _prepare_attachment, _store_received_attachment
from crew.state.home import get_owner_runtime_home
from crew.state.session_store import SQLiteSessionStore


class _ExternalAgents:
    def list_agents(self, *, owner_account_id: str = "", include_managed: bool = True):
        assert owner_account_id
        return [
            {
                "id": "ext-1",
                "name": "Local Codex",
                "description": "ACP local agent",
                "provider": "codex",
            }
        ]

    def agent_with_runtime(self, agent_id: str, *, owner_account_id: str = ""):
        assert agent_id == "ext-1"
        return ({"id": agent_id}, {"provider": "codex", "availability_status": "ready"})


def _service(tmp_path):
    sessions = SQLiteSessionStore(str(tmp_path / "sessions.db"))
    workspaces = InMemoryWorkspaceStore()
    return (
        CompanionService(
            CompanionStore(str(tmp_path / "companion.db")),
            session_store=sessions,
            workspace_store=workspaces,
            external_agents=_ExternalAgents(),
        ),
        sessions,
        workspaces,
    )


def _connect_peer(service: CompanionService, peer_id: str = "peer-1") -> None:
    service.store.upsert_peer(
        "owner-a",
        peer_id,
        profile={"peer_id": peer_id, "display_name": "小明"},
        connection_state="connected",
    )


def test_default_profile_publishes_crew_and_creates_companion_workspace(tmp_path):
    service, _, workspaces = _service(tmp_path)
    profile = service.public_profile("owner-a")
    assert [agent["display_name"] for agent in profile["agents"]] == ["Crew"]
    assert workspaces.get("companion", owner_account_id="owner-a")["name"] == "同伴空间"


def test_user_can_disable_default_crew_and_publish_external_agent(tmp_path):
    service, _, _ = _service(tmp_path)
    service.update_publications("owner-a", ["external:ext-1"])
    # Re-reading defaults must not silently republish Crew.
    profile = service.public_profile("owner-a")
    assert [agent["display_name"] for agent in profile["agents"]] == ["Local Codex"]
    candidates = {item["source_ref"]: item for item in service.publication_candidates("owner-a")}
    assert candidates["builtin:crew"]["published"] is False
    assert candidates["external:ext-1"]["published"] is True


def test_open_conversation_binds_workspace_and_uses_stable_session(tmp_path):
    service, sessions, _ = _service(tmp_path)
    first = service.open_conversation(
        "owner-a", kind="nearby_room", target_id="room-1", title="设计群"
    )
    second = service.open_conversation(
        "owner-a", kind="nearby_room", target_id="room-1", title="设计群"
    )
    assert first["session_id"] == second["session_id"]
    assert first["workspace_id"] == "companion"
    assert first["capabilities"]["can_mention_agents"] is True
    listed = sessions.list_sessions("companion", owner_account_id="owner-a")
    assert listed[0]["title"] == "设计群"


def test_dm_has_no_agent_capability_and_strips_mentions(tmp_path):
    service, sessions, _ = _service(tmp_path)
    _connect_peer(service)
    binding = service.open_conversation(
        "owner-a", kind="nearby_dm", target_id="peer-1", title="小明"
    )
    assert binding["capabilities"]["can_mention_agents"] is False
    receipt = service.enqueue_human_message(
        "owner-a",
        session_id=binding["session_id"],
        text="你好",
        mentions=["agent-should-not-pass"],
    )
    event = service.store.claim_outbox("owner-a")[0]
    assert event["event_id"] == receipt["event_id"]
    assert event["payload"]["mentions"] == []
    assert sessions.load(binding["session_id"], owner_account_id="owner-a")[0].content == "你好"
    saved = sessions.load(binding["session_id"], owner_account_id="owner-a")[0]
    assert saved.message_id == receipt["event_id"]
    assert saved.origin == {
        "source": "companion",
        "sender_kind": "human",
        "sender_id": "owner-a",
        "sender_name": "我",
        "is_self": True,
        "delivery_state": "queued",
    }


def test_message_with_attachment_is_persisted_and_queued_without_local_path(tmp_path):
    service, sessions, _ = _service(tmp_path)
    _connect_peer(service)
    binding = service.open_conversation(
        "owner-a", kind="nearby_dm", target_id="peer-1", title="小明"
    )
    upload = tmp_path / "note.txt"
    upload.write_text("hello", encoding="utf-8")
    service.enqueue_human_message(
        "owner-a",
        session_id=binding["session_id"],
        text="请看附件",
        attachments=[{
            "file_id": "file-1",
            "name": "note.txt",
            "path": str(upload),
            "mime_type": "text/plain",
            "size": 5,
            "sha256": hashlib.sha256(b"hello").hexdigest(),
        }],
    )
    history = sessions.load(binding["session_id"], owner_account_id="owner-a")
    assert history[0].content == f'附件「note.txt」位于: {upload}\n\n请看附件'
    event = service.store.claim_outbox("owner-a")[0]
    assert event["payload"]["files"] == [{
        "file_id": "file-1",
        "name": "note.txt",
        "mime_type": "text/plain",
        "size": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }]
    assert str(upload) not in str(event["payload"])


def test_attachment_prepare_and_receive_are_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    uploads = get_owner_runtime_home("owner-a") / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    source = uploads / "note.txt"
    source.write_bytes(b"hello")
    prepared = _prepare_attachment("owner-a", {"id": "file-1", "name": "note.txt", "path": str(source)})
    assert prepared["data_base64"] == base64.b64encode(b"hello").decode("ascii")
    assert prepared["sha256"] == hashlib.sha256(b"hello").hexdigest()
    with pytest.raises(ValueError, match="上传目录"):
        _prepare_attachment("owner-a", {"name": "escape.txt", "path": str(tmp_path / "escape.txt")})

    received = _store_received_attachment("owner-a", prepared)
    assert received["name"] == "note.txt"
    assert Path(received["path"]).exists()


def test_agent_can_only_send_to_existing_room(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(KeyError):
        service.enqueue_agent_room_message("owner-a", room_id="missing", text="done")
    _connect_peer(service)
    service.store.upsert_room("owner-a", "room-1", name="群聊", human_member_ids=["peer-1"])
    receipt = service.enqueue_agent_room_message("owner-a", room_id="room-1", text="done")
    assert receipt["status"] == "queued"


def test_offline_dm_and_room_never_enter_outbox(tmp_path):
    service, _, _ = _service(tmp_path)
    dm = service.open_conversation(
        "owner-a", kind="nearby_dm", target_id="peer-1", title="小明"
    )
    service.store.upsert_room(
        "owner-a", "room-1", name="项目群", human_member_ids=["peer-1"]
    )
    room = service.open_conversation(
        "owner-a", kind="nearby_room", target_id="room-1", title="项目群"
    )

    for session_id, error in (
        (dm["session_id"], "同伴暂时离线"),
        (room["session_id"], "暂无其他在线同伴"),
    ):
        with pytest.raises(ValueError, match=error):
            service.enqueue_human_message(
                "owner-a", session_id=session_id, text="这条消息不能入队"
            )
    assert service.store.claim_outbox("owner-a") == []


def test_outbox_failed_delivery_is_retryable(tmp_path):
    service, _, _ = _service(tmp_path)
    service.store.enqueue(
        "owner-a", kind="nearby_room", target_id="room-1", payload={"type": "test"}
    )
    first = service.store.claim_outbox("owner-a")
    assert len(first) == 1
    assert service.store.claim_outbox("owner-a") == []
    service.store.settle_outbox("owner-a", first[0]["event_id"], delivered=False)
    assert service.store.claim_outbox("owner-a")[0]["event_id"] == first[0]["event_id"]


def test_outbox_tracks_sent_delivered_and_failed_states(tmp_path):
    service, _, _ = _service(tmp_path)
    receipt = service.store.enqueue(
        "owner-a", kind="nearby_dm", target_id="peer-1", payload={"type": "message"}
    )
    for status in ("sent", "failed", "delivered"):
        service.store.set_outbox_status(
            "owner-a", receipt["event_id"], status=status
        )
        row = service.store._writer.execute(
            lambda conn: conn.execute(
                "SELECT status FROM companion_outbox WHERE owner_account_id = ? AND event_id = ?",
                ("owner-a", receipt["event_id"]),
            ).fetchone()
        )
        assert row[0] == status
    service.store.set_outbox_status(
        "owner-a", receipt["event_id"], status="sent"
    )
    row = service.store._writer.execute(
        lambda conn: conn.execute(
            "SELECT status FROM companion_outbox WHERE owner_account_id = ? AND event_id = ?",
            ("owner-a", receipt["event_id"]),
        ).fetchone()
    )
    assert row[0] == "delivered"


def test_append_idempotent_preserves_remote_identity(tmp_path):
    _, sessions, _ = _service(tmp_path)
    message = Message.user("收到")
    message.name = "小明"
    message.message_id = "remote-message-1"
    message.origin = {
        "source": "companion",
        "sender_kind": "human",
        "sender_id": "peer-1",
        "sender_name": "小明",
        "is_self": False,
        "delivery_state": "delivered",
    }
    assert sessions.append_idempotent("agent:main:nearby:dm:test", message, owner_account_id="owner-a")
    assert not sessions.append_idempotent("agent:main:nearby:dm:test", message, owner_account_id="owner-a")
    restored = sessions.load("agent:main:nearby:dm:test", owner_account_id="owner-a")
    assert len(restored) == 1
    assert restored[0].name == "小明"
    assert restored[0].origin == message.origin


def test_agent_runs_can_be_listed_globally_or_by_room(tmp_path):
    service, _, _ = _service(tmp_path)
    for room_id in ("room-1", "room-2"):
        service.store.create_run(
            "owner-a",
            room_id=room_id,
            public_agent_id="agent-crew",
            source_message_id=f"message-{room_id}",
            child_session_id=f"session-{room_id}",
        )

    assert [run["room_id"] for run in service.store.list_runs("owner-a")] == [
        "room-1",
        "room-2",
    ]
    assert [run["room_id"] for run in service.store.list_runs("owner-a", "room-2")] == [
        "room-2"
    ]
