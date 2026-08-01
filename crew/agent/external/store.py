"""SQLite storage for external runtimes and agents."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from crew.agent.external.runtime_profile import runtime_model_migrations
from crew.state._migration import rebuild_table_pk
from crew.team.capabilities import AGENT_PROFILE_VERSION, normalize_capabilities
from crew.team.roles import (
    CREW_BUILTIN_AGENT_ID,
    crew_builtin_agent_public,
    infer_role_key,
    is_crew_builtin_agent,
    role_preset,
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ExternalAgentStore:
    """Small additive schema that leaves existing Crew tables untouched."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path).expanduser())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_runtime (
                  id TEXT PRIMARY KEY,
                  provider TEXT NOT NULL,
                  name TEXT NOT NULL,
                  executable_path TEXT NOT NULL,
                  version TEXT,
                  protocol TEXT NOT NULL DEFAULT 'acp',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  last_seen_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_agent (
                  id TEXT PRIMARY KEY,
                  owner_account_id TEXT NOT NULL DEFAULT '',
                  name TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  runtime_id TEXT NOT NULL,
                  model TEXT,
                  system_prompt TEXT NOT NULL DEFAULT '',
                  custom_args_json TEXT NOT NULL DEFAULT '[]',
                  custom_env_json TEXT NOT NULL DEFAULT '{}',
                  profile_json TEXT NOT NULL DEFAULT '{}',
                  profile_version INTEGER NOT NULL DEFAULT 2,
                  profile_updated_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(runtime_id) REFERENCES external_runtime(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_team (
                  id TEXT PRIMARY KEY,
                  owner_account_id TEXT NOT NULL DEFAULT '',
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  leader_agent_id TEXT NOT NULL,
                  instructions TEXT NOT NULL DEFAULT '',
                  team_spec_json TEXT NOT NULL DEFAULT '{}',
                  formation_plan_json TEXT NOT NULL DEFAULT '{}',
                  archived_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(leader_agent_id) REFERENCES external_agent(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_team_member (
                  id TEXT PRIMARY KEY,
                  team_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT '',
                  role_key TEXT NOT NULL DEFAULT '',
                  role_label TEXT NOT NULL DEFAULT '',
                  capabilities_json TEXT NOT NULL DEFAULT '[]',
                  workflow_lane TEXT NOT NULL DEFAULT '',
                  sort_order INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(team_id) REFERENCES external_team(id),
                  FOREIGN KEY(agent_id) REFERENCES external_agent(id),
                  UNIQUE(team_id, agent_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_runtime_session_binding (
                  owner_account_id TEXT NOT NULL DEFAULT '',
                  crew_session_id TEXT NOT NULL,
                  external_agent_id TEXT NOT NULL,
                  runtime_id TEXT NOT NULL,
                  adapter_id TEXT NOT NULL,
                  cwd TEXT NOT NULL DEFAULT '',
                  native_session_id TEXT NOT NULL,
                  session_profile TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd),
                  FOREIGN KEY(external_agent_id) REFERENCES external_agent(id),
                  FOREIGN KEY(runtime_id) REFERENCES external_runtime(id)
                )
                """
            )
            self._ensure_column(
                conn,
                "external_runtime_session_binding",
                "session_profile",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(conn, "external_team_member", "role_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "external_team_member", "role_label", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "external_team_member", "capabilities_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "external_team_member", "workflow_lane", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "external_team", "team_spec_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "external_team", "formation_plan_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "external_agent", "profile_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(
                conn,
                "external_agent",
                "profile_version",
                f"INTEGER NOT NULL DEFAULT {AGENT_PROFILE_VERSION}",
            )
            self._ensure_column(conn, "external_agent", "profile_updated_at", "TEXT")
            self._ensure_column(conn, "external_agent", "owner_account_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "external_team", "owner_account_id", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_external_agent_owner ON external_agent(owner_account_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_external_team_owner ON external_team(owner_account_id, archived_at, created_at)"
            )
            self._migrate_embedded_formation_plans(conn)
            self._drop_column_if_exists(conn, "external_runtime", "status")
            self._migrate_legacy_acp_bindings(conn)
        self._backfill_agent_profiles()

    @staticmethod
    def _migrate_embedded_formation_plans(conn: sqlite3.Connection) -> None:
        """Move the legacy TeamSpec.formation payload into its own snapshot."""

        rows = conn.execute(
            "SELECT id, leader_agent_id, team_spec_json, formation_plan_json FROM external_team"
        ).fetchall()
        for row in rows:
            try:
                spec = json.loads(str(row["team_spec_json"] or "{}"))
                current_plan = json.loads(str(row["formation_plan_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            legacy = spec.pop("formation", None) if isinstance(spec, dict) else None
            if not isinstance(legacy, dict):
                continue
            if isinstance(current_plan, dict) and current_plan:
                conn.execute(
                    "UPDATE external_team SET team_spec_json = ? WHERE id = ?",
                    (json.dumps(spec, ensure_ascii=False), row["id"]),
                )
                continue
            assignment_by_agent = {
                str(item.get("agent_id") or ""): item
                for item in (legacy.get("assignments") or [])
                if isinstance(item, dict)
            }
            member_rows = conn.execute(
                "SELECT * FROM external_team_member WHERE team_id = ? ORDER BY sort_order ASC, created_at ASC",
                (row["id"],),
            ).fetchall()
            members: list[dict[str, Any]] = []
            covered: list[str] = []
            for member in member_rows:
                try:
                    assigned = json.loads(str(member["capabilities_json"] or "[]"))
                except json.JSONDecodeError:
                    assigned = []
                assigned = [str(item) for item in assigned if str(item)] if isinstance(assigned, list) else []
                covered.extend(assigned)
                assignment = assignment_by_agent.get(str(member["agent_id"]), {})
                members.append({
                    "agent_id": str(member["agent_id"]),
                    "role_key": str(member["role_key"] or ""),
                    "role_label": str(member["role_label"] or ""),
                    "assigned_capabilities": assigned,
                    "responsibility": {},
                    "responsibility_markdown": str(member["role"] or ""),
                    "selection_source": str(assignment.get("source") or "legacy"),
                    "locked": bool(assignment.get("locked")),
                    "selection_reason": "从旧 TeamSpec.formation 迁移。",
                })
            required = [str(item) for item in (legacy.get("required_capabilities") or []) if str(item)]
            covered_required = list(dict.fromkeys(item for item in covered if item in required))
            uncovered = [item for item in required if item not in covered_required]
            try:
                legacy_confidence = float(legacy.get("confidence") or 0.5)
            except (TypeError, ValueError):
                legacy_confidence = 0.5
            plan = {
                "version": 1,
                "leader_agent_id": str(legacy.get("leader_agent_id") or row["leader_agent_id"] or ""),
                "members": members,
                "coverage": {"required": required, "covered": covered_required, "uncovered": uncovered},
                "confidence": {
                    "requirement": legacy_confidence,
                    "capability_evidence": 0.15,
                    "coverage": (len(covered_required) / len(required)) if required else 1.0,
                    "overall": legacy_confidence,
                },
                "staffing_mode": str(legacy.get("staffing_mode") or "legacy"),
                "excluded_agent_ids": list(legacy.get("excluded_agents") or []),
                "reasons": ["从旧 TeamSpec.formation 迁移。"],
                "warnings": list(legacy.get("unresolved") or []),
            }
            conn.execute(
                "UPDATE external_team SET team_spec_json = ?, formation_plan_json = ? WHERE id = ?",
                (json.dumps(spec, ensure_ascii=False), json.dumps(plan, ensure_ascii=False), row["id"]),
            )

    def _backfill_agent_profiles(self) -> None:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, owner_account_id
                FROM external_agent
                WHERE profile_json IS NULL
                   OR profile_json = ''
                   OR profile_json = '{}'
                   OR profile_version < ?
                """,
                (AGENT_PROFILE_VERSION,),
            ).fetchall()
        for row in rows:
            try:
                self.refresh_agent_profile(
                    str(row["id"]),
                    owner_account_id=str(row["owner_account_id"] or ""),
                )
            except KeyError:
                continue

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _drop_column_if_exists(conn: sqlite3.Connection, table: str, column: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            return
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except sqlite3.OperationalError:
            # Older SQLite builds cannot drop columns; in that case the field
            # remains ignored by runtime serialization and new writes.
            pass

    def _ensure_acp_binding_owner_schema(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "external_acp_session_binding", "owner_account_id", "TEXT NOT NULL DEFAULT ''")
        rebuild_table_pk(
            conn,
            table="external_acp_session_binding",
            expected_pk=[
                "owner_account_id",
                "crew_session_id",
                "external_agent_id",
                "runtime_id",
                "provider",
                "cwd",
            ],
            new_ddl="""
                CREATE TABLE external_acp_session_binding_new (
                  owner_account_id TEXT NOT NULL DEFAULT '',
                  crew_session_id TEXT NOT NULL,
                  external_agent_id TEXT NOT NULL,
                  runtime_id TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  cwd TEXT NOT NULL DEFAULT '',
                  acp_session_id TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (owner_account_id, crew_session_id, external_agent_id, runtime_id, provider, cwd),
                  FOREIGN KEY(external_agent_id) REFERENCES external_agent(id),
                  FOREIGN KEY(runtime_id) REFERENCES external_runtime(id)
                )
            """,
            copy_sql="""
                INSERT OR IGNORE INTO external_acp_session_binding_new (
                  owner_account_id, crew_session_id, external_agent_id, runtime_id, provider, cwd,
                  acp_session_id, status, created_at, updated_at
                )
                SELECT owner_account_id, crew_session_id, external_agent_id, runtime_id, provider, cwd,
                       acp_session_id, status, created_at, updated_at
                FROM external_acp_session_binding
            """,
        )

    def _migrate_legacy_acp_bindings(self, conn: sqlite3.Connection) -> None:
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'external_acp_session_binding'"
        ).fetchone()
        if legacy is None:
            return
        self._ensure_acp_binding_owner_schema(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO external_runtime_session_binding (
              owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd,
              native_session_id, status, created_at, updated_at
            )
            SELECT owner_account_id, crew_session_id, external_agent_id, runtime_id, 'acp-stdio', cwd,
                   acp_session_id, status, created_at, updated_at
            FROM external_acp_session_binding
            """
        )
        conn.execute("DROP TABLE external_acp_session_binding")

    def upsert_runtime(self, runtime: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        rid = str(runtime["id"])
        provider = str(runtime.get("provider") or "").strip() or "external"
        name = str(runtime.get("name") or "").strip() or "外援"
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT created_at, metadata_json FROM external_runtime WHERE id = ?",
                (rid,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            metadata = dict(runtime.get("metadata", {}))
            if existing:
                try:
                    previous = json.loads(existing["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    previous = {}
                if (
                    "replaces_runtime_ids" not in metadata
                    and isinstance(previous.get("replaces_runtime_ids"), list)
                ):
                    metadata["replaces_runtime_ids"] = previous["replaces_runtime_ids"]
                if metadata.get("availability_status") != "ready":
                    if not metadata.get("models") and isinstance(previous.get("models"), list):
                        metadata["models"] = previous["models"]
                    if not metadata.get("default_model_id"):
                        metadata["default_model_id"] = previous.get("default_model_id", "")
                    if (
                        not isinstance(metadata.get("model_migrations"), dict)
                        and isinstance(previous.get("model_migrations"), dict)
                    ):
                        metadata["model_migrations"] = previous["model_migrations"]
                    current_probe = metadata.get("probe") if isinstance(metadata.get("probe"), dict) else {}
                    previous_probe = previous.get("probe") if isinstance(previous.get("probe"), dict) else {}
                    if not current_probe.get("last_success_at") and previous_probe.get("last_success_at"):
                        current_probe["last_success_at"] = previous_probe["last_success_at"]
                    metadata["probe"] = current_probe
            conn.execute(
                """
                INSERT OR REPLACE INTO external_runtime (
                  id, provider, name, executable_path, version, protocol,
                  metadata_json, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    provider,
                    name,
                    runtime.get("executable_path", ""),
                    runtime.get("version", "unknown"),
                    runtime.get("protocol", "acp"),
                    json.dumps(metadata, ensure_ascii=False),
                    created_at,
                    now,
                    now,
                ),
            )
        stored = self.get_runtime(rid)
        self._refresh_profiles_for_runtime(rid, stored)
        return stored

    @staticmethod
    def _runtime_descriptor_id(runtime: dict[str, Any]) -> str:
        metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
        descriptor_id = str(metadata.get("descriptor_id") or "").strip()
        if descriptor_id:
            return descriptor_id
        source = str(metadata.get("runtime_descriptor_source") or "").strip()
        provider = str(runtime.get("provider") or "").strip()
        return f"{source}:{provider}" if source and provider else ""

    def _replace_runtime(
        self,
        previous: dict[str, Any],
        replacement: dict[str, Any],
    ) -> None:
        """Move stable Agent identities to a replacement installation atomically."""

        previous_id = str(previous.get("id") or "")
        replacement_id = str(replacement.get("id") or "")
        if not previous_id or not replacement_id or previous_id == replacement_id:
            return

        now = _now()
        previous_metadata = dict(previous.get("metadata") or {})
        previous_probe = dict(previous_metadata.get("probe") or {})
        previous_probe.update({
            "error_code": "executable_replaced",
            "message": "运行时安装路径已变化，现有智能体已迁移到新路径",
            "checked_at": now,
        })
        previous_metadata.update({
            "availability_status": "unavailable",
            "lifecycle_status": "replaced",
            "replaced_by_runtime_id": replacement_id,
            "replacement_reason": "executable_path_changed",
            "replaced_at": now,
            "probe": previous_probe,
        })

        replacement_metadata = dict(replacement.get("metadata") or {})
        raw_replaced_ids = replacement_metadata.get("replaces_runtime_ids")
        replaced_ids = [
            str(item)
            for item in raw_replaced_ids if str(item)
        ] if isinstance(raw_replaced_ids, list) else []
        if previous_id not in replaced_ids:
            replaced_ids.append(previous_id)
        replacement_metadata["replaces_runtime_ids"] = replaced_ids

        with self._conn() as conn:
            conn.execute(
                "UPDATE external_runtime SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(previous_metadata, ensure_ascii=False), now, previous_id),
            )
            conn.execute(
                "UPDATE external_runtime SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(replacement_metadata, ensure_ascii=False), now, replacement_id),
            )
            conn.execute(
                """
                UPDATE external_agent
                SET runtime_id = ?, provider = ?, updated_at = ?
                WHERE runtime_id = ?
                """,
                (
                    replacement_id,
                    str(replacement.get("provider") or previous.get("provider") or ""),
                    now,
                    previous_id,
                ),
            )
            # Native sessions are tied to the old executable instance.  Agent
            # and Team identities survive, but the next turn must start a fresh
            # protocol session against the replacement runtime.
            conn.execute(
                "DELETE FROM external_runtime_session_binding WHERE runtime_id = ?",
                (previous_id,),
            )

        self._refresh_profiles_for_runtime(
            replacement_id,
            self.get_runtime(replacement_id),
        )

    def sync_runtimes(self, runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist a discovery snapshot and retain missing runtimes as unavailable."""

        detected_ids = {str(runtime.get("id") or "") for runtime in runtimes}
        for runtime in runtimes:
            self.upsert_runtime(runtime)
        detected_by_descriptor: dict[str, list[dict[str, Any]]] = {}
        for runtime_id in detected_ids:
            if not runtime_id:
                continue
            stored = self.get_runtime(runtime_id)
            descriptor_id = self._runtime_descriptor_id(stored)
            if descriptor_id:
                detected_by_descriptor.setdefault(descriptor_id, []).append(stored)
        detected_by_provider: dict[str, list[dict[str, Any]]] = {}
        for runtimes_for_descriptor in detected_by_descriptor.values():
            for stored in runtimes_for_descriptor:
                provider = str(stored.get("provider") or "").strip()
                if provider:
                    detected_by_provider.setdefault(provider, []).append(stored)
        for existing in self.list_runtimes():
            runtime_id = str(existing.get("id") or "")
            if runtime_id in detected_ids:
                continue
            metadata = dict(existing.get("metadata") or {})
            # Only discovery-managed runtimes participate in snapshot removal.
            # A manually registered/custom runtime may be intentionally absent
            # from the built-in detector catalog and must not be disabled here.
            if not metadata.get("runtime_profile_version"):
                continue
            if metadata.get("lifecycle_status") == "replaced":
                continue
            descriptor_id = self._runtime_descriptor_id(existing)
            replacements = detected_by_descriptor.get(descriptor_id, [])
            if not descriptor_id:
                # Early discovery snapshots did not persist descriptor_id/source.
                # They are still detector-owned (runtime_profile_version above),
                # so a single current built-in candidate from the same provider
                # is the unambiguous successor even when its capability probe is
                # temporarily degraded.  The executable path has already been
                # resolved; readiness must not leave a duplicate stale card.
                provider = str(existing.get("provider") or "").strip()
                replacements = detected_by_provider.get(provider, [])
            if len(replacements) == 1:
                self._replace_runtime(existing, replacements[0])
                continue
            metadata["availability_status"] = "unavailable"
            metadata["lifecycle_status"] = "missing"
            probe = dict(metadata.get("probe") or {})
            probe.update({
                "error_code": "executable_missing",
                "message": "未找到运行时可执行文件",
                "checked_at": _now(),
            })
            metadata["probe"] = probe
            with self._conn() as conn:
                conn.execute(
                    "UPDATE external_runtime SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), _now(), runtime_id),
                )
            self._refresh_profiles_for_runtime(runtime_id, self.get_runtime(runtime_id))
        return self.list_runtimes()

    def _refresh_profiles_for_runtime(self, runtime_id: str, runtime: dict[str, Any] | None = None) -> None:
        """Refresh current AgentProfile snapshots after Runtime facts change."""

        runtime_payload = runtime or self.get_runtime(runtime_id)
        metadata = runtime_payload.get("metadata") if isinstance(runtime_payload.get("metadata"), dict) else {}
        default_model = str(metadata.get("default_model_id") or "").strip()
        model_migrations = runtime_model_migrations(runtime_payload)
        if model_migrations:
            with self._conn() as conn:
                for source, target in model_migrations.items():
                    conn.execute(
                        """
                        UPDATE external_agent
                        SET model = ?, updated_at = ?
                        WHERE runtime_id = ? AND model = ?
                        """,
                        (target, _now(), runtime_id, source),
                    )
        if metadata.get("availability_status") == "ready" and default_model:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE external_agent SET model = ?, updated_at = ? WHERE runtime_id = ? AND COALESCE(model, '') = ''",
                    (default_model, _now(), runtime_id),
                )
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM external_agent WHERE runtime_id = ?", (runtime_id,)).fetchall()
        for row in rows:
            self.refresh_agent_profile(
                str(row["id"]),
                runtime=runtime_payload,
                owner_account_id=str(row["owner_account_id"] or ""),
            )

    def refresh_agent_profile(
        self,
        agent_id: str,
        *,
        runtime: dict[str, Any] | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """Rebuild and persist the current AgentProfile snapshot."""

        from crew.team.formation import build_agent_profile

        agent = self.get_agent(agent_id, owner_account_id=owner_account_id)
        runtime_payload = runtime or self.get_runtime(str(agent.get("runtime_id") or ""))
        profile = build_agent_profile(agent, runtime=runtime_payload).to_dict()
        if agent.get("profile") == profile:
            return agent
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE external_agent
                SET profile_json = ?, profile_version = ?, profile_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(profile, ensure_ascii=False),
                    int(profile.get("version") or 1),
                    now,
                    now,
                    agent_id,
                ),
            )
        return self.get_agent(agent_id, owner_account_id=owner_account_id)

    def list_runtimes(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM external_runtime ORDER BY updated_at DESC").fetchall()
        return [self._runtime_dict(row) for row in rows]

    def get_runtime(self, runtime_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM external_runtime WHERE id = ?", (runtime_id,)).fetchone()
        if row is None:
            raise KeyError(runtime_id)
        return self._runtime_dict(row)

    def create_agent(
        self,
        *,
        owner_account_id: str = "",
        name: str,
        runtime_id: str,
        model: str = "",
        system_prompt: str = "",
        custom_args: list[str] | None = None,
        custom_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        runtime = self.get_runtime(runtime_id)
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO external_agent (
                  id, owner_account_id, name, provider, runtime_id, model, system_prompt,
                  custom_args_json, custom_env_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    owner_account_id,
                    name,
                    runtime["provider"],
                    runtime_id,
                    model,
                    system_prompt,
                    json.dumps(custom_args or [], ensure_ascii=False),
                    json.dumps(custom_env or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.refresh_agent_profile(
            agent_id,
            runtime=runtime,
            owner_account_id=owner_account_id,
        )

    def list_agents(self, *, owner_account_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM external_agent WHERE owner_account_id = ? ORDER BY created_at DESC",
                (owner_account_id,),
            ).fetchall()
        return [self._agent_dict(row) for row in rows]

    def get_agent(self, agent_id: str, *, owner_account_id: str = "") -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM external_agent WHERE id = ? AND owner_account_id = ?",
                (agent_id, owner_account_id),
            ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        return self._agent_dict(row)

    def delete_agent(self, agent_id: str, *, owner_account_id: str = "") -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM external_agent WHERE id = ? AND owner_account_id = ?",
                (agent_id, owner_account_id),
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            used_by_team = conn.execute(
                """
                SELECT 1
                FROM external_team t
                LEFT JOIN external_team_member tm ON tm.team_id = t.id
                WHERE t.archived_at IS NULL
                  AND t.owner_account_id = ?
                  AND (t.leader_agent_id = ? OR tm.agent_id = ?)
                LIMIT 1
                """,
                (owner_account_id, agent_id, agent_id),
            ).fetchone()
            if used_by_team is not None:
                raise ValueError("智能体已在团队中，暂不能删除")
            conn.execute(
                "DELETE FROM external_agent WHERE id = ? AND owner_account_id = ?",
                (agent_id, owner_account_id),
            )

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        agent = self.get_agent(agent_id, owner_account_id=owner_account_id)
        runtime = self.get_runtime(agent["runtime_id"])
        return agent, runtime

    def get_runtime_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM external_runtime_session_binding
                WHERE owner_account_id = ?
                  AND crew_session_id = ?
                  AND external_agent_id = ?
                  AND runtime_id = ?
                  AND adapter_id = ?
                  AND cwd = ?
                """,
                (owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_runtime_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        native_session_id: str,
        cwd: str = "",
        session_profile: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        now = _now()
        with self._conn() as conn:
            existing = conn.execute(
                """
                SELECT created_at, session_profile
                FROM external_runtime_session_binding
                WHERE owner_account_id = ?
                  AND crew_session_id = ?
                  AND external_agent_id = ?
                  AND runtime_id = ?
                  AND adapter_id = ?
                  AND cwd = ?
                """,
                (owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            resolved_profile = (
                str(session_profile)
                if session_profile is not None
                else str(existing["session_profile"] or "") if existing else ""
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO external_runtime_session_binding (
                  owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd,
                  native_session_id, session_profile, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_account_id,
                    crew_session_id,
                    external_agent_id,
                    runtime_id,
                    adapter_id,
                    cwd,
                    native_session_id,
                    resolved_profile,
                    status,
                    created_at,
                    now,
                ),
            )
        binding = self.get_runtime_session_binding(
            owner_account_id=owner_account_id,
            crew_session_id=crew_session_id,
            external_agent_id=external_agent_id,
            runtime_id=runtime_id,
            adapter_id=adapter_id,
            cwd=cwd,
        )
        if binding is None:  # pragma: no cover - SQLite write failure would raise first
            raise RuntimeError("保存外部 Runtime 会话绑定失败")
        return binding

    def delete_runtime_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                DELETE FROM external_runtime_session_binding
                WHERE owner_account_id = ?
                  AND crew_session_id = ?
                  AND external_agent_id = ?
                  AND runtime_id = ?
                  AND adapter_id = ?
                  AND cwd = ?
                """,
                (owner_account_id, crew_session_id, external_agent_id, runtime_id, adapter_id, cwd),
            )

    def delete_runtime_bindings_for_session(
        self,
        crew_session_id: str,
        *,
        owner_account_id: str = "",
    ) -> int:
        """删除某 Crew 会话下全部外部 Runtime 绑定行，返回删除行数。"""
        sid = str(crew_session_id or "").strip()
        if not sid:
            return 0
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM external_runtime_session_binding WHERE owner_account_id = ? AND crew_session_id = ?",
                (owner_account_id, sid),
            )
            return int(cur.rowcount)

    # Compatibility methods keep existing callers stable while all protocols
    # now persist through the protocol-neutral table.
    def get_acp_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        provider: str,
        cwd: str = "",
    ) -> dict[str, Any] | None:
        del provider
        binding = self.get_runtime_session_binding(
            owner_account_id=owner_account_id,
            crew_session_id=crew_session_id,
            external_agent_id=external_agent_id,
            runtime_id=runtime_id,
            adapter_id="acp-stdio",
            cwd=cwd,
        )
        if binding is None:
            return None
        return {**binding, "acp_session_id": binding["native_session_id"]}

    def save_acp_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        provider: str,
        acp_session_id: str,
        cwd: str = "",
        status: str = "active",
    ) -> dict[str, Any]:
        del provider
        binding = self.save_runtime_session_binding(
            owner_account_id=owner_account_id,
            crew_session_id=crew_session_id,
            external_agent_id=external_agent_id,
            runtime_id=runtime_id,
            adapter_id="acp-stdio",
            native_session_id=acp_session_id,
            cwd=cwd,
            status=status,
        )
        return {**binding, "acp_session_id": binding["native_session_id"]}

    def delete_acp_session_binding(
        self,
        *,
        owner_account_id: str = "",
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        provider: str,
        cwd: str = "",
    ) -> None:
        del provider
        self.delete_runtime_session_binding(
            owner_account_id=owner_account_id,
            crew_session_id=crew_session_id,
            external_agent_id=external_agent_id,
            runtime_id=runtime_id,
            adapter_id="acp-stdio",
            cwd=cwd,
        )

    def delete_acp_bindings_for_session(
        self,
        crew_session_id: str,
        *,
        owner_account_id: str = "",
    ) -> int:
        return self.delete_runtime_bindings_for_session(
            crew_session_id,
            owner_account_id=owner_account_id,
        )

    def create_team(
        self,
        *,
        owner_account_id: str = "",
        name: str,
        leader_agent_id: str,
        members: list[dict[str, Any]],
        description: str = "",
        instructions: str = "",
        team_spec: dict[str, Any] | None = None,
        formation_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        leader_agent_id = str(leader_agent_id or "").strip() or CREW_BUILTIN_AGENT_ID
        crew_builtin_leader = is_crew_builtin_agent(leader_agent_id)
        if not crew_builtin_leader:
            self.get_agent(leader_agent_id, owner_account_id=owner_account_id)
        member_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for member in members:
            agent_id = str(member.get("agent_id") or "").strip()
            if not agent_id or agent_id in seen:
                continue
            if not is_crew_builtin_agent(agent_id):
                self.get_agent(agent_id, owner_account_id=owner_account_id)
            seen.add(agent_id)
            member_rows.append(dict(member, agent_id=agent_id))
        if leader_agent_id not in seen:
            member_rows.insert(0, {"agent_id": leader_agent_id, "role": "Leader", "role_key": "tech_lead"})
        team_id = f"team_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO external_team (
                  id, owner_account_id, name, description, leader_agent_id, instructions,
                  team_spec_json, formation_plan_json, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    team_id,
                    owner_account_id,
                    name,
                    description,
                    leader_agent_id,
                    instructions,
                    json.dumps(team_spec or {}, ensure_ascii=False),
                    json.dumps(formation_plan or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for index, member in enumerate(member_rows):
                agent_id = str(member.get("agent_id") or "").strip()
                role_key = str(member.get("role_key") or "").strip()
                if not role_key:
                    role_key = infer_role_key(str(member.get("role") or ""), is_leader=agent_id == leader_agent_id)
                preset = role_preset(role_key)
                capabilities = member.get("assigned_capabilities")
                if not isinstance(capabilities, list) or not capabilities:
                    capabilities = member.get("capabilities")
                if not isinstance(capabilities, list) or not capabilities:
                    capabilities = list(preset.get("capabilities") or [])
                capabilities = normalize_capabilities(capabilities)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO external_team_member (
                      id, team_id, agent_id, role, role_key, role_label,
                      capabilities_json, workflow_lane, sort_order, created_at
                    ) VALUES (
                      COALESCE((SELECT id FROM external_team_member WHERE team_id = ? AND agent_id = ?), ?),
                      ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        team_id,
                        agent_id,
                        f"team_member_{uuid.uuid4().hex[:12]}",
                        team_id,
                        agent_id,
                        str(member.get("role") or "").strip(),
                        str(preset["key"]),
                        str(member.get("role_label") or preset["label"]),
                        json.dumps(capabilities, ensure_ascii=False),
                        str(member.get("workflow_lane") or preset.get("workflow_lane") or ""),
                        int(member.get("sort_order", index) or index),
                        now,
                    ),
                )
        return self.get_team(team_id, owner_account_id=owner_account_id)

    def list_teams(self, *, owner_account_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM external_team WHERE owner_account_id = ? AND archived_at IS NULL ORDER BY created_at DESC",
                (owner_account_id,),
            ).fetchall()
        return [self.get_team(row["id"], owner_account_id=owner_account_id) for row in rows]

    def get_team(self, team_id: str, *, owner_account_id: str = "") -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM external_team WHERE id = ? AND owner_account_id = ? AND archived_at IS NULL",
                (team_id, owner_account_id),
            ).fetchone()
            if row is None:
                raise KeyError(team_id)
            member_rows = conn.execute(
                """
                SELECT tm.*, ea.name AS agent_name, ea.provider AS agent_provider
                FROM external_team_member tm
                LEFT JOIN external_agent ea ON ea.id = tm.agent_id AND ea.owner_account_id = ?
                WHERE tm.team_id = ?
                ORDER BY tm.sort_order ASC, tm.created_at ASC
                """,
                (owner_account_id, team_id),
            ).fetchall()
        team = dict(row)
        try:
            team["team_spec"] = json.loads(str(team.pop("team_spec_json") or "{}"))
        except json.JSONDecodeError:
            team["team_spec"] = {}
        try:
            team["formation_plan"] = json.loads(str(team.pop("formation_plan_json") or "{}"))
        except json.JSONDecodeError:
            team["formation_plan"] = {}
        team["members"] = [self._team_member_dict(member) for member in member_rows]
        return team

    def delete_team(self, team_id: str, *, owner_account_id: str = "") -> None:
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM external_team WHERE id = ? AND owner_account_id = ? AND archived_at IS NULL",
                (team_id, owner_account_id),
            ).fetchone()
            if row is None:
                raise KeyError(team_id)
            conn.execute(
                "UPDATE external_team SET archived_at = ?, updated_at = ? WHERE id = ? AND owner_account_id = ?",
                (now, now, team_id, owner_account_id),
            )

    @staticmethod
    def _runtime_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item.pop("status", None)
        return item

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["custom_args"] = json.loads(item.pop("custom_args_json") or "[]")
        item["custom_env"] = json.loads(item.pop("custom_env_json") or "{}")
        try:
            item["profile"] = json.loads(item.pop("profile_json") or "{}")
        except json.JSONDecodeError:
            item["profile"] = {}
        return item

    @staticmethod
    def _team_member_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if is_crew_builtin_agent(str(item.get("agent_id") or "")):
            builtin = crew_builtin_agent_public()
            item["agent_name"] = item.get("agent_name") or builtin["name"]
            item["agent_provider"] = item.get("agent_provider") or builtin["provider"]
        try:
            item["capabilities"] = json.loads(item.pop("capabilities_json") or "[]")
        except json.JSONDecodeError:
            item["capabilities"] = []
        item["assigned_capabilities"] = list(item["capabilities"])
        return item
