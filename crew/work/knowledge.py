"""Work knowledge: personal sediment via Wiki, org aggregation and publish requests."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.wiki.schemas import WikiPage

OrganizationProvider = Callable[[], Sequence[dict[str, Any]]]


class WikiSaver(Protocol):
    """Minimum WikiStore surface for personal knowledge operations."""

    def save_page(self, page: Any, owner_account_id: str = "", kb_id: str = "default") -> Any: ...

    def list_all(
        self, owner_account_id: str = "", kb_id: str = "default",
        limit: int = 100, offset: int = 0, brief: bool = False,
    ) -> list[Any]: ...


class WorkKnowledgeStore:
    """Personal knowledge via Wiki, org provider, publish requests and index status."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        wiki_store: WikiSaver | None = None,
        organization_provider: OrganizationProvider | None = None,
        wal_enabled: bool = True,
    ) -> None:
        self._wiki = wiki_store
        self._org_provider = organization_provider
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_publish_requests (
                owner_account_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, request_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_workspace_index_status (
                owner_account_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'idle',
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, workspace_id)
            )
            """
        )

    # ------------------------------------------------------------------ #
    # Personal knowledge (delegated to Wiki store)
    # ------------------------------------------------------------------ #

    def save_personal(
        self,
        owner_account_id: str,
        *,
        title: str,
        content: str,
        source_item_id: str | None = None,
        page_id: str | None = None,
        summary: str | None = None,
    ) -> Any:
        """Save one personal knowledge page via the Wiki store."""
        if self._wiki is None:
            raise RuntimeError("Wiki store is unavailable")
        owner = _required(owner_account_id, "owner_account_id")
        page = WikiPage(
            id=page_id or f"wk_{uuid.uuid4().hex}",
            page_type="entity",
            title=title,
            content=content,
            file_path="",
            sources=[f"work-item:{source_item_id}"] if source_item_id else [],
            summary=summary,
        )
        if page_id:
            get_page = getattr(self._wiki, "get", None)
            if callable(get_page):
                existing = get_page(page.id, owner_account_id=owner)
                if existing is not None:
                    page.file_path = existing.file_path
                    page.created_at = existing.created_at
        return self._wiki.save_page(page, owner_account_id=owner)

    def list_personal(self, owner_account_id: str) -> list[Any]:
        """List personal knowledge pages from the Wiki store."""
        if self._wiki is None:
            raise RuntimeError("Wiki store is unavailable")
        return self._wiki.list_all(owner_account_id=owner_account_id)

    # ------------------------------------------------------------------ #
    # Organization knowledge (read-only provider)
    # ------------------------------------------------------------------ #

    @property
    def organization_available(self) -> bool:
        """Whether a real organization knowledge provider is configured."""
        return self._org_provider is not None

    def list_organization(self, owner_account_id: str) -> list[dict[str, Any]]:
        """Return read-only organization knowledge; empty when no provider."""
        _required(owner_account_id, "owner_account_id")
        if self._org_provider is None:
            return []
        return [dict(item) for item in self._org_provider()]

    # ------------------------------------------------------------------ #
    # Publish requests (personal -> organization)
    # ------------------------------------------------------------------ #

    def request_publish(
        self,
        owner_account_id: str,
        *,
        page_id: str,
        target: str,
    ) -> dict[str, Any]:
        owner = _required(owner_account_id, "owner_account_id")
        page = _required(page_id, "page_id")
        tgt = _required(target, "target")
        request_id = f"pub_{uuid.uuid4().hex}"
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_publish_requests
                    (owner_account_id, request_id, page_id, target, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (owner, request_id, page, tgt, now, now),
            )
        )
        return {
            "request_id": request_id,
            "owner_account_id": owner,
            "page_id": page,
            "target": tgt,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }

    def list_publish_requests(self, owner_account_id: str) -> list[dict[str, Any]]:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT owner_account_id, request_id, page_id, target, status, created_at, updated_at
                FROM work_publish_requests
                WHERE owner_account_id = ?
                ORDER BY created_at DESC, request_id
                """,
                (owner,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Workspace index status
    # ------------------------------------------------------------------ #

    def get_index_status(self, owner_account_id: str, workspace_id: str) -> dict[str, Any]:
        owner = _required(owner_account_id, "owner_account_id")
        ws = _required(workspace_id, "workspace_id")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT enabled, state, updated_at
                FROM work_workspace_index_status
                WHERE owner_account_id = ? AND workspace_id = ?
                """,
                (owner, ws),
            ).fetchone()
        if row is None:
            return {"enabled": False, "state": "idle", "updated_at": 0.0}
        return {
            "enabled": bool(row["enabled"]),
            "state": str(row["state"]),
            "updated_at": float(row["updated_at"]),
        }

    def set_index_status(
        self,
        owner_account_id: str,
        workspace_id: str,
        *,
        enabled: bool | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        owner = _required(owner_account_id, "owner_account_id")
        ws = _required(workspace_id, "workspace_id")
        current = self.get_index_status(owner, ws)
        new_enabled = bool(enabled) if enabled is not None else current["enabled"]
        new_state = state if state is not None else current["state"]
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_workspace_index_status
                    (owner_account_id, workspace_id, enabled, state, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, workspace_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (owner, ws, int(new_enabled), new_state, now),
            )
        )
        return self.get_index_status(owner, ws)

    def delete_index_status(self, owner_account_id: str, workspace_id: str) -> None:
        """Clear index status; does not delete source files."""
        owner = _required(owner_account_id, "owner_account_id")
        ws = _required(workspace_id, "workspace_id")
        self._writer.execute(
            lambda conn: conn.execute(
                "DELETE FROM work_workspace_index_status WHERE owner_account_id = ? AND workspace_id = ?",
                (owner, ws),
            )
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
