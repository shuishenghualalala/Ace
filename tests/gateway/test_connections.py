"""ConnectionManager 推送、限流与断线回放测试。"""

from __future__ import annotations

import asyncio
import time

import pytest

from crew.gateway.connections import ConnectionManager
from crew.core.followup import get_followup_waiter


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
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")

    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "b"}}, owner_account_id="")

    # 推送成功
    assert len(ws.sent) == 2
    # gateway_sequence 单调递增
    assert ws.sent[0]["gateway_sequence"] == 1
    assert ws.sent[1]["gateway_sequence"] == 2
    # 缓存中保有副本
    assert conn._journal.count(key) == 2
    assert list(conn._journal.after(key, 2, 0))[0]["gateway_sequence"] == 1
    assert list(conn._journal.after(key, 2, 0))[1]["gateway_sequence"] == 2


@pytest.mark.asyncio
async def test_buffer_survives_disconnect(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}}, owner_account_id="")

    # 模拟断线：注销 socket
    conn.unregister_all(ws, {"s1"}, owner_account_id="")
    assert not conn.has_connection("s1", owner_account_id="")

    # 断线期间继续产生 chunk
    await conn.push_payload("s1", {"kind": "tool", "body": {"name": "x"}}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "final", "body": {"text": "done"}}, owner_account_id="")

    # 缓存保留
    assert conn._journal.count(key) == 3


