"""Subagent 自有三表持久层（ADR-0038 db-per-feature）。

`subagent_runs` 记子身份与结局（run log 以 JSON 列随行落库）；`subagent_edges`
记父子边（child 为主键、软删除）；`subagent_descriptors` 存版本化不可变
descriptor，写入采用 first-wins——同名后续声明不能改写已发布的出生证明。

三张表只引用不透明 id，不与 Team/Work/Wiki 等域建跨域外键；owner 隔离为
行级 `owner_account_id` 列，与其他 Feature 域一致。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from crew.state._migration import SUBAGENT_DB_TABLES
from crew.state.logging import get_logger
from crew.state.schema_version import copy_legacy_feature_rows, stamp_baseline
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("subagent.store")

SUBAGENT_SCHEMA_FEATURE = "subagent"
SUBAGENT_SCHEMA_VERSION = 1

DESCRIPTOR_VERSION = 1
# 版本化 payload 的合法字段全集：未知字段在读写两侧都拒绝（fail-loud），
# 防止祖先进程用更新词汇污染旧版本可读性。
_DESCRIPTOR_FIELDS = frozenset(
    {
        "version",
        "lifecycle",
        "provider",
        "parent_session_id",
        "label",
        "model",
        "persona",
        "tool_filter",
        "delegation_depth",
        "subagent_id",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subagent_runs (
  subagent_id       TEXT PRIMARY KEY,
  parent_session_id TEXT NOT NULL,
  owner_account_id  TEXT NOT NULL DEFAULT '',
  workspace_id      TEXT,
  depth             INTEGER NOT NULL,
  mode              TEXT NOT NULL,
  provider          TEXT NOT NULL,
  label             TEXT,
  status            TEXT NOT NULL,
  stop_reason       TEXT,
  timeout_kind      TEXT,
  run_log           TEXT NOT NULL DEFAULT '[]',
  created_at        REAL NOT NULL,
  settled_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent
  ON subagent_runs(parent_session_id, owner_account_id);
CREATE TABLE IF NOT EXISTS subagent_edges (
  child_id   TEXT PRIMARY KEY,
  parent_id  TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at REAL NOT NULL,
  closed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_subagent_edges_parent
  ON subagent_edges(parent_id, status);
CREATE TABLE IF NOT EXISTS subagent_descriptors (
  subagent_id TEXT PRIMARY KEY,
  version     INTEGER NOT NULL,
  payload     TEXT NOT NULL,
  created_at  REAL NOT NULL
);
"""


