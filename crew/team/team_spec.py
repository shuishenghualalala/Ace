"""TeamSpec: the structured task contract shared by Team subsystems."""

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


TeamSpecInput = Mapping[str, Any] | TeamSpec | None

_RUNTIME_EXECUTION_KEYS = frozenset({
    "requested_mode",
    "selected_mode",
    "budget",
    "turn_kind",
    "turn_decision_source",
    "profile_source",
})
_TASK_PROFILE_KEYS = frozenset({"intent", "complexity", "deliverable_shape"})
_TEAM_REQUIREMENT_KEYS = frozenset({"roles", "workflow_lanes", "capabilities"})
_LEGACY_TOP_LEVEL_KEYS = frozenset({
    "intent",
    "complexity",
    "deliverable_shape",
    "required_capabilities",
    "required_roles",
    "workflow_lanes",
    "required_lanes",
    "needs_build",
    "needs_verification",
    "needs_docs",
})


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
    unknown = sorted(set(explicit) - _RUNTIME_EXECUTION_KEYS)
    if unknown:
        raise ValueError(
            "TeamSpec.execution_profile 只允许运行控制字段，非法字段："
            + ", ".join(unknown)
        )
    return {key: explicit[key] for key in _RUNTIME_EXECUTION_KEYS if key in explicit}


def _default_task_profile(source: Mapping[str, Any]) -> dict[str, str]:
    explicit = _mapping(source.get("task_profile"))
    unknown = sorted(set(explicit) - _TASK_PROFILE_KEYS)
    if unknown:
        raise ValueError(
            "TeamSpec.task_profile 只允许任务语义字段，非法字段："
            + ", ".join(unknown)
        )
    return {
        "intent": str(explicit.get("intent") or "mixed"),
        "complexity": str(explicit.get("complexity") or "focused"),
        "deliverable_shape": str(explicit.get("deliverable_shape") or "unknown"),
    }


