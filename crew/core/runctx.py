"""运行期上下文：用 contextvar 传递「当前会话」给工具层。

工具 handler 签名只有 args，拿不到会话信息。需要感知当前会话的工具（如 cron_create
默认投递回当前会话）从这里读。由执行器（BuiltinExecutor）在执行一轮前设置。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable, Coroutine

current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
# 用户当前可见的会话 id。sidechain/Team Leader 的工具仍按 current_session_id
# 隔离执行历史，但权限确认等 side-channel 交互必须投递到这个可见会话。
current_display_session_id: ContextVar[str] = ContextVar(
    "current_display_session_id", default=""
)
# team member 执行工具时的子会话 id（envelope.params.member_session_id）。
# delegate_task/run_agent 后台入队按此 key 隔离，使完成通知能回到发起 member 而非 team_session。
# 主 agent 路径为空 -> 回退 current_session_id，行为不变。
current_subagent_notify_session: ContextVar[str] = ContextVar(
    "current_subagent_notify_session", default=""
)
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="")
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")
current_parent_task_id: ContextVar[str] = ContextVar("current_parent_task_id", default="")
current_workspace_id: ContextVar[str] = ContextVar("current_workspace_id", default="default")
current_owner_account_id: ContextVar[str] = ContextVar("current_owner_account_id", default="")
current_agent_workdir: ContextVar[str] = ContextVar("current_agent_workdir", default="")
current_workspace_guard: ContextVar[dict[str, Any] | None] = ContextVar("current_workspace_guard", default=None)
current_agent_id: ContextVar[str] = ContextVar("current_agent_id", default="")
current_session_source: ContextVar[dict[str, Any] | None] = ContextVar("current_session_source", default=None)
# 当前用户回合显式携带的附件绝对路径。Wiki capture 工具以此做 turn 级 allowlist，
# 即使模型猜到 owner uploads 目录中的旧文件路径也不能读取。
current_attachment_paths: ContextVar[tuple[str, ...]] = ContextVar("current_attachment_paths", default=())
# 当前回合附件的 (落盘路径, 原始文件名)。路径 allowlist 仍由
# ``current_attachment_paths`` 独立维护；这里仅为需要保留用户文件名的工具提供元数据。
current_attachment_files: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "current_attachment_files",
    default=(),
)
# 当前会话的用户类型（external/internal）。子 agent 据此继承父权限上限，
# 避免外部受限用户经 delegate_task/run_agent 越权拿到 internal 工具。
current_user_type: ContextVar[str] = ContextVar("current_user_type", default="internal")

# 当前 Agent 最终生效模型的能力集合。子 Agent 使用 ``inherit`` 时必须继承这个
# 运行时结果，而不能重新猜测全局 active profile（父会话可能绑定了其它模型）。
current_model_capabilities: ContextVar[tuple[str, ...] | None] = ContextVar(
    "current_model_capabilities", default=None
)

# 当前 Agent 实际生效的 LLM Provider。子 Agent ``model=inherit`` 必须继承它：
# 父会话可能绑定 owner 级模型，而 app 级 self.provider 在无全局 Key 时是 FakeProvider。
# 子 Agent 只借用、不持有（关闭仍由父 Agent 负责）。
current_provider: ContextVar[Any | None] = ContextVar("current_provider", default=None)

# 当前 Agent 本轮最终授权工具快照。子 Agent 必须在此基础上继续收窄，
# 不能仅凭 user_type 重新计算，否则会绕过父 Agent 的 Expert/会话级限制。
current_authorized_tool_names: ContextVar[frozenset[str] | None] = ContextVar(
    "current_authorized_tool_names", default=None
)

# 当前 agent 生效的 skill 范围 (enabled, disabled)。主 agent 运行时写入，
# 供 delegate_task 子 agent 继承父级的真实 skill 范围，而非只看 access_control 基线。
# (None, None) 表示不限制。
current_skill_scope: ContextVar[tuple[Any, Any]] = ContextVar(
    "current_skill_scope", default=(None, None)
)

# 当前 agent 已展开的 skill package slug 集合。
# 供 build_skills_index_prompt 决定是否在 system prompt 中暴露 package 内部 skills。
current_active_skill_packages: ContextVar[set[str]] = ContextVar(
    "current_active_skill_packages", default=set()
)

# 工具执行过程中向前端流式发射进度的回调。
# 由 tool_runner 在执行单个工具前注入，工具 handler（如 terminal）在长任务中调用
# emit_tool_progress(text) 发增量；无 sink 时为 no-op。签名：async (text: str) -> None
ToolProgressFn = Callable[[str], Coroutine[Any, Any, None]]
current_tool_progress_fn: ContextVar[ToolProgressFn | None] = ContextVar(
    "current_tool_progress_fn", default=None
)


async def emit_tool_progress(text: str) -> None:
    """工具 handler 调用：发射一段进度文本。无 sink 或无文本时静默。"""
    if not text:
        return
    fn = current_tool_progress_fn.get()
    if fn is None:
        return
    try:
        await fn(text)
    except Exception:  # noqa: BLE001 - 进度发射失败不得影响工具执行
        pass

# 当前执行上下文下可用的「向前端推事件」函数；gateway 注入，CLI 等无 UI 场景为 None。
# 签名：async push_fn(session_id: str, chunk: dict) -> None
PushFn = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]
current_push_fn: ContextVar[PushFn | None] = ContextVar("current_push_fn", default=None)

# 当前 turn 在 TaskRuntime 中的任务 ID 与 runtime 实例。
# 长耗时工具（如 Dynamic Kanban workflow）可直接 touch_activity 保活，避免被 inactivity 超时取消。
current_task_runtime_id: ContextVar[str] = ContextVar("current_task_runtime_id", default="")
current_task_runtime: ContextVar[Any | None] = ContextVar("current_task_runtime", default=None)

# 当前回合的业务活动回调。由调度器注入并负责限流，工具和子任务只需报告实际进展。
TaskActivityFn = Callable[[dict[str, Any] | None], None]
current_task_activity_fn: ContextVar[TaskActivityFn | None] = ContextVar(
    "current_task_activity_fn", default=None
)


def touch_current_task_activity(progress: dict[str, Any] | None = None) -> None:
    """报告当前回合的真实业务活动；无运行时上下文时静默。"""
    fn = current_task_activity_fn.get()
    if fn is None:
        return
    try:
        fn(progress)
    except Exception:  # noqa: BLE001 - 活动上报失败不得影响工具执行
        pass
