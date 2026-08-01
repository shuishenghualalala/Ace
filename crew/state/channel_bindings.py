"""渠道与桌面账号的绑定：谁在本网关连接过该渠道，谁可见渠道历史。"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

from crew.state.logging import get_logger

log = get_logger("state.channel_bindings")


class ChannelBindingsStore:
    """platform → gateway owner_account_id，连接成功时写入。"""

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
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    platform TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    PRIMARY KEY (platform)
                )
                """
            )
            self._conn.commit()

    def bind_on_connect(self, platform: str, owner_account_id: str) -> dict[str, Any]:
        """连接成功时绑定；同账号重连保留原 bound_at，换账号则重置 bound_at。

        首次绑定代表“当前登录账号从此刻接管这个本地网关里的该渠道”。
        bound_at 用当前时间，避免展示绑定前已经存在的渠道历史。
        """
        plat = str(platform or "").strip().lower()
        owner = str(owner_account_id or "").strip()
        if not plat or not owner:
            raise ValueError("platform 与 owner_account_id 必填")
        created = False
        owner_changed = False
        previous_owner = ""
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_account_id, bound_at FROM channel_bindings WHERE platform = ?",
                (plat,),
            ).fetchone()
            if row and str(row[0]) == owner:
                return {
                    "platform": plat,
                    "owner_account_id": owner,
                    "bound_at": float(row[1]),
                    "created": False,
                    "owner_changed": False,
                    "previous_owner_account_id": owner,
                }
            if row:
                previous_owner = str(row[0] or "")
                owner_changed = True
                bound_at = time.time()
            else:
                created = True
                bound_at = time.time()
            self._conn.execute(
                """
                INSERT INTO channel_bindings (platform, owner_account_id, bound_at)
                VALUES (?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    owner_account_id = excluded.owner_account_id,
                    bound_at = excluded.bound_at
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
            "owner_changed": owner_changed,
            "previous_owner_account_id": previous_owner,
        }

    def unbind(self, platform: str) -> None:
        plat = str(platform or "").strip().lower()
        with self._lock:
            self._conn.execute("DELETE FROM channel_bindings WHERE platform = ?", (plat,))
            self._conn.commit()

    def get_binding(self, platform: str) -> str | None:
        plat = str(platform or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_account_id FROM channel_bindings WHERE platform = ?",
                (plat,),
            ).fetchone()
        return str(row[0]) if row else None

    def list_for_owner(self, owner_account_id: str) -> list[dict[str, Any]]:
        owner = str(owner_account_id or "").strip()
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, owner_account_id, bound_at FROM channel_bindings WHERE owner_account_id = ?",
                (owner,),
            ).fetchall()
        return [
            {"platform": plat, "owner_account_id": own, "bound_at": bound_at}
            for plat, own, bound_at in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
