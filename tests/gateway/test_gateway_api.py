"""Gateway 新增 REST 端点测试。"""

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app
from crew.state.config import Config, ModelProfile
from crew.team.models import TeamPlan, TeamPlanNode
from crew.team.roles import CREW_BUILTIN_AGENT_ID


OWNER_A = "A:uid-a"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    return create_app(crew)


def test_gateway_wires_interaction_bridge_to_team_manager(tmp_path):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)

    create_app(crew)

    assert crew.interaction_bridge is not None
    assert crew.team is not None
    assert crew.team.interaction_bridge is crew.interaction_bridge


@pytest.mark.asyncio
async def test_api_usage(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_tokens" in data
    assert "session_count" in data


@pytest.mark.asyncio
async def test_api_session_context(api, auth_headers):
    transport = ASGITransport(app=api)
    # create_app fixture builds a real crew under app routes; use the endpoint
    # setup path that creates an owner-scoped session for the default test auth.
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/session/test-session/agent-config",
            json={"executor": "builtin", "title": "ctx"},
        )
        resp = await client.get("/api/session/test-session/context")
    assert resp.status_code == 200
    data = resp.json()
    assert "used_tokens" in data
    assert "max_tokens" in data
    assert "ratio" in data


@pytest.mark.asyncio
async def test_team_agent_config_update_evicts_team_cache(tmp_path, auth_headers):
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "team-config-cache.db"),
            cron_enabled=False,
            team_config={
                "members": [{
                    "member_id": "kk",
                    "name": "kk",
                    "executor": "builtin",
                    "capabilities": ["implementation"],
                }],
            },
        ),
        enable_team=True,
    )
    crew.session_store.ensure_session("team-config-cache", owner_account_id=OWNER_A)
    cached = crew.team._get_or_create("team-config-cache", owner_account_id=OWNER_A)
    assert cached is crew.team._teams[(OWNER_A, "team-config-cache")]

    app = create_app(crew)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        response = await client.put(
            "/api/session/team-config-cache/agent-config",
            json={
                "executor": "team",
                "team": {},
            },
        )

    assert response.status_code == 200
    assert (OWNER_A, "team-config-cache") not in crew.team._teams


@pytest.mark.asyncio
async def test_external_session_model_switch_requires_idle_and_runtime_catalog(tmp_path, auth_headers, monkeypatch):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    runtime = crew.external_agents.upsert_runtime({
        "id": "codex-model-switch",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "/bin/codex",
        "version": "test",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "gpt-default",
            "runtime_capabilities": {"model_switch": True},
            "models": [
                {"id": "gpt-default", "label": "GPT Default", "default": True},
                {"id": "gpt-alt", "label": "GPT Alt"},
            ],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Codex Agent",
        runtime_id=runtime["id"],
        model="gpt-default",
    )
    crew.session_store.ensure_session("external-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "external-model",
        {
            "executor": "acp",
            "external_agent_id": agent["id"],
        },
        owner_account_id=OWNER_A,
    )
    crew.session_store.save(
        "external-model",
        [Message.user("hello"), Message.assistant("answer", model="gpt-default")],
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        current = await client.get("/api/session/external-model/model")
        history = await client.get("/api/session/external-model")
        switched = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "gpt-alt"},
        )
        reloaded = await client.get("/api/session/external-model/model")
        invalid = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "unknown"},
        )
        monkeypatch.setattr(
            crew.dispatcher,
            "status",
            lambda *_args, **_kwargs: {"live": "running", "active_request_id": "req-1"},
        )
        busy = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "gpt-default"},
        )

    assert current.status_code == 200
    assert current.json()["source"] == "external"
    assert current.json()["model_profile_id"] == "gpt-default"
    assert current.json()["model_switchable"] is True
    assert [item["id"] for item in current.json()["models"]] == ["gpt-default", "gpt-alt"]
    assert history.json()[-1]["model"] == "gpt-default"
    assert switched.status_code == 200
    assert switched.json()["model_profile_id"] == "gpt-alt"
    assert reloaded.status_code == 200
    assert reloaded.json()["model_profile_id"] == "gpt-alt"
    assert invalid.status_code == 400
    assert busy.status_code == 409
    stored = crew.session_store.get_agent_config("external-model", owner_account_id=OWNER_A)
    assert stored["executor"] == "external"
    assert stored["external"]["model"] == "gpt-alt"


