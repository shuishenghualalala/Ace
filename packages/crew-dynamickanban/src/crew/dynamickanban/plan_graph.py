"""Dynamic Kanban 计划图：显式节点/边 + 拓扑序 + 确定性校验。

采用 Kanban 的“先草拟任务图，再按依赖创建卡片”思想：
- Planner 输出稳定的符号节点 ID 和边；
- 引擎按拓扑序创建任务，父任务的真实 DB ID 直接作为子任务的 parent_task_ids；
- 所有结构合法性校验在写 DB 之前完成。
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from crew.dynamickanban.models import PlanEdge, PlanNode, PlanResult
from crew.state.logging import get_logger

log = get_logger("dynamickanban.plan_graph")


class WorkflowGraphValidationError(ValueError):
    """Workflow definition 的 edges 不是可执行 DAG。"""


def validate_dag(
    node_ids: list[str],
    edges: list[tuple[str, str]],
    *,
    graph_name: str,
    node_name: str,
) -> list[tuple[str, str]]:
    """严格校验任意节点/边 DAG，并返回稳定去重后的边。"""

    normalized_ids = [str(node_id or "").strip() for node_id in node_ids]
    if not normalized_ids:
        raise WorkflowGraphValidationError(f"{graph_name} 至少需要一个 {node_name}")
    if any(not node_id for node_id in normalized_ids):
        raise WorkflowGraphValidationError(f"{graph_name} 包含空 {node_name} id")
    if len(set(normalized_ids)) != len(normalized_ids):
        raise WorkflowGraphValidationError(f"{graph_name} 包含重复 {node_name} id")

    known_ids = set(normalized_ids)
    normalized_edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    indegree = {phase_id: 0 for phase_id in normalized_ids}
    successors = {phase_id: [] for phase_id in normalized_ids}
    for raw_parent, raw_child in edges:
        parent = str(raw_parent or "").strip()
        child = str(raw_child or "").strip()
        if not parent or not child:
            raise WorkflowGraphValidationError(f"{graph_name} 包含空边端点")
        if parent not in known_ids or child not in known_ids:
            raise WorkflowGraphValidationError(
                f"{graph_name} 包含未知 {node_name} 引用: {parent} -> {child}"
            )
        if parent == child:
            raise WorkflowGraphValidationError(
                f"{graph_name} 包含自环: {parent} -> {child}"
            )
        edge = (parent, child)
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        normalized_edges.append(edge)
        successors[parent].append(child)
        indegree[child] += 1

    entries = [phase_id for phase_id in normalized_ids if indegree[phase_id] == 0]
    if not entries:
        raise WorkflowGraphValidationError(
            f"{graph_name} 没有合法入口 {node_name}，包含循环依赖"
        )

    queue = deque(entries)
    visited = 0
    while queue:
        phase_id = queue.popleft()
        visited += 1
        for child in successors[phase_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(normalized_ids):
        raise WorkflowGraphValidationError(f"{graph_name} 包含循环依赖")

    return normalized_edges


def validate_workflow_dag(
    phase_ids: list[str],
    edges: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """严格校验 Workflow phase DAG；持久化定义遇到坏图必须 fail closed。"""
    return validate_dag(
        phase_ids,
        edges,
        graph_name="workflow definition",
        node_name="phase",
    )

# 语义校验时忽略的高频虚词（中英文）
_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with",
    "的", "了", "和", "与", "在", "为", "是", "有", "被", "把", "从", "对",
    "进行", "完成", "实现", "编写", "撰写", "分析", "设计", "测试", "代码", "文档",
    "结果", "输出", "任务", "功能", "系统", "用户", "数据", "接口", "页面",
}


@dataclass
class PlanGraph:
    """Planner 产出的任务图。

    nodes: 符号 ID -> PlanNode
    edges: 依赖边列表（parent -> child）
    """

    nodes: dict[str, PlanNode] = field(default_factory=dict)
    edges: list[PlanEdge] = field(default_factory=list)

    @classmethod
    def from_plan_result(
        cls,
        plan: PlanResult,
        valid_roles: list[str] | None = None,
        default_role: str = "coder",
    ) -> PlanGraph:
        """从 PlanResult 构建图，兼容新版 nodes/edges 与旧版 tasks。"""
        valid_roles = list(valid_roles or [])

        if plan.has_graph():
            nodes = {}
            for n in plan.nodes:
                node = cls._norm_node(n, valid_roles, default_role)
                nodes[node.id] = node
            return cls(nodes=nodes, edges=list(plan.edges))

        # legacy tasks -> 自动分配符号 ID task_1, task_2, ...
        nodes: dict[str, PlanNode] = {}
        title_to_id: dict[str, str] = {}
        for idx, t in enumerate(plan.tasks):
            node_id = f"task_{idx + 1}"
            node = PlanNode(
                id=node_id,
                title=str(t.get("title") or ""),
                detail=str(t.get("detail") or t.get("title") or ""),
                assignee=cls._norm_assignee(t.get("assignee"), valid_roles, default_role),
            )
            nodes[node_id] = node
            title = node.title.strip()
            if title:
                title_to_id[title] = node_id

        edges: list[PlanEdge] = []
        for idx, t in enumerate(plan.tasks):
            child_id = f"task_{idx + 1}"
            for ref in t.get("parent_task_ids") or []:
                ref_str = str(ref).strip()
                if not ref_str:
                    continue
                parent_id = None
                if ref_str in nodes:
                    parent_id = ref_str
                elif ref_str.startswith("task_"):
                    # 兼容 0-based 或 1-based 的 task_N 引用
                    try:
                        n = int(ref_str.split("_", 1)[1])
                        if 1 <= n <= len(plan.tasks):
                            parent_id = f"task_{n}"
                        elif 0 <= n < len(plan.tasks):
                            parent_id = f"task_{n + 1}"
                    except ValueError:
                        pass
                else:
                    parent_id = title_to_id.get(ref_str)
                if parent_id and parent_id != child_id:
                    edges.append(PlanEdge(parent_id=parent_id, child_id=child_id))
        return cls(nodes=nodes, edges=edges)

    @staticmethod
    def _norm_node(node: PlanNode, valid_roles: list[str], default_role: str) -> PlanNode:
        node_id = str(node.id).strip()
        if not node_id:
            node_id = f"node_{uuid.uuid4().hex[:8]}"
        return PlanNode(
            id=node_id,
            title=str(node.title or ""),
            detail=str(node.detail or node.title or ""),
            assignee=PlanGraph._norm_assignee(node.assignee, valid_roles, default_role),
        )

    @staticmethod
    def _norm_assignee(value: Any, valid_roles: list[str], default_role: str) -> str:
        role = str(value or "").strip()
        if not role:
            return default_role
        if role in valid_roles:
            return role
        lower = role.lower().replace("-", "_")
        for valid in valid_roles:
            if valid.lower().replace("-", "_") == lower:
                return valid
        return default_role

    def validate_and_fix(self, existing_task_ids: set[str] | None = None) -> None:
        """原地校验并修复图：去重节点、归一化 assignee、去自环、去未知引用、打断循环依赖。"""
        existing_task_ids = set(existing_task_ids or ())

        # 1. 去重节点 ID，保留第一次出现
        deduped: dict[str, PlanNode] = {}
        for node in self.nodes.values():
            if node.id not in deduped:
                deduped[node.id] = node
            else:
                log.warning("计划图中存在重复节点 ID: %s，已忽略重复项", node.id)
        self.nodes = deduped

        # 2. 过滤非法边
        valid_edges: list[PlanEdge] = []
        known_ids = set(self.nodes.keys()) | existing_task_ids
        for e in self.edges:
            pid = e.parent_id.strip()
            cid = e.child_id.strip()
            if not pid or not cid:
                continue
            if pid == cid:
                log.warning("计划图中存在自环依赖: %s，已移除", pid)
                continue
            if pid not in known_ids:
                log.warning("计划图中存在未知父节点引用: %s -> %s，已移除", pid, cid)
                continue
            if cid not in self.nodes and cid not in existing_task_ids:
                log.warning("计划图中存在未知子节点引用: %s -> %s，已移除", pid, cid)
                continue
            valid_edges.append(PlanEdge(parent_id=pid, child_id=cid))
        self.edges = valid_edges

        # 3. 打断循环依赖（DFS）
        self._break_cycles()

        # Planner 初稿可先做确定性修复，但写库前仍必须通过与 Runtime 相同的严格 DAG 验证器。
        if known_ids:
            normalized_edges = validate_dag(
                [*self.nodes.keys(), *sorted(existing_task_ids - set(self.nodes))],
                [(edge.parent_id, edge.child_id) for edge in self.edges],
                graph_name="计划图",
                node_name="任务",
            )
            self.edges = [
                PlanEdge(parent_id=parent_id, child_id=child_id)
                for parent_id, child_id in normalized_edges
            ]

        # 4. 语义层面的可疑依赖/遗漏依赖警告（不改图，只打日志供排查）
        self._semantic_warnings()

    def _semantic_warnings(self) -> None:
        """检查并告警可能的错误依赖或遗漏依赖。"""
        if len(self.nodes) <= 1:
            return

        def _tokens(text: str) -> set[str]:
            words = re.findall(r"[a-zA-Z0-9\u4e00-\u9fa5]{2,}", str(text).lower())
            return {w for w in words if w not in _STOP_WORDS}

        # 4.1 父子任务 assignee 不同且 title/detail 无共享关键词 → 可能是不合理依赖
        for e in self.edges:
            parent = self.nodes.get(e.parent_id)
            child = self.nodes.get(e.child_id)
            if not parent or not child:
                continue
            if parent.assignee == child.assignee:
                continue
            shared = _tokens(parent.title + " " + parent.detail) & _tokens(child.title + " " + child.detail)
            if not shared:
                log.warning(
                    "语义校验：边 %s -> %s 的父子任务 assignee 不同（%s -> %s）且无共享关键词，可能是不合理依赖",
                    e.parent_id,
                    e.child_id,
                    parent.assignee,
                    child.assignee,
                )

        # 4.2 同角色任务标题高度相似但无任何依赖 → 可能遗漏依赖或应合并
        nodes_list = list(self.nodes.values())
        for i in range(len(nodes_list)):
            for j in range(i + 1, len(nodes_list)):
                a, b = nodes_list[i], nodes_list[j]
                if a.assignee != b.assignee:
                    continue
                shared_title = _tokens(a.title) & _tokens(b.title)
                if len(shared_title) >= 2:
                    has_edge = any(
                        (e.parent_id == a.id and e.child_id == b.id)
                        or (e.parent_id == b.id and e.child_id == a.id)
                        for e in self.edges
                    )
                    if not has_edge:
                        log.warning(
                            "语义校验：同角色任务 %s「%s」与 %s「%s」标题高度相似，但未建立依赖，可能遗漏",
                            a.id,
                            a.title,
                            b.id,
                            b.title,
                        )

    def _break_cycles(self) -> None:
        """DFS 检测并移除回边。"""
        children = self.children_map()
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in self.nodes}
        removed: list[tuple[str, str]] = []

        def dfs(node_id: str, stack: list[str]) -> None:
            color[node_id] = GRAY
            for child_id in list(children.get(node_id, [])):
                if child_id in stack:
                    # 发现环
                    removed.append((node_id, child_id))
                    children[node_id].remove(child_id)
                    continue
                if color.get(child_id, WHITE) == WHITE and child_id in color:
                    dfs(child_id, stack + [child_id])
            color[node_id] = BLACK

        for nid in list(self.nodes.keys()):
            if color[nid] == WHITE:
                dfs(nid, [nid])

        if removed:
            log.warning("发现循环依赖并自动移除：%s", removed)
            # 重建 edges
            edge_set = {(e.parent_id, e.child_id) for e in self.edges}
            for pid, cid in removed:
                edge_set.discard((pid, cid))
            self.edges = [PlanEdge(parent_id=pid, child_id=cid) for pid, cid in edge_set]

    def parents_map(self) -> dict[str, list[str]]:
        parents: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            if e.child_id in parents:
                parents[e.child_id].append(e.parent_id)
        return parents

    def children_map(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            if e.parent_id in children:
                children[e.parent_id].append(e.child_id)
        return children

    def topological_order(self) -> list[PlanNode]:
        """Kahn 算法拓扑排序；若存在环则回退到 DFS 前序。"""
        in_degree = {nid: 0 for nid in self.nodes}
        children = self.children_map()
        for nid in self.nodes:
            for child_id in children.get(nid, []):
                if child_id in in_degree:
                    in_degree[child_id] += 1

        queue: deque[str] = deque([nid for nid, d in in_degree.items() if d == 0])
        ordered: list[str] = []
        while queue:
            nid = queue.popleft()
            ordered.append(nid)
            for child_id in children.get(nid, []):
                if child_id in in_degree:
                    in_degree[child_id] -= 1
                    if in_degree[child_id] == 0:
                        queue.append(child_id)

        if len(ordered) != len(self.nodes):
            # 存在环时回退：按节点 ID 顺序 + 已有 order
            remaining = [nid for nid in self.nodes if nid not in ordered]
            log.warning("拓扑排序发现环，剩余节点按 ID 顺序追加：%s", remaining)
            ordered.extend(sorted(remaining))

        return [self.nodes[nid] for nid in ordered]

    def parents_of(self, node_id: str) -> list[str]:
        return [e.parent_id for e in self.edges if e.child_id == node_id]
