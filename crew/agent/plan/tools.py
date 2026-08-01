"""注册 plan 模式工具：enter_plan_mode / exit_plan_mode / todo。

工具 handler 只拿 args，故经 ``current_session_id`` ContextVar（执行器在每轮前设置，
见 ``crew/agent/executor/builtin.py``）取当前会话，再操作 ``PlanModeManager``。
"""

from __future__ import annotations

from typing import Any

from crew.core.runctx import current_owner_account_id, current_session_id
from crew.tools.registry import Registry, tool_error, tool_result

from .manager import PlanModeManager, plan_display_path, read_plan
from .prompts import (
    ENTER_PLAN_MODE_PROMPT,
    EXIT_PLAN_MODE_PROMPT,
)
from .todo import TODO_SCHEMA, todo_tool

# plan 模式下暴露给模型的只读工具白名单（+ 计划文件可写 + 退出/待办 + 澄清提问）。
# file_write 由 ToolRunner 的 plan 门控限制为「只能写计划文件」。
# ask_followup_question 复用全局已注册的交互工具（FollowupWaiter 异步挂起）
# AskUserQuestion：plan 模式下用它澄清需求，turn 以 ask_followup_question 或 exit_plan_mode 结束。
# terminal 保留在白名单内，但受 terminal_guard 只读/写门控约束；
# 后续如需更严格的 permission policy，再整体重构。
PLAN_MODE_TOOLS = [
    "file_read",
    "search_files",
    "terminal",
    "file_write",
    "exit_plan_mode",
    "todo",
    "ask_followup_question",
]


ENTER_PLAN_MODE_SCHEMA = {
    "name": "enter_plan_mode",
    "description": ENTER_PLAN_MODE_PROMPT,
    "parameters": {"type": "object", "properties": {}, "required": []},
}

EXIT_PLAN_MODE_SCHEMA = {
    "name": "exit_plan_mode",
    "description": EXIT_PLAN_MODE_PROMPT,
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register_plan_tools(registry: Registry, manager: PlanModeManager) -> None:
    """把 enter_plan_mode / exit_plan_mode / todo 注册到 registry（toolset='plan'）。"""

    def _handle_enter(args: dict[str, Any]) -> str:
        return tool_error(
            "enter_plan_mode 不能由模型主动调用。请用户通过 UI Plan 选择、WebSocket plan_enter/plan_active，"
            "或 CLI /plan 显式进入 Plan 模式。"
        )

    def _handle_exit(args: dict[str, Any]) -> str:
        sid = current_session_id.get()
        owner = current_owner_account_id.get()
        # exit_plan_mode 仅在 plan 模式激活时有效。审批通过后 plan 已退出，
        # 此时再调用（如任务收尾误触）不应重新置位 awaiting，否则会再次弹出 plan_review 卡片。
        if not manager.is_active(sid, owner_account_id=owner):
            return tool_error(
                "当前不在 plan 模式，无需请求审批。计划已被批准并执行，请直接给出最终回复。"
            )
        # 已在等待审批时，模型重复调 exit_plan_mode（弱模型常见）不应重新 submit_review——
        # 否则会覆盖 pending_review 并多耗轮次。直接提示等用户审批即可。
        if manager.is_awaiting_approval(sid, owner_account_id=owner):
            return tool_result(
                message="计划已提交，正在等待用户审批。请勿重复调用 exit_plan_mode，在用户答复前保持沉默。",
                plan_file=plan_display_path(sid, owner_account_id=owner),
            )
        # 统一走 submit_review：plan 非空则兜底落盘+awaiting 推审批卡，plan 为空则推「计划为空」提示卡。
        # 不再因 plan 文件为空就 tool_error 中断——那样模型会在文本里念「请审批」死循环而前端永无卡片。
        manager.submit_review(sid, owner_account_id=owner)
        plan_empty = not read_plan(sid, owner_account_id=owner)
        if plan_empty:
            return tool_error(
                f"计划文件为空。你必须先使用 file_write 将完整计划写入 "
                f"{plan_display_path(sid, owner_account_id=owner)}，然后才能调用 exit_plan_mode。"
                f"请立即停止探索，写入计划文件后再试。"
            )
        return tool_result(
            message="计划已提交，等待用户审批。在用户答复前请勿继续。",
            plan_file=plan_display_path(sid, owner_account_id=owner),
        )

    def _handle_todo(args: dict[str, Any]) -> str:
        sid = current_session_id.get()
        owner = current_owner_account_id.get()
        store = manager.todo_store(sid, owner_account_id=owner)
        result = todo_tool(todos=args.get("todos"), merge=args.get("merge", False), store=store)
        manager.mark_todo_used(sid, owner_account_id=owner)
        return result

    registry.register(
        name="enter_plan_mode",
        toolset="plan",
        schema=ENTER_PLAN_MODE_SCHEMA,
        handler=_handle_enter,
        emoji="📝",
        display_name="进入计划模式",
        ui_label_template="进入计划模式",
        always_load=True,
        search_hint="enter plan mode planning approval",
    )
    registry.register(
        name="exit_plan_mode",
        toolset="plan",
        schema=EXIT_PLAN_MODE_SCHEMA,
        handler=_handle_exit,
        emoji="✅",
        display_name="提交计划",
        ui_label_template="提交计划",
        always_load=True,
        search_hint="exit plan mode submit approval",
    )
    registry.register(
        name="todo",
        toolset="todo",
        schema=TODO_SCHEMA,
        handler=_handle_todo,
        emoji="📋",
        display_name="更新待办",
        ui_label_template="更新待办",
        always_load=True,
        search_hint="todo task checklist progress update",
    )
