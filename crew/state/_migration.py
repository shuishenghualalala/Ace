"""Small SQLite migration helpers."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("migration")

OWNER_TABLE_LABELS = {
    "sessions": "会话",
    "session_agent_config": "会话 Agent 配置",
    "channel_session_routes": "渠道会话路由",
    "workspaces": "工作空间",
    "cron_jobs": "定时任务",
    "runtime_tasks": "任务",
    "notifications": "通知",
    "compaction_summaries": "压缩摘要",
}

# Feature 拆库归属（ADR-0038）：cron 两表已迁至独立库（crew_data/cron.db），
# 其余 owner 表仍留在主库。后续批次拆库时在此登记新归属，扫描函数即可按库路由。
CRON_DB_TABLES: tuple[str, ...] = ("cron_jobs", "cron_job_runs")


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


def legacy_owner_counts(
    conn: sqlite3.Connection,
    tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Count rows that still use the legacy empty owner.

    tables 缺省扫描 OWNER_TABLE_LABELS 全集；不存在的表自动跳过。
    """

    counts: dict[str, int] = {}
    for table in tables if tables is not None else OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_account_id = ''").fetchone()
        counts[table] = int(row[0] or 0)
    return counts


def claim_legacy_owner_rows(
    conn: sqlite3.Connection,
    owner_account_id: str,
    tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Move legacy empty-owner rows to one explicit account."""

    changed: dict[str, int] = {}
    for table in tables if tables is not None else OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        conn.execute(
            f"UPDATE OR IGNORE {table} SET owner_account_id = ? WHERE owner_account_id = ''",
            (owner_account_id,),
        )
        changed[table] = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    return changed


def backfill_cron_owner_from_sessions(
    session_conn: sqlite3.Connection,
    cron_conn: sqlite3.Connection,
) -> int:
    """Backfill cron owner only when session_id maps to exactly one owner.

    sessions 与 cron_jobs 可能同库也可能分库（ADR-0038 cron 拆库）：分库时
    经参数化行传递匹配结果，不使用跨库 SQL；同库时两个连接是同一连接。
    """

    if not (
        _has_owner_column(session_conn, "sessions")
        and _has_owner_column(cron_conn, "cron_jobs")
    ):
        return 0
    single_owner = {
        str(row[0]): str(row[1])
        for row in session_conn.execute(
            """
            SELECT session_id, MIN(owner_account_id) AS owner_account_id
            FROM sessions
            WHERE owner_account_id != ''
            GROUP BY session_id
            HAVING COUNT(DISTINCT owner_account_id) = 1
            """
        ).fetchall()
    }
    orphan_session_ids = {
        str(row[0])
        for row in cron_conn.execute(
            "SELECT session_id FROM cron_jobs WHERE owner_account_id = ''"
        ).fetchall()
    }
    pairs = [
        (owner, session_id)
        for session_id, owner in single_owner.items()
        if session_id in orphan_session_ids
    ]
    if not pairs:
        return 0
    before = cron_conn.total_changes
    cron_conn.executemany(
        "UPDATE cron_jobs SET owner_account_id = ? "
        "WHERE session_id = ? AND owner_account_id = ''",
        pairs,
    )
    return cron_conn.total_changes - before


def backfill_empty_owner_rows(
    conn: sqlite3.Connection,
    tables: list[str] | None = None,
    *,
    owner_account_id: str = LOCAL_OWNER_ACCOUNT_ID,
) -> dict[str, int]:
    """把空 owner 行自动归一到指定账号（默认本机 ``local``）。

    owner 统一后系统不存在"无主"数据：历史 ``owner=''`` 行属于本机场景。
    回填策略（两步）：
    1. ``UPDATE OR IGNORE`` 逐行归一——与既有目标 owner 行主键冲突的空行跳过；
    2. 结束后仍残留的空行即冲突重复行，按"保归属行、删无主行"清除。
    tables 缺省扫描 OWNER_TABLE_LABELS 全集；不存在的表自动跳过。
    """
    changed: dict[str, int] = {}
    for table in tables or list(OWNER_TABLE_LABELS):
        if not _has_owner_column(conn, table):
            continue
        conn.execute(
            f"UPDATE OR IGNORE {table} SET owner_account_id = ? WHERE owner_account_id = ''",
            (owner_account_id,),
        )
        changed[table] = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
        conn.execute(f"DELETE FROM {table} WHERE owner_account_id = ''")
    return changed


def legacy_owner_scan_targets(
    main_db_path: str | Path,
    cron_db_path: str | Path | None = None,
) -> dict[Path, list[str]]:
    """把 owner 表清单按归属库解析为 ``路径→表清单`` 扫描映射。

    cron 两表归 cron 库（ADR-0038），其余表归主库；两路径指向同一文件时
    （回退配置把 cron_db_path 指回 crew.db）自动合并到同一条目。
    """

    targets: dict[Path, list[str]] = {Path(main_db_path): []}
    targets[Path(main_db_path)].extend(
        table for table in OWNER_TABLE_LABELS if table not in CRON_DB_TABLES
    )
    cron_path = Path(cron_db_path) if cron_db_path else Path(main_db_path)
    targets.setdefault(cron_path, []).extend(CRON_DB_TABLES)
    return targets


def _open_targets(
    targets: Mapping[str | Path, Sequence[str]],
    *,
    wal_enabled: bool,
) -> dict[Path, sqlite3.Connection]:
    return {
        Path(path): connect_sqlite(path, wal_enabled=wal_enabled)
        for path in targets
    }


def inspect_and_backfill_legacy_owners(
    targets: Mapping[str | Path, Sequence[str]],
    *,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], int]:
    """Run startup legacy-owner maintenance across mapped databases.

    targets 是 ``库路径→表清单`` 映射（见 :func:`legacy_owner_scan_targets`）。
    智能回填（cron 按 sessions 唯一归属）优先于 local 兜底，且能跨库工作：
    sessions 与 cron_jobs 各自从归属库读写。返回剩余空 owner 行计数与回填条数。
    """

    conns = _open_targets(targets, wal_enabled=wal_enabled)
    try:
        writers = {
            path: SQLiteWriteHelper(conn, threading.Lock())
            for path, conn in conns.items()
        }

        def route(table: str) -> Path | None:
            for path, tables in targets.items():
                if table in tables and _has_owner_column(conns[Path(path)], table):
                    return Path(path)
            return None

        backfilled = 0
        session_path = route("sessions")
        cron_path = route("cron_jobs")
        if session_path is not None and cron_path is not None:
            backfilled = writers[cron_path].execute(
                lambda c: backfill_cron_owner_from_sessions(conns[session_path], c)
            )
        claimed: dict[str, int] = {}
        for path, tables in targets.items():
            claimed.update(
                writers[Path(path)].execute(
                    lambda c, own_tables=list(tables): backfill_empty_owner_rows(c, own_tables)
                )
            )
        total = sum(claimed.values())
        if total:
            log.info("已将 %d 条无主数据归属本机账号 %s", total, LOCAL_OWNER_ACCOUNT_ID)
        counts: dict[str, int] = {}
        for path, tables in targets.items():
            counts.update(legacy_owner_counts(conns[Path(path)], list(tables)))
        return counts, backfilled
    finally:
        for conn in conns.values():
            conn.close()


def claim_legacy_owner_databases(
    targets: Mapping[str | Path, Sequence[str]],
    owner_account_id: str,
    *,
    dry_run: bool = False,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """Claim empty-owner rows across the mapped feature databases."""

    conns = _open_targets(targets, wal_enabled=wal_enabled)
    try:
        changed: dict[str, int] = {}
        remaining: dict[str, int] = {}
        for path, tables in targets.items():
            conn = conns[Path(path)]
            if dry_run:
                counts = legacy_owner_counts(conn, list(tables))
                changed.update(counts)
                remaining.update(counts)
                continue
            writer = SQLiteWriteHelper(conn, threading.Lock())
            changed.update(
                writer.execute(
                    lambda c, own_tables=list(tables): claim_legacy_owner_rows(
                        c, owner_account_id, own_tables
                    )
                )
            )
            remaining.update(legacy_owner_counts(conn, list(tables)))
        return changed, remaining
    finally:
        for conn in conns.values():
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
