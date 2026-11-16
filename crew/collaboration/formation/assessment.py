"""Read-only capability assessment for a fixed team roster."""
from __future__ import annotations

from typing import Any

from crew.team.agent_profile import build_agent_profile, evaluate_capability_coverage, is_agent_profile_available
from crew.team.capabilities import normalize_capabilities
from crew.team.roles import crew_builtin_agent_public, role_preset


def assess_roster(
    members: list[dict[str, Any]], agents: list[dict[str, Any]], required: list[str],
) -> dict[str, Any]:
    """Check existing evidence without selecting or modifying any member."""
    catalog = {str(agent.get("id") or ""): agent for agent in [crew_builtin_agent_public(), *agents]}
    ids = [str(item.get("agent_id") or "").strip() for item in members]
    if not ids or len(ids) > 50 or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("团队成员列表无效")
    if any(agent_id not in catalog for agent_id in ids):
        raise ValueError("部分成员不存在或当前不可访问，请返回组队页面检查")
    profiles = {agent_id: build_agent_profile(catalog[agent_id]) for agent_id in ids}
    per_member = []
    evidence_confidences: list[float] = []
    needed = list(normalize_capabilities(required))
    for member, agent_id in zip(members, ids, strict=True):
        role_key = str(member.get("role_key") or "").strip()
        role_caps = normalize_capabilities(role_preset(role_key).get("capabilities") or []) if role_key else []
        assigned = member.get("assigned_capabilities") or []
        if not isinstance(assigned, list):
            raise ValueError("成员能力列表无效")
        capabilities = normalize_capabilities([*assigned, *role_caps])
        evidence_confidences.extend(profiles[agent_id].confidence(capability) for capability in capabilities)
        needed.extend(capabilities)
        assessment = evaluate_capability_coverage(capabilities, {agent_id: profiles[agent_id]}, assigned_agent_ids=[agent_id])
        per_member.append({"agent_id": agent_id, "role_key": role_key,
                           "available": is_agent_profile_available(profiles[agent_id]), "coverage": assessment.to_dict()})
    coverage = evaluate_capability_coverage(needed, profiles, assigned_agent_ids=ids)
    return {
        "coverage": {
            "required": coverage.required, "covered": coverage.covered,
            "uncovered": coverage.missing, "unavailable": coverage.unavailable,
            "unverified": coverage.unknown,
        },
        "members": per_member,
        "capability_evidence_confidence": round(sum(evidence_confidences) / len(evidence_confidences), 4) if evidence_confidences else 0.0,
    }
