"""Owner-scoped SQLite projection for local observation records.

The store is deliberately independent from the canonical session database.  It
is a disposable diagnostic projection: a broken or unavailable file must never
prevent a request, session recovery, or shutdown from completing.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from crew.core.observability import SYSTEM_OWNER_ACCOUNT_ID, ObservationContext, ObservationRecord


TRACE_SCHEMA_VERSION = 4
TRACE_APPLICATION_ID = 0x41434554  # "ACET"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _owner(value: str | None) -> str:
    """Normalize an owner without turning an empty/untrusted value into local."""
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        # Owner ids are supplied by authenticated adapters.  Do not invoke a
        # user object's __str__ in a diagnostics path.
        text = ""
    return text or SYSTEM_OWNER_ACCOUNT_ID


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return json.dumps({"value": "<unserializable>"}, ensure_ascii=False)


def encode_cursor(started_at_us: int, trace_id: str) -> str:
    raw = _json([int(started_at_us), str(trace_id)]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[int, str] | None:
    if not cursor:
        return None
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return int(value[0]), str(value[1])
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("invalid trace cursor") from exc


class TraceStore:
    """Thread-safe SQLite writer/query facade with owner-scoped methods."""

    def __init__(self, path: str | os.PathLike[str], *, wal: bool = True) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._closed = False
        self._max_disk_bytes: int | None = None
        self._payloads_disabled = False
        self._degraded_reason = ""
        self._process_instance_id = ""
        self._disk_usage_cache = {"db_bytes": 0, "wal_bytes": 0, "shm_bytes": 0, "exports_bytes": 0, "total_bytes": 0}
        self._disk_checked_mono = 0.0
        if str(self.path) != ":memory:":
            self.path = self.path.resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd = os.open(self.path, flags, 0o600)
                os.close(fd)
        self._db = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
            timeout=0.15,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 150")
        if wal and str(self.path) != ":memory:":
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                # Some network filesystems and read-only mounts reject WAL.
                self._db.execute("PRAGMA journal_mode = DELETE")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            app_id = int(self._db.execute("PRAGMA application_id").fetchone()[0])
            if app_id not in (0, TRACE_APPLICATION_ID):
                raise RuntimeError("trace database belongs to another application")
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > TRACE_SCHEMA_VERSION:
                raise RuntimeError("trace database schema is newer than this application")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    owner_scope TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    started_at_us INTEGER NOT NULL,
                    ended_at_us INTEGER,
                    status TEXT NOT NULL DEFAULT 'running',
                    quality TEXT NOT NULL DEFAULT 'complete',
                    has_error_span INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    process_instance_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (owner_scope, trace_id)
                );
                CREATE TABLE IF NOT EXISTS spans (
                    owner_scope TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'internal',
                    module TEXT NOT NULL DEFAULT '',
                    component TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL DEFAULT '',
                    feature_id TEXT NOT NULL DEFAULT '',
                    started_at_us INTEGER NOT NULL,
                    ended_at_us INTEGER,
                    duration_ms REAL,
                    status TEXT NOT NULL DEFAULT 'running',
                    attempt INTEGER,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    process_instance_id TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (owner_scope, trace_id, span_id),
                    FOREIGN KEY (owner_scope, trace_id)
                      REFERENCES traces(owner_scope, trace_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    owner_scope TEXT NOT NULL,
                    ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    span_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'internal',
                    occurred_at_us INTEGER NOT NULL,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    module TEXT NOT NULL DEFAULT '',
                    component TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL DEFAULT '',
                    feature_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE (owner_scope, event_id)
                );
                CREATE TABLE IF NOT EXISTS payloads (
                    owner_scope TEXT NOT NULL,
                    payload_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL,
                    capture_state TEXT NOT NULL,
                    content_json TEXT,
                    redacted_paths_json TEXT NOT NULL DEFAULT '[]',
                    truncated_reason TEXT NOT NULL DEFAULT '',
                    observed_size INTEGER,
                    stored_size INTEGER,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    created_at_us INTEGER NOT NULL,
                    ingest_seq INTEGER,
                    PRIMARY KEY (owner_scope, payload_id),
                    FOREIGN KEY (owner_scope, trace_id)
                      REFERENCES traces(owner_scope, trace_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS trace_links (
                    owner_scope TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    linked_trace_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    PRIMARY KEY (owner_scope, trace_id, linked_trace_id, relation),
                    FOREIGN KEY (owner_scope, trace_id)
                      REFERENCES traces(owner_scope, trace_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS export_jobs (
                    export_id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    filter_json TEXT NOT NULL DEFAULT '{}',
                    format TEXT NOT NULL DEFAULT 'jsonl',
                    include_payloads INTEGER NOT NULL DEFAULT 0,
                    snapshot_ingest_seq INTEGER NOT NULL DEFAULT 0,
                    snapshot_started_at_us INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    count INTEGER NOT NULL DEFAULT 0,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    partial INTEGER NOT NULL DEFAULT 0,
                    partial_reason TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    created_at_us INTEGER NOT NULL,
                    updated_at_us INTEGER NOT NULL,
                    expires_at_us INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    process_instance_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_traces_owner_started
                    ON traces(owner_scope, started_at_us DESC, trace_id);
                CREATE INDEX IF NOT EXISTS idx_traces_owner_session
                    ON traces(owner_scope, session_id, started_at_us DESC);
                CREATE INDEX IF NOT EXISTS idx_traces_owner_status
                    ON traces(owner_scope, status, started_at_us DESC);
                CREATE INDEX IF NOT EXISTS idx_spans_trace_parent
                    ON spans(owner_scope, trace_id, parent_span_id, started_at_us);
                CREATE INDEX IF NOT EXISTS idx_spans_module_operation
                    ON spans(owner_scope, module, component, operation, started_at_us);
                CREATE INDEX IF NOT EXISTS idx_events_trace_seq
                    ON events(owner_scope, trace_id, ingest_seq);
                CREATE INDEX IF NOT EXISTS idx_events_module_operation
                    ON events(owner_scope, module, component, operation, occurred_at_us);
                CREATE INDEX IF NOT EXISTS idx_export_jobs_owner_status
                    ON export_jobs(owner_scope, status, updated_at_us DESC);
                CREATE INDEX IF NOT EXISTS idx_export_jobs_expiry
                    ON export_jobs(expires_at_us);
                """
            )
            self._ensure_column("traces", "process_instance_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("spans", "process_instance_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("events", "kind", "TEXT NOT NULL DEFAULT 'internal'")
            self._ensure_column("events", "source", "TEXT NOT NULL DEFAULT ''")
            payload_columns = {
                str(row[1]) for row in self._db.execute("PRAGMA table_info(payloads)").fetchall()
            }
            if "attributes_json" not in payload_columns:
                self._db.execute("ALTER TABLE payloads ADD COLUMN attributes_json TEXT NOT NULL DEFAULT '{}'")
            if "ingest_seq" not in payload_columns:
                self._db.execute("ALTER TABLE payloads ADD COLUMN ingest_seq INTEGER")
            self._db.execute(f"PRAGMA application_id = {TRACE_APPLICATION_ID}")
            self._db.execute(f"PRAGMA user_version = {TRACE_SCHEMA_VERSION}")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("trace store is closed")

    @staticmethod
    def _context(record: ObservationRecord | Mapping[str, Any]) -> ObservationContext:
        if isinstance(record, ObservationRecord):
            return record.context
        raw = record.get("context") or {}
        return raw if isinstance(raw, ObservationContext) else ObservationContext(**dict(raw))

    @staticmethod
    def _record_value(record: ObservationRecord | Mapping[str, Any], name: str, default: Any = None) -> Any:
        if isinstance(record, ObservationRecord):
            return getattr(record, name, default)
        return record.get(name, default)

    def write_batch(self, records: Iterable[ObservationRecord | Mapping[str, Any]]) -> int:
        """Persist accepted records in one short transaction; duplicates are idempotent."""
        rows = list(records)
        if not rows:
            return 0
        with self._lock:
            self._ensure_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                count = 0
                for record in rows:
                    count += self._write_record(record)
                self._db.execute("COMMIT")
                return count
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise

    def _write_record(self, record: ObservationRecord | Mapping[str, Any]) -> int:
        context = self._context(record)
        owner = _owner(context.owner_account_id)
        trace_id = str(context.trace_id or "")
        if not trace_id:
            # System events without a trace remain queryable in events only.
            trace_id = ""
        record_type = str(self._record_value(record, "record_type", "event") or "event")
        occurred = int(self._record_value(record, "occurred_at_us", time.time_ns() // 1_000))
        attrs = self._record_value(record, "attributes", {}) or {}
        attrs_dict = dict(attrs) if isinstance(attrs, Mapping) else {"value": "<non_mapping>"}
        started = self._record_value(record, "started_at_us")
        ended = self._record_value(record, "ended_at_us")
        status = str(self._record_value(record, "status", "") or "")
        name = str(self._record_value(record, "name", "event") or "event")[:512]
        if trace_id:
            self._db.execute(
                """
                INSERT INTO traces (
                  owner_scope, trace_id, session_id, request_id, source,
                  started_at_us, ended_at_us, status, quality, has_error_span,
                  summary, attributes_json, process_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?)
                ON CONFLICT(owner_scope, trace_id) DO UPDATE SET
                  ended_at_us = COALESCE(excluded.ended_at_us, traces.ended_at_us),
                  status = CASE WHEN excluded.status IN ('failed','cancelled','timed_out','incomplete')
                                THEN excluded.status ELSE traces.status END,
                  has_error_span = MAX(traces.has_error_span, excluded.has_error_span),
                  summary = CASE WHEN excluded.summary <> '' THEN excluded.summary ELSE traces.summary END,
                  process_instance_id = CASE WHEN excluded.process_instance_id <> ''
                    THEN excluded.process_instance_id ELSE traces.process_instance_id END
                """,
                (
                    owner,
                    trace_id,
                    str(context.session_id or ""),
                    str(context.request_id or ""),
                    str(context.source or ""),
                    int(started or occurred),
                    int(ended) if ended is not None else None,
                    status or "running",
                    1 if status in {"failed", "cancelled", "timed_out", "incomplete"} else 0,
                    str(attrs_dict.get("summary") or "")[:2_000],
                    _json(attrs_dict),
                    str(context.process_instance_id or "")[:128],
                ),
            )
            for link in context.links:
                if not isinstance(link, Mapping):
                    continue
                linked_trace_id = link.get("linked_trace_id") or link.get("trace_id")
                relation = link.get("relation") or "related"
                if isinstance(linked_trace_id, str) and linked_trace_id and isinstance(relation, str):
                    target = self._db.execute(
                        "SELECT 1 FROM traces WHERE owner_scope = ? AND trace_id = ?",
                        (owner, linked_trace_id),
                    ).fetchone()
                    if target is not None:
                        self._db.execute(
                            "INSERT OR IGNORE INTO trace_links "
                            "(owner_scope, trace_id, linked_trace_id, relation) VALUES (?, ?, ?, ?)",
                            (owner, trace_id, linked_trace_id[:128], relation[:128]),
                        )
        if record_type == "span.start":
            self._db.execute(
                """
                INSERT INTO spans (
                  owner_scope, trace_id, span_id, parent_span_id, name, kind,
                  module, component, operation, feature_id, started_at_us,
                  status, attempt, attributes_json, process_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_scope, trace_id, span_id) DO UPDATE SET
                  name = excluded.name, kind = excluded.kind,
                  attributes_json = excluded.attributes_json,
                  process_instance_id = CASE WHEN excluded.process_instance_id <> ''
                    THEN excluded.process_instance_id ELSE spans.process_instance_id END
                """,
                (
                    owner, trace_id, context.span_id, context.parent_span_id, name,
                    str(self._record_value(record, "kind", "internal") or "internal"),
                    context.module, context.component, context.operation, context.feature_id,
                    int(started or occurred), status or "running",
                    _int_attr(attrs_dict.get("attempt")), _json(attrs_dict),
                    str(context.process_instance_id or "")[:128],
                ),
            )
            return 1
        if record_type == "span.end":
            error_type = str(self._record_value(record, "error_type", "") or "")[:256]
            error_message = str(self._record_value(record, "error_message", "") or "")[:2_000]
            self._db.execute(
                """
                INSERT INTO spans (
                  owner_scope, trace_id, span_id, parent_span_id, name, kind,
                  module, component, operation, feature_id, started_at_us,
                  ended_at_us, duration_ms, status, attempt, attributes_json,
                  error_type, error_message, process_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_scope, trace_id, span_id) DO UPDATE SET
                  ended_at_us = excluded.ended_at_us, duration_ms = excluded.duration_ms,
                  status = excluded.status, error_type = excluded.error_type,
                  error_message = excluded.error_message,
                  attributes_json = excluded.attributes_json,
                  process_instance_id = CASE WHEN excluded.process_instance_id <> ''
                    THEN excluded.process_instance_id ELSE spans.process_instance_id END
                """,
                (
                    owner, trace_id, context.span_id, context.parent_span_id, name,
                    str(self._record_value(record, "kind", "internal") or "internal"),
                    context.module, context.component, context.operation, context.feature_id,
                    int(started or occurred), int(ended or occurred),
                    _float_attr(self._record_value(record, "duration_ms")), status or "succeeded",
                    _int_attr(attrs_dict.get("attempt")), _json(attrs_dict), error_type, error_message,
                    str(context.process_instance_id or "")[:128],
                ),
            )
            if status in {"failed", "cancelled", "timed_out"} and trace_id:
                self._db.execute(
                    "UPDATE traces SET has_error_span = 1 WHERE owner_scope = ? AND trace_id = ?",
                    (owner, trace_id),
                )
            if not context.parent_span_id and trace_id and status in {
                "succeeded", "failed", "cancelled", "timed_out", "incomplete",
            }:
                self._db.execute(
                    "UPDATE traces SET status = ?, ended_at_us = ? "
                    "WHERE owner_scope = ? AND trace_id = ?",
                    (status, int(ended or occurred), owner, trace_id),
                )
            return 1
        event_id = str(self._record_value(record, "event_id", "") or "")
        if not event_id:
            event_id = f"{trace_id}:{occurred}:{name}:{hash(_json(attrs_dict))}"
        level = str(attrs_dict.get("level") or "INFO").upper()[:32]
        message = str(attrs_dict.get("message") or attrs_dict.get("summary") or "")[:4_000]
        self._db.execute(
            """
            INSERT OR IGNORE INTO events (
              owner_scope, event_id, trace_id, span_id, name, occurred_at_us,
              level, module, component, operation, feature_id, source, message, status,
              attributes_json, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner, event_id, trace_id, context.span_id, name, occurred, level,
                context.module, context.component, context.operation, context.feature_id,
                context.source, message, status, _json(attrs_dict),
                str(self._record_value(record, "kind", "internal") or "internal")[:64],
            ),
        )
        return 1 if self._db.execute("SELECT changes()").fetchone()[0] else 0

    def write_payload(
        self,
        *,
        owner_account_id: str,
        payload_id: str,
        trace_id: str,
        span_id: str,
        stage: str,
        capture_state: str,
        content: Any,
        redacted_paths: Iterable[str] = (),
        truncated_reason: str = "",
        observed_size: int | None = None,
        stored_size: int | None = None,
        attributes: Mapping[str, Any] | None = None,
        created_at_us: int | None = None,
    ) -> None:
        self.write_payload_batch([{
            "owner_account_id": owner_account_id,
            "payload_id": payload_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "stage": stage,
            "capture_state": capture_state,
            "content": content,
            "redacted_paths": tuple(redacted_paths),
            "truncated_reason": truncated_reason,
            "observed_size": observed_size,
            "stored_size": stored_size,
            "attributes": dict(attributes or {}),
            "created_at_us": created_at_us,
        }])

    def write_payload_batch(self, payloads: Iterable[Mapping[str, Any]]) -> int:
        rows = list(payloads)
        if not rows:
            return 0
        with self._lock:
            self._ensure_open()
            if not self.payload_capture_allowed():
                return 0
            self._db.execute("BEGIN IMMEDIATE")
            try:
                inserted = 0
                for item in rows:
                    inserted += self._write_payload(item)
                self._db.execute("COMMIT")
                return inserted
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _write_payload(self, item: Mapping[str, Any]) -> int:
        ingest_seq = item.get("ingest_seq")
        if ingest_seq is None:
            # Events keep the public monotonic waterline.  Payloads get the
            # next value from the same projection so an export can exclude
            # content captured after its cutoff even when the producer did
            # not emit a companion event.
            row = self._db.execute(
                "SELECT MAX(value) FROM ("
                "SELECT COALESCE(MAX(ingest_seq), 0) AS value FROM events "
                "UNION ALL SELECT COALESCE(MAX(ingest_seq), 0) AS value FROM payloads"
                ")"
            ).fetchone()
            ingest_seq = int(row[0] or 0) + 1
        self._db.execute(
            """
            INSERT OR IGNORE INTO payloads (
              owner_scope, payload_id, trace_id, span_id, stage, capture_state,
              content_json, redacted_paths_json, truncated_reason,
              observed_size, stored_size, attributes_json, created_at_us, ingest_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _owner(item.get("owner_account_id") if isinstance(item.get("owner_account_id"), str) else None),
                item.get("payload_id") if isinstance(item.get("payload_id"), str) else "",
                item.get("trace_id") if isinstance(item.get("trace_id"), str) else "",
                item.get("span_id") if isinstance(item.get("span_id"), str) else "",
                (item.get("stage") if isinstance(item.get("stage"), str) else "")[:256],
                (item.get("capture_state") if isinstance(item.get("capture_state"), str) else "disabled")[:32],
                None if item.get("content") is None else _json(item.get("content")),
                _json(list(item.get("redacted_paths") or ())),
                (item.get("truncated_reason") if isinstance(item.get("truncated_reason"), str) else "")[:256],
                item.get("observed_size"), item.get("stored_size"),
                _json(item.get("attributes") if isinstance(item.get("attributes"), Mapping) else {}),
                int(item.get("created_at_us") or time.time_ns() // 1_000),
                int(ingest_seq),
            ),
        )
        return 1 if self._db.execute("SELECT changes()").fetchone()[0] else 0

    def list_traces(
        self,
        *,
        owner_account_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        status: Iterable[str] = (),
        source: Iterable[str] = (),
        module: Iterable[str] = (),
        component: Iterable[str] = (),
        operation: Iterable[str] = (),
        keyword: str = "",
        start_after_us: int | None = None,
        start_before_us: int | None = None,
        min_server_total_ms: float | None = None,
        min_duration_ms: float | None = None,
        feature_id: Iterable[str] = (),
        provider: Iterable[str] = (),
        model: Iterable[str] = (),
        tool: Iterable[str] = (),
        service: Iterable[str] = (),
        has_error_span: bool | None = None,
        snapshot_ingest_seq: int | None = None,
        snapshot_started_at_us: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        params: list[Any] = [_owner(owner_account_id)]
        clauses = ["t.owner_scope = ?"]
        if trace_id:
            clauses.append("t.trace_id = ?")
            params.append(str(trace_id))
        if session_id:
            clauses.append("t.session_id = ?")
            params.append(str(session_id))
        if request_id:
            clauses.append("t.request_id = ?")
            params.append(str(request_id))
        for field_name, values in (
            ("status", status), ("source", source),
        ):
            values = [str(item) for item in values if str(item)]
            if values:
                clauses.append(f"t.{field_name} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        for field_name, values in (("module", module), ("component", component), ("operation", operation)):
            values = [str(item) for item in values if str(item)]
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM spans s WHERE s.owner_scope=t.owner_scope "
                    f"AND s.trace_id=t.trace_id AND s.{field_name} IN ({placeholders}))"
                )
                params.extend(values)
        if keyword:
            clauses.append(
                "(LOWER(t.summary) LIKE ? OR EXISTS (SELECT 1 FROM events e WHERE "
                "e.owner_scope=t.owner_scope AND e.trace_id=t.trace_id AND LOWER(e.message) LIKE ?))"
            )
            needle = f"%{str(keyword).lower()}%"
            params.extend([needle, needle])
        if start_after_us is not None:
            clauses.append("t.started_at_us >= ?")
            params.append(int(start_after_us))
        if start_before_us is not None:
            clauses.append("t.started_at_us <= ?")
            params.append(int(start_before_us))
        if min_server_total_ms is not None:
            clauses.append("(t.ended_at_us - t.started_at_us) >= ?")
            params.append(float(min_server_total_ms) * 1_000)
        if min_duration_ms is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM spans s WHERE s.owner_scope=t.owner_scope "
                "AND s.trace_id=t.trace_id AND s.duration_ms >= ?)"
            )
            params.append(float(min_duration_ms))
        for field_name, values in (("feature_id", feature_id),):
            values = [str(item) for item in values if str(item)]
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM spans s WHERE s.owner_scope=t.owner_scope "
                    f"AND s.trace_id=t.trace_id AND s.{field_name} IN ({placeholders}))"
                )
                params.extend(values)
        for json_field, values in (
            ("provider", provider), ("model", model), ("tool", tool), ("service", service),
        ):
            values = [str(item) for item in values if str(item)]
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(
                    "EXISTS (SELECT 1 FROM spans s WHERE s.owner_scope=t.owner_scope "
                    "AND s.trace_id=t.trace_id AND json_extract(s.attributes_json, '$."
                    f"{json_field}') IN ({placeholders}))"
                )
                params.extend(values)
        if has_error_span is not None:
            clauses.append("t.has_error_span = ?")
            params.append(1 if has_error_span else 0)
        if snapshot_ingest_seq is not None:
            # A trace is eligible when it already had an observation at the
            # snapshot.  Existing traces remain eligible when later events
            # arrive; those later rows are filtered by list_events instead of
            # making the whole trace disappear from the export.
            cutoff = int(snapshot_ingest_seq)
            snapshot_time = int(snapshot_started_at_us or time.time_ns() // 1_000)
            clauses.append(
                "(EXISTS (SELECT 1 FROM events e_snapshot WHERE "
                "e_snapshot.owner_scope=t.owner_scope AND e_snapshot.trace_id=t.trace_id "
                "AND e_snapshot.ingest_seq <= ?) OR "
                "(NOT EXISTS (SELECT 1 FROM events e_any WHERE "
                "e_any.owner_scope=t.owner_scope AND e_any.trace_id=t.trace_id) "
                "AND t.started_at_us <= ?))"
            )
            params.extend([cutoff, snapshot_time])
        decoded = decode_cursor(cursor)
        if decoded is not None:
            clauses.append("(t.started_at_us < ? OR (t.started_at_us = ? AND t.trace_id < ?))")
            params.extend([decoded[0], decoded[0], decoded[1]])
        where = " AND ".join(clauses)
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                f"SELECT t.*, CASE WHEN t.ended_at_us IS NULL THEN NULL "
                f"ELSE (t.ended_at_us - t.started_at_us) / 1000.0 END AS server_total_ms "
                f"FROM traces t WHERE {where} ORDER BY t.started_at_us DESC, t.trace_id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(int(rows[-1]["started_at_us"]), str(rows[-1]["trace_id"])) if has_more and rows else None
        return {"items": [self._row(row) for row in rows], "next_cursor": next_cursor, "has_more": has_more}

    def get_trace(self, *, owner_account_id: str, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM traces WHERE owner_scope = ? AND trace_id = ?",
                (_owner(owner_account_id), str(trace_id)),
            ).fetchone()
        if row is None:
            return None
        result = self._row(row)
        result["links"] = self.list_links(owner_account_id=owner_account_id, trace_id=trace_id)
        return result

    def list_links(self, *, owner_account_id: str, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                "SELECT l.linked_trace_id, l.relation FROM trace_links l "
                "JOIN traces target ON target.owner_scope = l.owner_scope "
                "AND target.trace_id = l.linked_trace_id "
                "WHERE l.owner_scope = ? AND l.trace_id = ? ORDER BY l.linked_trace_id, l.relation",
                (_owner(owner_account_id), str(trace_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def write_link(
        self,
        *,
        owner_account_id: str,
        trace_id: str,
        linked_trace_id: str,
        relation: str = "related",
    ) -> bool:
        owner = _owner(owner_account_id)
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT 1 FROM traces WHERE owner_scope = ? AND trace_id = ?",
                (owner, str(trace_id)),
            ).fetchone()
            target = self._db.execute(
                "SELECT 1 FROM traces WHERE owner_scope = ? AND trace_id = ?",
                (owner, str(linked_trace_id)),
            ).fetchone() if linked_trace_id else None
            if row is None or target is None:
                return False
            self._db.execute(
                "INSERT OR IGNORE INTO trace_links "
                "(owner_scope, trace_id, linked_trace_id, relation) VALUES (?, ?, ?, ?)",
                (owner, str(trace_id), str(linked_trace_id)[:128], str(relation or "related")[:128]),
            )
            return bool(self._db.execute("SELECT changes()").fetchone()[0])

    def event_exists(self, *, owner_account_id: str, event_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT 1 FROM events WHERE owner_scope = ? AND event_id = ?",
                (_owner(owner_account_id), str(event_id)),
            ).fetchone()
        return row is not None

    def find_trace(self, *, owner_account_id: str, request_id: str = "", session_id: str = "") -> dict[str, Any] | None:
        """Resolve a trace only from server-owned correlation fields."""
        clauses = ["owner_scope = ?"]
        params: list[Any] = [_owner(owner_account_id)]
        if request_id:
            clauses.append("request_id = ?")
            params.append(str(request_id))
        if session_id:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if len(clauses) == 1:
            return None
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                f"SELECT * FROM traces WHERE {' AND '.join(clauses)} "
                "ORDER BY started_at_us DESC LIMIT 1",
                params,
            ).fetchone()
        return self._row(row) if row else None

    def list_spans(
        self,
        *,
        owner_account_id: str,
        trace_id: str,
        limit: int = 500,
        parent_span_id: str | None = None,
        kind: Iterable[str] = (),
        after_started_at_us: int | None = None,
        after_span_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["owner_scope = ?", "trace_id = ?"]
        params: list[Any] = [_owner(owner_account_id), str(trace_id)]
        if parent_span_id is not None:
            clauses.append("parent_span_id = ?")
            params.append(str(parent_span_id))
        kinds = [str(value) for value in kind if isinstance(value, str) and value]
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if after_started_at_us is not None:
            if after_span_id:
                clauses.append("(started_at_us > ? OR (started_at_us = ? AND span_id > ?))")
                params.extend([int(after_started_at_us), int(after_started_at_us), str(after_span_id)])
            else:
                clauses.append("started_at_us > ?")
                params.append(int(after_started_at_us))
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                f"SELECT * FROM spans WHERE {' AND '.join(clauses)} "
                "ORDER BY started_at_us ASC, span_id ASC LIMIT ?",
                [*params, max(1, min(int(limit), MAX_PAGE_SIZE + 1))],
            ).fetchall()
        return [self._row(row) for row in rows]

    def page_spans(
        self,
        *,
        owner_account_id: str,
        trace_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        parent_span_id: str | None = None,
        kind: Iterable[str] = (),
        after_started_at_us: int | None = None,
        after_span_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        items = self.list_spans(
            owner_account_id=owner_account_id,
            trace_id=trace_id,
            limit=limit + 1,
            parent_span_id=parent_span_id,
            kind=kind,
            after_started_at_us=after_started_at_us,
            after_span_id=after_span_id,
        )
        has_more = len(items) > limit
        items = items[:limit]
        next_item = items[-1] if has_more and items else None
        return {
            "items": items,
            "has_more": has_more,
            "next_started_at_us": next_item.get("started_at_us") if next_item else None,
            "next_span_id": next_item.get("span_id") if next_item else None,
        }

    def list_events(
        self,
        *,
        owner_account_id: str,
        trace_id: str | None = None,
        span_id: str | None = None,
        kind: Iterable[str] = (),
        level: Iterable[str] = (),
        limit: int = 500,
        after_seq: int | None = None,
        max_ingest_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["owner_scope = ?"]
        params: list[Any] = [_owner(owner_account_id)]
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(str(trace_id))
        if span_id:
            clauses.append("span_id = ?")
            params.append(str(span_id))
        kinds = [str(value) for value in kind if isinstance(value, str) and value]
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        levels = [str(value).upper() for value in level if str(value)]
        if levels:
            clauses.append(f"level IN ({','.join('?' for _ in levels)})")
            params.extend(levels)
        if after_seq is not None:
            clauses.append("ingest_seq > ?")
            params.append(int(after_seq))
        if max_ingest_seq is not None:
            clauses.append("ingest_seq <= ?")
            params.append(int(max_ingest_seq))
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
                "ORDER BY ingest_seq ASC LIMIT ?",
                [*params, max(1, min(int(limit), 2_000))],
            ).fetchall()
        return [self._row(row) for row in rows]

    def page_events(
        self,
        *,
        owner_account_id: str,
        trace_id: str | None = None,
        span_id: str | None = None,
        kind: Iterable[str] = (),
        level: Iterable[str] = (),
        limit: int = DEFAULT_PAGE_SIZE,
        after_seq: int | None = None,
        max_ingest_seq: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        items = self.list_events(
            owner_account_id=owner_account_id,
            trace_id=trace_id,
            span_id=span_id,
            kind=kind,
            level=level,
            limit=limit + 1,
            after_seq=after_seq,
            max_ingest_seq=max_ingest_seq,
        )
        has_more = len(items) > limit
        items = items[:limit]
        next_seq = items[-1].get("ingest_seq") if has_more and items else None
        return {"items": items, "has_more": has_more, "next_after_seq": next_seq}

    def list_logs(
        self,
        *,
        owner_account_id: str,
        limit: int = 500,
        level: Iterable[str] = (),
        keyword: str = "",
        trace_id: str | None = None,
        module: Iterable[str] = (),
        component: Iterable[str] = (),
        operation: Iterable[str] = (),
        source: Iterable[str] = (),
        start_after_us: int | None = None,
        start_before_us: int | None = None,
        after_seq: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        clauses = ["owner_scope = ?"]
        params: list[Any] = [_owner(owner_account_id)]
        levels = [str(value).upper() for value in level if str(value)]
        if levels:
            clauses.append(f"level IN ({','.join('?' for _ in levels)})")
            params.extend(levels)
        if keyword:
            clauses.append("(LOWER(message) LIKE ? OR LOWER(name) LIKE ?)")
            needle = f"%{str(keyword).lower()}%"
            params.extend([needle, needle])
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(str(trace_id))
        for field_name, values in (("module", module), ("component", component), ("operation", operation), ("source", source)):
            values = [str(value) for value in values if isinstance(value, str) and value]
            if values:
                clauses.append(f"{field_name} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if start_after_us is not None:
            clauses.append("occurred_at_us >= ?")
            params.append(int(start_after_us))
        if start_before_us is not None:
            clauses.append("occurred_at_us <= ?")
            params.append(int(start_before_us))
        if after_seq is not None:
            clauses.append("ingest_seq < ?")
            params.append(int(after_seq))
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
                "ORDER BY ingest_seq DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        has_more = len(rows) > limit
        items = [self._row(row) for row in rows[:limit]]
        return {
            "items": items,
            "total": len(items),
            "has_more": has_more,
            "next_after_seq": items[-1].get("ingest_seq") if items else None,
        }

    def get_payload(
        self,
        *,
        owner_account_id: str,
        payload_id: str,
        max_ingest_seq: int | None = None,
        max_created_at_us: int | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            clauses = ["owner_scope = ?", "payload_id = ?"]
            params: list[Any] = [_owner(owner_account_id), str(payload_id)]
            if max_ingest_seq is not None:
                clauses.append("(ingest_seq IS NULL OR ingest_seq <= ?)")
                params.append(int(max_ingest_seq))
            if max_created_at_us is not None:
                clauses.append("created_at_us <= ?")
                params.append(int(max_created_at_us))
            row = self._db.execute(
                f"SELECT * FROM payloads WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return self._row(row) if row else None

    def status(self, *, owner_account_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            params: tuple[Any, ...] = () if owner_account_id is None else (_owner(owner_account_id),)
            where = "" if owner_account_id is None else " WHERE owner_scope = ?"
            traces = self._db.execute(f"SELECT COUNT(*) FROM traces{where}", params).fetchone()[0]
            events = self._db.execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()[0]
            payloads = self._db.execute(f"SELECT COUNT(*) FROM payloads{where}", params).fetchone()[0]
            if owner_account_id is None:
                seq = self._db.execute("SELECT COALESCE(MAX(ingest_seq), 0) FROM events").fetchone()[0]
            else:
                seq = self._db.execute(
                    "SELECT COALESCE(MAX(ingest_seq), 0) FROM events WHERE owner_scope = ?",
                    (_owner(owner_account_id),),
                ).fetchone()[0]
            disk = self._refresh_disk_state(force=True)
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "path": str(self.path),
            "traces": int(traces),
            "events": int(events),
            "payloads": int(payloads),
            "latest_ingest_seq": int(seq),
            "degraded": bool(self._degraded_reason),
            "degraded_reason": self._degraded_reason,
            "disk_bytes": disk["total_bytes"],
            "disk_budget_bytes": self._max_disk_bytes,
            "payload_capture_enabled": not self._payloads_disabled,
        }

    def set_disk_budget(self, max_disk_bytes: int | None) -> None:
        with self._lock:
            self._max_disk_bytes = None if max_disk_bytes is None else max(0, int(max_disk_bytes))
            self._refresh_disk_state(force=True)

    def payload_capture_allowed(self) -> bool:
        with self._lock:
            self._refresh_disk_state()
            return not self._payloads_disabled

    def disk_usage(self, *, export_dir: str | os.PathLike[str] | None = None) -> dict[str, int]:
        """Return physical bytes, including SQLite sidecars and export artifacts."""
        if str(self.path) == ":memory:":
            db_bytes = wal_bytes = shm_bytes = 0
            base = Path.cwd()
        else:
            db_bytes = self._file_size(self.path)
            wal_bytes = self._file_size(Path(f"{self.path}-wal"))
            shm_bytes = self._file_size(Path(f"{self.path}-shm"))
            base = self.path.parent
        directory = Path(export_dir) if export_dir is not None else base / "exports"
        exports_bytes = 0
        if directory.is_dir():
            try:
                for child in directory.rglob("*"):
                    if child.is_file() and not child.is_symlink():
                        exports_bytes += self._file_size(child)
            except OSError:
                pass
        return {
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "exports_bytes": exports_bytes,
            "total_bytes": db_bytes + wal_bytes + shm_bytes + exports_bytes,
        }

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return max(0, int(path.stat().st_size))
        except OSError:
            return 0

    def _refresh_disk_state(self, *, force: bool = False) -> dict[str, int]:
        now_mono = time.monotonic()
        if not force and now_mono - self._disk_checked_mono < 1.0:
            return dict(self._disk_usage_cache)
        usage = self.disk_usage()
        self._disk_usage_cache = usage
        self._disk_checked_mono = now_mono
        if self._max_disk_bytes is not None and usage["total_bytes"] >= self._max_disk_bytes:
            self._payloads_disabled = True
            self._degraded_reason = "max_disk_bytes"
        elif self._degraded_reason == "max_disk_bytes":
            self._payloads_disabled = False
            self._degraded_reason = ""
        return usage

    def recover_incomplete(self, *, process_instance_id: str, now_us: int | None = None) -> dict[str, int]:
        """Mark only records owned by a prior process as incomplete."""
        process_id = str(process_instance_id or "")
        if not process_id:
            return {"traces": 0, "spans": 0}
        now = int(now_us or time.time_ns() // 1_000)
        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                trace_count = self._db.execute(
                    "UPDATE traces SET status='incomplete', quality='incomplete', ended_at_us=? "
                    "WHERE status='running' AND process_instance_id <> '' AND process_instance_id <> ?",
                    (now, process_id),
                ).rowcount
                span_count = self._db.execute(
                    "UPDATE spans SET status='incomplete', ended_at_us=?, duration_ms="
                    "CASE WHEN started_at_us <= ? THEN (? - started_at_us) / 1000.0 ELSE 0 END "
                    "WHERE status='running' AND process_instance_id <> '' AND process_instance_id <> ?",
                    (now, now, now, process_id),
                ).rowcount
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"traces": max(0, int(trace_count)), "spans": max(0, int(span_count))}

    def maintenance(
        self,
        *,
        process_instance_id: str = "",
        retention_days: int = 7,
        max_disk_bytes: int | None = None,
        export_dir: str | os.PathLike[str] | None = None,
        now_us: int | None = None,
    ) -> dict[str, Any]:
        """Run bounded, failure-contained startup maintenance."""
        result: dict[str, Any] = {"recovered": {"traces": 0, "spans": 0}, "pruned": 0, "exports": {}}
        try:
            if process_instance_id:
                result["recovered"] = self.recover_incomplete(process_instance_id=process_instance_id, now_us=now_us)
            self.set_disk_budget(max_disk_bytes)
            result["pruned"] = self.prune(retention_days=retention_days, max_rows=500)
            result["exports"] = self.cleanup_export_artifacts(export_dir=export_dir, now_us=now_us)
            with self._lock:
                self._ensure_open()
                try:
                    self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.DatabaseError:
                    pass
                try:
                    self._db.execute("PRAGMA incremental_vacuum(200)")
                except sqlite3.DatabaseError:
                    pass
                usage = self._refresh_disk_state(force=True)
            if max_disk_bytes is not None and usage["total_bytes"] > max(0, int(max_disk_bytes)):
                # Reclaim in bounded chunks.  A single trace can be larger
                # than the remaining budget, so keep trying while completed
                # groups exist; never run an unbounded deletion loop during
                # startup or a writer callback.
                budget_pruned = 0
                budget = max(0, int(max_disk_bytes))
                for _ in range(16):
                    with self._lock:
                        usage = self._refresh_disk_state(force=True)
                    if usage["total_bytes"] <= budget:
                        break
                    removed = self.prune_oldest_completed(max_rows=500)
                    budget_pruned += removed
                    if not removed:
                        break
                    with self._lock:
                        try:
                            self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        except sqlite3.DatabaseError:
                            pass
                result["budget_pruned"] = budget_pruned
                with self._lock:
                    usage = self._refresh_disk_state(force=True)
            result["disk"] = usage
        except Exception as exc:  # diagnostics maintenance is never business-critical
            result["error"] = f"{type(exc).__name__}: {exc}"[:500]
            try:
                with self._lock:
                    result["disk"] = self._refresh_disk_state(force=True)
            except Exception:
                pass
        return result

    def prune_oldest_completed(self, *, max_rows: int = 100) -> int:
        """Bounded emergency reclamation used before disabling payload capture."""
        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT owner_scope, trace_id FROM traces WHERE ended_at_us IS NOT NULL "
                    "ORDER BY ended_at_us ASC LIMIT ?",
                    (max(1, min(int(max_rows), 1_000)),),
                ).fetchall()
                for row in rows:
                    self._db.execute(
                        "DELETE FROM traces WHERE owner_scope = ? AND trace_id = ?",
                        (row[0], row[1]),
                    )
                self._db.execute("COMMIT")
                return len(rows)
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def create_export_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        now = int(job.get("created_at_us") or time.time_ns() // 1_000)
        export_dir = self.path.parent / "exports"
        artifact_value = str(job.get("artifact_path") or "")[:2_000]
        if artifact_value and not self._safe_export_path(Path(artifact_value), export_dir):
            raise ValueError("export artifact path is outside the export directory")
        values = {
            "export_id": str(job.get("export_id") or ""),
            "owner_scope": _owner(job.get("owner_scope") if isinstance(job.get("owner_scope"), str) else None),
            "filter_json": _json(job.get("filter") if isinstance(job.get("filter"), Mapping) else {}),
            "format": str(job.get("format") or "jsonl")[:16],
            "include_payloads": 1 if job.get("include_payloads") else 0,
            "snapshot_ingest_seq": int(job.get("snapshot_ingest_seq") or 0),
            "snapshot_started_at_us": int(job.get("snapshot_started_at_us") or now),
            "status": str(job.get("status") or "running")[:32],
            "count": int(job.get("count") or 0),
            "bytes": int(job.get("bytes") or 0),
            "partial": 1 if job.get("partial") else 0,
            "partial_reason": str(job.get("partial_reason") or "")[:128],
            "artifact_path": artifact_value,
            "created_at_us": now,
            "updated_at_us": int(job.get("updated_at_us") or now),
            "expires_at_us": int(job.get("expires_at_us") or (now + 3_600_000_000)),
            "error": str(job.get("error") or "")[:500],
            "process_instance_id": str(job.get("process_instance_id") or "")[:128],
        }
        with self._lock:
            self._ensure_open()
            self._db.execute(
                "INSERT INTO export_jobs (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                tuple(values.values()),
            )
        return self._export_job(values)

    def get_export_job(self, *, owner_account_id: str, export_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM export_jobs WHERE owner_scope = ? AND export_id = ?",
                (_owner(owner_account_id), str(export_id)),
            ).fetchone()
        return self._export_job(dict(row)) if row else None

    def update_export_job(self, *, owner_account_id: str, export_id: str, **updates: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "count", "bytes", "partial", "partial_reason", "artifact_path",
            "error", "process_instance_id", "snapshot_ingest_seq", "snapshot_started_at_us",
            "expires_at_us", "include_payloads", "format",
        }
        fields = {key: value for key, value in updates.items() if key in allowed}
        if not fields:
            return self.get_export_job(owner_account_id=owner_account_id, export_id=export_id)
        fields["updated_at_us"] = time.time_ns() // 1_000
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"partial", "include_payloads"}:
                normalized[key] = 1 if value else 0
            elif key in {"count", "bytes", "snapshot_ingest_seq", "snapshot_started_at_us", "expires_at_us", "updated_at_us"}:
                normalized[key] = int(value or 0)
            elif key == "artifact_path":
                path = Path(str(value or ""))
                if str(value or "") and not self._safe_export_path(path, self.path.parent / "exports"):
                    raise ValueError("export artifact path is outside the export directory")
                normalized[key] = str(value or "")[:2_000]
            else:
                normalized[key] = str(value or "")[:2_000]
        with self._lock:
            self._ensure_open()
            params = [*normalized.values(), _owner(owner_account_id), str(export_id)]
            self._db.execute(
                f"UPDATE export_jobs SET {', '.join(f'{key} = ?' for key in normalized)} "
                "WHERE owner_scope = ? AND export_id = ?",
                params,
            )
        return self.get_export_job(owner_account_id=owner_account_id, export_id=export_id)

    def recover_export_jobs(self, *, process_instance_id: str, now_us: int | None = None) -> int:
        now = int(now_us or time.time_ns() // 1_000)
        with self._lock:
            self._ensure_open()
            count = self._db.execute(
                "UPDATE export_jobs SET status='interrupted', partial=1, partial_reason='process_restart', "
                "updated_at_us=? WHERE status='running' AND process_instance_id <> ?",
                (now, str(process_instance_id or "")),
            ).rowcount
        return max(0, int(count))

    def cleanup_export_artifacts(
        self,
        *,
        export_dir: str | os.PathLike[str] | None = None,
        now_us: int | None = None,
        orphan_age_us: int = 3_600_000_000,
    ) -> dict[str, int]:
        now = int(now_us or time.time_ns() // 1_000)
        directory = Path(export_dir) if export_dir is not None else self.path.parent / "exports"
        deleted_files = 0
        expired_jobs = 0
        with self._lock:
            self._ensure_open()
            expired = self._db.execute(
                "SELECT export_id, artifact_path FROM export_jobs WHERE expires_at_us <= ?",
                (now,),
            ).fetchall()
            for row in expired:
                path = Path(str(row[1] or ""))
                if self._safe_export_path(path, directory) and path.exists():
                    try:
                        path.unlink()
                        deleted_files += 1
                    except OSError:
                        pass
                self._db.execute(
                    "UPDATE export_jobs SET status='expired', partial=1, partial_reason='expired', "
                    "artifact_path='', updated_at_us=? WHERE export_id = ?",
                    (now, row[0]),
                )
                expired_jobs += 1
        if directory.is_dir():
            cutoff = time.time() - max(0, orphan_age_us) / 1_000_000
            try:
                for path in directory.iterdir():
                    if not path.is_file() or not (path.name.endswith(".tmp") or path.name.endswith(".records.tmp")):
                        continue
                    try:
                        if path.stat().st_mtime <= cutoff:
                            path.unlink()
                            deleted_files += 1
                    except OSError:
                        pass
            except OSError:
                pass
        return {"expired_jobs": expired_jobs, "deleted_files": deleted_files}

    @staticmethod
    def _safe_export_path(path: Path, export_dir: Path) -> bool:
        try:
            return path.resolve().parent == export_dir.resolve()
        except OSError:
            return False

    @staticmethod
    def _export_job(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        try:
            result["filter"] = json.loads(result.pop("filter_json", "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["filter"] = {}
            result.pop("filter_json", None)
        result["include_payloads"] = bool(result.get("include_payloads"))
        result["partial"] = bool(result.get("partial"))
        result["path"] = result.get("artifact_path", "")
        return result

    def prune(self, *, retention_days: int = 7, max_rows: int | None = None) -> int:
        """Delete only completed old traces as a bounded maintenance operation."""
        cutoff = int((time.time() - max(0, int(retention_days)) * 86_400) * 1_000_000)
        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                params: list[Any] = [cutoff]
                limit_sql = ""
                if max_rows is not None:
                    limit_sql = " LIMIT ?"
                    params.append(max(1, int(max_rows)))
                ids = self._db.execute(
                    "SELECT owner_scope, trace_id FROM traces WHERE ended_at_us IS NOT NULL "
                    "AND ended_at_us < ? ORDER BY ended_at_us ASC" + limit_sql,
                    params,
                ).fetchall()
                for row in ids:
                    self._db.execute(
                        "DELETE FROM traces WHERE owner_scope = ? AND trace_id = ?",
                        (row[0], row[1]),
                    )
                self._db.execute("COMMIT")
                return len(ids)
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        result = dict(row)
        for key in ("attributes_json", "redacted_paths_json", "content_json"):
            if key in result:
                try:
                    result[key[:-5] if key.endswith("_json") else key] = json.loads(result[key])
                except (TypeError, ValueError, json.JSONDecodeError):
                    result[key[:-5] if key.endswith("_json") else key] = None if key == "content_json" else {}
                del result[key]
        return result


def _int_attr(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (ValueError, TypeError):
        return None


def _float_attr(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (ValueError, TypeError):
        return None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "TRACE_APPLICATION_ID",
    "TRACE_SCHEMA_VERSION",
    "TraceStore",
    "decode_cursor",
    "encode_cursor",
]
