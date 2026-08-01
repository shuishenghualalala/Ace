"""Small SQLite migration helpers."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

OWNER_TABLE_LABELS = {
    "sessions": "会话",
    "session_agent_config": "会话 Agent 配置",
    "workspaces": "工作空间",
    "cron_jobs": "定时任务",
    "runtime_tasks": "任务",
}


def primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return primary key column names ordered by SQLite PK position."""

    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        row[1]
        for row in sorted((r for r in info if int(r[5] or 0) > 0), key=lambda r: int(r[5]))
    ]


def _has_owner_column(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether a table exists and carries owner_account_id."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not row:
        return False
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return "owner_account_id" in cols


def legacy_owner_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count rows that still use the legacy empty owner."""

    counts: dict[str, int] = {}
    for table in OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_account_id = ''").fetchone()
        counts[table] = int(row[0] or 0)
    return counts


def claim_legacy_owner_rows(conn: sqlite3.Connection, owner_account_id: str) -> dict[str, int]:
    """Move legacy empty-owner rows to one explicit account."""

    changed: dict[str, int] = {}
    for table in OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        conn.execute(
            f"UPDATE OR IGNORE {table} SET owner_account_id = ? WHERE owner_account_id = ''",
            (owner_account_id,),
        )
        changed[table] = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    return changed


def backfill_cron_owner_from_sessions(conn: sqlite3.Connection) -> int:
    """Backfill cron owner only when session_id maps to exactly one owner."""

    if not (_has_owner_column(conn, "cron_jobs") and _has_owner_column(conn, "sessions")):
        return 0
    conn.execute(
        """
        WITH single_owner AS (
            SELECT session_id, MIN(owner_account_id) AS owner_account_id
            FROM sessions
            WHERE owner_account_id != ''
            GROUP BY session_id
            HAVING COUNT(DISTINCT owner_account_id) = 1
        )
        UPDATE cron_jobs
        SET owner_account_id = (
            SELECT single_owner.owner_account_id
            FROM single_owner
            WHERE single_owner.session_id = cron_jobs.session_id
        )
        WHERE owner_account_id = ''
          AND session_id IN (SELECT session_id FROM single_owner)
        """
    )
    return int(conn.execute("SELECT changes()").fetchone()[0] or 0)


def inspect_and_backfill_legacy_owners(
    db_path: str | Path,
    *,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], int]:
    """Run startup legacy-owner maintenance and return remaining counts."""

    conn = connect_sqlite(db_path, wal_enabled=wal_enabled)
    try:
        writer = SQLiteWriteHelper(conn, threading.Lock())
        backfilled = writer.execute(backfill_cron_owner_from_sessions)
        return legacy_owner_counts(conn), backfilled
    finally:
        conn.close()


def claim_legacy_owner_database(
    db_path: str | Path,
    owner_account_id: str,
    *,
    dry_run: bool = False,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """Claim empty-owner rows in the configured database."""

    conn = connect_sqlite(db_path, wal_enabled=wal_enabled)
    try:
        if dry_run:
            counts = legacy_owner_counts(conn)
            return counts, counts
        writer = SQLiteWriteHelper(conn, threading.Lock())
        changed = writer.execute(lambda c: claim_legacy_owner_rows(c, owner_account_id))
        return changed, legacy_owner_counts(conn)
    finally:
        conn.close()


def rebuild_table_pk(
    conn: sqlite3.Connection,
    *,
    table: str,
    expected_pk: list[str],
    new_ddl: str,
    copy_sql: str,
) -> bool:
    """Rebuild a table when its primary key does not match the expected shape."""

    if primary_key_columns(conn, table) == expected_pk:
        return False
    conn.execute(new_ddl)
    conn.execute(copy_sql)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    return True
