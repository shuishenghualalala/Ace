"""Gateway Owner 会话租约的 SQLite 事实源。

会话租约按 ``owner_account_id`` 唯一，而不是把整个 Gateway 锁成一个账号。
渠道或其它确实需要排他的资源应在各自资源边界加锁，不能复用本模块作为全局登录锁。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


@dataclass(frozen=True)
class ActiveOwnerLease:
    """一个 Owner 的最近认证会话及其权威验证时间。"""

    owner_account_id: str
    claimed_at: float
    verified_at: float


class ActiveOwnerConflict(RuntimeError):
    """保留供旧调用方导入；按 Owner 会话模型不再用于跨账号登录冲突。"""


class ActiveOwnerLeaseStore:
    """按 Owner 持久化 Gateway 会话租约和重启退出意图。"""

    _SESSION_TABLE = "owner_session_lease"
    _LOGOUT_TABLE = "owner_logout_intent"

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
            f"""
            CREATE TABLE IF NOT EXISTS {self._SESSION_TABLE} (
                owner_account_id TEXT PRIMARY KEY,
                claimed_at REAL NOT NULL,
                verified_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._LOGOUT_TABLE} (
                owner_account_id TEXT PRIMARY KEY,
                requested_at REAL NOT NULL
            )
            """
        )
        self._migrate_legacy_rows()

    def _migrate_legacy_rows(self) -> None:
        """Copy the old singleton lease into the owner-scoped tables once."""

        tables = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "active_owner_lease" in tables:
            legacy = self._conn.execute(
                """
                SELECT owner_account_id, claimed_at, verified_at
                FROM active_owner_lease
                """
            ).fetchone()
            if legacy is not None:
                self._conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {self._SESSION_TABLE}
                        (owner_account_id, claimed_at, verified_at)
                    VALUES (?, ?, ?)
                    """,
                    (str(legacy[0]), float(legacy[1]), float(legacy[2])),
                )
        if "active_owner_logout_intent" in tables:
            legacy_intent = self._conn.execute(
                """
                SELECT owner_account_id, requested_at
                FROM active_owner_logout_intent
                """
            ).fetchone()
            if legacy_intent is not None:
                self._conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {self._LOGOUT_TABLE}
                        (owner_account_id, requested_at)
                    VALUES (?, ?)
                    """,
                    (str(legacy_intent[0]), float(legacy_intent[1])),
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

    @staticmethod
    def _normalize_owner(owner_account_id: str) -> str:
        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 必填")
        return owner

    def get(self, owner_account_id: str) -> ActiveOwnerLease | None:
        """返回指定 Owner 的会话租约。"""

        owner = str(owner_account_id or "").strip()
        if not owner:
            return None
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT owner_account_id, claimed_at, verified_at
                FROM {self._SESSION_TABLE}
                WHERE owner_account_id = ?
                """,
                (owner,),
            ).fetchone()
        return self._lease_from_row(row)

    def list(self) -> list[ActiveOwnerLease]:
        """返回所有已登记的 Owner 会话。"""

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT owner_account_id, claimed_at, verified_at
                FROM {self._SESSION_TABLE}
                ORDER BY owner_account_id
                """
            ).fetchall()
        return [lease for row in rows if (lease := self._lease_from_row(row)) is not None]

    def current(self, owner_account_id: str | None = None) -> ActiveOwnerLease | None:
        """兼容旧调用；新代码必须传 Owner，避免重新引入全局身份语义。"""

        if owner_account_id is not None:
            return self.get(owner_account_id)
        leases = self.list()
        return leases[0] if len(leases) == 1 else None

    def claim(
        self,
        owner_account_id: str,
        *,
        verified_at: float | None = None,
    ) -> ActiveOwnerLease:
        """原子取得或刷新指定 Owner 的会话租约。"""

        owner = self._normalize_owner(owner_account_id)
        verified = float(time.time() if verified_at is None else verified_at)

        def _claim(conn: sqlite3.Connection) -> ActiveOwnerLease:
            row = conn.execute(
                f"""
                SELECT owner_account_id, claimed_at, verified_at
                FROM {self._SESSION_TABLE}
                WHERE owner_account_id = ?
                """,
                (owner,),
            ).fetchone()
            lease = self._lease_from_row(row)
            if lease is None:
                conn.execute(
                    f"""
                    INSERT INTO {self._SESSION_TABLE}
                        (owner_account_id, claimed_at, verified_at)
                    VALUES (?, ?, ?)
                    """,
                    (owner, verified, verified),
                )
                return ActiveOwnerLease(owner, verified, verified)
            if verified <= lease.verified_at + self._refresh_interval_seconds:
                return lease
            next_verified = max(lease.verified_at, verified)
            conn.execute(
                f"""
                UPDATE {self._SESSION_TABLE}
                SET verified_at = ?
                WHERE owner_account_id = ?
                """,
                (next_verified, owner),
            )
            return ActiveOwnerLease(owner, lease.claimed_at, next_verified)

        return self._writer.execute(_claim)

    def release(self, owner_account_id: str) -> bool:
        """释放指定 Owner 的会话租约，不触碰其它账号。"""

        owner = str(owner_account_id or "").strip()
        if not owner:
            return False

        def _release(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                f"DELETE FROM {self._SESSION_TABLE} WHERE owner_account_id = ?",
                (owner,),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    f"DELETE FROM {self._LOGOUT_TABLE} WHERE owner_account_id = ?",
                    (owner,),
                )
            return cursor.rowcount == 1

        return self._writer.execute(_release)

    def prepare_restart_logout(self, owner_account_id: str) -> bool:
        """记录指定 Owner 的重启退出意图，同时保留其资源清理边界。"""

        owner = str(owner_account_id or "").strip()
        if not owner:
            return False

        def _prepare(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                f"SELECT 1 FROM {self._SESSION_TABLE} WHERE owner_account_id = ?",
                (owner,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                f"""
                INSERT INTO {self._LOGOUT_TABLE} (owner_account_id, requested_at)
                VALUES (?, ?)
                ON CONFLICT(owner_account_id) DO UPDATE SET
                    requested_at = excluded.requested_at
                """,
                (owner, time.time()),
            )
            return True

        return self._writer.execute(_prepare)

    def pending_restart_logouts(self) -> list[str]:
        """返回所有需要在 Gateway 重启边界完成的 Owner 退出。"""

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT owner_account_id
                FROM {self._LOGOUT_TABLE}
                ORDER BY requested_at, owner_account_id
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def pending_restart_logout(self) -> str | None:
        """兼容旧调用，返回最早的一条退出意图。"""

        pending = self.pending_restart_logouts()
        return pending[0] if pending else None

    def complete_restart_logout(self, owner_account_id: str | None = None) -> list[str] | str | None:
        """原子完成一个或全部重启退出，并释放对应 Owner 会话。"""

        requested_owner = str(owner_account_id or "").strip()

        def _complete(conn: sqlite3.Connection) -> list[str]:
            if requested_owner:
                rows = conn.execute(
                    f"SELECT owner_account_id FROM {self._LOGOUT_TABLE} WHERE owner_account_id = ?",
                    (requested_owner,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT owner_account_id FROM {self._LOGOUT_TABLE} ORDER BY requested_at, owner_account_id"
                ).fetchall()
            owners = [str(row[0]) for row in rows]
            for owner in owners:
                conn.execute(
                    f"DELETE FROM {self._SESSION_TABLE} WHERE owner_account_id = ?",
                    (owner,),
                )
                conn.execute(
                    f"DELETE FROM {self._LOGOUT_TABLE} WHERE owner_account_id = ?",
                    (owner,),
                )
            return owners

        owners = self._writer.execute(_complete)
        if requested_owner:
            return requested_owner if owners else None
        return owners

    def close(self) -> None:
        """关闭 Store 持有的 SQLite 连接。"""

        with self._lock:
            self._conn.close()
