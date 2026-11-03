"""Planning preview invariants, independent of the legacy scheduler."""
import json
from types import SimpleNamespace

import pytest

from crew.collaboration.planning.plan import PlanningError, make_plan, validate_work_units
from crew.collaboration.planning.service import PlanningService
from crew.collaboration.planning.store import PlanningStore


def unit(key, depends_on=None):
    return {"key": key, "objective": f"完成 {key}", "inputs": [], "expected_outputs": ["结果"],
            "completion_criteria": ["有结果"], "depends_on": depends_on or [],
            "required_tools": [], "required_capabilities": []}


def test_invalid_dependencies_are_rejected():
    with pytest.raises(PlanningError):
        validate_work_units([unit("a", ["missing"])])
    with pytest.raises(PlanningError):
        validate_work_units([unit("a", ["b"]), unit("b", ["a"])])


def test_assignment_evidence_must_be_in_original():
    raw = {"work_units": [{**unit("review"), "matches": [{"member_id": "a", "reason": "指定",
                           "kind": "task_instruction", "reference": "让 A 审查"}]}]}
    members = [{"member_id": "a", "name": "A", "responsibility": "审查", "responsibility_source": "user"}]
    assert make_plan(raw, members, "让 A 审查代码")["assignments"][0]["source"] == "task_instruction"
    with pytest.raises(PlanningError):
        make_plan(raw, members, "审查代码")


def test_pending_decision_is_persisted_and_idempotent(tmp_path):
    store = PlanningStore(str(tmp_path / "test.db"))
    task = {"task_id": "task", "session_id": "session", "request_id": "request"}
    store.create("owner", task, "hash")
    plan = make_plan({"work_units": [unit("work")]}, [{"member_id": "a", "name": "A"}])
    store.finish("owner", "task", plan=plan)
    decision = plan["decisions"][0]["decision_id"]
    result = store.answer("owner", "task", decision, "a")
    assert result["state"] == "planned"
    assert len(store.answer("owner", "task", decision, "a")["plan"]["assignments"]) == 1
    assert store.get("other", "task") is None
    with pytest.raises(ValueError):
        store.answer("owner", "task", decision, "b")


@pytest.mark.asyncio
async def test_preview_splits_and_assigns_in_separate_calls(tmp_path):
    class Provider:
        calls = 0
        async def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(text=json.dumps({"work_units": [unit("research"), unit("write", ["research"])]}))
            return SimpleNamespace(text=json.dumps({"assignments": [
                {"key": "research", "matches": [{"member_id": "a", "reason": "负责研究", "kind": "responsibility", "reference": "负责研究"}]},
                {"key": "write", "matches": [{"member_id": "a", "reason": "团队成员", "kind": "inferred", "reference": ""}]},
            ]}))
    team = {"id": "team", "name": "团队", "members": [{"agent_id": "a", "agent_name": "A"}],
            "formation_plan": {"members": [{"agent_id": "a", "responsibility_source": "user", "original_user_responsibility": "负责研究"}]}}
    provider = Provider()
    service = PlanningService(PlanningStore(str(tmp_path / "test.db")))
    result = await service.create(owner="owner", request_id="r", session_id="s", goal="调研并写作", team=team, provider=provider)
    assert result["state"] == "planned"
    assert provider.calls == 2
    assert result["plan"]["assignments"][0]["source"] == "user_responsibility"
    units = result["plan"]["work_units"]
    assert units[1]["depends_on"] == [units[0]["unit_id"]]
    replay = await service.create(owner="owner", request_id="r", session_id="s", goal="调研并写作", team=team, provider=provider)
    assert replay["task"]["task_id"] == result["task"]["task_id"]
    assert provider.calls == 2


def test_handoff_requires_same_team_and_preserves_assignment():
    from crew.collaboration.planning.handoff import workflow_from_preview
    members = [{"member_id": "a", "name": "A"}]
    raw = {"work_units": [{**unit("work"), "matches": [{"member_id": "a", "reason": "适合", "kind": "inferred"}]}]}
    plan = make_plan(raw, members)
    preview = {"state": "planned", "task": {"task_id": "t", "goal": "目标", "session_id": "s", "team": {
        "team_id": "team", "team_updated_at": 1, "leader_id": "a", "members": members}}, "plan": plan}
    output = workflow_from_preview(preview, team_id="team", session_id="s", goal="目标", team_updated_at=1)
    assert output["nodes"][0]["assignee"] == "leader"
    assert output["nodes"][0]["assignment_reason"] == "适合"
    with pytest.raises(PlanningError):
        workflow_from_preview(preview, team_id="team", session_id="s", goal="目标", team_updated_at=2)


