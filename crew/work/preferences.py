"""Owner-scoped work preferences, evidence threshold and global switch."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.work.models import OwnerKey

AUTO_ENABLE_SESSION_THRESHOLD = 2
MAX_EVIDENCE_SUMMARY_CHARS = 240


class PreferenceScope(str, Enum):
    GLOBAL = "global"
    ITEM_TYPE = "item_type"
    WORKSPACE = "workspace"
    SOURCE = "source"


class PreferenceStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class WorkPreferenceConflictError(RuntimeError):
    """Raised when an update targets a stale preference version."""


@dataclass(frozen=True, slots=True)
class WorkPreference:
    owner_account_id: str
    preference_id: str
    category: str
    content: str
    scope: PreferenceScope
    scope_id: str | None
    status: PreferenceStatus
    auto_enabled: bool
    evidence_session_count: int
    version: int
    created_at: float
    updated_at: float

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.preference_id)


class WorkPreferenceStore:
    """Persist editable preferences without storing complete conversation evidence."""

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
            CREATE TABLE IF NOT EXISTS work_preference_settings (
                owner_account_id TEXT PRIMARY KEY,
                auto_learning_enabled INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_preferences (
                owner_account_id TEXT NOT NULL,
                preference_id TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT,
                status TEXT NOT NULL,
                auto_enabled INTEGER NOT NULL,
                candidate_fingerprint TEXT,
                evidence_session_count INTEGER NOT NULL,
                version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, preference_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_preferences_owner_candidate
                ON work_preferences(owner_account_id, candidate_fingerprint)
                WHERE candidate_fingerprint IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_preferences_owner_active
                ON work_preferences(owner_account_id, status, category, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS work_preference_evidence (
                owner_account_id TEXT NOT NULL,
                candidate_fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                evidence_summary TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, candidate_fingerprint, session_id)
            )
            """,
        )
        for statement in statements:
            conn.execute(statement)

    def get_auto_learning_enabled(self, owner_account_id: str) -> bool:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT auto_learning_enabled FROM work_preference_settings
                WHERE owner_account_id = ?
                """,
                (owner,),
            ).fetchone()
        return True if row is None else bool(row["auto_learning_enabled"])

    def set_auto_learning_enabled(self, owner_account_id: str, enabled: bool) -> None:
        owner = _required(owner_account_id, "owner_account_id")
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_preference_settings (
                    owner_account_id, auto_learning_enabled, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(owner_account_id) DO UPDATE SET
                    auto_learning_enabled = excluded.auto_learning_enabled,
                    updated_at = excluded.updated_at
                """,
                (owner, int(bool(enabled)), time.time()),
            )
        )

    def create(
        self,
        *,
        owner_account_id: str,
        category: str,
        content: str,
        scope: PreferenceScope | str = PreferenceScope.GLOBAL,
        scope_id: str | None = None,
    ) -> WorkPreference:
        """Create one user-authored active preference."""
        owner = _required(owner_account_id, "owner_account_id")
        normalized_category = _required(category, "category")
        normalized_content = _required(content, "content")
        normalized_scope, normalized_scope_id = _scope(scope, scope_id)
        now = time.time()
        preference = WorkPreference(
            owner_account_id=owner,
            preference_id=f"work_pref_{uuid.uuid4().hex}",
            category=normalized_category,
            content=normalized_content,
            scope=normalized_scope,
            scope_id=normalized_scope_id,
            status=PreferenceStatus.ACTIVE,
            auto_enabled=False,
            evidence_session_count=0,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._writer.execute(
            lambda conn: conn.execute(
                f"INSERT INTO work_preferences ({', '.join(_PREFERENCE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _PREFERENCE_COLUMNS)})",
                _preference_values(preference, candidate_fingerprint=None),
            )
        )
        return preference

    def record_candidate(
        self,
        *,
        owner_account_id: str,
        session_id: str,
        category: str,
        content: str,
        evidence_summary: str,
        scope: PreferenceScope | str = PreferenceScope.GLOBAL,
        scope_id: str | None = None,
    ) -> WorkPreference | None:
        """Record one compact per-session proof and auto-enable at the threshold."""
        owner = _required(owner_account_id, "owner_account_id")
        session = _required(session_id, "session_id")
        normalized_category = _required(category, "category")
        normalized_content = _required(content, "content")
        normalized_scope, normalized_scope_id = _scope(scope, scope_id)
        if not self.get_auto_learning_enabled(owner):
            return None
        fingerprint = _candidate_fingerprint(
            normalized_category,
            normalized_content,
            normalized_scope,
            normalized_scope_id,
        )
        full_evidence = " ".join(str(evidence_summary or "").split())
        evidence_hash = hashlib.sha256(full_evidence.encode("utf-8")).hexdigest()
        compact_evidence = full_evidence[:MAX_EVIDENCE_SUMMARY_CHARS]

        def _write(conn: sqlite3.Connection) -> WorkPreference | None:
            if not _auto_learning_enabled(conn, owner):
                return None
            conn.execute(
                """
                INSERT OR IGNORE INTO work_preference_evidence (
                    owner_account_id, candidate_fingerprint, session_id,
                    evidence_hash, evidence_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    fingerprint,
                    session,
                    evidence_hash,
                    compact_evidence,
                    time.time(),
                ),
            )
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM work_preference_evidence
                    WHERE owner_account_id = ? AND candidate_fingerprint = ?
                    """,
                    (owner, fingerprint),
                ).fetchone()[0]
            )
            existing = conn.execute(
                f"SELECT {', '.join(_PREFERENCE_COLUMNS)} FROM work_preferences "
                "WHERE owner_account_id = ? AND candidate_fingerprint = ?",
                (owner, fingerprint),
            ).fetchone()
            if existing is not None:
                current = _row_to_preference(existing)
                if current.evidence_session_count != count:
                    updated_at = time.time()
                    conn.execute(
                        """
                        UPDATE work_preferences
                        SET evidence_session_count = ?, updated_at = ?
                        WHERE owner_account_id = ? AND preference_id = ?
                        """,
                        (count, updated_at, owner, current.preference_id),
                    )
                    return replace(
                        current,
                        evidence_session_count=count,
                        updated_at=updated_at,
                    )
                return current
            if count < AUTO_ENABLE_SESSION_THRESHOLD:
                return None
            now = time.time()
            preference = WorkPreference(
                owner_account_id=owner,
                preference_id=f"work_pref_{uuid.uuid4().hex}",
                category=normalized_category,
                content=normalized_content,
                scope=normalized_scope,
                scope_id=normalized_scope_id,
                status=PreferenceStatus.ACTIVE,
                auto_enabled=True,
                evidence_session_count=count,
                version=1,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                f"INSERT INTO work_preferences ({', '.join(_PREFERENCE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _PREFERENCE_COLUMNS)})",
                _preference_values(preference, candidate_fingerprint=fingerprint),
            )
            return preference

        return self._writer.execute(_write)

    def list(self, owner_account_id: str) -> list[WorkPreference]:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_PREFERENCE_COLUMNS)} FROM work_preferences "
                "WHERE owner_account_id = ? ORDER BY created_at, rowid",
                (owner,),
            ).fetchall()
        return [_row_to_preference(row) for row in rows]

    def list_applicable(
        self,
        owner_account_id: str,
        *,
        category: str | None = None,
        workspace_id: str | None = None,
        item_type: str | None = None,
        source_key: str | None = None,
    ) -> list[WorkPreference]:
        """Return active preferences whose explicit scope matches the request."""
        owner = _required(owner_account_id, "owner_account_id")
        if not self.get_auto_learning_enabled(owner):
            return []
        wanted_category = _required(category, "category") if category is not None else None
        values = {
            PreferenceScope.WORKSPACE: str(workspace_id or "").strip(),
            PreferenceScope.ITEM_TYPE: str(item_type or "").strip(),
            PreferenceScope.SOURCE: str(source_key or "").strip(),
        }
        return [
            preference
            for preference in self.list(owner)
            if preference.status is PreferenceStatus.ACTIVE
            and (
                wanted_category is None
                or preference.category in {wanted_category, "general"}
            )
            and (
                preference.scope is PreferenceScope.GLOBAL
                or preference.scope_id == values[preference.scope]
            )
        ]

    def update(
        self,
        owner_account_id: str,
        preference_id: str,
        *,
        expected_version: int,
        content: str | None = None,
        scope: PreferenceScope | str | None = None,
        scope_id: str | None = None,
        status: PreferenceStatus | str | None = None,
    ) -> WorkPreference:
        """Edit, pause or resume one owned preference with optimistic versioning."""
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(preference_id, "preference_id")

        def _write(conn: sqlite3.Connection) -> WorkPreference:
            row = conn.execute(
                f"SELECT {', '.join(_PREFERENCE_COLUMNS)} FROM work_preferences "
                "WHERE owner_account_id = ? AND preference_id = ?",
                (owner, resource),
            ).fetchone()
            if row is None:
                raise KeyError(f"Work preference not found: {resource}")
            current = _row_to_preference(row)
            if current.version != expected_version:
                raise WorkPreferenceConflictError(
                    f"preference version changed: expected {expected_version}, "
                    f"actual {current.version}"
                )
            next_content = current.content if content is None else _required(content, "content")
            if scope is None:
                next_scope = current.scope
                next_scope_id = current.scope_id if scope_id is None else scope_id
            else:
                next_scope = PreferenceScope(scope)
                next_scope_id = scope_id
            next_scope, next_scope_id = _scope(next_scope, next_scope_id)
            next_status = current.status if status is None else PreferenceStatus(status)
            edited_definition = (
                next_content != current.content
                or next_scope is not current.scope
                or next_scope_id != current.scope_id
            )
            updated = WorkPreference(
                owner_account_id=owner,
                preference_id=resource,
                category=current.category,
                content=next_content,
                scope=next_scope,
                scope_id=next_scope_id,
                status=next_status,
                auto_enabled=False if edited_definition else current.auto_enabled,
                evidence_session_count=current.evidence_session_count,
                version=current.version + 1,
                created_at=current.created_at,
                updated_at=time.time(),
            )
            conn.execute(
                """
                UPDATE work_preferences SET
                    content = ?, scope = ?, scope_id = ?, status = ?,
                    auto_enabled = ?, candidate_fingerprint = ?,
                    evidence_session_count = ?, version = ?, updated_at = ?
                WHERE owner_account_id = ? AND preference_id = ? AND version = ?
                """,
                (
                    updated.content,
                    updated.scope.value,
                    updated.scope_id,
                    updated.status.value,
                    int(updated.auto_enabled),
                    None if edited_definition else row["candidate_fingerprint"],
                    updated.evidence_session_count,
                    updated.version,
                    updated.updated_at,
                    owner,
                    resource,
                    expected_version,
                ),
            )
            return updated

        return self._writer.execute(_write)

    def delete(
        self,
        owner_account_id: str,
        preference_id: str,
        *,
        expected_version: int,
    ) -> None:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(preference_id, "preference_id")

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                """
                SELECT version, candidate_fingerprint FROM work_preferences
                WHERE owner_account_id = ? AND preference_id = ?
                """,
                (owner, resource),
            ).fetchone()
            if row is None:
                raise KeyError(f"Work preference not found: {resource}")
            if int(row["version"]) != expected_version:
                raise WorkPreferenceConflictError("preference version changed")
            conn.execute(
                "DELETE FROM work_preferences "
                "WHERE owner_account_id = ? AND preference_id = ?",
                (owner, resource),
            )
            if row["candidate_fingerprint"] is not None:
                conn.execute(
                    """
                    DELETE FROM work_preference_evidence
                    WHERE owner_account_id = ? AND candidate_fingerprint = ?
                    """,
                    (owner, row["candidate_fingerprint"]),
                )

        self._writer.execute(_write)

    def evidence_count(self, owner_account_id: str) -> int:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            return int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM work_preference_evidence
                    WHERE owner_account_id = ?
                    """,
                    (owner,),
                ).fetchone()[0]
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_PREFERENCE_COLUMNS = (
    "owner_account_id",
    "preference_id",
    "category",
    "content",
    "scope",
    "scope_id",
    "status",
    "auto_enabled",
    "candidate_fingerprint",
    "evidence_session_count",
    "version",
    "created_at",
    "updated_at",
)


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _scope(
    scope: PreferenceScope | str,
    scope_id: str | None,
) -> tuple[PreferenceScope, str | None]:
    normalized_scope = PreferenceScope(scope)
    normalized_id = str(scope_id or "").strip() or None
    if normalized_scope is PreferenceScope.GLOBAL:
        if normalized_id is not None:
            raise ValueError("global preference cannot have scope_id")
    elif normalized_id is None:
        raise ValueError(f"{normalized_scope.value} preference requires scope_id")
    return normalized_scope, normalized_id


def _candidate_fingerprint(
    category: str,
    content: str,
    scope: PreferenceScope,
    scope_id: str | None,
) -> str:
    payload = json.dumps(
        {
            "category": category.casefold(),
            "content": " ".join(content.split()).casefold(),
            "scope": scope.value,
            "scope_id": scope_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _auto_learning_enabled(conn: sqlite3.Connection, owner_account_id: str) -> bool:
    row = conn.execute(
        """
        SELECT auto_learning_enabled FROM work_preference_settings
        WHERE owner_account_id = ?
        """,
        (owner_account_id,),
    ).fetchone()
    return True if row is None else bool(row["auto_learning_enabled"])


def _preference_values(
    preference: WorkPreference,
    *,
    candidate_fingerprint: str | None,
) -> tuple[Any, ...]:
    return (
        preference.owner_account_id,
        preference.preference_id,
        preference.category,
        preference.content,
        preference.scope.value,
        preference.scope_id,
        preference.status.value,
        int(preference.auto_enabled),
        candidate_fingerprint,
        preference.evidence_session_count,
        preference.version,
        preference.created_at,
        preference.updated_at,
    )


def _row_to_preference(row: sqlite3.Row) -> WorkPreference:
    return WorkPreference(
        owner_account_id=str(row["owner_account_id"]),
        preference_id=str(row["preference_id"]),
        category=str(row["category"]),
        content=str(row["content"]),
        scope=PreferenceScope(str(row["scope"])),
        scope_id=row["scope_id"],
        status=PreferenceStatus(str(row["status"])),
        auto_enabled=bool(row["auto_enabled"]),
        evidence_session_count=int(row["evidence_session_count"]),
        version=int(row["version"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
