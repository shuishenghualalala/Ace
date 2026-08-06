"""Approved Work source adapters and owner-scoped incremental sync state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.work.models import OwnerKey, SyncStatus


class SourceConnectionStatus(str, Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    SYNCING = "syncing"
    READY = "ready"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SourceRecordInput:
    external_id: str
    external_version: str
    title: str
    kind: str
    source_status: str
    due_at: float | None = None
    source_url: str = ""
    normalized: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SourceSyncBatch:
    records: tuple[SourceRecordInput, ...]
    next_cursor: str | None


class WorkSourceAdapter(Protocol):
    """Organization-provided adapter; credentials stay inside its implementation."""

    def fetch(self, cursor: str | None) -> SourceSyncBatch: ...


@dataclass(frozen=True, slots=True)
class WorkSourceState:
    owner_account_id: str
    connector_key: str
    enabled: bool
    status: SourceConnectionStatus
    cursor: str | None
    last_error: str
    last_synced_at: float | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class WorkSourceRecord:
    owner_account_id: str
    record_id: str
    connector_key: str
    external_id: str
    external_version: str
    title: str
    kind: str
    source_status: str
    due_at: float | None
    source_url: str
    normalized: dict[str, Any]
    pending_writeback: dict[str, Any]
    conflict_external: dict[str, Any]
    conflict_local: dict[str, Any]
    sync_status: SyncStatus
    updated_at: float

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.record_id)


class WorkSourceStore:
    """Persist source state while keeping provider clients and credentials outside."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        approved_source_keys: set[str],
        adapters: Mapping[str, WorkSourceAdapter],
        wal_enabled: bool = True,
    ) -> None:
        self._approved = {_required(key, "connector_key") for key in approved_source_keys}
        unknown_adapters = set(adapters) - self._approved
        if unknown_adapters:
            raise ValueError(f"adapters are not organization-approved: {sorted(unknown_adapters)}")
        self._adapters = dict(adapters)
        self._lock = threading.RLock()
        self._refreshing: set[tuple[str, str]] = set()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_sources (
                owner_account_id TEXT NOT NULL,
                connector_key TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                status TEXT NOT NULL,
                cursor TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                last_synced_at REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, connector_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_source_records (
                owner_account_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                connector_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                external_version TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_status TEXT NOT NULL,
                due_at REAL,
                source_url TEXT NOT NULL DEFAULT '',
                normalized_json TEXT NOT NULL DEFAULT '{}',
                pending_writeback_json TEXT NOT NULL DEFAULT '{}',
                conflict_external_json TEXT NOT NULL DEFAULT '{}',
                conflict_local_json TEXT NOT NULL DEFAULT '{}',
                sync_status TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, record_id),
                UNIQUE (owner_account_id, connector_key, external_id),
                FOREIGN KEY (owner_account_id, connector_key)
                    REFERENCES work_sources(owner_account_id, connector_key) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_source_records_owner_source
                ON work_source_records(owner_account_id, connector_key, updated_at DESC)
            """,
        )
        for statement in statements:
            conn.execute(statement)

    def set_enabled(
        self,
        owner_account_id: str,
        connector_key: str,
        enabled: bool,
    ) -> WorkSourceState:
        owner = _required(owner_account_id, "owner_account_id")
        key = self._approved_key(connector_key)
        status = (
            SourceConnectionStatus.DISABLED
            if not enabled
            else (
                SourceConnectionStatus.IDLE
                if key in self._adapters
                else SourceConnectionStatus.UNAVAILABLE
            )
        )
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_sources (
                    owner_account_id, connector_key, enabled, status, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, connector_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    status = excluded.status,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (owner, key, int(bool(enabled)), status.value, now),
            )
        )
        return self.get_state(owner, key)

    def get_state(self, owner_account_id: str, connector_key: str) -> WorkSourceState:
        owner = _required(owner_account_id, "owner_account_id")
        key = self._approved_key(connector_key)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT owner_account_id, connector_key, enabled, status, cursor,
                       last_error, last_synced_at, updated_at
                FROM work_sources
                WHERE owner_account_id = ? AND connector_key = ?
                """,
                (owner, key),
            ).fetchone()
        if row is None:
            return WorkSourceState(
                owner_account_id=owner,
                connector_key=key,
                enabled=False,
                status=SourceConnectionStatus.DISABLED,
                cursor=None,
                last_error="",
                last_synced_at=None,
                updated_at=0.0,
            )
        state = _row_to_state(row)
        if state.enabled and key not in self._adapters:
            return WorkSourceState(
                owner_account_id=state.owner_account_id,
                connector_key=state.connector_key,
                enabled=True,
                status=SourceConnectionStatus.UNAVAILABLE,
                cursor=state.cursor,
                last_error=state.last_error,
                last_synced_at=state.last_synced_at,
                updated_at=state.updated_at,
            )
        return state

    def list_states(self, owner_account_id: str) -> list[WorkSourceState]:
        return [
            self.get_state(owner_account_id, key)
            for key in sorted(self._approved)
        ]

    def refresh(self, owner_account_id: str, connector_key: str) -> WorkSourceState:
        """Run one incremental adapter pull and atomically advance its cursor."""
        owner = _required(owner_account_id, "owner_account_id")
        key = self._approved_key(connector_key)
        state = self.get_state(owner, key)
        if not state.enabled:
            raise ValueError(f"source is disabled: {key}")
        adapter = self._adapters.get(key)
        if adapter is None:
            return self._set_status(owner, key, SourceConnectionStatus.UNAVAILABLE)
        refresh_key = (owner, key)
        with self._lock:
            if refresh_key in self._refreshing:
                raise RuntimeError(f"source refresh already running: {key}")
            self._refreshing.add(refresh_key)
        try:
            self._set_status(owner, key, SourceConnectionStatus.SYNCING)
            batch = adapter.fetch(state.cursor)
            return self._persist_batch(owner, key, batch)
        except Exception as exc:
            self._set_status(
                owner,
                key,
                SourceConnectionStatus.ERROR,
                last_error=type(exc).__name__,
            )
            raise
        finally:
            with self._lock:
                self._refreshing.discard(refresh_key)

    def _persist_batch(
        self,
        owner_account_id: str,
        connector_key: str,
        batch: SourceSyncBatch,
    ) -> WorkSourceState:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            for raw in batch.records:
                record = _normalize_record(owner_account_id, connector_key, raw, now)
                existing = conn.execute(
                    """
                    SELECT pending_writeback_json, conflict_external_json,
                           conflict_local_json, sync_status
                    FROM work_source_records
                    WHERE owner_account_id = ? AND record_id = ?
                    """,
                    (owner_account_id, record.record_id),
                ).fetchone()
                preserve_local = (
                    existing is not None
                    and SyncStatus(str(existing["sync_status"]))
                    in {SyncStatus.PENDING_WRITEBACK, SyncStatus.CONFLICT}
                )
                conn.execute(
                    """
                    INSERT INTO work_source_records (
                        owner_account_id, record_id, connector_key, external_id,
                        external_version, title, kind, source_status, due_at, source_url,
                        normalized_json, pending_writeback_json, conflict_external_json,
                        conflict_local_json, sync_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_account_id, record_id) DO UPDATE SET
                        external_version = excluded.external_version,
                        title = excluded.title,
                        kind = excluded.kind,
                        source_status = excluded.source_status,
                        due_at = excluded.due_at,
                        source_url = excluded.source_url,
                        normalized_json = excluded.normalized_json,
                        pending_writeback_json = excluded.pending_writeback_json,
                        conflict_external_json = excluded.conflict_external_json,
                        conflict_local_json = excluded.conflict_local_json,
                        sync_status = excluded.sync_status,
                        updated_at = excluded.updated_at
                    """,
                    _record_values(record, existing if preserve_local else None),
                )
            conn.execute(
                """
                UPDATE work_sources SET
                    status = ?, cursor = ?, last_error = '',
                    last_synced_at = ?, updated_at = ?
                WHERE owner_account_id = ? AND connector_key = ?
                """,
                (
                    SourceConnectionStatus.READY.value,
                    batch.next_cursor,
                    now,
                    now,
                    owner_account_id,
                    connector_key,
                ),
            )

        self._writer.execute(_write)
        return self.get_state(owner_account_id, connector_key)

    def list_records(
        self,
        owner_account_id: str,
        *,
        connector_key: str | None = None,
    ) -> list[WorkSourceRecord]:
        owner = _required(owner_account_id, "owner_account_id")
        clauses = ["owner_account_id = ?"]
        params: list[Any] = [owner]
        if connector_key is not None:
            clauses.append("connector_key = ?")
            params.append(self._approved_key(connector_key))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_RECORD_COLUMNS)} FROM work_source_records "
                f"WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, record_id",
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_record(self, owner_account_id: str, record_id: str) -> WorkSourceRecord:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(record_id, "record_id")
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_RECORD_COLUMNS)} FROM work_source_records "
                "WHERE owner_account_id = ? AND record_id = ?",
                (owner, resource),
            ).fetchone()
        if row is None:
            raise KeyError(f"Work source record not found: {resource}")
        return _row_to_record(row)

    def delete_local_data(self, owner_account_id: str, connector_key: str) -> int:
        """Delete one connector's local records without touching external data or WorkItems."""
        owner = _required(owner_account_id, "owner_account_id")
        connector = self._approved_key(connector_key)

        def _write(conn: sqlite3.Connection) -> int:
            deleted = conn.execute(
                "DELETE FROM work_source_records "
                "WHERE owner_account_id = ? AND connector_key = ?",
                (owner, connector),
            ).rowcount
            conn.execute(
                """
                UPDATE work_sources SET
                    cursor = NULL,
                    last_synced_at = NULL,
                    last_error = '',
                    status = CASE WHEN enabled = 1 THEN 'idle' ELSE 'disabled' END,
                    updated_at = ?
                WHERE owner_account_id = ? AND connector_key = ?
                """,
                (time.time(), owner, connector),
            )
            return deleted

        return int(self._writer.execute(_write))

    def queue_writeback(
        self,
        owner_account_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> WorkSourceRecord:
        payload = _json_object(fields, "fields")
        return self._update_record_control(
            owner_account_id,
            record_id,
            pending=payload,
            conflict_external={},
            conflict_local={},
            status=SyncStatus.PENDING_WRITEBACK,
        )

    def mark_conflict(
        self,
        owner_account_id: str,
        record_id: str,
        *,
        external_value: Mapping[str, Any],
        local_value: Mapping[str, Any],
    ) -> WorkSourceRecord:
        external = _json_object(external_value, "external_value")
        local = _json_object(local_value, "local_value")
        return self._update_record_control(
            owner_account_id,
            record_id,
            pending=local,
            conflict_external=external,
            conflict_local=local,
            status=SyncStatus.CONFLICT,
        )

    def resolve_conflict(
        self,
        owner_account_id: str,
        record_id: str,
        *,
        resolution: str,
    ) -> WorkSourceRecord:
        current = self.get_record(owner_account_id, record_id)
        if current.sync_status is not SyncStatus.CONFLICT:
            raise ValueError("record has no conflict")
        if resolution == "external":
            pending: dict[str, Any] = {}
            status = SyncStatus.SYNCED
        elif resolution == "local":
            pending = current.conflict_local
            status = SyncStatus.PENDING_WRITEBACK
        else:
            raise ValueError("resolution must be external or local")
        return self._update_record_control(
            owner_account_id,
            record_id,
            pending=pending,
            conflict_external={},
            conflict_local={},
            status=status,
        )

    def _update_record_control(
        self,
        owner_account_id: str,
        record_id: str,
        *,
        pending: Mapping[str, Any],
        conflict_external: Mapping[str, Any],
        conflict_local: Mapping[str, Any],
        status: SyncStatus,
    ) -> WorkSourceRecord:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(record_id, "record_id")

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE work_source_records SET
                    pending_writeback_json = ?,
                    conflict_external_json = ?,
                    conflict_local_json = ?,
                    sync_status = ?,
                    updated_at = ?
                WHERE owner_account_id = ? AND record_id = ?
                """,
                (
                    _dump(pending),
                    _dump(conflict_external),
                    _dump(conflict_local),
                    status.value,
                    time.time(),
                    owner,
                    resource,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Work source record not found: {resource}")

        self._writer.execute(_write)
        return self.get_record(owner, resource)

    def _set_status(
        self,
        owner_account_id: str,
        connector_key: str,
        status: SourceConnectionStatus,
        *,
        last_error: str = "",
    ) -> WorkSourceState:
        self._writer.execute(
            lambda conn: conn.execute(
                """
                UPDATE work_sources SET status = ?, last_error = ?, updated_at = ?
                WHERE owner_account_id = ? AND connector_key = ?
                """,
                (
                    status.value,
                    str(last_error or "")[:80],
                    time.time(),
                    owner_account_id,
                    connector_key,
                ),
            )
        )
        return self.get_state(owner_account_id, connector_key)

    def _approved_key(self, connector_key: str) -> str:
        key = _required(connector_key, "connector_key")
        if key not in self._approved:
            raise KeyError(f"source is not organization-approved: {key}")
        return key

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_RECORD_COLUMNS = (
    "owner_account_id",
    "record_id",
    "connector_key",
    "external_id",
    "external_version",
    "title",
    "kind",
    "source_status",
    "due_at",
    "source_url",
    "normalized_json",
    "pending_writeback_json",
    "conflict_external_json",
    "conflict_local_json",
    "sync_status",
    "updated_at",
)


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_record(
    owner_account_id: str,
    connector_key: str,
    raw: SourceRecordInput,
    now: float,
) -> WorkSourceRecord:
    external_id = _required(raw.external_id, "external_id")
    record_id = "work_source_" + hashlib.sha256(
        f"{connector_key}\0{external_id}".encode("utf-8")
    ).hexdigest()
    return WorkSourceRecord(
        owner_account_id=owner_account_id,
        record_id=record_id,
        connector_key=connector_key,
        external_id=external_id,
        external_version=str(raw.external_version or "").strip(),
        title=_required(raw.title, "title"),
        kind=_required(raw.kind, "kind"),
        source_status=str(raw.source_status or "").strip(),
        due_at=float(raw.due_at) if raw.due_at is not None else None,
        source_url=str(raw.source_url or "").strip(),
        normalized=_json_object(raw.normalized or {}, "normalized"),
        pending_writeback={},
        conflict_external={},
        conflict_local={},
        sync_status=SyncStatus.SYNCED,
        updated_at=now,
    )


def _record_values(
    record: WorkSourceRecord,
    preserved: sqlite3.Row | None,
) -> tuple[Any, ...]:
    return (
        record.owner_account_id,
        record.record_id,
        record.connector_key,
        record.external_id,
        record.external_version,
        record.title,
        record.kind,
        record.source_status,
        record.due_at,
        record.source_url,
        _dump(record.normalized),
        (
            str(preserved["pending_writeback_json"])
            if preserved is not None
            else _dump(record.pending_writeback)
        ),
        (
            str(preserved["conflict_external_json"])
            if preserved is not None
            else _dump(record.conflict_external)
        ),
        (
            str(preserved["conflict_local_json"])
            if preserved is not None
            else _dump(record.conflict_local)
        ),
        (
            str(preserved["sync_status"])
            if preserved is not None
            else record.sync_status.value
        ),
        record.updated_at,
    )


def _row_to_state(row: sqlite3.Row) -> WorkSourceState:
    return WorkSourceState(
        owner_account_id=str(row["owner_account_id"]),
        connector_key=str(row["connector_key"]),
        enabled=bool(row["enabled"]),
        status=SourceConnectionStatus(str(row["status"])),
        cursor=str(row["cursor"]) if row["cursor"] is not None else None,
        last_error=str(row["last_error"]),
        last_synced_at=(
            float(row["last_synced_at"]) if row["last_synced_at"] is not None else None
        ),
        updated_at=float(row["updated_at"]),
    )


def _row_to_record(row: sqlite3.Row) -> WorkSourceRecord:
    return WorkSourceRecord(
        owner_account_id=str(row["owner_account_id"]),
        record_id=str(row["record_id"]),
        connector_key=str(row["connector_key"]),
        external_id=str(row["external_id"]),
        external_version=str(row["external_version"]),
        title=str(row["title"]),
        kind=str(row["kind"]),
        source_status=str(row["source_status"]),
        due_at=float(row["due_at"]) if row["due_at"] is not None else None,
        source_url=str(row["source_url"]),
        normalized=_load_object(row["normalized_json"]),
        pending_writeback=_load_object(row["pending_writeback_json"]),
        conflict_external=_load_object(row["conflict_external_json"]),
        conflict_local=_load_object(row["conflict_local_json"]),
        sync_status=SyncStatus(str(row["sync_status"])),
        updated_at=float(row["updated_at"]),
    )


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    normalized = dict(value)
    try:
        json.dumps(normalized, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return normalized


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_object(raw: str) -> dict[str, Any]:
    value = json.loads(str(raw or "{}"))
    return dict(value) if isinstance(value, dict) else {}
