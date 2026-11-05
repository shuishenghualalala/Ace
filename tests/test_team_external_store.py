"""Team 门面跨 Feature 解耦（命名空间审计 P1-2）的迁移与行为测试。

覆盖四类耦合的解除：
1. external_team / external_team_member 不再保留指向 external_agent 的外键；
2. 成员展示信息走写入时快照列，读取不 JOIN 外援表；
3. 存在性检查经注入的 lifecycle 完成；
4. 删除路径经注入的 lifecycle 完成（observations → bindings → agent）。
"""

from __future__ import annotations

import sqlite3

import pytest

from crew.agent.external.catalog import CREW_BUILTIN_AGENT_ID
from crew.agent.external.store import ExternalAgentStore
from crew.team.external_store import TeamExternalAgentStore


def _runtime() -> dict:
    return {
        "id": "runtime-a",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    }


def _foreign_key_tables(conn: sqlite3.Connection, table: str) -> set[str]:
    # foreign_key_list 行结构：id, seq, table, from, to, ...
    return {str(row[2]) for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def test_fresh_schema_has_no_cross_feature_foreign_keys(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "crew.db"))

    with sqlite3.connect(store.db_path) as conn:
        assert _foreign_key_tables(conn, "external_team") == set()
        assert _foreign_key_tables(conn, "external_team_member") == {"external_team"}


