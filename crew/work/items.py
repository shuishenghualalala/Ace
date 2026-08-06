"""Owner-scoped SQLite persistence for WorkItem and append-only activity."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.work.models import (
    BusinessStatus,
    Disposition,
    SourceReference,
    WorkItem,
)


class WorkItemConflictError(RuntimeError):
    """Raised when an optimistic update targets a stale WorkItem version."""


@dataclass(frozen=True, slots=True)
class WorkItemEvent:
    """One immutable, non-sensitive WorkItem state transition summary."""

    event_id: str
    owner_account_id: str
    item_id: str
    event_type: str
    actor: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    created_at: float


class WorkItemStore:
    """Persist Work items without coupling them to runtime task storage."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        wal_enabled: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_items (
                owner_account_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT,
                related_system TEXT,
                workspace_id TEXT,
                processing_session_id TEXT,
                business_status TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                priority TEXT NOT NULL,
                disposition TEXT NOT NULL,
                source_connector_key TEXT,
                source_external_id TEXT,
                source_external_version TEXT,
                due_at REAL,
                version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, item_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_owner_processing_session
                ON work_items(owner_account_id, processing_session_id)
                WHERE processing_session_id IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_items_owner_workspace
                ON work_items(owner_account_id, workspace_id, updated_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_items_owner_status
                ON work_items(owner_account_id, disposition, business_status, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS work_item_events (
                owner_account_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                before_state_json TEXT,
                after_state_json TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, event_id),
                FOREIGN KEY (owner_account_id, item_id)
                    REFERENCES work_items(owner_account_id, item_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_item_events_item
                ON work_item_events(owner_account_id, item_id, created_at, event_id)
            """,
        )
        for statement in statements:
            conn.execute(statement)
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(work_items)").fetchall()
        }
        if "related_system" not in columns:
            conn.execute("ALTER TABLE work_items ADD COLUMN related_system TEXT")
        if "category" not in columns:
            conn.execute("ALTER TABLE work_items ADD COLUMN category TEXT")

    def create(
        self,
        *,
        owner_account_id: str,
        title: str,
        actor: str = "user",
        **values: Any,
    ) -> WorkItem:
        """Create one owned item and its initial activity atomically."""
        item = WorkItem.create(
            owner_account_id=owner_account_id,
            title=title,
            **values,
        )

        def _write(conn: sqlite3.Connection) -> None:
            if self._select_item(conn, item.owner_account_id, item.item_id) is not None:
                raise ValueError(f"item_id already exists: {item.item_id}")
            self._assert_processing_session_available(conn, item)
            conn.execute(
                f"INSERT INTO work_items ({', '.join(_ITEM_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _ITEM_COLUMNS)})",
                _item_values(item),
            )
            self._append_event(
                conn,
                item,
                event_type="created",
                actor=actor,
                before=None,
                after=item,
            )

        try:
            self._writer.execute(_write)
        except sqlite3.IntegrityError as exc:
            raise ValueError("WorkItem uniqueness constraint failed") from exc
        return item

    def get(self, owner_account_id: str, item_id: str) -> WorkItem:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(item_id, "item_id")
        with self._lock:
            row = self._select_item(self._conn, owner, resource)
        if row is None:
            raise KeyError(f"WorkItem not found: {resource}")
        return _row_to_item(row)

    def list(
        self,
        owner_account_id: str,
        *,
        workspace_id: str | None = None,
        business_status: BusinessStatus | str | None = None,
        disposition: Disposition | str | None = None,
    ) -> list[WorkItem]:
        """List only one owner's items using explicit, parameterized filters."""
        owner = _required(owner_account_id, "owner_account_id")
        clauses = ["owner_account_id = ?"]
        params: list[Any] = [owner]
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(str(workspace_id).strip())
        if business_status is not None:
            clauses.append("business_status = ?")
            params.append(BusinessStatus(business_status).value)
        if disposition is not None:
            clauses.append("disposition = ?")
            params.append(Disposition(disposition).value)
        query = (
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM work_items "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at, item_id"
        )
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_item(row) for row in rows]

    def update(
        self,
        owner_account_id: str,
        item_id: str,
        *,
        expected_version: int,
        actor: str = "user",
        **changes: Any,
    ) -> WorkItem:
        """Atomically validate and persist one optimistic WorkItem patch."""
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(item_id, "item_id")

        def _write(conn: sqlite3.Connection) -> WorkItem:
            row = self._select_item(conn, owner, resource)
            if row is None:
                raise KeyError(f"WorkItem not found: {resource}")
            current = _row_to_item(row)
            if current.version != expected_version:
                raise WorkItemConflictError(
                    f"WorkItem version changed: expected {expected_version}, "
                    f"actual {current.version}"
                )
            updated = current.with_updates(**changes)
            if updated is current:
                return current
            self._assert_processing_session_available(conn, updated, exclude_item_id=resource)
            assignments = ", ".join(f"{column} = ?" for column in _MUTABLE_COLUMNS)
            cursor = conn.execute(
                f"UPDATE work_items SET {assignments} "
                "WHERE owner_account_id = ? AND item_id = ? AND version = ?",
                (
                    *_mutable_values(updated),
                    owner,
                    resource,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkItemConflictError("WorkItem changed during update")
            self._append_event(
                conn,
                updated,
                event_type="updated",
                actor=actor,
                before=current,
                after=updated,
            )
            return updated

        try:
            return self._writer.execute(_write)
        except sqlite3.IntegrityError as exc:
            raise ValueError("processing_session_id already belongs to another WorkItem") from exc

    def delete(
        self,
        owner_account_id: str,
        item_id: str,
        *,
        expected_version: int,
    ) -> None:
        """Delete only the owned Crew row and its activity."""
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(item_id, "item_id")

        def _write(conn: sqlite3.Connection) -> None:
            row = self._select_item(conn, owner, resource)
            if row is None:
                raise KeyError(f"WorkItem not found: {resource}")
            current = _row_to_item(row)
            if current.version != expected_version:
                raise WorkItemConflictError(
                    f"WorkItem version changed: expected {expected_version}, "
                    f"actual {current.version}"
                )
            conn.execute(
                "DELETE FROM work_items WHERE owner_account_id = ? AND item_id = ?",
                (owner, resource),
            )

        self._writer.execute(_write)

    def list_activity(self, owner_account_id: str, item_id: str) -> list[WorkItemEvent]:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(item_id, "item_id")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_id, owner_account_id, item_id, event_type, actor,
                       before_state_json, after_state_json, created_at
                FROM work_item_events
                WHERE owner_account_id = ? AND item_id = ?
                ORDER BY rowid
                """,
                (owner, resource),
            ).fetchall()
        return [
            WorkItemEvent(
                event_id=str(row["event_id"]),
                owner_account_id=str(row["owner_account_id"]),
                item_id=str(row["item_id"]),
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                before_state=_load_summary(row["before_state_json"]),
                after_state=_load_summary(row["after_state_json"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _select_item(
        conn: sqlite3.Connection,
        owner_account_id: str,
        item_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM work_items "
            "WHERE owner_account_id = ? AND item_id = ?",
            (owner_account_id, item_id),
        ).fetchone()

    @staticmethod
    def _assert_processing_session_available(
        conn: sqlite3.Connection,
        item: WorkItem,
        *,
        exclude_item_id: str | None = None,
    ) -> None:
        if item.processing_session_id is None:
            return
        query = (
            "SELECT item_id FROM work_items "
            "WHERE owner_account_id = ? AND processing_session_id = ?"
        )
        params: list[Any] = [item.owner_account_id, item.processing_session_id]
        if exclude_item_id is not None:
            query += " AND item_id != ?"
            params.append(exclude_item_id)
        if conn.execute(query, params).fetchone() is not None:
            raise ValueError(
                f"processing_session_id already belongs to another WorkItem: "
                f"{item.processing_session_id}"
            )

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        item: WorkItem,
        *,
        event_type: str,
        actor: str,
        before: WorkItem | None,
        after: WorkItem | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO work_item_events (
                owner_account_id, event_id, item_id, event_type, actor,
                before_state_json, after_state_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.owner_account_id,
                f"work_event_{uuid.uuid4().hex}",
                item.item_id,
                _required(event_type, "event_type"),
                _required(actor, "actor"),
                _dump_summary(before),
                _dump_summary(after),
                time.time(),
            ),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_ITEM_COLUMNS = (
    "owner_account_id",
    "item_id",
    "title",
    "description",
    "category",
    "related_system",
    "workspace_id",
    "processing_session_id",
    "business_status",
    "execution_status",
    "sync_status",
    "priority",
    "disposition",
    "source_connector_key",
    "source_external_id",
    "source_external_version",
    "due_at",
    "version",
    "created_at",
    "updated_at",
)
_MUTABLE_COLUMNS = _ITEM_COLUMNS[2:]


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _item_values(item: WorkItem) -> tuple[Any, ...]:
    source = item.source
    return (
        item.owner_account_id,
        item.item_id,
        item.title,
        item.description,
        item.category,
        item.related_system,
        item.workspace_id,
        item.processing_session_id,
        item.business_status.value,
        item.execution_status.value,
        item.sync_status.value,
        item.priority.value,
        item.disposition.value,
        source.connector_key if source else None,
        source.external_id if source else None,
        source.external_version if source else None,
        item.due_at,
        item.version,
        item.created_at,
        item.updated_at,
    )


def _mutable_values(item: WorkItem) -> tuple[Any, ...]:
    return _item_values(item)[2:]


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    source = None
    if row["source_connector_key"] is not None:
        source = SourceReference(
            connector_key=str(row["source_connector_key"]),
            external_id=str(row["source_external_id"]),
            external_version=str(row["source_external_version"] or ""),
        )
    return WorkItem(
        owner_account_id=str(row["owner_account_id"]),
        item_id=str(row["item_id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        category=row["category"],
        related_system=row["related_system"],
        workspace_id=row["workspace_id"],
        processing_session_id=row["processing_session_id"],
        business_status=str(row["business_status"]),
        execution_status=str(row["execution_status"]),
        sync_status=str(row["sync_status"]),
        priority=str(row["priority"]),
        disposition=str(row["disposition"]),
        source=source,
        due_at=float(row["due_at"]) if row["due_at"] is not None else None,
        version=int(row["version"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _state_summary(item: WorkItem) -> dict[str, Any]:
    return {
        "business_status": item.business_status.value,
        "disposition": item.disposition.value,
        "execution_status": item.execution_status.value,
        "sync_status": item.sync_status.value,
        "version": item.version,
    }


def _dump_summary(item: WorkItem | None) -> str | None:
    if item is None:
        return None
    return json.dumps(_state_summary(item), ensure_ascii=False, sort_keys=True)


def _load_summary(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return dict(json.loads(raw))
