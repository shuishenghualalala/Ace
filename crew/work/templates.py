"""Owner-scoped personal template store with three-layer aggregation."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

TemplateProvider = Callable[[], Sequence[dict[str, Any]]]


class TemplateSource(str, Enum):
    SYSTEM = "system"
    ORGANIZATION = "organization"
    PERSONAL = "personal"


@dataclass(frozen=True, slots=True)
class WorkTemplate:
    """One template from system, organization or personal storage."""

    owner_account_id: str
    template_id: str
    source: TemplateSource
    name: str
    description: str
    category: str
    blueprint: dict[str, Any]
    version: int
    usage_count: int
    last_used_at: float | None
    created_at: float
    updated_at: float


class WorkTemplateStore:
    """Persist personal templates and aggregate read-only system/org layers."""

    def __init__(
        self,
        db_path: str | Path = "crew_data/crew.db",
        *,
        system_provider: TemplateProvider | None = None,
        organization_provider: TemplateProvider | None = None,
        wal_enabled: bool = True,
    ) -> None:
        self._system_provider = system_provider
        self._organization_provider = organization_provider
        self._lock = threading.RLock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_templates (
                owner_account_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                blueprint_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                usage_count INTEGER NOT NULL DEFAULT 0,
                last_used_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_account_id, template_id)
            )
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_templates)")}
        if "usage_count" not in columns:
            conn.execute(
                "ALTER TABLE work_templates ADD COLUMN usage_count INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_work_templates_owner_recent
                ON work_templates(owner_account_id, last_used_at DESC)
                WHERE last_used_at IS NOT NULL
            """
        )

    def create(
        self,
        *,
        owner_account_id: str,
        name: str,
        description: str = "",
        category: str = "",
        blueprint: dict[str, Any] | None = None,
    ) -> WorkTemplate:
        owner = _required(owner_account_id, "owner_account_id")
        template_id = f"wt_{uuid.uuid4().hex}"
        now = time.time()
        bp_json = json.dumps(blueprint or {}, ensure_ascii=False, sort_keys=True)
        item = WorkTemplate(
            owner_account_id=owner,
            template_id=template_id,
            source=TemplateSource.PERSONAL,
            name=_required(name, "name"),
            description=str(description or "").strip(),
            category=str(category or "").strip(),
            blueprint=blueprint or {},
            version=1,
            usage_count=0,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO work_templates
                    (owner_account_id, template_id, name, description, category,
                     blueprint_json, version, usage_count, last_used_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (owner, template_id, item.name, item.description, item.category,
                 bp_json, 1, 0, None, now, now),
            )
        )
        return item

    def get(self, owner_account_id: str, template_id: str) -> WorkTemplate:
        owner = _required(owner_account_id, "owner_account_id")
        row = self._select(owner, template_id)
        if row is None:
            raise KeyError(f"Work template not found: {template_id}")
        return _row_to_template(row)

    def list_personal(self, owner_account_id: str) -> list[WorkTemplate]:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM work_templates
                WHERE owner_account_id = ?
                ORDER BY created_at, template_id
                """,
                (owner,),
            ).fetchall()
        return [_row_to_template(row) for row in rows]

    def list_recent(self, owner_account_id: str, *, limit: int = 10) -> list[WorkTemplate]:
        owner = _required(owner_account_id, "owner_account_id")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM work_templates
                WHERE owner_account_id = ? AND last_used_at IS NOT NULL
                ORDER BY last_used_at DESC, template_id
                LIMIT ?
                """,
                (owner, max(1, int(limit))),
            ).fetchall()
        return [_row_to_template(row) for row in rows]

    def update(
        self,
        owner_account_id: str,
        template_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        blueprint: dict[str, Any] | None = None,
    ) -> WorkTemplate:
        """Edit one owned personal template; system/org templates are read-only."""
        owner = _required(owner_account_id, "owner_account_id")
        try:
            current = self.get(owner, template_id)
        except KeyError:
            raise ValueError("only personal templates can be edited") from None
        now = time.time()
        new_name = name.strip() if name is not None else current.name
        if not new_name:
            raise ValueError("name cannot be empty")
        new_desc = description.strip() if description is not None else current.description
        new_category = category.strip() if category is not None else current.category
        bp_json = (
            json.dumps(blueprint, ensure_ascii=False, sort_keys=True)
            if blueprint is not None
            else json.dumps(current.blueprint, ensure_ascii=False, sort_keys=True)
        )
        new_version = current.version + 1 if blueprint is not None else current.version
        self._writer.execute(
            lambda conn: conn.execute(
                """
                UPDATE work_templates SET
                    name = ?, description = ?, category = ?,
                    blueprint_json = ?, version = ?, updated_at = ?
                WHERE owner_account_id = ? AND template_id = ?
                """,
                (new_name, new_desc, new_category, bp_json, new_version, now,
                 owner, template_id),
            )
        )
        return self.get(owner, template_id)

    def delete(self, owner_account_id: str, template_id: str) -> None:
        owner = _required(owner_account_id, "owner_account_id")
        resource = _required(template_id, "template_id")
        self._writer.execute(
            lambda conn: conn.execute(
                "DELETE FROM work_templates WHERE owner_account_id = ? AND template_id = ?",
                (owner, resource),
            )
        )

    def mark_used(self, owner_account_id: str, template_id: str) -> None:
        """Record a strictly ordered owner-local template use."""
        owner = _required(owner_account_id, "owner_account_id")
        def write(conn: sqlite3.Connection) -> None:
            latest = conn.execute(
                "SELECT MAX(last_used_at) FROM work_templates WHERE owner_account_id = ?",
                (owner,),
            ).fetchone()[0]
            used_at = max(time.time(), float(latest or 0) + 0.000001)
            conn.execute(
                """
                UPDATE work_templates SET last_used_at = ?, usage_count = usage_count + 1
                WHERE owner_account_id = ? AND template_id = ?
                """,
                (used_at, owner, template_id),
            )

        self._writer.execute(write)

    def aggregate(self, owner_account_id: str) -> list[WorkTemplate]:
        """Return all three layers: system (read-only), org (read-only), personal."""
        owner = _required(owner_account_id, "owner_account_id")
        result: list[WorkTemplate] = []

        if self._system_provider is not None:
            for raw in self._system_provider():
                result.append(_external_template(owner, raw, TemplateSource.SYSTEM))
        if self._organization_provider is not None:
            for raw in self._organization_provider():
                result.append(_external_template(owner, raw, TemplateSource.ORGANIZATION))

        result.extend(self.list_personal(owner))
        return result

    def _select(self, owner: str, template_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM work_templates WHERE owner_account_id = ? AND template_id = ?",
                (owner, template_id),
            ).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_template(row: sqlite3.Row) -> WorkTemplate:
    return WorkTemplate(
        owner_account_id=str(row["owner_account_id"]),
        template_id=str(row["template_id"]),
        source=TemplateSource.PERSONAL,
        name=str(row["name"]),
        description=str(row["description"]),
        category=str(row["category"]),
        blueprint=json.loads(row["blueprint_json"] or "{}"),
        version=int(row["version"]),
        usage_count=int(row["usage_count"]),
        last_used_at=float(row["last_used_at"]) if row["last_used_at"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _external_template(
    owner: str,
    raw: dict[str, Any],
    source: TemplateSource,
) -> WorkTemplate:
    return WorkTemplate(
        owner_account_id=owner,
        template_id=str(raw.get("template_id") or ""),
        source=source,
        name=str(raw.get("name") or ""),
        description=str(raw.get("description") or ""),
        category=str(raw.get("category") or ""),
        blueprint=raw.get("blueprint") or {},
        version=1,
        usage_count=max(0, int(raw.get("usage_count") or 0)),
        last_used_at=None,
        created_at=0.0,
        updated_at=0.0,
    )


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
