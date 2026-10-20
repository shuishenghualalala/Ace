"""通知中心的 SQLite 持久化。表：notifications（独立库 crew_data/notifications.db）。

每个来源（source）按 owner 维度独立保留最近 N 条，publish 时顺手裁剪，
避免通知表无限增长。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from crew.core.interfaces import Notification
from crew.state._migration import NOTIFICATIONS_DB_TABLES, backfill_empty_owner_rows
from crew.state.logging import get_logger
from crew.state.schema_version import copy_legacy_feature_rows, stamp_baseline
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("notifications.store")

# 每个 (owner, source) 最多保留的通知条数
MAX_PER_SOURCE = 200

# Feature 级 schema 版本（6H）：notifications 域单表 notifications，一张单行
# 版本表，当前结构登记为基线 v1；表结构演进在此号上递增并配迁移步骤。
# 常量放在 store 模块（与 cron/kanban 同法，避免循环导入）。
NOTIFICATIONS_SCHEMA_FEATURE = "notifications"
NOTIFICATIONS_SCHEMA_VERSION = 1


class NotificationStore:
    """通知的持久化存储。read_at 为 NULL 表示未读。"""

    def __init__(
        self,
        db_path: str = "crew_data/notifications.db",
        *,
        wal_enabled: bool = True,
        legacy_db_path: str | Path | None = None,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)
        if legacy_db_path is not None:
            self._migrate_legacy_rows(Path(legacy_db_path))

    def close(self) -> None:
        """关闭底层 SQLite 连接（幂等；仅测试/显式释放用，生命周期仍随宿主）。"""
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            self._conn.close()

    def _migrate_legacy_rows(self, legacy_path: Path) -> None:
        """copy-on-first-activate：目标库为空时从旧主库整表复制通知行。

        gate 在 ensure-schema + notifications_schema_version stamp 之后（构造
        顺序保证）。旧表保留在旧库不删（ADR-0038 回退备份）；目标库已有行即
        整体跳过，重复构造幂等零重复；复制走单事务，失败不半写。回退配置把
        notifications 库指回旧库同一文件时直接跳过。
        """

        if legacy_path.resolve() == self._path.resolve():
            return
        copied = self._writer.execute(
            lambda conn: copy_legacy_feature_rows(legacy_path, conn, NOTIFICATIONS_DB_TABLES)
        )
        if any(copied.values()):
            log.info("已从 %s 迁移 notifications 历史数据: %s", legacy_path, copied)

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id               TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                source           TEXT NOT NULL DEFAULT '',
                kind             TEXT NOT NULL DEFAULT '',
                title            TEXT NOT NULL DEFAULT '',
                body             TEXT NOT NULL DEFAULT '',
                payload          TEXT NOT NULL DEFAULT '',
                created_at       REAL NOT NULL,
                read_at          REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_owner_read "
            "ON notifications(owner_account_id, read_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_owner_created "
            "ON notifications(owner_account_id, created_at DESC)"
        )
        # 历史 owner='' 行归属本机 local（owner 统一后不存在无主通知）。
        backfill_empty_owner_rows(conn, ["notifications"])
        # 6H：Feature 级 schema 版本登记（幂等），必须先于 legacy 行复制，
        # 保证 copy-on-first-activate 的 gate 顺序（ensure-schema → stamp → copy）。
        stamp_baseline(conn, NOTIFICATIONS_SCHEMA_FEATURE, version=NOTIFICATIONS_SCHEMA_VERSION)

    @staticmethod
    def _row_to_notification(row: sqlite3.Row) -> Notification:
        raw_payload = str(row["payload"] or "")
        payload = None
        if raw_payload:
            try:
                parsed = json.loads(raw_payload)
                payload = parsed if isinstance(parsed, dict) else None
            except (TypeError, ValueError):
                payload = None
        return Notification(
            id=str(row["id"]),
            owner_account_id=str(row["owner_account_id"]),
            source=str(row["source"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            body=str(row["body"]),
            payload=payload,
            created_at=float(row["created_at"]),
            read_at=float(row["read_at"]) if row["read_at"] is not None else None,
        )

    def insert(self, notification: Notification, *, max_per_source: int = MAX_PER_SOURCE) -> Notification:
        """写入一条通知，并把同 (owner, source) 的通知裁剪到最近 max_per_source 条。"""
        if not notification.id:
            notification.id = uuid.uuid4().hex
        if not notification.created_at:
            notification.created_at = time.time()

        def _write(conn) -> None:
            conn.execute(
                "INSERT INTO notifications "
                "(id, owner_account_id, source, kind, title, body, payload, created_at, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notification.id,
                    notification.owner_account_id,
                    notification.source,
                    notification.kind,
                    notification.title,
                    notification.body,
                    json.dumps(notification.payload, ensure_ascii=False) if notification.payload else "",
                    notification.created_at,
                    notification.read_at,
                ),
            )
            conn.execute(
                """
                DELETE FROM notifications
                WHERE owner_account_id = ? AND source = ? AND id NOT IN (
                    SELECT id FROM notifications
                    WHERE owner_account_id = ? AND source = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (
                    notification.owner_account_id,
                    notification.source,
                    notification.owner_account_id,
                    notification.source,
                    max(1, int(max_per_source)),
                ),
            )

        self._writer.execute(_write)
        return notification

    def list(
        self,
        owner_account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> list[Notification]:
        sql = (
            "SELECT * FROM notifications WHERE owner_account_id = ?"
            + (" AND read_at IS NULL" if unread_only else "")
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql,
                (owner_account_id, max(0, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [self._row_to_notification(row) for row in rows]

    def unread_count(self, owner_account_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE owner_account_id = ? AND read_at IS NULL",
                (owner_account_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_read(self, owner_account_id: str, notification_id: str) -> bool:
        def _write(conn) -> int:
            cur = conn.execute(
                "UPDATE notifications SET read_at = ? "
                "WHERE owner_account_id = ? AND id = ? AND read_at IS NULL",
                (time.time(), owner_account_id, str(notification_id)),
            )
            return cur.rowcount

        return self._writer.execute(_write) > 0

    def mark_all_read(self, owner_account_id: str) -> int:
        def _write(conn) -> int:
            cur = conn.execute(
                "UPDATE notifications SET read_at = ? WHERE owner_account_id = ? AND read_at IS NULL",
                (time.time(), owner_account_id),
            )
            return cur.rowcount

        return int(self._writer.execute(_write))

    def mark_read_by_payload(self, source: str, key: str, owner_account_id: str) -> int:
        """把 payload 顶层任一值等于 key 的未读通知标记已读（严格限定 owner）。"""

        def _write(conn) -> int:
            cur = conn.execute(
                "UPDATE notifications SET read_at = ? "
                "WHERE source = ? AND read_at IS NULL AND payload != '' "
                "AND owner_account_id = ? "
                "AND EXISTS (SELECT 1 FROM json_each(notifications.payload) WHERE json_each.value = ?)",
                (time.time(), str(source), owner_account_id, str(key)),
            )
            return cur.rowcount

        return int(self._writer.execute(_write))

    def clear(self, owner_account_id: str) -> int:
        def _write(conn) -> int:
            cur = conn.execute(
                "DELETE FROM notifications WHERE owner_account_id = ?",
                (owner_account_id,),
            )
            return cur.rowcount

        return int(self._writer.execute(_write))