@pytest.mark.asyncio
async def test_external_session_agent_config_requires_one_canonical_id(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    runtime = crew.external_agents.upsert_runtime({
        "id": "external-config-runtime",
        "provider": "custom",
        "name": "External Config Runtime",
        "executable_path": "/bin/sh",
        "version": "test",
        "protocol": "cli",
        "metadata": {"availability_status": "ready"},
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="External Config Agent",
        runtime_id=runtime["id"],
        model="default",
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        missing = await client.put(
            "/api/session/external-config-missing/agent-config",
            json={"executor": "external"},
        )
        top_level = await client.put(
            "/api/session/external-config-top-level/agent-config",
            json={
                "executor": "external",
                "external_agent_id": agent["id"],
            },
        )
        conflict = await client.put(
            "/api/session/external-config-conflict/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": agent["id"]},
                "acp": {"external_agent_id": "different-agent"},
            },
        )
        equal = await client.put(
            "/api/session/external-config-equal/agent-config",
            json={
                "executor": "acp",
                "external": {"external_agent_id": agent["id"], "model": "default"},
                "acp": {"external_agent_id": agent["id"]},
            },
        )
        acp_legacy = await client.put(
            "/api/session/external-config-acp-legacy/agent-config",
            json={
                "executor": "acp",
                "external_agent_id": agent["id"],
            },
        )
        loaded = await client.get("/api/session/external-config-equal/agent-config")

    assert missing.status_code == 400
    assert top_level.status_code == 400
    assert "不支持顶层" in top_level.json()["error"]
    assert conflict.status_code == 400
    assert equal.status_code == 200
    assert acp_legacy.status_code == 200
    assert acp_legacy.json()["external"]["external_agent_id"] == agent["id"]
    assert "external_agent_id" not in acp_legacy.json()
    assert equal.json()["executor"] == "external"
    assert equal.json()["external"]["external_agent_id"] == agent["id"]
    assert "acp" not in equal.json()
    assert "external_agent_id" not in equal.json()
    assert loaded.status_code == 200
    assert loaded.json() == equal.json()


@pytest.mark.asyncio
async def test_external_session_model_resolves_adapter_declared_legacy_id(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    old_runtime = crew.external_agents.upsert_runtime({
        "id": "claude-session-model-migration",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": "/bin/claude",
        "version": "old",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "default",
            "models": [{"id": "default", "label": "CLI 默认模型", "default": True}],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Claude Agent",
        runtime_id=old_runtime["id"],
        model="default",
    )
    crew.external_agents.upsert_runtime({
        **old_runtime,
        "version": "new",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "sonnet",
            "models": [
                {"id": "sonnet", "label": "Claude Sonnet（当前）", "default": True},
                {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
            ],
            "model_migrations": {"default": "sonnet"},
        },
    })
    crew.session_store.ensure_session("claude-legacy-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "claude-legacy-model",
        {
            "executor": "external",
            "external": {
                "external_agent_id": agent["id"],
                "model": "default",
            },
        },
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        current = await client.get("/api/session/claude-legacy-model/model")

    assert current.status_code == 200
    assert current.json()["model_profile_id"] == "sonnet"
    assert current.json()["model_label"] == "Claude Sonnet（当前）"
    assert crew.external_agents.get_agent(
        agent["id"],
        owner_account_id=OWNER_A,
    )["model"] == "sonnet"


@pytest.mark.asyncio
async def test_external_session_model_switch_requires_explicit_runtime_capability(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    runtime = crew.external_agents.upsert_runtime({
        "id": "protocol-is-not-capability",
        "provider": "custom",
        "name": "Custom CLI",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "model-a",
            "models": [
                {"id": "model-a", "label": "Model A", "default": True},
                {"id": "model-b", "label": "Model B"},
            ],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Custom Agent",
        runtime_id=runtime["id"],
        model="model-a",
    )
    crew.session_store.ensure_session("explicit-capability", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "explicit-capability",
        {
            "executor": "external",
            "external": {"external_agent_id": agent["id"]},
        },
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        current = await client.get("/api/session/explicit-capability/model")
        switched = await client.put(
            "/api/session/explicit-capability/model",
            json={"model_profile_id": "model-b"},
        )

    assert current.status_code == 200
    assert current.json()["model_switchable"] is False
    assert switched.status_code == 409


@pytest.mark.asyncio
async def test_team_session_model_materializes_and_switches_one_member(tmp_path, auth_headers, monkeypatch):
    db_path = tmp_path / "crew.db"
    crew = build_app(config=Config(db_path=str(db_path), cron_enabled=False), enable_team=True)
    runtime = crew.external_agents.upsert_runtime({
        "id": "team-model-runtime",
        "provider": "custom",
        "name": "Team Model Runtime",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "model-b",
            "runtime_capabilities": {"model_switch": True, "session_resume": True},
            "models": [
                {"id": "model-a", "label": "Model A", "capabilities": ["text", "tools"]},
                {"id": "model-b", "label": "Model B", "default": True, "capabilities": ["text"]},
            ],
        },
    })
    leader = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Team Leader",
        runtime_id=runtime["id"],
        model="model-b",
    )
    member = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Team Member",
        runtime_id=runtime["id"],
        model="model-b",
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Model Team",
        leader_agent_id=leader["id"],
        members=[
            {"agent_id": leader["id"], "role": "Leader"},
            {"agent_id": member["id"], "role": "Member"},
        ],
    )
    crew.session_store.ensure_session("team-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-model",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    crew.session_store.ensure_session("team-model-other", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-model-other",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        initial = await client.get("/api/session/team-model/model")
        forged = await client.put(
            "/api/session/team-model/agent-config",
            json={
                "executor": "team",
                "team": {
                    "external_team_id": team["id"],
                    "member_model_bindings": {
                        leader["id"]: {"model_id": "model-a", "revision": 99},
                    },
                    "model_binding_revision": 99,
                },
            },
        )
        switched = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-a",
                "expected_revision": 1,
            },
        )
        switched_team = crew.team._get_or_create(
            "team-model",
            external_team_id=team["id"],
            owner_account_id=OWNER_A,
        )
        switched_profile = switched_team.member_profiles["leader"]
        selected = await client.get(
            f"/api/session/team-model/model?member_id={leader['id']}"
        )
        other_session = await client.get(
            f"/api/session/team-model-other/model?member_id={leader['id']}"
        )
        stale = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-b",
                "expected_revision": 1,
            },
        )
        restored = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-b",
                "expected_revision": 2,
                "restore_default": True,
            },
        )
        # Dispatcher 表示 Team 当前仍有一轮在执行；此时只要目标成员空闲，
        # 仍可更新它下一轮任务的模型。旧的 session-level busy guard 会误拒绝。
        monkeypatch.setattr(
            crew.dispatcher,
            "status",
            lambda *_args, **_kwargs: {"live": "running"},
        )
        member_switched_while_leader_busy = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": member["id"],
                "model_profile_id": "model-a",
                "expected_revision": 3,
            },
        )
        crew.team._mark_child_active({
            "child_id": "task-model::member",
            "parent_session_id": "team-model",
            "owner_account_id": OWNER_A,
            "member": member["name"],
            "agent": object(),
        })
        busy_member = await client.get(
            f"/api/session/team-model/model?member_id={member['id']}"
        )
        blocked_member_switch = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": member["id"],
                "model_profile_id": "model-b",
                "expected_revision": 4,
            },
        )
        crew.team._mark_child_done("team-model", "task-model::member", OWNER_A)

    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["scope"] == "team"
    assert initial_body["model_binding_revision"] == 1
    assert {item["member_id"] for item in initial_body["members"]} == {leader["id"], member["id"]}
    assert {item["model_profile_id"] for item in initial_body["members"]} == {"model-b"}
    assert all(item["model_switchable"] for item in initial_body["members"])
    assert forged.status_code == 200
    assert forged.json()["team"]["model_binding_revision"] == 1
    assert forged.json()["team"]["member_model_bindings"][leader["id"]]["model_id"] == "model-b"

    assert switched.status_code == 200
    assert switched.json()["scope"] == "team_member"
    assert switched.json()["model_profile_id"] == "model-a"
    assert switched_profile.model["id"] == "model-a"
    assert switched.json()["model_binding_revision"] == 2
    assert selected.json()["model_profile_id"] == "model-a"
    assert other_session.json()["model_profile_id"] == "model-b"
    assert stale.status_code == 409
    assert stale.json()["code"] == "model_binding_stale"
    assert restored.status_code == 200
    assert restored.json()["model_profile_id"] == "model-b"
    assert restored.json()["binding_source"] == "restored_from_agent_default"
    assert restored.json()["model_binding_revision"] == 3
    assert crew.external_agents.get_agent(leader["id"], owner_account_id=OWNER_A)["model"] == "model-b"
    assert member_switched_while_leader_busy.status_code == 200
    assert member_switched_while_leader_busy.json()["model_binding_revision"] == 4
    assert busy_member.status_code == 200
    assert busy_member.json()["status"] == "running"
    assert busy_member.json()["active_task_count"] == 1
    assert blocked_member_switch.status_code == 409
    assert blocked_member_switch.json()["code"] == "member_busy"

    stored = crew.session_store.get_agent_config("team-model", owner_account_id=OWNER_A)
    assert set(stored["team"]["member_model_bindings"]) == {leader["id"], member["id"]}
    assert stored["team"]["member_model_bindings"][member["id"]]["model_id"] == "model-a"
    with sqlite3.connect(db_path) as conn:
        envelope = json.loads(conn.execute(
            "SELECT profile_json FROM external_agent WHERE id = ?",
            (leader["id"],),
        ).fetchone()[0])
    assert set(envelope["model_overlays"]) == {"model-a", "model-b"}

    rebuilt = crew.team._build_team(
        "team-model",
        external_team_id=team["id"],
        owner_account_id=OWNER_A,
    )
    assert rebuilt.leader.executor.config.model == "model-b"


