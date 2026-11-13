"""看板工具：供 Dynamic Kanban 的 worker Agent 在运行时操作 board。"""

from __future__ import annotations

import json
from typing import Any

from crew.core.interfaces import Tool
from crew.dynamickanban.models import PlanDelta
from crew.dynamickanban.plan_graph import WorkflowGraphValidationError, validate_dag
from crew.dynamickanban.store import SQLiteKanbanStore


class _KanbanTool(Tool):
    name: str = ""
    toolset: str = "dynamic_kanban"
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    # 看板工具是 worker 的核心工具面（system prompt 直接引用），必须直接披露，
    # 不能藏在 tool_search 渐进披露后面
    should_defer: bool = False

    def __init__(
        self,
        store: SQLiteKanbanStore,
        workflow_id: str,
        actor: str = "worker",
        valid_roles: list[str] | None = None,
        on_plan_extension: Any | None = None,
    ) -> None:
        self.store = store
        self.workflow_id = workflow_id
        self.actor = actor
        self.valid_roles = valid_roles or []
        self.default_role = self.valid_roles[0] if self.valid_roles else ""
        # 可选回调：plan extension 应用到看板后调用 (delta, added_tasks)，
        # 供 Runtime 把新增任务写回 WorkflowDefinition，形成扩图闭环。
        self.on_plan_extension = on_plan_extension


class UpdateTaskStatusTool(_KanbanTool):
    name = "kanban_update_status"
    description = "更新当前或指定看板任务的状态和结果摘要。"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "status": {
                "type": "string",
                "enum": ["done", "failed", "blocked"],
                "description": "新状态",
            },
            "result_summary": {"type": "string", "description": "结果摘要"},
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "产出的工件路径列表",
            },
        },
        "required": ["task_id", "status"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        task_id = args["task_id"]
        status = args["status"]
        result_summary = args.get("result_summary", "")
        artifacts = args.get("artifacts")
        if not isinstance(artifacts, list):
            artifacts = [str(artifacts)] if artifacts else []
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            return json.dumps({"ok": False, "error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        if task.workflow_id != self.workflow_id:
            return json.dumps({"ok": False, "error": "任务不属于当前 workflow"}, ensure_ascii=False)
        self.store.update_task_status(
            task_id=task_id,
            status=status,
            result_summary=result_summary,
            artifacts=artifacts,
        )
        self.store.add_event(
            self.workflow_id,
            "task_status_updated",
            task_id=task_id,
            actor=self.actor,
            payload={"status": status, "result_summary": result_summary},
        )
        return json.dumps({"ok": True, "task_id": task_id, "status": status}, ensure_ascii=False)


class AddTaskTool(_KanbanTool):
    name = "kanban_add_task"
    description = "在看板中新增一个子任务，可指定负责人和父任务依赖。"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题"},
            "detail": {"type": "string", "description": "任务详情"},
            "assignee": {"type": "string", "description": "负责人角色"},
            "parent_task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "父任务 ID 列表",
            },
        },
        "required": ["title"],
    }

    def _norm_assignee(self, value: Any) -> str | None:
        role = str(value or "").strip()
        if not role:
            return self.default_role or None
        if not self.valid_roles:
            return role
        if role in self.valid_roles:
            return role
        lower = role.lower().replace("-", "_")
        for valid in self.valid_roles:
            if valid.lower().replace("-", "_") == lower:
                return valid
        return self.default_role or role

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.valid_roles:
            joined = ", ".join(self.valid_roles)
            self.description = (
                f"在看板中新增一个子任务。assignee 必须是团队成员角色之一：{joined}；"
                f"未指定或无效时默认归一化为 {self.default_role}。"
            )
            self.parameters["properties"]["assignee"]["description"] = (
                f"负责人角色，仅限：{joined}"
            )

    async def run(self, args: dict[str, Any]) -> str:
        assignee = self._norm_assignee(args.get("assignee"))
        parent_task_ids = args.get("parent_task_ids") or []
        for pid in parent_task_ids:
            try:
                pt = self.store.get_task(pid)
            except KeyError:
                return json.dumps({"ok": False, "error": f"父任务不存在: {pid}"}, ensure_ascii=False)
            if pt.workflow_id != self.workflow_id:
                return json.dumps({"ok": False, "error": f"父任务 {pid} 不属于当前 workflow"}, ensure_ascii=False)
        task = self.store.add_task(
            workflow_id=self.workflow_id,
            title=args["title"],
            detail=args.get("detail", ""),
            assignee=assignee,
            parent_task_ids=parent_task_ids,
        )
        self.store.add_event(
            self.workflow_id,
            "task_added",
            task_id=task.id,
            actor=self.actor,
            payload={"title": task.title, "assignee": task.assignee},
        )
        return json.dumps({"ok": True, "task_id": task.id, "assignee": task.assignee}, ensure_ascii=False)


