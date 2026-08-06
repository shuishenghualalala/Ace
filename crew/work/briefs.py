"""Owner-scoped current dashboard projections and immutable daily archives."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.work.models import OwnerKey


class WorkBriefArchivedError(RuntimeError):
    """Raised when a caller attempts to rewrite an immutable daily archive."""


@dataclass(frozen=True, slots=True)
class WorkPeriodReport:
    """One live or immutable period metric snapshot."""

    report_id: str | None
    period: str
    period_start: str
    period_end: str
    workspace_id: str | None
    metrics: dict[str, Any]
    archived: bool
    generated_at: float
    archived_at: float | None


@dataclass(frozen=True, slots=True)
class WorkDailyBrief:
    """One dashboard projection for an owner, business date and workspace scope."""

    owner_account_id: str
    brief_id: str
    business_date: str
    workspace_id: str | None
    content: dict[str, Any]
    input_version: str
    version: int
    archived: bool
    created_at: float
    updated_at: float
    archived_at: float | None

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.brief_id)


class WorkBriefStore:
    """Persist mutable current projections without allowing archive rewrites."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        clock: Callable[[], datetime] | None = None,
        wal_enabled: bool = True,
    ) -> None:
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_daily_briefs (
                owner_account_id TEXT NOT NULL,
                brief_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                content_json TEXT NOT NULL,
                input_version TEXT NOT NULL,
                version INTEGER NOT NULL,
                archived INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                archived_at REAL,
                PRIMARY KEY (owner_account_id, brief_id),
                UNIQUE (owner_account_id, business_date, workspace_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_daily_briefs_scope
                ON work_daily_briefs(
                    owner_account_id, workspace_id, business_date DESC, archived
                )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_period_reports (
                owner_account_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                period TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                generated_at REAL NOT NULL,
                archived_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, report_id),
                UNIQUE (owner_account_id, workspace_id, period, period_start)
            )
            """,
        )
        for statement in statements:
            conn.execute(statement)

    def put_current(
        self,
        *,
        owner_account_id: str,
        content: Mapping[str, Any],
        input_version: str,
        workspace_id: str | None = None,
    ) -> WorkDailyBrief:
        """Create or replace today's mutable projection for one dashboard scope."""
        self.freeze_due(owner_account_id)
        return self.put_for_date(
            owner_account_id=owner_account_id,
            business_date=self._local_now().date().isoformat(),
            content=content,
            input_version=input_version,
            workspace_id=workspace_id,
        )

    def put_for_date(
        self,
        *,
        owner_account_id: str,
        business_date: str,
        content: Mapping[str, Any],
        input_version: str,
        workspace_id: str | None = None,
    ) -> WorkDailyBrief:
        """Create or update one non-archived projection for an explicit business date."""
        owner = _required(owner_account_id, "owner_account_id")
        day = _business_date(business_date)
        input_token = _required(input_version, "input_version")
        scope = workspace_id.strip() if workspace_id else ""
        now = self._local_now()
        timestamp = now.timestamp()
        content_json = json.dumps(dict(content), ensure_ascii=False, sort_keys=True)

        def _write(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                """
                SELECT brief_id, archived
                FROM work_daily_briefs
                WHERE owner_account_id = ? AND business_date = ? AND workspace_id = ?
                """,
                (owner, day, scope),
            ).fetchone()
            if row is None:
                brief_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO work_daily_briefs (
                        owner_account_id, brief_id, business_date, workspace_id,
                        content_json, input_version, version, archived,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                    """,
                    (
                        owner,
                        brief_id,
                        day,
                        scope,
                        content_json,
                        input_token,
                        timestamp,
                        timestamp,
                    ),
                )
                return brief_id
            if bool(row["archived"]):
                raise WorkBriefArchivedError("archived work brief cannot be updated")
            conn.execute(
                """
                UPDATE work_daily_briefs
                SET content_json = ?, input_version = ?,
                    version = version + 1, updated_at = ?
                WHERE owner_account_id = ? AND brief_id = ?
                """,
                (content_json, input_token, timestamp, owner, row["brief_id"]),
            )
            return str(row["brief_id"])

        brief_id = self._writer.execute(_write)
        return self.get(owner, brief_id)

    def get_for_date(
        self,
        *,
        owner_account_id: str,
        business_date: str,
        workspace_id: str | None = None,
    ) -> WorkDailyBrief | None:
        """Read a scoped date after idempotently freezing any missed prior days."""
        owner = _required(owner_account_id, "owner_account_id")
        day = _business_date(business_date)
        scope = workspace_id.strip() if workspace_id else ""
        self.freeze_due(owner)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM work_daily_briefs
                WHERE owner_account_id = ? AND business_date = ? AND workspace_id = ?
                """,
                (owner, day, scope),
            ).fetchone()
        return _row_to_brief(row) if row is not None else None

    def get_current(
        self,
        *,
        owner_account_id: str,
        workspace_id: str | None = None,
    ) -> WorkDailyBrief | None:
        """Read today's projection in the system-local business timezone."""
        return self.get_for_date(
            owner_account_id=owner_account_id,
            business_date=self._local_now().date().isoformat(),
            workspace_id=workspace_id,
        )

    def freeze_due(self, owner_account_id: str) -> int:
        """Freeze all missed prior business days for one owner."""
        owner = _required(owner_account_id, "owner_account_id")
        today = self._local_now().date().isoformat()
        timestamp = self._local_now().timestamp()

        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                UPDATE work_daily_briefs
                SET archived = 1, archived_at = ?
                WHERE owner_account_id = ?
                  AND business_date < ?
                  AND archived = 0
                """,
                (timestamp, owner, today),
            )
            return max(cursor.rowcount, 0)

        return self._writer.execute(_write)

    def freeze(
        self,
        *,
        owner_account_id: str,
        business_date: str,
        workspace_id: str | None = None,
    ) -> WorkDailyBrief:
        """Idempotently freeze one existing projection without changing its content."""
        owner = _required(owner_account_id, "owner_account_id")
        day = _business_date(business_date)
        scope = workspace_id.strip() if workspace_id else ""
        timestamp = self._local_now().timestamp()

        def _write(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                """
                SELECT brief_id, archived
                FROM work_daily_briefs
                WHERE owner_account_id = ? AND business_date = ? AND workspace_id = ?
                """,
                (owner, day, scope),
            ).fetchone()
            if row is None:
                raise KeyError((day, scope))
            if not bool(row["archived"]):
                conn.execute(
                    """
                    UPDATE work_daily_briefs
                    SET archived = 1, archived_at = ?
                    WHERE owner_account_id = ? AND brief_id = ?
                    """,
                    (timestamp, owner, row["brief_id"]),
                )
            return str(row["brief_id"])

        brief_id = self._writer.execute(_write)
        return self.get(owner, brief_id)

    def get(self, owner_account_id: str, brief_id: str) -> WorkDailyBrief:
        """Read one brief without exposing another owner's matching identifier."""
        owner = _required(owner_account_id, "owner_account_id")
        identifier = _required(brief_id, "brief_id")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM work_daily_briefs
                WHERE owner_account_id = ? AND brief_id = ?
                """,
                (owner, identifier),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return _row_to_brief(row)

    def get_period_report(
        self,
        *,
        owner_account_id: str,
        period: str,
        period_start: str,
        workspace_id: str | None = None,
    ) -> WorkPeriodReport | None:
        """Return an immutable report snapshot when this period was archived."""
        owner = _required(owner_account_id, "owner_account_id")
        scope = workspace_id.strip() if workspace_id else ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT report_id, workspace_id, period, period_start, period_end,
                       metrics_json, generated_at, archived_at
                FROM work_period_reports
                WHERE owner_account_id = ? AND workspace_id = ?
                  AND period = ? AND period_start = ?
                """,
                (owner, scope, period, period_start),
            ).fetchone()
        return _row_to_period_report(row) if row is not None else None

    def archive_period_report(
        self,
        *,
        owner_account_id: str,
        period: str,
        period_start: str,
        period_end: str,
        metrics: Mapping[str, Any],
        workspace_id: str | None = None,
    ) -> WorkPeriodReport:
        """Create one immutable snapshot, or return the existing identical scope."""
        owner = _required(owner_account_id, "owner_account_id")
        scope = workspace_id.strip() if workspace_id else ""
        report_id = str(uuid.uuid4())
        timestamp = self._local_now().timestamp()
        metrics_json = json.dumps(dict(metrics), ensure_ascii=False, sort_keys=True)

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR IGNORE INTO work_period_reports (
                    owner_account_id, report_id, workspace_id, period,
                    period_start, period_end, metrics_json, generated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    report_id,
                    scope,
                    period,
                    period_start,
                    period_end,
                    metrics_json,
                    timestamp,
                    timestamp,
                ),
            )

        self._writer.execute(_write)
        archived = self.get_period_report(
            owner_account_id=owner,
            period=period,
            period_start=period_start,
            workspace_id=workspace_id,
        )
        if archived is None:
            raise RuntimeError("period report archive failed")
        return archived

    def _local_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return now.astimezone()
        return now

    def close(self) -> None:
        """Close the store connection."""
        with self._lock:
            self._conn.close()


def _row_to_brief(row: sqlite3.Row) -> WorkDailyBrief:
    return WorkDailyBrief(
        owner_account_id=str(row["owner_account_id"]),
        brief_id=str(row["brief_id"]),
        business_date=str(row["business_date"]),
        workspace_id=str(row["workspace_id"]) or None,
        content=json.loads(str(row["content_json"])),
        input_version=str(row["input_version"]),
        version=int(row["version"]),
        archived=bool(row["archived"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        archived_at=float(row["archived_at"]) if row["archived_at"] is not None else None,
    )


def _row_to_period_report(row: sqlite3.Row) -> WorkPeriodReport:
    return WorkPeriodReport(
        report_id=str(row["report_id"]),
        period=str(row["period"]),
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        workspace_id=str(row["workspace_id"]) or None,
        metrics=json.loads(str(row["metrics_json"])),
        archived=True,
        generated_at=float(row["generated_at"]),
        archived_at=float(row["archived_at"]),
    )


def _required(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _business_date(value: str) -> str:
    normalized = _required(value, "business_date")
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError("business_date must be an ISO date") from exc
