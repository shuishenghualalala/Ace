"""WebSocket subscribe/resume 回放测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

from crew.gateway.ws import (
    create_ws_router,
    normalize_team_execution_profile,
    normalize_user_mentions,
)

AUTH_HEADERS: dict[str, str] = {}


def test_team_execution_profile_accepts_requested_mode_only():
    assert normalize_team_execution_profile({"requested_mode": "auto"}) == {
        "requested_mode": "auto",
        "profile_source": "user",
    }
    assert normalize_team_execution_profile({"requested_mode": "ai"})["requested_mode"] == "ai"
    assert normalize_team_execution_profile({"requested_mode": "unknown"}) is None
    assert normalize_team_execution_profile({"mode": "fast"}) is None


def test_user_mentions_keep_display_text_out_of_identity_transport():
    assert normalize_user_mentions([
        {"kind": "team_member", "member_id": "agent_c6f06632e6a4"},
    ]) == [{"kind": "team_member", "member_id": "agent_c6f06632e6a4"}]
    assert normalize_user_mentions([]) == []
    assert normalize_user_mentions([
        {"kind": "file", "member_id": "agent_c6f06632e6a4"},
    ]) is None
    assert normalize_user_mentions({"kind": "team_member", "member_id": "kk"}) is None


class _ReplayConnections:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.replays: list[tuple[str, int]] = []
        self.owners: list[str] = []
        self.sockets: dict[str, list] = {}

    def register_owner(self, owner_account_id: str, _socket) -> None:
        self.owners.append(owner_account_id)

    def register(self, session_id: str, _socket, **_kwargs) -> None:
        self.registered.append(session_id)
        self.sockets.setdefault(session_id, []).append(_socket)

    def unregister_all(self, _socket, _session_ids: set[str], **_kwargs) -> None:
        pass

    async def send_socket(self, socket, payload: dict) -> None:
        await socket.send_json(payload)

    async def push_payload(self, session_id: str, payload: dict, **_kwargs) -> None:
        payload = {**payload, "session_id": session_id}
        for socket in self.sockets.get(session_id, []):
            await socket.send_json(payload)

    async def replay(self, session_id: str, socket, *, after_gateway_sequence: int = 0, filter_fn=None, **_kwargs) -> None:
        self.replays.append((session_id, after_gateway_sequence))
        await socket.send_json({
            "kind": "delta",
            "body": {"text": f"replay:{session_id}:{after_gateway_sequence}"},
            "is_final": False,
            "sequence": 0,
            "session_id": session_id,
            "gateway_sequence": after_gateway_sequence + 1,
        })


class _ActiveOwnerStub:
    def __init__(self) -> None:
        self.claimed: list[str] = []

    def claim(self, owner_account_id: str) -> None:
        self.claimed.append(owner_account_id)


def _client(connections: _ReplayConnections) -> TestClient:
    app = FastAPI()
    crew = SimpleNamespace(
        active_owner=_ActiveOwnerStub(),
        config=SimpleNamespace(),
        session_store=SimpleNamespace(session_belongs_to=lambda _sid, _owner: True),
    )
    dispatcher = SimpleNamespace()
    channel_manager = SimpleNamespace(status=lambda: [])
    app.include_router(create_ws_router(crew, dispatcher, connections, channel_manager))
    return TestClient(app)


def test_ws_subscribe_replays_after_last_gateway_sequence():
    connections = _ReplayConnections()
    client = _client(connections)

    with client.websocket_connect("/ws", headers=AUTH_HEADERS) as ws:
        ws.send_json({
            "action": "subscribe",
            "session_id": "s1",
            "sessions": ["s1", "s2"],
            "last_gateway_sequences": {"s1": 3, "s2": 8},
        })

        first = ws.receive_json()
        second = ws.receive_json()

    assert connections.registered == ["s1", "s2"]
    assert connections.replays == [("s1", 3), ("s2", 8)]
    assert first["body"]["text"] == "replay:s1:3"
    assert second["body"]["text"] == "replay:s2:8"


def test_ws_message_forwards_structured_user_mentions_before_dispatch(monkeypatch):
    connections = _ReplayConnections()
    captured = []

    async def fake_stream_and_broadcast(_crew, connection_store, envelope, owner):
        captured.append(envelope)
        await connection_store.push_payload(
            envelope.session_id,
            {
                "kind": "captured",
                "body": {"user_mentions": envelope.params.get("user_mentions")},
                "is_final": True,
                "sequence": 1,
            },
            owner_account_id=owner,
        )
        return "", None

    monkeypatch.setattr(
        "crew.gateway.ws.stream_and_broadcast",
        fake_stream_and_broadcast,
    )
    client = _client(connections)

    with client.websocket_connect("/ws", headers=AUTH_HEADERS) as ws:
        ws.send_json({
            "query": "@kk 你现在用什么模型",
            "session_id": "mention-s1",
            "external_team_id": "team-1",
            "user_mentions": [{"kind": "team_member", "member_id": "kk"}],
        })
        message = ws.receive_json()

    assert captured[0].query == "@kk 你现在用什么模型"
    assert captured[0].params["external_team_id"] == "team-1"
    assert captured[0].params["user_mentions"] == [
        {"kind": "team_member", "member_id": "kk"},
    ]
    assert message["body"]["user_mentions"] == captured[0].params["user_mentions"]


def test_ws_subscribe_replay_defaults_invalid_sequence_to_zero():
    connections = _ReplayConnections()
    client = _client(connections)

    with client.websocket_connect("/ws", headers=AUTH_HEADERS) as ws:
        ws.send_json({
            "action": "resume",
            "session_id": "s1",
            "last_gateway_sequences": {"s1": "bad"},
        })
        msg = ws.receive_json()

    assert connections.replays == [("s1", 0)]
    assert msg["gateway_sequence"] == 1


def test_followup_answer_waits_for_gateway_resolution_ack(monkeypatch):
    from crew.core import followup

    resolved = []
    monkeypatch.setattr(
        followup,
        "resolve_answer",
        lambda session_id, question_id, answers: (
            resolved.append((session_id, question_id, answers)) or True
        ),
    )
    connections = _ReplayConnections()
    client = _client(connections)

    with client.websocket_connect("/ws", headers=AUTH_HEADERS) as ws:
        ws.send_json({
            "action": "followup_answer",
            "session_id": "s1",
            "question_id": "permission-1",
            "answers": [{"question_id": "permission", "answers": ["allow_once"]}],
        })
        ack = ws.receive_json()

    assert resolved == [(
        "s1",
        "permission-1",
        [{"question_id": "permission", "answers": ["allow_once"]}],
    )]
    assert ack["kind"] == "followup_question"
    assert ack["body"] == {
        "question_id": "permission-1",
        "status": "resolved",
        "accepted": True,
        "note": "",
    }
