"""Stage 1 formation contracts for optional goals and explicit membership."""

import pytest
from httpx import ASGITransport, AsyncClient

from crew.team.formation import apply_formation_ai_audit, build_agent_profile, confirmed_formation_plan, fast_team_suggestion, formation_auto_decision
from crew.collaboration.formation.recommendations import apply_recommendations
from crew.collaboration.formation.assessment import assess_roster


def _agent(agent_id: str, *, available: bool = True) -> dict:
    agent = {"id": agent_id, "name": agent_id, "provider": "kimi", "model": "m"}
    agent["profile"] = build_agent_profile(agent).to_dict()
    if not available:
        agent["profile"]["availability"] = "unavailable"
    return agent


def test_responsibility_recommendation_preserves_user_duty_and_does_not_fabricate_capabilities():
    duty = '核对材料来源及事实'
    payload = {'name': '比赛团队', 'member_assignments': [{'agent_id': 'a', 'responsibility': duty}]}
    baseline = fast_team_suggestion(payload, [_agent('a')])
    result, reason = apply_recommendations(baseline, {'members': [{
        'agent_id': 'a', 'role_key': 'independent_reviewer', 'responsibility': '模型改写',
    }]}, team_goal='比赛团队', agent_names={'a': 'A'}, user_duties={'a': duty})
    assert result is not None, reason
    member = next(item for item in result['members'] if item['agent_id'] == 'a')
    planned = next(item for item in result['formation_plan']['members'] if item['agent_id'] == 'a')
    assert member['role_key'] == planned['role_key'] == 'independent_reviewer'
    assert member['responsibility']['mission'] == planned['responsibility']['mission'] == duty
    assert member['assigned_capabilities'] == []
    assert planned['responsibility_source'] == 'user'


def test_responsibility_recommendation_rejects_missing_extra_and_unknown_members():
    baseline = fast_team_suggestion({'name': '团队', 'required_agent_ids': ['a', 'b']}, [_agent('a'), _agent('b')])
    for proposed in [[], [{'agent_id': 'a', 'role_key': 'general_collaborator', 'responsibility': '协作'}],
                     [{'agent_id': 'missing', 'role_key': 'general_collaborator', 'responsibility': '协作'}]]:
        result, reason = apply_recommendations(baseline, {'members': proposed}, team_goal='团队', agent_names={}, user_duties={})
        assert result is None
        assert reason in {'invalid_ai_output', 'incomplete_ai_output'}


def test_capability_assessment_is_read_only_and_unknown_is_not_missing():
    agent = _agent('a')
    members = [{'agent_id': 'a', 'role_key': 'independent_reviewer', 'assigned_capabilities': []}]
    before = repr(members)
    result = assess_roster(members, [agent], [])
    assert repr(members) == before
    assert 'review' in result['coverage']['unverified']
    assert result['coverage']['uncovered'] == []
    with pytest.raises(ValueError):
        assess_roster([{'agent_id': 'other'}], [agent], [])


def test_recommendation_targets_preserve_explicit_role():
    baseline = fast_team_suggestion({'name': '团队', 'required_agent_ids': ['a']}, [_agent('a')])
    result, reason = apply_recommendations(baseline, {'members': [{
        'agent_id': 'a', 'role_key': 'qa_engineer', 'responsibility': '核对结果',
    }]}, team_goal='团队', agent_names={'a': 'A'}, user_duties={},
        target_ids={'a'}, user_roles={'a': 'independent_reviewer'})
    assert result is None
    assert reason == 'invalid_ai_output'


def test_name_only_recommends_an_available_member_without_fabricating_capabilities():
    result = fast_team_suggestion({"name": "我的团队"}, [_agent("b"), _agent("a")])
    members = {member["agent_id"]: member for member in result["members"]}
    assert set(members) == {"crew::builtin", "a"}
    assert members["a"]["assigned_capabilities"] == []
    assert members["a"]["role_key"] == "unclassified"
    assert "待验证" in members["a"]["selection_reason"]


def test_team_name_alone_does_not_assign_a_role_before_responsibilities():
    result = fast_team_suggestion({"name": "项目开发小队"}, [_agent("a")])
    member = next(member for member in result["members"] if member["agent_id"] == "a")
    assert member["role_key"] == "unclassified"
    assert member["assigned_capabilities"] == []
    plan_member = next(member for member in result["formation_plan"]["members"] if member["agent_id"] == "a")
    assert plan_member["responsibility_source"] == "system"
    assert plan_member["assigned_capabilities"] == []
    assert "member_responsibility_unresolved" in formation_auto_decision({"name": "项目开发小队"}, result)[1]


