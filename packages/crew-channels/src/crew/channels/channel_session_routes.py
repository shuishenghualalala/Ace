"""渠道稳定 key → 当前会话的路由存储。表：channel_session_routes（独立库 crew_data/channels.db）。

建表与读写原先坐在 core state 的 session_store.py（审计 P2-7 归属错位），
随 channels 域拆库（ADR-0038 最后一批）整体归位到本模块；sessions 表仍在
主库由 SQLiteSessionStore 负责，两者自此各自持有连接。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from crew.channels.channel_bindings import CHANNELS_SCHEMA_FEATURE, CHANNELS_SCHEMA_VERSION
from crew.state._migration import CHANNELS_ROUTES_DB_TABLES, backfill_empty_owner_rows
from crew.state.logging import get_logger
from crew.state.schema_version import copy_legacy_feature_rows, stamp_baseline
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("channels.channel_session_routes")


class ChannelSessionRouteStore:
    """渠道会话路由的持久化读写（owner + 渠道稳定 key → session_id）。"""

    def __init__(
        self,
        db_path: str = "crew_data/channels.db",
        *,
        wal_enabled: bool = True,
        legacy_db_path: str | Path | None = None,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)
        if legacy_db_path is not None:
            self._migrate_legacy_rows(Path(legacy_db_path))

    def _migrate_legacy_rows(self, legacy_path: Path) -> None:
        """copy-on-first-activate：目标库为空时从旧主库整表复制路由行。

        gate 在 ensure-schema + channels_schema_version stamp 之后（构造顺序
        保证）。channel_bindings 表由同库的 ChannelBindingsStore 构造时各自
        复制（域内无外键，两表独立 gate 无孤儿风险）。旧表保留在旧库不删
        （ADR-0038 回退备份）；目标表已有行即整体跳过，重复构造幂等零重复；
        复制走单事务，失败不半写。回退配置把 channels 库指回旧库同一文件时
        直接跳过。
        """

        if legacy_path.resolve() == self._path.resolve():
            return
        copied = self._writer.execute(
            lambda conn: copy_legacy_feature_rows(legacy_path, conn, CHANNELS_ROUTES_DB_TABLES)
        )
        if any(copied.values()):
            log.info("已从 %s 迁移渠道会话路由历史数据: %s", legacy_path, copied)

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_session_routes (
                owner_account_id TEXT NOT NULL,
                session_key     TEXT NOT NULL,
                session_id      TEXT NOT NULL,
                updated_at      REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_key)
            )
            """
        )
        # 历史 owner='' 行归属本机 local（owner 统一后不存在无主路由）。
        backfill_empty_owner_rows(conn, ["channel_session_routes"])
        # 6H：Feature 级 schema 版本登记（幂等），必须先于 legacy 行复制，
        # 保证 copy-on-first-activate 的 gate 顺序（ensure-schema → stamp → copy）。
        stamp_baseline(conn, CHANNELS_SCHEMA_FEATURE, version=CHANNELS_SCHEMA_VERSION)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        with self._lock:
            self._conn.close()

    # ---- 渠道稳定 key -> 当前实际 session ----
    def get_channel_session(self, session_key: str, owner_account_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM channel_session_routes "
                "WHERE owner_account_id = ? AND session_key = ?",
                (owner_account_id, session_key),
            ).fetchone()
        return str(row[0]) if row else None

    def set_channel_session(
        self,
        session_key: str,
        session_id: str,
        owner_account_id: str,
    ) -> None:
        now = time.time()

        def _write(conn):
            conn.execute(
                """
                INSERT INTO channel_session_routes (
                    owner_account_id, session_key, session_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_account_id, session_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (owner_account_id, session_key, session_id, now),
            )

        self._writer.execute(_write)

    def get_channel_session_key(
        self,
        session_id: str,
        owner_account_id: str,
    ) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_key FROM channel_session_routes "
                "WHERE owner_account_id = ? AND session_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (owner_account_id, session_id),
            ).fetchone()
        return str(row[0]) if row else None
