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
    raw = {"work_units": [{**unit("review"), "candidate_member_id": "a", "candidate_reason": "指定",
                           "assignment_source": "task_instruction", "responsibility_ref": "让 A 审查"}]}
    members = [{"member_id": "a", "responsibility": "审查", "responsibility_source": "user"}]
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
                {"key": "research", "candidate_member_id": "a", "candidate_reason": "负责研究", "assignment_source": "responsibility", "responsibility_ref": "负责研究"},
                {"key": "write", "candidate_member_id": "a", "candidate_reason": "团队成员", "assignment_source": "inferred"},
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
    raw = {"work_units": [{**unit("work"), "candidate_member_id": "a", "candidate_reason": "适合"}]}
    plan = make_plan(raw, members)
    preview = {"state": "planned", "task": {"task_id": "t", "goal": "目标", "session_id": "s", "team": {
        "team_id": "team", "team_updated_at": 1, "leader_id": "a", "members": members}}, "plan": plan}
    output = workflow_from_preview(preview, team_id="team", session_id="s", goal="目标", team_updated_at=1)
    assert output["nodes"][0]["assignee"] == "leader"
    assert output["nodes"][0]["assignment_reason"] == "适合"
    with pytest.raises(PlanningError):
        workflow_from_preview(preview, team_id="team", session_id="s", goal="目标", team_updated_at=2)
