"""ADR-0042 W4：会话增量事件表（D1a-D1e）行为测试。"""

from __future__ import annotations

import json
import sqlite3

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
    import time

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
