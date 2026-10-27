"""Gateway WS 出口契约测试：出口只认 envelope，feature_event 原帧透传。

历史背景：迁移期出口曾有 event_compat 适配器把 feature_event 转回旧 kind 帧
（未登记事件丢弃），由灰度开关控制；无旧客户端需要兼容后适配器与开关一并
删除，透传成为唯一行为。本文件固定删除后的出口契约：

- 已登记来源的 feature_event 原帧到达（不做任何 kind 转换）；
- 未登记的插件事件不被丢弃，原样到达客户端；
- 出口不做版本门控（任何 version 的 feature_event 都透传）；
- kanban 事件 body 保持中立 payload，出口不重建 event 字段；
- 非 feature_event 帧原样透传，仅追加出口分配的 gateway_sequence；
- 断线回放缓存与直发帧逐字节一致。
"""

from __future__ import annotations

from crew.core.envelope import feature_event_body
from crew.gateway.connections import ConnectionManager


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


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _connected_manager(owner: str = "owner-1", session: str = "s1") -> tuple[ConnectionManager, _FakeSocket]:
    cm = ConnectionManager(min_interval=0)
    ws = _FakeSocket()
    cm._conns[(owner, session)].add(ws)  # noqa: SLF001 - 出口契约测试直接装配连接
    return cm, ws


class TestOutboundContract:
    """WS 出口契约：feature_event 原帧透传，回放与直发同形态。"""

    async def test_feature_event_passes_through_unchanged(self):
        cm, ws = _connected_manager()
        frame = _feature_frame("wiki", "cards", {"pages": [{"title": "T"}]})

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        # 帧与入参一致，仅追加出口分配的 gateway_sequence
        assert ws.sent == [{**frame, "gateway_sequence": 1}]
        assert ws.sent[0]["body"] == feature_event_body("wiki", "cards", {"pages": [{"title": "T"}]})

    async def test_kanban_body_not_rebuilt(self):
        cm, ws = _connected_manager()
        frame = _feature_frame("kanban", "started", {"workflow_id": "wf1"})

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        # 旧协议出口会在 kanban body 顶部重建 event 字段；透传契约保持中立 payload
        assert ws.sent[0]["body"] == feature_event_body("kanban", "started", {"workflow_id": "wf1"})
        assert "event" not in ws.sent[0]["body"]["payload"]

    async def test_unregistered_plugin_event_reaches_client(self):
        cm, ws = _connected_manager()

        await cm.push_payload(
            "s1",
            _feature_frame("plugin_demo", "custom_signal", {"n": 7}),
            owner_account_id="owner-1",
        )

        assert len(ws.sent) == 1
        frame = ws.sent[0]
        assert frame["kind"] == "feature_event"
        assert frame["body"]["feature"] == "plugin_demo"
        assert frame["body"]["event"] == "custom_signal"
        assert frame["body"]["payload"] == {"n": 7}

    async def test_any_version_not_filtered(self):
        # 出口不做版本门控：登记表已不存在，任何 version 都透传
        cm, ws = _connected_manager()
        frame = _feature_frame("team", "internal_message", {})
        frame["body"]["version"] = 2

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        assert ws.sent == [{**frame, "gateway_sequence": 1}]

    async def test_non_feature_frames_unaffected(self):
        cm, ws = _connected_manager()
        payload = {
            "kind": "delta",
            "body": {"text": "hi"},
            "is_final": False,
            "sequence": 0,
            "request_id": "req_1",
            "session_id": "s1",
        }

        await cm.push_payload("s1", payload, owner_account_id="owner-1")

        assert ws.sent == [{**payload, "gateway_sequence": 1}]

    async def test_replay_form_matches_live_form(self):
        # 断线期间推送入缓存后回放：回放帧与直发帧逐字节一致
        offline = ConnectionManager(min_interval=0)
        frames = [
            _feature_frame("wiki", "cards", {"pages": []}),
            _feature_frame("plugin_demo", "custom_signal", {"n": 1}),
        ]
        for frame in frames:
            await offline.push_payload("s1", frame, owner_account_id="owner-1")
        replay_ws = _FakeSocket()
        await offline.replay("s1", replay_ws, owner_account_id="owner-1")

        live_cm, live_ws = _connected_manager()
        for frame in frames:
            await live_cm.push_payload("s1", frame, owner_account_id="owner-1")

        assert replay_ws.sent == live_ws.sent
        assert replay_ws.sent == [
            {**frame, "gateway_sequence": seq} for seq, frame in enumerate(frames, start=1)
        ]
