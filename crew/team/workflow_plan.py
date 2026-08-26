"""WorkflowPlan V1 and the constrained semantic planning front-end.

This module owns the stable per-turn planning contract.  It intentionally does
not know about SQLite or Team runtime state: Dynamic Kanban persists the plan
snapshot and projects its nodes into mutable execution rows.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

from crew.team.capabilities import normalize_capabilities, normalize_capability

PlanningMode = Literal["auto", "fast", "standard", "ai"]
DependencyPattern = Literal["sequential", "parallel_merge", "staged"]
QualityPolicy = Literal["none", "leader_review", "independent_review", "evaluator_optimizer"]
Level = Literal["low", "medium", "high"]
RequestedScope = Literal["plan_only", "execute", "uncertain"]

WORKFLOW_PLAN_VERSION = 1
_WORK_UNIT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_planning_mode(value: object, *, default: PlanningMode = "auto") -> PlanningMode:
    mode = str(value or "").strip().lower()
    return mode if mode in {"auto", "fast", "standard", "ai"} else default  # type: ignore[return-value]


def normalize_plan_node_refs(raw: Any) -> list[str]:
    """Normalize a plan-change dependency field into unique node ids."""

    items = [raw] if isinstance(raw, str) else list(raw or [])
    refs: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in refs:
            refs.append(value)
    return refs


def plan_edges_have_cycle(node_ids: set[str], edges: list[Any]) -> bool:
    """Return whether the supplied plan edges contain a cycle."""

    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        if edge.parent_id not in node_ids or edge.child_id not in node_ids:
            continue
        outgoing.setdefault(edge.parent_id, []).append(edge.child_id)
        indegree[edge.child_id] = indegree.get(edge.child_id, 0) + 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for child_id in outgoing.get(node_id, []):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
    return visited != len(node_ids)


def safe_plan_node_id(value: str, *, fallback: str = "node") -> str:
    text = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:48] or fallback


@dataclass(frozen=True)
class WorkUnit:
    id: str
    objective: str
    display_title: str = ""
    required_capabilities: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    kind: str = "other"
    needs_independent_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanningDecision:
    goal_clarity: Level = "medium"
    requested_scope: RequestedScope = "uncertain"
    critical_missing_info: list[str] = field(default_factory=list)
    dependency_pattern: DependencyPattern = "sequential"
    quality_policy: QualityPolicy = "leader_review"
    dynamic_discovery: bool = False
    conditional_branching: bool = False
    iteration_until_convergence: bool = False
    risk_level: Level = "low"
    semantic_uncertainty: Level = "medium"
    work_units: list[WorkUnit] = field(default_factory=list)
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowPlan:
    task: dict[str, Any]
    planning: dict[str, Any]
    nodes: list[dict[str, Any]]
    edges: list[dict[str, str]]
    budget_snapshot: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    version: int = WORKFLOW_PLAN_VERSION
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def planning_decision_messages(
    *,
    goal: str,
    team_spec: dict[str, Any],
    members: list[dict[str, Any]],
    requested_mode: PlanningMode,
    max_work_units: int = 8,
    scope_confirmation: str = "",
) -> tuple[str, str]:
    payload = {
        "goal": goal,
        "team_spec": team_spec,
        "members": members,
        "policy": {
            "requested_mode": requested_mode,
            "max_work_units": max(1, int(max_work_units)),
            "scope_confirmation": str(scope_confirmation or "").strip() or None,
        },
    }
    system = (
        "你是 Crew Workflow PlanningDecision。只输出 JSON；不输出 Markdown/解释。"
        "先识别用户本轮明确要求的交付范围，再拆 work_units、依赖形态、质量策略和缺失事实；"
        "不生成 DAG nodes/edges，"
        "不选择/改派 Agent，不修改 Team/权限/预算。"
        "requested_scope 只表示本轮用户是否要求实际执行：plan_only=只写方案/设计/文档，"
        "execute=明确要求开发、修改、运行或验证，uncertain=无法可靠判断。"
        "用户本轮明确范围优先于 TeamSpec 的默认 intent 和 workflow_lanes；不要因为默认团队包含 build/verify"
        "就把只写方案的请求自动扩展为实现和验证。"
        + (
            "用户已经确认本轮范围为 plan_only，只生成方案类工作，不生成 build/verify 工作。"
            if scope_confirmation == "plan_only"
            else "用户已经确认本轮范围为 execute，可以按用户目标规划实际开发和验证。"
            if scope_confirmation == "execute"
            else ""
        )
        + "members 中的 formation_responsibility 只表示常驻分工边界；先按任务需要拆工作，"
        + "不得为了让每个成员都有任务而生成重复或近义 work_unit。"
        + "Leader 的目标确认、TeamPlan 创建/调整、派活和最终汇总由执行器自动生成，"
        + "不得把这些控制动作重复写入 work_units，也不得使用 leader_ 前缀作为 work_unit id。"
        )
    user = (
        "JSON schema="
        '{"goal_clarity":"low|medium|high","requested_scope":"plan_only|execute|uncertain",'
        '"critical_missing_info":[],"dependency_pattern":"sequential|parallel_merge|staged",'
        '"quality_policy":"none|leader_review|independent_review|evaluator_optimizer","dynamic_discovery":false,'
        '"conditional_branching":false,"iteration_until_convergence":false,"risk_level":"low|medium|high",'
        '"semantic_uncertainty":"low|medium|high","work_units":[{"id":"snake_case","objective":"可执行目标",'
        '"display_title":"中文短标题，不超过12字",'
        '"kind":"plan|research|analysis|design|build|verify|docs|other","required_capabilities":["上下文能力key"],'
        '"depends_on":["已有work_unit id"],"expected_output":"可检查输出","needs_independent_review":false}]}'
        f"\n规则：work_units<={max(1, int(max_work_units))}；能合并就合并；缺少用户事实才写 critical_missing_info；"
        "只保留需要团队成员实际完成的领域工作，省略 Leader 建计划、派活、收集回传和最终总结；"
        "每个 work_unit 必须给 display_title，面向看板展示，使用中文短标题，避免重复完整用户 prompt；"
        "需要运行发现/条件分支/循环评估时分别置 dynamic_discovery/conditional_branching/iteration_until_convergence=true。"
        f"\nctx={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    return system, user


def _level(value: object, default: Level = "medium") -> Level:
    item = str(value or "").strip().lower()
    return item if item in {"low", "medium", "high"} else default  # type: ignore[return-value]


def _dependency_pattern(value: object) -> DependencyPattern:
    item = str(value or "").strip().lower()
    return item if item in {"sequential", "parallel_merge", "staged"} else "sequential"  # type: ignore[return-value]


def _quality_policy(value: object) -> QualityPolicy:
    item = str(value or "").strip().lower()
    return item if item in {"none", "leader_review", "independent_review", "evaluator_optimizer"} else "leader_review"  # type: ignore[return-value]


def _requested_scope(value: object) -> RequestedScope:
    item = str(value or "").strip().lower()
    return item if item in {"plan_only", "execute", "uncertain"} else "uncertain"  # type: ignore[return-value]


def _has_cycle(units: list[WorkUnit]) -> bool:
    dependencies = {unit.id: set(unit.depends_on) for unit in units}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for parent_id in dependencies.get(node_id, set()):
            if visit(parent_id):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in dependencies)


def coerce_planning_decision(data: dict[str, Any], *, max_work_units: int = 12) -> PlanningDecision:
    raw_units = data.get("work_units") if isinstance(data.get("work_units"), list) else []
    units: list[WorkUnit] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_units[:max(1, int(max_work_units))]):
        if not isinstance(raw, dict):
            continue
        unit_id = str(raw.get("id") or f"work_{index + 1}").strip().lower().replace("-", "_")
        if not _WORK_UNIT_ID_RE.fullmatch(unit_id) or unit_id in seen:
            continue
        objective = str(raw.get("objective") or "").strip()
        expected_output = str(raw.get("expected_output") or "").strip()
        if not objective or not expected_output:
            continue
        raw_capabilities = (
            raw.get("required_capabilities")
            if isinstance(raw.get("required_capabilities"), list)
            else []
        )
        invalid_capabilities = [
            str(item)
            for item in raw_capabilities
            if str(item).strip() and not normalize_capability(item)
        ]
        required_capabilities = normalize_capabilities(raw_capabilities)
        if invalid_capabilities:
            raise ValueError(
                f"PlanningDecision work unit {unit_id} contains unsupported capabilities: "
                + ", ".join(invalid_capabilities)
            )
        if not required_capabilities:
            raise ValueError(f"PlanningDecision work unit {unit_id} missing required_capabilities")
        seen.add(unit_id)
        units.append(WorkUnit(
            id=unit_id,
            objective=objective,
            display_title=str(raw.get("display_title") or "").strip()[:32],
            kind=str(raw.get("kind") or "other").strip().lower() or "other",
            required_capabilities=required_capabilities,
            depends_on=[str(item or "").strip() for item in (raw.get("depends_on") or []) if str(item or "").strip()],
            expected_output=expected_output,
            needs_independent_review=bool(raw.get("needs_independent_review")),
        ))
    valid_ids = {unit.id for unit in units}
    units = [
        WorkUnit(
            id=unit.id,
            objective=unit.objective,
            display_title=unit.display_title,
            kind=unit.kind,
            required_capabilities=unit.required_capabilities,
            depends_on=list(dict.fromkeys(parent for parent in unit.depends_on if parent in valid_ids and parent != unit.id)),
            expected_output=unit.expected_output,
            needs_independent_review=unit.needs_independent_review,
        )
        for unit in units
    ]
    if not units:
        raise ValueError("PlanningDecision did not contain valid work units")
    if _has_cycle(units):
        raise ValueError("PlanningDecision work units contain a dependency cycle")
    missing = [
        str(item).strip()
        for item in (data.get("critical_missing_info") or [])
        if str(item).strip()
    ][:6]
    return PlanningDecision(
        goal_clarity=_level(data.get("goal_clarity")),
        requested_scope=_requested_scope(data.get("requested_scope")),
        critical_missing_info=missing,
        dependency_pattern=_dependency_pattern(data.get("dependency_pattern")),
        quality_policy=_quality_policy(data.get("quality_policy")),
        dynamic_discovery=bool(data.get("dynamic_discovery")),
        conditional_branching=bool(data.get("conditional_branching")),
        iteration_until_convergence=bool(data.get("iteration_until_convergence")),
        risk_level=_level(data.get("risk_level"), "low"),
        semantic_uncertainty=_level(data.get("semantic_uncertainty")),
        work_units=units,
    )


def select_planning_mode(
    decision: PlanningDecision,
    *,
    requested_mode: PlanningMode,
    standard_max_work_units: int = 8,
) -> PlanningMode:
    if requested_mode != "auto":
        return requested_mode
    if decision.dynamic_discovery or decision.conditional_branching or decision.iteration_until_convergence:
        return "ai"
    if (
        len(decision.work_units) == 1
        and decision.risk_level == "low"
        and decision.quality_policy == "none"
        and not decision.work_units[0].needs_independent_review
    ):
        return "fast"
    if len(decision.work_units) <= max(1, int(standard_max_work_units)):
        return "standard"
    return "ai"


def confidence_dimensions(
    decision: PlanningDecision,
    *,
    capability_coverage: float,
) -> dict[str, float]:
    requirement = {"low": 0.35, "medium": 0.68, "high": 1.0}[decision.goal_clarity]
    if decision.critical_missing_info:
        requirement = min(requirement, 0.45)
    complete_units = sum(bool(unit.objective and unit.expected_output) for unit in decision.work_units)
    topology = complete_units / len(decision.work_units) if decision.work_units else 0.0
    if decision.semantic_uncertainty == "high":
        topology = min(topology, 0.5)
    elif decision.semantic_uncertainty == "medium":
        topology = min(topology, 0.75)
    capability = min(1.0, max(0.0, float(capability_coverage)))
    return {
        "requirement": round(requirement, 4),
        "topology": round(topology, 4),
        "capability": round(capability, 4),
        "overall": round(min(requirement, topology, capability), 4),
    }


def node_execution_contract(
    *,
    goal: str,
    node_id: str,
    title: str,
    lane: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable execution contract projected into a plan node."""

    metadata = metadata if isinstance(metadata, Mapping) else {}
    outputs = {
        "lead": ["任务拆解", "依赖关系", "验收标准"],
        "plan": ["方案", "约束", "验收标准"],
        "design": ["设计说明", "关键状态", "实现约束"],
        "build": ["实现产物", "变更说明", "自测结果"],
        "verify": ["测试记录", "缺陷/风险", "验收结论"],
        "docs": ["交付记录", "产物引用", "后续建议"],
        "release": ["发布检查", "环境说明", "风险清单"],
        "summary": ["最终结论", "产物清单", "风险与下一步"],
    }.get(lane, ["执行结果", "风险说明", "下一步"])
    expected_output = str(metadata.get("expected_output") or "").strip()
    raw_expected_outputs = metadata.get("expected_outputs")
    declared_outputs = (
        [expected_output]
        if expected_output
        else [
            str(item).strip()
            for item in (
                raw_expected_outputs
                if isinstance(raw_expected_outputs, (list, tuple))
                else []
            )
            if str(item).strip()
        ]
    )
    if declared_outputs:
        outputs = declared_outputs
    acceptance_criteria = [
        "输出必须具体可检查。",
        "如需改变团队成员或补员，必须先提示用户并等待确认。",
        f"结果必须服务于用户目标：{goal}",
    ]
    if declared_outputs:
        acceptance_criteria.insert(
            0,
            f"必须交付本节点声明的输出：{'、'.join(declared_outputs)}。",
        )
    return {
        "purpose": title,
        "inputs": ["用户目标", "上游节点结果"],
        "outputs": outputs,
        "acceptance_criteria": acceptance_criteria,
        "requires_leader_review": "review" in node_id or "方案" in title or "审阅" in title,
        "conflict_scope": lane,
    }


