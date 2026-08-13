"""Team formation pipeline.

This module owns intelligent team formation. Gateway code should only pass
payloads and available agents in, then return the resulting suggestion.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from crew.team import agent_profile as _agent_profile
from crew.team.agent_profile import AgentProfile, build_agent_profile
from crew.team.agent_profile import (
    evaluate_capability_coverage,
    is_agent_profile_available,
)
from crew.team.capabilities import (
    CAPABILITIES,
    CAPABILITY_LABELS,
    CAPABILITY_ROLE_KEYS,
    capabilities_from_text,
    normalize_capabilities,
    normalize_capability,
)
from crew.team.roles import (
    CREW_BUILTIN_AGENT_ID,
    all_role_public_payloads,
    compile_role_responsibility,
    crew_builtin_agent_public,
    infer_role_key,
    intelligent_role_markdown,
    is_crew_builtin_agent,
    public_responsibility,
    responsibility_signature,
    role_preset,
    role_public_payload,
)
from crew.team.team_spec import build_team_spec

# Preserve the original formation.py import surface while the implementation
# lives in the focused model-aware profile module.
AgentCapabilityProfile = _agent_profile.AgentCapabilityProfile
CapabilityAssessment = _agent_profile.CapabilityAssessment
CapabilityEvidence = _agent_profile.CapabilityEvidence
apply_execution_observations = _agent_profile.apply_execution_observations
build_agent_capability_profile = _agent_profile.build_agent_capability_profile


@dataclass
class FormationConstraints:
    leader_agent_id: str = ""
    excluded_agent_ids: set[str] = field(default_factory=set)
    required_agent_ids: set[str] = field(default_factory=set)
    forced_agent_ids: set[str] = field(default_factory=set)
    assignments: dict[str, str] = field(default_factory=dict)
    allow_system_fill: bool = True
    reasons: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FormationPlan:
    version: int
    leader_agent_id: str
    members: list[dict[str, Any]]
    coverage: dict[str, list[str]]
    confidence: dict[str, float]
    staffing_mode: str
    excluded_agent_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def confirmed_formation_plan(
    *,
    leader_agent_id: str,
    members: list[dict[str, Any]],
    existing: dict[str, Any] | None = None,
    team_goal: str = "",
) -> dict[str, Any]:
    """Normalize the user-confirmed roster into the persisted FormationPlan."""

    current = existing if isinstance(existing, dict) else {}
    existing_members = {
        str(item.get("agent_id") or ""): item
        for item in (current.get("members") or [])
        if isinstance(item, dict)
    }
    normalized_members: list[dict[str, Any]] = []
    all_assigned: list[str] = []
    for member in members:
        agent_id = str(member.get("agent_id") or "").strip()
        if not agent_id:
            continue
        prior = existing_members.get(agent_id, {})
        role_key = str(member.get("role_key") or prior.get("role_key") or "").strip()
        preset = role_preset(role_key)
        assigned = member.get("assigned_capabilities")
        if not isinstance(assigned, list) or not assigned:
            assigned = member.get("capabilities")
        if not isinstance(assigned, list) or not assigned:
            assigned = prior.get("assigned_capabilities") or list(preset.get("capabilities") or [])
        assigned = normalize_capabilities(assigned)
        all_assigned.extend(assigned)
        explicit_responsibility = (
            member.get("responsibility")
            if isinstance(member.get("responsibility"), dict) and member.get("responsibility")
            else None
        )
        prior_responsibility = (
            prior.get("responsibility")
            if isinstance(prior.get("responsibility"), dict) and prior.get("responsibility")
            else None
        )
        prior_role_key = str(prior.get("role_key") or "").strip()
        if explicit_responsibility is not None:
            responsibility = public_responsibility(explicit_responsibility)
        elif prior_responsibility is not None and prior_role_key == str(preset.get("key") or role_key):
            responsibility = public_responsibility(prior_responsibility)
        else:
            responsibility = public_responsibility(compile_role_responsibility(
                role_key=str(preset.get("key") or role_key),
                team_goal=team_goal,
                assigned_capabilities=assigned,
                is_leader=agent_id == leader_agent_id,
            ))
        responsibility_markdown = str(
            member.get("responsibility_markdown")
            or member.get("role")
            or (
                prior.get("responsibility_markdown")
                if prior_role_key == str(preset.get("key") or role_key)
                else ""
            )
            or intelligent_role_markdown(
                role_key=str(preset.get("key") or role_key),
                agent_name=str(member.get("agent_name") or agent_id),
                team_goal=team_goal,
                is_leader=agent_id == leader_agent_id,
                assigned_capabilities=assigned,
                responsibility=responsibility,
            )
        )
        normalized_members.append({
            "agent_id": agent_id,
            "role_key": str(preset.get("key") or role_key),
            "role_label": str(member.get("role_label") or prior.get("role_label") or preset.get("label") or ""),
            "assigned_capabilities": assigned,
            "responsibility": responsibility,
            "responsibility_markdown": responsibility_markdown,
            "selection_source": str(prior.get("selection_source") or "user"),
            "locked": bool(prior.get("locked", True)),
            "selection_reason": str(prior.get("selection_reason") or "用户确认的团队成员。"),
        })

    prior_coverage = current.get("coverage") if isinstance(current.get("coverage"), dict) else {}
    required = normalize_capabilities(prior_coverage.get("required") or [])
    if not required:
        required = list(dict.fromkeys(all_assigned))
    covered = list(dict.fromkeys(item for item in all_assigned if item in required))
    uncovered = [item for item in required if item not in covered]
    confidence = current.get("confidence") if isinstance(current.get("confidence"), dict) else {}
    coverage_score = (len(covered) / len(required)) if required else 1.0
    return FormationPlan(
        version=max(1, int(current.get("version") or 1)),
        leader_agent_id=leader_agent_id,
        members=normalized_members,
        coverage={"required": required, "covered": covered, "uncovered": uncovered},
        confidence={
            "requirement": _clamp_score(confidence.get("requirement"), 1.0),
            "capability_evidence": _clamp_score(confidence.get("capability_evidence"), 0.15),
            "coverage": round(coverage_score, 4),
            "overall": _clamp_score(confidence.get("overall"), coverage_score),
        },
        staffing_mode=str(current.get("staffing_mode") or "user_confirmed"),
        excluded_agent_ids=[str(item) for item in (current.get("excluded_agent_ids") or []) if str(item)],
        reasons=[str(item) for item in (current.get("reasons") or ["用户确认团队成员与职责。"]) if str(item)],
        warnings=[str(item) for item in (current.get("warnings") or []) if str(item)],
    ).to_dict()


def _goal_text(payload: dict[str, Any], *, include_workflow: bool = True) -> str:
    keys = ("name", "description", "workflow") if include_workflow else ("name", "description")
    return "\n".join(
        str(payload.get(key) or "").strip()
        for key in keys
        if str(payload.get(key) or "").strip()
    )


def _agent_name(agent: dict[str, Any]) -> str:
    return str(agent.get("name") or agent.get("provider") or agent.get("id") or "Agent")


def _agent_aliases(agent: dict[str, Any]) -> list[str]:
    aliases = [
        str(agent.get("id") or ""),
        str(agent.get("name") or ""),
    ]
    if agent.get("id") == CREW_BUILTIN_AGENT_ID:
        aliases.extend(["crew", "crew::builtin", "内置crew", "crew内置", "crew 内置智能体"])
    out: list[str] = []
    for alias in aliases:
        value = alias.strip().lower()
        if not value:
            continue
        out.append(value)
        for part in re.split(r"[\s/_:-]+", value):
            if len(part) > 1:
                out.append(part)
        compact = value.replace(" ", "")
        if compact != value:
            out.append(compact)
    return list(dict.fromkeys(out))


def _alias_start(text: str, alias: str) -> int:
    if len(alias) > 1:
        return text.find(alias)
    match = re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", text)
    return match.start() if match else -1


def _clause_around(text: str, start: int, alias_length: int) -> str:
    separators = "，,；;。！？!?\n"
    left = start
    while left > 0 and text[left - 1] not in separators:
        left -= 1
    right = start + alias_length
    while right < len(text) and text[right] not in separators:
        right += 1
    return text[left:right]


def _capability_from_text(text: str, allowed: set[str] | None = None) -> str:
    candidates = allowed or set(CAPABILITIES)
    return next((capability for capability in capabilities_from_text(text, include_planning=True) if capability in candidates), "")


def _extract_constraints(text: str, agents: list[dict[str, Any]]) -> FormationConstraints:
    lowered = str(text or "").lower()
    compact = lowered.replace(" ", "")
    constraints = FormationConstraints()
    if any(word in compact for word in ("不要补员", "不用补员", "不要自动加", "不要自动补", "只要这些", "仅这些")):
        constraints.allow_system_fill = False
        constraints.reasons.append("用户限制系统自动补员。")
    for agent in agents:
        agent_id = str(agent.get("id") or "")
        if not agent_id:
            continue
        for alias in _agent_aliases(agent):
            search_text = compact if " " not in alias else lowered
            start = _alias_start(search_text, alias)
            if start < 0:
                continue
            clause = _clause_around(search_text, start, len(alias))
            name = _agent_name(agent)
            if any(word in clause for word in ("leader", "队长", "负责人", "统筹")):
                constraints.leader_agent_id = agent_id
                constraints.required_agent_ids.add(agent_id)
                constraints.reasons.append(f"用户指定 {name} 为 Leader。")
            if any(word in clause for word in ("不让", "不要", "别让", "不用", "排除", "禁用")) and any(
                word in clause for word in ("加入", "进组", "参与", "成员", "组队", "团队", "负责", "做")
            ):
                constraints.excluded_agent_ids.add(agent_id)
                constraints.reasons.append(f"用户明确排除 {name}。")
            if any(word in clause for word in ("加入", "进组", "参与", "成员")) and agent_id not in constraints.excluded_agent_ids:
                constraints.required_agent_ids.add(agent_id)
                constraints.reasons.append(f"用户要求 {name} 加入团队。")
            capability = _capability_from_text(clause)
            if capability and capability != "planning" and agent_id not in constraints.excluded_agent_ids:
                constraints.assignments[capability] = agent_id
                constraints.required_agent_ids.add(agent_id)
                constraints.reasons.append(f"用户指定 {name} 负责{CAPABILITY_LABELS[capability]}。")
            break
    for agent_id in sorted(constraints.required_agent_ids & constraints.excluded_agent_ids):
        agent = next((item for item in agents if str(item.get("id") or "") == agent_id), {})
        constraints.conflicts.append(f"{_agent_name(agent)} 同时被要求加入和排除。")
    for capability, agent_id in constraints.assignments.items():
        if agent_id in constraints.excluded_agent_ids:
            agent = next((item for item in agents if str(item.get("id") or "") == agent_id), {})
            constraints.conflicts.append(f"{_agent_name(agent)} 同时被排除又被指定负责{CAPABILITY_LABELS[capability]}。")
    return constraints


def _payload_agent_ids(payload: dict[str, Any], key: str, valid_ids: set[str]) -> set[str]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return set()
    return {
        str(agent_id).strip()
        for agent_id in raw
        if str(agent_id).strip() in valid_ids
    }


def _clamp_score(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(score, 1.0))


def rank_staffing_candidates(
    required_capabilities: list[str],
    agents: list[dict[str, Any]],
    *,
    excluded_agent_ids: set[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Rank ready External Agents for one Runtime staffing gap."""

    required = normalize_capabilities(required_capabilities)
    if not required:
        return []
    excluded = {str(item or "").strip() for item in (excluded_agent_ids or set()) if str(item or "").strip()}
    ranked: list[tuple[tuple[int, float, float, str], dict[str, Any]]] = []
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id or agent_id in excluded or is_crew_builtin_agent(agent_id):
            continue
        profile = build_agent_profile(agent)
        if not is_agent_profile_available(profile):
            continue
        coverage = evaluate_capability_coverage(
            required,
            {agent_id: profile},
            assigned_agent_ids=[agent_id],
        )
        if coverage.status != "covered":
            continue
        covered = list(coverage.covered)
        capability_evidence = {
            capability: {
                "score": round(profile.score(capability), 4),
                "confidence": round(profile.confidence(capability), 4),
                "sources": list(dict.fromkeys(
                    evidence.source
                    for evidence in profile.capabilities[capability].evidence
                ))[:3],
            }
            for capability in covered
        }
        weighted_score = sum(
            profile.score(capability) * max(profile.confidence(capability), 0.15)
            for capability in covered
        )
        candidate = {
            "candidate_type": "agent",
            "external_agent_id": agent_id,
            "name": _agent_name(agent),
            "selection_source": "managed_pool" if str(agent.get("managed_kind") or "") else "existing_agent",
            "runtime_id": str(agent.get("runtime_id") or ""),
            "model_id": str(agent.get("model") or ""),
            "profile_version": profile.version,
            "covered_capabilities": covered,
            "capability_evidence": capability_evidence,
            "reason": f"覆盖 {len(covered)}/{len(required)} 项所需能力，且 Runtime/model 当前可用。",
        }
        rank = (-len(covered), -weighted_score, -sum(profile.score(item) for item in covered), agent_id)
        ranked.append((rank, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked[:max(1, int(limit or 1))]]


def role_key_for_capabilities(capabilities: list[str]) -> str:
    """Public Runtime staffing projection onto the existing standard role catalog."""

    return _role_key_for_capabilities(normalize_capabilities(capabilities))


def _required_capabilities(_text: str, team_spec: Any) -> list[str]:
    required = normalize_capabilities(team_spec.team_requirements.get("capabilities") or [])
    workflow_lanes = set(team_spec.team_requirements.get("workflow_lanes") or [])
    if "build" in workflow_lanes and not {
        "frontend", "backend", "implementation",
    }.intersection(required):
        required.append("implementation")
    if "verify" in workflow_lanes:
        required.append("verification")
    if "docs" in workflow_lanes or "release" in workflow_lanes:
        required.append("documentation")
    return [capability for capability in normalize_capabilities(required) if capability != "planning"]


def _formation_team_spec_input(payload: dict[str, Any], goal: str) -> dict[str, Any]:
    """Build the canonical TeamSpec envelope from the formation request."""

    requirements = dict(payload.get("team_requirements") or {})
    requested_capabilities = payload.get("required_capabilities")
    if requested_capabilities is not None and "capabilities" not in requirements:
        requirements["capabilities"] = requested_capabilities
    requested_roles = payload.get("required_roles")
    if requested_roles is not None and "roles" not in requirements:
        requirements["roles"] = requested_roles
    task_profile = payload.get("task_profile")
    return {
        "goal": goal,
        "task_profile": dict(task_profile) if isinstance(task_profile, dict) else {},
        "team_requirements": requirements,
        **{
            key: payload[key]
            for key in (
                "collaboration_mode",
                "planning",
                "policy",
                "deliverables",
                "success_criteria",
                "risk_level",
                "uncertainty",
                "planner_notes",
            )
            if key in payload
        },
    }


def _role_key_for_capabilities(capabilities: list[str]) -> str:
    caps = set(capabilities)
    if {"frontend", "backend"}.issubset(caps):
        return "fullstack_developer"
    if "frontend" in caps:
        return "frontend_developer"
    if "backend" in caps:
        return "backend_developer"
    # A single available Agent may cover both implementation and verification.
    # Its primary role must still describe the build responsibility; otherwise a
    # development team is misleadingly presented as Leader + QA only.
    if "implementation" in caps:
        return "fullstack_developer"
    if caps & {"information_retrieval", "research", "analysis"}:
        return "research_analyst"
    if "testing" in caps:
        return "qa_engineer"
    if caps & {"documentation", "synthesis"}:
        return "technical_writer"
    if caps & {"review", "verification"}:
        return "independent_reviewer"
    for capability in CAPABILITIES:
        if capability in caps and capability != "planning":
            return CAPABILITY_ROLE_KEYS[capability]
    return "fullstack_developer"


def _slot_role_key(slot: dict[str, Any]) -> str:
    role_key = str(slot.get("role_key") or "").strip()
    if role_key:
        return role_key
    capability = normalize_capability(slot.get("capability"))
    return CAPABILITY_ROLE_KEYS.get(capability, "fullstack_developer")


def _slot_capability(slot: dict[str, Any]) -> str:
    capability = normalize_capability(slot.get("capability"))
    if capability:
        return capability
    role_key = _slot_role_key(slot)
    return _capability_for_role_key(role_key)


def _capability_for_role_key(role_key: str) -> str:
    preset = role_preset(role_key)
    capabilities = normalize_capabilities(preset.get("capabilities") or [])
    if capabilities:
        return next((capability for capability in capabilities if capability != "planning"), "planning")
    lane = str(preset.get("workflow_lane") or "").lower()
    if lane == "design":
        return "design"
    if lane == "verify":
        return "verification"
    if lane == "docs":
        return "documentation"
    if lane in {"lead", "plan"}:
        return "planning"
    return "implementation"


def _member_payload(
    *,
    agent: dict[str, Any],
    role_key: str,
    capabilities: list[str],
    description: str,
    workflow: str,
    is_leader: bool,
    selection_reason: str,
) -> dict[str, Any]:
    preset = role_preset(role_key)
    assigned_capabilities = normalize_capabilities(capabilities or list(preset.get("capabilities") or []))
    compiled_responsibility = compile_role_responsibility(
        role_key=str(preset["key"]),
        team_goal=description,
        assigned_capabilities=assigned_capabilities,
        is_leader=is_leader,
    )
    responsibility = public_responsibility(compiled_responsibility)
    role = intelligent_role_markdown(
        role_key=str(preset["key"]),
        agent_name=_agent_name(agent),
        team_goal=description,
        workflow=workflow,
        is_leader=is_leader,
        assigned_capabilities=assigned_capabilities,
        responsibility=responsibility,
    )
    return {
        "agent_id": str(agent.get("id") or ""),
        "role": role,
        "responsibility": responsibility,
        "responsibility_markdown": role,
        "role_key": str(preset["key"]),
        "role_label": str(preset["label"]),
        "assigned_capabilities": assigned_capabilities,
        # Compatibility projection for the existing UI and team member store.
        "capabilities": assigned_capabilities,
        "workflow_lane": preset.get("workflow_lane") or "build",
        "selection_reason": selection_reason,
    }


def _render_workflow(members: list[dict[str, Any]], agent_by_id: dict[str, dict[str, Any]]) -> str:
    if not members:
        return ""
    leader = members[0]
    leader_name = _agent_name(agent_by_id.get(str(leader["agent_id"])) or {"name": "Leader"})
    lines = [f"1. {leader_name} 明确目标、任务边界和验收标准。"]
    step = 2
    for member in members[1:]:
        agent_name = _agent_name(agent_by_id.get(str(member["agent_id"])) or {"name": member["agent_id"]})
        role_label = str(member.get("role_label") or role_preset(str(member.get("role_key") or ""))["label"])
        responsibility = member.get("responsibility") if isinstance(member.get("responsibility"), dict) else {}
        mission = str(responsibility.get("mission") or "").strip()
        deliverables = [
            str(item).strip()
            for item in (responsibility.get("deliverables") or [])
            if str(item).strip()
        ]
        output = "、".join(deliverables[:3]) or "可验收结果"
        lines.append(
            f"{step}. {agent_name}（{role_label}）{mission or '承担已确认的团队常驻职责'}，提交{output}。"
        )
        step += 1
    if len(members) == 1:
        lines.append(f"2. {leader_name} 独立完成任务并直接交付结果。")
    else:
        lines.append(f"{step}. {leader_name} 审阅成员结果，必要时安排返工，最终汇总交付。")
    return "\n".join(lines)


def _resolve_responsibility_overlaps(
    members: list[dict[str, Any]],
    *,
    leader_id: str,
    locked_agent_ids: set[str],
    agent_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Remove redundant system picks while preserving every user-locked member."""

    kept: list[dict[str, Any]] = []
    signature_index: dict[tuple[str, str, tuple[str, ...], tuple[str, ...]], int] = {}
    warnings: list[str] = []
    for member in members:
        agent_id = str(member.get("agent_id") or "")
        if agent_id == leader_id:
            kept.append(member)
            continue
        signature = responsibility_signature(
            role_key=str(member.get("role_key") or ""),
            assigned_capabilities=list(member.get("assigned_capabilities") or []),
            responsibility=member.get("responsibility") if isinstance(member.get("responsibility"), dict) else None,
        )
        prior_index = signature_index.get(signature)
        if prior_index is None:
            signature_index[signature] = len(kept)
            kept.append(member)
            continue
        prior = kept[prior_index]
        prior_id = str(prior.get("agent_id") or "")
        current_locked = agent_id in locked_agent_ids
        prior_locked = prior_id in locked_agent_ids
        if current_locked and not prior_locked:
            kept[prior_index] = member
            continue
        if not current_locked:
            continue
        warnings.append(
            f"{_agent_name(agent_by_id.get(prior_id) or {'name': prior_id})} 与 "
            f"{_agent_name(agent_by_id.get(agent_id) or {'name': agent_id})} 的常驻职责范围相同；"
            "团队会保留用户确认的成员，但实际任务不会为了占满成员而重复派活。"
        )
        kept.append(member)
    return kept, warnings


def _choose_minimal_team(
    *,
    required_capabilities: list[str],
    candidates: list[dict[str, Any]],
    profiles: dict[str, AgentProfile],
    leader_id: str,
    constraints: FormationConstraints,
) -> dict[str, list[str]]:
    assignment: dict[str, list[str]] = {}
    uncovered = set(required_capabilities)
    for capability, agent_id in constraints.assignments.items():
        profile = profiles.get(agent_id)
        if (
            capability in uncovered
            and agent_id not in constraints.excluded_agent_ids
            and profile is not None
            and is_agent_profile_available(profile)
        ):
            assignment.setdefault(agent_id, []).append(capability)
            uncovered.remove(capability)
    for agent_id in constraints.required_agent_ids:
        if agent_id != leader_id and agent_id not in constraints.excluded_agent_ids:
            profile = profiles.get(agent_id)
            if profile is None or not is_agent_profile_available(profile):
                continue
            assigned = assignment.setdefault(agent_id, [])
            if not assigned and uncovered:
                best_capability = max(
                    uncovered,
                    key=lambda capability: profile.score(capability),
                )
                assigned.append(best_capability)
                uncovered.remove(best_capability)
    while uncovered and constraints.allow_system_fill:
        best_agent: dict[str, Any] | None = None
        best_cover: list[str] = []
        best_score = -1.0
        for agent in candidates:
            agent_id = str(agent.get("id") or "")
            if not agent_id or agent_id == leader_id or agent_id in constraints.excluded_agent_ids:
                continue
            profile = profiles[agent_id]
            if not is_agent_profile_available(profile):
                continue
            coverage = evaluate_capability_coverage(
                uncovered,
                {agent_id: profile},
                assigned_agent_ids=[agent_id],
            )
            cover = list(coverage.covered)
            if not cover:
                continue
            score = sum(profile.score(cap) for cap in cover)
            if score > best_score:
                best_agent = agent
                best_cover = cover
                best_score = score
        if best_agent is None:
            for agent in candidates:
                agent_id = str(agent.get("id") or "")
                if not agent_id or agent_id == leader_id or agent_id in constraints.excluded_agent_ids:
                    continue
                profile = profiles[agent_id]
                if not is_agent_profile_available(profile):
                    continue
                coverage = evaluate_capability_coverage(
                    uncovered,
                    {agent_id: profile},
                    assigned_agent_ids=[agent_id],
                )
                cover = list(coverage.covered)
                score = sum(profile.score(cap) for cap in cover)
                if score > best_score:
                    best_agent = agent
                    best_cover = cover
                    best_score = score
        if best_agent is None:
            break
        agent_id = str(best_agent.get("id") or "")
        assignment.setdefault(agent_id, []).extend(best_cover)
        uncovered.difference_update(best_cover)
    return assignment


def _profile_available_for_formation(profile: AgentProfile) -> bool:
    return is_agent_profile_available(profile)


def fast_team_suggestion(payload: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
    all_agents = [crew_builtin_agent_public(), *agents]
    raw_slots = payload.get("slots")
    has_slots = isinstance(raw_slots, list)
    slots = [slot for slot in raw_slots if isinstance(slot, dict)] if has_slots else []
    text = _goal_text(payload, include_workflow=not has_slots)
    description = str(payload.get("description") or "").strip()
    team_spec_input = _formation_team_spec_input(
        payload,
        _goal_text(payload, include_workflow=True),
    )
    team_spec = build_team_spec(team_spec_input)
    constraints = FormationConstraints() if has_slots else _extract_constraints(text, all_agents)

    agent_by_id = {str(agent.get("id") or ""): agent for agent in all_agents}
    valid_ids = set(agent_by_id)
    if not has_slots:
        structured_required = _payload_agent_ids(payload, "required_agent_ids", valid_ids)
        structured_excluded = _payload_agent_ids(payload, "excluded_agent_ids", valid_ids)
        structured_forced = _payload_agent_ids(payload, "force_required_agent_ids", valid_ids)
        constraints.required_agent_ids.update(structured_required)
        constraints.excluded_agent_ids.update(structured_excluded)
        constraints.forced_agent_ids.update(structured_forced & structured_required)
    requested_leader = str(payload.get("leader_agent_id") or "").strip()
    leader_id = requested_leader if requested_leader in valid_ids else ""
    if not leader_id and constraints.leader_agent_id in valid_ids:
        leader_id = constraints.leader_agent_id
    for slot in slots:
        if bool(slot.get("is_leader")):
            slot_agent = str(slot.get("agent_id") or "").strip()
            if slot_agent in valid_ids:
                leader_id = slot_agent
                break
    if not leader_id:
        leader_id = CREW_BUILTIN_AGENT_ID

    excluded = set(constraints.excluded_agent_ids)
    if leader_id in excluded:
        constraints.conflicts.append(f"{_agent_name(agent_by_id[leader_id])} 同时被指定为 Leader 和排除。")
        excluded.discard(leader_id)
    candidates = [agent for agent in all_agents if str(agent.get("id") or "") not in excluded]

    profiles = {
        str(agent.get("id") or ""): build_agent_profile(agent)
        for agent in candidates
    }
    leader_profile = profiles.get(leader_id)
    if (
        leader_id != CREW_BUILTIN_AGENT_ID
        and (leader_profile is None or not _profile_available_for_formation(leader_profile))
    ):
        constraints.conflicts.append(
            f"{_agent_name(agent_by_id[leader_id])} 当前运行时或模型不可用，Leader 已改为 Crew 内置智能体。"
        )
        leader_id = CREW_BUILTIN_AGENT_ID

    required_capabilities = _required_capabilities(text, team_spec)
    requested_capabilities = normalize_capabilities(payload.get("required_capabilities") or [])
    # Custom capabilities are now an explicit capability-key list.  Natural
    # language descriptions belong in the PlanningDecision input boundary and
    # must not be reinterpreted here by a second keyword table.
    custom_requested_capabilities = normalize_capabilities(payload.get("custom_capabilities") or [])
    required_capabilities = list(dict.fromkeys([
        *required_capabilities,
        *requested_capabilities,
        *custom_requested_capabilities,
    ]))
    decision_capabilities = list(dict.fromkeys([
        *requested_capabilities,
        *custom_requested_capabilities,
    ])) or list(required_capabilities)
    required_agent_conflicts: list[dict[str, Any]] = []
    if not has_slots:
        for agent_id in sorted(constraints.required_agent_ids):
            if agent_id == leader_id or agent_id in excluded:
                continue
            profile = profiles.get(agent_id)
            if profile is None:
                continue
            if not _profile_available_for_formation(profile):
                required_agent_conflicts.append({
                    "agent_id": agent_id,
                    "agent_name": _agent_name(agent_by_id[agent_id]),
                    "required_capabilities": list(decision_capabilities),
                    "matched_capabilities": [],
                    "best_score": 0.0,
                    "best_confidence": 1.0,
                    "reason": "Agent 当前运行时或模型不可用，不能加入 Formation。",
                })
                continue
            if agent_id in constraints.forced_agent_ids or agent_id in constraints.assignments.values():
                continue
            if not decision_capabilities:
                continue
            assessments = [
                profile.capabilities.get(capability)
                for capability in decision_capabilities
            ]
            # Missing evidence is not negative evidence. Only interrupt the user
            # when every relevant capability has a reliable, explicitly low assessment.
            if any(assessment is None or assessment.confidence < 0.5 for assessment in assessments):
                continue
            matched = [
                capability
                for capability in decision_capabilities
                if profile.score(capability) >= 0.5
            ]
            if matched:
                continue
            best_score = max((profile.score(capability) for capability in decision_capabilities), default=0.0)
            best_confidence = max((profile.confidence(capability) for capability in decision_capabilities), default=0.0)
            required_agent_conflicts.append({
                "agent_id": agent_id,
                "agent_name": _agent_name(agent_by_id[agent_id]),
                "required_capabilities": list(decision_capabilities),
                "matched_capabilities": [],
                "best_score": round(best_score, 4),
                "best_confidence": round(best_confidence, 4),
                "reason": "现有能力画像对这些能力有明确评估，但评分未达到组队阈值。",
            })
        constraints.required_agent_ids.difference_update(
            item["agent_id"] for item in required_agent_conflicts
        )
    conflict_agent_ids = {item["agent_id"] for item in required_agent_conflicts}
    selection_candidates = [
        agent for agent in candidates
        if str(agent.get("id") or "") not in conflict_agent_ids
    ]
    member_capabilities: dict[str, list[str]] = {}
    if has_slots:
        for slot in slots:
            if bool(slot.get("is_leader")):
                continue
            agent_id = str(slot.get("agent_id") or slot.get("suggested_agent_id") or "").strip()
            profile = profiles.get(agent_id)
            if (
                agent_id in agent_by_id
                and agent_id not in excluded
                and profile is not None
                and _profile_available_for_formation(profile)
            ):
                capability = _slot_capability(slot)
                member_capabilities.setdefault(agent_id, [])
                if capability:
                    member_capabilities[agent_id].append(capability)
    else:
        member_capabilities = _choose_minimal_team(
            required_capabilities=required_capabilities,
            candidates=selection_candidates,
            profiles=profiles,
            leader_id=leader_id,
            constraints=constraints,
        )

    members = [
        _member_payload(
            agent=agent_by_id[leader_id],
            role_key="project_manager",
            capabilities=["planning"],
            description=description,
            workflow="",
            is_leader=True,
            selection_reason="作为团队调度中心，负责拆解、派发、验收和最终汇总。",
        )
    ]
    for agent_id, capabilities in member_capabilities.items():
        if agent_id == leader_id or agent_id not in agent_by_id:
            continue
        clean_capabilities = [cap for cap in CAPABILITIES if cap in set(capabilities) and cap != "planning"]
        if not clean_capabilities and not has_slots and agent_id not in constraints.required_agent_ids:
            continue
        role_key = _role_key_for_capabilities(clean_capabilities)
        if has_slots:
            slot = next(
                (
                    item for item in slots
                    if str(item.get("agent_id") or item.get("suggested_agent_id") or "").strip() == agent_id
                    and not bool(item.get("is_leader"))
                ),
                {},
            )
            role_key = _slot_role_key(slot)
        if has_slots:
            reason = "用户槽位确认。"
        elif any(constraints.assignments.get(capability) == agent_id for capability in clean_capabilities):
            reason = "用户指定分工，系统保留该成员职责。"
        else:
            reason = "根据能力画像覆盖必需能力。"
        members.append(_member_payload(
            agent=agent_by_id[agent_id],
            role_key=role_key,
            capabilities=clean_capabilities,
            description=description,
            workflow="",
            is_leader=False,
            selection_reason=reason,
        ))

    locked_agent_ids = (
        {str(member.get("agent_id") or "") for member in members if str(member.get("agent_id") or "")}
        if has_slots
        else set(constraints.required_agent_ids)
    )
    members, overlap_warnings = _resolve_responsibility_overlaps(
        members,
        leader_id=leader_id,
        locked_agent_ids=locked_agent_ids,
        agent_by_id=agent_by_id,
    )
    workflow = _render_workflow(members, agent_by_id)
    for member in members:
        member["role"] = intelligent_role_markdown(
            role_key=str(member["role_key"]),
            agent_name=_agent_name(agent_by_id.get(str(member["agent_id"])) or {}),
            team_goal=description,
            workflow=workflow,
            is_leader=member["agent_id"] == leader_id,
            assigned_capabilities=list(member.get("assigned_capabilities") or []),
            responsibility=member.get("responsibility") if isinstance(member.get("responsibility"), dict) else None,
        )
        member["responsibility_markdown"] = member["role"]

    formation_profiles = {
        agent_id: profiles[agent_id]
        for agent_id in (
            str(member.get("agent_id") or "")
            for member in members
        )
        if agent_id in profiles
    }
    formation_coverage = evaluate_capability_coverage(
        required_capabilities,
        formation_profiles,
        assigned_agent_ids=formation_profiles.keys(),
    )
    covered_capabilities = list(formation_coverage.covered)
    uncovered_capabilities = list(dict.fromkeys([
        *formation_coverage.missing,
        *formation_coverage.unavailable,
        *formation_coverage.unknown,
    ]))
    assessment_confidences = [
        profiles[member["agent_id"]].confidence(capability)
        for member in members
        if member["agent_id"] in profiles
        for capability in (member.get("assigned_capabilities") or [])
        if capability in CAPABILITIES
    ]
    evidence_confidence = (
        sum(assessment_confidences) / len(assessment_confidences)
        if assessment_confidences else 0.15
    )
    # No explicit requirement is not proof of full coverage.  Keep the Fast
    # baseline conservative so an underspecified TeamSpec cannot skip the
    # Formation review path by reporting a false 100% match.
    coverage_confidence = (
        len(covered_capabilities) / len(required_capabilities)
        if required_capabilities else 0.0
    )
    requirement_confidence = 0.85 if description or text else 0.4
    if constraints.conflicts:
        requirement_confidence = min(requirement_confidence, 0.3)
    warnings = [*constraints.conflicts, *overlap_warnings]
    if uncovered_capabilities:
        warnings.append(
            "未覆盖能力：" + "、".join(CAPABILITY_LABELS[item] for item in uncovered_capabilities)
        )

    formation_members: list[dict[str, Any]] = []
    for member in members:
        source = "user" if has_slots or member["agent_id"] in constraints.required_agent_ids else "system"
        formation_members.append({
            "agent_id": member["agent_id"],
            "role_key": member.get("role_key") or "",
            "role_label": member.get("role_label") or "",
            "assigned_capabilities": list(member.get("assigned_capabilities") or []),
            "responsibility": dict(member.get("responsibility") or {}),
            "responsibility_markdown": str(member.get("responsibility_markdown") or ""),
            "selection_source": source,
            "locked": bool(has_slots or member["agent_id"] in constraints.required_agent_ids),
            "selection_reason": str(member.get("selection_reason") or ""),
        })

    formation_plan = FormationPlan(
        version=1,
        leader_agent_id=leader_id,
        members=formation_members,
        coverage={
            "required": list(required_capabilities),
            "covered": covered_capabilities,
            "uncovered": uncovered_capabilities,
        },
        confidence={
            "requirement": round(requirement_confidence, 4),
            "capability_evidence": round(evidence_confidence, 4),
            "coverage": round(coverage_confidence, 4),
            "overall": round((requirement_confidence + evidence_confidence + coverage_confidence) / 3, 4),
        },
        staffing_mode="slot_confirmed" if has_slots else "minimal_sufficient",
        excluded_agent_ids=sorted(excluded),
        reasons=[*constraints.reasons],
        warnings=warnings,
    )
    return {
        "leader_agent_id": leader_id,
        "workflow": workflow,
        "members": [{**member, "sort_order": index} for index, member in enumerate(members)],
        "decision_required": bool(required_agent_conflicts),
        "required_agent_conflicts": required_agent_conflicts,
        "team_spec": team_spec.to_dict(),
        "formation_plan": formation_plan.to_dict(),
        "reasons": [
            *constraints.reasons,
            *constraints.conflicts,
            f"能力需求：{'、'.join(CAPABILITY_LABELS[cap] for cap in required_capabilities) or 'Leader 可直接处理'}。",
            "采用当前槽位作为成员来源。" if has_slots else "采用最小充分成员选择。",
        ],
    }


FORMATION_AI_MAX_AGENT_CANDIDATES = 12
FORMATION_AI_MAX_EVIDENCE_PER_AGENT = 6
FORMATION_AI_MAX_RUNTIME_OPTIONS = 24
FORMATION_AUTO_OVERALL_MIN = 0.80
FORMATION_AUTO_REQUIREMENT_MIN = 0.80
FORMATION_AUTO_EVIDENCE_MIN = 0.75


def formation_auto_decision(
    payload: dict[str, Any],
    baseline: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Decide whether Auto should ask Formation AI to audit the Fast draft.

    Fast may skip AI only for a fully covered, high-confidence, low-uncertainty
    and structurally simple draft.  The gate consumes existing TeamSpec and
    FormationPlan fields instead of maintaining a second task classifier.
    """

    plan = baseline.get("formation_plan") if isinstance(baseline.get("formation_plan"), dict) else {}
    coverage = plan.get("coverage") if isinstance(plan.get("coverage"), dict) else {}
    confidence = plan.get("confidence") if isinstance(plan.get("confidence"), dict) else {}
    team_spec = baseline.get("team_spec") if isinstance(baseline.get("team_spec"), dict) else {}
    task_profile = (
        team_spec.get("task_profile")
        if isinstance(team_spec.get("task_profile"), dict)
        else {}
    )
    policy = team_spec.get("policy") if isinstance(team_spec.get("policy"), dict) else {}
    planning = team_spec.get("planning") if isinstance(team_spec.get("planning"), dict) else {}
    reasons: list[str] = []

    if normalize_capabilities(coverage.get("uncovered") or []):
        reasons.append("uncovered_capabilities")
    if float(confidence.get("coverage") or 0.0) < 1.0:
        reasons.append("coverage_below_1.00")
    if float(confidence.get("overall") or 0.0) < FORMATION_AUTO_OVERALL_MIN:
        reasons.append("overall_confidence_below_0.80")
    if float(confidence.get("requirement") or 0.0) < FORMATION_AUTO_REQUIREMENT_MIN:
        reasons.append("requirement_confidence_below_0.80")
    if float(confidence.get("capability_evidence") or 0.0) < FORMATION_AUTO_EVIDENCE_MIN:
        reasons.append("capability_evidence_below_0.75")
    if str(team_spec.get("uncertainty") or "high") != "low":
        reasons.append("team_spec_uncertainty")
    if str(task_profile.get("complexity") or "multi_role") not in {"simple", "focused"}:
        reasons.append("structured_multi_role_task")
    if list(plan.get("warnings") or []):
        reasons.append("formation_warnings")
    if baseline.get("decision_required") or list(baseline.get("required_agent_conflicts") or []):
        reasons.append("unresolved_member_constraints")
    if list(policy.get("risk_flags") or []) or list(planning.get("missing_info") or []):
        reasons.append("unresolved_team_spec_risk")
    if any(str(item).strip() for item in (payload.get("custom_capabilities") or [])):
        reasons.append("custom_capability_requirement")

    return bool(reasons), list(dict.fromkeys(reasons))


def _ready_runtime_model_options(runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable, ready runtime/model options for AI hints and local fallback."""

    options: list[dict[str, Any]] = []
    for runtime in runtimes:
        metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
        if str(metadata.get("availability_status") or "") != "ready":
            continue
        runtime_id = str(runtime.get("id") or "").strip()
        if not runtime_id:
            continue
        models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
        default_model_id = str(metadata.get("default_model_id") or "").strip()
        model_ids = [
            str(model.get("id") or model.get("model_id") or "").strip()
            for model in models
            if isinstance(model, dict)
            and str(model.get("id") or model.get("model_id") or "").strip()
        ]
        if not model_ids:
            continue
        if default_model_id not in model_ids:
            default_model_id = model_ids[0]
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or model.get("model_id") or "").strip()
            if not model_id:
                continue
            raw_capabilities = [
                str(item).strip()
                for item in (model.get("capabilities") or [])
                if str(item).strip()
            ]
            options.append({
                "runtime_id": runtime_id,
                "runtime_name": str(runtime.get("name") or runtime_id),
                "model_id": model_id,
                "capabilities": raw_capabilities,
                "normalized_capabilities": normalize_capabilities(raw_capabilities),
                "is_default": model_id == default_model_id,
            })
    return sorted(
        options,
        key=lambda item: (
            str(item["runtime_id"]),
            not bool(item["is_default"]),
            str(item["model_id"]),
        ),
    )


def _recommended_runtime_model(
    options: list[dict[str, Any]],
    *,
    required_capabilities: list[str],
    requested_runtime_id: str,
    requested_model_id: str,
) -> dict[str, Any] | None:
    """Validate an AI recommendation, otherwise rank a deterministic fallback."""

    if not options:
        return None
    exact = next((
        option
        for option in options
        if option["runtime_id"] == requested_runtime_id
        and option["model_id"] == requested_model_id
    ), None)
    if exact is not None:
        return exact

    scoped = [
        option for option in options
        if requested_runtime_id and option["runtime_id"] == requested_runtime_id
    ]
    candidates = scoped or options
    required = set(normalize_capabilities(required_capabilities))

    def rank(option: dict[str, Any]) -> tuple[int, int, str, str]:
        supported = set(option.get("normalized_capabilities") or [])
        overlap = len(required & supported)
        return (
            -overlap,
            -int(bool(option.get("is_default"))),
            str(option["runtime_id"]),
            str(option["model_id"]),
        )

    return min(candidates, key=rank)


def ready_runtime_model_options(runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Public, deterministic ready Runtime/model catalog for Runtime staffing."""

    return _ready_runtime_model_options(runtimes)


def recommend_runtime_model(
    options: list[dict[str, Any]],
    *,
    required_capabilities: list[str],
) -> dict[str, Any] | None:
    """Choose the best ready Runtime/model without provider-brand heuristics."""

    return _recommended_runtime_model(
        options,
        required_capabilities=required_capabilities,
        requested_runtime_id="",
        requested_model_id="",
    )


def formation_ai_context(
    payload: dict[str, Any],
    agents: list[dict[str, Any]],
    baseline: dict[str, Any],
    runtimes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the compact, evidence-only context consumed by Formation AI.

    The model audits a deterministic Fast draft. Provider brands, full system
    prompts and other execution instructions are intentionally excluded.
    """

    baseline_plan = baseline.get("formation_plan") if isinstance(baseline.get("formation_plan"), dict) else {}
    baseline_coverage = baseline_plan.get("coverage") if isinstance(baseline_plan.get("coverage"), dict) else {}
    relevant_capabilities = set(normalize_capabilities(baseline_coverage.get("required") or []))
    baseline_members = [
        member
        for member in (baseline.get("members") or [])
        if isinstance(member, dict)
    ]
    protected_agent_ids = {
        str(member.get("agent_id") or "")
        for member in baseline_members
        if str(member.get("agent_id") or "")
    }
    protected_agent_ids.update(
        str(member.get("agent_id") or "")
        for member in (baseline_plan.get("members") or [])
        if isinstance(member, dict) and bool(member.get("locked"))
    )

    runtime_by_id = {
        str(runtime.get("id") or ""): runtime
        for runtime in runtimes
        if isinstance(runtime, dict)
    }
    profiled_agents: list[tuple[dict[str, Any], AgentProfile]] = []
    for agent in [crew_builtin_agent_public(), *agents]:
        runtime = runtime_by_id.get(str(agent.get("runtime_id") or ""))
        profile = build_agent_profile(
            agent,
            runtime=None if isinstance(agent.get("profile"), dict) and agent.get("profile") else runtime,
        )
        if _profile_available_for_formation(profile):
            profiled_agents.append((agent, profile))

    def candidate_rank(item: tuple[dict[str, Any], AgentProfile]) -> tuple[int, float, str]:
        agent, profile = item
        agent_id = str(agent.get("id") or "")
        relevance = sum(
            profile.score(capability) * profile.confidence(capability)
            for capability in relevant_capabilities
        )
        return (
            0 if agent_id in protected_agent_ids else 1,
            -relevance,
            agent_id,
        )

    ranked_agents = sorted(profiled_agents, key=candidate_rank)
    protected_agents = [
        item for item in ranked_agents
        if str(item[0].get("id") or "") in protected_agent_ids
    ]
    optional_agents = [
        item for item in ranked_agents
        if str(item[0].get("id") or "") not in protected_agent_ids
    ]
    selected_agents = [
        *protected_agents,
        *optional_agents[:max(0, FORMATION_AI_MAX_AGENT_CANDIDATES - len(protected_agents))],
    ]
    agent_context: list[dict[str, Any]] = []
    for agent, profile in selected_agents:
        agent_id = str(agent.get("id") or "")
        assessments = [
            (capability, assessment)
            for capability, assessment in profile.capabilities.items()
            if assessment.confidence >= 0.35
        ]
        assessments.sort(key=lambda item: (
            0 if item[0] in relevant_capabilities else 1,
            -(item[1].score * item[1].confidence),
            item[0],
        ))
        evidence = []
        for capability, assessment in assessments[:FORMATION_AI_MAX_EVIDENCE_PER_AGENT]:
            evidence.append({
                "capability": capability,
                "score": round(assessment.score, 3),
                "confidence": round(assessment.confidence, 3),
                "sources": list(dict.fromkeys(item.source for item in assessment.evidence))[:2],
            })
        agent_context.append({
            "agent_id": agent_id,
            "name": _agent_name(agent),
            "availability": profile.availability,
            "model_binding_status": profile.model.get("binding_status") or "unverified",
            "capability_evidence": evidence,
        })

    raw_team_spec = baseline.get("team_spec") if isinstance(baseline.get("team_spec"), dict) else {}
    compact_team_spec = {
        key: raw_team_spec.get(key)
        for key in ("version", "execution_profile", "team_requirements")
        if key in raw_team_spec
    }
    runtime_options = sorted(
        _ready_runtime_model_options(runtimes),
        key=lambda item: (
            not bool(item["is_default"]),
            str(item["runtime_id"]),
            str(item["model_id"]),
        ),
    )[:FORMATION_AI_MAX_RUNTIME_OPTIONS]
    return {
        "team_input": {
            "name": str(payload.get("name") or "")[:500],
            "description": str(payload.get("description") or "")[:4096],
            "team_spec": compact_team_spec,
        },
        "immutable_constraints": {
            "leader_agent_id": str(baseline.get("leader_agent_id") or ""),
            "locked_agent_ids": [
                str(member.get("agent_id") or "")
                for member in (baseline_plan.get("members") or [])
                if isinstance(member, dict) and bool(member.get("locked"))
            ],
            "excluded_agent_ids": list(baseline_plan.get("excluded_agent_ids") or []),
        },
        "fast_baseline": {
            "members": [
                {
                    "agent_id": str(member.get("agent_id") or ""),
                    "role_key": str(member.get("role_key") or ""),
                    "assigned_capabilities": list(member.get("assigned_capabilities") or []),
                    "locked": bool(next((
                        item.get("locked")
                        for item in (baseline_plan.get("members") or [])
                        if isinstance(item, dict)
                        and item.get("agent_id") == member.get("agent_id")
                    ), False)),
                }
                for member in (baseline.get("members") or [])
                if isinstance(member, dict)
            ],
            "coverage": baseline_plan.get("coverage") or {},
            "confidence": baseline_plan.get("confidence") or {},
            "warnings": list(baseline_plan.get("warnings") or []),
        },
        "available_agents": agent_context,
        "standard_roles": [
            {
                "role_key": str(role.get("key") or ""),
                "label": str(role.get("label") or ""),
                "capabilities": list(role.get("capabilities") or []),
                "scope": str(role.get("description") or ""),
            }
            for role in all_role_public_payloads()
        ],
        "ready_runtime_options": [
            {
                "runtime_id": str(option["runtime_id"]),
                "model_id": str(option["model_id"]),
                "capabilities": list(option["capabilities"]),
                "is_default": bool(option["is_default"]),
            }
            for option in runtime_options
        ],
    }


def apply_formation_ai_audit(
    payload: dict[str, Any],
    agents: list[dict[str, Any]],
    baseline: dict[str, Any],
    audit: Any,
    runtimes: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Validate an AI audit, locally compile it and keep only a better result."""

    if not isinstance(audit, dict):
        return None, "invalid_ai_output"
    member_changes = audit.get("member_changes")
    legacy_proposed = audit.get("proposed_members")
    if not isinstance(member_changes, dict) and not isinstance(legacy_proposed, list):
        return None, "invalid_ai_output"

    candidates = [crew_builtin_agent_public(), *agents]
    agent_by_id = {
        str(agent.get("id") or ""): agent
        for agent in candidates
        if str(agent.get("id") or "")
    }
    runtime_by_id = {
        str(runtime.get("id") or ""): runtime
        for runtime in runtimes
        if isinstance(runtime, dict)
    }
    profiles = {
        agent_id: build_agent_profile(
            agent,
            runtime=(
                None
                if isinstance(agent.get("profile"), dict) and agent.get("profile")
                else runtime_by_id.get(str(agent.get("runtime_id") or ""))
            ),
        )
        for agent_id, agent in agent_by_id.items()
    }
    role_by_key = {
        str(role.get("key") or ""): role
        for role in all_role_public_payloads()
        if str(role.get("key") or "")
    }
    baseline_plan = baseline.get("formation_plan") if isinstance(baseline.get("formation_plan"), dict) else {}
    baseline_members = [
        member for member in (baseline.get("members") or [])
        if isinstance(member, dict)
    ]
    baseline_member_by_id = {
        str(member.get("agent_id") or ""): member
        for member in baseline_members
        if str(member.get("agent_id") or "")
    }
    locked_ids = {
        str(member.get("agent_id") or "")
        for member in (baseline_plan.get("members") or [])
        if isinstance(member, dict) and bool(member.get("locked"))
    }
    leader_id = str(baseline.get("leader_agent_id") or "")
    locked_ids.add(leader_id)
    excluded_ids = {
        str(agent_id)
        for agent_id in (baseline_plan.get("excluded_agent_ids") or [])
        if str(agent_id)
    }
    if leader_id not in agent_by_id:
        return None, "invalid_ai_output"

    if isinstance(member_changes, dict):
        raw_removed = member_changes.get("remove_agent_ids") or []
        raw_upserts = member_changes.get("upsert_members") or []
        if not isinstance(raw_removed, list) or not isinstance(raw_upserts, list):
            return None, "invalid_ai_output"
        removed_ids = {
            str(agent_id).strip()
            for agent_id in raw_removed[:12]
            if str(agent_id).strip()
        }
        if removed_ids & locked_ids:
            return None, "invalid_ai_output"
        proposed_by_id = {
            str(member.get("agent_id") or ""): dict(member)
            for member in baseline_members
            if str(member.get("agent_id") or "")
            and str(member.get("agent_id") or "") not in locked_ids
            and str(member.get("agent_id") or "") not in removed_ids
        }
        for item in raw_upserts[:12]:
            if not isinstance(item, dict):
                continue
            agent_id = str(item.get("agent_id") or "").strip()
            if not agent_id or agent_id in locked_ids:
                continue
            proposed_by_id[agent_id] = item
        proposed = list(proposed_by_id.values())
    else:
        proposed = legacy_proposed

    goal = "\n".join([
        str(payload.get("name") or ""),
        str(payload.get("description") or ""),
        str(payload.get("workflow") or ""),
        str(baseline.get("team_spec") or ""),
    ]).strip().lower()
    requirement_audit = audit.get("requirement_audit")
    requirement_audit = requirement_audit if isinstance(requirement_audit, dict) else {}
    baseline_coverage = baseline_plan.get("coverage") if isinstance(baseline_plan.get("coverage"), dict) else {}
    baseline_required = normalize_capabilities(baseline_coverage.get("required") or [])
    audited_required = list(baseline_required)
    for role in requirement_audit.get("required_roles") or []:
        if not isinstance(role, dict):
            continue
        evidence_quote = " ".join(str(role.get("evidence_quote") or "").lower().split())
        if len(evidence_quote) < 4 or evidence_quote not in " ".join(goal.split()):
            continue
        audited_required.extend(normalize_capabilities(role.get("required_capabilities") or []))
    audited_required = list(dict.fromkeys(audited_required))

    description = str(payload.get("description") or payload.get("name") or "").strip()
    members: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def append_member(
        *,
        agent_id: str,
        role_key: str,
        assigned_capabilities: list[str],
        reason: str,
        focus: str = "",
        deliverables: list[str] | None = None,
    ) -> None:
        if agent_id in seen_ids:
            return
        member = _member_payload(
            agent=agent_by_id[agent_id],
            role_key=role_key,
            capabilities=assigned_capabilities,
            description=description,
            workflow="",
            is_leader=agent_id == leader_id,
            selection_reason=reason[:500],
        )
        responsibility = dict(member.get("responsibility") or {})
        if focus:
            responsibility["mission"] = focus[:300]
        clean_deliverables = [
            str(item).strip()[:160]
            for item in (deliverables or [])
            if str(item).strip()
        ][:5]
        if clean_deliverables:
            responsibility["deliverables"] = clean_deliverables
        member["responsibility"] = public_responsibility(responsibility)
        members.append(member)
        seen_ids.add(agent_id)

    leader_baseline = baseline_member_by_id.get(leader_id)
    if leader_baseline is None:
        return None, "invalid_ai_output"
    append_member(
        agent_id=leader_id,
        role_key=str(leader_baseline.get("role_key") or "project_manager"),
        assigned_capabilities=list(leader_baseline.get("assigned_capabilities") or ["planning"]),
        reason=str(leader_baseline.get("selection_reason") or "固定 Leader。"),
    )
    if isinstance(leader_baseline.get("responsibility"), dict):
        members[-1]["responsibility"] = dict(leader_baseline["responsibility"])
    for agent_id in sorted(locked_ids - {leader_id}):
        member = baseline_member_by_id.get(agent_id)
        if member is None or agent_id in excluded_ids or agent_id not in agent_by_id:
            return None, "invalid_ai_output"
        append_member(
            agent_id=agent_id,
            role_key=str(member.get("role_key") or ""),
            assigned_capabilities=list(member.get("assigned_capabilities") or []),
            reason=str(member.get("selection_reason") or "保留用户锁定成员。"),
        )
        if isinstance(member.get("responsibility"), dict):
            members[-1]["responsibility"] = dict(member["responsibility"])

    grounded_scope_change = False
    for item in proposed[:12]:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "").strip()
        role_key = str(item.get("role_key") or "").strip()
        if (
            not agent_id
            or agent_id in seen_ids
            or agent_id in excluded_ids
            or agent_id not in agent_by_id
            or role_key not in role_by_key
        ):
            continue
        profile = profiles[agent_id]
        if not _profile_available_for_formation(profile):
            continue
        assigned = normalize_capabilities(item.get("assigned_capabilities") or [])
        if not assigned:
            assigned = normalize_capabilities(role_by_key[role_key].get("capabilities") or [])
        focus = str(item.get("responsibility_focus") or "").strip()
        evidence_quote = " ".join(str(item.get("evidence_quote") or "").lower().split())
        baseline_member = baseline_member_by_id.get(agent_id)
        if (
            len(evidence_quote) >= 4
            and evidence_quote in " ".join(goal.split())
            and baseline_member is not None
            and (
                str(baseline_member.get("role_key") or "") != role_key
                or (
                    focus
                    and str((baseline_member.get("responsibility") or {}).get("mission") or "") != focus
                )
            )
        ):
            grounded_scope_change = True
        append_member(
            agent_id=agent_id,
            role_key=role_key,
            assigned_capabilities=assigned,
            reason=str(item.get("reason") or "AI 根据目标与能力证据优化分工。"),
            focus=focus,
            deliverables=item.get("deliverables") if isinstance(item.get("deliverables"), list) else [],
        )

    members, overlap_warnings = _resolve_responsibility_overlaps(
        members,
        leader_id=leader_id,
        locked_agent_ids=locked_ids,
        agent_by_id=agent_by_id,
    )
    if not members:
        return None, "empty_ai_team"
    workflow = _render_workflow(members, agent_by_id)
    for member in members:
        member["role"] = intelligent_role_markdown(
            role_key=str(member.get("role_key") or ""),
            agent_name=_agent_name(agent_by_id.get(str(member.get("agent_id") or "")) or {}),
            team_goal=description,
            workflow=workflow,
            is_leader=member.get("agent_id") == leader_id,
            assigned_capabilities=list(member.get("assigned_capabilities") or []),
            responsibility=member.get("responsibility") if isinstance(member.get("responsibility"), dict) else None,
        )
        member["responsibility_markdown"] = member["role"]

    covered = list(dict.fromkeys(
        capability
        for member in members
        for capability in (member.get("assigned_capabilities") or [])
        if capability in audited_required
    ))
    uncovered = [capability for capability in audited_required if capability not in covered]
    protected_baseline_coverage = {
        capability
        for member in baseline_members
        for capability in (member.get("assigned_capabilities") or [])
        if capability in baseline_required
        and not (
            str(member.get("agent_id") or "") in profiles
            and profiles[str(member.get("agent_id"))].confidence(capability) >= 0.5
            and profiles[str(member.get("agent_id"))].score(capability) < 0.5
        )
    }
    if any(capability not in covered for capability in protected_baseline_coverage):
        return None, "baseline_coverage_regressed"

    def unsupported_count(items: list[dict[str, Any]]) -> int:
        return sum(
            1
            for member in items
            for capability in (member.get("assigned_capabilities") or [])
            if member.get("agent_id") in profiles
            and profiles[str(member.get("agent_id"))].confidence(capability) >= 0.5
            and profiles[str(member.get("agent_id"))].score(capability) < 0.5
        )

    def duplicate_count(items: list[dict[str, Any]]) -> int:
        signatures = [
            responsibility_signature(
                role_key=str(member.get("role_key") or ""),
                assigned_capabilities=list(member.get("assigned_capabilities") or []),
                responsibility=member.get("responsibility") if isinstance(member.get("responsibility"), dict) else None,
            )
            for member in items
            if member.get("agent_id") != leader_id
        ]
        return len(signatures) - len(set(signatures))

    def evidence_confidence(items: list[dict[str, Any]]) -> float:
        values = [
            profiles[str(member.get("agent_id"))].confidence(capability)
            for member in items
            if str(member.get("agent_id") or "") in profiles
            for capability in (member.get("assigned_capabilities") or [])
            if capability in CAPABILITIES
        ]
        return sum(values) / len(values) if values else 0.15

    separation_constraints = audit.get("separation_constraints") or []
    if not isinstance(separation_constraints, list):
        return None, "invalid_ai_output"
    normalized_goal = " ".join(goal.split())
    for constraint in separation_constraints[:12]:
        if not isinstance(constraint, dict):
            return None, "invalid_ai_output"
        separated_capabilities = normalize_capabilities(constraint.get("capabilities") or [])
        evidence_quote = " ".join(str(constraint.get("evidence_quote") or "").lower().split())
        if (
            not separated_capabilities
            or not str(constraint.get("independent_from") or "").strip()
            or len(evidence_quote) < 4
            or evidence_quote not in normalized_goal
        ):
            return None, "ungrounded_separation_constraint"

    ready_options = _ready_runtime_model_options(runtimes)
    staffing_plan = audit.get("staffing_plan")
    if staffing_plan is not None and not isinstance(staffing_plan, dict):
        return None, "invalid_ai_output"
    raw_staffing_members = (
        staffing_plan.get("members") or []
        if isinstance(staffing_plan, dict)
        else audit.get("staffing_gaps") or []
    )
    if not isinstance(raw_staffing_members, list):
        return None, "invalid_ai_output"

    staffing_gaps: list[dict[str, Any]] = []
    staffing_signatures: set[tuple[str, tuple[str, ...], str, str]] = set()
    for index, gap in enumerate(raw_staffing_members[:12]):
        if not isinstance(gap, dict):
            continue
        role_key = str(gap.get("role_key") or "").strip()
        gap_capabilities = normalize_capabilities(gap.get("required_capabilities") or [])
        gap_capabilities = [
            capability for capability in gap_capabilities
            if capability in audited_required and capability in uncovered
        ]
        if role_key not in role_by_key or not gap_capabilities or not ready_options:
            continue
        requested_runtime_id = str(gap.get("recommended_runtime_id") or "").strip()
        requested_model_id = str(gap.get("recommended_model_id") or "").strip()
        recommendation = _recommended_runtime_model(
            ready_options,
            required_capabilities=gap_capabilities,
            requested_runtime_id=requested_runtime_id,
            requested_model_id=requested_model_id,
        )
        if recommendation is None:
            continue
        signature = (
            role_key,
            tuple(sorted(gap_capabilities)),
            str(recommendation["runtime_id"]),
            str(recommendation["model_id"]),
        )
        if signature in staffing_signatures:
            continue
        staffing_signatures.add(signature)
        staffing_gaps.append({
            "gap_id": f"formation_gap_{index + 1}",
            "role_key": role_key,
            "role_label": str(role_by_key[role_key].get("label") or role_key),
            "required_capabilities": gap_capabilities,
            "responsibility_focus": str(gap.get("responsibility_focus") or "")[:300],
            "reason": str(gap.get("reason") or "当前成员无法独立覆盖这项职责。")[:500],
            "recommended_runtime_id": recommendation["runtime_id"],
            "recommended_runtime_name": recommendation["runtime_name"],
            "recommended_model_id": recommendation["model_id"],
        })

    baseline_unsupported = unsupported_count(baseline_members)
    candidate_unsupported = unsupported_count(members)
    baseline_duplicates = duplicate_count(baseline_members)
    candidate_duplicates = duplicate_count(members)
    baseline_evidence = evidence_confidence(baseline_members)
    candidate_evidence = evidence_confidence(members)
    same_or_better_coverage = len(covered) >= len(normalize_capabilities(baseline_coverage.get("covered") or []))
    material_improvements: list[str] = []
    if len(covered) > len(normalize_capabilities(baseline_coverage.get("covered") or [])):
        material_improvements.append("补充了目标所需能力覆盖")
    if candidate_unsupported < baseline_unsupported:
        material_improvements.append("修正了缺少能力证据的成员分工")
    if candidate_duplicates < baseline_duplicates:
        material_improvements.append("消除了重复常驻职责")
    if same_or_better_coverage and len(members) < len(baseline_members):
        material_improvements.append("以更少成员保持了同等能力覆盖")
    if candidate_evidence >= baseline_evidence + 0.05:
        material_improvements.append("提高了成员分工的能力证据可信度")
    if grounded_scope_change:
        material_improvements.append("根据目标原文修正了成员职责范围")
    if staffing_gaps:
        material_improvements.append("识别出当前团队无法覆盖的独立职责")
    if not material_improvements:
        return None, "no_material_improvement"
    if candidate_unsupported > baseline_unsupported or candidate_duplicates > baseline_duplicates:
        return None, "quality_regressed"

    assessment_confidences = [
        profiles[str(member.get("agent_id"))].confidence(capability)
        for member in members
        if str(member.get("agent_id") or "") in profiles
        for capability in (member.get("assigned_capabilities") or [])
        if capability in CAPABILITIES
    ]
    requirement_confidence = max(
        0.85,
        float((baseline_plan.get("confidence") or {}).get("requirement") or 0.0),
    )
    coverage_confidence = len(covered) / len(audited_required) if audited_required else 1.0
    evidence_value = (
        sum(assessment_confidences) / len(assessment_confidences)
        if assessment_confidences else 0.15
    )
    warnings = [*overlap_warnings]
    if uncovered:
        warnings.append("未覆盖能力：" + "、".join(CAPABILITY_LABELS[item] for item in uncovered))
    formation_members = []
    for member in members:
        prior = next((
            item for item in (baseline_plan.get("members") or [])
            if isinstance(item, dict) and item.get("agent_id") == member.get("agent_id")
        ), {})
        formation_members.append({
            "agent_id": member["agent_id"],
            "role_key": member.get("role_key") or "",
            "role_label": member.get("role_label") or "",
            "assigned_capabilities": list(member.get("assigned_capabilities") or []),
            "responsibility": dict(member.get("responsibility") or {}),
            "responsibility_markdown": str(member.get("responsibility_markdown") or ""),
            "selection_source": str(prior.get("selection_source") or "ai"),
            "locked": bool(prior.get("locked", False)),
            "selection_reason": str(member.get("selection_reason") or ""),
        })
    result_plan = FormationPlan(
        version=1,
        leader_agent_id=leader_id,
        members=formation_members,
        coverage={"required": audited_required, "covered": covered, "uncovered": uncovered},
        confidence={
            "requirement": round(requirement_confidence, 4),
            "capability_evidence": round(evidence_value, 4),
            "coverage": round(coverage_confidence, 4),
            "overall": round((requirement_confidence + evidence_value + coverage_confidence) / 3, 4),
        },
        staffing_mode="ai_reviewed",
        excluded_agent_ids=sorted(excluded_ids),
        reasons=[
            *[str(item)[:500] for item in (audit.get("reasons") or []) if str(item).strip()][:6],
            *material_improvements,
        ],
        warnings=warnings,
    )
    return {
        "leader_agent_id": leader_id,
        "workflow": workflow,
        "members": [{**member, "sort_order": index} for index, member in enumerate(members)],
        "decision_required": False,
        "required_agent_conflicts": [],
        "staffing_decision_required": bool(staffing_gaps),
        "staffing_gaps": staffing_gaps,
        "staffing_only_improvement": bool(
            staffing_gaps
            and material_improvements == ["识别出当前团队无法覆盖的独立职责"]
        ),
        "ai_material_improvements": material_improvements,
        "team_spec": baseline.get("team_spec") or {},
        "formation_plan": result_plan.to_dict(),
        "reasons": result_plan.reasons,
    }, ""


def build_team_draft(payload: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    leader_id = str(payload.get("leader_agent_id") or "").strip()
    candidates = [crew_builtin_agent_public(), *agents]
    agent_by_id = {str(agent.get("id") or ""): agent for agent in candidates}
    if leader_id not in agent_by_id:
        leader_id = ""
    goal = "\n".join(item for item in (name, description) if item).strip()
    team_spec_input = _formation_team_spec_input(payload, goal)
    spec = build_team_spec(team_spec_input)
    required_capabilities = _required_capabilities(goal, spec)
    draft_slots = payload.get("draft_slots")
    requested_slots = [slot for slot in draft_slots if isinstance(slot, dict)] if isinstance(draft_slots, list) else []
    profiles = {
        str(agent.get("id") or ""): build_agent_profile(agent)
        for agent in candidates
    }
    slots: list[dict[str, Any]] = []
    if leader_id:
        slots.append({
            "slot_id": "leader",
            "role_key": "project_manager",
            "role_label": "Leader",
            "capability": "planning",
            "agent_id": leader_id,
            "is_leader": True,
            "required": True,
            "locked": True,
        })
    used = {leader_id} if leader_id else set()

    slot_specs: list[dict[str, Any]] = []
    if requested_slots:
        for slot in requested_slots[:6]:
            role_key = str(slot.get("role_key") or "").strip()
            if not role_key or str(role_preset(role_key).get("key") or "") != role_key:
                continue
            capability = normalize_capability(slot.get("capability"))
            if not capability:
                capability = _capability_for_role_key(role_key)
            if capability == "planning":
                capability = "implementation"
            agent_id = str(slot.get("agent_id") or slot.get("suggested_agent_id") or "").strip()
            slot_specs.append({
                "role_key": role_key,
                "capability": capability,
                "agent_id": agent_id if agent_id in agent_by_id and agent_id != leader_id else "",
                "required": bool(slot.get("required", True)),
            })
    if not slot_specs:
        slot_specs = [
            {
                "role_key": CAPABILITY_ROLE_KEYS[capability],
                "capability": capability,
                "agent_id": "",
                "required": capability != "documentation",
            }
            for capability in required_capabilities
        ]

    for index, slot_spec in enumerate(slot_specs):
        capability = str(slot_spec.get("capability") or "").strip()
        role_key = str(slot_spec.get("role_key") or "").strip() or CAPABILITY_ROLE_KEYS.get(capability, "fullstack_developer")
        suggested_id = str(slot_spec.get("agent_id") or "").strip()
        if suggested_id in used:
            suggested_id = ""
        available = [
            agent for agent in candidates
            if str(agent.get("id") or "") not in used
        ]
        if not suggested_id:
            external = [agent for agent in available if str(agent.get("id") or "") != CREW_BUILTIN_AGENT_ID]
            if external:
                available = external
            suggested = max(
                available,
                key=lambda agent: profiles[str(agent.get("id") or "")].score(capability),
                default=None,
            )
            suggested_id = str((suggested or {}).get("id") or "")
        if suggested_id:
            used.add(suggested_id)
        preset = role_preset(role_key)
        slots.append({
            "slot_id": f"{capability or 'role'}_{index + 1}",
            "role_key": role_key,
            "role_label": preset["label"],
            "capability": capability,
            "agent_id": suggested_id,
            "suggested_agent_id": suggested_id,
            "is_leader": False,
            "required": bool(slot_spec.get("required", True)),
            "locked": False,
        })
    workflow = _workflow_from_slots(slots, agent_by_id)
    draft_description = description or _draft_description(name, required_capabilities)
    return {
        "description": draft_description,
        "workflow": workflow,
        "slots": slots,
        "team_spec": spec.to_dict(),
    }


def _draft_description(name: str, required_capabilities: list[str]) -> str:
    if not name:
        return ""
    capability_text = "、".join(CAPABILITY_LABELS[cap] for cap in required_capabilities)
    if capability_text:
        return (
            f"1. 负责范围：围绕{name}承接并拆解用户目标。\n"
            f"2. 所需能力：需要{capability_text}与 Leader 统筹能力。\n"
            "3. 交付结果：形成可直接使用、可追踪的团队成果。\n"
            "4. 验收标准：成员结果经 Leader 审阅、风险处理和统一汇总后交付。"
        )
    return (
        f"1. 负责范围：围绕{name}处理目标明确的协作任务。\n"
        "2. 所需能力：由 Leader 按任务内容匹配合适成员与能力。\n"
        "3. 交付结果：形成结构清楚、可直接使用的团队成果。\n"
        "4. 验收标准：过程可追踪，成员结果经 Leader 审阅并统一交付。"
    )


def _workflow_from_slots(slots: list[dict[str, Any]], agent_by_id: dict[str, dict[str, Any]]) -> str:
    if not slots:
        return ""
    leader_slot = next((slot for slot in slots if bool(slot.get("is_leader"))), {})
    leader_name = _agent_name(agent_by_id.get(str(leader_slot.get("agent_id") or "")) or {"name": "Leader"})
    lines = [f"1. Leader：{leader_name}，负责理解目标、拆解任务、协调推进、验收结果和最终交付。"]
    step = 2
    for slot in slots:
        if bool(slot.get("is_leader")):
            continue
        agent_name = _agent_name(agent_by_id.get(str(slot.get("agent_id") or "")) or {"name": "待选择"})
        role_key = _slot_role_key(slot)
        preset = role_preset(role_key)
        responsibility = str(preset["description"]).strip().rstrip("。")
        if responsibility.startswith("负责"):
            responsibility = responsibility[2:]
        lines.append(f"{step}. {preset['label']}：建议【{agent_name}】担任，负责{responsibility}。")
        step += 1
    if step > 2:
        lines.append(f"{step}. 各成员提交结果后，由 {leader_name} 审阅并汇总交付。")
    return "\n".join(lines)


def enrich_team_member_role(
    member: dict[str, Any],
    *,
    agent: dict[str, Any],
    workflow: str,
    description: str,
    is_leader: bool = False,
) -> dict[str, Any]:
    role_key = str(member.get("role_key") or "").strip() or infer_role_key(str(member.get("role") or ""), is_leader=is_leader)
    preset = role_preset(role_key)
    role = str(member.get("role") or "").strip() or intelligent_role_markdown(
        role_key=str(preset["key"]),
        agent_name=_agent_name(agent),
        team_goal=description,
        workflow=workflow,
        is_leader=is_leader,
    )
    return {
        **member,
        "role": role,
        "role_key": preset["key"],
        "role_label": preset["label"],
        "capabilities": list(preset.get("capabilities") or []),
        "workflow_lane": preset.get("workflow_lane") or "build",
    }


def fallback_team_suggestion(payload: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
    return fast_team_suggestion(payload, agents)


def suggest_role_description(payload: dict[str, Any], agent: dict[str, Any] | None = None) -> dict[str, Any]:
    role_key = str(payload.get("role_key") or "").strip()
    if not role_key:
        role_key = infer_role_key(
            str(payload.get("current_description") or ""),
            is_leader=bool(payload.get("is_leader")),
        )
    preset = role_preset(role_key)
    agent_name = str(payload.get("agent_name") or (agent or {}).get("name") or "").strip()
    team_goal = "\n".join(
        part
        for part in [
            str(payload.get("name") or "").strip(),
            str(payload.get("description") or payload.get("team_description") or "").strip(),
        ]
        if part
    )
    workflow = str(payload.get("workflow") or "").strip()
    return {
        **role_public_payload(preset),
        "role": intelligent_role_markdown(
            role_key=str(preset["key"]),
            agent_name=agent_name,
            team_goal=team_goal,
            workflow=workflow,
            is_leader=bool(payload.get("is_leader")),
        ),
    }