@pytest.mark.asyncio
async def test_replay_respects_after_sequence(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    for i in range(5):
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": str(i)}}, owner_account_id="")

    ws2 = _FakeWS()
    conn.register("s1", ws2, owner_account_id="")
    await conn.replay("s1", ws2, after_gateway_sequence=3, owner_account_id="")

    # 只收到 4、5（gateway_sequence 4, 5）
    assert [p["gateway_sequence"] for p in ws2.sent] == [4, 5]


@pytest.mark.asyncio
async def test_versioned_replay_accepts_both_edges_of_the_retained_window(monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._CHUNK_BUFFER_SIZE", 2)
    conn = ConnectionManager(min_interval=0)
    frames = [{"kind": "delta", "body": {"text": str(i)}} for i in range(3)]
    owner = "owner"
    for frame in frames:
        await conn.push_payload("s1", frame, owner_account_id=owner)
    epoch = frames[0]["gateway_epoch"]

    for sequence, expected in ((1, frames[1:]), (3, [])):
        socket = _FakeWS()
        assert await conn.replay(
            "s1", socket, owner_account_id=owner, register_for_live=True,
            after_gateway_sequence=99, resume_cursor={"epoch": epoch, "sequence": sequence},
        )
        assert socket.sent == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("sequence", "reason"), [
    (0, "cursor_expired"),
    (4, "cursor_ahead"),
    (False, "invalid_cursor"),
    (-1, "invalid_cursor"),
    ("1", "invalid_cursor"),
])
async def test_versioned_reset_hands_off_only_events_after_the_cut(monkeypatch, sequence, reason):
    monkeypatch.setattr("crew.gateway.connections._CHUNK_BUFFER_SIZE", 2)
    conn = ConnectionManager(min_interval=0)
    owner = "owner"
    frame = {"kind": "delta", "body": {"text": "older text"}}
    for _ in range(3):
        await conn.push_payload("s1", frame, owner_account_id=owner)
    epoch = frame["gateway_epoch"]
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowSubscriber(_FakeWS):
        async def send_json(self, data: dict) -> None:
            if data["kind"] == "gateway_resync":
                started.set()
                await release.wait()
            await super().send_json(data)

    socket = SlowSubscriber()
    subscribe = asyncio.create_task(conn.replay(
        "s1", socket, owner_account_id=owner, register_for_live=True,
        resume_cursor={"epoch": epoch, "sequence": sequence},
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    live = asyncio.create_task(conn.push_payload("s1", {"kind": "delta"}, owner_account_id=owner))
    await asyncio.sleep(0)
    assert not socket.sent
    release.set()
    await asyncio.wait_for(asyncio.gather(subscribe, live), timeout=1)

    assert [(f["kind"], f["gateway_sequence"], f["gateway_epoch"]) for f in socket.sent] == [
        ("gateway_resync", 3, epoch), ("delta", 4, epoch),
    ]
    assert socket.sent[0]["body"] == {"reason": reason, "floor": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_stale_epoch_rejected_even_when_number_matches_current_head(restart):
    conn = ConnectionManager(min_interval=0)
    owner = "owner"
    first = {"kind": "delta"}
    await conn.push_payload("s1", first, owner_account_id=owner)
    if restart:
        conn = ConnectionManager(min_interval=0)
    else:
        await conn.close_owner(owner)
    current = {"kind": "delta"}
    await conn.push_payload("s1", current, owner_account_id=owner)
    assert current["gateway_sequence"] == first["gateway_sequence"]
    socket = _FakeWS()

    await conn.replay(
        "s1", socket, owner_account_id=owner, register_for_live=True,
        resume_cursor={"epoch": first["gateway_epoch"], "sequence": first["gateway_sequence"]},
    )

    reset, = socket.sent
    assert reset["kind"] == "gateway_resync"
    assert reset["body"]["reason"] == "epoch_changed"
    assert reset["gateway_epoch"] == current["gateway_epoch"] != first["gateway_epoch"]


@pytest.mark.asyncio
async def test_empty_ring_preserves_recovery_floor(conn):
    owner = "owner"
    frame = {"kind": "delta"}
    await conn.push_payload("s1", frame, owner_account_id=owner)
    conn.clear_buffer("s1", owner_account_id=owner)
    socket = _FakeWS()

    await conn.replay(
        "s1", socket, owner_account_id=owner,
        resume_cursor={"epoch": frame["gateway_epoch"], "sequence": 0},
    )

    reset, = socket.sent
    assert reset["body"] == {"reason": "cursor_expired", "floor": 1}
    assert reset["gateway_sequence"] == 1


@pytest.mark.asyncio
async def test_active_snapshot_preserves_frozen_history_and_evicted_frames(monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._CHUNK_BUFFER_SIZE", 2)
    conn = ConnectionManager(min_interval=0)
    history = {
        "items": [{"role": "assistant", "content": "prior"}, {"role": "user", "content": "question"}],
        "has_more": True,
        "next_before": "s1:7",
    }
    await conn.capture_turn_history("owner", "s1", "req", 123.5, lambda: True, lambda: history)
    history["items"].append({"role": "assistant", "content": "persisted concurrently"})
    for sequence, (kind, body) in enumerate([
        ("delta", {"text": "first "}), ("delta", {"text": "second"}),
        ("tool", {"phase": "start", "id": "tool-1"}),
        ("tool", {"phase": "result", "id": "tool-1", "result": "ok"}),
        ("thinking", {"text": "ponder"}), ("delta", {"text": "third"}),
    ], 1):
        payload = {
            "kind": kind, "body": body, "sequence": sequence, "session_id": "s1", "request_id": "req",
        }
        await conn.push_payload("s1", payload, owner_account_id="owner")
    socket = _FakeWS()
    epoch = payload["gateway_epoch"]
    cursor = {"epoch": epoch, "sequence": 1}

    assert await conn.replay(
        "s1", socket, owner_account_id="owner", register_for_live=True, resume_cursor=cursor,
        snapshot_request_id=lambda: "req",
    )
    reset, = socket.sent
    assert reset["gateway_sequence"] == 6
    snapshot = reset["body"]["turn_snapshot"]
    assert snapshot["history"] == {**history, "items": history["items"][:2]}
    assert snapshot["request_id"] == "req" and snapshot["started_at"] == 123.5
    compacted = snapshot["frames"]
    assert [(f["kind"], f["gateway_sequence"]) for f in compacted] == [
        ("delta", 2), ("tool", 3), ("tool", 4), ("thinking", 5), ("delta", 6),
    ]
    assert compacted[0]["body"] == {
        "text": "first second", "delta_start": 1, "delta_end": 2, "delta_parts": [[1, 6], [2, 6]],
    }
    assert {f["gateway_epoch"] for f in compacted} == {epoch}

    cold = _FakeWS()
    assert await conn.replay("s1", cold, owner_account_id="owner", snapshot_request_id=lambda: "req")
    cold_reset, = cold.sent
    assert cold_reset == reset

    for owner, request in (("owner", None), ("other-owner", "req"), ("owner", "old-req")):
        observer = _FakeWS()
        await conn.replay(
            "s1", observer, owner_account_id=owner, resume_cursor=None,
            snapshot_request_id=lambda: request,
        )
        assert "turn_snapshot" not in observer.sent[0]["body"]

    await conn.push_payload("s1", {"kind": "final", "request_id": "req"}, owner_account_id="owner")
    assert socket.sent[-1]["gateway_sequence"] == 7
    after = _FakeWS()
    await conn.replay("s1", after, owner_account_id="owner", resume_cursor=cursor, snapshot_request_id=lambda: "req")
    assert "turn_snapshot" not in after.sent[0]["body"]


@pytest.mark.asyncio
async def test_forced_repair_reports_current_snapshot_and_settled_fallback(conn):
    owner, sid = "owner", "s1"
    history = {"items": [{"role": "user", "content": "question"}], "has_more": False}
    await conn.capture_turn_history(owner, sid, "req", 1.0, lambda: True, lambda: history)
    frame = {"kind": "delta", "request_id": "req", "body": {"text": "prefix"}}
    await conn.push_payload(sid, frame, owner_account_id=owner)
    cursor = {"epoch": frame["gateway_epoch"], "sequence": frame["gateway_sequence"]}

    observers = []
    for cursor_options in ({}, {"resume_cursor": cursor}):
        socket = _FakeWS()
        observers.append(socket)
        assert await conn.replay(
            sid, socket, owner_account_id=owner, snapshot_request_id=lambda: "req",
            register_for_live=True, repair_id="attempt_01", **cursor_options,
        )
        reset, = socket.sent
        assert (reset["kind"], reset["gateway_epoch"], reset["gateway_sequence"]) == (
            "gateway_resync", cursor["epoch"], cursor["sequence"],
        )
        assert reset["body"] == {
            "reason": "client_stream_gap", "floor": 0, "repair_id": "attempt_01",
            "turn_snapshot": {"request_id": "req", "started_at": 1.0, "history": history, "frames": [frame]},
        }

    foreign = _FakeWS()
    await conn.replay(sid, foreign, owner_account_id="other-owner", repair_id="other", snapshot_request_id=lambda: "req")
    assert foreign.sent[0]["body"] == {"reason": "client_stream_gap", "floor": 0, "repair_id": "other"}

    await conn.push_payload(sid, {"kind": "final", "request_id": "req", "is_final": True}, owner_account_id=owner)
    for observer in observers:
        assert [event["kind"] for event in observer.sent] == ["gateway_resync", "final"]
    settled = _FakeWS()
    await conn.replay(sid, settled, owner_account_id=owner, repair_id="settled", snapshot_request_id=lambda: "req")
    reset, = settled.sent
    assert reset["gateway_sequence"] == cursor["sequence"] + 1
    assert reset["body"] == {"reason": "client_stream_gap", "floor": 0, "repair_id": "settled"}


@pytest.mark.asyncio
async def test_exhausted_snapshot_budget_does_not_publish_a_partial_turn(conn, monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._MAX_ACTIVE_SNAPSHOT_BYTES", 512)
    await conn.capture_turn_history("owner", "s1", "req", 1.0, lambda: True, lambda: {"items": []})
    await conn.push_payload("s1", {
        "kind": "delta", "request_id": "req", "body": {"text": "x" * 1024},
    }, owner_account_id="owner")
    socket = _FakeWS()

    await conn.replay("s1", socket, owner_account_id="owner", resume_cursor=None, snapshot_request_id=lambda: "req")

    assert socket.sent[0]["kind"] == "gateway_resync"
    assert "turn_snapshot" not in socket.sent[0]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_answer", [False, True])
async def test_followup_history_boundary_retires_only_recorded_answer_snapshot(conn, recorded_answer):
    history = {"items": [{"role": "user", "content": "question"}], "has_more": False}
    await conn.capture_turn_history("owner", "s1", "req", 1.0, lambda: True, lambda: history)
    await conn.push_payload("s1", {"kind": "delta", "request_id": "req", "body": {"text": "Before"}}, owner_account_id="owner")
    body = {"question_id": "question", "status": "resolved"}
    if recorded_answer:
        body.update(answer_history_id="followup:question", answer_text="Selected")
    await conn.push_payload("s1", {"kind": "followup_question", "request_id": "req", "body": body}, owner_account_id="owner")
    await conn.push_payload("s1", {"kind": "delta", "request_id": "req", "body": {"text": "After"}}, owner_account_id="owner")
    observer = _FakeWS()
    await conn.replay("s1", observer, owner_account_id="owner", resume_cursor=None, snapshot_request_id=lambda: "req")
    assert ("turn_snapshot" in observer.sent[0]["body"]) is (not recorded_answer)


@pytest.mark.asyncio
async def test_resync_restores_only_still_waiting_owner_prompts_after_ring_eviction(conn):
    waiter = get_followup_waiter()
    sid = "s1"
    waiting = waiter.create(sid, owner_account_id="A")
    other_owner = waiter.create(sid, owner_account_id="B")
    cancelled = waiter.create(sid, owner_account_id="A")
    try:
        for owner, question_id in (("A", waiting), ("A", cancelled), ("B", other_owner)):
            await conn.push_payload(sid, {
                "kind": "followup_question",
                "session_id": sid,
                "body": {"question_id": question_id, "questions": [{"question": "Continue?"}]},
            }, owner_account_id=owner)
        waiter.cancel(sid, cancelled, owner_account_id="A")
        conn.clear_buffer(sid, owner_account_id="A")
        socket = _FakeWS()

        await conn.replay(sid, socket, owner_account_id="A", resume_cursor=None)

        reset, = socket.sent
        recovered, = reset["body"]["pending_followups"]
        assert recovered["body"]["question_id"] == waiting
        assert reset["body"]["reason"] == "invalid_cursor"
    finally:
        for owner, question_id in (("A", waiting), ("B", other_owner), ("A", cancelled)):
            waiter.cancel(sid, question_id, owner_account_id=owner)
            waiter.discard(sid, question_id, owner_account_id=owner)


@pytest.mark.asyncio
async def test_large_followup_is_delivered_without_retaining_an_extra_copy(conn, monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._MAX_FOLLOWUP_FRAME_BYTES", 128)
    waiter = get_followup_waiter()
    sid = "s1"
    question_id = waiter.create(sid, owner_account_id="owner")
    socket = _FakeWS()
    conn.register(sid, socket, owner_account_id="owner")
    prompt = "x" * 256
    try:
        await conn.push_payload(sid, {
            "kind": "followup_question",
            "session_id": sid,
            "body": {"question_id": question_id, "questions": [{"question": prompt}]},
        }, owner_account_id="owner")
        reset_socket = _FakeWS()
        await conn.replay(sid, reset_socket, owner_account_id="owner", resume_cursor=None)

        assert socket.sent[0]["body"]["questions"] == [{"question": prompt}]
        assert "pending_followups" not in reset_socket.sent[0]["body"]
    finally:
        waiter.cancel(sid, question_id, owner_account_id="owner")
        waiter.discard(sid, question_id, owner_account_id="owner")


@pytest.mark.asyncio
async def test_subscribe_replays_before_queued_live_push(conn: ConnectionManager):
    sending = asyncio.Event()
    release = asyncio.Event()

    class SlowObserver(_FakeWS):
        async def send_json(self, data: dict) -> None:
            sending.set()
            await release.wait()
            await super().send_json(data)

    observer = SlowObserver()
    subscriber = _FakeWS()
    conn.register("s1", observer, owner_account_id="")
    first = asyncio.create_task(conn.push_payload("s1", {"kind": "delta"}, owner_account_id=""))
    await asyncio.wait_for(sending.wait(), timeout=1)
    replay = asyncio.create_task(conn.replay("s1", subscriber, owner_account_id="", register_for_live=True))
    await asyncio.sleep(0)
    live = asyncio.create_task(conn.push_payload("s1", {"kind": "final", "is_final": True}, owner_account_id=""))
    await asyncio.wait_for(asyncio.gather(first, replay, live), timeout=1)
    assert replay.result() is True
    assert [frame["gateway_sequence"] for frame in subscriber.sent] == [1, 2]
    assert not observer.sent
    release.set()
    await asyncio.sleep(0)
    assert [frame["gateway_sequence"] for frame in observer.sent] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_socket", [False, True])
async def test_subscribe_delivers_rate_limited_frames_once(same_socket: bool):
    conn = ConnectionManager(min_interval=60)
    observer = _FakeWS()
    subscriber = _FakeWS()
    conn.register("s1", observer, owner_account_id="")
    if same_socket:
        conn.register("s1", subscriber, owner_account_id="")

    await conn.push_payload("s1", {"kind": "status"}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "status"}, owner_account_id="")
    conn.clear_buffer("s1", owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta"}, owner_account_id="")
    timer = conn._flush_tasks[("", "s1")]

    assert await conn.replay(
        "s1", subscriber, owner_account_id="", register_for_live=True,
        after_gateway_sequence=1, filter_fn=lambda frame: frame["kind"] != "status",
    )
    await asyncio.gather(timer, return_exceptions=True)
    await conn.push_payload("s1", {"kind": "final", "is_final": True}, owner_account_id="")
    assert [frame["gateway_sequence"] for frame in observer.sent] == [1, 2, 3, 4]
    assert [frame["gateway_sequence"] for frame in subscriber.sent] == ([1, 2, 3, 4] if same_socket else [3, 4])


@pytest.mark.asyncio
async def test_logout_during_replay_does_not_restore_subscription(conn: ConnectionManager):
    sending = asyncio.Event()
    release = asyncio.Event()

    class SlowSubscriber(_FakeWS):
        async def send_json(self, data: dict) -> None:
            sending.set()
            await release.wait()
            await super().send_json(data)

    socket = SlowSubscriber()
    conn.register_owner("owner", socket)
    await conn.push_payload("s1", {"kind": "delta"}, owner_account_id="owner")
    replay = asyncio.create_task(conn.replay("s1", socket, owner_account_id="owner", register_for_live=True))
    await asyncio.wait_for(sending.wait(), timeout=1)
    push = asyncio.create_task(conn.push_payload("s1", {"kind": "final"}, owner_account_id="owner"))
    await asyncio.sleep(0)
    assert await conn.close_owner("owner") == 1
    release.set()

    await asyncio.wait_for(asyncio.gather(replay, push), timeout=1)
    assert replay.result() is False
    assert not conn.has_connection("s1", owner_account_id="owner")
    assert conn._journal.count(("owner", "s1")) == 0


@pytest.mark.asyncio
async def test_logout_during_flush_does_not_deliver_old_frames_to_new_login():
    conn = ConnectionManager(min_interval=60)
    sending = asyncio.Event()
    release = asyncio.Event()

    class SlowObserver(_FakeWS):
        async def send_json(self, data: dict) -> None:
            if data["gateway_sequence"] == 2:
                sending.set()
                await release.wait()
            await super().send_json(data)

    observer = SlowObserver()
    conn.register_owner("owner", observer)
    conn.register("s1", observer, owner_account_id="owner")
    for kind in ("delta", "status", "tool"):
        await conn.push_payload("s1", {"kind": kind}, owner_account_id="owner")
    final = asyncio.create_task(conn.push_payload(
        "s1", {"kind": "final", "is_final": True}, owner_account_id="owner",
    ))
    await asyncio.wait_for(sending.wait(), timeout=1)
    assert await conn.close_owner("owner") == 1
    new_socket = _FakeWS()
    conn.register_owner("owner", new_socket)
    assert await conn.replay("s1", new_socket, owner_account_id="owner", register_for_live=True)
    release.set()

    await asyncio.wait_for(final, timeout=1)
    assert new_socket.sent == []
    await conn.push_payload("s1", {"kind": "final", "is_final": True}, owner_account_id="owner")
    assert [frame["gateway_sequence"] for frame in new_socket.sent] == [1]


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
    assert next(conn._journal.after(("A:uid-a", "same"), 1, 0))["gateway_sequence"] == 1
    assert next(conn._journal.after(("B:uid-b", "same"), 1, 0))["gateway_sequence"] == 1


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
    assert conn._journal.count(("A:uid-a", "same")) == 0
    assert conn.has_connection("same", owner_account_id="B:uid-b") is True
    assert conn._journal.count(("B:uid-b", "same")) == 1


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
    conn.register("s1", ws, owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "a"}}, owner_account_id="")
    key = ("", "s1")
    assert conn._gateway_seq[key] == 1
    assert conn._journal.count(key) == 1

    conn.clear_buffer("s1", owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "b"}}, owner_account_id="")

    # 清空缓存后 sequence 保持单调递增，不重置；缓存只保留新帧
    assert conn._gateway_seq[key] == 2
    assert ws.sent[-1]["gateway_sequence"] == 2
    assert conn._journal.count(key) == 1


@pytest.mark.asyncio
async def test_register_resets_consecutive_failures(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")
    conn._consecutive_failures[key] = 5

    conn.unregister_all(ws, {"s1"}, owner_account_id="")
    # 默认 cleanup 会清掉失败计数
    ws2 = _FakeWS()
    conn.register("s1", ws2, owner_account_id="")
    assert conn._consecutive_failures.get(key) is None


@pytest.mark.asyncio
async def test_replay_stops_on_dead_socket(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    for i in range(3):
        await conn.push_payload("s1", {"kind": "delta", "body": {"text": str(i)}}, owner_account_id="")

    dead = _FakeWS()
    dead.closed = True
    assert not await conn.replay("s1", dead, owner_account_id="", register_for_live=True)

    # 遇到死 socket 应立即停止，不抛异常
    assert dead.sent == []
    assert dead not in conn._conns[("", "s1")]


@pytest.mark.asyncio
async def test_replay_filter_skips_filtered_payloads(conn: ConnectionManager):
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "keep"}}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "followup_question", "body": {"question_id": "q1"}}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "final", "body": {"text": "done"}}, owner_account_id="")

    ws2 = _FakeWS()
    conn.register("s1", ws2, owner_account_id="")
    await conn.replay(
        "s1",
        ws2,
        after_gateway_sequence=0,
        filter_fn=lambda p: p.get("kind") != "followup_question",
    owner_account_id="",
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
    assert "delta_parts" not in merged[0]["body"]


def test_delta_parts_preserve_utf16_frame_boundaries_and_cap(conn, monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._MAX_DELTA_PARTS", 3)
    texts = ["A", "\u4e2d\U0001f4a1", "", "tail"]
    frames = [
        {"kind": "delta", "gateway_sequence": seq, "body": {"text": body}}
        for seq, body in zip((2, 5, 9, 10), texts, strict=True)
    ]

    merged, single = conn._merge_pending_payloads(frames)
    assert merged["gateway_sequence"] == 9
    assert merged["body"] == {"text": "".join(texts[:3]), "delta_parts": [[2, 1], [5, 3], [9, 0]]}
    assert single == frames[-1]
    assert all("delta_parts" not in frame["body"] for frame in frames)
    assert conn._merge_pending_payloads([merged, single]) == [merged, single]


@pytest.mark.parametrize("earlier", [None, True, -1, 3, 4, 2**53])
def test_legacy_merge_stays_unannotated_with_invalid_exact_sequence(conn, earlier):
    frames = [
        {"kind": "delta", "gateway_sequence": earlier, "sequence": 1, "body": {"text": "first"}},
        {"kind": "delta", "gateway_sequence": 3, "sequence": 2, "body": {"text": "second"}},
    ]
    merged, = conn._merge_pending_payloads(frames)
    assert merged["body"] == {"text": "firstsecond", "delta_start": 1, "delta_end": 2}


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
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")

    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "A"}, "is_final": False}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "B"}, "is_final": False}, owner_account_id="")
    conn._last_push_ts[key] = 0
    await conn.push_payload("s1", {"kind": "delta", "body": {"text": "C"}, "is_final": False}, owner_account_id="")
    await conn._flush_pending(key)

    assert [p["body"]["text"] for p in ws.sent] == ["A", "BC"]


