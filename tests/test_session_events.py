"""ADR-0042 W4：会话增量事件表（D1a-D1e）行为测试。"""

from __future__ import annotations

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
                "INSERT INTO writer_leases (owner_account_id, session_id, owner_pid, fence, expires_at) "
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
