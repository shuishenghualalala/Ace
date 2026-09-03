"""Feature Event 命名空间与旧帧出口适配的契约测试。

覆盖：
- ResponseChunk.feature_event / feature_event_body 的信封结构；
- event_compat 的映射、字段透传与未登记事件丢弃；
- connections.push_payload 出口：feature_event 帧在回放缓存与线上均为旧 kind。
"""

from __future__ import annotations

import pytest

from crew.core.envelope import ResponseChunk, feature_event_body
from crew.gateway.connections import ConnectionManager
from crew.gateway.event_compat import expand_outgoing_payload


def _feature_frame(feature: str, event: str, payload: dict, **fields: object) -> dict:
    frame = {
        "kind": "feature_event",
        "body": feature_event_body(feature, event, payload),
        "is_final": False,
        "sequence": 0,
        "request_id": "req_1",
        "session_id": "s1",
    }
    frame.update(fields)
    return frame


class TestFeatureEventEnvelope:
    def test_response_chunk_factory(self):
        chunk = ResponseChunk.feature_event("req_1", "wiki", "cards", {"pages": []})
        assert chunk.kind == "feature_event"
        assert chunk.body == {
            "feature": "wiki",
            "event": "cards",
            "version": 1,
            "payload": {"pages": []},
        }

    def test_feature_event_body_defaults(self):
        assert feature_event_body("team", "internal_message") == {
            "feature": "team",
            "event": "internal_message",
            "version": 1,
            "payload": {},
        }


class TestExpandOutgoingPayload:
    def test_non_feature_event_passthrough(self):
        payload = {"kind": "delta", "body": {"text": "hi"}}
        assert expand_outgoing_payload(payload) == [payload]

    @pytest.mark.parametrize(
        ("feature", "event", "legacy_kind"),
        [
            ("team", "internal_message", "team_internal"),
            ("wiki", "cards", "wiki_cards"),
            ("wiki", "changed", "wiki_changed"),
            ("wiki", "ingest_progress", "wiki_ingest_progress"),
        ],
    )
    def test_registered_events_convert_to_legacy_kind(self, feature, event, legacy_kind):
        payload = {"text": "x"}
        frames = expand_outgoing_payload(_feature_frame(feature, event, payload))
        assert len(frames) == 1
        frame = frames[0]
        assert frame["kind"] == legacy_kind
        assert frame["body"] == payload
        # 帧级字段透传
        assert frame["request_id"] == "req_1"
        assert frame["session_id"] == "s1"
        assert frame["is_final"] is False
        assert frame["sequence"] == 0

    def test_unknown_event_dropped(self, caplog):
        frames = expand_outgoing_payload(_feature_frame("wiki", "future_event", {}))
        assert frames == []

    def test_unknown_version_dropped(self):
        # version=2 未登记，不能与 v1 的旧帧混发
        frame = _feature_frame("team", "internal_message", {})
        frame["body"]["version"] = 2
        assert expand_outgoing_payload(frame) == []


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class TestPushPayloadConversion:
    @pytest.mark.asyncio
    async def test_feature_event_buffered_and_sent_as_legacy_kind(self):
        cm = ConnectionManager(min_interval=0)
        ws = _FakeSocket()
        cm._conns[("owner-1", "s1")].add(ws)  # noqa: SLF001 - 出口契约测试直接装配连接

        await cm.push_payload(
            "s1",
            _feature_frame("wiki", "cards", {"pages": [{"title": "T"}]}),
            owner_account_id="owner-1",
        )

        assert len(ws.sent) == 1
        frame = ws.sent[0]
        assert frame["kind"] == "wiki_cards"
        assert frame["body"] == {"pages": [{"title": "T"}]}
        assert frame["gateway_sequence"] == 1

    @pytest.mark.asyncio
    async def test_replay_serves_legacy_kind(self):
        cm = ConnectionManager(min_interval=0)
        # 无活跃连接时入回放缓存
        await cm.push_payload(
            "s1",
            _feature_frame("team", "internal_message", {"text": "hi"}),
            owner_account_id="owner-1",
        )
        ws = _FakeSocket()
        await cm.replay("s1", ws, owner_account_id="owner-1")
        assert [frame["kind"] for frame in ws.sent] == ["team_internal"]
        assert ws.sent[0]["body"] == {"text": "hi"}

    @pytest.mark.asyncio
    async def test_unknown_feature_event_not_buffered(self):
        cm = ConnectionManager(min_interval=0)
        await cm.push_payload(
            "s1",
            _feature_frame("wiki", "not_registered", {}),
            owner_account_id="owner-1",
        )
        ws = _FakeSocket()
        await cm.replay("s1", ws, owner_account_id="owner-1")
        assert ws.sent == []


class TestTeamInternalChunkContract:
    def test_team_internal_chunk_emits_feature_event(self):
        from crew.team.team_manager import InProcessTeamManager

        chunk = InProcessTeamManager._team_internal_chunk(
            "req_1",
            agent_id="leader",
            text="你好",
            is_leader=True,
        )
        assert chunk.kind == "feature_event"
        assert chunk.body["feature"] == "team"
        assert chunk.body["event"] == "internal_message"
        assert chunk.body["version"] == 1
        payload = chunk.body["payload"]
        assert payload["text"] == "你好"
        assert payload["agent_id"] == "leader"
        assert payload["is_leader"] is True

        # 出口转换为旧帧后，线上 body 与原 team_internal body 一致
        frame = expand_outgoing_payload(
            {
                "kind": "feature_event",
                "body": chunk.body,
                "request_id": "req_1",
                "session_id": "s1",
            }
        )[0]
        assert frame["kind"] == "team_internal"
        assert frame["body"]["text"] == "你好"
        assert frame["request_id"] == "req_1"
