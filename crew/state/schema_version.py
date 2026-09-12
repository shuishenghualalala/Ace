"""Per-Feature SQLite schema 版本表助手 + copy-on-first-activate 迁移。

约定：每张库表归属唯一 Feature；Feature 的 schema 版本记录在自己的单行表
``<feature>_schema_version``（version + updated_at）里。同一数据库中多个
Feature 的版本表共存，互不影响。本模块只负责登记与读取版本号，不触碰任何
业务表——表结构演进始终由各 Feature 自己的迁移代码显式执行。

写入层强制单调递增：任何回退或平写都会抛 :class:`SchemaVersionError`。
所有 SQL 参数化；表名无法参数化，因此 feature 名被严格限定为
``[a-z][a-z0-9_]*`` 标识符片段后才允许拼接。

Feature 拆库（db-per-feature）的存量数据迁移也收敛在本模块：目标库
ensure-schema + 版本 stamp 之后，用 :func:`copy_legacy_feature_rows` 把旧
主库中的本 Feature 表行一次性复制过来（幂等，单事务，不半写）。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

_FEATURE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SchemaVersionError(RuntimeError):
    """Feature 名非法，或版本写入违反单调递增约束。"""


def version_table_name(feature: str) -> str:
    """Return the version table name for a validated feature token."""
    if not _FEATURE_RE.fullmatch(feature):
        raise SchemaVersionError(f"feature 名需匹配 [a-z][a-z0-9_]*: {feature!r}")
    return f"{feature}_schema_version"


def ensure_version_table(conn: sqlite3.Connection, feature: str) -> None:
    """Create the feature's single-row version table when missing (幂等)."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {version_table_name(feature)} ("
        "singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1), "
        "version INTEGER NOT NULL, "
        "updated_at REAL NOT NULL)"
    )


def current_version(conn: sqlite3.Connection, feature: str) -> int:
    """Return the recorded schema version; 表或记录缺失时返回 0。"""
    table = version_table_name(feature)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute(
        f"SELECT version FROM {table} WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def apply_version(conn: sqlite3.Connection, feature: str, version: int) -> None:
    """Advance the feature's schema version；只接受严格递增的写入。"""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SchemaVersionError(f"schema 版本必须是正整数: {version!r}")
    current = current_version(conn, feature)
    if version <= current:
        raise SchemaVersionError(
            f"{feature} schema 版本只能递增：当前 v{current}，拒绝写入 v{version}"
        )
    ensure_version_table(conn, feature)
    table = version_table_name(feature)
    stamp = time.time()
    updated = conn.execute(
        f"UPDATE {table} SET version = ?, updated_at = ? WHERE singleton = 1",
        (version, stamp),
    )
    if updated.rowcount == 0:
        conn.execute(
            f"INSERT INTO {table} (singleton, version, updated_at) VALUES (1, ?, ?)",
            (version, stamp),
        )


def stamp_baseline(conn: sqlite3.Connection, feature: str, *, version: int) -> int:
    """Record the initial schema baseline once；重复调用不改动任何记录。

    供 Feature 装配入口在 ensure-schema 之后调用：首次运行补写版本记录，
    已有库（无论已登记到哪个版本）保持原样，不重建表也不回退。返回当前生效版本。
    """
    ensure_version_table(conn, feature)
    if current_version(conn, feature) == 0:
        apply_version(conn, feature, version)
    return current_version(conn, feature)


def _copy_legacy_rows(
    legacy_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, int]:
    """Copy rows table by table; callers own the transaction boundary."""

    copied: dict[str, int] = {}
    for table in tables:
        legacy_has = legacy_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if legacy_has is None:
            copied[table] = 0
            continue
        if target_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is None:
            raise SchemaVersionError(
                f"目标库缺少表 {table}：copy 前必须先完成目标库 ensure-schema"
            )
        columns = [str(row[1]) for row in legacy_conn.execute(f"PRAGMA table_info({table})")]
        column_list = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = legacy_conn.execute(f"SELECT {column_list} FROM {table}").fetchall()
        target_conn.executemany(
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})", rows
        )
        copied[table] = len(rows)
    return copied


def copy_legacy_feature_rows(
    legacy_db_path: str | Path,
    target_conn: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, int]:
    """把旧主库中本 Feature 的表行一次性复制到目标库（单事务，幂等）。

    语义（ADR-0038 copy-on-first-activate）：

    - 旧库文件缺失，或某表在旧库不存在 → 跳过该表（计 0 行）；
    - 目标库任何一张本 Feature 表已有行 → 整体跳过（幂等：目标库已有行即
      整体不复制）。gate 是域级的而非逐表的：逐表跳过会在域内存在外键时
      复制出跨表孤儿——父表因已有行被跳过后，空的子表仍会从旧库复制引用
      父表的行；
    - 其余情况整表复制，全部写入发生在同一个 SAVEPOINT 内——任一失败
      整体回滚，目标库不产生半写；
    - 旧表保留在旧库不删，作为天然回退与备份。

    目标连接的事务状态由调用方决定：本函数用 SAVEPOINT 保证在 autocommit
    连接（独立调用）与 ``BEGIN IMMEDIATE`` 写事务（经 SQLiteWriteHelper 调用）
    两种上下文下都是原子的。返回 ``{表名: 复制行数}``，跳过的表计 0。
    """

    for table in tables:
        if not _FEATURE_RE.fullmatch(table):
            raise SchemaVersionError(f"表名需匹配 [a-z][a-z0-9_]*: {table!r}")
    legacy_path = Path(legacy_db_path)
    if not legacy_path.exists():
        return {table: 0 for table in tables}
    for table in tables:
        exists = target_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is not None and int(
            target_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ) > 0:
            return {table: 0 for table in tables}
    legacy_conn = sqlite3.connect(str(legacy_path), timeout=1.0)
    try:
        target_conn.execute("SAVEPOINT copy_legacy_feature_rows")
        try:
            copied = _copy_legacy_rows(legacy_conn, target_conn, tables)
        except BaseException:
            target_conn.execute("ROLLBACK TO copy_legacy_feature_rows")
            target_conn.execute("RELEASE copy_legacy_feature_rows")
            raise
        target_conn.execute("RELEASE copy_legacy_feature_rows")
        return copied
    finally:
        legacy_conn.close()
