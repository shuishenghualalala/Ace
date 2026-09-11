"""Per-Feature SQLite schema 版本表助手。

约定：每张库表归属唯一 Feature；Feature 的 schema 版本记录在自己的单行表
``<feature>_schema_version``（version + updated_at）里。同一数据库中多个
Feature 的版本表共存，互不影响。本模块只负责登记与读取版本号，不触碰任何
业务表——表结构演进始终由各 Feature 自己的迁移代码显式执行。

写入层强制单调递增：任何回退或平写都会抛 :class:`SchemaVersionError`。
所有 SQL 参数化；表名无法参数化，因此 feature 名被严格限定为
``[a-z][a-z0-9_]*`` 标识符片段后才允许拼接。
"""

from __future__ import annotations

import re
import sqlite3
import time

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
