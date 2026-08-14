"""Owner-scoped Work settings: notifications, DND and Workspace Wiki ingestion."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class WorkspaceValidator(Protocol):
    """Minimum WorkspaceStore surface needed to validate ownership."""

    def get(self, workspace_id: str, owner_account_id: str = "") -> dict[str, Any]: ...


class WorkSettingsStore:
    """Persist notification, DND and Wiki ingestion settings without deleting history."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        workspace_store: WorkspaceValidator | None = None,
        wal_enabled: bool = True,
    ) -> None:
        self._workspace_store = workspace_store
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_settings (
                owner_account_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                dnd_enabled INTEGER NOT NULL DEFAULT 0,
                dnd_start TEXT,
                dnd_end TEXT,
                source_notifications_json TEXT NOT NULL DEFAULT '{}',
                auto_status_transition INTEGER NOT NULL DEFAULT 0,
                wiki_fulltext_indexing INTEGER NOT NULL DEFAULT 0,
                wiki_ingestion_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, workspace_id)
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(work_settings)").fetchall()
        }
        if "auto_status_transition" not in columns:
            conn.execute(
                "ALTER TABLE work_settings "
                "ADD COLUMN auto_status_transition INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------ #
    # Account-level settings
    # ------------------------------------------------------------------ #

    def get_account_settings(self, owner_account_id: str) -> dict[str, Any]:
        """Return account-level DND and source notification settings."""
        row = self._select(owner_account_id, "")
        return _row_to_account_settings(row)

    def update_account_settings(
        self,
        owner_account_id: str,
        *,
        dnd_enabled: bool | None = None,
        dnd_start: str | None = None,
        dnd_end: str | None = None,
        source_notifications: dict[str, bool] | None = None,
        auto_status_transition: bool | None = None,
    ) -> dict[str, Any]:
        """Update account-level settings; unspecified fields retain their previous value."""
        if dnd_enabled is not None and dnd_enabled:
            _validate_dnd_times(dnd_start, dnd_end, require_both=True)
        elif dnd_enabled is not None and not dnd_enabled:
            # Turning off DND retains previous times but they may be partial.
            pass
        elif dnd_start is not None or dnd_end is not None:
            _validate_dnd_times(dnd_start, dnd_end, require_both=False)

        source_json = None
        if source_notifications is not None:
            source_json = json.dumps(
                {str(k): bool(v) for k, v in source_notifications.items()},
                ensure_ascii=False,
                sort_keys=True,
            )

        self._upsert(
            owner_account_id,
            "",
            dnd_enabled=dnd_enabled,
            dnd_start=dnd_start,
            dnd_end=dnd_end,
            source_notifications_json=source_json,
            auto_status_transition=auto_status_transition,
        )
        return self.get_account_settings(owner_account_id)

    # ------------------------------------------------------------------ #
    # Workspace-level settings
    # ------------------------------------------------------------------ #

    def get_workspace_settings(
        self,
        owner_account_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Return workspace-level Wiki ingestion settings."""
        row = self._select(owner_account_id, workspace_id)
        return _row_to_workspace_settings(row)

    def update_workspace_settings(
        self,
        owner_account_id: str,
        workspace_id: str,
        *,
        wiki_fulltext_indexing: bool | None = None,
        wiki_ingestion_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update workspace-level settings; validates workspace ownership."""
        if self._workspace_store is not None:
            self._workspace_store.get(workspace_id, owner_account_id=owner_account_id)
        self._upsert(
            owner_account_id,
            workspace_id,
            wiki_fulltext_indexing=wiki_fulltext_indexing,
            wiki_ingestion_enabled=wiki_ingestion_enabled,
        )
        return self.get_workspace_settings(owner_account_id, workspace_id)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _select(
        self, owner_account_id: str, workspace_id: str
    ) -> sqlite3.Row | None:
        owner = _required(owner_account_id, "owner_account_id")
        ws = str(workspace_id or "").strip()
        with self._lock:
            return self._conn.execute(
                """
                SELECT owner_account_id, workspace_id, dnd_enabled, dnd_start, dnd_end,
                       source_notifications_json, auto_status_transition,
                       wiki_fulltext_indexing,
                       wiki_ingestion_enabled, updated_at
                FROM work_settings
                WHERE owner_account_id = ? AND workspace_id = ?
                """,
                (owner, ws),
            ).fetchone()

    def _upsert(
        self,
        owner_account_id: str,
        workspace_id: str,
        **fields: Any,
    ) -> None:
        owner = _required(owner_account_id, "owner_account_id")
        ws = str(workspace_id or "").strip()
        current = self._select(owner, ws)

        merged: dict[str, Any] = {
            "dnd_enabled": 0 if current is None else current["dnd_enabled"],
            "dnd_start": None if current is None else current["dnd_start"],
            "dnd_end": None if current is None else current["dnd_end"],
            "source_notifications_json": "{}" if current is None else current["source_notifications_json"],
            "auto_status_transition": 0 if current is None else current["auto_status_transition"],
            "wiki_fulltext_indexing": 0 if current is None else current["wiki_fulltext_indexing"],
            "wiki_ingestion_enabled": 1 if current is None else current["wiki_ingestion_enabled"],
        }

        if "dnd_enabled" in fields and fields["dnd_enabled"] is not None:
            merged["dnd_enabled"] = int(bool(fields["dnd_enabled"]))
        if "dnd_start" in fields and fields["dnd_start"] is not None:
            merged["dnd_start"] = fields["dnd_start"]
        if "dnd_end" in fields and fields["dnd_end"] is not None:
            merged["dnd_end"] = fields["dnd_end"]
        if "source_notifications_json" in fields and fields["source_notifications_json"] is not None:
            merged["source_notifications_json"] = fields["source_notifications_json"]
        if "auto_status_transition" in fields and fields["auto_status_transition"] is not None:
            merged["auto_status_transition"] = int(bool(fields["auto_status_transition"]))
        if "wiki_fulltext_indexing" in fields and fields["wiki_fulltext_indexing"] is not None:
            merged["wiki_fulltext_indexing"] = int(bool(fields["wiki_fulltext_indexing"]))
        if "wiki_ingestion_enabled" in fields and fields["wiki_ingestion_enabled"] is not None:
            merged["wiki_ingestion_enabled"] = int(bool(fields["wiki_ingestion_enabled"]))

        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_settings (
                    owner_account_id, workspace_id, dnd_enabled, dnd_start, dnd_end,
                    source_notifications_json, auto_status_transition,
                    wiki_fulltext_indexing, wiki_ingestion_enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, workspace_id) DO UPDATE SET
                    dnd_enabled = excluded.dnd_enabled,
                    dnd_start = excluded.dnd_start,
                    dnd_end = excluded.dnd_end,
                    source_notifications_json = excluded.source_notifications_json,
                    auto_status_transition = excluded.auto_status_transition,
                    wiki_fulltext_indexing = excluded.wiki_fulltext_indexing,
                    wiki_ingestion_enabled = excluded.wiki_ingestion_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    owner,
                    ws,
                    merged["dnd_enabled"],
                    merged["dnd_start"],
                    merged["dnd_end"],
                    merged["source_notifications_json"],
                    merged["auto_status_transition"],
                    merged["wiki_fulltext_indexing"],
                    merged["wiki_ingestion_enabled"],
                    now,
                ),
            )
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _validate_dnd_times(
    dnd_start: str | None,
    dnd_end: str | None,
    *,
    require_both: bool,
) -> None:
    """Validate HH:MM format; when enabling DND, both times must be present."""
    if require_both:
        if not dnd_start:
            raise ValueError("dnd_start is required when dnd_enabled is true")
        if not dnd_end:
            raise ValueError("dnd_end is required when dnd_enabled is true")
    if dnd_start is not None and not _TIME_RE.match(dnd_start):
        raise ValueError(f"invalid dnd_start format: {dnd_start!r}")
    if dnd_end is not None and not _TIME_RE.match(dnd_end):
        raise ValueError(f"invalid dnd_end format: {dnd_end!r}")


def _row_to_account_settings(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {
            "dnd_enabled": False,
            "dnd_start": None,
            "dnd_end": None,
            "source_notifications": {},
            "auto_status_transition": False,
            "wiki_fulltext_indexing": False,
            "wiki_ingestion_enabled": True,
        }
    return {
        "dnd_enabled": bool(row["dnd_enabled"]),
        "dnd_start": row["dnd_start"],
        "dnd_end": row["dnd_end"],
        "source_notifications": json.loads(row["source_notifications_json"] or "{}"),
        "auto_status_transition": bool(row["auto_status_transition"]),
        "wiki_fulltext_indexing": False,
        "wiki_ingestion_enabled": True,
    }


def _row_to_workspace_settings(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {
            "dnd_enabled": False,
            "wiki_fulltext_indexing": False,
            "wiki_ingestion_enabled": True,
        }
    return {
        "dnd_enabled": bool(row["dnd_enabled"]),
        "wiki_fulltext_indexing": bool(row["wiki_fulltext_indexing"]),
        "wiki_ingestion_enabled": bool(row["wiki_ingestion_enabled"]),
    }


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