def test_ai_can_assign_a_role_after_suggesting_a_responsibility_for_name_only_team():
    payload = {"name": "项目开发小队"}
    agents = [_agent("a")]
    baseline = fast_team_suggestion(payload, agents)
    audit = {"member_changes": {"upsert_members": [{
        "agent_id": "a", "role_key": "fullstack_developer",
        "responsibility_focus": "根据团队目标实现功能并自检",
        "assigned_capabilities": [], "reason": "根据团队名称推测，待用户确认",
    }]}}
    result, reason = apply_formation_ai_audit(payload, agents, baseline, audit, [])
    assert result is not None, reason
    member = next(item for item in result["members"] if item["agent_id"] == "a")
    assert member["role_key"] == "fullstack_developer"
    assert member["responsibility"]["mission"] == "根据团队目标实现功能并自检"
    assert "待用户确认" in member["selection_reason"]


def test_ai_cannot_assign_a_specific_role_to_an_unclassified_member_without_a_duty():
    payload = {"name": "项目开发小队"}
    agents = [_agent("a")]
    baseline = fast_team_suggestion(payload, agents)
    audit = {"member_changes": {"upsert_members": [{
        "agent_id": "a", "role_key": "fullstack_developer", "responsibility_focus": "",
    }]}}
    result, _ = apply_formation_ai_audit(payload, agents, baseline, audit, [])
    if result is not None:
        member = next(item for item in result["members"] if item["agent_id"] == "a")
        assert member["role_key"] == "unclassified"


def test_name_only_excludes_unavailable_and_user_excluded_members():
    result = fast_team_suggestion(
        {"name": "我的团队", "excluded_agent_ids": ["a"]},
        [_agent("a"), _agent("b", available=False), _agent("c")],
    )
    assert {member["agent_id"] for member in result["members"]} == {"crew::builtin", "c"}


def test_required_member_is_kept_without_a_capability_requirement():
    result = fast_team_suggestion({"name": "我的团队", "required_agent_ids": ["a"]}, [_agent("a")])
    assert {member["agent_id"] for member in result["members"]} == {"crew::builtin", "a"}
    member = next(item for item in result["formation_plan"]["members"] if item["agent_id"] == "a")
    assert member["locked"] is True


def test_required_member_is_not_filtered_out_by_low_capability_evidence():
    agent = {"id": "a", "name": "A", "provider": "kimi", "model": "m", "capabilities": {"design": 0.1}}
    result = fast_team_suggestion(
        {"name": "设计团队", "required_agent_ids": ["a"], "required_capabilities": ["design"]},
        [agent],
    )
    assert result["decision_required"] is False
    assert "a" in {member["agent_id"] for member in result["members"]}
    assert "design" not in result["formation_plan"]["coverage"]["covered"]


def test_missing_required_member_is_reported_instead_of_silently_removed():
    result = fast_team_suggestion({"name": "我的团队", "required_agent_ids": ["missing"]}, [])
    assert result["decision_required"] is True
    assert result["required_agent_conflicts"][0]["agent_id"] == "missing"


def test_required_member_cannot_be_silently_excluded():
    result = fast_team_suggestion(
        {"name": "我的团队", "required_agent_ids": ["a"], "excluded_agent_ids": ["a"]},
        [_agent("a")],
    )
    assert result["decision_required"] is True
    assert "同时" in result["required_agent_conflicts"][0]["reason"]


def test_unavailable_leader_requires_user_to_choose_again():
    result = fast_team_suggestion({"name": "我的团队", "leader_agent_id": "a"}, [_agent("a", available=False)])
    assert result["decision_required"] is True
    assert result["required_agent_conflicts"][0]["agent_id"] == "a"


def test_user_responsibility_is_preserved_verbatim():
    result = fast_team_suggestion(
        {"name": "比赛团队", "member_assignments": [{"agent_id": "a", "responsibility": "核对评分标准并标明来源"}]},
        [_agent("a")],
    )
    member = next(item for item in result["members"] if item["agent_id"] == "a")
    assert member["role"] == "核对评分标准并标明来源"
    plan_member = next(item for item in result["formation_plan"]["members"] if item["agent_id"] == "a")
    assert plan_member["responsibility_source"] == "user"
    assert plan_member["responsibility"]["mission"] == member["role"]


