"""Team DAG planner built on the Dynamic Kanban graph model."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from crew.core.types import ChatResponse, Message
from crew.core.text_parsing import extract_json_object
from crew.dynamickanban.models import PlanEdge, PlanNode, PlanResult
from crew.dynamickanban.plan_graph import PlanGraph
from crew.team import flow_builder
from crew.team.agent_profile import AgentProfile, evaluate_capability_coverage, is_agent_profile_available
from crew.team.capabilities import normalize_capabilities, normalize_capability
from crew.team.models import TeamMemberSpec
from crew.team.policy_checker import TeamPolicyReport, analyze_team_policy
from crew.team.result_presenter import workflow_lane_order
from crew.team.team_spec import (
    TeamSpec,
    TeamSpecInput,
    build_team_spec,
    team_spec_from_planning_decision,
)
from crew.team.workflow_plan import (
    PlanningDecision,
    PlanningMode,
    WorkUnit,
    coerce_planning_decision,
    confidence_dimensions,
    normalize_planning_mode,
    planning_decision_messages,
    select_planning_mode,
    workflow_plan_from_graph,
)

DEFAULT_PLANNING_DECISION_TIMEOUT = 30.0
PLANNING_DECISION_MAX_TOKENS = 4096
PLANNING_DECISION_CACHE_TTL_SECONDS = 600.0
PLANNING_DECISION_WARMUP_TIMEOUT = 6.0

_PLANNING_DECISION_CACHE: dict[str, tuple[float, PlanningDecision]] = {}
_PLANNING_WARMUP_TASKS: dict[str, asyncio.Task[None]] = {}
_PLANNING_WARMUP_STATE: dict[str, dict[str, Any]] = {}
PlanningProgressCallback = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class TeamGraphPlan:
    spec: TeamSpec
    nodes: list[dict[str, Any]]
    edges: list[list[str]]
    policy_report: TeamPolicyReport
    planner_notes: list[str] = field(default_factory=list)
    workflow_plan: dict[str, Any] = field(default_factory=dict)
    critical_missing_info: list[str] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        return {
            "team_spec": self.spec.to_dict(),
            "policy_report": self.policy_report.to_dict(),
            "planner_notes": list(self.planner_notes),
            "workflow_plan": dict(self.workflow_plan),
            "critical_missing_info": list(self.critical_missing_info),
        }


class PlanningDecisionFailure(Exception):
    """Classified failure from the lightweight PlanningDecision call."""

    def __init__(self, error_type: str, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class PlanningDecisionCall:
    decision: PlanningDecision
    elapsed_ms: int
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _valid_member_ids(team: Any) -> list[str]:
    members = list(getattr(team, "members", {}) or {})
    return list(dict.fromkeys(["leader", *members]))


def _merged_execution_profile(spec: TeamSpec, execution_profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = dict(spec.execution_profile or {})
    incoming = execution_profile if isinstance(execution_profile, dict) else {}
    allowed_keys = {
        "requested_mode",
        "selected_mode",
        "budget",
        "turn_kind",
        "turn_decision_source",
        "profile_source",
    }
    unknown = sorted(set(incoming) - allowed_keys)
    if unknown:
        raise ValueError(
            "TeamGraphPlanner.execution_profile 只允许运行控制字段，非法字段："
            + ", ".join(unknown)
        )
    for key, value in incoming.items():
        if value is None:
            continue
        if key not in allowed_keys:
            continue
        if key == "budget" and isinstance(value, dict):
            profile[key] = {**dict(profile.get(key) or {}), **value}
        else:
            profile[key] = value
    requested_mode = normalize_planning_mode(incoming.get("requested_mode") or "auto")
    profile["requested_mode"] = requested_mode
    profile.setdefault("budget", {})
    return profile


def _runtime_execution_profile(profile: dict[str, Any]) -> dict[str, Any]:
    runtime_keys = {
        "requested_mode",
        "selected_mode",
        "budget",
        "turn_kind",
        "turn_decision_source",
        "profile_source",
    }
    return {key: value for key, value in profile.items() if key in runtime_keys}


FastPrimary = TeamMemberSpec | str


def _candidate_lane(candidate: FastPrimary) -> str:
    if candidate == "leader":
        return "lead"
    return flow_builder.workflow_lane(candidate)


def _member_priority(candidate: FastPrimary, preferred_lanes: tuple[str, ...], index: int) -> tuple[int, int]:
    lane = _candidate_lane(candidate)
    try:
        lane_score = preferred_lanes.index(lane)
    except ValueError:
        lane_score = len(preferred_lanes)
    return lane_score, index


def _select_fast_primary(
    members: list[TeamMemberSpec],
    profile: dict[str, Any],
    task_profile: dict[str, Any] | None = None,
) -> FastPrimary | None:
    candidates: list[FastPrimary] = list(members)
    if not candidates:
        return None
    task_profile = task_profile if isinstance(task_profile, dict) else {}
    intent = str(task_profile.get("intent") or profile.get("task_intent") or "").strip().lower()
    shape = str(task_profile.get("deliverable_shape") or profile.get("deliverable_shape") or "").strip().lower()
    if intent in {"question", "inquiry", "chat"}:
        preferred = ("docs", "plan", "lead", "build", "verify", "design", "release", "other")
    elif shape in {"review", "verification", "qa"}:
        preferred = ("verify", "plan", "docs", "build", "design", "release", "lead", "other")
    elif shape in {"doc", "docs", "documentation", "research"}:
        preferred = ("docs", "plan", "build", "design", "verify", "release", "lead", "other")
    else:
        preferred = ("build", "design", "plan", "docs", "verify", "release", "lead", "other")
    if "lead" in preferred:
        candidates.append("leader")
    return min(candidates, key=lambda item: _member_priority(item, preferred, candidates.index(item)))


def _select_fast_verifier(
    members: list[TeamMemberSpec],
    *,
    primary_id: str,
    required_lanes: set[str],
) -> TeamMemberSpec | None:
    if "verify" not in required_lanes:
        return None
    verifiers = [
        member
        for member in members
        if member.member_id != primary_id and flow_builder.workflow_lane(member) == "verify"
    ]
    return verifiers[0] if verifiers else None


def _build_fast_workflow_nodes(
    team: Any,
    goal: str,
    profile: dict[str, Any],
    *,
    required_capabilities: list[str] | None = None,
    required_lanes: set[str] | None = None,
    capability_source: str = "team_spec",
    task_profile: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    members = list((getattr(team, "members", {}) or {}).values())
    primary = _select_fast_primary(members, profile, task_profile)
    if primary is None:
        return [], []
    task_title = flow_builder.goal_title(goal)
    primary_id = "leader" if primary == "leader" else primary.member_id
    primary_lane = _candidate_lane(primary)
    primary_metadata = (
        flow_builder.node_metadata("lead", label="理解上下文、确认信息缺口、汇总团队状态", key="team_lead")
        if primary == "leader"
        else flow_builder.member_node_metadata(primary, primary_lane)
    )
    task_capabilities = normalize_capabilities(required_capabilities or [])
    if task_capabilities:
        primary_metadata.update({
            "required_capabilities": task_capabilities,
            "capability_source": capability_source,
        })
    elif primary == "leader":
        primary_metadata["capability_source"] = "leader_direct"
    nodes: list[dict[str, Any]] = [
        {
            "id": "leader_plan",
            "title": f"Leader 快速定向：{task_title}",
            "detail": f"确认目标、执行边界和轻量验收口径，直接推进核心成员执行：{goal}",
            "assignee": "leader",
            "metadata": flow_builder.node_metadata("lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead"),
        },
    ]
    edges: list[Any] = []
    execute_parents = ["leader_plan"]
    nodes.append(
        {
            "id": "fast_execute",
            "title": f"快速执行：{task_title}",
            "detail": f"聚焦用户目标完成主要产物或核心结论，并同步说明风险和自检结果：{goal}",
            "assignee": primary_id,
            "metadata": primary_metadata,
        }
    )
    for parent in execute_parents:
        edges.append([parent, "fast_execute"])
    summary_parent = "fast_execute"
    verifier = _select_fast_verifier(
        members,
        primary_id=primary_id,
        required_lanes=set(required_lanes or []),
    )
    if verifier is not None:
        verifier_metadata = flow_builder.member_node_metadata(verifier, "verify")
        verifier_metadata.update({
            "required_capabilities": ["review", "testing", "verification"],
            "capability_source": "system_contract",
        })
        nodes.append({
            "id": "fast_verify",
            "title": f"轻量验证：{task_title}",
            "detail": f"基于快速执行产物做独立轻量验证，输出通过结论、风险和需要用户确认的问题：{goal}",
            "assignee": verifier.member_id,
            "metadata": verifier_metadata,
        })
        edges.append(["fast_execute", "fast_verify"])
        summary_parent = "fast_verify"
    nodes.append({
        "id": "leader_summary",
        "title": f"Leader 快速汇总：{task_title}",
        "detail": f"整合执行结果，给出面向用户的最终结论、产物引用、风险和下一步建议：{goal}",
        "assignee": "leader",
        "metadata": flow_builder.node_metadata("summary", label="汇总结论、验收反馈", key="team_lead"),
    })
    edges.append([summary_parent, "leader_summary"])
    return nodes, edges


def _member_by_id(team: Any) -> dict[str, TeamMemberSpec]:
    return dict((getattr(team, "members", {}) or {}))


def _member_capability_sets(team: Any) -> dict[str, list[str]]:
    """Return the effective capability set for DAG admission.

    A TeamMemberSpec is the user's confirmed assignment contract. A resolved
    AgentProfile adds model/runtime evidence, but must not erase capabilities
    already assigned to that member: a newly materialized profile commonly has
    only weak priors before the first execution observation.
    """

    result: dict[str, list[str]] = {}
    profiles = getattr(team, "member_profiles", {})
    leader = getattr(team, "leader_spec", None)
    members = [leader] if isinstance(leader, TeamMemberSpec) else []
    members.extend((getattr(team, "members", {}) or {}).values())
    for member in members:
        if not member.member_id:
            continue
        assigned = normalize_capabilities(member.capabilities or [])
        if not assigned:
            assigned = normalize_capabilities(
                flow_builder.member_node_metadata(member).get("required_capabilities") or []
            )
        profile = profiles.get(member.member_id) if isinstance(profiles, dict) else None
        if isinstance(profile, AgentProfile):
            profiled = [
                capability
                for capability, assessment in profile.capabilities.items()
                if is_agent_profile_available(profile) and assessment.score >= 0.5
            ]
            result[member.member_id] = normalize_capabilities([*assigned, *profiled])
            continue
        result[member.member_id] = assigned
    return result


def _member_metadata_sets(team: Any) -> dict[str, dict[str, Any]]:
    """Return member identity metadata used when admission changes assignees."""

    return {
        member.member_id: flow_builder.member_node_metadata(member)
        for member in (getattr(team, "members", {}) or {}).values()
        if member.member_id
    }


def _member_lane_for_assignee(member_map: dict[str, TeamMemberSpec], assignee: str, fallback: str = "other") -> str:
    if assignee == "leader":
        return "lead"
    member = member_map.get(assignee)
    return flow_builder.workflow_lane(member) if member is not None else fallback


def _member_metadata_for_assignee(
    member_map: dict[str, TeamMemberSpec],
    assignee: str,
    lane: str,
) -> dict[str, Any]:
    if assignee == "leader":
        return flow_builder.node_metadata(lane or "lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead")
    member = member_map.get(assignee)
    if member is None:
        return flow_builder.node_metadata(lane or "other")
    return flow_builder.member_node_metadata(member, lane or None)


def _node_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("node_id") or "").strip()


def _node_lane(raw: dict[str, Any]) -> str:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return str(metadata.get("workflow_lane") or "other").strip().lower() or "other"


def _edge_pair(raw: Any) -> tuple[str, str] | None:
    if isinstance(raw, dict):
        parent = str(raw.get("parent_id") or raw.get("from") or "").strip()
        child = str(raw.get("child_id") or raw.get("to") or "").strip()
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        parent = str(raw[0]).strip()
        child = str(raw[1]).strip()
    else:
        return None
    if not parent or not child:
        return None
    return parent, child


def _unique_node_id(base_id: str, used: set[str]) -> str:
    candidate_base = str(base_id or "node").strip() or "node"
    candidate = candidate_base
    index = 2
    while candidate in used:
        candidate = f"{candidate_base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _normalize_raw_graph_ids(
    raw_nodes: list[dict[str, Any]],
    raw_edges: list[Any],
) -> tuple[list[dict[str, Any]], list[list[str]], list[str]]:
    original_ids = [
        str(raw.get("id") or raw.get("node_id") or f"node_{index + 1}").strip() or f"node_{index + 1}"
        for index, raw in enumerate(raw_nodes)
        if isinstance(raw, dict)
    ]
    reserved = set(original_ids)
    used: set[str] = set()
    occurrences: dict[str, list[tuple[int, str]]] = {}
    normalized_nodes: list[dict[str, Any]] = []
    notes: list[str] = []

    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        original_id = str(raw.get("id") or raw.get("node_id") or f"node_{index + 1}").strip() or f"node_{index + 1}"
        if original_id in used:
            reserved.discard(original_id)
            new_id = _unique_node_id(original_id, used | reserved)
            used.add(new_id)
            notes.append(f"重复节点 ID {original_id} 已重命名为 {new_id}。")
        else:
            new_id = _unique_node_id(original_id, used)
        copied = dict(raw)
        copied["id"] = new_id
        copied["node_id"] = new_id
        metadata = dict(copied.get("metadata") or {})
        if copied.get("workflow_lane") and not metadata.get("workflow_lane"):
            metadata["workflow_lane"] = str(copied.get("workflow_lane") or "")
        if copied.get("display_title") and not metadata.get("display_title"):
            metadata["display_title"] = str(copied.get("display_title") or "")
        if new_id != original_id:
            metadata["normalized_from_node_id"] = original_id
        copied["metadata"] = metadata
        normalized_nodes.append(copied)
        occurrences.setdefault(original_id, []).append((len(normalized_nodes) - 1, new_id))

    def _resolve_child(child_id: str, parent_index: int | None) -> tuple[str, int] | None:
        candidates = occurrences.get(child_id) or []
        if not candidates:
            return None
        if parent_index is not None:
            for candidate_index, candidate_id in candidates:
                if candidate_index > parent_index:
                    return candidate_id, candidate_index
        candidate_index, candidate_id = candidates[0]
        return candidate_id, candidate_index

    def _resolve_parent(parent_id: str, child_index: int | None) -> tuple[str, int] | None:
        candidates = occurrences.get(parent_id) or []
        if not candidates:
            return None
        if child_index is not None:
            before = [item for item in candidates if item[0] < child_index]
            if before:
                candidate_index, candidate_id = before[-1]
                return candidate_id, candidate_index
        candidate_index, candidate_id = candidates[-1]
        return candidate_id, candidate_index

    normalized_edges: list[list[str]] = []
    for raw in raw_edges:
        pair = _edge_pair(raw)
        if pair is None:
            continue
        parent_raw, child_raw = pair
        parent_candidates = occurrences.get(parent_raw) or []
        child_candidates = occurrences.get(child_raw) or []
        parent_index_hint = parent_candidates[0][0] if len(parent_candidates) == 1 else None
        child_index_hint = child_candidates[0][0] if len(child_candidates) == 1 else None
        child = _resolve_child(child_raw, parent_index_hint)
        parent = _resolve_parent(parent_raw, child[1] if child else child_index_hint)
        if parent is not None and len(child_candidates) > 1:
            child = _resolve_child(child_raw, parent[1])
        if parent is None or child is None:
            continue
        parent_id, child_id = parent[0], child[0]
        if parent_id == child_id:
            continue
        edge = [parent_id, child_id]
        if edge not in normalized_edges:
            normalized_edges.append(edge)
    return normalized_nodes, normalized_edges, notes


def _dedupe_graph_edges(graph: PlanGraph) -> None:
    seen: set[tuple[str, str]] = set()
    deduped: list[PlanEdge] = []
    for edge in graph.edges:
        pair = (edge.parent_id, edge.child_id)
        if pair in seen:
            continue
        seen.add(pair)
        deduped.append(edge)
    graph.edges = deduped


def _ensure_leader_summary_terminal(graph: PlanGraph) -> list[str]:
    if "leader_summary" not in graph.nodes:
        return []
    notes: list[str] = []
    summary_outgoing = [edge for edge in graph.edges if edge.parent_id == "leader_summary"]
    if summary_outgoing:
        graph.edges = [edge for edge in graph.edges if edge.parent_id != "leader_summary"]
        notes.append("移除 leader_summary 的下游依赖，保持最终汇总为终端节点。")
    children = graph.children_map()
    existing = {(edge.parent_id, edge.child_id) for edge in graph.edges}
    added: list[str] = []
    for node_id in list(graph.nodes):
        if node_id == "leader_summary":
            continue
        if children.get(node_id):
            continue
        pair = (node_id, "leader_summary")
        if pair in existing:
            continue
        graph.edges.append(PlanEdge(parent_id=node_id, child_id="leader_summary"))
        existing.add(pair)
        added.append(node_id)
    if added:
        notes.append(f"补齐终端节点到 leader_summary 的依赖：{', '.join(added)}。")
    _dedupe_graph_edges(graph)
    graph.validate_and_fix()
    _dedupe_graph_edges(graph)
    return notes


def _budget_max_nodes(profile: dict[str, Any]) -> int | None:
    budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
    try:
        max_nodes = int(budget.get("max_nodes") or 0)
    except (TypeError, ValueError):
        return None
    return max_nodes if max_nodes > 0 else None


def _standard_node_required(raw: dict[str, Any], required_lanes: set[str]) -> bool:
    node_id = _node_id(raw)
    if node_id in {"leader_plan", "leader_summary"}:
        return True
    lane = _node_lane(raw)
    if lane in {"lead", "summary"}:
        return True
    if lane in required_lanes or (lane == "release" and "docs" in required_lanes):
        return True
    return False


def _trim_edges_for_removed_nodes(
    raw_edges: list[Any],
    *,
    keep_ids: set[str],
    removed_ids: list[str],
    ordered_ids: list[str],
) -> list[list[str]]:
    edges = [
        [parent, child]
        for item in raw_edges
        for pair in [_edge_pair(item)]
        if pair is not None
        for parent, child in [pair]
        if parent in keep_ids and child in keep_ids
    ]
    original_pairs = [_edge_pair(item) for item in raw_edges]
    original_edges = [
        [parent, child]
        for pair in original_pairs
        if pair is not None
        for parent, child in [pair]
    ]
    for removed_id in removed_ids:
        parents = [parent for parent, child in original_edges if child == removed_id and parent in keep_ids]
        children = [child for parent, child in original_edges if parent == removed_id and child in keep_ids]
        for parent in parents:
            for child in children:
                if parent != child and [parent, child] not in edges:
                    edges.append([parent, child])

    parented = {child for _, child in edges}
    for node_id in ordered_ids:
        if node_id == "leader_plan" or node_id not in keep_ids or node_id in parented:
            continue
        if "leader_plan" in keep_ids:
            edges.append(["leader_plan", node_id])
    return edges


def _apply_standard_budget(
    raw_nodes: list[dict[str, Any]],
    raw_edges: list[Any],
    profile: dict[str, Any],
    required_lanes: set[str],
) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
    max_nodes = _budget_max_nodes(profile)
    if max_nodes is None or len(raw_nodes) <= max_nodes:
        return raw_nodes, raw_edges, []

    ordered_ids = [_node_id(node) for node in raw_nodes if _node_id(node)]
    keep_ids = set(ordered_ids)
    removable_priority = {
        "release": 0,
        "docs": 1,
        "plan": 2,
        "design": 3,
        "verify": 4,
        "other": 5,
        "build": 6,
    }
    candidates = [
        (removable_priority.get(_node_lane(node), 5), -index, _node_id(node))
        for index, node in enumerate(raw_nodes)
        if _node_id(node) and not _standard_node_required(node, required_lanes)
    ]
    removed_ids: list[str] = []
    for _, _, node_id in sorted(candidates):
        if len(keep_ids) <= max_nodes:
            break
        keep_ids.discard(node_id)
        removed_ids.append(node_id)

    trimmed_nodes = [node for node in raw_nodes if _node_id(node) in keep_ids]
    trimmed_edges = _trim_edges_for_removed_nodes(
        raw_edges,
        keep_ids=keep_ids,
        removed_ids=removed_ids,
        ordered_ids=ordered_ids,
    )
    notes: list[str] = []
    if removed_ids:
        notes.append(f"Standard Team 根据 execution_profile.budget.max_nodes 裁剪可选节点：{', '.join(removed_ids)}。")
    if len(trimmed_nodes) > max_nodes:
        notes.append("Standard Team 预算低于关键节点数量，保留必要节点并跳过进一步裁剪。")
    return trimmed_nodes, trimmed_edges, notes


def _build_standard_workflow_nodes(
    team: Any,
    goal: str,
    profile: dict[str, Any],
    team_spec: TeamSpec,
) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
    raw_nodes, raw_edges = flow_builder.build_default_workflow_nodes(team, goal, team_spec=team_spec)
    required_lanes = set(team_spec.team_requirements.get("workflow_lanes") or [])
    trimmed_nodes, trimmed_edges, notes = _apply_standard_budget(
        raw_nodes, raw_edges, profile, required_lanes
    )
    return trimmed_nodes, trimmed_edges, notes


def _semantic_lane(unit: WorkUnit) -> str:
    kind = str(unit.kind or "").strip().lower()
    if kind in {"plan", "research", "analysis"}:
        return "plan"
    if kind in {"design", "build", "verify", "docs", "release"}:
        return kind
    capabilities = set(unit.required_capabilities)
    if capabilities & {"testing", "verification", "review"}:
        return "verify"
    if capabilities & {"documentation", "synthesis"}:
        return "docs"
    if capabilities & {"frontend", "backend", "implementation"}:
        return "build"
    if capabilities & {"research", "analysis", "information_retrieval", "requirements", "planning"}:
        return "plan"
    return "other"


def _member_capabilities(
    member: TeamMemberSpec,
    capability_sets: dict[str, list[str]] | None = None,
) -> set[str]:
    if capability_sets is not None and member.member_id in capability_sets:
        return set(capability_sets[member.member_id])
    return set(normalize_capabilities(member.capabilities or []))


def _work_unit_member_score(
    unit: WorkUnit,
    member: TeamMemberSpec,
    index: int,
    capability_sets: dict[str, list[str]] | None = None,
) -> tuple[int, int, int]:
    required = set(unit.required_capabilities)
    target_lane = _semantic_lane(unit)
    capabilities = _member_capabilities(member, capability_sets)
    overlap = len(required & capabilities)
    covers_all = int(bool(required) and required <= capabilities)
    lane_match = int(flow_builder.workflow_lane(member) == target_lane)
    return covers_all, overlap * 2 + lane_match, -index


def _rank_work_unit_members(
    unit: WorkUnit,
    members: list[TeamMemberSpec],
    capability_sets: dict[str, list[str]] | None = None,
) -> list[tuple[TeamMemberSpec, tuple[int, int, int], int]]:
    ranked = [
        (member, _work_unit_member_score(unit, member, index, capability_sets), index)
        for index, member in enumerate(members)
    ]
    return sorted(ranked, key=lambda item: item[1], reverse=True)


def _assign_work_unit(
    unit: WorkUnit,
    members: list[TeamMemberSpec],
    capability_sets: dict[str, list[str]] | None = None,
) -> TeamMemberSpec | None:
    ranked = _rank_work_unit_members(unit, members, capability_sets)
    return ranked[0][0] if ranked else None


def _work_unit_depths(units: list[WorkUnit]) -> dict[str, int]:
    by_id = {unit.id: unit for unit in units}
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(unit_id: str) -> int:
        if unit_id in memo:
            return memo[unit_id]
        if unit_id in visiting:
            return 0
        visiting.add(unit_id)
        unit = by_id.get(unit_id)
        parents = [parent for parent in (unit.depends_on if unit is not None else []) if parent in by_id]
        value = max((depth(parent) + 1 for parent in parents), default=0)
        visiting.remove(unit_id)
        memo[unit_id] = value
        return value

    return {unit.id: depth(unit.id) for unit in units}


def _assign_work_units_balanced(
    units: list[WorkUnit],
    members: list[TeamMemberSpec],
    capability_sets: dict[str, list[str]] | None = None,
) -> tuple[dict[str, TeamMemberSpec | None], dict[str, dict[str, Any]]]:
    if not members:
        return {unit.id: None for unit in units}, {}
    depths = _work_unit_depths(units)
    groups: dict[tuple[int, str], list[WorkUnit]] = {}
    for unit in units:
        groups.setdefault((depths.get(unit.id, 0), _semantic_lane(unit)), []).append(unit)

    assignments: dict[str, TeamMemberSpec | None] = {}
    assignment_meta: dict[str, dict[str, Any]] = {}
    for (depth, lane), group_units in groups.items():
        usage: dict[str, int] = {}
        group_id = f"depth_{depth}:{lane}"
        for unit in group_units:
            ranked = _rank_work_unit_members(unit, members, capability_sets)
            if not ranked:
                assignments[unit.id] = None
                continue
            top_quality = ranked[0][1][:2]
            eligible = [
                item for item in ranked
                if item[1][:2] == top_quality
            ]
            chosen, chosen_score, _ = min(
                eligible,
                key=lambda item: (usage.get(item[0].member_id, 0), -item[1][0], -item[1][1], item[2]),
            )
            usage[chosen.member_id] = usage.get(chosen.member_id, 0) + 1
            assignments[unit.id] = chosen
            if len(group_units) > 1 and len({item[0].member_id for item in eligible}) > 1:
                assignment_meta[unit.id] = {
                    "parallel_assignment": True,
                    "assignment_group": group_id,
                    "assignment_reason": "同层同泳道并行工作单元在同等能力候选间均衡分配。",
                    "assignment_score": {
                        "covers_all": chosen_score[0],
                        "capability_lane_score": chosen_score[1],
                    },
                }
    return assignments, assignment_meta


def _semantic_standard_workflow_nodes(
    team: Any,
    goal: str,
    decision: PlanningDecision,
    profile: dict[str, Any],
    required_lanes: set[str],
) -> tuple[list[dict[str, Any]], list[Any], list[str], float]:
    members: list[TeamMemberSpec] = list((getattr(team, "members", {}) or {}).values())
    task_title = flow_builder.goal_title(goal)
    nodes: list[dict[str, Any]] = [{
        "id": "leader_plan",
        "title": f"Leader 确认目标：{task_title}",
        "detail": f"确认本轮目标、工作单元、依赖、成员分配和验收标准：{goal}",
        "assignee": "leader",
        "metadata": flow_builder.node_metadata("lead", label="确认目标、派活跟踪、处理阻塞", key="team_lead"),
    }]
    edges: list[list[str]] = []
    reserved_leader_units = [
        unit for unit in decision.work_units
        if unit.id == "leader" or unit.id.startswith("leader_")
    ]
    units = [
        unit for unit in decision.work_units
        if unit.id != "leader" and not unit.id.startswith("leader_")
    ]
    if decision.dependency_pattern in {"sequential", "staged"} and all(not unit.depends_on for unit in units):
        units = [
            WorkUnit(
                id=unit.id,
                objective=unit.objective,
                display_title=unit.display_title,
                kind=unit.kind,
                required_capabilities=unit.required_capabilities,
                depends_on=[units[index - 1].id] if index else [],
                expected_output=unit.expected_output,
                needs_independent_review=unit.needs_independent_review,
            )
            for index, unit in enumerate(units)
        ]

    capability_sets = _member_capability_sets(team)
    assignments, assignment_meta = _assign_work_units_balanced(units, members, capability_sets)
    unit_ids = {unit.id for unit in units}
    assigned_coverage: list[float] = []
    for unit in units:
        member = assignments.get(unit.id)
        assignee = member.member_id if member is not None else "leader"
        lane = _semantic_lane(unit)
        metadata = _member_metadata_for_assignee(_member_by_id(team), assignee, lane)
        metadata.update({
            "work_unit_kind": unit.kind,
            "required_capabilities": list(unit.required_capabilities),
            "capability_source": "work_unit",
            "expected_output": unit.expected_output,
            "needs_independent_review": unit.needs_independent_review,
            **({"display_title": unit.display_title} if unit.display_title else {}),
            "full_title": unit.objective,
        })
        metadata.update(assignment_meta.get(unit.id) or {})
        nodes.append({
            "id": unit.id,
            "title": unit.objective,
            "detail": f"完成工作单元并交付可检查结果：{unit.expected_output}",
            "assignee": assignee,
            "metadata": metadata,
        })
        member_caps = _member_capabilities(member, capability_sets) if member is not None else set()
        required = set(unit.required_capabilities)
        assigned_coverage.append(len(required & member_caps) / len(required) if required else 1.0)
        valid_parents = [parent_id for parent_id in unit.depends_on if parent_id in unit_ids]
        if valid_parents:
            edges.extend([[parent_id, unit.id] for parent_id in valid_parents])
        else:
            edges.append(["leader_plan", unit.id])

    depended_on = {
        parent
        for unit in units
        for parent in unit.depends_on
        if parent in unit_ids
    }
    sinks = [unit.id for unit in units if unit.id not in depended_on]
    needs_review = (
        decision.quality_policy in {"independent_review", "evaluator_optimizer"}
        or any(unit.needs_independent_review for unit in units)
    )
    summary_parents = list(sinks) or ["leader_plan"]
    if needs_review:
        review_unit = WorkUnit(
            id="independent_review",
            objective="独立核验本轮结果",
            kind="verify",
            required_capabilities=["review", "verification"],
            expected_output="审阅结论、缺陷和是否可交付的判断",
        )
        reviewer = _assign_work_unit(review_unit, members, capability_sets)
        reviewer_id = reviewer.member_id if reviewer is not None else "leader"
        review_meta = _member_metadata_for_assignee(_member_by_id(team), reviewer_id, "verify")
        review_meta.update({
            "work_unit_kind": "verify",
            "required_capabilities": ["review", "verification"],
            "capability_source": "system_contract",
            "quality_policy": decision.quality_policy,
        })
        nodes.append({
            "id": "independent_review",
            "title": "独立核验本轮结果",
            "detail": "按本轮验收条件核验各工作单元结果；不通过时给出明确修订意见。",
            "assignee": reviewer_id,
            "metadata": review_meta,
        })
        edges.extend([[parent_id, "independent_review"] for parent_id in sinks])
        summary_parents = ["independent_review"]

    nodes.append({
        "id": "leader_summary",
        "title": f"Leader 汇总交付：{task_title}",
        "detail": f"汇总所有工作单元和审阅结论，形成面向用户的最终交付：{goal}",
        "assignee": "leader",
        "metadata": flow_builder.node_metadata("summary", label="汇总结论、验收反馈", key="team_lead"),
    })
    edges.extend([[parent_id, "leader_summary"] for parent_id in summary_parents])
    nodes, edges, budget_notes = _apply_standard_budget(
        nodes,
        edges,
        profile,
        required_lanes,
    )
    coverage = min(assigned_coverage) if assigned_coverage else 0.0
    notes = [
        "Standard Team 使用 PlanningDecision 语义工作单元编译通用 DAG。",
        f"dependency_pattern={decision.dependency_pattern}; quality_policy={decision.quality_policy}。",
        *(
            [
                "已忽略 PlanningDecision 重复生成的 Leader 控制工作单元："
                + "、".join(unit.id for unit in reserved_leader_units)
                + "；Leader 计划与汇总由执行器统一生成。"
            ]
            if reserved_leader_units
            else []
        ),
        *budget_notes,
    ]
    return nodes, edges, notes, coverage


def _compact_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _planning_team_spec_summary(spec: TeamSpec, profile: dict[str, Any]) -> dict[str, Any]:
    requirements = spec.team_requirements if isinstance(spec.team_requirements, dict) else {}
    planning = spec.planning if isinstance(spec.planning, dict) else {}
    return {
        "intent": str(spec.task_profile.get("intent") or "mixed"),
        "complexity": str(spec.task_profile.get("complexity") or "focused"),
        "required_capabilities": normalize_capabilities(requirements.get("capabilities") or []),
        "workflow_lanes": [
            str(item)
            for item in (requirements.get("workflow_lanes") or [])
            if str(item).strip()
        ][:8],
        "deliverables": [
            _compact_text(item.get("description") or item.get("type") or item, 120)
            if isinstance(item, dict) else _compact_text(item, 120)
            for item in (spec.deliverables or [])
        ][:6],
        "success_criteria": [_compact_text(item, 140) for item in (spec.success_criteria or [])][:6],
        "risk_level": str(spec.risk_level or "low"),
        "uncertainty": str(spec.uncertainty or "low"),
        "missing_info": [_compact_text(item, 140) for item in (planning.get("missing_info") or [])][:6],
    }


def _team_members_for_planning_prompt(members: list[TeamMemberSpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for member in members:
        metadata = member.metadata if isinstance(member.metadata, dict) else {}
        row = {
            "member_id": member.member_id,
            "role": _compact_text(metadata.get("role_label") or member.role or member.name, 120),
            "workflow_lane": flow_builder.workflow_lane(member),
            "capabilities": normalize_capabilities(member.capabilities or []),
        }
        responsibility = metadata.get("formation_responsibility")
        if isinstance(responsibility, dict) and responsibility:
            row["formation_responsibility"] = {
                "mission": _compact_text(responsibility.get("mission"), 180),
                "deliverables": [
                    _compact_text(item, 100)
                    for item in (responsibility.get("deliverables") or [])
                    if str(item).strip()
                ][:4],
            }
        rows.append(row)
    return rows


def _provider_identity(provider: Any) -> str:
    model = str(getattr(provider, "model", "") or "")
    return f"{provider.__class__.__module__}.{provider.__class__.__qualname__}:{model}"


def _planning_cache_key(
    *,
    provider: Any,
    goal: str,
    team_spec: dict[str, Any],
    members: list[dict[str, Any]],
    requested_mode: PlanningMode,
    max_work_units: int,
) -> str:
    payload = {
        "provider": _provider_identity(provider),
        "goal": goal,
        "team_spec": team_spec,
        "members": members,
        "requested_mode": requested_mode,
        "max_work_units": max_work_units,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(cache_key: str, ttl_s: float) -> PlanningDecision | None:
    if ttl_s <= 0:
        return None
    item = _PLANNING_DECISION_CACHE.get(cache_key)
    if item is None:
        return None
    created_at, decision = item
    if time.time() - created_at > ttl_s:
        _PLANNING_DECISION_CACHE.pop(cache_key, None)
        return None
    return decision


def _cache_put(cache_key: str, decision: PlanningDecision, ttl_s: float) -> None:
    if ttl_s <= 0:
        return
    _PLANNING_DECISION_CACHE[cache_key] = (time.time(), decision)


async def _notify_planning_progress(
    progress: PlanningProgressCallback | None,
    *,
    phase: str,
    status: str = "running",
    label: str = "",
    elapsed_ms: int | None = None,
    detail: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> None:
    if progress is None:
        return
    payload: dict[str, Any] = {
        "phase": phase,
        "status": status,
        "label": label,
        "detail": detail,
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    if diagnostics:
        payload["diagnostics"] = dict(diagnostics)
    try:
        result = progress(payload)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - progress is best-effort and must not break planning
        return


async def _chat_provider_text(provider: Any, messages: list[Message], *, max_tokens: int) -> ChatResponse:
    try:
        return await provider.chat(messages, tools=None, max_tokens=max_tokens)
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        return await provider.chat(messages, tools=None)


async def _stream_provider_text(
    provider: Any,
    messages: list[Message],
    *,
    max_tokens: int,
    timeout_s: float,
    progress: PlanningProgressCallback | None = None,
) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    chunks: list[str] = []
    diagnostics: dict[str, Any] = {
        "transport": "stream",
        "first_token_ms": None,
        "first_chunk_ms": None,
        "first_reasoning_ms": None,
        "first_content_ms": None,
        "partial_chars": 0,
        "reasoning_chars": 0,
    }

    try:
        stream = provider.stream_chat(messages, tools=None, max_tokens=max_tokens)
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        stream = provider.stream_chat(messages, tools=None)

    async def collect() -> None:
        nonlocal chunks
        async for chunk in stream:
            now_ms = int((time.perf_counter() - started) * 1000)
            reasoning = str(getattr(chunk, "reasoning_content", "") or "")
            delta = str(getattr(chunk, "delta_text", "") or "")
            finish_reason = str(getattr(chunk, "finish_reason", "") or "")
            if finish_reason:
                diagnostics["finish_reason"] = finish_reason
            if diagnostics["first_chunk_ms"] is None:
                diagnostics["first_chunk_ms"] = now_ms
                await _notify_planning_progress(
                    progress,
                    phase="connected",
                    label="规划模型已响应，正在识别工作单元",
                    elapsed_ms=now_ms,
                    diagnostics=diagnostics,
                )
            if reasoning:
                diagnostics["reasoning_chars"] = int(diagnostics.get("reasoning_chars") or 0) + len(reasoning)
                if diagnostics["first_reasoning_ms"] is None:
                    diagnostics["first_reasoning_ms"] = now_ms
                    await _notify_planning_progress(
                        progress,
                        phase="reasoning",
                        label="正在推演任务拆分",
                        elapsed_ms=now_ms,
                        diagnostics=diagnostics,
                    )
            if not delta:
                continue
            if diagnostics["first_token_ms"] is None:
                diagnostics["first_token_ms"] = now_ms
                diagnostics["first_content_ms"] = now_ms
                await _notify_planning_progress(
                    progress,
                    phase="content",
                    label="正在生成规划结果",
                    elapsed_ms=now_ms,
                    diagnostics=diagnostics,
                )
            chunks.append(delta)
            diagnostics["partial_chars"] = sum(len(item) for item in chunks)

    try:
        await asyncio.wait_for(collect(), timeout=max(0.2, timeout_s))
    except asyncio.TimeoutError as exc:
        diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        diagnostics["partial_chars"] = sum(len(item) for item in chunks)
        error_type = (
            "reasoning_only_timeout"
            if diagnostics.get("first_reasoning_ms") is not None and not chunks
            else "timeout"
        )
        label = (
            "规划模型已推演但未输出结构化结果，已切换到稳定执行图"
            if error_type == "reasoning_only_timeout"
            else "规划模型响应较慢，已切换到稳定执行图"
        )
        await _notify_planning_progress(
            progress,
            phase="fallback",
            status="fallback",
            label=label,
            elapsed_ms=int(diagnostics["elapsed_ms"]),
            diagnostics=diagnostics,
        )
        raise PlanningDecisionFailure(
            error_type,
            f"PlanningDecision stream timed out after {timeout_s:.1f}s",
            diagnostics,
        ) from exc
    diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    diagnostics["partial_chars"] = sum(len(item) for item in chunks)
    return "".join(chunks), diagnostics


async def _race_provider_text(
    provider: Any,
    messages: list[Message],
    *,
    max_tokens: int,
    timeout_s: float,
    reasoning_grace_s: float,
    progress: PlanningProgressCallback | None = None,
) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    race_timeout_s = max(0.2, timeout_s)
    stream_chunks: list[str] = []
    stream_diag: dict[str, Any] = {
        "transport": "race",
        "first_token_ms": None,
        "first_chunk_ms": None,
        "first_reasoning_ms": None,
        "first_content_ms": None,
        "partial_chars": 0,
        "reasoning_chars": 0,
    }

    try:
        stream = provider.stream_chat(messages, tools=None, max_tokens=max_tokens)
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        stream = provider.stream_chat(messages, tools=None)

    async def collect_stream() -> str:
        async for chunk in stream:
            now_ms = int((time.perf_counter() - started) * 1000)
            reasoning = str(getattr(chunk, "reasoning_content", "") or "")
            delta = str(getattr(chunk, "delta_text", "") or "")
            finish_reason = str(getattr(chunk, "finish_reason", "") or "")
            if finish_reason:
                stream_diag["finish_reason"] = finish_reason
            if stream_diag["first_chunk_ms"] is None:
                stream_diag["first_chunk_ms"] = now_ms
                await _notify_planning_progress(
                    progress,
                    phase="connected",
                    label="规划模型已响应，正在识别工作单元",
                    elapsed_ms=now_ms,
                    diagnostics=stream_diag,
                )
            if reasoning:
                stream_diag["reasoning_chars"] = int(stream_diag.get("reasoning_chars") or 0) + len(reasoning)
                if stream_diag["first_reasoning_ms"] is None:
                    stream_diag["first_reasoning_ms"] = now_ms
                    await _notify_planning_progress(
                        progress,
                        phase="reasoning",
                        label="正在推演任务拆分",
                        elapsed_ms=now_ms,
                        diagnostics=stream_diag,
                    )
            if not delta:
                continue
            if stream_diag["first_token_ms"] is None:
                stream_diag["first_token_ms"] = now_ms
                stream_diag["first_content_ms"] = now_ms
                await _notify_planning_progress(
                    progress,
                    phase="content",
                    label="正在生成规划结果",
                    elapsed_ms=now_ms,
                    diagnostics=stream_diag,
                )
            stream_chunks.append(delta)
            stream_diag["partial_chars"] = sum(len(item) for item in stream_chunks)
            try:
                _json_from_text("".join(stream_chunks))
                return "".join(stream_chunks)
            except (TypeError, ValueError):
                pass
        return "".join(stream_chunks)

    async def collect_chat() -> ChatResponse:
        return await _chat_provider_text(provider, messages, max_tokens=max_tokens)

    stream_task = asyncio.create_task(collect_stream())
    chat_task = asyncio.create_task(collect_chat())
    tasks = {stream_task, chat_task}
    diagnostics: dict[str, Any] = {
        "cache_hit": False,
        "transport": "race",
        "race_timeout_ms": int(race_timeout_s * 1000),
    }

    try:
        deadline = started + race_timeout_s
        pending: set[asyncio.Task[Any]] = set(tasks)
        while pending and time.perf_counter() < deadline:
            wait_s = max(0.0, deadline - time.perf_counter())
            done, pending = await asyncio.wait(pending, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            if chat_task in done:
                response = chat_task.result()
                text = str(response.text or "")
                chat_reasoning = str(getattr(response, "reasoning_content", "") or "")
                diagnostics.update({
                    "chat_elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "chat_reasoning_chars": len(chat_reasoning),
                    "chat_finish_reason": response.finish_reason,
                })
                if text.strip():
                    stream_task.cancel()
                    diagnostics.update(stream_diag)
                    diagnostics.update({
                        "transport": "chat_race_won",
                        "race_winner": "chat",
                        "partial_chars": len(text),
                        "reasoning_chars": int(stream_diag.get("reasoning_chars") or 0) + len(chat_reasoning),
                        "cancelled_transport": "stream",
                    })
                    return text, diagnostics
            if stream_task in done:
                text = stream_task.result()
                diagnostics.update({"stream_elapsed_ms": int((time.perf_counter() - started) * 1000)})
                if text.strip():
                    chat_task.cancel()
                    diagnostics.update(stream_diag)
                    diagnostics.update({
                        "transport": "stream_race_won",
                        "race_winner": "stream",
                        "cancelled_transport": "chat",
                    })
                    return text, diagnostics

        has_reasoning = int(stream_diag.get("reasoning_chars") or 0) > 0 or stream_diag.get("first_reasoning_ms") is not None
        if chat_task.done():
            try:
                response = chat_task.result()
                diagnostics["chat_reasoning_chars"] = len(str(getattr(response, "reasoning_content", "") or ""))
                diagnostics["chat_finish_reason"] = response.finish_reason
            except Exception:
                pass
        if not has_reasoning:
            for task in tasks:
                task.cancel()
            diagnostics.update(stream_diag)
            diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            raise PlanningDecisionFailure("timeout", f"PlanningDecision race timed out after {timeout_s:.1f}s", diagnostics)

        chat_task.cancel()
        await _notify_planning_progress(
            progress,
            phase="reasoning",
            label="模型仍在推演规划，尚未输出结构化结果，继续等待",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            diagnostics=stream_diag,
        )
        try:
            text = await asyncio.wait_for(stream_task, timeout=max(0.2, reasoning_grace_s))
        except asyncio.TimeoutError as exc:
            stream_task.cancel()
            diagnostics.update(stream_diag)
            diagnostics.update({
                "transport": "stream_reasoning_grace",
                "race_winner": "",
                "reasoning_grace_used": True,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            })
            raise PlanningDecisionFailure(
                "reasoning_only_timeout",
                f"PlanningDecision stream reasoning grace timed out after {reasoning_grace_s:.1f}s",
                diagnostics,
            ) from exc
        if text.strip():
            diagnostics.update(stream_diag)
            diagnostics.update({
                "transport": "stream_reasoning_grace",
                "race_winner": "stream",
                "reasoning_grace_used": True,
                "stream_elapsed_ms": int((time.perf_counter() - started) * 1000),
                "cancelled_transport": "chat",
            })
            return text, diagnostics
        diagnostics.update(stream_diag)
        diagnostics.update({
            "transport": "stream_reasoning_grace",
            "reasoning_grace_used": True,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })
        raise PlanningDecisionFailure(
            _empty_planning_response_error_type(diagnostics),
            "PlanningDecision produced no structured JSON content",
            diagnostics,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


def _has_reasoning_without_content(diagnostics: dict[str, Any]) -> bool:
    reasoning_chars = int(diagnostics.get("reasoning_chars") or 0)
    chat_reasoning_chars = int(diagnostics.get("chat_reasoning_chars") or 0)
    return (
        (reasoning_chars > 0 or chat_reasoning_chars > 0 or diagnostics.get("first_reasoning_ms") is not None)
        and int(diagnostics.get("partial_chars") or 0) == 0
        and diagnostics.get("first_content_ms") is None
    )


def _empty_planning_response_error_type(diagnostics: dict[str, Any]) -> str:
    if not _has_reasoning_without_content(diagnostics):
        return "stream_empty"
    finish_reasons = {
        str(diagnostics.get("finish_reason") or ""),
        str(diagnostics.get("stream_finish_reason") or ""),
        str(diagnostics.get("chat_finish_reason") or ""),
    }
    if "length" in finish_reasons:
        return "reasoning_only_length"
    return "reasoning_only_empty"


async def _planning_provider_warmup(provider: Any) -> None:
    key = _provider_identity(provider)
    started = time.perf_counter()
    state: dict[str, Any] = {"status": "running", "started_at": time.time()}
    _PLANNING_WARMUP_STATE[key] = state
    messages = [
        Message.system("只输出 JSON。"),
        Message.user('{"ok":true}'),
    ]
    try:
        text, diagnostics = await _stream_provider_text(
            provider,
            messages,
            max_tokens=32,
            timeout_s=PLANNING_DECISION_WARMUP_TIMEOUT,
        )
        if not text.strip():
            response = await asyncio.wait_for(
                _chat_provider_text(provider, messages, max_tokens=32),
                timeout=PLANNING_DECISION_WARMUP_TIMEOUT,
            )
            text = response.text
            diagnostics = {"transport": "chat", "partial_chars": len(text or "")}
        state.update({
            "status": "success",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "transport": diagnostics.get("transport"),
            "partial_chars": diagnostics.get("partial_chars", len(text or "")),
        })
    except Exception as exc:  # noqa: BLE001
        state.update({
            "status": "failed",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc) or exc.__class__.__name__,
        })


def schedule_planning_provider_warmup(provider: Any | None) -> None:
    if provider is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    key = _provider_identity(provider)
    task = _PLANNING_WARMUP_TASKS.get(key)
    if task is not None and not task.done():
        return
    if (_PLANNING_WARMUP_STATE.get(key) or {}).get("status") == "success":
        return
    _PLANNING_WARMUP_TASKS[key] = loop.create_task(_planning_provider_warmup(provider))


async def _planning_decision_with_llm(
    *,
    provider: Any,
    goal: str,
    spec: TeamSpec,
    members: list[TeamMemberSpec],
    requested_mode: PlanningMode,
    profile: dict[str, Any],
    progress: PlanningProgressCallback | None = None,
) -> PlanningDecisionCall:
    budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
    try:
        timeout_s = float(budget.get("planning_decision_timeout") or DEFAULT_PLANNING_DECISION_TIMEOUT)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_PLANNING_DECISION_TIMEOUT
    try:
        reasoning_grace_s = float(budget.get("planning_decision_reasoning_grace_timeout") or timeout_s)
    except (TypeError, ValueError):
        reasoning_grace_s = timeout_s
    try:
        max_work_units = int(budget.get("standard_max_work_units") or 8)
    except (TypeError, ValueError):
        max_work_units = 8
    try:
        cache_ttl = float(budget.get("planning_decision_cache_ttl") or PLANNING_DECISION_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        cache_ttl = PLANNING_DECISION_CACHE_TTL_SECONDS
    team_spec_summary = _planning_team_spec_summary(spec, profile)
    member_summary = _team_members_for_planning_prompt(members)
    system, user = planning_decision_messages(
        goal=_compact_text(goal, 1200),
        team_spec=team_spec_summary,
        members=member_summary,
        requested_mode=requested_mode,
        max_work_units=max_work_units,
    )
    messages = [Message.system(system), Message.user(user)]
    prompt_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
    await _notify_planning_progress(
        progress,
        phase="started",
        label="正在理解任务目标",
        elapsed_ms=0,
        diagnostics={"prompt_bytes": prompt_bytes, "transport": "race"},
    )
    cache_key = _planning_cache_key(
        provider=provider,
        goal=_compact_text(goal, 1200),
        team_spec=team_spec_summary,
        members=member_summary,
        requested_mode=requested_mode,
        max_work_units=max_work_units,
    )
    cached = _cache_get(cache_key, cache_ttl)
    if cached is not None:
        await _notify_planning_progress(
            progress,
            phase="parsed",
            status="done",
            label="已复用规划结果，正在生成团队执行图",
            elapsed_ms=0,
            diagnostics={"cache_hit": True, "transport": "cache", "prompt_bytes": prompt_bytes},
        )
        return PlanningDecisionCall(
            decision=cached,
            elapsed_ms=0,
            diagnostics={
                "cache_hit": True,
                "transport": "cache",
                "prompt_bytes": prompt_bytes,
                "cache_ttl": cache_ttl,
            },
        )

    started = time.perf_counter()
    diagnostics: dict[str, Any] = {
        "cache_hit": False,
        "prompt_bytes": prompt_bytes,
        "transport": "race",
    }
    try:
        text, stream_diagnostics = await _race_provider_text(
            provider,
            messages,
            max_tokens=PLANNING_DECISION_MAX_TOKENS,
            timeout_s=timeout_s,
            reasoning_grace_s=reasoning_grace_s,
            progress=progress,
        )
        diagnostics.update(stream_diagnostics)
    except PlanningDecisionFailure as exc:
        diagnostics.update(exc.diagnostics)
        raise
    except asyncio.TimeoutError as exc:
        diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        raise PlanningDecisionFailure(
            "timeout",
            f"PlanningDecision timed out after {timeout_s:.1f}s",
            diagnostics,
        ) from exc
    except Exception as exc:
        diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        raise PlanningDecisionFailure("provider_error", str(exc) or exc.__class__.__name__, diagnostics) from exc
    try:
        data = _json_from_text(text)
    except ValueError as exc:
        diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        error_type = (
            _empty_planning_response_error_type(diagnostics)
            if "empty LLM graph response" in str(exc)
            else "invalid_json"
        )
        error_message = (
            "PlanningDecision produced reasoning but no structured JSON content"
            if error_type.startswith("reasoning_only")
            else str(exc)
        )
        raise PlanningDecisionFailure(error_type, error_message, diagnostics) from exc
    try:
        decision = coerce_planning_decision(data, max_work_units=max_work_units + 4)
    except ValueError as exc:
        diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        raise PlanningDecisionFailure("schema_invalid", str(exc), diagnostics) from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    diagnostics["elapsed_ms"] = elapsed_ms
    await _notify_planning_progress(
        progress,
        phase="parsed",
        status="done",
        label="已识别工作单元，正在生成团队执行图",
        elapsed_ms=elapsed_ms,
        diagnostics=diagnostics,
    )
    _cache_put(cache_key, decision, cache_ttl)
    return PlanningDecisionCall(decision=decision, elapsed_ms=elapsed_ms, diagnostics=diagnostics)


def _json_from_text(text: str) -> dict[str, Any]:
    body = str(text or "").strip()
    parsed = extract_json_object(text)
    if parsed is None:
        message = "empty LLM graph response" if not body else "invalid LLM graph JSON"
        raise ValueError(message)
    return parsed


def _team_members_for_prompt(members: list[TeamMemberSpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for member in members:
        rows.append({
            "member_id": member.member_id,
            "name": member.name,
            "role": member.role,
            "workflow_lane": flow_builder.workflow_lane(member),
            "capabilities": list(member.capabilities or []),
            "metadata": dict(member.metadata or {}),
        })
    return rows


def _ai_llm_prompt(
    *,
    goal: str,
    spec: TeamSpec,
    members: list[TeamMemberSpec],
    profile: dict[str, Any],
    policy_report: TeamPolicyReport,
) -> list[Message]:
    budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
    max_nodes = budget.get("max_nodes") or 7
    payload = {
        "goal": goal,
        "team_spec": spec.to_dict(),
        "execution_profile": profile,
        "members": _team_members_for_prompt(members),
        "policy_report": policy_report.to_dict(),
        "limits": {
            "max_nodes": max_nodes,
            "strategy": "ai_single_dag",
            "must_respect_user_team": True,
            "no_auto_staffing_or_reassignment_without_user_consent": True,
            "planning_modes": flow_builder.planning_modes(profile),
        },
    }
    system = (
        "你是 Crew Team 的 AI DAG 规划器。只输出 JSON，不输出解释。\n"
        "目标：为 AI Planner 生成一个单方案 DAG，优先保证协作质量与依赖完整性。\n"
        "约束：只能使用 leader 或 members 中存在的 member_id 作为 assignee；不得自动补员、改派或改变用户团队；"
        "如团队能力不足，在节点 detail 中写清风险和需用户确认事项。\n"
        "节点应具体、可执行、数量克制；必须包含 leader_plan 和 leader_summary；"
        "当 planning_modes 要求方案时，build lane 使用 design -> leader review -> build，verify lane 使用 plan -> leader review -> verify。"
    )
    user = (
        "请基于以下上下文生成 Team DAG JSON。\n"
        "JSON schema:\n"
        "{\n"
        '  "nodes": [\n'
        '    {"id": "leader_plan", "title": "...", "detail": "...", "assignee": "leader", "workflow_lane": "lead"},\n'
        '    {"id": "node_id", "title": "...", "detail": "...", "assignee": "member_id", "workflow_lane": "plan|design|build|verify|docs|release|summary|other", "required_capabilities": ["上下文能力key"]}\n'
        "  ],\n"
        '  "edges": [["leader_plan", "node_id"], ["node_id", "leader_summary"]],\n'
        '  "notes": ["可选规划说明"]\n'
        "}\n\n"
        f"上下文：\n{json.dumps(payload, ensure_ascii=False)}"
    )
    return [Message.system(system), Message.user(user)]


def _coerce_llm_nodes(
    *,
    data: dict[str, Any],
    team: Any,
    goal: str,
) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
    member_map = _member_by_id(team)
    valid_assignees = {"leader", *member_map.keys()}
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("LLM graph response missing nodes")
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or item.get("node_id") or f"llm_node_{index + 1}").strip()
        if not node_id or node_id in seen:
            continue
        assignee = str(item.get("assignee") or "").strip() or "leader"
        if assignee not in valid_assignees:
            raise ValueError(f"LLM graph assigned unknown member: {assignee}")
        lane = str(item.get("workflow_lane") or "").strip().lower()
        if not lane:
            lane = _member_lane_for_assignee(member_map, assignee, "other")
        metadata = _member_metadata_for_assignee(member_map, assignee, lane)
        metadata.pop("required_capabilities", None)
        metadata.pop("capability_source", None)
        if isinstance(item.get("required_capabilities"), list):
            metadata.update({
                "required_capabilities": list(item.get("required_capabilities") or []),
                "capability_source": "ai_planner",
            })
        nodes.append({
            "id": node_id,
            "title": str(item.get("title") or node_id).strip(),
            "detail": str(item.get("detail") or item.get("description") or goal).strip(),
            "assignee": assignee,
            "metadata": metadata,
        })
        seen.add(node_id)
    if not nodes:
        raise ValueError("LLM graph response produced no valid nodes")

    if "leader_plan" not in seen:
        nodes.insert(0, {
            "id": "leader_plan",
            "title": f"Leader 拆分任务：{flow_builder.goal_title(goal)}",
            "detail": f"确认目标、成员分工、依赖和验收标准：{goal}",
            "assignee": "leader",
            "metadata": flow_builder.node_metadata("lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead"),
        })
        seen.add("leader_plan")
    if "leader_summary" not in seen:
        nodes.append({
            "id": "leader_summary",
            "title": f"Leader 汇总：{flow_builder.goal_title(goal)}",
            "detail": f"汇总所有成员结果，形成面向用户的最终交付说明：{goal}",
            "assignee": "leader",
            "metadata": flow_builder.node_metadata("summary", label="汇总结论、验收反馈", key="team_lead"),
        })
        seen.add("leader_summary")

    edges = []
    raw_edges = data.get("edges") if isinstance(data.get("edges"), list) else []
    for item in raw_edges:
        pair = _edge_pair(item)
        if pair is None:
            continue
        parent, child = pair
        if parent in seen and child in seen and parent != child:
            edges.append([parent, child])

    if not edges:
        executable = next((node["id"] for node in nodes if node["id"] not in {"leader_plan", "leader_summary"}), "")
        if executable:
            edges = [["leader_plan", executable], [executable, "leader_summary"]]
        else:
            edges = [["leader_plan", "leader_summary"]]
    notes = [
        str(item)
        for item in (data.get("notes") if isinstance(data.get("notes"), list) else [])
        if str(item).strip()
    ]
    return nodes, edges, notes


async def _build_ai_workflow_nodes_with_llm(
    *,
    provider: Any,
    team: Any,
    goal: str,
    spec: TeamSpec,
    profile: dict[str, Any],
    policy_report: TeamPolicyReport,
    members: list[TeamMemberSpec],
) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
    budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
    try:
        timeout_s = float(budget.get("ai_planning_timeout") or 20.0)
    except (TypeError, ValueError):
        timeout_s = 20.0
    response = await asyncio.wait_for(
        provider.chat(
            _ai_llm_prompt(
                goal=goal,
                spec=spec,
                members=members,
                profile=profile,
                policy_report=policy_report,
            ),
            tools=None,
        ),
        timeout=max(0.2, timeout_s),
    )
    data = _json_from_text(response.text)
    nodes, edges, notes = _coerce_llm_nodes(data=data, team=team, goal=goal)
    return nodes, edges, ["AI Planner 使用 LLM 生成单方案 DAG。", *notes]


def _node_contract(goal: str, node: PlanNode, metadata: dict[str, Any]) -> dict[str, Any]:
    lane = str(metadata.get("workflow_lane") or "other")
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
    return {
        "purpose": node.title,
        "inputs": ["用户目标", "上游节点结果"],
        "outputs": outputs,
        "acceptance_criteria": [
            "输出必须具体可检查。",
            "如需改变团队成员或补员，必须先提示用户并等待确认。",
            f"结果必须服务于用户目标：{goal}",
        ],
        "requires_leader_review": "review" in node.id or "方案" in node.title or "审阅" in node.title,
        "conflict_scope": lane,
    }


def _normalize_nodes_with_graph(
    *,
    goal: str,
    raw_nodes: list[dict[str, Any]],
    raw_edges: list[Any],
    valid_roles: list[str],
    execution_profile: dict[str, Any] | None = None,
    plan_strategy: str = "rule_dag_with_plan_graph",
    member_capabilities: dict[str, list[str]] | None = None,
    member_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[list[str]], list[str]]:
    raw_nodes, normalized_raw_edges, id_notes = _normalize_raw_graph_ids(raw_nodes, raw_edges)
    plan_nodes: list[PlanNode] = []
    raw_meta: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("id") or raw.get("node_id") or f"node_{index + 1}").strip()
        plan_nodes.append(PlanNode(
            id=node_id,
            title=str(raw.get("title") or node_id).strip(),
            detail=str(raw.get("detail") or raw.get("description") or raw.get("title") or "").strip(),
            assignee=str(raw.get("assignee") or "").strip() or "leader",
        ))
        raw_meta[node_id] = dict(raw.get("metadata") or {})

    plan_edges: list[PlanEdge] = []
    for parent, child in normalized_raw_edges:
        plan_edges.append(PlanEdge(parent_id=parent, child_id=child))

    graph = PlanGraph.from_plan_result(
        PlanResult(summary=goal, nodes=plan_nodes, edges=plan_edges),
        valid_roles=valid_roles,
        default_role=next((role for role in valid_roles if role != "leader"), valid_roles[0] if valid_roles else "leader"),
    )
    graph.validate_and_fix()
    _dedupe_graph_edges(graph)
    graph_notes = _ensure_leader_summary_terminal(graph)

    normalized_nodes: list[dict[str, Any]] = []
    capability_notes: list[str] = []
    capability_sets = {
        str(member_id): normalize_capabilities(capabilities)
        for member_id, capabilities in (member_capabilities or {}).items()
        if str(member_id).strip()
    }
    ordered_nodes = [
        graph.nodes[node.id]
        for node in plan_nodes
        if node.id in graph.nodes
    ]
    ordered_nodes.extend(
        node for node in graph.topological_order()
        if node.id not in {item.id for item in ordered_nodes}
    )
    for node in ordered_nodes:
        metadata = dict(raw_meta.get(node.id) or {})
        lane = str(metadata.get("workflow_lane") or "other").strip() or "other"
        raw_capabilities = (
            metadata.get("required_capabilities")
            if isinstance(metadata.get("required_capabilities"), list)
            else []
        )
        invalid_capabilities = [
            str(item)
            for item in raw_capabilities
            if str(item).strip() and not normalize_capability(item)
        ]
        required_capabilities = normalize_capabilities(raw_capabilities)
        is_control_node = node.assignee == "leader" and node.id.startswith("leader_")
        is_leader_direct = (
            node.assignee == "leader"
            and metadata.get("capability_source") == "leader_direct"
        )
        if invalid_capabilities:
            raise ValueError(
                f"node {node.id} contains unsupported capabilities: "
                + ", ".join(invalid_capabilities)
            )
        if not is_control_node and not is_leader_direct and not required_capabilities:
            raise ValueError(f"execution node {node.id} missing required_capabilities")
        coverage = None
        assignment_reason = ""
        previous_assignee = node.assignee or "leader"
        if required_capabilities and not is_control_node and not is_leader_direct:
            coverage = evaluate_capability_coverage(
                required_capabilities,
                capability_sets=capability_sets,
                assigned_agent_ids=[previous_assignee],
            )
            if coverage.status != "covered":
                replacement = next(
                    (
                        member_id
                        for member_id, capabilities in capability_sets.items()
                        if member_id != previous_assignee
                        and evaluate_capability_coverage(
                            required_capabilities,
                            capability_sets={member_id: capabilities},
                            assigned_agent_ids=[member_id],
                        ).status == "covered"
                    ),
                    "",
                )
                if replacement:
                    node.assignee = replacement
                    assignment_reason = "已有成员完整覆盖节点能力，规划阶段确定性修正负责人。"
                    replacement_metadata = dict((member_metadata or {}).get(replacement) or {})
                    for key in (
                        "role_label",
                        "role_key",
                        "formation_plan_version",
                        "formation_scope_key",
                        "responsibility_mission",
                        "expected_outputs",
                    ):
                        if key in replacement_metadata:
                            metadata[key] = replacement_metadata[key]
                        else:
                            metadata.pop(key, None)
                    coverage = evaluate_capability_coverage(
                        required_capabilities,
                        capability_sets=capability_sets,
                        assigned_agent_ids=[replacement],
                    )
                    capability_notes.append(
                        f"节点 {node.id} 已从 {previous_assignee} 改派给 {replacement}：{assignment_reason}"
                    )
                else:
                    capability_notes.append(
                        f"节点 {node.id} 生成时未找到能完整覆盖 "
                        f"{'、'.join(required_capabilities)} 的现有成员；保留计划缺口，运行前不得伪装为已覆盖。"
                    )
        metadata.update({
            "workflow_lane": lane,
            "required_capabilities": required_capabilities,
            "capability_source": str(
                metadata.get("capability_source")
                or ("control_node" if is_control_node else "")
            ),
            "display_order": metadata.get("display_order") or workflow_lane_order(lane),
            "planner": "team_graph_planner",
            "plan_strategy": plan_strategy,
            "execution_profile": dict(execution_profile or {}),
            "execution_budget": dict((execution_profile or {}).get("budget") or {}),
            "execution_mode": str(
                (execution_profile or {}).get("selected_mode")
                or (execution_profile or {}).get("requested_mode")
                or "standard"
            ),
            "agent_log_style": "agent_turn",
            "execution_events": list(metadata.get("execution_events") or []),
            "execution_contract": _node_contract(goal, node, metadata),
        })
        if coverage is not None:
            metadata["capability_coverage"] = coverage.to_dict()
            metadata["capability_status"] = coverage.status
            if assignment_reason:
                metadata.update({
                    "assignment_source": "existing_member_reassignment",
                    "previous_assignee": previous_assignee,
                    "assignment_reason": assignment_reason,
                })
            elif coverage.status != "covered":
                metadata["capability_gap_source"] = "dag_admission"
        normalized_nodes.append({
            "id": node.id,
            "title": node.title,
            "detail": node.detail,
            "assignee": node.assignee or "leader",
            "metadata": metadata,
        })

    normalized_edges = [[edge.parent_id, edge.child_id] for edge in graph.edges]
    notes = [
        "复用 Dynamic Kanban PlanGraph 完成 Team DAG 校验。",
        "当前版本尊重用户团队配置；仅在现有成员完整覆盖时做本轮确定性改派，不新增成员。",
        *id_notes,
        *graph_notes,
        *capability_notes,
    ]
    if plan_strategy == "fast_minimal_path":
        notes.insert(0, "Fast Team 使用 workflow_lane 极简协作 DAG：leader_plan -> fast_execute -> leader_summary，必要时插入 fast_verify。")
    if plan_strategy == "ai_single_dag":
        notes.insert(0, "AI Planner 使用 LLM 单方案 DAG，并通过 PlanGraph 校验依赖。")
    if plan_strategy == "standard_role_dag":
        notes.insert(0, "Standard Team 使用固定角色分层 DAG，并通过 PlanGraph 校验依赖；仅在显式预算存在时裁剪可选节点。")
    return normalized_nodes, normalized_edges, notes


def _annotate_planner_metrics(
    nodes: list[dict[str, Any]],
    *,
    status: str,
    elapsed_ms: int | None = None,
    error: str = "",
    error_type: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> None:
    if not nodes:
        return
    meta = dict(nodes[0].get("metadata") or {})
    meta["llm_planning_status"] = status
    if elapsed_ms is not None:
        meta["llm_planning_elapsed_ms"] = elapsed_ms
    if error:
        meta["llm_planning_error"] = error
    if error_type:
        meta["llm_planning_error_type"] = error_type
    for key, value in (diagnostics or {}).items():
        if key in {
            "transport",
            "prompt_bytes",
            "first_token_ms",
            "first_chunk_ms",
            "first_reasoning_ms",
            "first_content_ms",
            "partial_chars",
            "reasoning_chars",
            "race_timeout_ms",
            "race_winner",
            "chat_elapsed_ms",
            "stream_elapsed_ms",
            "chat_reasoning_chars",
            "chat_finish_reason",
            "reasoning_grace_used",
            "cancelled_transport",
            "cache_hit",
        }:
            meta[f"llm_planning_{key}"] = value
    nodes[0]["metadata"] = meta


def _attach_policy_warnings(
    nodes: list[dict[str, Any]],
    policy_report: TeamPolicyReport,
) -> None:
    if not policy_report.warnings or not nodes:
        return
    first = nodes[0]
    meta = dict(first.get("metadata") or {})
    meta["policy_report"] = policy_report.to_dict()
    meta["execution_events"] = [
        *list(meta.get("execution_events") or []),
        *[
            {
                "id": f"{first['id']}:policy:{index}",
                "kind": "status",
                "event_type": "status",
                "event_icon": "alert",
                "event_title": "团队配置提示",
                "event_text": warning.message,
                "collapsed": False,
            }
            for index, warning in enumerate(policy_report.warnings)
        ],
    ]
    first["metadata"] = meta


def _result_with_workflow_plan(
    *,
    spec: TeamSpec,
    nodes: list[dict[str, Any]],
    edges: list[list[str]],
    policy_report: TeamPolicyReport,
    notes: list[str],
    profile: dict[str, Any],
    requested_mode: PlanningMode,
    selected_mode: PlanningMode,
    engine: str,
    dependency_pattern: str,
    quality_policy: str,
    confidence: dict[str, float],
    fallback_from: str | None = None,
    extra_warnings: list[str] | None = None,
    critical_missing_info: list[str] | None = None,
    planning_decision: dict[str, Any] | None = None,
) -> TeamGraphPlan:
    _attach_policy_warnings(nodes, policy_report)
    warnings = [item.message for item in policy_report.warnings]
    warnings.extend(extra_warnings or [])
    workflow_plan = workflow_plan_from_graph(
        goal=spec.goal,
        team_spec=spec.to_dict(),
        nodes=nodes,
        edges=edges,
        requested_mode=requested_mode,
        selected_mode=selected_mode,
        engine=engine,
        dependency_pattern=dependency_pattern,
        quality_policy=quality_policy,
        confidence=confidence,
        reasons=notes,
        budget=dict(profile.get("budget") or {}),
        warnings=warnings,
        fallback_from=fallback_from,
        planning_decision=planning_decision,
    ).to_dict()
    return TeamGraphPlan(
        spec=spec,
        nodes=nodes,
        edges=edges,
        policy_report=policy_report,
        planner_notes=notes,
        workflow_plan=workflow_plan,
        critical_missing_info=list(critical_missing_info or []),
    )


class TeamGraphPlanner:
    """Create a TeamPlan DAG while respecting user-defined teams."""

    def plan(
        self,
        team: Any,
        goal: str,
        execution_profile: dict[str, Any] | None = None,
        team_spec: TeamSpecInput = None,
    ) -> TeamGraphPlan:
        spec_source = team_spec if team_spec is not None else {"goal": goal}
        if isinstance(spec_source, dict) and not str(spec_source.get("goal") or "").strip():
            spec_source = {"goal": goal, **spec_source}
        base_spec = build_team_spec(spec_source)
        profile = _merged_execution_profile(base_spec, execution_profile)
        requested_mode = normalize_planning_mode(profile.get("requested_mode"))
        selected_mode: PlanningMode = "fast" if requested_mode == "fast" else "standard"
        fallback_from = "ai" if requested_mode == "ai" else None
        profile.update({"requested_mode": requested_mode, "selected_mode": selected_mode})
        spec = replace(base_spec, execution_profile=_runtime_execution_profile(profile))
        members: list[TeamMemberSpec] = list((getattr(team, "members", {}) or {}).values())
        policy_report = analyze_team_policy(spec=spec, members=members)
        budget_notes: list[str] = []
        if selected_mode == "fast":
            raw_nodes, raw_edges = _build_fast_workflow_nodes(
                team,
                goal,
                profile,
                required_capabilities=normalize_capabilities(spec.team_requirements.get("capabilities") or []),
                required_lanes=set(spec.team_requirements.get("workflow_lanes") or []),
                task_profile=spec.task_profile,
            )
            plan_strategy = "fast_minimal_path"
        else:
            raw_nodes, raw_edges, budget_notes = _build_standard_workflow_nodes(team, goal, profile, spec)
            plan_strategy = "standard_role_dag"
        nodes, edges, notes = _normalize_nodes_with_graph(
            goal=goal,
            raw_nodes=raw_nodes,
            raw_edges=raw_edges,
            valid_roles=_valid_member_ids(team),
            execution_profile=profile,
            plan_strategy=plan_strategy,
            member_capabilities=_member_capability_sets(team),
            member_metadata=_member_metadata_sets(team),
        )
        if plan_strategy == "standard_role_dag" and budget_notes:
            notes = [*budget_notes, *notes]
        if fallback_from:
            notes = ["同步规划没有可用 LLM，AI Planner 回退 Standard。", *notes]
        return _result_with_workflow_plan(
            spec=spec,
            nodes=nodes,
            edges=edges,
            policy_report=policy_report,
            notes=notes,
            profile=profile,
            requested_mode=requested_mode,
            selected_mode=selected_mode,
            engine="fast_compiler" if selected_mode == "fast" else "legacy_role_compiler",
            dependency_pattern="sequential" if selected_mode == "fast" else "staged",
            quality_policy="none" if selected_mode == "fast" else "leader_review",
            confidence={"requirement": 0.68, "topology": 1.0, "capability": 1.0, "overall": 0.68},
            fallback_from=fallback_from,
        )

    async def plan_async(
        self,
        team: Any,
        goal: str,
        execution_profile: dict[str, Any] | None = None,
        *,
        team_spec: TeamSpecInput = None,
        provider: Any | None = None,
        planning_progress: PlanningProgressCallback | None = None,
    ) -> TeamGraphPlan:
        spec_source = team_spec if team_spec is not None else {"goal": goal}
        if isinstance(spec_source, dict) and not str(spec_source.get("goal") or "").strip():
            spec_source = {"goal": goal, **spec_source}
        base_spec = build_team_spec(spec_source)
        profile = _merged_execution_profile(base_spec, execution_profile)
        requested_mode = normalize_planning_mode(profile.get("requested_mode"))
        members: list[TeamMemberSpec] = list((getattr(team, "members", {}) or {}).values())
        if provider is None or requested_mode == "fast":
            return self.plan(team, goal, execution_profile=execution_profile, team_spec=base_spec)

        decision: PlanningDecision | None = None
        decision_error = ""
        decision_error_type = ""
        decision_diagnostics: dict[str, Any] = {}
        if requested_mode in {"auto", "standard"}:
            try:
                started = time.perf_counter()
                decision_call = await _planning_decision_with_llm(
                    provider=provider,
                    goal=goal,
                    spec=base_spec,
                    members=members,
                    requested_mode=requested_mode,
                    profile=profile,
                    progress=planning_progress,
                )
                decision = decision_call.decision
                decision_elapsed_ms = decision_call.elapsed_ms
                decision_diagnostics = dict(decision_call.diagnostics)
            except PlanningDecisionFailure as exc:
                decision_error = str(exc)
                decision_error_type = exc.error_type
                decision_diagnostics = dict(exc.diagnostics)
                decision_elapsed_ms = int((time.perf_counter() - started) * 1000) if "started" in locals() else 0
            except Exception as exc:  # noqa: BLE001
                decision_error = str(exc)
                decision_error_type = "unknown"
                decision_diagnostics = {}
                decision_elapsed_ms = int((time.perf_counter() - started) * 1000) if "started" in locals() else 0

        if decision is not None:
            budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
            try:
                standard_limit = int(budget.get("standard_max_work_units") or 8)
            except (TypeError, ValueError):
                standard_limit = 8
            selected_mode = select_planning_mode(
                decision,
                requested_mode=requested_mode,
                standard_max_work_units=standard_limit,
            )
        elif requested_mode == "ai":
            selected_mode = "ai"
        else:
            fallback = self.plan(
                team,
                goal,
                execution_profile={**profile, "requested_mode": "standard"},
                team_spec=base_spec,
            )
            await _notify_planning_progress(
                planning_progress,
                phase="fallback",
                status="fallback",
                label="规划模型响应较慢，已切换到稳定执行图",
                elapsed_ms=decision_elapsed_ms,
                diagnostics=decision_diagnostics,
            )
            _annotate_planner_metrics(
                fallback.nodes,
                status="fallback",
                elapsed_ms=decision_elapsed_ms,
                error=decision_error,
                error_type=decision_error_type,
                diagnostics=decision_diagnostics,
            )
            fallback_plan = dict(fallback.workflow_plan)
            planning = dict(fallback_plan.get("planning") or {})
            planning.update({
                "requested_mode": requested_mode,
                "selected_mode": "standard",
                "fallback_from": "planning_decision",
                "planning_decision": {
                    "status": "fallback",
                    "elapsed_ms": decision_elapsed_ms,
                    "error_type": decision_error_type or "unknown",
                    "error": decision_error,
                    "fallback_from": "planning_decision",
                    **decision_diagnostics,
                },
            })
            fallback_plan["planning"] = planning
            return TeamGraphPlan(
                spec=fallback.spec,
                nodes=fallback.nodes,
                edges=fallback.edges,
                policy_report=fallback.policy_report,
                planner_notes=[
                    f"PlanningDecision 失败，耗时 {decision_elapsed_ms}ms，回退确定性 Standard：{decision_error}",
                    *fallback.planner_notes,
                ],
                workflow_plan=fallback_plan,
            )

        if decision is not None:
            base_spec = team_spec_from_planning_decision(base_spec, decision)
        profile.update({"requested_mode": requested_mode, "selected_mode": selected_mode})
        spec = replace(base_spec, execution_profile=_runtime_execution_profile(profile))
        policy_report = analyze_team_policy(spec=spec, members=members)

        if selected_mode == "fast":
            fast_required_capabilities = normalize_capabilities(
                capability
                for unit in (decision.work_units if decision is not None else [])
                for capability in unit.required_capabilities
            )
            if not fast_required_capabilities:
                fast_required_capabilities = normalize_capabilities(
                    spec.team_requirements.get("capabilities") or []
                )
            raw_nodes, raw_edges = _build_fast_workflow_nodes(
                team,
                goal,
                profile,
                required_capabilities=fast_required_capabilities,
                required_lanes=set(spec.team_requirements.get("workflow_lanes") or []),
                capability_source="work_unit_summary" if decision is not None else "team_spec",
                task_profile=spec.task_profile,
            )
            nodes, edges, notes = _normalize_nodes_with_graph(
                goal=goal,
                raw_nodes=raw_nodes,
                raw_edges=raw_edges,
                valid_roles=_valid_member_ids(team),
                execution_profile=profile,
                plan_strategy="fast_minimal_path",
                member_capabilities=_member_capability_sets(team),
                member_metadata=_member_metadata_sets(team),
            )
            confidence = confidence_dimensions(decision, capability_coverage=1.0) if decision else {
                "requirement": 0.68, "topology": 1.0, "capability": 1.0, "overall": 0.68,
            }
            await _notify_planning_progress(
                planning_progress,
                phase="compiled",
                status="done",
                label="团队执行图已生成",
                elapsed_ms=decision_elapsed_ms,
                diagnostics=decision_diagnostics,
            )
            notes = [f"PlanningDecision 耗时 {decision_elapsed_ms}ms，Auto 选择 Fast。", *notes]
            return _result_with_workflow_plan(
                spec=spec, nodes=nodes, edges=edges, policy_report=policy_report, notes=notes,
                profile=profile, requested_mode=requested_mode, selected_mode="fast",
                engine="fast_compiler", dependency_pattern="sequential", quality_policy="none",
                confidence=confidence,
                extra_warnings=list(decision.critical_missing_info) if decision else [],
                critical_missing_info=list(decision.critical_missing_info) if decision else [],
                planning_decision={
                    "status": "success",
                    "elapsed_ms": decision_elapsed_ms,
                    "error_type": "",
                    "error": "",
                    "fallback_from": None,
                    **decision_diagnostics,
                } if decision else None,
            )

        if selected_mode == "standard" and decision is not None:
            raw_nodes, raw_edges, semantic_notes, coverage = _semantic_standard_workflow_nodes(
                team,
                goal,
                decision,
                profile,
                set(spec.team_requirements.get("workflow_lanes") or []),
            )
            nodes, edges, normalize_notes = _normalize_nodes_with_graph(
                goal=goal,
                raw_nodes=raw_nodes,
                raw_edges=raw_edges,
                valid_roles=_valid_member_ids(team),
                execution_profile=profile,
                plan_strategy="standard_semantic_dag",
                member_capabilities=_member_capability_sets(team),
                member_metadata=_member_metadata_sets(team),
            )
            confidence = confidence_dimensions(decision, capability_coverage=coverage)
            await _notify_planning_progress(
                planning_progress,
                phase="compiled",
                status="done",
                label="团队执行图已生成",
                elapsed_ms=decision_elapsed_ms,
                diagnostics=decision_diagnostics,
            )
            notes = [
                f"PlanningDecision 耗时 {decision_elapsed_ms}ms。",
                *semantic_notes,
                *normalize_notes,
            ]
            return _result_with_workflow_plan(
                spec=spec, nodes=nodes, edges=edges, policy_report=policy_report, notes=notes,
                profile=profile, requested_mode=requested_mode, selected_mode="standard",
                engine="topology_compiler", dependency_pattern=decision.dependency_pattern,
                quality_policy=decision.quality_policy, confidence=confidence,
                extra_warnings=list(decision.critical_missing_info),
                critical_missing_info=list(decision.critical_missing_info),
                planning_decision={
                    "status": "success",
                    "elapsed_ms": decision_elapsed_ms,
                    "error_type": "",
                    "error": "",
                    "fallback_from": None,
                    **decision_diagnostics,
                },
            )

        try:
            started = time.perf_counter()
            raw_nodes, raw_edges, llm_notes = await _build_ai_workflow_nodes_with_llm(
                provider=provider,
                team=team,
                goal=goal,
                spec=spec,
                profile=profile,
                policy_report=policy_report,
                members=members,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            nodes, edges, notes = _normalize_nodes_with_graph(
                goal=goal,
                raw_nodes=raw_nodes,
                raw_edges=raw_edges,
                valid_roles=_valid_member_ids(team),
                execution_profile=profile,
                plan_strategy="ai_single_dag",
                member_capabilities=_member_capability_sets(team),
                member_metadata=_member_metadata_sets(team),
            )
            _annotate_planner_metrics(nodes, status="success", elapsed_ms=elapsed_ms)
            notes = [f"AI Planner LLM DAG 耗时 {elapsed_ms}ms。", *llm_notes, *notes]
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((time.perf_counter() - started) * 1000) if "started" in locals() else None
            fallback = self.plan(
                team,
                goal,
                execution_profile={**profile, "requested_mode": "standard"},
                team_spec=spec,
            )
            _annotate_planner_metrics(
                fallback.nodes,
                status="fallback",
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )
            return TeamGraphPlan(
                spec=fallback.spec,
                nodes=fallback.nodes,
                edges=fallback.edges,
                policy_report=fallback.policy_report,
                planner_notes=[
                    f"AI Planner 失败，耗时 {elapsed_ms if elapsed_ms is not None else 'unknown'}ms，已回退 Standard：{exc}",
                    *fallback.planner_notes,
                ],
                workflow_plan={
                    **fallback.workflow_plan,
                    "planning": {
                        **dict(fallback.workflow_plan.get("planning") or {}),
                        "requested_mode": requested_mode,
                        "selected_mode": "standard",
                        "fallback_from": "ai",
                    },
                },
            )
        ai_confidence = confidence_dimensions(decision, capability_coverage=1.0) if decision else {
            "requirement": 0.68, "topology": 0.68, "capability": 1.0, "overall": 0.68,
        }
        return _result_with_workflow_plan(
            spec=spec, nodes=nodes, edges=edges, policy_report=policy_report, notes=notes,
            profile=profile, requested_mode=requested_mode, selected_mode="ai",
            engine="ai_planner", dependency_pattern="dynamic", quality_policy="leader_review",
            confidence=ai_confidence,
            extra_warnings=list(decision.critical_missing_info) if decision else [],
            critical_missing_info=list(decision.critical_missing_info) if decision else [],
        )
