"""模块接口契约（ABC）。

每个业务模块实现这里定义的某个接口，模块之间只通过这些接口交互。
装配在 crew/app.py 用依赖注入完成。

【分工对照】
  LLMProvider   -> crew/providers/      (A)
  Tool/Registry -> crew/tools/          (B)
  Plugin        -> crew/plugins/        (B)
  Agent         -> crew/agent/          (A)
  SessionStore  -> crew/state/          (D)
  MemoryProvider-> crew/memory/         (D)
  Channel       -> crew/gateway/        (C)   含 MCP Server（对外暴露会话）
  TeamManager   -> crew/team/           (E)
  TaskManager   -> crew/tasks/          (E)
  Scheduler     -> crew/cron/           (E)   含 CronService/CronJobStore 定时任务引擎

  另：MCP Client（接外部 MCP server 当工具）-> crew/tools/mcp_client.py
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.types import ChatResponse, Message, StreamChunk, ToolCall, ToolOutput, ToolResult


# --------------------------------------------------------------------------- #
# Provider 层
# --------------------------------------------------------------------------- #
class LLMProvider(ABC):
    """LLM 适配器。把内核的 Message/工具 schema 翻译成具体厂商 API。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """一次（非流式）补全。返回归一化的 ChatResponse。

        ``max_tokens`` 仅用于本次调用临时覆盖 provider 默认值（如截断重试时加大输出上限）；
        为 None 时使用 provider 自身配置。
        """
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式补全，逐 token 返回增量文本。工具调用在流结束后以完整形式给出。

        ``max_tokens`` 仅用于本次调用临时覆盖 provider 默认值（如截断重试时加大输出上限）；
        为 None 时使用 provider 自身配置。
        """
        raise NotImplementedError
        yield  # pragma: no cover


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #
class Tool(ABC):
    """单个工具。子类实现 name/description/parameters/run。"""

    name: str = ""
    toolset: str = "default"
    description: str = ""
    display_name: str = ""
    ui_label_template: str = ""
    # JSON Schema（OpenAI function.parameters 形状）
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> str | ToolOutput:
        """执行工具，返回文本结果。失败抛 ToolError。"""
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        """转成 OpenAI tools[] 中的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry(ABC):
    """工具注册表，负责注册、查询、执行。"""

    @abstractmethod
    def register(self, tool: Tool | None = None, **kwargs: Any) -> None:
        """注册工具。推荐 Crew：

        register(name=..., toolset=..., schema=..., handler=...)
        """
        ...

    @abstractmethod
    def get(self, name: str) -> Tool: ...

    @abstractmethod
    def names(self) -> list[str]: ...

    def ui_meta(self, name: str) -> dict[str, str]:
        """返回仅用于前端展示的工具元数据；不进入 LLM tool schema。"""
        return {}

    @abstractmethod
    def list_schemas(
        self,
        only: list[str] | None = None,
        *,
        enabled_toolsets: list[str] | None = None,
        disabled_toolsets: list[str] | None = None,
        enabled_tools: list[str] | None = None,
        disabled_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回工具 schema 列表；only 指定则只返回子集（Team 角色用）。"""
        ...

    @abstractmethod
    async def execute(self, tool_call: ToolCall) -> ToolResult: ...


# --------------------------------------------------------------------------- #
# Agent 内核
# --------------------------------------------------------------------------- #
class Agent(ABC):
    """单智能体运行时。消费 Envelope，产出 ResponseChunk 流。"""

    @abstractmethod
    async def run(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """运行一轮对话（含多步工具调用），流式产出响应帧。"""
        raise NotImplementedError
        yield  # pragma: no cover  (标记为 async generator)


# --------------------------------------------------------------------------- #
# 会话 / 状态
# --------------------------------------------------------------------------- #
class SessionStore(ABC):
    """会话历史持久化。"""

    @abstractmethod
    def load(self, session_id: str, owner_account_id: str = "") -> list[Message]: ...

    @abstractmethod
    def append(self, session_id: str, messages: list[Message], owner_account_id: str = "") -> None: ...

    def append_idempotent(
        self,
        session_id: str,
        message: Message,
        owner_account_id: str = "",
    ) -> bool:
        """Append an externally identified message once.

        Persistent stores should override this with an atomic implementation.
        """
        if message.message_id and any(
            item.message_id == message.message_id
            for item in self.load(session_id, owner_account_id=owner_account_id)
        ):
            return False
        self.append(session_id, [message], owner_account_id=owner_account_id)
        return True

    @abstractmethod
    def save(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        owner_account_id: str = "",
        *,
        title_fallback: str | None = None,
    ) -> None:
        """整体覆盖保存。

        workspace_id 仅在首次创建（INSERT）时写入，后续 save 不覆盖——会话归属
        在创建时确定，避免每轮回写把归属冲掉。

        title_fallback：写入 DB 的标题 fallback。
          - None（默认）：取首条 user 消息截断，保持旧行为，兼容未传该参数的调用方。
          - ""：显式留空占位，等 set_title 写入摘要标题（enable_title=True 时使用，
                避免截断的用户原话抢占摘要标题）。
          - 其它字符串：用该值作为 fallback。
        """
        ...

    @abstractmethod
    def clear(self, session_id: str, owner_account_id: str = "") -> None: ...

    @abstractmethod
    def set_title(self, session_id: str, title: str, owner_account_id: str = "") -> None:
        """设置会话标题（覆盖默认的「首条 user 消息」标题）。"""
        ...

    @abstractmethod
    def set_archived(self, session_id: str, archived: bool, owner_account_id: str = "") -> None:
        """归档 / 取消归档会话。归档会话从主列表隐藏，可在归档视图查看与恢复。"""
        ...

    @abstractmethod
    def set_pinned(self, session_id: str, pinned: bool, owner_account_id: str = "") -> None:
        """置顶 / 取消置顶会话。置顶会话在主列表排序靠前。"""
        ...

    @abstractmethod
    def set_status(self, session_id: str, status: str, error: str = "", owner_account_id: str = "") -> None:
        """记录上一轮运行的 terminal 结果（completed / failed）及错误信息。"""
        ...

    @abstractmethod
    def get_status(self, session_id: str, owner_account_id: str = "") -> tuple[str, str]:
        """取上一轮运行结果 (last_status, last_error)；无记录返回 ("", "")。"""
        ...

    @abstractmethod
    def get_workspace_id(self, session_id: str, owner_account_id: str = "") -> str | None:
        """读取会话所属 workspace_id；会话不存在时返回 None（工作区隔离用）。"""
        ...

    @abstractmethod
    def list_sessions(
        self,
        workspace_id: str | None = None,
        owner_account_id: str = "",
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """列出会话摘要：[{session_id, title, message_count, updated_at, workspace_id, archived, pinned}]。
        默认按 pinned DESC, updated_at DESC 排序（置顶优先）。
        workspace_id 非空时只返回该工作空间的会话；include_archived=False（默认）时排除已归档会话。"""
        ...


# --------------------------------------------------------------------------- #
# 工作空间（会话的上层容器，承载共享指令）
# --------------------------------------------------------------------------- #
class WorkspaceStore(ABC):
    """工作空间持久化：分组会话 + 承载空间级指令。"""

    @abstractmethod
    def create(
        self,
        name: str,
        description: str = "",
        instructions: str = "",
        root_path: str = "",
        owner_account_id: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get(self, workspace_id: str, owner_account_id: str = "") -> dict[str, Any]: ...

    @abstractmethod
    def list(self, owner_account_id: str = "") -> list[dict[str, Any]]: ...

    @abstractmethod
    def update(self, workspace_id: str, owner_account_id: str = "", **fields: Any) -> dict[str, Any]:
        """更新 name/description/instructions/root_path 中的任意字段。"""
        ...

    @abstractmethod
    def delete(self, workspace_id: str, owner_account_id: str = "") -> None: ...


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #
class MemoryProvider(ABC):
    """可插拔记忆后端。"""

    @abstractmethod
    async def prefetch(self, session_id: str, query: str) -> str:
        """根据 query 取回相关记忆，拼成一段文本注入上下文。无则返回空串。"""
        ...

    @abstractmethod
    async def write(self, session_id: str, messages: list[Message]) -> None:
        """对话结束后写入/更新记忆。"""
        ...

    async def delete(self, session_id: str, owner_account_id: str | None = None) -> None:
        """删除某会话的记忆行。删会话清账时调用；默认 no-op，实现类应覆盖。"""
        return None


# --------------------------------------------------------------------------- #
# 插件（生命周期钩子）
# --------------------------------------------------------------------------- #
class Plugin(ABC):
    """生命周期钩子。所有钩子默认 no-op，子类按需重写。"""

    name: str = "plugin"

    async def pre_llm_call(self, session_id: str, messages: list[Message]) -> None:
        """每次调用 LLM 前。可改写 messages（原地）。"""

    async def pre_tool_call(self, tool_call: ToolCall) -> None:
        """工具执行前。可做权限校验（抛异常即拦截）。"""

    async def post_tool_call(self, tool_call: ToolCall, result: ToolResult) -> None:
        """工具执行后。可观测/审计。"""


# --------------------------------------------------------------------------- #
# 渠道（Gateway 接入）
# --------------------------------------------------------------------------- #
# 渠道收到外部消息后，调用这个回调把 Envelope 投递进内核，并异步拿回响应帧。
MessageHandler = Callable[[Envelope], AsyncIterator[ResponseChunk]]


class Channel(ABC):
    """一个接入渠道（web / cli_bridge / 未来的 IM 等）。"""

    name: str = "channel"

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """启动渠道，注入消息处理器。"""
        ...


# --------------------------------------------------------------------------- #
# 多智能体 Team
# --------------------------------------------------------------------------- #
class TeamManager(ABC):
    """Team 生命周期与协同。"""

    @abstractmethod
    async def interact(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """按 session 获取/创建 Team，投递用户输入，流式产出协同结果。"""
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def destroy(self, session_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# 任务管理
# --------------------------------------------------------------------------- #
class TaskManager(ABC):
    """任务生命周期：创建、派发、状态流转、查询。"""

    @abstractmethod
    def create(self, session_id: str, title: str, detail: str = "", assignee: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def assign(self, task_id: str, assignee: str) -> dict[str, Any]: ...

    @abstractmethod
    def update_status(self, task_id: str, status: str, result: str = "") -> dict[str, Any]: ...

    @abstractmethod
    def get(self, task_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list(self, session_id: str) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# 计划任务
# --------------------------------------------------------------------------- #
class Scheduler(ABC):
    """定时任务调度。"""

    @abstractmethod
    def add_job(self, name: str, interval_seconds: float, callback: Callable[[], Awaitable[None]]) -> str: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...