@pytest.mark.asyncio
async def test_team_builtin_member_model_switch_uses_bound_profile(tmp_path, auth_headers, monkeypatch):
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        cron_enabled=False,
        active_model_id="builtin-b",
        default_model_id="builtin-b",
        model_profiles={
            "builtin-a": ModelProfile(
                id="builtin-a",
                name="Builtin A",
                api_key="test-a-key",
                model="builtin-a-model",
                builtin=True,
            ),
            "builtin-b": ModelProfile(
                id="builtin-b",
                name="Builtin B",
                api_key="test-b-key",
                model="builtin-b-model",
                builtin=True,
            ),
        },
    )
    crew = build_app(config=cfg, enable_team=True)
    # Gateway owner 的模型目录来自登录 overlay；测试中直接提供可用 profile，
    # 以验证 Team 内置成员的绑定不会退回全局默认 Provider。
    profiles = dict(cfg.model_profiles)
    monkeypatch.setattr(crew, "owner_model_profiles", lambda _owner: profiles)
    monkeypatch.setattr(crew.config, "owner_default_model_id", lambda _owner: "builtin-b")
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Builtin Model Team",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[{"agent_id": CREW_BUILTIN_AGENT_ID, "role": "Leader"}],
    )
    crew.session_store.ensure_session("team-builtin-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-builtin-model",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        initial = await client.get("/api/session/team-builtin-model/model")
        switched = await client.put(
            "/api/session/team-builtin-model/model",
            json={
                "member_id": CREW_BUILTIN_AGENT_ID,
                "model_profile_id": "builtin-a",
                "expected_revision": 1,
            },
        )

    assert initial.status_code == 200
    initial_member = initial.json()["members"][0]
    assert initial_member["member_id"] == CREW_BUILTIN_AGENT_ID
    assert initial_member["model_profile_id"] == "builtin-b"
    assert initial_member["model_switchable"] is True
    assert switched.status_code == 200
    assert switched.json()["model_profile_id"] == "builtin-a"
    assert switched.json()["runtime_id"] == "builtin"

    rebuilt = crew.team._build_team(
        "team-builtin-model",
        external_team_id=team["id"],
        owner_account_id=OWNER_A,
    )
    assert rebuilt.leader.executor.provider.model == "builtin-a-model"


@pytest.mark.asyncio
async def test_team_model_switch_rejects_pending_hard_execution_requirement(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=True)
    runtime = crew.external_agents.upsert_runtime({
        "id": "team-hard-requirement-runtime",
        "provider": "custom",
        "name": "Hard Requirement Runtime",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "model-a",
            "runtime_capabilities": {"model_switch": True, "session_resume": True},
            "models": [
                {"id": "model-a", "label": "Model A", "capabilities": ["tools"]},
                {"id": "model-b", "label": "Model B", "capabilities": ["text"]},
            ],
        },
    })
    leader = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Hard Requirement Leader",
        runtime_id=runtime["id"],
        model="model-a",
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Hard Requirement Team",
        leader_agent_id=leader["id"],
        members=[{"agent_id": leader["id"], "role": "Leader"}],
    )
    crew.session_store.ensure_session("team-hard", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-hard",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        initial = await client.get("/api/session/team-hard/model")
        crew.team._get_or_create(
            "team-hard",
            external_team_id=team["id"],
            owner_account_id=OWNER_A,
        )
        crew.team._plans[(OWNER_A, "team-hard")] = TeamPlan(
            team_session_id="team-hard",
            goal="完成需要工具的节点",
            nodes={
                "build": TeamPlanNode(
                    node_id="build",
                    title="工具执行",
                    assignee="leader",
                    metadata={"execution_requirements": {"tools": True}},
                ),
            },
        )
        switched = await client.put(
            "/api/session/team-hard/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-b",
                "expected_revision": 1,
            },
        )

    assert initial.status_code == 200
    assert switched.status_code == 409
    assert switched.json()["code"] == "pending_work_incompatible"
    assert switched.json()["incompatible_nodes"] == [{
        "node_id": "build",
        "title": "工具执行",
        "missing": ["tools"],
    }]


