"""Agent-facing tools for the unified task runtime."""

from __future__ import annotations

import json
from typing import Any

from crew.core.runctx import current_owner_account_id


def register_task_tools(registry: Any, runtime: Any) -> None:
    async def _get(args: dict[str, Any]) -> str:
        owner = current_owner_account_id.get()
        return json.dumps(runtime.get(str(args.get("task_id") or ""), owner_account_id=owner), ensure_ascii=False)

    async def _wait(args: dict[str, Any]) -> str:
        owner = current_owner_account_id.get()
        return json.dumps(
            await runtime.wait(str(args.get("task_id") or ""), timeout=args.get("timeout"), owner_account_id=owner),
            ensure_ascii=False,
        )

    async def _cancel(args: dict[str, Any]) -> str:
        owner = current_owner_account_id.get()
        return json.dumps(
            await runtime.cancel(
                str(args.get("task_id") or ""),
                reason=str(args.get("reason") or "用户取消"),
                owner_account_id=owner,
            ),
            ensure_ascii=False,
        )

    async def _list(args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "tasks": runtime.list_tasks(
                    session_id=str(args.get("session_id") or "") or None,
                    status=str(args.get("status") or "") or None,
                    limit=int(args.get("limit") or 100),
                    owner_account_id=current_owner_account_id.get(),
                )
            },
            ensure_ascii=False,
        )

    registry.register(
        name="task_get",
        toolset="tasks",
        schema={
            "name": "task_get",
            "description": "查询统一后台任务状态与结果",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        handler=_get,
        is_async=True,
        display_name="查询任务",
        ui_label_template="查询任务 {task_id}",
        should_defer=True,
        search_hint="task get status result background runtime",
    )
    registry.register(
        name="task_wait",
        toolset="tasks",
        schema={
            "name": "task_wait",
            "description": "等待任务结束；等待超时不会终止实际任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "timeout": {"type": "number", "minimum": 0},
                },
                "required": ["task_id"],
            },
        },
        handler=_wait,
        is_async=True,
        display_name="等待任务",
        ui_label_template="等待任务 {task_id}",
        should_defer=True,
        search_hint="task wait background runtime completion timeout",
    )
    registry.register(
        name="task_cancel",
        toolset="tasks",
        schema={
            "name": "task_cancel",
            "description": "取消一个运行中的统一后台任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
        handler=_cancel,
        is_async=True,
        display_name="取消任务",
        ui_label_template="取消任务 {task_id}",
        should_defer=True,
        search_hint="task cancel stop background runtime",
    )
    registry.register(
        name="task_list",
        toolset="tasks",
        schema={
            "name": "task_list",
            "description": "列出统一任务，可按会话和状态过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            },
        },
        handler=_list,
        is_async=True,
        display_name="列出任务",
        ui_label_template="列出任务",
        should_defer=True,
        search_hint="task list background runtime status session",
    )