def test_user_duty_outranks_inferred_member():
    members = [{"member_id": "a", "name": "A", "responsibility": "负责审阅代码", "responsibility_source": "user"},
               {"member_id": "b", "name": "B", "responsibility": "", "responsibility_source": "unknown"}]
    raw = {"work_units": [{**unit("review"), "matches": [
        {"member_id": "b", "kind": "inferred", "reference": "", "reason": "具备分析能力"},
        {"member_id": "a", "kind": "responsibility", "reference": "负责审阅代码", "reason": "用户职责"},
    ]}]}
    assignment = make_plan(raw, members)["assignments"][0]
    assert assignment["member_id"] == "a"
    assert assignment["source"] == "user_responsibility"


@pytest.mark.asyncio
async def test_clarification_resumes_same_task_and_is_idempotent(tmp_path):
    class Provider:
        calls = 0
        async def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(text=json.dumps({"critical_missing_info": "评审哪个项目？"}))
            if self.calls == 2:
                return SimpleNamespace(text=json.dumps({"work_units": [unit("review")]}))
            return SimpleNamespace(text=json.dumps({"assignments": [{"key": "review", "matches": [
                {"member_id": "a", "kind": "inferred", "reason": "可审阅", "reference": ""}]}]}))
    provider = Provider()
    team = {"id": "team", "members": [{"agent_id": "a"}], "formation_plan": {}}
    service = PlanningService(PlanningStore(str(tmp_path / "test.db")))
    result = await service.create(owner="owner", request_id="r", session_id="s", goal="评审项目", team=team, provider=provider)
    assert result["state"] == "waiting_user"
    task_id = result["task"]["task_id"]
    decision_id = result["plan"]["decisions"][0]["decision_id"]
    result = await service.answer_clarification(owner="owner", task_id=task_id, decision_id=decision_id,
                                                answer="评审 A 项目", provider=provider)
    assert result["state"] == "planned"
    assert result["task"]["task_id"] == task_id
    assert result["task"]["clarifications"][0]["answer"] == "评审 A 项目"
    replay = await service.answer_clarification(owner="owner", task_id=task_id, decision_id=decision_id,
                                                answer="评审 A 项目", provider=provider)
    assert replay["state"] == "planned"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_conversation_handoff_claims_only_once(tmp_path):
    from crew.collaboration.planning.conversation import plan_conversation
    class Provider:
        calls = 0
        async def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(text=json.dumps({"work_units": [unit("review")]}))
            return SimpleNamespace(text=json.dumps({"assignments": [{"key": "review", "matches": [
                {"member_id": "a", "kind": "responsibility", "reference": "负责审阅", "reason": "用户职责"}]}]}))
    team = {"id": "team", "updated_at": 1, "leader_agent_id": "a",
            "members": [{"agent_id": "a", "agent_name": "A"}], "formation_plan": {"members": [
                {"agent_id": "a", "responsibility_source": "user", "original_user_responsibility": "负责审阅"}]}}
    store = PlanningStore(str(tmp_path / "test.db"))
    async def ask(_decision):
        raise AssertionError("明确的职责不应追问")
    workflow = await plan_conversation(PlanningService(store), owner="owner", request_id="r", session_id="s",
                                       goal="审阅代码", team=team, provider=Provider(), inputs=[], ask=ask,
                                       current_team=lambda: team)
    assert workflow["nodes"][0]["assignment_source"] == "user_responsibility"
    task_id = workflow["task"]["task_id"]
    plan_id = workflow["collaboration_plan_id"]
    assert store.claim_handoff("owner", task_id, plan_id)
    assert not store.claim_handoff("owner", task_id, plan_id)


def test_explicit_assignment_and_exclusion_constrain_semantic_suggestions():
    from crew.collaboration.planning.service import merge_assignments
    members = [{"member_id": "a", "name": "A", "responsibility": "研究", "responsibility_source": "user"},
               {"member_id": "b", "name": "B", "responsibility": "审阅", "responsibility_source": "user"}]
    raw = {"work_units": [unit("review")], "member_constraints": [
        {"member_id": "a", "action": "assign", "unit_keys": ["review"], "reference": "让 A 负责审阅"},
        {"member_id": "b", "action": "exclude", "unit_keys": ["review"], "reference": "B 不参与审阅"}]}
    proposed = {"assignments": [{"key": "review", "matches": [
        {"member_id": "b", "kind": "responsibility", "reference": "审阅", "reason": "长期职责"}]}]}
    goal = "让 A 负责审阅，B 不参与审阅"
    merged = merge_assignments(raw, proposed, members=members, goal=goal, clarifications=[])
    assignment = make_plan(merged, members, goal)["assignments"][0]
    assert assignment["member_id"] == "a"
    assert assignment["source"] == "task_instruction"