@pytest.mark.asyncio
async def test_internal_interaction_api_rejects_missing_or_invalid_token(api):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        missing = await client.post(
            "/api/internal/interactions/ask",
            json={"questions": [{"question": "?", "options": ["A"]}]},
        )
        invalid = await client.post(
            "/api/internal/interactions/ask",
            headers={"Authorization": "Bearer invalid"},
            json={"questions": [{"question": "?", "options": ["A"]}]},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_internal_interaction_api_returns_followup_answer(tmp_path, monkeypatch):
    from crew.core.followup import resolve_answer
    from crew.gateway.interaction_bridge import interaction_bridge

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.active_owner.claim(OWNER_A)
    app = create_app(crew)

    async def push(session_id, payload, *, owner_account_id):
        assert owner_account_id == OWNER_A
        resolve_answer(
            session_id,
            payload["body"]["question_id"],
            [{"question_id": "q1", "answers": ["B"]}],
        )

    interaction_bridge.configure(
        push_fn=push,
        gateway_url="http://127.0.0.1:8000",
    )
    binding = interaction_bridge.create_binding(
        owner_account_id=OWNER_A,
        display_session_id="main",
        origin_session_id="main::codex",
        agent_name="Codex",
        ttl_seconds=30,
    )
    assert binding is not None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/internal/interactions/ask",
            headers={"Authorization": f"Bearer {binding.token}"},
            json={
                "title": "选择",
                "questions": [{
                    "id": "q1",
                    "question": "选哪个？",
                    "options": ["A", "B"],
                    "multiSelect": False,
                }],
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["answers"] == [
        {"question_id": "q1", "answers": ["B"]},
    ]


@pytest.mark.asyncio
async def test_feishu_challenge(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/feishu/events", json={"challenge": "abc123"})
    # feishu channel 未启用时 503；启用时返回 challenge
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert resp.json().get("challenge") == "abc123"


@pytest.mark.asyncio
async def test_cron_run_now_creates_manual_fire_without_changing_schedule(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=True)
    crew = build_app(config=cfg, enable_team=False)
    job = crew.cron_store.create(
        name="run-now",
        schedule="every 1h",
        query="ping",
        session_id="s1",
        owner_account_id=OWNER_A,
    )
    crew.session_store.save("s1", [Message.user("cron")], owner_account_id=OWNER_A)
    crew.cron_store.set_enabled(job["id"], False, owner_account_id=OWNER_A)
    before = crew.cron_store.get(job["id"], owner_account_id=OWNER_A)
    app = create_app(crew)
    await crew.cron_service.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(f"/api/cron/jobs/{job['id']}/run")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["job"]["enabled"] is False
    assert data["job"]["last_status"] == ""
    for _ in range(50):
        await asyncio.sleep(0.01)
        runs = crew.cron_store.get_job_runs(job["id"])
        if runs and runs[0]["status"] != "running":
            break
    refreshed = crew.cron_store.get(job["id"], owner_account_id=OWNER_A)
    assert refreshed["next_run_at"] == before["next_run_at"]
    assert refreshed["enabled"] == before["enabled"]
    assert len(runs) == 1
    assert runs[0]["fire_kind"] == "manual"
    await crew.shutdown()


@pytest.mark.asyncio
async def test_external_session_binding_is_owner_validated_and_projected(
    tmp_path,
    auth_headers,
):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    runtime = crew.external_agents.upsert_runtime({
        "id": "runtime-session-binding",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "/bin/codex",
        "protocol": "cli",
        "metadata": {"availability_status": "ready"},
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Codex 外援",
        runtime_id=runtime["id"],
        model="gpt-test",
    )
    other_owner_agent = crew.external_agents.create_agent(
        owner_account_id="B:uid-b",
        name="其他账号外援",
        runtime_id=runtime["id"],
        model="gpt-test",
    )
    degraded_runtime = crew.external_agents.upsert_runtime({
        "id": "runtime-session-binding-degraded",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": "/bin/claude",
        "protocol": "cli",
        "metadata": {"availability_status": "degraded"},
    })
    degraded_agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="不可用外援",
        runtime_id=degraded_runtime["id"],
        model="claude-test",
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="研发外援团",
        leader_agent_id=agent["id"],
        members=[
            {
                "agent_id": agent["id"],
                "role": "Leader",
                "role_key": "tech_lead",
                "role_label": "技术负责人",
            },
        ],
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        missing = await client.put(
            "/api/session/missing-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": "missing-agent"},
            },
        )
        cross_owner = await client.put(
            "/api/session/cross-owner-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": other_owner_agent["id"]},
            },
        )
        degraded = await client.put(
            "/api/session/degraded-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": degraded_agent["id"]},
            },
        )
        agent_bound = await client.put(
            "/api/session/agent-bound/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": agent["id"]},
            },
        )
        team_bound = await client.put(
            "/api/session/team-bound/agent-config",
            json={
                "executor": "team",
                "team": {"external_team_id": team["id"]},
            },
        )
        listed = await client.get("/api/sessions")

    assert missing.status_code == 404
    assert cross_owner.status_code == 404
    assert degraded.status_code == 409
    assert agent_bound.status_code == 200
    assert team_bound.status_code == 200
    rows = {row["session_id"]: row for row in listed.json()}
    assert "missing-external" not in rows
    assert "cross-owner-external" not in rows
    assert "degraded-external" not in rows
    assert rows["agent-bound"]["agent_binding"] == {
        "kind": "external_agent",
        "id": agent["id"],
    }
    assert rows["team-bound"]["agent_binding"] == {
        "kind": "external_team",
        "id": team["id"],
    }


