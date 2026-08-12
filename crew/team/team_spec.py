"""TeamSpec: the structured task contract shared by Team subsystems.

``TeamSpec`` describes what a user task explicitly requires.  It does not
choose members, trigger runtime staffing, or infer a workflow from business
words in a free-form prompt.  Formation and runtime planning consume the
normalized contract, while semantic interpretation belongs to their own
input boundary (for example, a structured form or ``PlanningDecision``).

The string input accepted by :func:`build_team_spec` is a migration boundary
for older callers.  It preserves the goal text and deliberately leaves all
task requirements unspecified; it is not a keyword-based fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

from crew.team.capabilities import normalize_capabilities


TeamIntent = Literal["chat", "question", "research", "implementation", "testing", "documentation", "mixed"]
TeamComplexity = Literal["simple", "focused", "multi_role"]
PlanStrategy = Literal["direct", "rule_dag", "llm_dag", "planning_role_first", "require_user_review"]
StaffingStrategy = Literal["fixed_team", "suggest_only", "ask_before_fill"]
ReflectionPolicy = Literal["none", "on_failure", "after_planning", "before_final", "high_risk"]


@dataclass(frozen=True)
class TeamSpec:
    version: int
    goal: str
    collaboration_mode: str = "leader_mesh"
    task_profile: dict[str, str] = field(default_factory=dict)
    execution_profile: dict[str, Any] = field(default_factory=dict)
    team_requirements: dict[str, Any] = field(default_factory=dict)
    planning: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    deliverables: list[dict[str, str]] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    risk_level: str = "low"
    uncertainty: str = "low"
    planner_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TeamSpecInput = Mapping[str, Any] | TeamSpec | str | None


def _unique(values: list[str], *, limit: int = 8) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:limit]


def _text_list(value: Any, *, limit: int = 8) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return _unique([str(item).strip() for item in values], limit=limit)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _explicit_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _normalize_deliverables(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            item_type = str(item.get("type") or "").strip()
            description = str(item.get("description") or "").strip()
            if item_type or description:
                normalized.append({"type": item_type or "answer", "description": description})
        elif str(item).strip():
            normalized.append({"type": "answer", "description": str(item).strip()})
    return normalized[:8]


def _default_execution_profile(source: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _mapping(source.get("execution_profile"))
    runtime_keys = (
        "requested_mode",
        "selected_mode",
        "budget",
        "turn_kind",
        "turn_decision_source",
        "profile_source",
    )
    profile: dict[str, Any] = {}
    for key in runtime_keys:
        if key in explicit:
            profile[key] = explicit[key]
        elif key in source:
            profile[key] = source[key]
    return profile


def _default_task_profile(source: Mapping[str, Any]) -> dict[str, str]:
    explicit = _mapping(source.get("task_profile"))
    legacy = _mapping(source.get("execution_profile"))
    return {
        "intent": str(
            explicit.get("intent")
            or source.get("intent")
            or legacy.get("intent")
            or "mixed"
        ),
        "complexity": str(
            explicit.get("complexity")
            or source.get("complexity")
            or legacy.get("complexity")
            or "focused"
        ),
        "deliverable_shape": str(
            explicit.get("deliverable_shape")
            or source.get("deliverable_shape")
            or legacy.get("deliverable_shape")
            or "unknown"
        ),
    }


def _explicit_workflow_lanes(
    source: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> list[str]:
    """Return the canonical workflow lanes from explicit input only.

    ``needs_*`` is accepted as a compatibility input for existing callers, but
    it is immediately projected into ``team_requirements.workflow_lanes``.
    Consumers should use that canonical field instead of the legacy flags.
    """

    explicit_lanes = requirements.get("workflow_lanes")
    if explicit_lanes is None:
        explicit_lanes = source.get("workflow_lanes")
    if explicit_lanes is None:
        explicit_lanes = _mapping(source.get("execution_profile")).get("required_lanes")
    lanes = _text_list(explicit_lanes, limit=8)
    legacy_lanes = {
        "needs_build": "build",
        "needs_verification": "verify",
        "needs_docs": "docs",
    }
    for flag, lane in legacy_lanes.items():
        legacy_profile = _mapping(source.get("execution_profile"))
        explicit_value = legacy_profile.get(flag) if flag in legacy_profile else source.get(flag)
        if _explicit_bool(explicit_value):
            lanes.append(lane)
    return _unique(lanes, limit=8)


def _build_normalized_spec(source: Mapping[str, Any]) -> TeamSpec:
    requirements_input = _mapping(source.get("team_requirements"))
    task_profile = _default_task_profile(source)
    execution_profile = _default_execution_profile(source)
    explicit_capabilities = requirements_input.get("capabilities")
    if explicit_capabilities is None:
        explicit_capabilities = source.get("required_capabilities")
    capabilities = normalize_capabilities(_text_list(explicit_capabilities, limit=16))

    roles = _text_list(
        requirements_input.get("roles")
        if requirements_input.get("roles") is not None
        else source.get("required_roles"),
        limit=8,
    )
    lanes = _explicit_workflow_lanes(source, requirements_input)

    planning_input = _mapping(source.get("planning"))
    policy_input = _mapping(source.get("policy"))
    missing_info = _text_list(
        planning_input.get("missing_info")
        if planning_input.get("missing_info") is not None
        else source.get("missing_info"),
        limit=8,
    )
    risk_flags = _text_list(policy_input.get("risk_flags"), limit=8)
    constraints = _text_list(
        policy_input.get("constraints")
        if policy_input.get("constraints") is not None
        else source.get("constraints"),
        limit=8,
    )
    consent_actions = _text_list(policy_input.get("consent_required_actions"), limit=8)
    success_criteria = _text_list(source.get("success_criteria"), limit=8)
    deliverables = _normalize_deliverables(source.get("deliverables"))

    has_structured_requirements = bool(
        capabilities or roles or lanes or deliverables or success_criteria
    )
    raw_goal = str(source.get("goal") or "").strip()
    explicit_uncertainty = str(source.get("uncertainty") or "").strip()
    uncertainty = explicit_uncertainty or ("low" if has_structured_requirements else "high")
    explicit_risk_level = str(source.get("risk_level") or "").strip()
    risk_level = explicit_risk_level or ("high" if risk_flags else "low")

    planning = {
        "strategy": str(planning_input.get("strategy") or "direct"),
        "reflection_policy": str(planning_input.get("reflection_policy") or "on_failure"),
        "missing_info": missing_info,
        "build_plan_mode": str(planning_input.get("build_plan_mode") or "auto"),
        "verify_plan_mode": str(planning_input.get("verify_plan_mode") or "required"),
        "user_review_gate": str(planning_input.get("user_review_gate") or "on_risk"),
    }
    policy = {
        "user_team_locked": _explicit_bool(policy_input.get("user_team_locked"), True),
        "staffing_strategy": str(policy_input.get("staffing_strategy") or "suggest_only"),
        "constraints": constraints,
        "risk_flags": risk_flags,
        "consent_required_actions": consent_actions,
    }
    planner_notes = _text_list(source.get("planner_notes"), limit=8)
    if not raw_goal:
        planner_notes = _unique(["用户目标为空，需要先补充任务目标。", *planner_notes])
    elif not has_structured_requirements:
        planner_notes = _unique([
            "仅收到非结构化目标文本；未推断角色、能力或交付阶段。",
            *planner_notes,
        ])

    return TeamSpec(
        version=3,
        goal=raw_goal,
        collaboration_mode=str(source.get("collaboration_mode") or "leader_mesh"),
        task_profile=task_profile,
        execution_profile=execution_profile,
        team_requirements={
            "roles": roles,
            "workflow_lanes": lanes,
            "capabilities": capabilities,
        },
        planning=planning,
        policy=policy,
        deliverables=deliverables,
        success_criteria=success_criteria,
        risk_level=risk_level,
        uncertainty=uncertainty,
        planner_notes=planner_notes,
    )


def build_team_spec(source: TeamSpecInput = None) -> TeamSpec:
    """Normalize one explicit TeamSpec input.

    Preferred input is a mapping containing ``goal`` plus structured fields
    such as ``team_requirements``, ``deliverables`` and ``success_criteria``.
    Passing a string is supported only for migration: its text is retained as
    ``goal`` and no role, capability, intent, deliverable, or workflow stage
    is inferred from that text.
    """
    if isinstance(source, TeamSpec):
        return source
    if isinstance(source, Mapping):
        return _build_normalized_spec(source)
    return _build_normalized_spec({"goal": str(source or "")})


def team_spec_from_planning_decision(
    base_spec: TeamSpec,
    decision: Any,
) -> TeamSpec:
    """Project one structured PlanningDecision into the shared TeamSpec.

    PlanningDecision understands the free-form goal once.  This function is
    the data-contract boundary: downstream Formation and Workflow consumers
    receive the same normalized capabilities, lanes, deliverables and risk
    information instead of interpreting the prompt independently.
    """

    capabilities = normalize_capabilities([
        *(base_spec.team_requirements.get("capabilities") or []),
        *(
            capability
            for unit in decision.work_units
            for capability in unit.required_capabilities
        ),
        *(["review", "verification"] if decision.quality_policy in {
            "independent_review", "evaluator_optimizer",
        } else []),
    ])
    lane_by_kind = {
        "plan": "plan",
        "research": "plan",
        "analysis": "plan",
        "design": "design",
        "build": "build",
        "verify": "verify",
        "docs": "docs",
    }
    lanes = list(dict.fromkeys([
        *(base_spec.team_requirements.get("workflow_lanes") or []),
        *(
            lane_by_kind[unit.kind]
            for unit in decision.work_units
            if unit.kind in lane_by_kind
        ),
        *(["verify"] if decision.quality_policy in {
            "independent_review", "evaluator_optimizer",
        } else []),
    ]))
    decision_deliverables = [
        {
            "type": str(unit.kind or "answer"),
            "description": str(unit.expected_output or unit.objective).strip(),
        }
        for unit in decision.work_units
        if str(unit.expected_output or unit.objective).strip()
    ]
    planning = {
        **dict(base_spec.planning or {}),
        "strategy": "llm_dag",
        "reflection_policy": "after_planning" if decision.quality_policy != "none" else "none",
        "missing_info": list(decision.critical_missing_info),
    }
    requirements = {
        **dict(base_spec.team_requirements or {}),
        "capabilities": capabilities,
        "workflow_lanes": lanes,
    }
    notes = [
        "TeamSpec 已由 PlanningDecision 的结构化工作单元生成；下游不再从用户目标重复推断。",
        *list(base_spec.planner_notes or []),
    ]
    return TeamSpec(
        **{
            **base_spec.to_dict(),
            "task_profile": dict(base_spec.task_profile or {}),
            "team_requirements": requirements,
            "planning": planning,
            "deliverables": [
                *list(base_spec.deliverables or []),
                *decision_deliverables,
            ][:8],
            "success_criteria": list(dict.fromkeys([
                *list(base_spec.success_criteria or []),
                *(
                    str(unit.expected_output or unit.objective).strip()
                    for unit in decision.work_units
                    if str(unit.expected_output or unit.objective).strip()
                ),
            ]))[:8],
            "risk_level": decision.risk_level,
            "uncertainty": decision.semantic_uncertainty,
            "planner_notes": list(dict.fromkeys(notes))[:8],
        }
    )
