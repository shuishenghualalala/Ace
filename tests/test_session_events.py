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