@pytest.mark.asyncio
async def test_cron_retry_creates_linked_fire_without_replaying_source(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=True)
    crew = build_app(config=cfg, enable_team=False)
    job = crew.cron_store.create(
        name="retry",
        schedule="every 1h",
        query="ping",
        session_id="s-retry",
        owner_account_id=OWNER_A,
    )
    crew.session_store.save("s-retry", [Message.user("cron")], owner_account_id=OWNER_A)
    source = crew.cron_store.claim_manual_fire(job["id"], owner_account_id=OWNER_A)
    assert source is not None
    assert crew.cron_store.finish_job_run(source["id"], "failed", "boom") is True
    app = create_app(crew)
    await crew.cron_service.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(f"/api/cron/fires/{source['id']}/retry")

    assert resp.status_code == 202
    assert resp.json()["source_fire_id"] == source["id"]
    for _ in range(50):
        await asyncio.sleep(0.01)
        runs = crew.cron_store.get_job_runs(job["id"])
        if len(runs) == 2 and runs[0]["status"] != "running":
            break
    assert len(runs) == 2
    assert runs[0]["fire_kind"] == "retry"
    assert runs[0]["retry_of_fire_id"] == source["id"]
    assert runs[0]["status"] == "completed"
    assert runs[1]["id"] == source["id"]
    assert runs[1]["status"] == "failed"
    await crew.shutdown()


