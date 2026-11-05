"""4A-1 contracts for the standalone external-agent Catalog boundary."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from crew.agent.executor.base import ExecutionContext
from crew.agent.executor.external import (
    ClientExecutorConfig,
    ExternalExecutorConfig,
    _run_client_prompt,
)

from crew.agent.external.catalog import (
    CREW_BUILTIN_AGENT_ID,
    ExternalAgentCatalog,
    builtin_agent_public,
)
from crew.agent.external.store import ExternalAgentStore
from crew.agent.external.tools import register_external_agent_tools
from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.team.external_store import TeamExternalAgentStore


def _runtime(runtime_id: str = "runtime-a") -> dict[str, Any]:
    return {
        "id": runtime_id,
        "provider": "test-provider",
        "name": "Test Runtime",
        "executable_path": str(Path(sys.executable)),
        "version": "1.0",
        "protocol": "acp",
        "metadata": {
            "availability_status": "ready",
            "models": [{"id": "model-a", "name": "Model A", "default": True}],
            "default_model_id": "model-a",
        },
    }


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


class FakeCatalog:
    """Minimal non-SQLite Catalog for a real executor consumer."""

    def __init__(self) -> None:
        self.runtime = _runtime()
        self.runtime["executable_path"] = "test_runtime_module"
        self.agent = {
            "id": "agent-a",
            "name": "Fake Agent",
            "provider": "test-provider",
            "runtime_id": "runtime-a",
            "model": "model-a",
            "system_prompt": "",
        }

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del owner_account_id
        if agent_id != self.agent["id"]:
            raise KeyError(agent_id)
        return dict(self.agent), dict(self.runtime)


def test_external_store_is_a_standalone_catalog_without_team_tables(tmp_path):
    db_path = tmp_path / "external.sqlite"

    store = ExternalAgentStore(str(db_path))

    tables = _table_names(db_path)
    assert "external_agent" in tables
    assert "external_runtime" in tables
    assert "external_team" not in tables
    assert "external_team_member" not in tables
    assert not hasattr(store, "create_team")
    assert not hasattr(store, "get_team")


def test_catalog_owner_isolation_and_delete_do_not_require_team_schema(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "external.sqlite"))
    store.upsert_runtime(_runtime())
    agent = store.create_agent(
        owner_account_id="owner-a",
        name="Owner A Agent",
        runtime_id="runtime-a",
        model="model-a",
    )

    assert store.list_agents(owner_account_id="owner-b") == []
    with pytest.raises(KeyError):
        store.get_agent(agent["id"], owner_account_id="owner-b")

    store.delete_agent(agent["id"], owner_account_id="owner-a")
    assert store.list_agents(owner_account_id="owner-a") == []


def test_builtin_identity_is_stable_and_public_payload_is_copy_safe():
    first = builtin_agent_public()
    first["name"] = "mutated"

    assert CREW_BUILTIN_AGENT_ID == "crew::builtin"
    assert builtin_agent_public()["name"] == "Crew 内置智能体"
    assert first["id"] == CREW_BUILTIN_AGENT_ID
    assert ExternalAgentCatalog.__name__ == "ExternalAgentCatalog"


def test_external_agent_tools_accept_catalog_protocol_annotation():
    annotation = get_type_hints(register_external_agent_tools)["store"]

    assert annotation is ExternalAgentCatalog


def test_executor_configs_accept_catalog_protocol_annotation():
    assert get_type_hints(ClientExecutorConfig)["external_store"] == ExternalAgentCatalog | None
    assert get_type_hints(ExternalExecutorConfig)["external_store"] == ExternalAgentCatalog | None


@pytest.mark.asyncio
async def test_fake_catalog_runs_real_client_executor_consumer(tmp_path, monkeypatch):
    module_path = tmp_path / "test_runtime_module.py"
    module_path.write_text(
        "def run_agent(prompt, **kwargs):\n    return {'text': prompt}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    catalog = FakeCatalog()
    context = ExecutionContext(
        session_id="session-a",
        request_id="request-a",
        system_prompt="",
        messages=[],
        query="hello",
        cwd=str(tmp_path),
    )

    output = await _run_client_prompt(
        "hello",
        ClientExecutorConfig(external_agent_id="agent-a", external_store=catalog),
        context,
    )

    assert "Fake Agent" in output
    assert output.endswith("hello")


def test_team_facade_preserves_default_roles_owner_isolation_and_delete_guard(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "teams.sqlite"))
    store.upsert_runtime(_runtime())
    leader = store.create_agent(
        owner_account_id="owner-a",
        name="Leader",
        runtime_id="runtime-a",
        model="model-a",
    )
    member = store.create_agent(
        owner_account_id="owner-a",
        name="Member",
        runtime_id="runtime-a",
        model="model-a",
    )

    team = store.create_team(
        owner_account_id="owner-a",
        name="Team A",
        leader_agent_id=leader["id"],
        members=[{"agent_id": leader["id"]}, {"agent_id": member["id"], "role": "测试"}],
    )

    by_agent = {item["agent_id"]: item for item in team["members"]}
    assert by_agent[leader["id"]]["role_key"] == "project_manager"
    assert by_agent[leader["id"]]["capabilities"] == ["planning"]
    assert by_agent[member["id"]]["role_key"] == "qa_engineer"
    assert by_agent[member["id"]]["capabilities"] == ["testing", "verification"]
    with pytest.raises(KeyError):
        store.get_team(team["id"], owner_account_id="owner-b")
    with pytest.raises(ValueError, match="团队"):
        store.delete_agent(leader["id"], owner_account_id="owner-a")

    store.delete_team(team["id"], owner_account_id="owner-a")
    store.delete_agent(leader["id"], owner_account_id="owner-a")


def test_team_facade_migrates_legacy_embedded_formation_plan(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    store = TeamExternalAgentStore(str(db_path))
    store.upsert_runtime(_runtime())
    leader = store.create_agent(
        owner_account_id="owner-a",
        name="Leader",
        runtime_id="runtime-a",
        model="model-a",
    )
    team = store.create_team(
        owner_account_id="owner-a",
        name="Legacy Team",
        leader_agent_id=leader["id"],
        members=[{"agent_id": leader["id"], "role": "Leader"}],
        team_spec={
            "goal": "legacy",
            "formation": {
                "leader_agent_id": leader["id"],
                "required_capabilities": ["planning"],
                "confidence": 0.8,
                "staffing_mode": "legacy",
                "assignments": [
                    {
                        "agent_id": leader["id"],
                        "source": "user_forced",
                        "locked": True,
                    }
                ],
            },
        },
    )
    store = TeamExternalAgentStore(str(db_path))

    migrated = store.get_team(team["id"], owner_account_id="owner-a")
    assert "formation" not in migrated["team_spec"]
    assert migrated["team_spec"]["goal"] == "legacy"
    assert migrated["formation_plan"]["leader_agent_id"] == leader["id"]
    assert migrated["formation_plan"]["coverage"]["required"] == ["planning"]
    assert migrated["formation_plan"]["staffing_mode"] == "legacy"
    assert migrated["formation_plan"]["members"][0]["selection_source"] == "user_forced"
    assert migrated["formation_plan"]["members"][0]["locked"] is True


def test_team_facade_migration_tolerates_malformed_legacy_confidence(tmp_path):
    db_path = tmp_path / "legacy-confidence.sqlite"
    store = TeamExternalAgentStore(str(db_path))
    store.upsert_runtime(_runtime())
    leader = store.create_agent(
        owner_account_id="owner-a",
        name="Leader",
        runtime_id="runtime-a",
        model="model-a",
    )
    team = store.create_team(
        owner_account_id="owner-a",
        name="Legacy Team",
        leader_agent_id=leader["id"],
        members=[{"agent_id": leader["id"]}],
        team_spec={"formation": {"confidence": "not-a-number"}},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE external_team SET formation_plan_json = '{}' WHERE id = ?",
            (team["id"],),
        )

    migrated = TeamExternalAgentStore(str(db_path)).get_team(team["id"], owner_account_id="owner-a")
    assert migrated["formation_plan"]["confidence"]["overall"] == pytest.approx(0.5)


def test_team_facade_upgrades_old_team_schema_and_backfills_local_owner(tmp_path):
    db_path = tmp_path / "old-team-schema.sqlite"
    catalog = ExternalAgentStore(str(db_path))
    catalog.upsert_runtime(_runtime())
    agent = catalog.create_agent(
        owner_account_id=LOCAL_OWNER_ACCOUNT_ID,
        name="Legacy Leader",
        runtime_id="runtime-a",
        model="model-a",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE external_team (
              id TEXT PRIMARY KEY,
              owner_account_id TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              leader_agent_id TEXT NOT NULL,
              instructions TEXT NOT NULL DEFAULT '',
              team_spec_json TEXT NOT NULL DEFAULT '{}',
              formation_plan_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE external_team_member (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT '',
              sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            INSERT INTO external_team
              (id, name, leader_agent_id, created_at, updated_at)
            VALUES ('legacy-team', 'Legacy', 'AGENT_ID', '2026-01-01', '2026-01-01');
            INSERT INTO external_team_member
              (id, team_id, agent_id, role, created_at)
            VALUES ('legacy-member', 'legacy-team', 'AGENT_ID', 'Leader', '2026-01-01');
            """.replace("AGENT_ID", agent["id"])
        )

    migrated = TeamExternalAgentStore(str(db_path)).get_team(
        "legacy-team", owner_account_id=LOCAL_OWNER_ACCOUNT_ID
    )
    assert migrated["owner_account_id"] == LOCAL_OWNER_ACCOUNT_ID
    assert migrated["archived_at"] is None
    assert migrated["members"][0]["agent_id"] == agent["id"]
