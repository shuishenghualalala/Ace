"""owner 统一回填：历史 owner='' 行自动归属本机 local 的迁移语义。

owner 归一化后系统不存在"无主"数据；本文件覆盖：
1. backfill_empty_owner_rows 的通用语义（回填 / 冲突丢弃 / 跳过无关表）；
2. 启动期 inspect_and_backfill_legacy_owners 的整体行为（智能回填优先于 local 兜底）；
3. 各独立库文件存储打开时对存量数据的就地归一。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crew.agent.compact.store import SummaryStore
from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.memory.simple import SQLiteMemory
from crew.notifications.store import NotificationStore
from crew.state._migration import (
    backfill_empty_owner_rows,
    inspect_and_backfill_legacy_owners,
    legacy_owner_counts,
    legacy_owner_scan_targets,
)
from crew.channels.channel_bindings import ChannelBindingsStore
from crew.state.plugin_preferences import PluginPreferencesStore


def _make_conn_with_sessions(rows: list[tuple[str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE sessions (
            owner_account_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (owner_account_id, session_id)
        )
        """
    )
    conn.executemany("INSERT INTO sessions VALUES (?, ?, '')", rows)
    return conn


def test_backfill_rehomes_orphan_rows_to_local():
    conn = _make_conn_with_sessions([("", "s1"), ("", "s2")])

    changed = backfill_empty_owner_rows(conn, ["sessions"])

    assert changed["sessions"] == 2
    rows = conn.execute("SELECT owner_account_id FROM sessions ORDER BY session_id").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID,)] * 2


def test_backfill_conflict_keeps_owned_row_and_drops_empty_row():
    """同一 session_id 同时存在 '' 与 local 行时：保归属行、删无主行。"""
    conn = _make_conn_with_sessions([("", "s1"), (LOCAL_OWNER_ACCOUNT_ID, "s1"), ("", "s2")])

    changed = backfill_empty_owner_rows(conn, ["sessions"])

    # s1 空行与 local 行主键冲突被丢弃；s2 正常归一
    assert changed["sessions"] == 1
    rows = conn.execute("SELECT owner_account_id, session_id FROM sessions ORDER BY session_id").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID, "s1"), (LOCAL_OWNER_ACCOUNT_ID, "s2")]


def test_backfill_skips_missing_and_ownerless_tables():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE plain (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO plain VALUES ('x')")

    # 不存在的表与无 owner 列的表都安全跳过
    changed = backfill_empty_owner_rows(conn, ["not_exists", "plain"])
    assert changed == {}
    assert conn.execute("SELECT COUNT(*) FROM plain").fetchone()[0] == 1


def test_startup_backfill_smart_attribution_wins_over_local(tmp_path: Path):
    """cron 无主任务能按会话唯一归属时优先归真实 owner，其余归 local。"""
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE sessions (
            owner_account_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            PRIMARY KEY (owner_account_id, session_id)
        )
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('email:alice', 's-alice')")
    conn.execute(
        """
        CREATE TABLE cron_jobs (
            id TEXT PRIMARY KEY,
            owner_account_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.executemany(
        "INSERT INTO cron_jobs VALUES (?, ?, ?)",
        [("job-a", "", "s-alice"), ("job-b", "", "")],
    )
    conn.commit()
    conn.close()

    counts, _ = inspect_and_backfill_legacy_owners(legacy_owner_scan_targets(db, db))

    conn = sqlite3.connect(db)
    owners = dict(conn.execute("SELECT id, owner_account_id FROM cron_jobs").fetchall())
    # job-a 唯一归属 email:alice（智能回填）；job-b 无处归属 → local 兜底
    assert owners == {"job-a": "email:alice", "job-b": LOCAL_OWNER_ACCOUNT_ID}
    assert counts == {table: 0 for table in counts}


def test_startup_backfill_leaves_no_orphan_sessions(tmp_path: Path):
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE sessions (
            owner_account_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            PRIMARY KEY (owner_account_id, session_id)
        )
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('', 'legacy-1')")
    conn.commit()
    conn.close()

    counts, _ = inspect_and_backfill_legacy_owners(legacy_owner_scan_targets(db, db))

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT owner_account_id FROM sessions").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID,)]
    assert counts.get("sessions", 0) == 0
    assert legacy_owner_counts(conn)["sessions"] == 0
    conn.close()


def test_memory_store_backfills_legacy_rows_on_open(tmp_path: Path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "owner_account_id TEXT NOT NULL DEFAULT '', session_id TEXT, text TEXT, ts REAL)"
    )
    conn.execute("INSERT INTO memory (owner_account_id, session_id, text, ts) VALUES ('', 's1', '旧记忆', 1)")
    conn.commit()
    conn.close()

    store = SQLiteMemory(str(db))
    try:
        owner = store._owner()
        rows = sqlite3.connect(db).execute(
            "SELECT owner_account_id, text FROM memory"
        ).fetchall()
        assert rows == [(LOCAL_OWNER_ACCOUNT_ID, "旧记忆")]
        assert owner == LOCAL_OWNER_ACCOUNT_ID
    finally:
        store.close()


def test_channel_bindings_backfill_makes_legacy_binding_visible_to_local(tmp_path: Path):
    db = tmp_path / "bindings.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE channel_bindings (
            platform TEXT NOT NULL,
            owner_account_id TEXT NOT NULL,
            bound_at REAL NOT NULL,
            PRIMARY KEY (platform, owner_account_id)
        )
        """
    )
    conn.execute("INSERT INTO channel_bindings VALUES ('feishu', '', 1.0)")
    conn.commit()
    conn.close()

    store = ChannelBindingsStore(str(db))
    binding = store.get_binding("feishu", owner_account_id=LOCAL_OWNER_ACCOUNT_ID)
    assert binding is not None


def test_plugin_preferences_backfill_on_open(tmp_path: Path):
    db = tmp_path / "prefs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE plugin_preferences (
            owner_account_id TEXT NOT NULL,
            plugin_key TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (owner_account_id, plugin_key)
        )
        """
    )
    conn.execute("INSERT INTO plugin_preferences VALUES ('', 'browser', 1, 1.0)")
    conn.commit()
    conn.close()

    PluginPreferencesStore(str(db))
    rows = sqlite3.connect(db).execute("SELECT owner_account_id FROM plugin_preferences").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID,)]


def test_notifications_store_backfill_on_open(tmp_path: Path):
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE notifications (
            id TEXT PRIMARY KEY,
            owner_account_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            read_at REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO notifications (id, owner_account_id, created_at) VALUES ('n1', '', 1.0)"
    )
    conn.commit()
    conn.close()

    NotificationStore(str(db))
    rows = sqlite3.connect(db).execute("SELECT owner_account_id FROM notifications").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID,)]


def test_compaction_store_backfill_on_open(tmp_path: Path):
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE compaction_summaries (
            owner_account_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            covered_count INTEGER NOT NULL,
            ineffective_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (owner_account_id, session_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO compaction_summaries VALUES ('', 's1', '摘要', 3, 0, 1.0)"
    )
    conn.commit()
    conn.close()

    SummaryStore(str(db))
    rows = sqlite3.connect(db).execute("SELECT owner_account_id FROM compaction_summaries").fetchall()
    assert rows == [(LOCAL_OWNER_ACCOUNT_ID,)]


if __name__ == "__main__":
    pytest.main([__file__])