@pytest.mark.asyncio
async def test_unified_task_api_list_wait_cancel(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=False)
    crew.session_store.save("s1", [Message.user("task")], owner_account_id=OWNER_A)
    task = crew.tasks.create_runtime(
        kind="team",
        session_id="s1",
        title="api task",
        owner_account_id=OWNER_A,
    )
    crew.tasks.mark_running(task["task_id"])
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": "s1"})
        assert listed.status_code == 200
        assert listed.json()[0]["task_id"] == task["task_id"]

        waited = await client.post(
            f"/api/tasks/{task['task_id']}/wait",
            json={"timeout": 0.01},
        )
        assert waited.status_code == 200
        assert waited.json()["retrieval_status"] == "timeout"

        cancelled = await client.post(
            f"/api/tasks/{task['task_id']}/cancel",
            json={"reason": "test"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_team_task_api_ignores_legacy_turn_child_tasks_without_workflow(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False, gateway_dev_mode=False)
    crew = build_app(config=cfg, enable_team=False)
    parent = "web_team_parent"
    child = f"{parent}::turn::req_abc"
    crew.session_store.save(parent, [Message.user("开发一个贪吃蛇游戏")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    parent_task = crew.tasks.create_runtime(
        kind="agent_turn", session_id=parent, title="开发一个贪吃蛇游戏", owner_account_id=OWNER_A
    )
    team_task = crew.tasks.create_runtime(
        kind="team", session_id=child, title="实现：贪吃蛇小游戏", owner_account_id=OWNER_A
    )
    crew.tasks.finish(
        parent_task["task_id"],
        owner_account_id=OWNER_A,
        result="团队工作流完成",
    )
    crew.tasks.finish(
        team_task["task_id"],
        owner_account_id=OWNER_A,
        result="成员完成开发",
    )
    other_task = crew.tasks.create_runtime(
        kind="team", session_id="other", title="不应出现", owner_account_id=OWNER_A
    )
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})
        history = await client.get(f"/api/session/{parent}")

    assert listed.status_code == 200
    titles = [item["title"] for item in listed.json()]
    assert titles == ["开发一个贪吃蛇游戏"]
    ids = [item["task_id"] for item in listed.json()]
    assert parent_task["task_id"] in ids
    assert team_task["task_id"] not in ids
    assert other_task["task_id"] not in ids
    assert history.status_code == 200
    assert any(item["role"] == "assistant" and "团队工作流完成" in item["content"] for item in history.json())
    assert any("成员完成开发" in item["content"] for item in history.json())


@pytest.mark.asyncio
async def test_team_task_api_projects_team_plan_nodes(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False, gateway_dev_mode=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_plan_parent"
    crew.session_store.save(parent, [Message.user("写一个像素游戏")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        parent,
        goal="写一个像素游戏",
        nodes=[
            {"id": "leader_plan", "title": "Leader 承接任务", "assignee": "leader"},
            {
                "id": "dev",
                "title": "开发像素游戏",
                "assignee": "coder",
                "metadata": {
                    "workflow_lane": "build",
                    "display_order": 41,
                    "role_label": "自定义开发主责",
                },
            },
            {"id": "verify", "title": "验证像素游戏", "assignee": "researcher"},
            {"id": "leader_summary", "title": "Leader 汇总结论", "assignee": "leader"},
        ],
        edges=[
            ["leader_plan", "dev"],
            ["dev", "verify"],
            ["verify", "leader_summary"],
        ],
        owner_account_id=OWNER_A,
    )
    delegate_task = crew.tasks.create_runtime(
        kind="team",
        session_id=parent,
        title="开发像素游戏",
        owner_account_id=OWNER_A,
    )
    crew.tasks.touch_activity(delegate_task["task_id"], {"plan_node_id": "dev"})
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    assert [row["title"] for row in rows] == [
        "Leader 承接任务",
        "开发像素游戏",
        "验证像素游戏",
        "Leader 汇总结论",
    ]
    assert delegate_task["task_id"] not in [row["task_id"] for row in rows]
    assert [row["progress"]["source"] for row in rows] == ["team_kanban"] * 4
    by_task_id = {row["task_id"]: row for row in rows}
    by_plan_node_id = {row["progress"]["plan_node_id"]: row for row in rows}
    assert [row["progress"]["workflow_lane"] for row in rows] == ["lead", "build", "verify", "summary"]
    assert [row["progress"]["display_order"] for row in rows] == [10, 41, 50, 80]
    assert by_plan_node_id["dev"]["progress"]["role_label"] == "自定义开发主责"

    def dependency_plan_ids(row, key: str) -> list[str]:
        return [
            by_task_id[task_id]["progress"]["plan_node_id"]
            for task_id in row["progress"][key]
        ]

    assert dependency_plan_ids(by_plan_node_id["leader_plan"], "child_node_ids") == ["dev"]
    assert dependency_plan_ids(by_plan_node_id["dev"], "parent_node_ids") == ["leader_plan"]
    assert dependency_plan_ids(by_plan_node_id["verify"], "parent_node_ids") == ["dev"]
    assert dependency_plan_ids(by_plan_node_id["leader_summary"], "parent_node_ids") == ["verify"]
    assert {row["progress"]["turn_session_id"] for row in rows} == {parent}
    assert {row["progress"]["turn_title"] for row in rows} == {"写一个像素游戏"}


@pytest.mark.asyncio
async def test_team_task_api_keeps_direct_turn_separate_and_hides_shell(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_mixed_parent"
    turn = f"{parent}::turn::req_test"
    crew.session_store.save(parent, [Message.user("你好"), Message.user("测试")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        turn,
        goal="测试",
        nodes=[
            {"id": "leader_plan", "title": "Leader 承接任务", "assignee": "leader"},
            {"id": "test_plan", "title": "测试设计：测试", "assignee": "kk"},
        ],
        edges=[["leader_plan", "test_plan"]],
        owner_account_id=OWNER_A,
    )
    direct = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你好",
        detail="你好",
        owner_account_id=OWNER_A,
    )
    team_wrapper = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="测试",
        detail="测试",
        owner_account_id=OWNER_A,
    )
    delegate = crew.tasks.create_runtime(
        kind="team",
        session_id=turn,
        title="测试设计：测试",
        detail="测试设计：测试",
        owner_account_id=OWNER_A,
    )
    shell = crew.tasks.create_runtime(
        kind="shell",
        session_id=turn,
        title="npm test",
        detail="npm test",
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(direct["task_id"], owner_account_id=OWNER_A, result="你好！")
    crew.tasks.update_status(team_wrapper["task_id"], "cancelled", "已停止当前回复")
    crew.tasks.touch_activity(delegate["task_id"], {"plan_node_id": "test_plan"})
    crew.tasks.finish(shell["task_id"], owner_account_id=OWNER_A, result="shell output")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    ids = {row["task_id"] for row in rows}
    assert direct["task_id"] in ids
    assert team_wrapper["task_id"] not in ids
    assert shell["task_id"] not in ids
    assert {row["progress"]["turn_title"] for row in rows} == {"你好", "测试"}
    assert len({row["progress"]["turn_session_id"] for row in rows}) == 2


@pytest.mark.asyncio
async def test_team_task_api_hides_parent_turn_when_plan_has_no_delegate_tasks(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_leader_only_parent"
    request_id = "req_question"
    turn = f"{parent}::turn::{request_id}"
    prompt = "刚刚项目从头到尾做完用了多长时间"
    crew.session_store.save(parent, [Message.user(prompt)], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        turn,
        goal=prompt,
        nodes=[
            {"id": "leader_plan", "title": f"Leader 拆分任务：{prompt}", "assignee": "leader"},
            {"id": "leader_summary", "title": f"Leader 汇总：{prompt}", "assignee": "leader"},
        ],
        edges=[["leader_plan", "leader_summary"]],
        owner_account_id=OWNER_A,
    )
    parent_task = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        request_id=request_id,
        title=prompt,
        detail=prompt,
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(parent_task["task_id"], owner_account_id=OWNER_A, result="完成")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    assert parent_task["task_id"] not in {row["task_id"] for row in rows}
    assert [row["title"] for row in rows] == [
        f"Leader 拆分任务：{prompt}",
        f"Leader 汇总：{prompt}",
    ]


@pytest.mark.asyncio
async def test_team_session_history_uses_visible_parent_turns_only(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_visible_history"
    turn = f"{parent}::turn::req_test"
    runtime = crew.external_agents.upsert_runtime({
        "id": "rt-team-history",
        "provider": "test",
        "name": "Test Runtime",
        "executable_path": "/bin/test",
    })
    hh = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="hh",
        runtime_id=runtime["id"],
    )
    kk = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="kk",
        runtime_id=runtime["id"],
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="测试团队",
        leader_agent_id=hh["id"],
        members=[
            {"agent_id": hh["id"], "role": "Leader", "role_key": "tech_lead", "role_label": "leader"},
            {"agent_id": kk["id"], "role": "测试", "role_key": "qa_engineer", "role_label": "测试"},
        ],
    )
    crew.session_store.save(parent, [Message.user("内部恢复消息", is_meta=True)], owner_account_id=OWNER_A)
    crew.session_store.save(
        f"{turn}::leader",
        [Message.user("测试设计：你能帮我测试一下我的贪吃蛇游戏么"), Message.assistant("Leader 内部执行日志")],
        owner_account_id=OWNER_A,
    )
    crew.session_store.save(
        f"{turn}::kk",
        [Message.user("测试执行：你能帮我测试一下我的贪吃蛇游戏么"), Message.assistant("kk 内部执行日志")],
        owner_account_id=OWNER_A,
    )
    crew.session_store.set_agent_config(
        parent,
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    first = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你好",
        detail="你好",
        owner_account_id=OWNER_A,
    )
    second = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你能帮我测试一下我的贪吃蛇游戏么",
        detail="你能帮我测试一下我的贪吃蛇游戏么",
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(first["task_id"], owner_account_id=OWNER_A, result="你好！")
    crew.tasks.update_status(second["task_id"], "cancelled", "已停止当前回复")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        history = await client.get(f"/api/session/{parent}")

    assert history.status_code == 200
    rows = history.json()
    user_messages = [row["content"] for row in rows if row["role"] == "user"]
    internal_messages = [row for row in rows if row["role"] == "team_internal"]
    assert user_messages == ["你好", "你能帮我测试一下我的贪吃蛇游戏么"]
    assert all("测试设计：" not in row["content"] for row in rows)
    assert [row["content"] for row in internal_messages] == ["Leader 内部执行日志", "kk 内部执行日志"]
    assert internal_messages[0]["agent_id"] == hh["id"]
    assert internal_messages[0]["agent_name"] == "hh"
    assert internal_messages[0]["agent_role"] == "leader"
    assert internal_messages[0]["agent_tone"] == 0
    assert internal_messages[0]["is_leader"] is True
    assert internal_messages[1]["agent_id"] == kk["id"]
    assert internal_messages[1]["agent_name"] == "kk"
    assert internal_messages[1]["agent_role"] == "测试"
    assert internal_messages[1]["agent_tone"] == 1
    assert internal_messages[1]["is_leader"] is False


@pytest.mark.asyncio
async def test_team_task_api_does_not_synthesize_legacy_delegate_fallback(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_fallback_title"
    turn = f"{parent}::turn::req_test"
    crew.session_store.save(parent, [Message.user("你能帮我测试一下我的贪吃蛇游戏么")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.tasks.create_runtime(
        kind="team",
        session_id=turn,
        title="测试设计：你能帮我测试一下我的贪吃蛇游戏么",
        detail="测试设计：你能帮我测试一下我的贪吃蛇游戏么\n\n并行准备测试路径",
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    assert listed.json() == []