class AddDependencyTool(_KanbanTool):
    name = "kanban_add_dependency"
    description = "在看板中新增一条任务依赖：parent_task_id -> child_task_id。"
    parameters = {
        "type": "object",
        "properties": {
            "parent_task_id": {"type": "string"},
            "child_task_id": {"type": "string"},
        },
        "required": ["parent_task_id", "child_task_id"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        parent_id = args["parent_task_id"]
        child_id = args["child_task_id"]
        try:
            parent = self.store.get_task(parent_id)
            child = self.store.get_task(child_id)
        except KeyError as exc:
            return json.dumps({"ok": False, "error": f"任务不存在: {exc}"}, ensure_ascii=False)
        if parent.workflow_id != self.workflow_id or child.workflow_id != self.workflow_id:
            return json.dumps({"ok": False, "error": "依赖任务不属于当前 workflow"}, ensure_ascii=False)
        self.store.add_dependency(parent_id, child_id)
        return json.dumps(
            {"ok": True, "parent": parent_id, "child": child_id},
            ensure_ascii=False,
        )


class ReportBlockerTool(_KanbanTool):
    name = "kanban_report_blocker"
    description = "报告当前任务遇到阻塞，需要 Planner 重新规划或人工介入。"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID，不传则指当前任务"},
            "reason": {"type": "string", "description": "阻塞原因"},
        },
        "required": ["reason"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        reason = args["reason"]
        if task_id:
            try:
                task = self.store.get_task(task_id)
            except KeyError:
                return json.dumps({"ok": False, "error": f"任务不存在: {task_id}"}, ensure_ascii=False)
            if task.workflow_id != self.workflow_id:
                return json.dumps({"ok": False, "error": "任务不属于当前 workflow"}, ensure_ascii=False)
            self.store.update_task_status(task_id=task_id, status="blocked")
            self.store.add_event(
                self.workflow_id,
                "task_blocked",
                task_id=task_id,
                actor=self.actor,
                payload={"reason": reason},
            )
        return json.dumps({"ok": True, "task_id": task_id, "reason": reason}, ensure_ascii=False)


class PlanNextTool(_KanbanTool):
    name = "kanban_plan_next"
    description = "规划型角色专用：提交下一阶段任务计划，动态扩展 DAG。"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "当前规划型任务 ID，新增任务默认依赖此任务",
            },
            "plan_json": {
                "type": "string",
                "description": "JSON 字符串，包含 add_tasks 和可选的 add_dependencies",
            },
        },
        "required": ["task_id", "plan_json"],
    }

    def _norm_assignee(self, value: Any) -> str | None:
        role = str(value or "").strip()
        if not role:
            return self.default_role or None
        if not self.valid_roles:
            return role
        if role in self.valid_roles:
            return role
        lower = role.lower().replace("-", "_")
        for valid in self.valid_roles:
            if valid.lower().replace("-", "_") == lower:
                return valid
        return self.default_role or role

    def _validate_extension(
        self,
        add_tasks: list[dict[str, Any]],
        dependencies: list[tuple[str, str]],
    ) -> None:
        """在写库前校验 Plan Extension 不会破坏当前 Board DAG。"""
        tasks = self.store.list_tasks(self.workflow_id)
        task_ids = [task.id for task in tasks]
        known_ids = set(task_ids)
        for task in add_tasks:
            for parent_id in task.get("parent_task_ids") or []:
                if parent_id not in known_ids:
                    raise WorkflowGraphValidationError(
                        f"计划扩展包含未知任务引用: {parent_id}"
                    )

        current_edges = [
            (parent_id, task.id)
            for task in tasks
            for parent_id in self.store.get_parent_task_ids(task.id)
        ]
        validate_dag(
            task_ids,
            [*current_edges, *dependencies],
            graph_name="计划扩展",
            node_name="任务",
        )

    async def run(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        plan_json = args.get("plan_json", "")
        if not task_id:
            return json.dumps({"ok": False, "error": "缺少 task_id"}, ensure_ascii=False)
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            return json.dumps({"ok": False, "error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        if task.workflow_id != self.workflow_id:
            return json.dumps({"ok": False, "error": "任务不属于当前 workflow"}, ensure_ascii=False)

        try:
            data = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"plan_json 解析失败: {exc}"}, ensure_ascii=False)
        if not isinstance(data, dict):
            return json.dumps({"ok": False, "error": "plan_json 必须是 JSON 对象"}, ensure_ascii=False)

        add_tasks: list[dict[str, Any]] = []
        for t in data.get("add_tasks") or []:
            if not isinstance(t, dict):
                continue
            parent_ids: list[str] = []
            for ref in t.get("parent_task_ids") or []:
                ref_str = str(ref).strip()
                if ref_str.upper() == "CURRENT_TASK_ID":
                    parent_ids.append(task_id)
                elif ref_str:
                    parent_ids.append(ref_str)
            if not parent_ids:
                parent_ids = [task_id]
            add_tasks.append(
                {
                    "title": str(t.get("title") or "子任务"),
                    "detail": str(t.get("detail") or t.get("title") or ""),
                    "assignee": self._norm_assignee(t.get("assignee")),
                    "parent_task_ids": parent_ids,
                    "status": str(t.get("status") or "pending"),
                    "max_retries": int(t.get("max_retries", 2)),
                }
            )

        deps: list[tuple[str, str]] = []
        for d in data.get("add_dependencies") or []:
            if isinstance(d, (list, tuple)) and len(d) >= 2:
                deps.append((str(d[0]), str(d[1])))

        delta = PlanDelta(
            add_tasks=add_tasks,
            add_dependencies=deps,
            note=str(data.get("note", "")),
        )
        try:
            self._validate_extension(add_tasks, deps)
        except WorkflowGraphValidationError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        added = self.store.apply_plan_extension(self.workflow_id, delta)
        definition_synced = False
        sync_error = ""
        if self.on_plan_extension is not None:
            try:
                self.on_plan_extension(delta, added)
                definition_synced = True
            except Exception as exc:  # noqa: BLE001 - 扩图写回 definition 失败不影响看板结果
                sync_error = str(exc)
        self.store.add_event(
            self.workflow_id,
            "plan_expanded",
            task_id=task_id,
            actor=self.actor,
            payload={"added_task_ids": [t.id for t in added]},
        )
        return json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "added_count": len(added),
                "added_task_ids": [t.id for t in added],
                "definition_synced": definition_synced,
                **({"definition_sync_error": sync_error} if sync_error else {}),
            },
            ensure_ascii=False,
        )


def create_kanban_registry(
    store: SQLiteKanbanStore,
    workflow_id: str,
    actor: str = "worker",
    valid_roles: list[str] | None = None,
    on_plan_extension: Any | None = None,
) -> dict[str, Tool]:
    """为该 workflow 的 worker 创建一组看板工具实例。"""
    tool_classes = [UpdateTaskStatusTool, AddTaskTool, AddDependencyTool, ReportBlockerTool, PlanNextTool]
    tools: dict[str, Tool] = {}
    for cls in tool_classes:
        tool = cls(store, workflow_id, actor, valid_roles=valid_roles, on_plan_extension=on_plan_extension)
        tools[tool.name] = tool
    return tools
