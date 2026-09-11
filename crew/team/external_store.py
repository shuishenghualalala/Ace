"""Team-owned persistence facade over the neutral external agent catalog."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from crew.agent.external.catalog import (
    CREW_BUILTIN_AGENT_ID,
    ExternalAgentLifecycle,
    builtin_agent_public,
    is_builtin_agent,
)
from crew.agent.external.capabilities import normalize_capabilities
from crew.agent.external.store import ExternalAgentStore, _now
from crew.state._migration import backfill_empty_owner_rows
from crew.team.roles import infer_role_key, role_preset


class _InheritedLifecycle:
    """Embedded-mode view of the external lifecycle served by the base store.

    Cross-feature operations always go through the ``ExternalAgentLifecycle``
    surface. When no provider is injected the facade itself still owns the
    catalog implementation, so this view dispatches to the inherited base
    methods and keeps the team-side ``delete_agent`` override out of the
    external deletion path.
    """

    def __init__(self, store: ExternalAgentStore) -> None:
        self._store = store

    def get_agent(self, agent_id: str, *, owner_account_id: str) -> dict[str, Any]:
        return self._store.get_agent(agent_id, owner_account_id=owner_account_id)

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None:
        ExternalAgentStore.delete_agent(self._store, agent_id, owner_account_id=owner_account_id)


class TeamExternalAgentStore(ExternalAgentStore):
    """Compatibility facade that adds Team-owned tables and operations."""

    # 命名空间约定：跨 Feature 不建外键。leader_agent_id / agent_id 只是普通
    # 字符串引用；成员展示信息走写入时快照列，读取不再 JOIN 外援表。
    _EXTERNAL_TEAM_DDL = """
        CREATE TABLE IF NOT EXISTS {table} (
          id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL DEFAULT '',
          name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          leader_agent_id TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
          team_spec_json TEXT NOT NULL DEFAULT '{{}}', formation_plan_json TEXT NOT NULL DEFAULT '{{}}',
          archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """

    _EXTERNAL_TEAM_MEMBER_DDL = """
        CREATE TABLE IF NOT EXISTS {table} (
          id TEXT PRIMARY KEY, team_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT '', role_key TEXT NOT NULL DEFAULT '',
          role_label TEXT NOT NULL DEFAULT '', capabilities_json TEXT NOT NULL DEFAULT '[]',
          workflow_lane TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '',
          agent_provider TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(team_id) REFERENCES external_team(id), UNIQUE(team_id, agent_id)
        )
    """

    def __init__(
        self,
        db_path: str,
        *,
        external_catalog_provider: Callable[[], ExternalAgentLifecycle | None] | None = None,
    ) -> None:
        super().__init__(db_path)
        self._external_catalog_provider = external_catalog_provider

    def _external_lifecycle(self) -> ExternalAgentLifecycle:
        """Resolve the external-agent lifecycle for cross-feature operations.

        Without an injected provider the facade keeps its embedded
        compatibility role and serves the lifecycle from the inherited catalog
        implementation. With a provider, resolution follows the active external
        feature generation; an inactive external feature fails closed instead
        of falling back to raw SQL against the external tables.
        """
        if self._external_catalog_provider is None:
            return _InheritedLifecycle(self)
        catalog = self._external_catalog_provider()
        if catalog is None:
            raise RuntimeError("外部智能体服务未激活")
        return catalog

    def _create_schema(self, conn) -> None:
        super()._create_schema(conn)
        conn.execute(self._EXTERNAL_TEAM_DDL.format(table="external_team"))
        conn.execute(self._EXTERNAL_TEAM_MEMBER_DDL.format(table="external_team_member"))
        self._drop_cross_feature_foreign_keys(conn, "external_team", self._EXTERNAL_TEAM_DDL)
        self._drop_cross_feature_foreign_keys(
            conn, "external_team_member", self._EXTERNAL_TEAM_MEMBER_DDL
        )
        self._ensure_column(conn, "external_team", "owner_account_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "external_team", "archived_at", "TEXT")
        self._ensure_column(conn, "external_team", "created_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "external_team", "updated_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "external_team", "team_spec_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column(
            conn, "external_team", "formation_plan_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        self._ensure_column(conn, "external_team_member", "role_key", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "external_team_member", "role_label", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            conn, "external_team_member", "capabilities_json", "TEXT NOT NULL DEFAULT '[]'"
        )
        self._ensure_column(
            conn, "external_team_member", "workflow_lane", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(conn, "external_team_member", "agent_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            conn, "external_team_member", "agent_provider", "TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_team_owner ON external_team(owner_account_id, archived_at, created_at)"
        )
        backfill_empty_owner_rows(conn, ["external_team"])
        self._backfill_member_agent_snapshots(conn)
        self._migrate_embedded_formation_plans(conn)

    @staticmethod
    def _drop_cross_feature_foreign_keys(conn, table: str, ddl_template: str) -> bool:
        """Rebuild ``table`` without foreign keys into the external namespace.

        ``leader_agent_id`` and ``agent_id`` stay plain string references per
        the namespace rules. The rebuild follows the repository's create, copy,
        drop and rename migration shape, copies the intersection of old and new
        columns, and is idempotent.
        """
        references = {
            str(row["table"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        if "external_agent" not in references:
            return False
        conn.execute(f"DROP TABLE IF EXISTS {table}_new")
        conn.execute(ddl_template.format(table=f"{table}_new"))
        old_columns = [
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        new_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table}_new)").fetchall()
        }
        shared = ", ".join(name for name in old_columns if name in new_columns)
        conn.execute(f"INSERT INTO {table}_new ({shared}) SELECT {shared} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        return True

    @staticmethod
    def _backfill_member_agent_snapshots(conn) -> None:
        """One-time backfill of member name/provider snapshots from the catalog.

        This is the only place team persistence still reads external_agent
        rows: legacy members with empty snapshots are resolved through the
        same owner-scoped lookup the removed LEFT JOIN used. Rows created
        after this migration carry write-time snapshots and stay untouched.
        """
        owners = conn.execute(
            "SELECT DISTINCT owner_account_id FROM external_team"
        ).fetchall()
        for owner_row in owners:
            owner = str(owner_row["owner_account_id"] or "")
            conn.execute(
                """
                UPDATE external_team_member
                SET agent_name = COALESCE((
                      SELECT ea.name FROM external_agent ea
                      WHERE ea.id = external_team_member.agent_id
                        AND ea.owner_account_id = ?
                    ), ''),
                    agent_provider = COALESCE((
                      SELECT ea.provider FROM external_agent ea
                      WHERE ea.id = external_team_member.agent_id
                        AND ea.owner_account_id = ?
                    ), '')
                WHERE agent_name = ''
                  AND agent_provider = ''
                  AND team_id IN (
                    SELECT id FROM external_team WHERE owner_account_id = ?
                  )
                """,
                (owner, owner, owner),
            )

    @staticmethod
    def _migrate_embedded_formation_plans(conn) -> None:
        rows = conn.execute(
            "SELECT id, leader_agent_id, team_spec_json, formation_plan_json FROM external_team"
        ).fetchall()
        for row in rows:
            try:
                spec = json.loads(str(row["team_spec_json"] or "{}"))
                current = json.loads(str(row["formation_plan_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            legacy = spec.pop("formation", None) if isinstance(spec, dict) else None
            if not isinstance(legacy, dict):
                continue
            if isinstance(current, dict) and current:
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
            members = []
            covered = []
            for member in conn.execute(
                "SELECT * FROM external_team_member WHERE team_id = ? ORDER BY sort_order, created_at",
                (row["id"],),
            ).fetchall():
                try:
                    assigned = json.loads(str(member["capabilities_json"] or "[]"))
                except json.JSONDecodeError:
                    assigned = []
                assigned = [str(item) for item in assigned] if isinstance(assigned, list) else []
                covered.extend(assigned)
                assignment = assignment_by_agent.get(str(member["agent_id"]), {})
                members.append(
                    {
                        "agent_id": str(member["agent_id"]),
                        "role_key": str(member["role_key"] or ""),
                        "role_label": str(member["role_label"] or ""),
                        "assigned_capabilities": assigned,
                        "responsibility": {},
                        "responsibility_markdown": str(member["role"] or ""),
                        "selection_source": str(assignment.get("source") or "legacy"),
                        "locked": bool(assignment.get("locked")),
                        "selection_reason": "从旧 TeamSpec.formation 迁移。",
                    }
                )
            required = [
                str(item) for item in (legacy.get("required_capabilities") or []) if str(item)
            ]
            covered_required = list(dict.fromkeys(item for item in covered if item in required))
            try:
                confidence = float(legacy.get("confidence") or 0.5)
            except (TypeError, ValueError):
                confidence = 0.5
            plan = {
                "version": 1,
                "leader_agent_id": str(
                    legacy.get("leader_agent_id") or row["leader_agent_id"] or ""
                ),
                "members": members,
                "coverage": {
                    "required": required,
                    "covered": covered_required,
                    "uncovered": [item for item in required if item not in covered_required],
                },
                "confidence": {
                    "requirement": confidence,
                    "capability_evidence": 0.15,
                    "coverage": len(covered_required) / len(required) if required else 1.0,
                    "overall": confidence,
                },
                "staffing_mode": str(legacy.get("staffing_mode") or "legacy"),
                "excluded_agent_ids": list(legacy.get("excluded_agents") or []),
                "reasons": ["从旧 TeamSpec.formation 迁移。"],
                "warnings": list(legacy.get("unresolved") or []),
            }
            conn.execute(
                "UPDATE external_team SET team_spec_json = ?, formation_plan_json = ? WHERE id = ?",
                (
                    json.dumps(spec, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    row["id"],
                ),
            )

    def create_team(
        self,
        *,
        owner_account_id: str,
        name: str,
        leader_agent_id: str,
        members: list[dict[str, Any]],
        description: str = "",
        instructions: str = "",
        team_spec: dict[str, Any] | None = None,
        formation_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lifecycle = self._external_lifecycle()
        snapshots: dict[str, tuple[str, str]] = {}

        def agent_snapshot(agent_id: str) -> tuple[str, str]:
            """Resolve name/provider through the catalog; unknown ids raise KeyError."""
            cached = snapshots.get(agent_id)
            if cached is None:
                if is_builtin_agent(agent_id):
                    builtin = builtin_agent_public()
                    cached = (str(builtin["name"]), str(builtin["provider"]))
                else:
                    agent = lifecycle.get_agent(agent_id, owner_account_id=owner_account_id)
                    cached = (str(agent.get("name") or ""), str(agent.get("provider") or ""))
                snapshots[agent_id] = cached
            return cached

        leader_agent_id = str(leader_agent_id or "").strip() or CREW_BUILTIN_AGENT_ID
        agent_snapshot(leader_agent_id)
        rows, seen = [], set()
        for member in members:
            agent_id = str(member.get("agent_id") or "").strip()
            if not agent_id or agent_id in seen:
                continue
            agent_snapshot(agent_id)
            seen.add(agent_id)
            rows.append(dict(member, agent_id=agent_id))
        if leader_agent_id not in seen:
            rows.insert(0, {"agent_id": leader_agent_id, "role": "Leader", "role_key": "tech_lead"})
        team_id, now = f"team_{uuid.uuid4().hex[:12]}", _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO external_team (id, owner_account_id, name, description, leader_agent_id, instructions, team_spec_json, formation_plan_json, archived_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
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
            for index, member in enumerate(rows):
                agent_id = member["agent_id"]
                agent_name, agent_provider = agent_snapshot(agent_id)
                role_key = str(member.get("role_key") or "") or infer_role_key(
                    str(member.get("role") or ""), is_leader=agent_id == leader_agent_id
                )
                preset = role_preset(role_key)
                capabilities = (
                    member.get("assigned_capabilities")
                    or member.get("capabilities")
                    or preset.get("capabilities")
                    or []
                )
                conn.execute(
                    "INSERT OR REPLACE INTO external_team_member (id, team_id, agent_id, role, role_key, role_label, capabilities_json, workflow_lane, sort_order, created_at, agent_name, agent_provider) VALUES (COALESCE((SELECT id FROM external_team_member WHERE team_id = ? AND agent_id = ?), ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        team_id,
                        agent_id,
                        f"team_member_{uuid.uuid4().hex[:12]}",
                        team_id,
                        agent_id,
                        str(member.get("role") or "").strip(),
                        str(preset["key"]),
                        str(member.get("role_label") or preset["label"]),
                        json.dumps(normalize_capabilities(capabilities), ensure_ascii=False),
                        str(member.get("workflow_lane") or preset.get("workflow_lane") or ""),
                        int(member.get("sort_order", index) or index),
                        now,
                        agent_name,
                        agent_provider,
                    ),
                )
        return self.get_team(team_id, owner_account_id=owner_account_id)

    def list_teams(self, *, owner_account_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM external_team WHERE owner_account_id = ? AND archived_at IS NULL ORDER BY created_at DESC",
                (owner_account_id,),
            ).fetchall()
        return [self.get_team(row["id"], owner_account_id=owner_account_id) for row in rows]

    def get_team(self, team_id: str, *, owner_account_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM external_team WHERE id = ? AND owner_account_id = ? AND archived_at IS NULL",
                (team_id, owner_account_id),
            ).fetchone()
            if row is None:
                raise KeyError(team_id)
            member_rows = conn.execute(
                "SELECT * FROM external_team_member WHERE team_id = ? ORDER BY sort_order, created_at",
                (team_id,),
            ).fetchall()
        team = dict(row)
        for key in ("team_spec_json", "formation_plan_json"):
            target = key[:-5] if key.endswith("_json") else key
            try:
                team[target] = json.loads(str(team.pop(key) or "{}"))
            except json.JSONDecodeError:
                team[target] = {}
        team["members"] = [self._team_member_dict(row) for row in member_rows]
        return team

    def delete_team(self, team_id: str, *, owner_account_id: str) -> None:
        with self._conn() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM external_team WHERE id = ? AND owner_account_id = ? AND archived_at IS NULL",
                    (team_id, owner_account_id),
                ).fetchone()
                is None
            ):
                raise KeyError(team_id)
            conn.execute(
                "UPDATE external_team SET archived_at = ?, updated_at = ? WHERE id = ? AND owner_account_id = ?",
                (_now(), _now(), team_id, owner_account_id),
            )

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None:
        """Delete an external agent after refusing active team memberships.

        Existence and the external-side deletion (observations, then session
        bindings, then the agent row) are owned by the external feature and
        reached through the injected lifecycle; this facade only contributes
        the team-membership guard over its own tables.
        """
        lifecycle = self._external_lifecycle()
        lifecycle.get_agent(agent_id, owner_account_id=owner_account_id)
        with self._conn() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM external_team t LEFT JOIN external_team_member tm ON tm.team_id = t.id WHERE t.archived_at IS NULL AND t.owner_account_id = ? AND (t.leader_agent_id = ? OR tm.agent_id = ?) LIMIT 1",
                    (owner_account_id, agent_id, agent_id),
                ).fetchone()
                is not None
            ):
                raise ValueError("智能体已在团队中，暂不能删除")
        lifecycle.delete_agent(agent_id, owner_account_id=owner_account_id)

    @staticmethod
    def _team_member_dict(row) -> dict[str, Any]:
        item = dict(row)
        if is_builtin_agent(str(item.get("agent_id") or "")):
            builtin = builtin_agent_public()
            item["agent_name"] = item.get("agent_name") or builtin["name"]
            item["agent_provider"] = item.get("agent_provider") or builtin["provider"]
        try:
            item["capabilities"] = json.loads(item.pop("capabilities_json") or "[]")
        except json.JSONDecodeError:
            item["capabilities"] = []
        item["assigned_capabilities"] = list(item["capabilities"])
        return item
