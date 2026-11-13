"""Select initial team members without conflating membership and ability."""

from __future__ import annotations

from typing import Any

from crew.team.agent_profile import (
    AgentProfile,
    evaluate_capability_coverage,
    is_agent_profile_available,
)


def select_members(
    *,
    required_capabilities: list[str],
    candidates: list[dict[str, Any]],
    profiles: dict[str, AgentProfile],
    leader_id: str,
    required_ids: set[str],
    excluded_ids: set[str],
    capability_assignments: dict[str, str],
    allow_system_fill: bool,
) -> dict[str, list[str]]:
    """Respect required members and cover evidenced requirements with a small team.

    An unspecified goal selects one available teammate as a neutral initial
    suggestion. Its capabilities remain empty until supported by evidence.
    """
    assignment: dict[str, list[str]] = {}
    uncovered = set(required_capabilities)
    for capability, agent_id in capability_assignments.items():
        profile = profiles.get(agent_id)
        if (capability in uncovered and agent_id not in excluded_ids
                and profile is not None and is_agent_profile_available(profile)):
            assignment.setdefault(agent_id, []).append(capability)
            uncovered.remove(capability)
    for agent_id in sorted(required_ids):
        profile = profiles.get(agent_id)
        if (agent_id == leader_id or agent_id in excluded_ids or profile is None
                or not is_agent_profile_available(profile)):
            continue
        assigned = assignment.setdefault(agent_id, [])
        if uncovered:
            coverage = evaluate_capability_coverage(uncovered, {agent_id: profile}, assigned_agent_ids=[agent_id])
            assigned.extend(cap for cap in coverage.covered if cap not in assigned)
            uncovered.difference_update(coverage.covered)
    while uncovered and allow_system_fill:
        best_id = ""
        best_cover: list[str] = []
        best_score = -1.0
        for agent in candidates:
            agent_id = str(agent.get("id") or "")
            profile = profiles.get(agent_id)
            if (not agent_id or agent_id == leader_id or agent_id in excluded_ids
                    or profile is None or not is_agent_profile_available(profile)):
                continue
            coverage = evaluate_capability_coverage(uncovered, {agent_id: profile}, assigned_agent_ids=[agent_id])
            cover = list(coverage.covered)
            score = sum(profile.score(cap) for cap in cover)
            if cover and score > best_score:
                best_id, best_cover, best_score = agent_id, cover, score
        if not best_id:
            break
        assignment.setdefault(best_id, []).extend(best_cover)
        uncovered.difference_update(best_cover)
    if not assignment and allow_system_fill:
        available = sorted(
            agent_id for agent in candidates
            if (agent_id := str(agent.get("id") or "")) not in {"", leader_id}
            and agent_id not in excluded_ids and agent_id in profiles
            and is_agent_profile_available(profiles[agent_id])
            and (not required_capabilities or any(profiles[agent_id].confidence(cap) < 0.5 for cap in required_capabilities))
        )
        if available:
            assignment[available[0]] = []
    return assignment
