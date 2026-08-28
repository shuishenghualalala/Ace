"""通知中心的 SQLite 持久化。表：notifications（建在共享 crew.db）。

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
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

# 每个 (owner, source) 最多保留的通知条数
MAX_PER_SOURCE = 200


class NotificationStore:
    """通知的持久化存储。read_at 为 NULL 表示未读。"""

    def __init__(self, db_path: str = "crew_data/crew.db", *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

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

    def mark_read_by_payload(self, source: str, key: str, owner_account_id: str = "") -> int:
        """把 payload 顶层任一值等于 key 的未读通知标记已读。owner 为空时跨 owner 匹配。"""

        def _write(conn) -> int:
            sql = (
                "UPDATE notifications SET read_at = ? "
                "WHERE source = ? AND read_at IS NULL AND payload != '' "
                "AND EXISTS (SELECT 1 FROM json_each(notifications.payload) WHERE json_each.value = ?)"
            )
            params: tuple = (time.time(), str(source), str(key))
            if owner_account_id:
                sql += " AND owner_account_id = ?"
                params = (*params, owner_account_id)
            cur = conn.execute(sql, params)
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
