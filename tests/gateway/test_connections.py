"""ConnectionManager 推送、限流与断线回放测试。"""

from __future__ import annotations

import asyncio
import time

import pytest

from crew.gateway.connections import ConnectionManager


class _FakeWS:
    """模拟 FastAPI WebSocket，记录 send_json 调用。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


@pytest.fixture
def conn():
    return ConnectionManager(min_interval=0)


@pytest.mark.asyncio
async def test_push_assigns_gateway_sequence_and_buffers(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")

    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}})
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "b"}})

    # 推送成功
    assert len(ws.sent) == 2
    # gateway_sequence 单调递增
    assert ws.sent[0]["gateway_sequence"] == 1
    assert ws.sent[1]["gateway_sequence"] == 2
    # 缓存中保有副本
    assert len(conn._chunk_buffers[key]) == 2
    assert conn._chunk_buffers[key][0]["gateway_sequence"] == 1
    assert conn._chunk_buffers[key][1]["gateway_sequence"] == 2


@pytest.mark.asyncio
async def test_buffer_survives_disconnect(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}})

    # 模拟断线：注销 socket
    conn.unregister_all(ws, {"s1"})
    assert not conn.has_connection("s1")

    # 断线期间继续产生 chunk
    await conn.push_payload("s1", {"kind": "tool", "body": {"name": "x"}})
    await conn.push_payload("s1", {"kind": "final", "body": {"text": "done"}})

    # 缓存保留
    assert len(conn._chunk_buffers[key]) == 3

    # 新连接注册并回放
    ws2 = _FakeWS()
    conn.register("s1", ws2)
    await conn.replay("s1", ws2, after_gateway_sequence=1)

    # 只回放 > 1 的帧
    assert len(ws2.sent) == 2
    assert ws2.sent[0]["kind"] == "tool"
    assert ws2.sent[1]["kind"] == "final"


@pytest.mark.asyncio
async def test_replay_respects_after_sequence(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    for i in range(5):
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": str(i)}})

    ws2 = _FakeWS()
    conn.register("s1", ws2)
    await conn.replay("s1", ws2, after_gateway_sequence=3)

    # 只收到 4、5（gateway_sequence 4, 5）
    assert [p["gateway_sequence"] for p in ws2.sent] == [4, 5]


@pytest.mark.asyncio
async def test_replay_survives_concurrent_push_append(conn: ConnectionManager):
    """回放持锁逐帧 await send 期间，push_payload 在锁外向同一 deque append。

    直接迭代 deque 会抛 'deque mutated during iteration' 并拆掉整条 WS；
    快照迭代免疫，且回放期间新到的帧应由 push_payload 在回放结束后实时补发。
    """
    ws = _FakeWS()
    conn.register("s1", ws)
    for i in range(3):
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": str(i)}})

    class _SlowWS(_FakeWS):
        async def send_json(self, data: dict) -> None:
            await asyncio.sleep(0)  # 让出事件循环，制造并发 append 窗口
            await super().send_json(data)

    ws2 = _SlowWS()
    conn.register("s1", ws2)

    async def _concurrent_push() -> None:
        await asyncio.sleep(0)
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": "late"}})

    await asyncio.gather(
        conn.replay("s1", ws2, after_gateway_sequence=0),
        _concurrent_push(),
    )

    assert [p["body"]["text"] for p in ws2.sent] == ["0", "1", "2", "late"]


@pytest.mark.asyncio
async def test_same_session_id_is_buffered_per_owner(conn: ConnectionManager):
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    conn.register("same", ws_a, owner_account_id="A:uid-a")
    conn.register("same", ws_b, owner_account_id="B:uid-b")

    await conn.push_payload("same", {"kind": "delta", "body": {"text": "a"}}, owner_account_id="A:uid-a")
    await conn.push_payload("same", {"kind": "delta", "body": {"text": "b"}}, owner_account_id="B:uid-b")

    assert [p["body"]["text"] for p in ws_a.sent] == ["a"]
    assert [p["body"]["text"] for p in ws_b.sent] == ["b"]
    assert conn._chunk_buffers[("A:uid-a", "same")][0]["gateway_sequence"] == 1
    assert conn._chunk_buffers[("B:uid-b", "same")][0]["gateway_sequence"] == 1


@pytest.mark.asyncio
async def test_push_accepts_owner_account_id(conn: ConnectionManager):
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    conn.register("same", ws_a, owner_account_id="A:uid-a")
    conn.register("same", ws_b, owner_account_id="B:uid-b")

    class Chunk:
        kind = "delta"
        body = {"text": "owned"}
        is_final = False
        sequence = 0
        request_id = "req"

    await conn.push("same", Chunk(), owner_account_id="B:uid-b")

    assert ws_a.sent == []
    assert [p["body"]["text"] for p in ws_b.sent] == ["owned"]


@pytest.mark.asyncio
async def test_notify_owner_reaches_owner_socket_without_session_subscription(conn: ConnectionManager):
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    conn.register_owner("A:uid-a", ws_a)
    conn.register_owner("B:uid-b", ws_b)

    await conn.notify_owner(
        "A:uid-a",
        {
            "kind": "channel_session_updated",
            "body": {"platform": "feishu", "session_id": "agent:main:feishu:dm:u1"},
            "session_id": "agent:main:feishu:dm:u1",
        },
    )

    assert len(ws_a.sent) == 1
    assert ws_a.sent[0]["kind"] == "channel_session_updated"
    assert ws_b.sent == []


@pytest.mark.asyncio
async def test_unregister_all_removes_owner_subscription(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register_owner("A:uid-a", ws)
    conn.unregister_all(ws, set(), owner_account_id="A:uid-a")

    await conn.notify_owner("A:uid-a", {"kind": "channel_session_updated"})

    assert ws.sent == []


@pytest.mark.asyncio
async def test_close_owner_drops_sockets_pending_frames_and_replay_buffer(conn: ConnectionManager):
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    conn.register_owner("A:uid-a", ws_a)
    conn.register("same", ws_a, owner_account_id="A:uid-a")
    conn.register_owner("B:uid-b", ws_b)
    conn.register("same", ws_b, owner_account_id="B:uid-b")
    await conn.push_payload(
        "same", {"kind": "delta", "body": {"text": "a"}}, owner_account_id="A:uid-a"
    )
    await conn.push_payload(
        "same", {"kind": "delta", "body": {"text": "b"}}, owner_account_id="B:uid-b"
    )

    closed = await conn.close_owner("A:uid-a", code=4401, reason="Login required")

    assert closed == 1
    assert ws_a.closed is True
    assert ws_a.close_code == 4401
    assert conn._owner_conns.get("A:uid-a") is None
    assert conn._conns.get(("A:uid-a", "same")) is None
    assert conn._chunk_buffers.get(("A:uid-a", "same")) is None
    assert conn.has_connection("same", owner_account_id="B:uid-b") is True
    assert len(conn._chunk_buffers[("B:uid-b", "same")]) == 1


@pytest.mark.asyncio
async def test_notify_owner_removes_dead_owner_socket(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register_owner("A:uid-a", ws)
    conn.register("s1", ws, owner_account_id="A:uid-a")
    ws.closed = True

    await conn.notify_owner("A:uid-a", {"kind": "channel_session_updated"})

    assert conn._owner_conns.get("A:uid-a") is None
    assert conn._conns.get(("A:uid-a", "s1")) is None


@pytest.mark.asyncio
async def test_clear_buffer_clears_payloads_but_keeps_sequence(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}})
    key = ("", "s1")
    assert conn._gateway_seq[key] == 1
    assert len(conn._chunk_buffers[key]) == 1

    conn.clear_buffer("s1")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "b"}})

    # 清空缓存后 sequence 保持单调递增，不重置；缓存只保留新帧
    assert conn._gateway_seq[key] == 2
    assert ws.sent[-1]["gateway_sequence"] == 2
    assert len(conn._chunk_buffers[key]) == 1


@pytest.mark.asyncio
async def test_register_resets_consecutive_failures(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")
    conn._consecutive_failures[key] = 5

    conn.unregister_all(ws, {"s1"})
    # 默认 cleanup 会清掉失败计数
    ws2 = _FakeWS()
    conn.register("s1", ws2)
    assert conn._consecutive_failures.get(key) is None


@pytest.mark.asyncio
async def test_replay_stops_on_dead_socket(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    for i in range(3):
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": str(i)}})

    dead = _FakeWS()
    dead.closed = True
    await conn.replay("s1", dead, after_gateway_sequence=0)

    # 遇到死 socket 应立即停止，不抛异常
    assert dead.sent == []


@pytest.mark.asyncio
async def test_replay_filter_skips_filtered_payloads(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws)
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "keep"}})
    await conn.push_payload("s1", {"kind": "followup_question", "body": {"question_id": "q1"}})
    await conn.push_payload("s1", {"kind": "final", "body": {"text": "done"}})

    ws2 = _FakeWS()
    conn.register("s1", ws2)
    await conn.replay(
        "s1",
        ws2,
        after_gateway_sequence=0,
        filter_fn=lambda p: p.get("kind") != "followup_question",
    )

    assert [p["kind"] for p in ws2.sent] == ["delta", "final"]


def test_merge_pending_delta_keeps_request_ids_separate(conn: ConnectionManager):
    payloads = [
        {"kind": "delta", "request_id": "req-1", "sequence": 1, "body": {"text": "a", "delta_start": 1, "delta_end": 1}},
        {"kind": "delta", "request_id": "req-1", "sequence": 2, "body": {"text": "b", "delta_start": 2, "delta_end": 2}},
        {"kind": "delta", "request_id": "req-2", "sequence": 3, "body": {"text": "x", "delta_start": 3, "delta_end": 3}},
        {"kind": "delta", "request_id": "req-2", "sequence": 4, "body": {"text": "y", "delta_start": 4, "delta_end": 4}},
    ]

    merged = conn._merge_pending_payloads(payloads)

    assert [(p["request_id"], p["body"]["text"]) for p in merged] == [
        ("req-1", "ab"),
        ("req-2", "xy"),
    ]
    assert [(p["body"]["delta_start"], p["body"]["delta_end"]) for p in merged] == [
        (1, 2),
        (3, 4),
    ]


def test_merge_pending_delta_adds_sequence_range_without_body_metadata(conn: ConnectionManager):
    payloads = [
        {"kind": "delta", "request_id": "req-1", "sequence": 5, "body": {"text": "hello "}},
        {"kind": "delta", "request_id": "req-1", "sequence": 6, "body": {"text": "world"}},
    ]

    merged = conn._merge_pending_payloads(payloads)

    assert len(merged) == 1
    assert merged[0]["body"]["text"] == "hello world"
    assert merged[0]["body"]["delta_start"] == 5
    assert merged[0]["body"]["delta_end"] == 6


def test_merge_pending_thinking_keeps_latest_snapshot_per_request(conn: ConnectionManager):
    """thinking 是累计全文快照；限流窗口内只需发送同一 request 的最新状态。"""
    payloads = [
        {"kind": "thinking", "request_id": "req-1", "sequence": 1, "body": {"text": "先"}},
        {"kind": "thinking", "request_id": "req-1", "sequence": 2, "body": {"text": "先分析"}},
        {"kind": "thinking", "request_id": "req-2", "sequence": 3, "body": {"text": "另"}},
        {"kind": "thinking", "request_id": "req-2", "sequence": 4, "body": {"text": "另一轮"}},
    ]

    merged = conn._merge_pending_payloads(payloads)

    assert [(p["request_id"], p["sequence"], p["body"]["text"]) for p in merged] == [
        ("req-1", 2, "先分析"),
        ("req-2", 4, "另一轮"),
    ]


def test_merge_pending_thinking_preserves_mixed_event_order(conn: ConnectionManager):
    """非 thinking 事件切断合并区间，不能被最新快照跨事件越过。"""
    payloads = [
        {"kind": "thinking", "request_id": "req", "sequence": 1, "body": {"text": "A"}},
        {"kind": "thinking", "request_id": "req", "sequence": 2, "body": {"text": "AB"}},
        {"kind": "status", "request_id": "req", "sequence": 3, "body": {"text": "执行中"}},
        {"kind": "thinking", "request_id": "req", "sequence": 4, "body": {"text": "ABC"}},
        {"kind": "thinking", "request_id": "req", "sequence": 5, "body": {"text": "ABCD"}},
    ]

    merged = conn._merge_pending_payloads(payloads)

    assert [(p["kind"], p["sequence"]) for p in merged] == [
        ("thinking", 2),
        ("status", 3),
        ("thinking", 5),
    ]


@pytest.mark.asyncio
async def test_pending_payloads_are_not_bypassed_when_rate_window_reopens():
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")

    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "A"}, "is_final": False})
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "B"}, "is_final": False})
    conn._last_push_ts[key] = 0
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "C"}, "is_final": False})
    await conn._flush_pending(key)

    assert [p["body"]["text"] for p in ws.sent] == ["A", "BC"]


@pytest.mark.asyncio
async def test_final_flushes_pending_deltas_before_final():
    """final 到达时，pending delta 合并帧应先于 final 帧推送。"""
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")
    # 让后续 delta 进入限流缓存而非立即推送
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload("s1", {"kind": "delta", "sequence": 1, "body": {"text": "A"}})
    await conn.push_payload("s1", {"kind": "delta", "sequence": 2, "body": {"text": "B"}})
    await conn.push_payload(
        "s1", {"kind": "final", "body": {"text": "AB"}, "is_final": True}
    )

    # 合并后的 delta 在 final 之前到达
    assert [p["kind"] for p in ws.sent] == ["delta", "final"]
    delta_payload = ws.sent[0]
    assert delta_payload["body"]["text"] == "AB"
    assert delta_payload["body"]["delta_start"] == 1
    assert delta_payload["body"]["delta_end"] == 2
    assert delta_payload["gateway_sequence"] == 2
    final_payload = ws.sent[1]
    assert final_payload["kind"] == "final"
    assert final_payload["gateway_sequence"] == 3


@pytest.mark.asyncio
async def test_tool_start_flushes_pending_and_bypasses_rate_limit():
    """tool/start 是用户可感知控制帧：先刷新 pending delta，再立即推送工具开始。"""
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload("s1", {"kind": "delta", "sequence": 1, "body": {"text": "准备"}})
    assert ws.sent == []
    assert key in conn._pending_payloads

    await conn.push_payload(
        "s1",
        {
            "kind": "tool",
            "sequence": 2,
            "body": {"phase": "start", "tool_call_id": "t1", "name": "file_write"},
        },
    )

    assert [p["kind"] for p in ws.sent] == ["delta", "tool"]
    assert ws.sent[0]["body"]["text"] == "准备"
    assert ws.sent[1]["body"]["phase"] == "start"
    assert ws.sent[1]["body"]["name"] == "file_write"
    assert key not in conn._pending_payloads


@pytest.mark.asyncio
async def test_tool_result_remains_rate_limited():
    """只有 tool/start 绕过限流；tool/result 继续受限流保护。"""
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws)
    key = ("", "s1")
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload(
        "s1",
        {
            "kind": "tool",
            "sequence": 1,
            "body": {"phase": "result", "tool_call_id": "t1", "name": "file_write"},
        },
    )

    assert ws.sent == []
    assert conn._pending_payloads[key][0]["body"]["phase"] == "result"
    conn.unregister_all(ws, {"s1"})
    await asyncio.sleep(0)


def test_tool_planning_status_is_priority_payload():
    assert ConnectionManager._is_priority_payload({
        "kind": "status",
        "body": {"activity": "tool_planning", "message": "正在规划工具调用…"},
    })
