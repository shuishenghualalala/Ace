"""TeamPlan 的持久化与恢复适配器。

这个模块只负责把 TeamPlan 与 Dynamic Kanban 之间的状态互相投影。
它不参与 DAG 规划、成员选择或节点执行；运行时编排仍由 TeamManager
负责，避免把持久化事实源混入业务决策。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from crew.state.logging import get_logger
from crew.team.models import TeamMemberSpec, TeamPlan, TeamPlanEdge, TeamPlanNode

log = get_logger("team.plan_store")
TeamKey = tuple[str, str]

_TEAM_PLAN_TO_KANBAN_STATUS = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "done",
    "failed": "failed",
    "blocked": "blocked",
    "needs_info": "blocked",
    "cancelled": "cancelled",
}
_KANBAN_TO_TEAM_PLAN_STATUS = {
    "pending": "pending",
    "ready": "pending",
    "running": "in_progress",
    "in_progress": "in_progress",
    "done": "completed",
    "completed": "completed",
    "failed": "failed",
    "blocked": "blocked",
    "needs_info": "blocked",
    "cancelled": "cancelled",
}


def kanban_status(status: str) -> str:
    return _TEAM_PLAN_TO_KANBAN_STATUS.get(str(status or "").strip().lower(), "pending")


def taskboard_status(status: str) -> str:
    return _KANBAN_TO_TEAM_PLAN_STATUS.get(str(status or "").strip().lower(), "pending")


def node_event_index(
    events: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, int], dict[str, dict[str, Any]]]:
    """把 Kanban 事件归一为 task/node、重试次数和节点 metadata 索引。"""

    task_to_node: dict[str, str] = {}
    attempts: dict[str, int] = {}
    node_metadata: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(events, key=lambda item: float(item.get("ts") or 0))
    for event in ordered_events:
        event_type = str(event.get("event_type") or "")
        if event_type == "team_plan_created":
            payload = dict(event.get("payload") or {})
            for node_id, task_id in dict(payload.get("node_task_ids") or {}).items():
                task_to_node[str(task_id)] = str(node_id)
            for node in payload.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("node_id") or node.get("id") or "").strip()
                metadata = node.get("metadata")
                if node_id and isinstance(metadata, dict):
                    node_metadata[node_id] = dict(metadata)
            continue
        if event_type == "workflow_plan_revised":
            payload = dict(event.get("payload") or {})
            for node_id, task_id in dict(payload.get("node_task_ids") or {}).items():
                task_to_node[str(task_id)] = str(node_id)
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            metadata_updates = delta.get("updated_node_metadata") if isinstance(delta, dict) else None
            if isinstance(metadata_updates, dict):
                for node_id, metadata in metadata_updates.items():
                    if not isinstance(metadata, dict):
                        continue
                    node_key = str(node_id or "").strip()
                    if node_key:
                        node_metadata[node_key] = {
                            **dict(node_metadata.get(node_key) or {}),
                            **dict(metadata),
                        }
            continue
        if event_type != "team_node_updated":
            continue
        task_id = str(event.get("task_id") or "")
        payload = dict(event.get("payload") or {})
        node_id = str(payload.get("node_id") or "")
        if task_id and node_id:
            task_to_node[task_id] = node_id
        if node_id:
            attempts[node_id] = max(attempts.get(node_id, 0), int(payload.get("attempt_count") or 0))
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                node_metadata[node_id] = {
                    **dict(node_metadata.get(node_id) or {}),
                    **dict(metadata),
                }
    return task_to_node, attempts, node_metadata


def _visible_session_id(session_id: str) -> str:
    marker = "::turn::"
    return session_id.split(marker, 1)[0] if marker in session_id else session_id


class TeamPlanStore:
    """TeamPlan 的 Dynamic Kanban 适配器。

    ``plans`` 等字典由 TeamManager 持有，适配器只负责更新这些运行时索引，
    不复制 Team 生命周期，也不引入第二套 TeamPlan 缓存。
    """

    def __init__(
        self,
        *,
        kanban_store: Any | None,
        plans: dict[TeamKey, TeamPlan],
        plan_workflows: dict[TeamKey, str],
        plan_node_tasks: dict[tuple[str, str, str], str],
        runtime_member_snapshots: dict[TeamKey, dict[str, TeamMemberSpec]],
        refresh_plan_status: Callable[[TeamPlan], None],
    ) -> None:
        self.kanban_store = kanban_store
        self.plans = plans
        self.plan_workflows = plan_workflows
        self.plan_node_tasks = plan_node_tasks
        self.runtime_member_snapshots = runtime_member_snapshots
        self.refresh_plan_status = refresh_plan_status

    @staticmethod
    def _key(session_id: str, owner_account_id: str = "") -> TeamKey:
        return str(owner_account_id or ""), str(session_id or "")

    def _store_for_owner(self, owner_account_id: str = "") -> Any | None:
        store = self.kanban_store
        if store is None:
            return None
        owner = str(owner_account_id or "").strip()
        if not owner:
            return None
        if hasattr(store, "for_owner"):
            return store.for_owner(owner)
        return store

    @staticmethod
    def _runtime_members_from_snapshot(raw_members: Any) -> dict[str, TeamMemberSpec]:
        if not isinstance(raw_members, list):
            return {}
        members: dict[str, TeamMemberSpec] = {}
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                continue
            try:
                member = TeamMemberSpec.from_config(raw_member)
            except (TypeError, ValueError) as exc:
                log.warning("忽略无效的 Runtime 临时成员快照 err=%s", exc)
                continue
            if member.member_id and member.member_id != "leader":
                members[member.member_id] = member
        return members

    def hydrate(
        self,
        session_id: str,
        *,
        node_id: str = "",
        external_team_id: str = "",
        owner_account_id: str = "",
    ) -> TeamPlan | None:
        """从 Dynamic Kanban 恢复最近的 TeamPlan。"""

        store = self._store_for_owner(owner_account_id)
        if store is None:
            return None
        try:
            workflows = store.list_workflows_by_session_prefix(_visible_session_id(session_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("读取持久化 TeamPlan 失败 session=%s err=%s", session_id, exc)
            return None

        candidates = [
            workflow
            for workflow in workflows
            if str((getattr(workflow, "context", {}) or {}).get("source") or "") == "team"
            and str((getattr(workflow, "context", {}) or {}).get("owner_account_id") or "")
            == str(owner_account_id or "")
        ]
        candidates.sort(
            key=lambda workflow: float(
                getattr(workflow, "updated_at", 0) or getattr(workflow, "created_at", 0) or 0
            ),
            reverse=True,
        )
        target_node_id = str(node_id or "").strip()
        requested_session_id = str(session_id or "").strip()
        requested_external_team_id = str(external_team_id or "").strip()
        for workflow in candidates:
            workflow_context = getattr(workflow, "context", {}) or {}
            if requested_external_team_id and str(
                workflow_context.get("external_team_id") or ""
            ).strip() != requested_external_team_id:
                continue
            workflow_session_id = str(getattr(workflow, "session_id", "") or "").strip()
            if requested_session_id and workflow_session_id != requested_session_id \
                    and not workflow_session_id.startswith(f"{requested_session_id}::turn::"):
                continue
            try:
                board = store.get_board_state(workflow.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 TeamPlan 看板失败 workflow=%s err=%s", workflow.id, exc)
                continue

            events = list(board.get("events") or [])
            task_to_node, attempts, event_metadata = node_event_index(events)
            tasks = list(board.get("tasks") or [])
            task_by_node: dict[str, dict[str, Any]] = {}
            for task in tasks:
                task_key = str(task.get("id") or "").strip()
                mapped_node_id = task_to_node.get(task_key, task_key)
                if mapped_node_id:
                    task_by_node[mapped_node_id] = task

            workflow_plan = dict(
                board.get("workflow_plan")
                or (getattr(workflow, "context", {}) or {}).get("workflow_plan")
                or {}
            )
            raw_nodes: dict[str, dict[str, Any]] = {
                str(raw.get("id") or raw.get("node_id") or "").strip(): dict(raw)
                for raw in workflow_plan.get("nodes") or []
                if isinstance(raw, dict)
                and str(raw.get("id") or raw.get("node_id") or "").strip()
            }
            for event in sorted(events, key=lambda item: float(item.get("ts") or 0)):
                if str(event.get("event_type") or "") != "team_plan_created":
                    continue
                for raw in (event.get("payload") or {}).get("nodes") or []:
                    if isinstance(raw, dict):
                        raw_id = str(raw.get("node_id") or raw.get("id") or "").strip()
                        if raw_id:
                            raw_nodes[raw_id] = {**raw_nodes.get(raw_id, {}), **dict(raw)}
            node_ids = list(dict.fromkeys([*raw_nodes, *task_by_node]))
            if target_node_id and target_node_id not in node_ids:
                continue
            if not node_ids:
                continue

            metadata_by_node = {node_key: dict(value) for node_key, value in event_metadata.items()}
            latest_node_events: dict[str, dict[str, Any]] = {}
            for event in sorted(events, key=lambda item: float(item.get("ts") or 0)):
                if str(event.get("event_type") or "") != "team_node_updated":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                event_node_id = str(payload.get("node_id") or "").strip()
                if event_node_id:
                    latest_node_events[event_node_id] = dict(payload)

            plan = TeamPlan(
                team_session_id=str(getattr(workflow, "session_id", "") or session_id),
                goal=str(
                    (workflow_plan.get("task") or {}).get("goal")
                    or getattr(workflow, "title", "")
                    or "团队工作流"
                ).strip(),
                plan_id=str(
                    (getattr(workflow, "context", {}) or {}).get("team_plan_id")
                    or f"persisted_{workflow.id}"
                ),
            )
            for current_node_id in node_ids:
                raw = raw_nodes.get(current_node_id, {})
                task = task_by_node.get(current_node_id, {})
                event_payload = latest_node_events.get(current_node_id, {})
                raw_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
                metadata = {**dict(raw_metadata), **dict(metadata_by_node.get(current_node_id) or {})}
                if raw.get("required_capabilities") and "required_capabilities" not in metadata:
                    metadata["required_capabilities"] = list(raw.get("required_capabilities") or [])
                assignee = str(
                    task.get("assignee")
                    if task.get("assignee") is not None
                    else raw.get("assignee_id") or raw.get("assignee") or ""
                ).strip()
                plan.nodes[current_node_id] = TeamPlanNode(
                    node_id=current_node_id,
                    title=str(
                        raw.get("display_title")
                        or raw.get("title")
                        or task.get("title")
                        or current_node_id
                    ),
                    detail=str(
                        raw.get("detail") or task.get("detail") or raw.get("title") or current_node_id
                    ),
                    assignee=assignee,
                    status=taskboard_status(str(task.get("status") or "pending")),
                    result_summary=str(task.get("result_summary") or event_payload.get("result_summary") or ""),
                    artifact_refs=list(task.get("artifact_paths") or []),
                    delegate_task_id=str(event_payload.get("delegate_task_id") or ""),
                    attempt_count=attempts.get(current_node_id, int(task.get("retry_count") or 0)),
                    last_error=(
                        str(task.get("result_summary") or event_payload.get("last_error") or "")
                        if str(task.get("status") or "") in {"failed", "blocked"}
                        else str(event_payload.get("last_error") or "")
                    ),
                    metadata=metadata,
                )

            raw_edges = workflow_plan.get("edges") or []
            edges: list[TeamPlanEdge] = []
            for raw_edge in raw_edges:
                if not isinstance(raw_edge, dict):
                    continue
                parent_id = str(raw_edge.get("from") or raw_edge.get("parent_id") or "").strip()
                child_id = str(raw_edge.get("to") or raw_edge.get("child_id") or "").strip()
                if parent_id in plan.nodes and child_id in plan.nodes:
                    edges.append(TeamPlanEdge(parent_id=parent_id, child_id=child_id))
            if not edges:
                task_to_node_id = {
                    str(task.get("id") or ""): node_key
                    for node_key, task in task_by_node.items()
                }
                for dependency in board.get("dependencies") or []:
                    parent_id = task_to_node_id.get(str(dependency.get("parent_task_id") or ""))
                    child_id = task_to_node_id.get(str(dependency.get("child_task_id") or ""))
                    if parent_id and child_id:
                        edges.append(TeamPlanEdge(parent_id=parent_id, child_id=child_id))
            unique_edges: list[TeamPlanEdge] = []
            seen_edges: set[tuple[str, str]] = set()
            for edge in edges:
                edge_key = (edge.parent_id, edge.child_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    unique_edges.append(edge)
            plan.edges = unique_edges
            self.refresh_plan_status(plan)

            key = self._key(plan.team_session_id, owner_account_id)
            self.plans[key] = plan
            self.plan_workflows[key] = str(workflow.id)
            self.runtime_member_snapshots[key] = self._runtime_members_from_snapshot(
                workflow_plan.get("runtime_members")
            )
            for current_node_id, task in task_by_node.items():
                task_id = str(task.get("id") or "").strip()
                if task_id:
                    self.plan_node_tasks[(owner_account_id, plan.team_session_id, current_node_id)] = task_id
            return plan
        return None

    def runtime_members_for_session(
        self,
        session_id: str,
        *,
        external_team_id: str = "",
        owner_account_id: str = "",
    ) -> list[TeamMemberSpec]:
        key = self._key(session_id, owner_account_id)
        if key not in self.runtime_member_snapshots:
            self.hydrate(
                session_id,
                external_team_id=external_team_id,
                owner_account_id=owner_account_id,
            )
        return list(self.runtime_member_snapshots.get(key, {}).values())

    def persist(
        self,
        plan: TeamPlan,
        *,
        owner_account_id: str = "",
        external_team_id: str = "",
        workflow_plan: dict[str, Any] | None = None,
    ) -> None:
        store = self._store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        if key in self.plan_workflows:
            return
        try:
            plan_snapshot = json.loads(json.dumps(workflow_plan or {}, ensure_ascii=False))
            if plan_snapshot:
                task = dict(plan_snapshot.get("task") or {})
                marker = "::turn::"
                task["turn_id"] = (
                    plan.team_session_id.split(marker, 1)[1].split("::", 1)[0]
                    if marker in plan.team_session_id
                    else plan.team_session_id
                )
                plan_snapshot["task"] = task
            context = {
                "source": "team",
                "owner_account_id": owner_account_id,
                "parent_session_id": _visible_session_id(plan.team_session_id),
                "team_session_id": plan.team_session_id,
                "team_plan_id": plan.plan_id,
                "external_team_id": external_team_id,
                "workflow_plan": plan_snapshot,
                "current_revision": int(plan_snapshot.get("revision") or 1) if plan_snapshot else 0,
            }
            event_payload = {
                "team_plan_id": plan.plan_id,
                "goal": plan.goal,
                "workflow_plan": plan_snapshot,
                "nodes": [node.to_dict() for node in plan.nodes.values()],
                "edges": [edge.to_dict() for edge in plan.edges],
            }
            if hasattr(store, "create_workflow_graph"):
                workflow, tasks = store.create_workflow_graph(
                    plan.team_session_id,
                    plan.goal,
                    context=context,
                    nodes=[
                        {
                            "id": node.node_id,
                            "title": node.title,
                            "detail": node.detail,
                            "assignee": node.assignee,
                            "status": kanban_status(node.status),
                            "max_retries": int((plan_snapshot.get("budget_snapshot") or {}).get("max_retries") or 2),
                        }
                        for node in plan.nodes.values()
                    ],
                    edges=[(edge.parent_id, edge.child_id) for edge in plan.edges],
                    event_type="team_plan_created",
                    event_payload=event_payload,
                    actor="team_runtime",
                )
                self.plan_workflows[key] = workflow.id
                for node_id, task in tasks.items():
                    self.plan_node_tasks[(owner_account_id, plan.team_session_id, node_id)] = task.id
                return

            workflow = store.create_workflow(
                session_id=plan.team_session_id,
                title=plan.goal,
                context=context,
            )
            self.plan_workflows[key] = workflow.id
            node_to_task: dict[str, str] = {}
            parent_map: dict[str, list[str]] = {}
            for edge in plan.edges:
                parent_map.setdefault(edge.child_id, []).append(edge.parent_id)
            for node in plan.nodes.values():
                parent_task_ids = [
                    node_to_task[parent_id]
                    for parent_id in parent_map.get(node.node_id, [])
                    if parent_id in node_to_task
                ]
                task = store.add_task(
                    workflow.id,
                    title=node.title,
                    detail=node.detail,
                    assignee=node.assignee,
                    parent_task_ids=parent_task_ids or None,
                    status=kanban_status(node.status),
                    auto_promote=False,
                )
                node_to_task[node.node_id] = task.id
                self.plan_node_tasks[(owner_account_id, plan.team_session_id, node.node_id)] = task.id
            store.add_event(
                workflow.id,
                "team_plan_created",
                actor="team_runtime",
                payload={**event_payload, "node_task_ids": node_to_task},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("TeamPlan 同步到 kanban store 失败 session=%s err=%s", plan.team_session_id, exc)

    def sync_node(self, plan: TeamPlan, node: TeamPlanNode, owner_account_id: str = "") -> None:
        store = self._store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        workflow_id = self.plan_workflows.get(key)
        task_id = self.plan_node_tasks.get((owner_account_id, plan.team_session_id, node.node_id))
        if not workflow_id or not task_id:
            return
        try:
            store.update_task_status(
                task_id,
                kanban_status(node.status),
                result_summary=node.result_summary or node.last_error or None,
                artifacts=node.artifact_refs or None,
                assignee=node.assignee,
            )
            if hasattr(store, "update_workflow_status"):
                store.update_workflow_status(workflow_id, plan.status)
            store.add_event(
                workflow_id,
                "team_node_updated",
                task_id=task_id,
                actor=node.assignee or "team_runtime",
                payload={
                    "team_plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "status": node.status,
                    "result_summary": node.result_summary,
                    "last_error": node.last_error,
                    "delegate_task_id": node.delegate_task_id,
                    "attempt_count": node.attempt_count,
                    "metadata": dict(node.metadata or {}),
                },
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "TeamPlan 节点同步到 kanban store 失败 session=%s node=%s",
                plan.team_session_id,
                node.node_id,
            )
