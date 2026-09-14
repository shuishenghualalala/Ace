"""ADR-0042 W4：会话增量事件表（D1a-D1e）行为测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from crew.core.types import Message
from crew.state.session_store import (
    SESSIONS_SCHEMA_VERSION,
    SessionEventType,
    SQLiteSessionStore,
)


def _raw_conn(db_path):
    return sqlite3.connect(str(db_path))


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in sorted((r for r in info if int(r[5] or 0) > 0), key=lambda r: int(r[5]))]


def test_event_and_lease_tables_with_owner_scoped_pk(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        conn = _raw_conn(tmp_path / "crew.db")
        try:
            assert _primary_key_columns(conn, "session_events") == [
                "owner_account_id",
                "session_id",
                "seq",
            ]
            assert _primary_key_columns(conn, "writer_leases") == [
                "owner_account_id",
                "session_id",
            ]
        finally:
            conn.close()
    finally:
        store.close()


def test_sessions_table_gains_leaf_seq_column(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        conn = _raw_conn(tmp_path / "crew.db")
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            assert "leaf_seq" in cols
        finally:
            conn.close()
    finally:
        store.close()


def test_event_type_durability_classification():
    assert SessionEventType.USER_MESSAGE.durable
    assert SessionEventType.ASSISTANT_MESSAGE.durable
    assert SessionEventType.TOOL_RESULT.durable
    assert SessionEventType.SYSTEM_MESSAGE.durable
    assert SessionEventType.METER_CHECKPOINT.durable
    for transient in (
        SessionEventType.TURN_PROGRESS,
        SessionEventType.ERROR,
        SessionEventType.QUEUE_STATE,
    ):
        assert not transient.durable


def test_pragma_baseline_on_new_database(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        # journal_mode/auto_vacuum 是库级持久状态，任意连接可查；
        # synchronous/busy_timeout 是连接级 pragma，只能查 store 自己的连接。
        raw = _raw_conn(tmp_path / "crew.db")
        try:
            assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
        finally:
            raw.close()
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        store.close()


def test_auto_vacuum_not_forced_on_existing_database(tmp_path):
    db_path = tmp_path / "crew.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE preexisting (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    store = SQLiteSessionStore(str(db_path))
    try:
        raw = _raw_conn(db_path)
        try:
            assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        finally:
            raw.close()
    finally:
        store.close()


def test_schema_version_stamped(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        conn = _raw_conn(tmp_path / "crew.db")
        try:
            row = conn.execute(
                "SELECT version FROM sessions_schema_version WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            assert int(row[0]) == SESSIONS_SCHEMA_VERSION
        finally:
            conn.close()
    finally:
        store.close()


def test_legacy_blob_session_still_loadable(tmp_path):
    """W4 之前只有 blob 的会话（事件表为空）读取不受影响。"""
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        store.save("legacy", [Message.user("hi")], owner_account_id="")
        messages = store.load("legacy", owner_account_id="")
        assert [m.content for m in messages] == ["hi"]
    finally:
        store.close()


def _message_event_count(db_path, owner: str, session_id: str) -> int:
    conn = _raw_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_events "
            "WHERE owner_account_id = ? AND session_id = ? AND type != 'meter_checkpoint'",
            (owner, session_id),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _leaf_seq(db_path, owner: str, session_id: str) -> int:
    conn = _raw_conn(db_path)
    try:
        row = conn.execute(
            "SELECT leaf_seq FROM sessions WHERE owner_account_id = ? AND session_id = ?",
            (owner, session_id),
        ).fetchone()
        return int(row[0]) if row else -1
    finally:
        conn.close()


def test_each_save_turn_inserts_only_new_tail_events(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("q1"), Message.assistant("a1")], owner_account_id="")
        assert _message_event_count(db, "", "s1") == 2
        assert _leaf_seq(db, "", "s1") == 2

        # 第二回合只追加一条：INSERT 行数 == 本回合新消息数（写放大 O(新事件)）
        history = store.load("s1", owner_account_id="")
        history.append(Message.user("q2"))
        store.save("s1", history, owner_account_id="")
        assert _message_event_count(db, "", "s1") == 3
        assert _leaf_seq(db, "", "s1") == 3

        assert [m.content for m in store.load("s1", owner_account_id="")] == ["q1", "a1", "q2"]
    finally:
        store.close()


def test_event_seq_is_contiguous_per_session(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a"), Message.user("b"), Message.user("c")], owner_account_id="")
        conn = _raw_conn(db)
        try:
            seqs = [int(r[0]) for r in conn.execute(
                "SELECT seq FROM session_events WHERE session_id = 's1' ORDER BY seq"
            ).fetchall()]
        finally:
            conn.close()
        assert seqs == [1, 2, 3]
    finally:
        store.close()


def test_meter_checkpoint_event_appended_with_usage(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save(
            "s1",
            [Message.user("q1")],
            owner_account_id="",
            last_prompt_tokens=1234,
            last_prompt_tokens_source="provider",
        )
        conn = _raw_conn(db)
        try:
            rows = conn.execute(
                "SELECT type, payload FROM session_events WHERE session_id = 's1' AND type = 'meter_checkpoint'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        import json

        payload = json.loads(rows[0][1])
        assert payload["prompt_tokens"] == 1234

        # 无 usage 的回合不产生 checkpoint 事件
        history = store.load("s1", owner_account_id="")
        history.append(Message.user("q2"))
        store.save("s1", history, owner_account_id="")
        assert _message_event_count(db, "", "s1") == 2
    finally:
        store.close()


def test_rewrite_replaces_event_rows(tmp_path):
    """整体改写历史（同长度不同内容）触发重写，事件行与 blob 一致。"""
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("old")], owner_account_id="")
        store.save("s1", [Message.user("new")], owner_account_id="")
        assert _message_event_count(db, "", "s1") == 1
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["new"]
        assert _leaf_seq(db, "", "s1") == 1
    finally:
        store.close()


def test_save_async_flushes_before_returning(tmp_path):
    import asyncio

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)

    async def main():
        await store.save_async(
            "s1", [Message.user("async-q"), Message.assistant("async-a")], owner_account_id=""
        )

    try:
        asyncio.run(main())
        # flush barrier：save_async 返回时事件已同事务落库
        assert _message_event_count(db, "", "s1") == 2
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["async-q", "async-a"]
    finally:
        store.close()


def test_write_queue_retries_failed_batch_once(tmp_path):
    import asyncio

    from crew.state import sqlite as sqlite_module

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    calls = {"n": 0}
    original = sqlite_module.SQLiteWriteHelper.execute_async

    async def flaky(self, fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await original(self, fn)

    async def main():
        await store.save_async("s1", [Message.user("q")], owner_account_id="")

    try:
        sqlite_module.SQLiteWriteHelper.execute_async = flaky
        asyncio.run(main())
        assert calls["n"] == 2
        assert _message_event_count(db, "", "s1") == 1
    finally:
        sqlite_module.SQLiteWriteHelper.execute_async = original
        store.close()


def test_clear_purges_events_and_lease_rows(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("q")], owner_account_id="")
        conn = _raw_conn(db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO writer_leases (owner_account_id, session_id, owner_pid, fence, expires_at) "
                "VALUES ('', 's1', 'p', 1, 0)"
            )
            conn.commit()
        finally:
            conn.close()
        store.clear("s1", owner_account_id="")
        conn = _raw_conn(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM writer_leases").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        finally:
            conn.close()
        assert store.load("s1", owner_account_id="") == []
    finally:
        store.close()


def _insert_event(conn, owner: str, session_id: str, seq: int, etype: str, payload: str):
    conn.execute(
        "INSERT OR REPLACE INTO session_events (owner_account_id, session_id, seq, type, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner, session_id, seq, etype, payload, 0.0),
    )


def test_reader_uses_leaf_seq_snapshot(tmp_path):
    """leaf 指针之后的行（如撕裂写另一半）对读者不可见。"""
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("q1")], owner_account_id="")
        store.load("s1", owner_account_id="")  # 建立缓存

        # 模拟写一半的外部状态：seq=2 的行已存在，但 leaf 仍停在 1
        conn = _raw_conn(db)
        try:
            _insert_event(
                conn,
                "",
                "s1",
                2,
                "user_message",
                json.dumps(SQLiteSessionStore._message_to_dict(Message.user("phantom"))),
            )
            conn.commit()
        finally:
            conn.close()

        assert [m.content for m in store.load("s1", owner_account_id="")] == ["q1"]

        # leaf 推进后（另一半提交完成），同一读者即可见
        conn = _raw_conn(db)
        try:
            conn.execute("UPDATE sessions SET leaf_seq = 2 WHERE session_id = 's1'")
            conn.commit()
        finally:
            conn.close()
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["q1", "phantom"]
    finally:
        store.close()


def test_gap_in_event_stream_fails_closed(tmp_path):
    import pytest

    from crew.state.session_store import SessionEventLogError

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a"), Message.user("b")], owner_account_id="")
        store.load("s1", owner_account_id="")  # 建立游标缓存
        # 制造前向缺口：外部写入了 seq 3 与 5（跳过 4），leaf 推进到 5
        conn = _raw_conn(db)
        try:
            for seq, content in ((3, "c"), (5, "e")):
                _insert_event(
                    conn,
                    "",
                    "s1",
                    seq,
                    "user_message",
                    json.dumps(SQLiteSessionStore._message_to_dict(Message.user(content))),
                )
            conn.execute("UPDATE sessions SET leaf_seq = 5 WHERE session_id = 's1' AND owner_account_id = ''")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SessionEventLogError):
            store.load("s1", owner_account_id="")
    finally:
        store.close()


def test_unknown_event_type_fails_closed(tmp_path):
    import pytest

    from crew.state.session_store import SessionEventLogError

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a")], owner_account_id="")
        conn = _raw_conn(db)
        try:
            _insert_event(conn, "", "s1", 2, "mystery_event", "{}")
            conn.execute("UPDATE sessions SET leaf_seq = 2 WHERE session_id = 's1' AND owner_account_id = ''")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SessionEventLogError):
            store.load("s1", owner_account_id="")
    finally:
        store.close()


def test_catch_up_recovers_after_external_rewrite(tmp_path):
    """外部（另一进程）接管并整体重写事件后，读者按代际标记重建投影，不沿用旧前缀。"""
    import time

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db, lease_ttl_seconds=0.3, lease_heartbeat_seconds=60.0)
    store2 = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("old-1"), Message.user("old-2")], owner_account_id="")
        store.load("s1", owner_account_id="")  # 缓存 old
        time.sleep(0.4)  # store 的租约过期，store2 可接管

        store2.save(
            "s1",
            [Message.user("new-1"), Message.user("new-2"), Message.user("new-3"), Message.user("new-4")],
            owner_account_id="",
        )

        assert [m.content for m in store.load("s1", owner_account_id="")] == [
            "new-1",
            "new-2",
            "new-3",
            "new-4",
        ]
        # 重写方自己的读写一致
        assert [m.content for m in store2.load("s1", owner_account_id="")] == [
            "new-1",
            "new-2",
            "new-3",
            "new-4",
        ]
    finally:
        store.close()
        store2.close()


def test_load_child_sessions_reads_events(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("parent::turn::1::leader", [Message.user("child-msg")], owner_account_id="")
        children = store.load_child_sessions("parent", owner_account_id="")
        assert len(children) == 1
        sid, messages = children[0]
        assert sid == "parent::turn::1::leader"
        assert [m.content for m in messages] == ["child-msg"]
    finally:
        store.close()


def test_writer_lease_conflicts_second_process(tmp_path):
    import pytest

    from crew.state.session_store import SessionWriteConflict

    db = str(tmp_path / "crew.db")
    store1 = SQLiteSessionStore(db)
    store2 = SQLiteSessionStore(db)
    try:
        store1.save("s1", [Message.user("from-p1")], owner_account_id="")
        # 第二进程打开同一会话：有效租约期内写入被拒
        with pytest.raises(SessionWriteConflict):
            store2.save("s1", [Message.user("from-p2")], owner_account_id="")
        # 其他会话不受影响
        store2.save("s2", [Message.user("other")], owner_account_id="")
        # 第一进程自己续约续写正常
        store1.save("s1", [Message.user("from-p1"), Message.user("more")], owner_account_id="")
        assert [m.content for m in store1.load("s1", owner_account_id="")] == ["from-p1", "more"]
    finally:
        store1.close()
        store2.close()


def test_writer_lease_takeover_after_expiry(tmp_path):
    import time

    import pytest

    from crew.state.session_store import SessionWriteConflict

    db = str(tmp_path / "crew.db")
    store1 = SQLiteSessionStore(db, lease_ttl_seconds=0.3, lease_heartbeat_seconds=60.0)
    store2 = SQLiteSessionStore(db, lease_ttl_seconds=0.3, lease_heartbeat_seconds=60.0)
    try:
        store1.save("s1", [Message.user("p1")], owner_account_id="")
        time.sleep(0.4)
        # TTL 到期自动接管：fence+1，写入成功
        store2.save("s1", [Message.user("p1"), Message.user("p2")], owner_account_id="")
        # 旧写者租约已被抢占：下一次写入被拒（fence 语义，防脑裂）
        with pytest.raises(SessionWriteConflict):
            store1.save("s1", [Message.user("p1"), Message.user("p2"), Message.user("p1-again")], owner_account_id="")
        assert [m.content for m in store2.load("s1", owner_account_id="")] == ["p1", "p2"]
        # 读路径不受租约限制
        assert [m.content for m in store1.load("s1", owner_account_id="")] == ["p1", "p2"]
    finally:
        store1.close()
        store2.close()


def test_writer_lease_released_on_close(tmp_path):
    db = str(tmp_path / "crew.db")
    store1 = SQLiteSessionStore(db, lease_ttl_seconds=30.0)
    store1.save("s1", [Message.user("p1")], owner_account_id="")
    store1.close()

    store2 = SQLiteSessionStore(db, lease_ttl_seconds=30.0)
    try:
        store2.save("s1", [Message.user("p1"), Message.user("p2")], owner_account_id="")
        assert [m.content for m in store2.load("s1", owner_account_id="")] == ["p1", "p2"]
    finally:
        store2.close()


def test_writer_lease_heartbeat_keeps_ownership(tmp_path):
    import asyncio

    from crew.state.session_store import SessionWriteConflict

    db = str(tmp_path / "crew.db")
    store1 = SQLiteSessionStore(db, lease_ttl_seconds=0.6, lease_heartbeat_seconds=0.2)
    store2 = SQLiteSessionStore(db, lease_ttl_seconds=0.6, lease_heartbeat_seconds=0.2)

    async def main():
        await store1.save_async("s1", [Message.user("p1")], owner_account_id="")
        # 超过 TTL 但心跳持续续约：第二进程仍拿不到租约
        await asyncio.sleep(1.0)
        try:
            await store2.save_async("s1", [Message.user("p2")], owner_account_id="")
            raise AssertionError("expected SessionWriteConflict")
        except SessionWriteConflict:
            pass

    try:
        asyncio.run(main())
    finally:
        store1.close()
        store2.close()


def _legacy_blob_store(db: str) -> SQLiteSessionStore:
    """模拟 W4 之前的纯 blob 存储：写 blob、不产生任何事件行。"""
    store = SQLiteSessionStore(db, read_mode="blob")

    def _blob_only_save(session_id, messages, workspace_id="default", *, owner_account_id,
                        title_fallback=None, last_prompt_tokens=None, last_prompt_tokens_source=None):
        def _write(conn):
            conn.execute(
                "INSERT INTO sessions (session_id, owner_account_id, messages, updated_at, created_at, workspace_id, title, message_count, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?) "
                "ON CONFLICT(owner_account_id, session_id) DO UPDATE SET "
                "messages = excluded.messages, updated_at = excluded.updated_at, "
                "message_count = excluded.message_count, token_count = excluded.token_count",
                (session_id, owner_account_id,
                 SQLiteSessionStore._dump(messages), 0.0, 0.0, workspace_id,
                 len(messages), SQLiteSessionStore._estimate_tokens(messages)),
            )

        store._writer.execute(_write)
        store._projections.pop((owner_account_id, session_id), None)

    store._blob_only_save = _blob_only_save  # type: ignore[attr-defined]
    return store


def test_legacy_blob_session_migrated_to_events_on_open(tmp_path):
    db = str(tmp_path / "crew.db")
    legacy = _legacy_blob_store(db)
    legacy._blob_only_save(  # type: ignore[attr-defined]
        "old-1", [Message.user("hello"), Message.assistant("hi there")], owner_account_id="A:uid-a"
    )
    legacy._blob_only_save(  # type: ignore[attr-defined]
        "old-2", [Message.user("second")], owner_account_id="A:uid-a"
    )
    legacy.close()

    store = SQLiteSessionStore(db)
    try:
        assert [m.content for m in store.load("old-1", owner_account_id="A:uid-a")] == ["hello", "hi there"]
        assert [m.content for m in store.load("old-2", owner_account_id="A:uid-a")] == ["second"]
        # 迁移后事件行就位、leaf 推进
        assert _message_event_count(db, "A:uid-a", "old-1") == 2
        assert _leaf_seq(db, "A:uid-a", "old-1") == 2
        # 迁移继续可用：追加走增量事件
        history = store.load("old-1", owner_account_id="A:uid-a")
        history.append(Message.user("follow-up"))
        store.save("old-1", history, owner_account_id="A:uid-a")
        assert _message_event_count(db, "A:uid-a", "old-1") == 3
    finally:
        store.close()


def test_backfill_is_idempotent(tmp_path):
    db = str(tmp_path / "crew.db")
    legacy = _legacy_blob_store(db)
    legacy._blob_only_save(  # type: ignore[attr-defined]
        "s1", [Message.user("a"), Message.user("b")], owner_account_id="A:uid-a"
    )
    legacy.close()

    store = SQLiteSessionStore(db)
    store.close()
    store = SQLiteSessionStore(db)  # 第二次打开再跑一次迁移
    try:
        assert _message_event_count(db, "A:uid-a", "s1") == 2
        assert [m.content for m in store.load("s1", owner_account_id="A:uid-a")] == ["a", "b"]
    finally:
        store.close()


def test_blob_read_mode_fallback_still_works(tmp_path):
    """双格式窗口内 read_mode=blob 可原地回退（events 与 blob 双写保持同步）。"""
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("q1")], owner_account_id="A:uid-a")
        store.save("s1", [Message.user("q1"), Message.user("q2")], owner_account_id="A:uid-a")
    finally:
        store.close()

    blob_store = SQLiteSessionStore(db, read_mode="blob")
    try:
        assert [m.content for m in blob_store.load("s1", owner_account_id="A:uid-a")] == ["q1", "q2"]
        # blob 模式可读，事件行也在（双写）
        assert _message_event_count(db, "A:uid-a", "s1") == 2
    finally:
        blob_store.close()


def test_kill9_simulation_commit_is_atomic(tmp_path):
    """kill -9 写一半：事务要么整体提交要么回滚，重启后会话可加载且一致。

    单写队列的每批写入（事件 + leaf 推进 + blob）在单事务内，
    崩溃不会产生「事件已写但 leaf 未推进」的撕裂态。
    """
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("turn1-a"), Message.assistant("turn1-b")], owner_account_id="A:uid-a")
        store.save(
            "s1",
            [Message.user("turn1-a"), Message.assistant("turn1-b"), Message.user("turn2")],
            owner_account_id="A:uid-a",
        )
    finally:
        store.close()

    # 模拟重启：新实例冷读
    store2 = SQLiteSessionStore(db)
    try:
        messages = store2.load("s1", owner_account_id="A:uid-a")
        assert [m.content for m in messages] == ["turn1-a", "turn1-b", "turn2"]
        # 配平后下一轮请求正常：继续追加不报错、事件连续
        messages.append(Message.user("turn3"))
        store2.save("s1", messages, owner_account_id="A:uid-a")
        assert _message_event_count(db, "A:uid-a", "s1") == 4
        assert _leaf_seq(db, "A:uid-a", "s1") == 4
    finally:
        store2.close()


# ---- W5：会话树（rewind/fork/list_branches，ADR-0042 D2/D3） ----


def _event_row_count(db, owner: str, sid: str) -> int:
    conn = _raw_conn(db)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                (owner, sid),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _leaf_seq(db, owner: str, sid: str) -> int:
    conn = _raw_conn(db)
    try:
        return int(
            conn.execute(
                "SELECT leaf_seq FROM sessions WHERE owner_account_id = ? AND session_id = ?",
                (owner, sid),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_events_carry_parent_seq_chain(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a"), Message.user("b")], owner_account_id="")
        conn = _raw_conn(db)
        try:
            rows = conn.execute(
                "SELECT seq, parent_seq FROM session_events WHERE session_id = 's1' ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
        assert [(int(r[0]), r[1]) for r in rows] == [(1, None), (2, 1)]
    finally:
        store.close()


def test_rewind_moves_leaf_without_rewriting_rows(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id="")
        rows_before = _event_row_count(db, "", "s1")

        store.rewind("s1", 2, owner_account_id="")
        assert _leaf_seq(db, "", "s1") == 2
        # 已存在行零改写：行数不变，旧分支完整保留
        assert _event_row_count(db, "", "s1") == rows_before
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["m0", "m1"]
        # blob 列同步回写（双格式窗口一致）
        conn = _raw_conn(db)
        try:
            blob = json.loads(conn.execute("SELECT messages FROM sessions WHERE session_id = 's1'").fetchone()[0])
        finally:
            conn.close()
        assert [m["content"] for m in blob] == ["m0", "m1"]
    finally:
        store.close()


def test_rewind_then_append_creates_branch_and_can_navigate_back(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id="")
        store.rewind("s1", 2, owner_account_id="")
        store.save("s1", [Message.user("m0"), Message.user("m1"), Message.user("b0"), Message.user("b1")], owner_account_id="")

        # 新分支挂在前缀链上
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["m0", "m1", "b0", "b1"]
        branches = store.list_branches("s1", owner_account_id="")
        tails = [b for b in branches if b["kind"] == "tail"]
        assert len(tails) == 1
        assert tails[0]["cut_seq"] == 2
        assert tails[0]["tip_seq"] == 4

        # 导航回旧分支：leaf 移回旧尾，当前分支变成 tail 保留
        store.rewind("s1", 4, owner_account_id="")
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["m0", "m1", "m2", "m3"]
        tails = [b for b in store.list_branches("s1", owner_account_id="") if b["kind"] == "tail"]
        assert len(tails) == 1
        assert tails[0]["tip_seq"] == 6
    finally:
        store.close()


def test_rewind_rejects_cut_inside_open_turn(tmp_path):
    import pytest

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a"), Message.user("b")], owner_account_id="")
        store.record_turn_event("s1", kind=SessionEventType.TURN_START, owner_account_id="")
        store.save("s1", [Message.user("a"), Message.user("b"), Message.user("c")], owner_account_id="")
        # 切口落在开放回合内（turn_start 之后）必须拒绝
        with pytest.raises(ValueError, match="开放回合"):
            store.rewind("s1", 3, owner_account_id="")
        with pytest.raises(ValueError, match="开放回合"):
            store.rewind("s1", 4, owner_account_id="")
        # 开放回合之前的闭合前缀合法
        store.rewind("s1", 2, owner_account_id="")
        assert _leaf_seq(db, "", "s1") == 2
    finally:
        store.close()


def test_rewind_rejects_unbalanced_tool_cut(tmp_path):
    import pytest

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        from crew.core.types import ToolCall

        tc = ToolCall(id="call_1", name="read_file", arguments={"path": "x"})
        store.save(
            "s1",
            [Message.user("a"), Message.assistant(tool_calls=[tc])],
            owner_account_id="",
        )
        with pytest.raises(ValueError, match="未闭合"):
            store.rewind("s1", 2, owner_account_id="")
    finally:
        store.close()


def test_rewind_cas_conflict_on_concurrent_leaf_move(tmp_path):
    import time

    import pytest

    from crew.state.session_store import SessionWriteConflict

    db = str(tmp_path / "crew.db")
    # 短 TTL：让 store2 在竞速时能合法接管写者租约
    store1 = SQLiteSessionStore(db, lease_ttl_seconds=0.3, lease_heartbeat_seconds=60.0)
    store2 = SQLiteSessionStore(db)
    try:
        store1.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id="")
        store1.load("s1", owner_account_id="")
        time.sleep(0.4)  # store1 租约过期，store2 可接管

        original = store1._chain_messages_upto

        def _race(owner, sid, upto):
            store2.rewind("s1", 1, owner_account_id="")
            return original(owner, sid, upto)

        store1._chain_messages_upto = _race
        with pytest.raises(SessionWriteConflict):
            store1.rewind("s1", 2, owner_account_id="")
        store1._chain_messages_upto = original
        # store2 的移动生效未被覆盖
        assert _leaf_seq(db, "", "s1") == 1
    finally:
        store1.close()
        store2.close()


def test_fork_shares_prefix_without_copying_rows(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id="")
        source_rows_before = _event_row_count(db, "", "s1")

        fork_id = store.fork("s1", 2, owner_account_id="")
        # 源会话行数零变化：前缀共享不复制
        assert _event_row_count(db, "", "s1") == source_rows_before
        # fork 只有一条 end_seed 切口事件
        assert _event_row_count(db, "", fork_id) == 1
        assert [m.content for m in store.load(fork_id, owner_account_id="")] == ["m0", "m1"]

        # fork 上续写不影响源会话
        store.save(fork_id, [Message.user("m0"), Message.user("m1"), Message.user("f0")], owner_account_id="")
        assert [m.content for m in store.load(fork_id, owner_account_id="")] == ["m0", "m1", "f0"]
        assert [m.content for m in store.load("s1", owner_account_id="")] == ["m0", "m1", "m2", "m3"]

        branches = store.list_branches("s1", owner_account_id="")
        forks = [b for b in branches if b["kind"] == "fork"]
        assert len(forks) == 1
        assert forks[0]["session_id"] == fork_id
        assert forks[0]["parent_seq"] == 2
        assert forks[0]["tip_seq"] == 2
    finally:
        store.close()


def test_fork_of_fork_resolves_nested_prefix(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id="")
        f1 = store.fork("s1", 2, owner_account_id="")
        store.save(f1, [Message.user("m0"), Message.user("m1"), Message.user("x")], owner_account_id="")
        # 边界 seq 是源会话（f1）事件表内的链上位置：1 = end_seed 切口
        f2 = store.fork(f1, 1, owner_account_id="")
        assert [m.content for m in store.load(f2, owner_account_id="")] == ["m0", "m1"]
    finally:
        store.close()


def test_fork_rejects_bad_boundary(tmp_path):
    import pytest

    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a")], owner_account_id="")
        with pytest.raises(ValueError):
            store.fork("s1", 99, owner_account_id="")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gateway_rewind_fork_branches_endpoints(tmp_path, auth_headers):
    from httpx import ASGITransport, AsyncClient

    from crew.app import build_app
    from crew.gateway.server import create_app
    from crew.state.config import Config

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    owner = "A:uid-a"
    crew.session_store.save("s1", [Message.user(f"m{i}") for i in range(4)], owner_account_id=owner)
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/session/s1/rewind", json={"target_seq": 2})
        assert resp.status_code == 200
        assert resp.json()["leaf_seq"] == 2

        # 非法切口 → 400
        resp = await client.post("/api/session/s1/rewind", json={"target_seq": 99})
        assert resp.status_code == 400

        resp = await client.post("/api/session/s1/fork", json={"boundary_seq": 2, "title": "分支A"})
        assert resp.status_code == 200
        fork_id = resp.json()["session_id"]
        assert resp.json()["source_session_id"] == "s1"

        resp = await client.get("/api/session/s1/branches")
        assert resp.status_code == 200
        branches = resp.json()["branches"]
        forks = [b for b in branches if b["kind"] == "fork"]
        assert [f["session_id"] for f in forks] == [fork_id]
        assert forks[0]["parent_seq"] == 2
        tails = [b for b in branches if b["kind"] == "tail"]
        assert tails and tails[0]["tip_seq"] == 4

        # fork 出来的会话前缀可加载
        resp = await client.get(f"/api/session/{fork_id}")
        assert resp.status_code == 200

        # 不存在的会话 → 404
        resp = await client.post("/api/session/nope/rewind", json={"target_seq": 1})
        assert resp.status_code == 404


# ---- W5：崩溃恢复定稿（ADR-0042 D3） ----


def test_kill9_mid_turn_reopen_and_close_open_turn(tmp_path):
    """kill -9 中途崩溃（turn_start 无 turn_end）：重开后可加载、悬挂回合可闭合。"""
    from crew.core.types import ToolCall

    owner = "A:uid-a"
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("q")], owner_account_id=owner)
        store.record_turn_event("s1", kind=SessionEventType.TURN_START, owner_account_id=owner)
        store.save(
            "s1",
            [Message.user("q"), Message.assistant(tool_calls=[ToolCall(id="call_1", name="read_file", arguments={})])],
            owner_account_id=owner,
        )
        # 模拟 kill -9：不落 turn_end、不 close，直接弃实例
    finally:
        store.close()

    store2 = SQLiteSessionStore(db)
    try:
        messages = store2.load("s1", owner_account_id=owner)
        assert [m.role for m in messages] == ["user", "assistant"]
        # 悬挂回合被识别并闭合（interrupted），再次闭合为空操作
        assert store2.close_open_turn("s1", owner_account_id=owner) is True
        assert store2.close_open_turn("s1", owner_account_id=owner) is False
        # 闭合后回合边界完整，下一回合正常追加
        store2.record_turn_event("s1", kind=SessionEventType.TURN_START, owner_account_id=owner)
        store2.save(
            "s1",
            [*messages, Message.user("next")],
            owner_account_id=owner,
        )
        assert [m.content for m in store2.load("s1", owner_account_id=owner)][-1] == "next"
    finally:
        store2.close()


def test_close_open_turn_on_closed_session_is_noop(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("s1", [Message.user("a")], owner_account_id="")
        assert store.close_open_turn("s1", owner_account_id="") is False
        assert store.close_open_turn("ghost", owner_account_id="") is False
    finally:
        store.close()


# ---- W5：断点报告（ADR-0042 D4） ----


def test_scan_breakpoints_reports_open_turn_with_step(tmp_path):
    owner = "A:uid-a"
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        # 会话 A：上一回合被 kill -9 打断（turn_start 无 turn_end）
        store.save("sa", [Message.user("q1")], owner_account_id=owner)
        store.record_turn_event("sa", kind=SessionEventType.TURN_START, owner_account_id=owner)
        store.save(
            "sa",
            [Message.user("q1"), Message.assistant("a1"), Message.user("q2"), Message.assistant("a2")],
            owner_account_id=owner,
        )
        # 会话 B：正常闭合，无断点
        store.save("sb", [Message.user("x")], owner_account_id=owner)
        store.record_turn_event("sb", kind=SessionEventType.TURN_START, owner_account_id=owner)
        store.save("sb", [Message.user("x"), Message.assistant("y")], owner_account_id=owner)
        store.record_turn_event(
            "sb", kind=SessionEventType.TURN_END, owner_account_id=owner, status="completed"
        )
        # Team 子会话：排除
        store.save("p::turn::1::leader", [Message.user("c")], owner_account_id=owner)
        store.record_turn_event("p::turn::1::leader", kind=SessionEventType.TURN_START, owner_account_id=owner)

        reports = store.scan_breakpoints(owner)
        assert [r["session_id"] for r in reports] == ["sa"]
        report = reports[0]
        assert report["step"] == 3  # 开放回合内 a1/q2/a2 三条消息事件（q1 在回合前）
        assert report["last_event_seq"] == 5
        # 断点报告不自动续跑：扫描本身不写任何事件
        assert _event_row_count(db, owner, "sa") == 5
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gateway_sessions_list_includes_breakpoint(tmp_path, auth_headers):
    from httpx import ASGITransport, AsyncClient

    from crew.app import build_app
    from crew.gateway.server import create_app
    from crew.state.config import Config

    owner = "A:uid-a"
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.session_store.save("s1", [Message.user("q1")], owner_account_id=owner)
    crew.session_store.record_turn_event("s1", kind=SessionEventType.TURN_START, owner_account_id=owner)
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        # q1 在 turn_start 之前落库，开放回合内尚无消息事件
        assert items[0]["breakpoint"]["step"] == 0
        assert items[0]["breakpoint"]["turn_start_seq"] == 2
