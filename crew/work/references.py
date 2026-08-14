"""Product-mode session ownership and owner-validated Work references."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from crew.core.types import Message
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.work.models import OwnerKey, ProductMode, WorkSessionLink


class SessionSnapshotSource(Protocol):
    """Minimum trusted SessionStore surface needed to create owned snapshots."""

    def session_belongs_to(self, session_id: str, owner_account_id: str) -> bool: ...

    def load(self, session_id: str, owner_account_id: str = "") -> list[Message]: ...


class ReferenceType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    WORK_ITEM = "work_item"
    WORK_SESSION = "work_session"
    AGENT_SESSION = "agent_session"
    PERSONAL_KNOWLEDGE = "personal_knowledge"
    ORGANIZATION_KNOWLEDGE = "organization_knowledge"
    SOURCE_RECORD = "source_record"


@dataclass(frozen=True, slots=True)
class WorkReference:
    """A context pointer; it never carries file or execution authority."""

    owner_account_id: str
    reference_id: str
    target_session_id: str
    reference_type: ReferenceType
    source_id: str
    target_item_id: str | None = None
    snapshot_version: str = ""
    snapshot_summary: str = ""
    source_link: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.reference_id)


class WorkReferenceStore:
    """Persist session ownership and context references without permission grants."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        session_store: SessionSnapshotSource,
        wal_enabled: bool = True,
    ) -> None:
        self._session_store = session_store
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_session_links (
                owner_account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                product_mode TEXT NOT NULL,
                work_item_id TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_id),
                FOREIGN KEY (owner_account_id, work_item_id)
                    REFERENCES work_items(owner_account_id, item_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_session_links_owner_item
                ON work_session_links(owner_account_id, work_item_id)
                WHERE work_item_id IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS work_references (
                owner_account_id TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                target_session_id TEXT NOT NULL,
                target_item_id TEXT,
                reference_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL DEFAULT '',
                snapshot_summary TEXT NOT NULL DEFAULT '',
                source_link TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, reference_id),
                FOREIGN KEY (owner_account_id, target_session_id)
                    REFERENCES work_session_links(owner_account_id, session_id) ON DELETE CASCADE,
                FOREIGN KEY (owner_account_id, target_item_id)
                    REFERENCES work_items(owner_account_id, item_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_references_target
                ON work_references(owner_account_id, target_session_id, created_at)
            """,
        )
        for statement in statements:
            conn.execute(statement)

    def link_session(
        self,
        *,
        owner_account_id: str,
        session_id: str,
        product_mode: ProductMode | str,
        work_item_id: str | None = None,
    ) -> WorkSessionLink:
        """Fix one owned Session to one product mode for its lifetime."""
        link = WorkSessionLink(
            owner_account_id=owner_account_id,
            session_id=session_id,
            product_mode=product_mode,
            work_item_id=work_item_id,
        )

        def _write(conn: sqlite3.Connection) -> WorkSessionLink:
            existing = self._select_session_link(conn, link.owner_account_id, link.session_id)
            if existing is not None:
                current = _row_to_session_link(existing)
                if current == link:
                    return current
                raise ValueError(
                    f"session already belongs to product mode: {current.product_mode.value}"
                )
            if link.work_item_id is not None:
                item_row = conn.execute(
                    """
                    SELECT processing_session_id
                    FROM work_items
                    WHERE owner_account_id = ? AND item_id = ?
                    """,
                    (link.owner_account_id, link.work_item_id),
                ).fetchone()
                if item_row is None:
                    raise KeyError(f"WorkItem not found: {link.work_item_id}")
                if str(item_row["processing_session_id"] or "") != link.session_id:
                    raise ValueError(
                        "work_item_id processing_session_id must be assigned "
                        "through WorkItemStore first"
                    )
                occupied = conn.execute(
                    """
                    SELECT session_id FROM work_session_links
                    WHERE owner_account_id = ? AND work_item_id = ?
                    """,
                    (link.owner_account_id, link.work_item_id),
                ).fetchone()
                if occupied is not None:
                    raise ValueError(
                        f"work_item_id already has a processing session: {link.work_item_id}"
                    )
            conn.execute(
                """
                INSERT INTO work_session_links (
                    owner_account_id, session_id, product_mode, work_item_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    link.owner_account_id,
                    link.session_id,
                    link.product_mode.value,
                    link.work_item_id,
                    time.time(),
                ),
            )
            return link

        try:
            return self._writer.execute(_write)
        except sqlite3.IntegrityError as exc:
            raise ValueError("session or work_item_id mapping is not unique") from exc

    def get_session_link(
        self,
        owner_account_id: str,
        session_id: str,
    ) -> WorkSessionLink:
        owner = _required(owner_account_id, "owner_account_id")
        session = _required(session_id, "session_id")
        with self._lock:
            row = self._select_session_link(self._conn, owner, session)
        if row is None:
            raise KeyError(f"Work session link not found: {session}")
        return _row_to_session_link(row)

    def list_session_links(
        self,
        owner_account_id: str,
        *,
        product_mode: ProductMode | str | None = None,
    ) -> list[WorkSessionLink]:
        """List one owner's product-mode mappings in creation order."""
        owner = _required(owner_account_id, "owner_account_id")
        clauses = ["owner_account_id = ?"]
        params: list[str] = [owner]
        if product_mode is not None:
            clauses.append("product_mode = ?")
            params.append(ProductMode(product_mode).value)
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner_account_id, session_id, product_mode, work_item_id "
                f"FROM work_session_links WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at, rowid",
                params,
            ).fetchall()
        return [_row_to_session_link(row) for row in rows]

    def create_reference(
        self,
        *,
        owner_account_id: str,
        target_session_id: str,
        reference_type: ReferenceType | str,
        source_id: str,
        target_item_id: str | None = None,
        source_link: str = "",
    ) -> WorkReference:
        """Create a non-Agent context pointer without reading its target."""
        ref_type = _reference_type(reference_type)
        if ref_type is ReferenceType.AGENT_SESSION:
            raise ValueError("agent_session references require the backend snapshot endpoint")
        return self._insert_reference(
            owner_account_id=owner_account_id,
            target_session_id=target_session_id,
            reference_type=ref_type,
            source_id=source_id,
            target_item_id=target_item_id,
            source_link=source_link,
        )

    def _insert_reference(
        self,
        *,
        owner_account_id: str,
        target_session_id: str,
        reference_type: ReferenceType,
        source_id: str,
        target_item_id: str | None = None,
        source_link: str = "",
        snapshot_version: str = "",
        snapshot_summary: str = "",
    ) -> WorkReference:
        """Insert one already-classified context pointer."""
        owner = _required(owner_account_id, "owner_account_id")
        target_session = _required(target_session_id, "target_session_id")
        source = _required(source_id, "source_id")
        target_link = self._require_work_target(owner, target_session)
        target_item = str(target_item_id or target_link.work_item_id or "").strip() or None
        if (
            target_link.work_item_id is not None
            and target_item is not None
            and target_link.work_item_id != target_item
        ):
            raise ValueError("target_item_id does not belong to target_session_id")
        now = time.time()
        reference = WorkReference(
            owner_account_id=owner,
            reference_id=f"work_ref_{uuid.uuid4().hex}",
            target_session_id=target_session,
            target_item_id=target_item,
            reference_type=reference_type,
            source_id=source,
            snapshot_version=str(snapshot_version or "").strip(),
            snapshot_summary=str(snapshot_summary or ""),
            source_link=str(source_link or "").strip(),
            created_at=now,
            updated_at=now,
        )

        def _write(conn: sqlite3.Connection) -> None:
            self._require_owned_source(
                conn,
                owner_account_id=owner,
                reference_type=reference_type,
                source_id=source,
            )
            if target_item is not None and conn.execute(
                """
                SELECT 1 FROM work_items
                WHERE owner_account_id = ? AND item_id = ?
                """,
                (owner, target_item),
            ).fetchone() is None:
                raise KeyError(f"WorkItem not found: {target_item}")
            conn.execute(
                """
                INSERT INTO work_references (
                    owner_account_id, reference_id, target_session_id, target_item_id,
                    reference_type, source_id, snapshot_version, snapshot_summary,
                    source_link, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _reference_values(reference),
            )

        self._writer.execute(_write)
        return reference

    @staticmethod
    def _require_owned_source(
        conn: sqlite3.Connection,
        *,
        owner_account_id: str,
        reference_type: ReferenceType,
        source_id: str,
    ) -> None:
        table_and_column = {
            ReferenceType.WORK_ITEM: ("work_items", "item_id"),
            ReferenceType.WORK_SESSION: ("work_session_links", "session_id"),
        }.get(reference_type)
        if table_and_column is None:
            return
        table, column = table_and_column
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE owner_account_id = ? AND {column} = ?",
            (owner_account_id, source_id),
        ).fetchone()
        if row is None:
            raise PermissionError("source entity is not owned by the current account")

    def create_agent_session_snapshot(
        self,
        *,
        owner_account_id: str,
        target_session_id: str,
        source_session_id: str,
        target_item_id: str | None = None,
    ) -> WorkReference:
        """Load an owned Agent session in the backend and persist its bounded snapshot."""
        owner = _required(owner_account_id, "owner_account_id")
        source = _required(source_session_id, "source_session_id")
        summary, version = self._owned_agent_snapshot(owner, source)
        return self._insert_reference(
            owner_account_id=owner,
            target_session_id=target_session_id,
            target_item_id=target_item_id,
            reference_type=ReferenceType.AGENT_SESSION,
            source_id=source,
            source_link=source,
            snapshot_version=version,
            snapshot_summary=summary,
        )

    def refresh_agent_session_snapshot(
        self,
        *,
        owner_account_id: str,
        reference_id: str,
    ) -> WorkReference:
        """Refresh one Agent reference from the current owned SessionStore value."""
        owner = _required(owner_account_id, "owner_account_id")
        reference = self.get_reference(owner, reference_id)
        if reference.reference_type is not ReferenceType.AGENT_SESSION:
            raise ValueError("only agent_session references can refresh snapshots")
        summary, version = self._owned_agent_snapshot(owner, reference.source_id)
        refreshed = WorkReference(
            owner_account_id=reference.owner_account_id,
            reference_id=reference.reference_id,
            target_session_id=reference.target_session_id,
            target_item_id=reference.target_item_id,
            reference_type=reference.reference_type,
            source_id=reference.source_id,
            snapshot_version=version,
            snapshot_summary=summary,
            source_link=reference.source_link,
            created_at=reference.created_at,
            updated_at=time.time(),
        )
        self._writer.execute(
            lambda conn: conn.execute(
                """
                UPDATE work_references
                SET snapshot_version = ?, snapshot_summary = ?, updated_at = ?
                WHERE owner_account_id = ? AND reference_id = ?
                """,
                (
                    refreshed.snapshot_version,
                    refreshed.snapshot_summary,
                    refreshed.updated_at,
                    owner,
                    refreshed.reference_id,
                ),
            )
        )
        return refreshed

    def get_reference(
        self,
        owner_account_id: str,
        reference_id: str,
    ) -> WorkReference:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(reference_id, "reference_id")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT owner_account_id, reference_id, target_session_id, target_item_id,
                       reference_type, source_id, snapshot_version, snapshot_summary,
                       source_link, created_at, updated_at
                FROM work_references
                WHERE owner_account_id = ? AND reference_id = ?
                """,
                (owner, resource),
            ).fetchone()
        if row is None:
            raise KeyError(f"Work reference not found: {resource}")
        return _row_to_reference(row)


    def delete_reference(self, owner_account_id: str, reference_id: str) -> None:
        """Remove one owned reference; never touches the referenced entity."""
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(reference_id, "reference_id")

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "DELETE FROM work_references WHERE owner_account_id = ? AND reference_id = ?",
                (owner, resource),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Work reference not found: {resource}")

        self._writer.execute(_write)

    def list_references(
        self,
        owner_account_id: str,
        target_session_id: str,
    ) -> list[WorkReference]:
        owner = _required(owner_account_id, "owner_account_id")
        session = _required(target_session_id, "target_session_id")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT owner_account_id, reference_id, target_session_id, target_item_id,
                       reference_type, source_id, snapshot_version, snapshot_summary,
                       source_link, created_at, updated_at
                FROM work_references
                WHERE owner_account_id = ? AND target_session_id = ?
                ORDER BY created_at, reference_id
                """,
                (owner, session),
            ).fetchall()
        return [_row_to_reference(row) for row in rows]

    def _owned_agent_snapshot(self, owner_account_id: str, session_id: str) -> tuple[str, str]:
        if not self._session_store.session_belongs_to(session_id, owner_account_id):
            raise PermissionError("source session is not owned by the current account")
        with self._lock:
            source_link = self._select_session_link(self._conn, owner_account_id, session_id)
        if (
            source_link is not None
            and _row_to_session_link(source_link).product_mode is ProductMode.WORK
        ):
            raise ValueError("source session is a Work session, not an Agent session")
        messages = self._session_store.load(session_id, owner_account_id=owner_account_id)
        return _snapshot(messages)

    def _require_work_target(
        self,
        owner_account_id: str,
        session_id: str,
    ) -> WorkSessionLink:
        try:
            link = self.get_session_link(owner_account_id, session_id)
        except KeyError as exc:
            raise PermissionError("target Work session is not owned by the current account") from exc
        if link.product_mode is not ProductMode.WORK:
            raise ValueError("target_session_id must belong to work mode")
        return link

    @staticmethod
    def _select_session_link(
        conn: sqlite3.Connection,
        owner_account_id: str,
        session_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT owner_account_id, session_id, product_mode, work_item_id
            FROM work_session_links
            WHERE owner_account_id = ? AND session_id = ?
            """,
            (owner_account_id, session_id),
        ).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _required(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _reference_type(value: ReferenceType | str) -> ReferenceType:
    try:
        return ReferenceType(value)
    except ValueError as exc:
        raise ValueError(f"invalid reference_type: {value!r}") from exc


def _row_to_session_link(row: sqlite3.Row) -> WorkSessionLink:
    return WorkSessionLink(
        owner_account_id=str(row["owner_account_id"]),
        session_id=str(row["session_id"]),
        product_mode=str(row["product_mode"]),
        work_item_id=row["work_item_id"],
    )


def _reference_values(reference: WorkReference) -> tuple[object, ...]:
    return (
        reference.owner_account_id,
        reference.reference_id,
        reference.target_session_id,
        reference.target_item_id,
        reference.reference_type.value,
        reference.source_id,
        reference.snapshot_version,
        reference.snapshot_summary,
        reference.source_link,
        reference.created_at,
        reference.updated_at,
    )


def _row_to_reference(row: sqlite3.Row) -> WorkReference:
    return WorkReference(
        owner_account_id=str(row["owner_account_id"]),
        reference_id=str(row["reference_id"]),
        target_session_id=str(row["target_session_id"]),
        target_item_id=row["target_item_id"],
        reference_type=ReferenceType(str(row["reference_type"])),
        source_id=str(row["source_id"]),
        snapshot_version=str(row["snapshot_version"]),
        snapshot_summary=str(row["snapshot_summary"]),
        source_link=str(row["source_link"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _snapshot(messages: list[Message]) -> tuple[str, str]:
    visible = [
        {
            "role": message.role,
            "content": message.text_content[:2000],
            "timestamp": message.timestamp,
        }
        for message in messages
        if message.role in {"user", "assistant"} and not message.is_meta
    ][-12:]
    payload = json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    summary = "\n\n".join(
        f"{entry['role']}: {entry['content']}" for entry in visible if entry["content"]
    )[:12000]
    return summary, hashlib.sha256(payload.encode("utf-8")).hexdigest()
