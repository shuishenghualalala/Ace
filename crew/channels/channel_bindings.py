"""按平台与 Owner 保存渠道连接绑定。表：channel_bindings（独立库 crew_data/channels.db）。"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from crew.state._migration import CHANNELS_BINDINGS_DB_TABLES, backfill_empty_owner_rows
from crew.state.logging import get_logger
from crew.state.schema_version import copy_legacy_feature_rows, stamp_baseline

log = get_logger("channels.channel_bindings")

# Feature 级 schema 版本（6H）：channels 域（本 store 的 channel_bindings 表与
# channel_session_routes 模块的 channel_session_routes 表，同住 channels.db）共用
# 一张 channels_schema_version 表，当前结构登记为基线 v1；表结构演进在此号上
# 递增并配迁移步骤。常量放在本模块（与 sites.store 同法）：routes store /
# management 反向导入这里，放别处会构成循环导入。
CHANNELS_SCHEMA_FEATURE = "channels"
CHANNELS_SCHEMA_VERSION = 1


class ChannelBindingsStore:
    """允许同一平台由多个 Owner 使用各自的渠道实例。"""

    _TABLE = "channel_bindings"

    def __init__(
        self,
        db_path: str = "crew_data/channels.db",
        *,
        wal_enabled: bool = True,
        legacy_db_path: str | Path | None = None,
    ) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        if wal_enabled:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()
        if legacy_db_path is not None:
            self._migrate_legacy_rows(Path(legacy_db_path))

    def _migrate_legacy_rows(self, legacy_path: Path) -> None:
        """copy-on-first-activate：目标库为空时从旧主库整表复制绑定行。

        gate 在 ensure-schema + channels_schema_version stamp 之后（构造顺序
        保证）。channel_session_routes 表由同库的 ChannelSessionRouteStore
        构造时各自复制（域内无外键，两表独立 gate 无孤儿风险）。旧表保留在
        旧库不删（ADR-0038 回退备份）；目标表已有行即整体跳过，重复构造幂等
        零重复；复制走单事务，失败不半写。回退配置把 channels 库指回旧库
        同一文件时直接跳过。
        """

        if legacy_path.resolve() == Path(self._db_path).resolve():
            return
        with self._lock:
            copied = copy_legacy_feature_rows(
                legacy_path, self._conn, CHANNELS_BINDINGS_DB_TABLES
            )
            self._conn.commit()
        if any(copied.values()):
            log.info("已从 %s 迁移渠道绑定历史数据: %s", legacy_path, copied)

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
            # 历史 owner='' 行归属本机 local（owner 统一后不存在无主绑定）。
            backfill_empty_owner_rows(self._conn, [self._TABLE])
            # 6H：Feature 级 schema 版本登记（幂等），必须先于 legacy 行复制，
            # 保证 copy-on-first-activate 的 gate 顺序（ensure-schema → stamp → copy）。
            stamp_baseline(self._conn, CHANNELS_SCHEMA_FEATURE, version=CHANNELS_SCHEMA_VERSION)
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

    def get_binding(self, platform: str, owner_account_id: str) -> str | None:
        """返回指定 Owner 的绑定。"""

        plat = str(platform or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT owner_account_id FROM {self._TABLE}
                WHERE platform = ? AND owner_account_id = ?
                """,
                (plat, owner_account_id),
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
