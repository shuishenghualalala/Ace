"""按平台与 Owner 保存渠道连接绑定。"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

from crew.state.logging import get_logger

log = get_logger("state.channel_bindings")


class ChannelBindingsStore:
    """允许同一平台由多个 Owner 使用各自的渠道实例。"""

    _TABLE = "channel_bindings"

    def __init__(self, db_path: str, *, wal_enabled: bool = True) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        if wal_enabled:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._TABLE} (
                    platform TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    PRIMARY KEY (platform, owner_account_id)
                )
                """
            )
            columns = self._conn.execute(f"PRAGMA table_info({self._TABLE})").fetchall()
            primary_keys = [str(row[1]) for row in columns if int(row[5] or 0) > 0]
            if primary_keys == ["platform"]:
                self._conn.execute(
                    """
                    CREATE TABLE channel_bindings_v2 (
                        platform TEXT NOT NULL,
                        owner_account_id TEXT NOT NULL,
                        bound_at REAL NOT NULL,
                        PRIMARY KEY (platform, owner_account_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_bindings_v2
                        (platform, owner_account_id, bound_at)
                    SELECT platform, owner_account_id, bound_at
                    FROM channel_bindings
                    """
                )
                self._conn.execute("DROP TABLE channel_bindings")
                self._conn.execute("ALTER TABLE channel_bindings_v2 RENAME TO channel_bindings")
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_channel_bindings_owner ON {self._TABLE}(owner_account_id)"
            )
            self._conn.commit()

    @staticmethod
    def _normalize(platform: str, owner_account_id: str) -> tuple[str, str]:
        plat = str(platform or "").strip().lower()
        owner = str(owner_account_id or "").strip()
        if not plat or not owner:
            raise ValueError("platform 与 owner_account_id 必填")
        return plat, owner

    def bind_on_connect(self, platform: str, owner_account_id: str) -> dict[str, Any]:
        """连接成功时登记指定 Owner 的平台实例，不覆盖其它 Owner。"""

        plat, owner = self._normalize(platform, owner_account_id)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT bound_at FROM {self._TABLE}
                WHERE platform = ? AND owner_account_id = ?
                """,
                (plat, owner),
            ).fetchone()
            if row:
                bound_at = float(row[0])
                created = False
            else:
                bound_at = time.time()
                created = True
                self._conn.execute(
                    f"""
                    INSERT INTO {self._TABLE} (platform, owner_account_id, bound_at)
                    VALUES (?, ?, ?)
                    """,
                    (plat, owner, bound_at),
                )
                self._conn.commit()
        log.info("渠道绑定 platform=%s owner=%s", plat, owner)
        return {
            "platform": plat,
            "owner_account_id": owner,
            "bound_at": bound_at,
            "created": created,
            "owner_changed": False,
            "previous_owner_account_id": owner,
        }

    def unbind(self, platform: str, owner_account_id: str | None = None) -> None:
        plat = str(platform or "").strip().lower()
        owner = str(owner_account_id or "").strip()
        with self._lock:
            if owner:
                self._conn.execute(
                    f"DELETE FROM {self._TABLE} WHERE platform = ? AND owner_account_id = ?",
                    (plat, owner),
                )
            else:
                self._conn.execute(f"DELETE FROM {self._TABLE} WHERE platform = ?", (plat,))
            self._conn.commit()

    def get_binding(self, platform: str, owner_account_id: str | None = None) -> str | None:
        """返回指定 Owner 的绑定；无 Owner 参数时保留旧的首条兼容视图。"""

        plat = str(platform or "").strip().lower()
        owner = str(owner_account_id or "").strip()
        with self._lock:
            if owner:
                row = self._conn.execute(
                    f"""
                    SELECT owner_account_id FROM {self._TABLE}
                    WHERE platform = ? AND owner_account_id = ?
                    """,
                    (plat, owner),
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"""
                    SELECT owner_account_id FROM {self._TABLE}
                    WHERE platform = ? ORDER BY bound_at, owner_account_id LIMIT 1
                    """,
                    (plat,),
                ).fetchone()
        return str(row[0]) if row else None

    def list_for_platform(self, platform: str) -> list[dict[str, Any]]:
        plat = str(platform or "").strip().lower()
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT platform, owner_account_id, bound_at
                FROM {self._TABLE}
                WHERE platform = ? ORDER BY bound_at, owner_account_id
                """,
                (plat,),
            ).fetchall()
        return [
            {"platform": row[0], "owner_account_id": row[1], "bound_at": float(row[2])}
            for row in rows
        ]

    def list_for_owner(self, owner_account_id: str) -> list[dict[str, Any]]:
        owner = str(owner_account_id or "").strip()
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT platform, owner_account_id, bound_at
                FROM {self._TABLE}
                WHERE owner_account_id = ? ORDER BY platform
                """,
                (owner,),
            ).fetchall()
        return [
            {"platform": row[0], "owner_account_id": row[1], "bound_at": float(row[2])}
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
