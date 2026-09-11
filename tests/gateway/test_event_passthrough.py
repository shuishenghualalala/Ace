"""CREW_GATEWAY_FEATURE_EVENT_PASSTHROUGH 出口透传灰度开关的契约测试。

覆盖：
- 开关解析：布尔环境变量归一，缺省关闭；
- 关闭（默认）：push_payload 出口与回放缓存维持旧协议，逐字节与既有
  test_event_compat 契约一致；
- 开启：feature_event 帧原样透传（已登记事件不再转换、未登记的插件事件
  不再丢弃），回放缓存与直发帧形态一致；
- 开关关闭后出口恢复现状。

测试通过 monkeypatch 替换 connections 模块级缓存开关，不依赖进程环境，
互不泄漏（测试结束时自动还原）。
"""

from __future__ import annotations

import pytest

import crew.gateway.connections as connections_module
from crew.core.envelope import feature_event_body
from crew.gateway.connections import ConnectionManager

_SWITCH = "CREW_GATEWAY_FEATURE_EVENT_PASSTHROUGH"


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


class TestSwitchResolution:
    """开关解析：布尔归一，缺省关闭；进程级只在模块加载时读一次环境变量。"""

    def test_unset_env_defaults_to_off(self, monkeypatch):
        monkeypatch.delenv(_SWITCH, raising=False)
        assert connections_module._env_flag_enabled(_SWITCH) is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_truthy_values_enable(self, monkeypatch, raw):
        monkeypatch.setenv(_SWITCH, raw)
        assert connections_module._env_flag_enabled(_SWITCH) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
    def test_other_values_keep_off(self, monkeypatch, raw):
        monkeypatch.setenv(_SWITCH, raw)
        assert connections_module._env_flag_enabled(_SWITCH) is False


class TestPassthroughOffMatchesLegacy:
    """开关关闭（默认路径）：出口与旧协议契约逐字节一致。"""

    @pytest.fixture(autouse=True)
    def _switch_off(self, monkeypatch):
        monkeypatch.setattr(connections_module, "FEATURE_EVENT_PASSTHROUGH", False)

    async def test_registered_event_converts_and_sends_legacy_frame(self):
        cm, ws = _connected_manager()
        frame = _feature_frame("wiki", "cards", {"pages": [{"title": "T"}]})

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        # 整帧相等：旧 kind + 透传帧级字段 + 出口分配的 gateway_sequence
        assert ws.sent == [
            {
                "kind": "wiki_cards",
                "body": {"pages": [{"title": "T"}]},
                "is_final": False,
                "sequence": 0,
                "request_id": "req_1",
                "session_id": "s1",
                "gateway_sequence": 1,
            }
        ]

    async def test_unregistered_plugin_event_dropped(self):
        cm, ws = _connected_manager()

        await cm.push_payload(
            "s1",
            _feature_frame("plugin_demo", "custom_signal", {"n": 1}),
            owner_account_id="owner-1",
        )

        assert ws.sent == []

    async def test_replay_serves_legacy_frames(self):
        cm = ConnectionManager(min_interval=0)
        await cm.push_payload(
            "s1",
            _feature_frame("team", "internal_message", {"text": "hi"}),
            owner_account_id="owner-1",
        )
        ws = _FakeSocket()

        await cm.replay("s1", ws, owner_account_id="owner-1")

        assert [frame["kind"] for frame in ws.sent] == ["team_internal"]
        assert ws.sent[0]["body"] == {"text": "hi"}

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


class TestPassthroughOn:
    """开关开启：feature_event 帧原样透传，未登记事件不再丢弃。"""

    @pytest.fixture(autouse=True)
    def _switch_on(self, monkeypatch):
        monkeypatch.setattr(connections_module, "FEATURE_EVENT_PASSTHROUGH", True)

    async def test_registered_event_passes_through_unchanged(self):
        cm, ws = _connected_manager()
        frame = _feature_frame("wiki", "cards", {"pages": [{"title": "T"}]})

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        # 已登记事件不再转成旧 kind：帧与入参一致，仅追加 gateway_sequence
        assert ws.sent == [{**frame, "gateway_sequence": 1}]
        assert ws.sent[0]["body"] == feature_event_body("wiki", "cards", {"pages": [{"title": "T"}]})

    async def test_kanban_body_not_rebuilt(self):
        cm, ws = _connected_manager()
        frame = _feature_frame("kanban", "started", {"workflow_id": "wf1"})

        await cm.push_payload("s1", frame, owner_account_id="owner-1")

        # 旧协议出口会在 kanban body 顶部重建 event 字段；透传模式保持中立 payload
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

    async def test_unknown_version_not_dropped(self):
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


class TestSwitchToggle:
    """开关关闭即恢复现状：同进程内切换开关，出口行为随之切换。"""

    async def test_disabling_restores_legacy_contract(self, monkeypatch):
        monkeypatch.setattr(connections_module, "FEATURE_EVENT_PASSTHROUGH", True)
        cm, ws = _connected_manager()
        await cm.push_payload(
            "s1",
            _feature_frame("plugin_demo", "custom_signal", {"n": 1}),
            owner_account_id="owner-1",
        )
        assert ws.sent[0]["kind"] == "feature_event"

        monkeypatch.setattr(connections_module, "FEATURE_EVENT_PASSTHROUGH", False)
        await cm.push_payload(
            "s1",
            _feature_frame("plugin_demo", "custom_signal", {"n": 2}),
            owner_account_id="owner-1",
        )
        await cm.push_payload(
            "s1",
            _feature_frame("wiki", "cards", {"pages": []}),
            owner_account_id="owner-1",
        )

        # 关闭后恢复：未登记事件回到丢弃（不占用 gateway_sequence，与既有
        # 丢弃契约一致），已登记事件回到旧 kind 转换
        assert len(ws.sent) == 2
        assert ws.sent[1]["kind"] == "wiki_cards"
        assert ws.sent[1]["gateway_sequence"] == 2