def workflow_plan_from_graph(
    *,
    goal: str,
    team_spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[list[str]],
    requested_mode: PlanningMode,
    selected_mode: PlanningMode,
    engine: str,
    dependency_pattern: str,
    quality_policy: str,
    confidence: dict[str, float],
    reasons: list[str],
    budget: dict[str, Any],
    warnings: list[str],
    fallback_from: str | None = None,
    planning_decision: dict[str, Any] | None = None,
    requested_scope: str | None = None,
) -> WorkflowPlan:
    deliverables = team_spec.get("deliverables") if isinstance(team_spec.get("deliverables"), list) else []
    task_deliverables = [
        str(item.get("description") or item.get("type") or "").strip() if isinstance(item, dict) else str(item).strip()
        for item in deliverables
    ]
    plan_nodes: list[dict[str, Any]] = []
    for node in nodes:
        metadata = dict(node.get("metadata") or {})
        contract = dict(metadata.get("execution_contract") or {})
        plan_nodes.append({
            "id": str(node.get("id") or ""),
            "title": str(node.get("title") or ""),
            "display_title": str(metadata.get("display_title") or ""),
            "kind": str(metadata.get("work_unit_kind") or metadata.get("workflow_lane") or "other"),
            "assignee_id": str(node.get("assignee") or "leader"),
            "required_capabilities": normalize_capabilities(metadata.get("required_capabilities") or []),
            "capability_source": str(metadata.get("capability_source") or ""),
            "inputs": list(contract.get("inputs") or ["task.goal"]),
            "expected_outputs": list(contract.get("outputs") or []),
            "acceptance_criteria": list(contract.get("acceptance_criteria") or []),
        })
    planning_payload = {
        "requested_mode": requested_mode,
        "selected_mode": selected_mode,
        "engine": engine,
        "dependency_pattern": dependency_pattern,
        "quality_policy": quality_policy,
        "confidence": confidence.get("overall", 0.0),
        "confidence_dimensions": dict(confidence),
        "reason_codes": list(dict.fromkeys(str(item) for item in reasons if str(item))),
        "fallback_from": fallback_from,
    }
    if requested_scope in {"plan_only", "execute", "uncertain"}:
        planning_payload["requested_scope"] = requested_scope
    if planning_decision:
        planning_payload["planning_decision"] = dict(planning_decision)
    return WorkflowPlan(
        task={
            "turn_id": "",
            "goal": goal,
            "deliverables": [item for item in task_deliverables if item],
            "acceptance_criteria": list(team_spec.get("success_criteria") or []),
        },
        planning=planning_payload,
        nodes=plan_nodes,
        edges=[{"from": str(parent), "to": str(child)} for parent, child in edges],
        budget_snapshot=dict(budget),
        warnings=list(dict.fromkeys(str(item) for item in warnings if str(item))),
    )
