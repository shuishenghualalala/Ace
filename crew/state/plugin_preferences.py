"""用户级插件开关偏好：per-owner 的 plugin enabled 状态持久化。

系统级（plugins.enabled/disabled 配置）与角色级（access_control 的
enabled_plugins/disabled_plugins）始终优先于本表的账号偏好；本表只表达
"当前账号在自己被允许的范围内是否打开该插件"。

user_enabled 缺省语义（无记录时）：internal 默认开，external 默认关（fail-closed）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("state.plugin_preferences")


class PluginPreferencesStore:
    """plugin_preferences(owner_account_id, plugin_key, enabled, updated_at)。"""

    def __init__(self, db_path: str, *, wal_enabled: bool = True) -> None:
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled)
        self._lock = threading.RLock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_preferences (
                    owner_account_id TEXT NOT NULL,
                    plugin_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_account_id, plugin_key)
                )
                """
            )
            self._conn.commit()

    def get_enabled(self, owner_account_id: str, plugin_key: str) -> bool | None:
        """读取账号对某插件的开关；无记录返回 None（由调用方按 fail-closed 缺省处理）。"""
        owner = str(owner_account_id or "").strip()
        key = str(plugin_key or "").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM plugin_preferences WHERE owner_account_id = ? AND plugin_key = ?",
                (owner, key),
            ).fetchone()
        return bool(row[0]) if row else None

    def set_enabled(self, owner_account_id: str, plugin_key: str, enabled: bool) -> None:
        owner = str(owner_account_id or "").strip()
        key = str(plugin_key or "").strip()
        if not owner or not key:
            raise ValueError("owner_account_id 与 plugin_key 必填")

        def _write(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO plugin_preferences (owner_account_id, plugin_key, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_account_id, plugin_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (owner, key, 1 if enabled else 0, time.time()),
            )

        self._writer.execute(_write)
        log.info("插件偏好写入 owner=%s plugin=%s enabled=%s", owner, key, enabled)

    def list_for_owner(self, owner_account_id: str) -> dict[str, bool]:
        owner = str(owner_account_id or "").strip()
        with self._lock:
            rows = self._conn.execute(
                "SELECT plugin_key, enabled FROM plugin_preferences WHERE owner_account_id = ?",
                (owner,),
            ).fetchall()
        return {str(key): bool(enabled) for key, enabled in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def plugin_role_allowed(ac: dict[str, Any] | None, plugin_key: str) -> bool:
    """角色（access_control）是否允许使用该插件。

    语义对齐 toolset 过滤的三态：disabled 优先；enabled=None/["*"] 不限制；
    enabled 为显式列表时需包含 key；enabled=[] 表示全禁。
    """
    key = str(plugin_key or "").strip()
    if not key:
        return False
    if ac is None:
        return True
    disabled = ac.get("disabled_plugins")
    if disabled is not None and ("*" in disabled or key in disabled):
        return False
    enabled = ac.get("enabled_plugins")
    if enabled is None or "*" in enabled or key in enabled:
        return True
    return False


def plugin_effective_enabled(
    *,
    system_enabled: bool,
    role_allowed: bool,
    user_enabled: bool | None,
    user_type: str,
) -> bool:
    """effective_enabled = system_enabled && role_allowed && user_enabled。

    user_enabled 为 None（无偏好记录）时按角色缺省：internal 默认开，
    其它（含 external）默认关 —— 外部用户必须显式 opt-in，fail-closed。
    """
    if user_enabled is None:
        user_enabled = str(user_type or "").strip().lower() == "internal"
    return bool(system_enabled and role_allowed and user_enabled)