def test_legacy_foreign_key_schema_rebuilds_and_backfills_snapshots(tmp_path):
    db_path = tmp_path / "legacy-fk.sqlite"
    catalog = ExternalAgentStore(str(db_path))
    catalog.upsert_runtime(_runtime())
    agent = catalog.create_agent(
        owner_account_id="local",
        name="Legacy Leader",
        runtime_id="runtime-a",
        model="model-a",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE external_team (
              id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
              leader_agent_id TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
              team_spec_json TEXT NOT NULL DEFAULT '{}', formation_plan_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(leader_agent_id) REFERENCES external_agent(id)
            );
            CREATE TABLE external_team_member (
              id TEXT PRIMARY KEY, team_id TEXT NOT NULL, agent_id TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT '', role_key TEXT NOT NULL DEFAULT '',
              role_label TEXT NOT NULL DEFAULT '', capabilities_json TEXT NOT NULL DEFAULT '[]',
              workflow_lane TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              FOREIGN KEY(team_id) REFERENCES external_team(id),
              FOREIGN KEY(agent_id) REFERENCES external_agent(id), UNIQUE(team_id, agent_id)
            );
            INSERT INTO external_team
              (id, name, leader_agent_id, created_at, updated_at)
            VALUES ('legacy-team', 'Legacy', 'AGENT_ID', '2026-01-01', '2026-01-01');
            INSERT INTO external_team_member
              (id, team_id, agent_id, role, sort_order, created_at)
            VALUES ('legacy-member', 'legacy-team', 'AGENT_ID', 'Leader', 0, '2026-01-01');
            """.replace("AGENT_ID", agent["id"])
        )

    store = TeamExternalAgentStore(str(db_path))
    with sqlite3.connect(store.db_path) as conn:
        assert _foreign_key_tables(conn, "external_team") == set()
        assert _foreign_key_tables(conn, "external_team_member") == {"external_team"}
    team = store.get_team("legacy-team", owner_account_id="local")
    member = team["members"][0]
    assert member["agent_id"] == agent["id"]
    assert member["agent_name"] == "Legacy Leader"
    assert member["agent_provider"] == "kimi"
    assert member["role"] == "Leader"

    reopened = TeamExternalAgentStore(str(db_path))
    assert reopened.get_team("legacy-team", owner_account_id="local")["members"][0][
        "agent_name"
    ] == "Legacy Leader"


def test_get_team_reads_snapshots_without_joining_external_agent(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "crew.db"))
    store.upsert_runtime(_runtime())
    agent = store.create_agent(
        owner_account_id="local", name="Kimi Coder", runtime_id="runtime-a"
    )
    team = store.create_team(
        owner_account_id="local",
        name="研发团队",
        leader_agent_id=agent["id"],
        members=[{"agent_id": agent["id"], "role": "Leader"}],
    )

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE external_team_member SET agent_name = '快照名', agent_provider = '快照提供方' WHERE team_id = ?",
            (team["id"],),
        )
        conn.execute(
            "UPDATE external_agent SET name = '改名后', provider = '新提供方' WHERE id = ?",
            (agent["id"],),
        )

    member = store.get_team(team["id"], owner_account_id="local")["members"][0]
    assert member["agent_name"] == "快照名"
    assert member["agent_provider"] == "快照提供方"


def test_create_team_snapshots_come_from_catalog_not_client_payload(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "crew.db"))
    store.upsert_runtime(_runtime())
    agent = store.create_agent(
        owner_account_id="local", name="真实名称", runtime_id="runtime-a"
    )

    team = store.create_team(
        owner_account_id="local",
        name="研发团队",
        leader_agent_id=agent["id"],
        members=[
            {
                "agent_id": agent["id"],
                "role": "Leader",
                "agent_name": "客户端伪造名",
                "agent_provider": "客户端伪造提供方",
            }
        ],
    )

    member = team["members"][0]
    assert member["agent_name"] == "真实名称"
    assert member["agent_provider"] == "kimi"


def test_builtin_member_display_falls_back_to_builtin_identity(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "crew.db"))
    store.upsert_runtime(_runtime())
    writer = store.create_agent(
        owner_account_id="local", name="Kimi Writer", runtime_id="runtime-a"
    )

    team = store.create_team(
        owner_account_id="local",
        name="内置协作团队",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[
            {"agent_id": CREW_BUILTIN_AGENT_ID, "role": "负责拆解", "role_key": "tech_lead"},
            {"agent_id": writer["id"], "role": "负责写作", "role_key": "technical_writer"},
        ],
    )
    reloaded = store.get_team(team["id"], owner_account_id="local")
    builtin = next(
        member for member in reloaded["members"] if member["agent_id"] == CREW_BUILTIN_AGENT_ID
    )

    assert builtin["agent_name"] == "Crew 内置智能体"
    assert builtin["agent_provider"] == "crew"


def test_delete_agent_guard_cleanup_and_unknown_agent(tmp_path):
    store = TeamExternalAgentStore(str(tmp_path / "crew.db"))
    store.upsert_runtime(_runtime())
    agent = store.create_agent(
        owner_account_id="local", name="Kimi Coder", runtime_id="runtime-a", model="model-a"
    )
    store.record_agent_profile_observation(
        owner_account_id="local",
        external_agent_id=agent["id"],
        source_run_id="run-1",
        source_node_id="node-1",
        source_attempt_id="attempt-1",
        capabilities=["backend"],
        assessment_source="execution",
        outcome="success",
        quality_weight=1.0,
    )
    store.save_runtime_session_binding(
        owner_account_id="local",
        crew_session_id="crew-1",
        external_agent_id=agent["id"],
        runtime_id="runtime-a",
        adapter_id="acp-stdio",
        native_session_id="native-1",
    )
    team = store.create_team(
        owner_account_id="local",
        name="研发团队",
        leader_agent_id=agent["id"],
        members=[{"agent_id": agent["id"], "role": "Leader"}],
    )

    with pytest.raises(ValueError, match="团队"):
        store.delete_agent(agent["id"], owner_account_id="local")
    with pytest.raises(KeyError):
        store.delete_agent("agent_missing", owner_account_id="local")

    store.delete_team(team["id"], owner_account_id="local")
    store.delete_agent(agent["id"], owner_account_id="local")

    assert store.list_agents(owner_account_id="local") == []
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM external_agent_profile_observation WHERE external_agent_id = ?",
            (agent["id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM external_runtime_session_binding WHERE external_agent_id = ?",
            (agent["id"],),
        ).fetchone()[0] == 0


class _FakeLifecycle:
    """记录调用并返回固定身份的 lifecycle 替身。"""

    def __init__(self, agent: dict | None = None, *, fail_delete: bool = False) -> None:
        self.agent = agent
        self.fail_delete = fail_delete
        self.get_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def get_agent(self, agent_id: str, *, owner_account_id: str) -> dict:
        self.get_calls.append((agent_id, owner_account_id))
        if self.agent is None:
            raise KeyError(agent_id)
        return self.agent

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None:
        self.delete_calls.append((agent_id, owner_account_id))
        if self.fail_delete:
            raise RuntimeError("lifecycle delete failed")


def test_injected_lifecycle_routes_existence_and_delete(tmp_path):
    lifecycle = _FakeLifecycle({"id": "agent-1", "name": "注入名", "provider": "注入提供方"})
    store = TeamExternalAgentStore(
        str(tmp_path / "crew.db"),
        external_catalog_provider=lambda: lifecycle,
    )

    team = store.create_team(
        owner_account_id="local",
        name="注入团队",
        leader_agent_id="agent-1",
        members=[{"agent_id": "agent-1", "role": "Leader"}],
    )

    assert ("agent-1", "local") in lifecycle.get_calls
    assert team["members"][0]["agent_name"] == "注入名"

    # agent-1 仍在团队中会被门面守卫拦截；改删不在团队中的 agent-2 验证路由。
    store.delete_agent("agent-2", owner_account_id="local")

    assert lifecycle.delete_calls == [("agent-2", "local")]


def test_unknown_agent_via_injected_lifecycle_raises_key_error(tmp_path):
    lifecycle = _FakeLifecycle()
    store = TeamExternalAgentStore(
        str(tmp_path / "crew.db"),
        external_catalog_provider=lambda: lifecycle,
    )

    with pytest.raises(KeyError):
        store.create_team(
            owner_account_id="local",
            name="缺失团队",
            leader_agent_id="agent-missing",
            members=[],
        )
    with pytest.raises(KeyError):
        store.delete_agent("agent-missing", owner_account_id="local")
    assert lifecycle.delete_calls == []


def test_inactive_external_feature_fails_closed(tmp_path):
    store = TeamExternalAgentStore(
        str(tmp_path / "crew.db"),
        external_catalog_provider=lambda: None,
    )

    with pytest.raises(RuntimeError, match="外部智能体服务未激活"):
        store.delete_agent("agent-1", owner_account_id="local")
    with pytest.raises(RuntimeError, match="外部智能体服务未激活"):
        store.create_team(
            owner_account_id="local",
            name="孤儿团队",
            leader_agent_id="agent-1",
            members=[],
        )