def _explicit_workflow_lanes(
    source: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> list[str]:
    del source
    return _text_list(requirements.get("workflow_lanes"), limit=8)


def _build_normalized_spec(source: Mapping[str, Any]) -> TeamSpec:
    legacy_fields = sorted(set(source) & _LEGACY_TOP_LEVEL_KEYS)
    if legacy_fields:
        raise ValueError(
            "TeamSpec 必须使用结构化字段，禁止旧兼容字段："
            + ", ".join(legacy_fields)
        )
    requirements_input = _mapping(source.get("team_requirements"))
    unknown_requirements = sorted(set(requirements_input) - _TEAM_REQUIREMENT_KEYS)
    if unknown_requirements:
        raise ValueError(
            "TeamSpec.team_requirements 只允许 roles、workflow_lanes、capabilities，非法字段："
            + ", ".join(unknown_requirements)
        )
    task_profile = _default_task_profile(source)
    execution_profile = _default_execution_profile(source)
    explicit_capabilities = requirements_input.get("capabilities")
    capabilities = normalize_capabilities(_text_list(explicit_capabilities, limit=16))

    roles = _text_list(
        requirements_input.get("roles"),
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

    Input must be a mapping containing ``goal`` plus structured fields such as
    ``task_profile``, ``team_requirements`` and ``deliverables``.  A free-form
    string is deliberately rejected so that this contract has no hidden
    migration or keyword-inference path.
    """
    if isinstance(source, TeamSpec):
        return source
    if isinstance(source, Mapping):
        return _build_normalized_spec(source)
    if source is None:
        return _build_normalized_spec({})
    raise TypeError(
        "build_team_spec 只接受 TeamSpec、结构化 Mapping 或 None；不再接受字符串目标。"
    )


def team_spec_for_creation(
    source: TeamSpecInput = None,
    *,
    description: str = "",
) -> dict[str, Any]:
    """Build the durable TeamSpec from the team-creation form.

    Team creation is a structured UI flow, not a chat turn. When the form
    does not provide a TeamSpec, only its explicit description is used as the
    initial goal; the team name is intentionally not treated as task
    semantics. The normalized snapshot is persisted once the user confirms
    the roster.
    """

    if source is None:
        source = {"goal": str(description or "").strip()}
    return build_team_spec(source).to_dict()


def persisted_team_spec_for_turn(source: Mapping[str, Any], goal: str) -> dict[str, Any]:
    """Project a persisted TeamSpec snapshot into the current turn contract.

    External teams created before TeamSpec V3 may keep task semantics in
    ``execution_profile``. That storage compatibility is handled only at the
    persistence boundary; explicit runtime input still goes through the strict
    ``build_team_spec`` validation above. The current user goal always wins.
    """

    raw = dict(source)
    legacy_execution = _mapping(raw.get("execution_profile"))
    task_profile = _mapping(raw.get("task_profile"))
    for key in _TASK_PROFILE_KEYS:
        if key not in task_profile and key in legacy_execution:
            task_profile[key] = legacy_execution[key]

    return {
        **raw,
        "goal": str(goal or "").strip(),
        "task_profile": task_profile,
        "execution_profile": {
            key: legacy_execution[key]
            for key in _RUNTIME_EXECUTION_KEYS
            if key in legacy_execution
        },
    }


def team_spec_from_planning_decision(
    base_spec: TeamSpec,
    decision: Any,
) -> TeamSpec:
    """Project one structured PlanningDecision into the shared TeamSpec.

    PlanningDecision understands the free-form goal once. This function is
    the current-turn data-contract boundary: downstream Workflow consumers
    receive the same normalized task profile, capabilities, lanes,
    deliverables and risk information instead of interpreting the prompt
    independently. Durable team policy is retained, while durable task
    requirements are not merged into the current turn.
    """

    # The persisted TeamSpec describes the team's durable boundary. Once the
    # current prompt has been understood, its work units become the source of
    # truth for this turn. Unioning the two would leak a durable build/verify
    # lane into an otherwise plan-only request.
    work_units = [
        unit
        for unit in decision.work_units
        if unit.id != "leader" and not unit.id.startswith("leader_")
    ]
    capabilities = normalize_capabilities([
        capability
        for unit in work_units
        for capability in unit.required_capabilities
    ] + (
        ["review", "verification"]
        if decision.quality_policy in {"independent_review", "evaluator_optimizer"}
        else []
    ))
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
        lane_by_kind[unit.kind]
        for unit in work_units
        if unit.kind in lane_by_kind
    ] + (
        ["verify"]
        if decision.quality_policy in {"independent_review", "evaluator_optimizer"}
        else []
    )))
    decision_deliverables = [
        {
            "type": str(unit.kind or "answer"),
            "description": str(unit.expected_output or unit.objective).strip(),
        }
        for unit in work_units
        if str(unit.expected_output or unit.objective).strip()
    ]
    kinds = {str(unit.kind or "other").strip().lower() for unit in work_units}
    if "build" in kinds and "verify" in kinds:
        intent = "mixed"
        deliverable_shape = "artifact"
    elif "build" in kinds:
        intent = "implementation"
        deliverable_shape = "artifact"
    elif "verify" in kinds:
        intent = "testing"
        deliverable_shape = "verification"
    elif kinds & {"research", "analysis"}:
        intent = "research"
        deliverable_shape = "research"
    elif kinds & {"plan", "design", "docs"}:
        intent = "documentation"
        deliverable_shape = "docs"
    else:
        intent = str(base_spec.task_profile.get("intent") or "mixed")
        deliverable_shape = str(base_spec.task_profile.get("deliverable_shape") or "unknown")
    if len(work_units) <= 1:
        complexity = "simple"
    elif len(work_units) <= 3:
        complexity = "focused"
    else:
        complexity = "multi_role"
    planning = {
        "strategy": "llm_dag",
        "reflection_policy": "after_planning" if decision.quality_policy != "none" else "none",
        "missing_info": list(decision.critical_missing_info),
        "build_plan_mode": "auto" if "build" in lanes else "skip",
        "verify_plan_mode": "required" if "verify" in lanes else "skip",
        "user_review_gate": str((base_spec.planning or {}).get("user_review_gate") or "on_risk"),
    }
    requirements = {
        "roles": [],
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
            "task_profile": {
                "intent": intent,
                "complexity": complexity,
                "deliverable_shape": deliverable_shape,
            },
            "team_requirements": requirements,
            "planning": planning,
            "deliverables": decision_deliverables[:8],
            "success_criteria": [
                str(unit.expected_output or unit.objective).strip()
                for unit in work_units
                if str(unit.expected_output or unit.objective).strip()
            ][:8],
            "risk_level": decision.risk_level,
            "uncertainty": decision.semantic_uncertainty,
            "planner_notes": list(dict.fromkeys(notes))[:8],
        }
    )
