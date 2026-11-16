"""Validate role recommendations independently of capability evidence.

A role expresses a proposed responsibility; it is not proof that the member
has the tools or experience to perform it. This module never changes roster
membership or user-authored responsibilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crew.team.roles import all_role_public_payloads


@dataclass(frozen=True)
class ResponsibilityRecommendation:
    agent_id: str
    role_key: str
    mission: str
    rationale: str


def recommendation_targets(baseline: dict[str, Any], assignments: list[dict[str, Any]]) -> set[str]:
    """Find non-leader members whose duty or role still needs a suggestion."""
    explicit = {str(item.get("agent_id") or ""): item for item in assignments if isinstance(item, dict)}
    leader = str(baseline.get("leader_agent_id") or "")
    return {
        str(member.get("agent_id") or "")
        for member in baseline.get("members", []) if isinstance(member, dict)
        and str(member.get("agent_id") or "") not in {"", leader}
        and not (str(explicit.get(str(member.get("agent_id") or ""), {}).get("role_key") or "").strip()
                 and str(explicit.get(str(member.get("agent_id") or ""), {}).get("responsibility") or "").strip())
    }


def validate_recommendations(
    raw: Any, *, unresolved_ids: set[str], user_duties: dict[str, str], user_roles: dict[str, str] | None = None,
) -> tuple[list[ResponsibilityRecommendation], str]:
    """Return a complete set of proposals or an explicit validation failure."""
    if not isinstance(raw, dict) or not isinstance(raw.get("members"), list):
        return [], "invalid_ai_output"
    roles = {str(item["key"]) for item in all_role_public_payloads()} - {"unclassified"}
    proposals: dict[str, ResponsibilityRecommendation] = {}
    for item in raw["members"]:
        if not isinstance(item, dict):
            return [], "invalid_ai_output"
        agent_id = str(item.get("agent_id") or "").strip()
        role_key = str(item.get("role_key") or "").strip()
        mission = str(item.get("responsibility") or "").strip()
        if (agent_id not in unresolved_ids or agent_id in proposals or role_key not in roles
                or (user_roles or {}).get(agent_id) not in (None, "", role_key)):
            return [], "invalid_ai_output"
        if not mission and not user_duties.get(agent_id):
            return [], "invalid_ai_output"
        proposals[agent_id] = ResponsibilityRecommendation(
            agent_id=agent_id,
            role_key=role_key,
            mission=(user_duties.get(agent_id) or mission)[:4096],
            rationale=str(item.get("reason") or "根据团队目标建议，能力待验证。")[:500],
        )
    if set(proposals) != unresolved_ids:
        return [], "incomplete_ai_output"
    return list(proposals.values()), ""


def apply_recommendations(
    baseline: dict[str, Any], raw: Any, *, team_goal: str,
    agent_names: dict[str, str], user_duties: dict[str, str],
    target_ids: set[str] | None = None, user_roles: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Attach proposed responsibilities to the existing roster and its plan."""
    from copy import deepcopy
    from crew.team.roles import compile_role_responsibility, intelligent_role_markdown, public_responsibility, role_preset

    members = [item for item in (baseline.get("members") or []) if isinstance(item, dict)]
    unresolved_ids = target_ids if target_ids is not None else {
        str(item.get("agent_id") or "") for item in members
        if str(item.get("role_key") or "") in {"", "unclassified"}
    }
    recommendations, reason = validate_recommendations(raw, unresolved_ids=unresolved_ids, user_duties=user_duties, user_roles=user_roles)
    if reason:
        return None, reason
    result = deepcopy(baseline)
    result_members = result.get("members") or []
    plan = result.get("formation_plan") if isinstance(result.get("formation_plan"), dict) else {}
    plan_members = {str(item.get("agent_id") or ""): item for item in plan.get("members") or [] if isinstance(item, dict)}
    for proposal in recommendations:
        member = next(item for item in result_members if item.get("agent_id") == proposal.agent_id)
        preset = role_preset(proposal.role_key)
        responsibility = (dict(member.get("responsibility") or {}) if user_duties.get(proposal.agent_id)
                          else public_responsibility(compile_role_responsibility(
                              role_key=proposal.role_key, team_goal=team_goal, assigned_capabilities=[],
                          )))
        responsibility["mission"] = proposal.mission
        markdown = intelligent_role_markdown(
            role_key=proposal.role_key, agent_name=agent_names.get(proposal.agent_id, proposal.agent_id),
            team_goal=team_goal, assigned_capabilities=[], responsibility=responsibility,
        )
        role_text = user_duties.get(proposal.agent_id) or markdown
        retained_capabilities = list(member.get("assigned_capabilities") or []) if member.get("role_key") == proposal.role_key else []
        fields = {
            "role_key": proposal.role_key, "role_label": str(preset["label"]),
            "responsibility": responsibility, "responsibility_markdown": role_text,
            "responsibility_source": "user" if user_duties.get(proposal.agent_id) else "ai",
            "assigned_capabilities": retained_capabilities, "capabilities": retained_capabilities,
            "workflow_lane": preset.get("workflow_lane") or "other",
            "selection_reason": proposal.rationale,
        }
        member.update(fields)
        member["role"] = role_text
        if proposal.agent_id in plan_members:
            plan_members[proposal.agent_id].update(fields)
    # This is a team-level reference, not a task execution graph.
    lines = ["团队协作参考："]
    for member in result_members:
        name = agent_names.get(str(member.get("agent_id") or ""), str(member.get("agent_id") or ""))
        mission = str((member.get("responsibility") or {}).get("mission") or "按确认职责协调工作")
        lines.append(f"- {name}（{member.get('role_label') or '成员'}）：{mission}")
    lines.append("具体任务的参与成员、分工和依赖关系根据任务需要确定。")
    result["workflow"] = "\n".join(lines)
    result["ai_material_improvements"] = ["已按团队目标推荐成员职责和角色"]
    return result, ""