def test_catalog_rechecks_runtime_availability_without_changing_stored_profile(tmp_path):
    from crew.gateway.routers.runtimes import _formation_agent_catalog

    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    agent = {**_agent("a"), "runtime_id": "rt"}

    class Store:
        def list_teams(self, **kwargs):
            return []

        def list_agents(self, **kwargs):
            return [agent]

        def list_runtimes(self):
            return [{"id": "rt", "executable_path": str(executable),
                     "metadata": {"availability_status": "ready"}}]

    assert _formation_agent_catalog(Store(), owner_account_id="owner")[0]["profile"]["availability"] == "ready"
    executable.unlink()
    assert _formation_agent_catalog(Store(), owner_account_id="owner")[0]["profile"]["availability"] == "unavailable"
    assert agent["profile"]["availability"] == "ready"


def test_changing_role_does_not_replace_the_users_responsibility():
    result = confirmed_formation_plan(
        leader_agent_id="crew::builtin",
        members=[{"agent_id": "a", "role_key": "technical_writer", "role": "负责审核评分标准"}],
        existing={"members": [{"agent_id": "a", "role_key": "research_analyst",
                               "responsibility_source": "user",
                               "responsibility": {"mission": "负责审核评分标准"}}]},
    )
    assert result["members"][0]["responsibility"]["mission"] == "负责审核评分标准"
    assert result["members"][0]["responsibility_source"] == "user"


def test_confirmed_plan_keeps_original_user_duty_after_adopting_suggestion():
    result = confirmed_formation_plan(
        leader_agent_id="crew::builtin",
        members=[{"agent_id": "a", "role_key": "independent_reviewer", "role": "审阅建议"}],
        existing={"members": [{"agent_id": "a", "role_key": "independent_reviewer",
                               "responsibility_source": "user", "responsibility": {"mission": "审阅建议"},
                               "original_user_responsibility": "用户原职责"}]},
    )
    assert result["members"][0]["original_user_responsibility"] == "用户原职责"


@pytest.mark.asyncio
async def test_suggestion_and_team_creation_preserve_user_duty(tmp_path, monkeypatch, auth_headers):
    from crew.app import build_app
    from crew.gateway.server import create_app

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(enable_team=False)
    monkeypatch.setattr(app, "_session_demo_mode", lambda *_: False)
    app.external_agents.upsert_runtime({"id": "rt", "type": "claude", "provider": "anthropic"})
    agent = app.external_agents.create_agent(owner_account_id="A:uid-a", name="A", runtime_id="rt", model="m")
    candidate = {**agent, "profile": _agent(agent["id"])["profile"]}
    monkeypatch.setattr("crew.gateway.routers.runtimes._formation_agent_catalog", lambda *args, **kwargs: [candidate])
    async with AsyncClient(transport=ASGITransport(app=create_app(app)), base_url="http://test", headers=auth_headers) as client:
        response = await client.post("/api/external-teams/suggest", json={
            "name": "比赛团队", "formation_mode": "fast",
            "member_assignments": [{"agent_id": agent["id"], "responsibility": "核对评分标准并标明来源"}],
        })
        assert response.status_code == 200
        suggestion = response.json()
        response = await client.post("/api/external-teams", json={
            "name": "比赛团队", "leader_agent_id": suggestion["leader_agent_id"],
            "members": suggestion["members"], "formation_plan": suggestion["formation_plan"],
        })
        assert response.status_code == 200, response.text
        team = response.json()
        assert not team.get("description")
        member = next(item for item in team["formation_plan"]["members"] if item["agent_id"] == agent["id"])
        assert member["responsibility"]["mission"] == "核对评分标准并标明来源"
        assert member["responsibility_source"] == "user"


@pytest.mark.asyncio
async def test_unconfigured_builtin_leader_cannot_create_a_false_ready_team(tmp_path, monkeypatch, auth_headers):
    from crew.app import build_app
    from crew.gateway.server import create_app

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(enable_team=False)
    monkeypatch.setattr(app, "_session_demo_mode", lambda *_: True)
    async with AsyncClient(transport=ASGITransport(app=create_app(app)), base_url="http://test", headers=auth_headers) as client:
        response = await client.post("/api/external-teams/suggest", json={
            "name": "我的团队", "formation_mode": "fast",
        })
        assert response.status_code == 409
        assert response.json()["code"] == "builtin_model_unavailable"
        response = await client.post("/api/external-teams", json={
            "name": "我的团队", "leader_agent_id": "crew::builtin",
            "members": [{"agent_id": "crew::builtin", "role": "协调"}],
        })
        assert response.status_code == 409
