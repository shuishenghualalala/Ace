"""本地站点、发布版本与页面注释的 SQLite 存储。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


class SQLiteSiteStore:
    def __init__(self, db_path: str, *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._wal_enabled = wal_enabled
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        with self._lock:
            self._conn.close()

    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def wal_enabled(self) -> bool:
        return self._wal_enabled

    @staticmethod
    def _init_schema(conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                owner_account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL,
                build_command TEXT NOT NULL DEFAULT '',
                output_directory TEXT NOT NULL DEFAULT '',
                active_release_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_sites_owner_workspace
                ON sites(owner_account_id, workspace_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS site_releases (
                owner_account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                status TEXT NOT NULL,
                release_path TEXT NOT NULL DEFAULT '',
                manifest TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_releases_site
                ON site_releases(owner_account_id, site_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS site_annotations (
                owner_account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                release_id TEXT NOT NULL,
                route TEXT NOT NULL DEFAULT '/',
                selector TEXT NOT NULL DEFAULT '',
                element_tag TEXT NOT NULL DEFAULT '',
                element_text TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_annotations_site
                ON site_annotations(owner_account_id, site_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS inspiration_annotations (
                owner_account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                inspiration_id TEXT NOT NULL,
                inspiration_kind TEXT NOT NULL,
                revision_id TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '/',
                selector TEXT NOT NULL DEFAULT '',
                element_tag TEXT NOT NULL DEFAULT '',
                element_text TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_inspiration_annotations_item
                ON inspiration_annotations(owner_account_id, inspiration_id, created_at DESC);
            INSERT OR IGNORE INTO inspiration_annotations(
                owner_account_id,id,inspiration_id,inspiration_kind,revision_id,route,
                selector,element_tag,element_text,comment,context,status,created_at,updated_at
            ) SELECT owner_account_id,id,site_id,'site',release_id,route,selector,
                element_tag,element_text,comment,context,status,created_at,updated_at
              FROM site_annotations;
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sites)").fetchall()}
        if "description" not in columns:
            conn.execute("ALTER TABLE sites ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _site_row(row) -> dict[str, Any]:
        return {
            "id": row[0], "workspace_id": row[1], "session_id": row[2],
            "name": row[3], "description": row[4], "source_path": row[5],
            "build_command": row[6], "output_directory": row[7],
            "active_release_id": row[8], "created_at": row[9], "updated_at": row[10],
        }

    def upsert_site(
        self, *, owner: str, workspace_id: str, session_id: str, name: str,
        source_path: str, build_command: str, output_directory: str, description: str = "",
        site_id: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        sid = site_id.strip() or f"site_{uuid.uuid4().hex[:12]}"

        def _write(conn):
            existing = conn.execute(
                "SELECT 1 FROM sites WHERE owner_account_id=? AND id=?", (owner, sid)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE sites SET workspace_id=?, session_id=?, name=?, description=?, source_path=?, "
                    "build_command=?, output_directory=?, updated_at=? "
                    "WHERE owner_account_id=? AND id=?",
                    (workspace_id, session_id, name, description, source_path, build_command,
                     output_directory, now, owner, sid),
                )
            else:
                conn.execute(
                    "INSERT INTO sites(owner_account_id,id,workspace_id,session_id,name,description,source_path,"
                    "build_command,output_directory,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, sid, workspace_id, session_id, name, description, source_path,
                     build_command, output_directory, now, now),
                )
        self._writer.execute(_write)
        return self.get_site(owner, sid)

    def get_site(self, owner: str, site_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,workspace_id,session_id,name,description,source_path,build_command,output_directory,"
                "active_release_id,created_at,updated_at FROM sites "
                "WHERE owner_account_id=? AND id=?", (owner, site_id),
            ).fetchone()
        if not row:
            raise KeyError("站点不存在")
        return self._site_row(row)

    def list_sites(self, owner: str, workspace_id: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT id,workspace_id,session_id,name,description,source_path,build_command,output_directory,"
               "active_release_id,created_at,updated_at FROM sites WHERE owner_account_id=?")
        params: list[Any] = [owner]
        if workspace_id:
            sql += " AND workspace_id=?"
            params.append(workspace_id)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._site_row(row) for row in rows]

    def delete_site(self, owner: str, site_id: str) -> None:
        def _write(conn):
            conn.execute("DELETE FROM site_annotations WHERE owner_account_id=? AND site_id=?", (owner, site_id))
            conn.execute("DELETE FROM site_releases WHERE owner_account_id=? AND site_id=?", (owner, site_id))
            conn.execute("DELETE FROM sites WHERE owner_account_id=? AND id=?", (owner, site_id))
        self._writer.execute(_write)

    def create_release(self, owner: str, site_id: str) -> dict[str, Any]:
        rid = f"rel_{uuid.uuid4().hex[:12]}"
        now = time.time()
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_releases(owner_account_id,id,site_id,status,created_at) VALUES(?,?,?,?,?)",
            (owner, rid, site_id, "building", now),
        ))
        return {"id": rid, "site_id": site_id, "status": "building", "created_at": now}

    def finish_release(
        self, owner: str, site_id: str, release_id: str, *, status: str,
        release_path: str = "", manifest: dict[str, Any] | None = None, error: str = "",
    ) -> None:
        payload = json.dumps(manifest or {}, ensure_ascii=False)
        now = time.time()
        def _write(conn):
            conn.execute(
                "UPDATE site_releases SET status=?,release_path=?,manifest=?,error=? "
                "WHERE owner_account_id=? AND id=? AND site_id=?",
                (status, release_path, payload, error, owner, release_id, site_id),
            )
            if status == "ready":
                conn.execute(
                    "UPDATE sites SET active_release_id=?,updated_at=? WHERE owner_account_id=? AND id=?",
                    (release_id, now, owner, site_id),
                )
        self._writer.execute(_write)

    def get_release(self, owner: str, release_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,site_id,status,release_path,manifest,error,created_at FROM site_releases "
                "WHERE owner_account_id=? AND id=?", (owner, release_id),
            ).fetchone()
        if not row:
            raise KeyError("站点版本不存在")
        try:
            manifest = json.loads(row[4] or "{}")
        except json.JSONDecodeError:
            manifest = {}
        return {"id": row[0], "site_id": row[1], "status": row[2],
                "release_path": row[3], "manifest": manifest, "error": row[5],
                "created_at": row[6]}

    def list_releases(self, owner: str, site_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM site_releases WHERE owner_account_id=? AND site_id=? ORDER BY created_at DESC",
                (owner, site_id),
            ).fetchall()
        return [self.get_release(owner, row[0]) for row in rows]

    def create_annotation(self, owner: str, site_id: str, release_id: str, data: dict[str, Any]) -> dict[str, Any]:
        aid = f"ann_{uuid.uuid4().hex[:12]}"
        now = time.time()
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        values = (owner, aid, site_id, release_id, str(data.get("route") or "/"),
                  str(data.get("selector") or ""), str(data.get("element_tag") or ""),
                  str(data.get("element_text") or "")[:2000], str(data.get("comment") or "").strip(),
                  json.dumps(context, ensure_ascii=False), "open", now, now)
        if not values[8]:
            raise ValueError("注释内容不能为空")
        def _write(conn):
            conn.execute(
                "INSERT INTO site_annotations(owner_account_id,id,site_id,release_id,route,selector,"
                "element_tag,element_text,comment,context,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
            )
            conn.execute(
                "INSERT OR REPLACE INTO inspiration_annotations(owner_account_id,id,inspiration_id,"
                "inspiration_kind,revision_id,route,selector,element_tag,element_text,comment,context,"
                "status,created_at,updated_at) VALUES(?,?,?,'site',?,?,?,?,?,?,?,?,?,?)",
                (owner, aid, site_id, release_id, values[4], values[5], values[6], values[7],
                 values[8], values[9], values[10], values[11], values[12]),
            )
        self._writer.execute(_write)
        return self.get_annotation(owner, aid)

    def get_annotation(self, owner: str, annotation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,site_id,release_id,route,selector,element_tag,element_text,comment,context,status,"
                "created_at,updated_at FROM site_annotations WHERE owner_account_id=? AND id=?",
                (owner, annotation_id),
            ).fetchone()
        if not row:
            raise KeyError("注释不存在")
        try:
            context = json.loads(row[8] or "{}")
        except json.JSONDecodeError:
            context = {}
        return {"id": row[0], "site_id": row[1], "release_id": row[2], "route": row[3],
                "selector": row[4], "element_tag": row[5], "element_text": row[6],
                "comment": row[7], "context": context, "status": row[9],
                "created_at": row[10], "updated_at": row[11]}

    def list_annotations(self, owner: str, site_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM site_annotations WHERE owner_account_id=? AND site_id=? ORDER BY created_at DESC",
                (owner, site_id),
            ).fetchall()
        return [self.get_annotation(owner, row[0]) for row in rows]

    def update_annotation_status(self, owner: str, annotation_id: str, status: str) -> dict[str, Any]:
        if status not in {"open", "resolved", "rejected"}:
            raise ValueError("无效的注释状态")
        now = time.time()
        def _write(conn):
            conn.execute(
                "UPDATE site_annotations SET status=?,updated_at=? WHERE owner_account_id=? AND id=?",
                (status, now, owner, annotation_id),
            )
            conn.execute(
                "UPDATE inspiration_annotations SET status=?,updated_at=? WHERE owner_account_id=? AND id=?",
                (status, now, owner, annotation_id),
            )
        self._writer.execute(_write)
        return self.get_annotation(owner, annotation_id)

    @staticmethod
    def _inspiration_annotation_row(row) -> dict[str, Any]:
        try:
            context = json.loads(row[9] or "{}")
        except json.JSONDecodeError:
            context = {}
        inspiration_kind = row[2]
        target_kind = str(context.get("targetKind") or (
            "site_dom" if inspiration_kind == "site" else
            "widget_dom" if context.get("widgetId") else inspiration_kind
        ))
        return {
            "id": row[0], "inspirationId": row[1], "inspirationKind": row[2],
            "targetKind": target_kind,
            "canvasId": str(context.get("canvasId") or ""),
            "widgetId": str(context.get("widgetId") or ""),
            "mountId": str(context.get("mountId") or ""),
            "revisionId": row[3], "route": row[4], "selector": row[5],
            "elementTag": row[6], "elementText": row[7], "comment": row[8],
            "context": context, "status": row[10], "createdAt": row[11],
            "updatedAt": row[12],
        }

    def create_inspiration_annotation(
        self, owner: str, inspiration_id: str, inspiration_kind: str,
        revision_id: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        if inspiration_kind not in {"site", "canvas", "widget"}:
            raise ValueError("无效的灵感类型")
        comment = str(data.get("comment") or "").strip()
        if not comment:
            raise ValueError("注释内容不能为空")
        annotation_id = f"iann_{uuid.uuid4().hex[:12]}"
        now = time.time()
        context = dict(data.get("context")) if isinstance(data.get("context"), dict) else {}
        for key in ("targetKind", "canvasId", "widgetId", "mountId"):
            if data.get(key):
                context[key] = str(data[key])
        context.setdefault(
            "targetKind",
            "site_dom" if inspiration_kind == "site" else
            "widget_dom" if context.get("widgetId") else inspiration_kind,
        )
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO inspiration_annotations(owner_account_id,id,inspiration_id,inspiration_kind,"
            "revision_id,route,selector,element_tag,element_text,comment,context,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, annotation_id, inspiration_id, inspiration_kind, revision_id,
             str(data.get("route") or "/"), str(data.get("selector") or ""),
             str(data.get("element_tag") or data.get("elementTag") or ""),
             str(data.get("element_text") or data.get("elementText") or "")[:2000],
             comment, json.dumps(context, ensure_ascii=False), "open", now, now),
        ))
        return self.get_inspiration_annotation(owner, annotation_id)

    def get_inspiration_annotation(self, owner: str, annotation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,inspiration_id,inspiration_kind,revision_id,route,selector,element_tag,"
                "element_text,comment,context,status,created_at,updated_at "
                "FROM inspiration_annotations WHERE owner_account_id=? AND id=?",
                (owner, annotation_id),
            ).fetchone()
        if not row:
            raise KeyError("灵感注释不存在")
        return self._inspiration_annotation_row(row)

    def list_inspiration_annotations(self, owner: str, inspiration_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM inspiration_annotations WHERE owner_account_id=? AND inspiration_id=? "
                "ORDER BY created_at DESC", (owner, inspiration_id),
            ).fetchall()
        return [self.get_inspiration_annotation(owner, row[0]) for row in rows]

    def update_inspiration_annotation_status(
        self, owner: str, annotation_id: str, status: str,
    ) -> dict[str, Any]:
        if status not in {"open", "resolved", "rejected"}:
            raise ValueError("无效的注释状态")
        self.get_inspiration_annotation(owner, annotation_id)
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE inspiration_annotations SET status=?,updated_at=? "
            "WHERE owner_account_id=? AND id=?",
            (status, time.time(), owner, annotation_id),
        ))
        return self.get_inspiration_annotation(owner, annotation_id)

    def delete_inspiration_annotations(self, owner: str, inspiration_id: str) -> None:
        self._writer.execute(lambda conn: conn.execute(
            "DELETE FROM inspiration_annotations WHERE owner_account_id=? AND inspiration_id=?",
            (owner, inspiration_id),
        ))