def validate_descriptor_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验并复制一份 descriptor payload；版本不符或字段未知即抛 ValueError。"""

    if not isinstance(payload, Mapping):
        raise ValueError("descriptor payload must be a mapping")
    unknown = set(payload) - _DESCRIPTOR_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"descriptor payload has unknown fields: {names}")
    version = int(payload.get("version", 0) or 0)
    if version != DESCRIPTOR_VERSION:
        raise ValueError(
            f"descriptor version {version} is not supported "
            f"(expected {DESCRIPTOR_VERSION})"
        )
    return dict(payload)


class SubagentStore:
    """Feature 自有库表：runs / edges / descriptors。"""

    def __init__(
        self,
        db_path: str | Path = "crew_data/subagent.db",
        *,
        wal_enabled: bool = True,
        legacy_db_path: str | Path | None = None,
        retain_days: int = 30,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.Lock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._closed = False
        self._writer.execute(self._init_schema)
        if legacy_db_path is not None:
            self._migrate_legacy_rows(Path(legacy_db_path))
        if retain_days > 0:
            self._purge_safely(retain_days)

    # ---- schema / migration ----

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        stamp_baseline(
            conn, SUBAGENT_SCHEMA_FEATURE, version=SUBAGENT_SCHEMA_VERSION
        )

    def _migrate_legacy_rows(self, legacy_path: Path) -> None:
        """copy-on-first-activate：目标库任一本域表已有行即整体跳过。"""

        if legacy_path.resolve() == self._path.resolve():
            return
        copied = self._writer.execute(
            lambda conn: copy_legacy_feature_rows(
                legacy_path, conn, SUBAGENT_DB_TABLES
            )
        )
        if any(copied.values()):
            log.info("已从 %s 迁移 subagent 域表: %s", legacy_path, copied)

    def _purge_safely(self, retain_days: int) -> None:
        try:
            removed = self.purge_expired(retain_days)
            if removed:
                log.info("subagent 过期清理：删除 %d 行（保留 %d 天）", removed, retain_days)
        except Exception:  # noqa: BLE001 - 开库清理失败不阻断装配
            log.exception("subagent 过期清理失败")

    # ---- runs ----

    def record_run_start(
        self,
        *,
        subagent_id: str,
        parent_session_id: str,
        owner_account_id: str,
        workspace_id: str | None,
        depth: int,
        mode: str,
        provider: str,
        label: str | None,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        row = {
            "subagent_id": subagent_id,
            "parent_session_id": parent_session_id,
            "owner_account_id": owner_account_id,
            "workspace_id": workspace_id,
            "depth": int(depth),
            "mode": str(mode),
            "provider": str(provider),
            "label": label,
            "status": "running",
            "run_log": json.dumps(list(events), ensure_ascii=False),
            "created_at": time.time(),
        }

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO subagent_runs(
                       subagent_id, parent_session_id, owner_account_id,
                       workspace_id, depth, mode, provider, label, status,
                       run_log, created_at)
                   VALUES(
                       :subagent_id, :parent_session_id, :owner_account_id,
                       :workspace_id, :depth, :mode, :provider, :label, :status,
                       :run_log, :created_at)
                   ON CONFLICT(subagent_id) DO NOTHING""",
                row,
            )

        self._writer.execute(write)

    def settle_run(
        self,
        subagent_id: str,
        *,
        stop_reason: str,
        timeout_kind: str | None = None,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """终态落库：结局、超时种类与完整 run log（append-only 事件流）。"""

        payload = json.dumps(list(events), ensure_ascii=False)
        settled_at = time.time()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE subagent_runs
                   SET status = 'settled', stop_reason = :stop_reason,
                       timeout_kind = :timeout_kind, run_log = :run_log,
                       settled_at = :settled_at
                   WHERE subagent_id = :subagent_id""",
                {
                    "subagent_id": subagent_id,
                    "stop_reason": stop_reason,
                    "timeout_kind": timeout_kind,
                    "run_log": payload,
                    "settled_at": settled_at,
                },
            )

        self._writer.execute(write)

    def get_run(self, subagent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM subagent_runs WHERE subagent_id = ?",
            (str(subagent_id),),
        ).fetchone()
        return _run_row_to_dict(row)

    def list_runs(
        self,
        *,
        parent_session_id: str | None = None,
        owner_account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if parent_session_id is not None:
            clauses.append("parent_session_id = ?")
            params.append(str(parent_session_id))
        if owner_account_id is not None:
            clauses.append("owner_account_id = ?")
            params.append(str(owner_account_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"SELECT * FROM subagent_runs {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_run_row_to_dict(row) for row in rows]

    # ---- edges ----

    def open_edge(self, child_id: str, parent_id: str) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO subagent_edges(child_id, parent_id, status, created_at)
                   VALUES(?, ?, 'open', ?)
                   ON CONFLICT(child_id) DO NOTHING""",
                (str(child_id), str(parent_id), time.time()),
            )

        self._writer.execute(write)

    def close_edge(self, child_id: str) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE subagent_edges
                   SET status = 'closed', closed_at = ?
                   WHERE child_id = ? AND status = 'open'""",
                (time.time(), str(child_id)),
            )

        self._writer.execute(write)

    def get_edge(self, child_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM subagent_edges WHERE child_id = ?",
            (str(child_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    # ---- descriptors ----

    def write_descriptor(self, payload: Mapping[str, Any]) -> bool:
        """first-wins 写入：已存在同名 descriptor 时保留首份并返回 False。"""

        validated = validate_descriptor_payload(payload)
        subagent_id = str(validated.get("subagent_id") or "").strip()
        if not subagent_id:
            raise ValueError("descriptor payload requires subagent_id")
        version = int(validated["version"])

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT version FROM subagent_descriptors WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if row is not None:
                return False
            conn.execute(
                """INSERT INTO subagent_descriptors(
                       subagent_id, version, payload, created_at)
                   VALUES(?, ?, ?, ?)""",
                (
                    subagent_id,
                    version,
                    json.dumps(validated, ensure_ascii=False),
                    time.time(),
                ),
            )
            return True

        return bool(self._writer.execute(write))

    def read_descriptor(self, subagent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT version, payload FROM subagent_descriptors WHERE subagent_id = ?",
            (str(subagent_id),),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        # 读侧同样校验：库里已损坏/超前的版本不得静默进入运行时。
        return validate_descriptor_payload(payload)

    # ---- retention ----

    def purge_expired(self, retain_days: int, *, now: float | None = None) -> int:
        """删除超过保留期的 run/descriptor/edge（以 settled/created 较晚者计）。"""

        retain_days = int(retain_days)
        if retain_days <= 0:
            return 0
        cutoff = (now if now is not None else time.time()) - retain_days * 86400.0

        def write(conn: sqlite3.Connection) -> int:
            expired = conn.execute(
                """SELECT subagent_id FROM subagent_runs
                   WHERE COALESCE(settled_at, created_at) < ?""",
                (cutoff,),
            ).fetchall()
            ids = [row["subagent_id"] for row in expired]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM subagent_runs WHERE subagent_id IN ({marks})", ids
            )
            conn.execute(
                f"DELETE FROM subagent_descriptors WHERE subagent_id IN ({marks})", ids
            )
            conn.execute(
                f"DELETE FROM subagent_edges WHERE child_id IN ({marks})", ids
            )
            return len(ids)

        return int(self._writer.execute(write))

    # ---- lifecycle ----

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - close 尽力而为
                log.debug("关闭 subagent store 连接失败", exc_info=True)


def _run_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["run_log"] = json.loads(item.get("run_log") or "[]")
    except (TypeError, ValueError):
        item["run_log"] = []
    return item


__all__ = [
    "DESCRIPTOR_VERSION",
    "SUBAGENT_DB_TABLES",
    "SUBAGENT_SCHEMA_FEATURE",
    "SUBAGENT_SCHEMA_VERSION",
    "SubagentStore",
    "validate_descriptor_payload",
]