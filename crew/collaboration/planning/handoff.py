"""Validate and project an approved preview for a future execution adapter.

This is a pure projection. It never marks a task dispatched or calls an agent.
"""
from __future__ import annotations

from typing import Any

from crew.collaboration.planning.plan import PlanningError


def workflow_from_preview(preview: dict[str, Any], *, team_id: str, session_id: str,
                          goal: str, team_updated_at: Any) -> dict[str, Any]:
    task = preview.get("task") or {}
    team = task.get("team") or {}
    plan = preview.get("plan") or {}
    if preview.get("state") != "planned" or plan.get("status") != "planned":
        raise PlanningError("规划尚未完成或存在未回答的问题")
    if task.get("session_id") != session_id or task.get("goal") != goal or team.get("team_id") != team_id:
        raise PlanningError("任务、团队或会话已改变，请重新规划")
    if team.get("team_updated_at") != team_updated_at:
        raise PlanningError("团队配置已改变，请重新规划")
    members = {str(member.get("member_id") or "") for member in team.get("members", [])}
    units = plan.get("work_units") or []
    assignments = plan.get("assignments") or []
    if not units or len(units) != len(assignments):
        raise PlanningError("每个工作单元必须有一位负责人")
    by_unit = {item.get("unit_id"): item for item in assignments}
    unit_ids = {item.get("unit_id") for item in units}
    if len(by_unit) != len(assignments) or len(unit_ids) != len(units) or set(by_unit) != unit_ids:
        raise PlanningError("工作单元与负责人不一致")
    if any(isinstance(ref, dict) and ref.get("kind") == "output_contract"
           and ref.get("id") == "project_review_v1" for ref in task.get("inputs", [])):
        from crew.collaboration.review import validate_review_plan
        validate_review_plan(units, assignments=assignments, leader_id=team.get("leader_id", ""))
    nodes = []
    edges = []
    for unit in units:
        unit_id = unit["unit_id"]
        assignment = by_unit[unit_id]
        member_id = assignment.get("member_id")
        if member_id not in members:
            raise PlanningError("负责人不在团队快照中")
        assignee = "leader" if member_id == team.get("leader_id") else member_id
        dependencies = unit.get("depends_on") or []
        if any(parent not in unit_ids or parent == unit_id for parent in dependencies):
            raise PlanningError("工作依赖无效")
        nodes.append({
            "id": unit_id, "title": unit["objective"], "detail": unit["objective"], "assignee": assignee,
            "inputs": list(unit.get("inputs") or []), "expected_outputs": list(unit.get("expected_outputs") or []),
            "acceptance_criteria": list(unit.get("completion_criteria") or []),
            "required_tools": list(unit.get("required_tools") or []),
            # Inferred capabilities are not evidence of a hard execution gate.
            "required_capabilities": [],
            "suggested_capabilities": list(unit.get("required_capabilities") or []),
            "requirements_source": str(unit.get("requirements_source") or "model_suggestion"),
            "assignment_source": assignment.get("source"), "assignment_reason": assignment.get("reason"),
            "responsibility_quote": assignment.get("responsibility_ref"),
            "preserve_assignee": assignment.get("source") in {"task_instruction", "user_clarification", "user_responsibility", "confirmed_suggestion", "user_decision"},
        })
        edges.extend({"parent_id": parent, "child_id": unit_id} for parent in dependencies)
    # The source plan was validated, but persisted data must still be checked.
    remaining = {unit["unit_id"]: set(unit.get("depends_on") or []) for unit in units}
    while remaining:
        ready = {key for key, deps in remaining.items() if not deps}
        if not ready:
            raise PlanningError("工作依赖存在循环")
        remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
    return {"task": {"task_id": task["task_id"], "session_id": session_id, "goal": goal, "team_snapshot": team,
                     "inputs": list(task.get("inputs") or []),
                     "clarifications": list(task.get("clarifications") or [])},
            "collaboration_plan_id": plan["plan_id"],
            "planning": {"engine": "collaboration_planner", "selected_mode": "standard",
                         "member_constraints": list(plan.get("member_constraints") or [])},
            "nodes": nodes, "edges": edges, "revision": 1}
