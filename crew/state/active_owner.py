"""Gateway Active Owner 排他租约的 SQLite 事实源。"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


@dataclass(frozen=True)
class ActiveOwnerLease:
    """当前 Gateway 唯一登录 Owner 及其最近权威验证时间。"""

    owner_account_id: str
    claimed_at: float
    verified_at: float


class ActiveOwnerConflict(RuntimeError):
    """另一个 Owner 已持有排他租约。"""


class ActiveOwnerLeaseStore:
    """以 SQLite 单行记录原子管理 Gateway 的唯一 Active Owner。

    普通连接断开不会触碰该记录。调用方只能在明确登录/权威心跳成功时
    ``claim``，并在显式 Logout 或权威 session 过期后 ``release``。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        wal_enabled: bool = True,
        refresh_interval_seconds: float = 1.0,
    ) -> None:
        self._lock = threading.RLock()
        self._refresh_interval_seconds = max(0.0, float(refresh_interval_seconds))
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_owner_lease (
                lease_id INTEGER PRIMARY KEY CHECK (lease_id = 1),
                owner_account_id TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                verified_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_owner_logout_intent (
                intent_id INTEGER PRIMARY KEY CHECK (intent_id = 1),
                owner_account_id TEXT NOT NULL,
                requested_at REAL NOT NULL
            )
            """
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row | tuple | None) -> ActiveOwnerLease | None:
        if row is None:
            return None
        return ActiveOwnerLease(
            owner_account_id=str(row[0]),
            claimed_at=float(row[1]),
            verified_at=float(row[2]),
        )

    def current(self) -> ActiveOwnerLease | None:
        """返回当前租约快照；没有登录 Owner 时返回 ``None``。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT owner_account_id, claimed_at, verified_at
                FROM active_owner_lease
                WHERE lease_id = 1
                """
            ).fetchone()
        return self._lease_from_row(row)

    def claim(
        self,
        owner_account_id: str,
        *,
        verified_at: float | None = None,
    ) -> ActiveOwnerLease:
        """原子取得或刷新租约；其他 Owner 已占用时 fail closed。"""

        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 必填")
        verified = float(time.time() if verified_at is None else verified_at)

        def _claim(conn: sqlite3.Connection) -> ActiveOwnerLease:
            row = conn.execute(
                """
                SELECT owner_account_id, claimed_at, verified_at
                FROM active_owner_lease
                WHERE lease_id = 1
                """
            ).fetchone()
            lease = self._lease_from_row(row)
            if lease is None:
                conn.execute(
                    """
                    INSERT INTO active_owner_lease (
                        lease_id, owner_account_id, claimed_at, verified_at
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (owner, verified, verified),
                )
                return ActiveOwnerLease(owner, verified, verified)
            if lease.owner_account_id != owner:
                raise ActiveOwnerConflict("Gateway 已由其他账号登录")
            # Keep the ownership check and successful return inside the same
            # BEGIN IMMEDIATE boundary.  This coalesces WAL updates without a
            # stale read-return window where another connection can hand off.
            if verified <= lease.verified_at + self._refresh_interval_seconds:
                return lease
            next_verified = max(lease.verified_at, verified)
            conn.execute(
                """
                UPDATE active_owner_lease
                SET verified_at = ?
                WHERE lease_id = 1 AND owner_account_id = ?
                """,
                (next_verified, owner),
            )
            return ActiveOwnerLease(owner, lease.claimed_at, next_verified)

        return self._writer.execute(_claim)

    def release(self, owner_account_id: str) -> bool:
        """仅当调用方仍是当前 Owner 时释放租约。"""

        owner = str(owner_account_id or "").strip()
        if not owner:
            return False

        def _release(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                DELETE FROM active_owner_lease
                WHERE lease_id = 1 AND owner_account_id = ?
                """,
                (owner,),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    DELETE FROM active_owner_logout_intent
                    WHERE intent_id = 1 AND owner_account_id = ?
                    """,
                    (owner,),
                )
            return cursor.rowcount == 1

        return self._writer.execute(_release)

    def prepare_restart_logout(self, owner_account_id: str) -> bool:
        """Persist a logout intent while retaining the lease across process restart.

        This is used when a channel SDK cannot be stopped in-process.  Keeping
        the lease prevents another owner from connecting until process death
        has physically torn down the old channel connection.
        """

        owner = str(owner_account_id or "").strip()
        if not owner:
            return False

        def _prepare(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT owner_account_id FROM active_owner_lease WHERE lease_id = 1"
            ).fetchone()
            if row is None or str(row[0]) != owner:
                return False
            conn.execute(
                """
                INSERT INTO active_owner_logout_intent (
                    intent_id, owner_account_id, requested_at
                ) VALUES (1, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    owner_account_id = excluded.owner_account_id,
                    requested_at = excluded.requested_at
                """,
                (owner, time.time()),
            )
            return True

        return self._writer.execute(_prepare)

    def pending_restart_logout(self) -> str | None:
        """Return the owner whose logout must complete after Gateway restart."""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT owner_account_id
                FROM active_owner_logout_intent
                WHERE intent_id = 1
                """
            ).fetchone()
        return str(row[0]) if row is not None else None

    def complete_restart_logout(self) -> str | None:
        """Atomically release the retained lease after the old process has died."""

        def _complete(conn: sqlite3.Connection) -> str | None:
            intent = conn.execute(
                """
                SELECT owner_account_id
                FROM active_owner_logout_intent
                WHERE intent_id = 1
                """
            ).fetchone()
            if intent is None:
                return None
            owner = str(intent[0])
            lease = conn.execute(
                "SELECT owner_account_id FROM active_owner_lease WHERE lease_id = 1"
            ).fetchone()
            if lease is not None and str(lease[0]) != owner:
                raise ActiveOwnerConflict("退出重启意图与当前 Active Owner 不一致")
            if lease is not None:
                conn.execute(
                    """
                    DELETE FROM active_owner_lease
                    WHERE lease_id = 1 AND owner_account_id = ?
                    """,
                    (owner,),
                )
            conn.execute(
                "DELETE FROM active_owner_logout_intent WHERE intent_id = 1"
            )
            return owner

        return self._writer.execute(_complete)

    def close(self) -> None:
        """关闭 Store 持有的 SQLite 连接。"""

        with self._lock:
            self._conn.close()
