"""对话级 Plan 模式子模块。

Plan 模式负责进入只读探索、形成设计、写入计划文件、等待审批并进入执行，
同时使用 todo 工具追踪计划进度。

- manager  PlanModeManager 状态机 + 计划文件读写
- todo      TodoStore（实现）
- prompts   enter/exit 工具描述 + 工作流指引
- tools     register_plan_tools：注册 enter_plan_mode / exit_plan_mode / todo
"""

from __future__ import annotations

from .manager import (
    PlanModeManager,
    PlanPhase,
    clear_plan_dir,
    plan_display_path,
    plan_path,
    plans_dir,
    read_plan,
    write_plan,
)
from .attachments import (
    PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS,
    PLAN_MODE_TURNS_BETWEEN_ATTACHMENTS,
    count_plan_attachments_since_last_exit,
    count_user_turns_since_last_plan_attachment,
    create_plan_attachment_message,
    get_plan_mode_attachment_messages,
)
from .prompts import (
    PLAN_APPROVED_CONTENT_MAX_CHARS,
    PLAN_APPROVED_REMINDER,
    PLAN_EXIT_REMINDER,
    PLAN_REENTRY_REMINDER,
    PLAN_WORKFLOW_INSTRUCTIONS,
    SPARSE_PLAN_WORKFLOW_INSTRUCTIONS,
    TODO_REMINDER,
    format_approved_plan_content,
)
from .todo import TodoStore
from .tools import PLAN_MODE_TOOLS, register_plan_tools

__all__ = [
    "PlanModeManager",
    "PlanPhase",
    "TodoStore",
    "register_plan_tools",
    "plan_path",
    "plan_display_path",
    "plans_dir",
    "clear_plan_dir",
    "read_plan",
    "write_plan",
    "PLAN_MODE_TOOLS",
    "PLAN_WORKFLOW_INSTRUCTIONS",
    "SPARSE_PLAN_WORKFLOW_INSTRUCTIONS",
    "PLAN_REENTRY_REMINDER",
    "PLAN_EXIT_REMINDER",
    "PLAN_APPROVED_REMINDER",
    "format_approved_plan_content",
    "PLAN_APPROVED_CONTENT_MAX_CHARS",
    "TODO_REMINDER",
    "PLAN_MODE_TURNS_BETWEEN_ATTACHMENTS",
    "PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS",
    "create_plan_attachment_message",
    "get_plan_mode_attachment_messages",
    "count_user_turns_since_last_plan_attachment",
    "count_plan_attachments_since_last_exit",
]