@pytest.mark.asyncio
async def test_final_flushes_pending_deltas_before_final():
    """final 到达时，pending delta 合并帧应先于 final 帧推送。"""
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")
    # 让后续 delta 进入限流缓存而非立即推送
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload("s1", {"kind": "delta", "sequence": 1, "body": {"text": "A"}}, owner_account_id="")
    await conn.push_payload("s1", {"kind": "delta", "sequence": 2, "body": {"text": "B"}}, owner_account_id="")
    await conn.push_payload(
        "s1", {"kind": "final", "body": {"text": "AB"}, "is_final": True},
    owner_account_id="",
    )

    # 合并后的 delta 在 final 之前到达
    assert [p["kind"] for p in ws.sent] == ["delta", "final"]
    delta_payload = ws.sent[0]
    assert delta_payload["body"]["text"] == "AB"
    assert delta_payload["body"]["delta_start"] == 1
    assert delta_payload["body"]["delta_end"] == 2
    assert delta_payload["body"]["delta_parts"] == [[1, 1], [2, 1]]
    assert delta_payload["gateway_sequence"] == 2
    final_payload = ws.sent[1]
    assert final_payload["kind"] == "final"
    assert final_payload["gateway_sequence"] == 3


@pytest.mark.asyncio
async def test_tool_start_flushes_pending_and_bypasses_rate_limit():
    """tool/start 是用户可感知控制帧：先刷新 pending delta，再立即推送工具开始。"""
    conn = ConnectionManager(min_interval=10.0)
    ws = _FakeWS()
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload("s1", {"kind": "delta", "sequence": 1, "body": {"text": "准备"}}, owner_account_id="")
    assert ws.sent == []
    assert key in conn._pending_payloads

    await conn.push_payload(
        "s1",
        {
            "kind": "tool",
            "sequence": 2,
            "body": {"phase": "start", "tool_call_id": "t1", "name": "file_write"},
        },
    owner_account_id="",
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
    conn.register("s1", ws, owner_account_id="")
    key = ("", "s1")
    conn._last_push_ts[key] = time.monotonic()

    await conn.push_payload(
        "s1",
        {
            "kind": "tool",
            "sequence": 1,
            "body": {"phase": "result", "tool_call_id": "t1", "name": "file_write"},
        },
    owner_account_id="",
    )

    assert ws.sent == []
    assert conn._pending_payloads[key][0]["body"]["phase"] == "result"
    conn.unregister_all(ws, {"s1"}, owner_account_id="")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_replay_window_limits_preserve_contiguous_suffix_and_resync(monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._REPLAY_SESSION_BYTES", 600)
    monkeypatch.setattr("crew.gateway.connections._REPLAY_TOTAL_BYTES", 900)
    monkeypatch.setattr("crew.gateway.connections._REPLAY_SESSIONS", 2)
    conn = ConnectionManager(min_interval=0)
    first = {"kind": "delta", "body": {"text": "a"}}
    await conn.push_payload("hot", first, owner_account_id="a")
    epoch = first["gateway_epoch"]
    await conn.push_payload("hot", {"kind": "delta", "body": {"text": "x" * 800}}, owner_account_id="a")
    await conn.push_payload("hot", {"kind": "final", "is_final": True}, owner_account_id="a")
    for cursor, expected in ((1, "gateway_resync"), (2, "final")):
        socket = _FakeWS()
        await conn.replay("hot", socket, owner_account_id="a", resume_cursor={"epoch": epoch, "sequence": cursor})
        assert [f["kind"] for f in socket.sent] == [expected]
        assert socket.sent[0]["gateway_sequence"] == 3
    for i in range(10):
        await conn.push_payload(str(i), {"kind": "delta", "body": {"text": "é" * 50}}, owner_account_id="b")
        assert conn._journal.size <= 900
    assert conn._journal.count(("a", "hot")) == 0
    assert conn._journal.count(("b", "9")) == 1
    await conn.close_owner("b")
    assert conn._journal.size == 0


@pytest.mark.asyncio
async def test_overflow_disconnects_only_the_slow_subscriber_and_can_resume(conn):
    from crew.gateway.socket_delivery import SocketDelivery

    conn._delivery = SocketDelivery(conn.send_socket, conn._disconnect_failed_socket,
                                    max_bytes=1500, max_frames=3, max_age=1)
    blocked = asyncio.Event()

    class Slow(_FakeWS):
        async def send_json(self, data):
            await blocked.wait()

    slow, fast = Slow(), _FakeWS()
    for ws in (slow, fast):
        conn.register_owner("owner", ws)
        conn.register("s", ws, owner_account_id="owner")
    for i in range(6):
        await conn.push_payload("s", {"kind": "delta", "body": {"text": str(i)}}, owner_account_id="owner")
    await asyncio.sleep(0)
    assert slow.close_code == 1013 and not slow.sent
    assert slow not in conn._ws_to_sessions and slow not in conn._ws_to_owners
    assert [f["gateway_sequence"] for f in fast.sent] == list(range(1, 7))
    resumed = _FakeWS()
    assert await conn.replay("s", resumed, owner_account_id="owner",
                             resume_cursor={"epoch": fast.sent[0]["gateway_epoch"], "sequence": 3})
    assert [f["gateway_sequence"] for f in resumed.sent] == [4, 5, 6]


@pytest.mark.asyncio
async def test_long_batch_interval_cannot_accumulate_unbounded_frames(monkeypatch):
    monkeypatch.setattr("crew.gateway.connections._MAX_PENDING_BYTES", 800)
    conn = ConnectionManager(min_interval=600)
    ws = _FakeWS()
    conn.register("s", ws, owner_account_id="a")
    for _ in range(20):
        await conn.push_payload("s", {"kind": "tool", "body": {"result": "x" * 200}}, owner_account_id="a")
        assert conn._pending_bytes.get(("a", "s"), 0) < 800
    await conn.push_payload("s", {"kind": "final", "is_final": True}, owner_account_id="a")
    assert [frame["gateway_sequence"] for frame in ws.sent] == list(range(1, 22))
    assert ("a", "s") not in conn._pending_bytes
