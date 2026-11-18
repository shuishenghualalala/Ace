"""SQLite 工作空间存储。

默认内置一个 "default" 工作空间（承载未分组/历史会话），不可删除。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from crew.core.interfaces import WorkspaceStore
from crew.state.models import Workspace
from crew.state._migration import backfill_empty_owner_rows, primary_key_columns, rebuild_table_pk
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

DEFAULT_ID = "default"

# 内置工作空间：get() 查不到时幂等自建（对齐 default 的既有行为）。
# wiki：Wiki Agent 会话的分组空间（crew/gateway/routers/wiki.py 创建会话时写死
# workspace_id="wiki"）；hidden=1 不进工作空间选择器，root_path 恒空——wiki 会话
# 不获得文件系统可信根目录，知识库读写走 wiki_lib 自己的路径。
BUILTIN_WORKSPACES: dict[str, tuple[str, int]] = {
    DEFAULT_ID: ("默认工作空间", 0),
    "wiki": ("Wiki 知识库", 1),
}


def _normalize_root_path(path: str) -> str:
    """规范化本地根目录为绝对路径；无效输入返回空串。"""
    raw = path or ""
    if not raw.strip():
        return ""
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return ""
    return str(resolved) if resolved.is_dir() else ""


class SQLiteWorkspaceStore(WorkspaceStore):
    def __init__(self, db_path: str = "crew_data/crew.db", *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        with self._lock:
            self._conn.close()

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                owner_account_id TEXT NOT NULL DEFAULT '',
                id          TEXT NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                instructions TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (owner_account_id, id)
            )
            """
        )
        self._migrate_owner_primary_key(conn)
        self._migrate_root_path_column(conn)
        self._migrate_hidden_column(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspaces_owner_root ON workspaces(owner_account_id, root_path)")
        # 历史 owner='' 行归属本机 local（owner 统一后不存在无主工作空间）。
        backfill_empty_owner_rows(conn, ["workspaces"])

    def _migrate_root_path_column(self, conn) -> None:
        info = conn.execute("PRAGMA table_info(workspaces)").fetchall()
        columns = {r[1] for r in info}
        if "root_path" not in columns:
            conn.execute("ALTER TABLE workspaces ADD COLUMN root_path TEXT NOT NULL DEFAULT ''")

    def _migrate_hidden_column(self, conn) -> None:
        info = conn.execute("PRAGMA table_info(workspaces)").fetchall()
        columns = {r[1] for r in info}
        if "hidden" not in columns:
            conn.execute("ALTER TABLE workspaces ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

    def _migrate_owner_primary_key(self, conn) -> None:
        """Migrate legacy global workspace table to owner-scoped primary key."""

        info = conn.execute("PRAGMA table_info(workspaces)").fetchall()
        columns = {r[1] for r in info}
        if columns >= {"owner_account_id", "id"} and primary_key_columns(conn, "workspaces") == ["owner_account_id", "id"]:
            return
        if "owner_account_id" not in columns:
            conn.execute("ALTER TABLE workspaces ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")
        rebuild_table_pk(
            conn,
            table="workspaces",
            expected_pk=["owner_account_id", "id"],
            new_ddl="""
                CREATE TABLE workspaces_new (
                    owner_account_id TEXT NOT NULL DEFAULT '',
                    id          TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    PRIMARY KEY (owner_account_id, id)
                )
            """,
            copy_sql="""
                INSERT OR IGNORE INTO workspaces_new (
                    owner_account_id, id, name, description, instructions, created_at, updated_at
                )
                SELECT owner_account_id, id, name, description, instructions, created_at, updated_at
                FROM workspaces
            """,
        )

    @staticmethod
    def _row(r) -> dict:
        return {
            "id": r[0], "name": r[1], "description": r[2],
            "instructions": r[3], "created_at": r[4], "updated_at": r[5],
            "root_path": r[6] if len(r) > 6 else "",
            "hidden": bool(r[7]) if len(r) > 7 else False,
        }

    def _ensure_builtin(self, workspace_id: str, owner_account_id: str) -> None:
        """内置工作空间（见 BUILTIN_WORKSPACES）不存在时幂等补建。"""
        name, hidden = BUILTIN_WORKSPACES[workspace_id]
        now = time.time()
        def _write(conn):
            conn.execute(
                "INSERT OR IGNORE INTO workspaces "
                "(owner_account_id, id, name, description, instructions, created_at, updated_at, hidden) "
                "VALUES (?, ?, ?, '', '', ?, ?, ?)",
                (owner_account_id, workspace_id, name, now, now, hidden),
            )
        self._writer.execute(_write)

    @staticmethod
    def _same_directory(stored: str, selected: os.stat_result) -> bool:
        try:
            return os.path.samestat(os.stat(stored), selected)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _exact_binding(conn, owner: str, root: str, excluded_id: str) -> str | None:
        row = conn.execute(
            "SELECT id FROM workspaces WHERE owner_account_id=? AND root_path=? AND id<>? ORDER BY created_at, id LIMIT 1",
            (owner, root, excluded_id),
        ).fetchone()
        return row[0] if row else None

    def _inspect_roots(self, owner: str, root: str, excluded_id: str = "") -> tuple[os.stat_result, dict[str, bool]]:
        if not root:
            raise ValueError("root_path 不是有效目录")
        try:
            selected = os.stat(root)
        except OSError as exc:
            raise ValueError("root_path 不是有效目录") from exc
        with self._lock:
            if self._exact_binding(self._conn, owner, root, excluded_id):
                return selected, {}
            rows = self._conn.execute(
                "SELECT DISTINCT root_path FROM workspaces WHERE owner_account_id=? AND root_path<>'' AND id<>?",
                (owner, excluded_id),
            ).fetchall()
        paths = {row[0] for row in rows}
        # A directory may have moved behind an old symlink. Probe the filesystem outside the write transaction.
        checks = {path: self._same_directory(path, selected) for path in paths}
        return selected, checks

    def _bound_root(self, conn, owner: str, root: str, inspected: tuple[os.stat_result, dict[str, bool]],
                    excluded_id: str = "") -> str | None:
        match = self._exact_binding(conn, owner, root, excluded_id)
        if match:
            return match
        selected, checks = inspected
        rows = conn.execute(
            "SELECT id, root_path FROM workspaces WHERE owner_account_id=? AND root_path<>'' AND id<>? ORDER BY created_at, id",
            (owner, excluded_id),
        )
        for workspace_id, stored in rows:
            # A concurrent writer can bind a directory after inspection and before BEGIN IMMEDIATE.
            if stored not in checks:
                checks[stored] = self._same_directory(stored, selected)
            if checks[stored]:
                return workspace_id
        return None

    def create(
        self,
        name: str,
        description: str = "",
        instructions: str = "",
        root_path: str = "",
        *,
        owner_account_id: str,
        reuse_root: bool = False,
    ) -> dict:
        normalized_root = _normalize_root_path(root_path)
        inspected = self._inspect_roots(owner_account_id, normalized_root) if reuse_root else None
        ws = Workspace(
            name=name,
            description=description,
            instructions=instructions,
            root_path=normalized_root,
            hidden=False,
        )
        def _write(conn):
            if inspected is not None:
                bound = self._bound_root(conn, owner_account_id, normalized_root, inspected)
                if bound:
                    return bound
            conn.execute(
                "INSERT INTO workspaces "
                "(owner_account_id, id, name, description, instructions, created_at, updated_at, root_path, hidden) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_account_id,
                    ws.id,
                    ws.name,
                    ws.description,
                    ws.instructions,
                    ws.created_at,
                    ws.updated_at,
                    ws.root_path,
                    1 if ws.hidden else 0,
                ),
            )
            return ws.id

        bound = self._writer.execute(_write)
        return ws.to_dict() if bound == ws.id else self.get(bound, owner_account_id)

    def get(self, workspace_id: str, owner_account_id: str) -> dict:
        if workspace_id in BUILTIN_WORKSPACES:
            self._ensure_builtin(workspace_id, owner_account_id)
        with self._lock:
            r = self._conn.execute(
                "SELECT id, name, description, instructions, created_at, updated_at, root_path, hidden "
                "FROM workspaces WHERE owner_account_id = ? AND id = ?",
                (owner_account_id, workspace_id),
            ).fetchone()
        if not r:
            raise KeyError(f"工作空间不存在: {workspace_id}")
        return self._row(r)

    def list(self, owner_account_id: str) -> list[dict]:
        self._ensure_builtin(DEFAULT_ID, owner_account_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, description, instructions, created_at, updated_at, root_path, hidden "
                "FROM workspaces WHERE owner_account_id = ? ORDER BY created_at ASC",
                (owner_account_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def update(self, workspace_id: str, owner_account_id: str, **fields) -> dict:
        allowed = {
            k: v for k, v in fields.items()
            if k in ("name", "description", "instructions", "root_path", "hidden") and v is not None
        }
        if "root_path" in allowed:
            allowed["root_path"] = _normalize_root_path(str(allowed["root_path"]))
        reuse_root = fields.get("reuse_root") is True
        inspected = self._inspect_roots(owner_account_id, allowed.get("root_path", ""), workspace_id) if reuse_root else None
        if "hidden" in allowed:
            allowed["hidden"] = 1 if bool(allowed["hidden"]) else 0
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            def _write(conn):
                if inspected is not None:
                    if not conn.execute("SELECT 1 FROM workspaces WHERE owner_account_id=? AND id=?",
                                        (owner_account_id, workspace_id)).fetchone():
                        raise KeyError(f"工作空间不存在: {workspace_id}")
                    bound = self._bound_root(conn, owner_account_id, allowed["root_path"], inspected, workspace_id)
                    if bound:
                        return bound
                conn.execute(
                    f"UPDATE workspaces SET {sets}, updated_at = ? WHERE owner_account_id = ? AND id = ?",
                    (*allowed.values(), time.time(), owner_account_id, workspace_id),
                )
                return workspace_id

            workspace_id = self._writer.execute(_write)
        return self.get(workspace_id, owner_account_id=owner_account_id)

    def delete(self, workspace_id: str, owner_account_id: str, *, writer=None) -> None:
        if workspace_id in BUILTIN_WORKSPACES:
            raise ValueError("内置工作空间不可删除")
        if writer is not None:
            writer.execute(
                "DELETE FROM workspaces WHERE owner_account_id = ? AND id = ?",
                (owner_account_id, workspace_id),
            )
            return
        def _write(conn):
            conn.execute(
                "DELETE FROM workspaces WHERE owner_account_id = ? AND id = ?",
                (owner_account_id, workspace_id),
            )
        self._writer.execute(_write)
