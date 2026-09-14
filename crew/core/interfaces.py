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
  NotificationCenter -> crew/notifications/   站内通知中心（存储 + 推送）

  另：MCP Client（接外部 MCP server 当工具）-> crew/tools/mcp_client.py
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol, runtime_checkable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.timeout_policy import DEFAULT_EXTERNAL_IDLE_SECONDS
from crew.core.types import ChatResponse, Message, StreamChunk, ToolCall, ToolOutput, ToolResult
from crew.security.models import AdditionalPermissionProfile


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
        response_format: dict[str, Any] | None = None,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
    ) -> ChatResponse:
        """一次（非流式）补全。返回归一化的 ChatResponse。

        ``max_tokens`` 仅用于本次调用临时覆盖 provider 默认值（如截断重试时加大输出上限）；
        为 None 时使用 provider 自身配置。

        ``response_format`` 和 ``reasoning_mode`` 是结构化短调用的可选能力；不支持的
        provider 可以忽略它们，调用方会在兼容层降级到普通 JSON 提示。

        ``reasoning_mode`` 取值：None = 模型默认行为；"off"/"disabled" = 关闭思考；
        其余为思考等级（minimal/low/medium/high/xhigh/max），由厂商档案
        （crew.providers.vendors）映射为各家专属参数，不支持的档位被忽略。

        ``purpose`` 是辅助调用的元数据标记（如 ``"compaction"`` 摘要、标题生成），
        不进请求体；provider 可把它写进调用 trace 以便审计与计费归类。
        """
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_mode: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式补全，逐 token 返回增量文本。工具调用在流结束后以完整形式给出。

        ``max_tokens`` 仅用于本次调用临时覆盖 provider 默认值（如截断重试时加大输出上限）；
        为 None 时使用 provider 自身配置。

        ``response_format`` 和 ``reasoning_mode`` 与 ``chat`` 保持相同的可选能力契约。
        """
        raise NotImplementedError
        yield  # pragma: no cover


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #
class ToolResultRetention(str, Enum):
    """工具结果在 Agent 上下文中的保留方式。

    这是工具自身的契约，不属于压缩器的工具名白名单。Feature 或插件注册工具时
    声明语义，压缩器只按语义处理；未知工具默认按 IMPORTANT 保护。
    """

    TEMPORARY = "temporary"
    RESOURCE = "resource"
    INSTRUCTION = "instruction"
    IMPORTANT = "important"


@dataclass(frozen=True)
class ToolResultPolicy:
    """一次具体工具调用的结果保留策略。"""

    retention: ToolResultRetention = ToolResultRetention.IMPORTANT
    identity: str = ""


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

    def result_policy(self, args: dict[str, Any]) -> ToolResultPolicy:
        """返回结果保留策略；第三方 Tool 未声明时安全地保留重要结果。"""
        return ToolResultPolicy()


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

    def result_policy(self, name: str, args: dict[str, Any]) -> ToolResultPolicy:
        """解析一次工具调用的结果保留策略；未知工具默认按重要结果保护。"""
        return ToolResultPolicy()

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
    def load(self, session_id: str, owner_account_id: str) -> list[Message]: ...

    @abstractmethod
    def append(self, session_id: str, messages: list[Message], owner_account_id: str) -> None: ...

    @abstractmethod
    def save(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        *,
        owner_account_id: str,
        title_fallback: str | None = None,
        last_prompt_tokens: int | None = None,
        last_prompt_tokens_source: str | None = None,
    ) -> None:
        """整体覆盖保存。

        workspace_id 仅在首次创建（INSERT）时写入，后续 save 不覆盖——会话归属
        在创建时确定，避免每轮回写把归属冲掉。

        title_fallback：写入 DB 的标题 fallback。
          - None（默认）：取首条 user 消息截断，保持旧行为，兼容未传该参数的调用方。
          - ""：显式留空占位，等 set_title 写入摘要标题（enable_title=True 时使用，
                避免截断的用户原话抢占摘要标题）。
          - 其它字符串：用该值作为 fallback。

        last_prompt_tokens：本轮 Provider 返回的真实 prompt token 数；None 表示
          Provider 没有返回可用 usage。last_prompt_tokens_source 可为 provider 或
          request_view，后者表示按本次实际发送视图（system/message/tools）计算。
        """
        ...

    async def load_async(self, session_id: str, *, owner_account_id: str) -> list[Message]:
        """异步读路径：同步实现的 store 默认在线程池执行，不阻塞事件循环。"""
        return await asyncio.to_thread(self.load, session_id, owner_account_id=owner_account_id)

    async def save_async(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        *,
        owner_account_id: str,
        title_fallback: str | None = None,
        last_prompt_tokens: int | None = None,
        last_prompt_tokens_source: str | None = None,
    ) -> None:
        """异步写路径：同步实现的 store 默认在线程池执行，不阻塞事件循环。"""
        await asyncio.to_thread(
            self.save,
            session_id,
            messages,
            workspace_id=workspace_id,
            owner_account_id=owner_account_id,
            title_fallback=title_fallback,
            last_prompt_tokens=last_prompt_tokens,
            last_prompt_tokens_source=last_prompt_tokens_source,
        )

    @abstractmethod
    def clear_prompt_usage(self, session_id: str, owner_account_id: str) -> None:
        """清除会话上一轮的 Provider prompt usage。"""
        ...

    @abstractmethod
    def clear(self, session_id: str, owner_account_id: str) -> None: ...

    @abstractmethod
    def set_title(self, session_id: str, title: str, owner_account_id: str) -> None:
        """设置会话标题（覆盖默认的「首条 user 消息」标题）。"""
        ...

    @abstractmethod
    def set_archived(self, session_id: str, archived: bool, owner_account_id: str) -> None:
        """归档 / 取消归档会话。归档会话从主列表隐藏，可在归档视图查看与恢复。"""
        ...

    @abstractmethod
    def set_pinned(self, session_id: str, pinned: bool, owner_account_id: str) -> None:
        """置顶 / 取消置顶会话。置顶会话在主列表排序靠前。"""
        ...

    @abstractmethod
    def set_status(self, session_id: str, status: str, owner_account_id: str, error: str = "") -> None:
        """记录上一轮运行的 terminal 结果（completed / failed）及错误信息。"""
        ...

    @abstractmethod
    def get_status(self, session_id: str, owner_account_id: str) -> tuple[str, str]:
        """取上一轮运行结果 (last_status, last_error)；无记录返回 ("", "")。"""
        ...

    @abstractmethod
    def get_workspace_id(self, session_id: str, owner_account_id: str) -> str | None:
        """读取会话所属 workspace_id；会话不存在时返回 None（工作区隔离用）。"""
        ...

    @abstractmethod
    def list_sessions(
        self,
        workspace_id: str | None = None,
        *,
        owner_account_id: str,
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
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get(self, workspace_id: str, owner_account_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list(self, owner_account_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def update(self, workspace_id: str, owner_account_id: str, **fields: Any) -> dict[str, Any]:
        """更新 name/description/instructions/root_path 中的任意字段。"""
        ...

    @abstractmethod
    def delete(self, workspace_id: str, owner_account_id: str) -> None: ...


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

    async def stop(self) -> None:
        """停止渠道，释放网络与任务资源。"""

    def bind_app(self, app: Any) -> None:
        """注入 CrewApp，供需要调用后端能力的渠道插件使用。"""

    async def send_to_target(
        self,
        target: str,
        text: str,
        origin: Any | None = None,
    ) -> bool:
        """投递 outbound 文本到指定目标。"""
        return False

    def status_detail(self) -> dict[str, Any]:
        """返回渠道运行态的扩展诊断信息。"""
        return {}

    def apply_config(self, config: Any) -> None:
        """热应用非连接类配置变更。"""


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
# 通知中心
# --------------------------------------------------------------------------- #
@dataclass
class Notification:
    """一条站内通知。payload 为跳转上下文（如 session_id / request_id），read_at=None 表示未读。"""

    owner_account_id: str
    source: str
    kind: str
    title: str
    body: str = ""
    payload: dict[str, Any] | None = None
    id: str = ""
    created_at: float = 0.0
    read_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "payload": self.payload,
            "created_at": self.created_at,
            "read_at": self.read_at,
        }


class NotificationCenter(ABC):
    """通知中心契约：各来源只调 publish，持久化/未读数/推送由实现统一负责。"""

    @abstractmethod
    def publish(self, notification: Notification) -> Notification: ...

    @abstractmethod
    def list(
        self,
        owner_account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> list[Notification]: ...

    @abstractmethod
    def unread_count(self, owner_account_id: str) -> int: ...

    @abstractmethod
    def mark_read(self, owner_account_id: str, notification_id: str) -> bool: ...

    @abstractmethod
    def mark_all_read(self, owner_account_id: str) -> int: ...

    @abstractmethod
    def mark_read_by_payload(self, source: str, key: str, owner_account_id: str) -> int:
        """把 payload 顶层任一值等于 key 的未读通知标记已读（如审批 request_id 被处理后自动已读）。"""
        ...

    @abstractmethod
    def clear(self, owner_account_id: str) -> int: ...


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


# --------------------------------------------------------------------------- #
# 外部 Runtime 执行契约
# --------------------------------------------------------------------------- #
# Executor（消费者）与 external 实现包（Provider 侧）共用的中性协议面：
# 错误契约、流事件/执行请求 DTO、Catalog 协议与模型归一纯函数。
# 实现包不得整体搬入 core；实现细节经同代 Service Lease / 注入解析。
# 这些名字历史上定义在 crew.agent.external.*，原位置保留迁移期薄 re-export，
# 待 crew-agent 与 crew-external-agents 正式拆包且消费方全部改用本模块后删除。


class AcpAdapterError(RuntimeError):
    """ACP 协议适配失败（executor 据此生成对话错误帧并标记会话绑定）。"""


class ExternalCliError(RuntimeError):
    """外部 CLI runtime 一次性执行失败。"""


class CodexAdapterError(RuntimeError):
    """Codex app-server 协议适配失败。"""


class RuntimeResumeRejected(RuntimeError):
    """Adapter rejected a native session/thread before current-turn work began."""


PermissionDecision = Literal["allow", "deny"]


@dataclass(frozen=True)
class AcpPermissionRequest:
    """Normalized inbound ACP permission request.

    Policy stays outside the protocol adapter.  The adapter only validates the
    runtime-advertised options and maps an allow/deny decision back to one of
    those exact option ids.
    """

    session_id: str
    tool_call: dict[str, Any]
    options: tuple[dict[str, Any], ...]
    raw_params: dict[str, Any]


@dataclass(frozen=True)
class RuntimeMcpServer:
    """Protocol-neutral, argv-only MCP server declaration."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RuntimeMcpServer":
        name = str(raw.get("name") or raw.get("id") or "").strip()
        command = str(raw.get("command") or "").strip()
        if not name or not command:
            raise ValueError("Runtime MCP server 必须包含 name 和 command")
        raw_args = raw.get("args")
        args = tuple(str(item) for item in raw_args) if isinstance(raw_args, list) else ()
        raw_env = raw.get("env")
        env: list[tuple[str, str]] = []
        if isinstance(raw_env, dict):
            env.extend((str(key), str(value)) for key, value in raw_env.items())
        elif isinstance(raw_env, list):
            for item in raw_env:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("name") or "").strip()
                if key:
                    env.append((key, str(item.get("value") or "")))
        return cls(name=name, command=command, args=tuple(args), env=tuple(env))

    def stdio_config(self, *, env_as_list: bool = False) -> dict[str, Any]:
        config: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
        }
        if self.env:
            config["env"] = (
                [{"name": key, "value": value} for key, value in self.env]
                if env_as_list
                else {key: value for key, value in self.env}
            )
        return config


@dataclass
class ExternalToolEvent:
    name: str
    phase: str
    detail: str = ""
    tool_call_id: str = ""
    args: str = ""


@dataclass
class ExternalStreamEvent:
    kind: str
    text: str = ""
    tool: ExternalToolEvent | None = None
    session_id: str = ""
    session_resumed: bool = False
    session_reset: bool = False
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class RuntimeExecutionRequest:
    executable_path: str
    provider: str
    prompt: str
    model: str = ""
    cwd: str = "."
    system_prompt: str = ""
    launch_args: list[str] = field(default_factory=list)
    custom_args: list[str] = field(default_factory=list)
    custom_env: dict[str, str] = field(default_factory=dict)
    credential_home_paths: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    mcp_servers: list[RuntimeMcpServer] = field(default_factory=list)
    additional_permissions: AdditionalPermissionProfile = field(
        default_factory=AdditionalPermissionProfile
    )
    dynamic_tools: list[dict[str, Any]] = field(default_factory=list)
    dynamic_tool_handler: Any = None
    resume_session_id: str = ""
    timeout: float = DEFAULT_EXTERNAL_IDLE_SECONDS
    # Absolute monotonic deadline for the whole external turn.  The boolean
    # distinguishes an explicitly unlimited policy (deadline=None) from old
    # direct adapter callers that still expect their legacy watchdog.
    hard_deadline: float | None = None
    hard_timeout_enabled: bool = False
    permission_handler: Any = None
    # Adapter identity is separate from provider identity (for example a
    # provider may switch between ACP and Codex app-server implementations).
    adapter_id: str = ""

    def __post_init__(self) -> None:
        self.mcp_servers = [
            item if isinstance(item, RuntimeMcpServer) else RuntimeMcpServer.from_mapping(item)
            for item in self.mcp_servers
        ]


@dataclass
class ExternalCliConfig:
    """外部 CLI runtime 一次性执行的请求 DTO（实现方为 external 包的 CLI runner）。"""

    provider: str
    executable_path: str
    prompt: str
    model: str = ""
    cwd: str = "."
    system_prompt: str = ""
    custom_args: list[str] = field(default_factory=list)
    custom_env: dict[str, str] = field(default_factory=dict)
    credential_home_paths: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    # ``None`` means no wall-clock hard deadline; the caller may still impose
    # an idle deadline at the runtime-adapter layer.
    timeout: float | None = DEFAULT_EXTERNAL_IDLE_SECONDS


@dataclass(frozen=True)
class RuntimeModelProfile:
    id: str
    label: str
    provider: str = ""
    default: bool = False
    capabilities: tuple[str, ...] = ()
    thinking_levels: tuple[str, ...] = ()
    context_window: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label or self.id,
            "provider": self.provider,
            "default": self.default,
            "capabilities": list(self.capabilities),
            "thinking_levels": list(self.thinking_levels),
        }
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        return payload


def normalize_runtime_models(raw: Any) -> list[RuntimeModelProfile]:
    """Normalize current structured models and legacy string catalogs."""

    if not isinstance(raw, list):
        return []
    result: list[RuntimeModelProfile] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            model_id = entry.strip()
            payload: dict[str, Any] = {}
        elif isinstance(entry, dict):
            model_id = str(entry.get("id") or entry.get("model_id") or entry.get("modelId") or "").strip()
            payload = entry
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        capabilities = payload.get("capabilities") or []
        thinking = payload.get("thinking_levels") or payload.get("thinkingLevels") or []
        raw_context_window = (
            payload.get("context_window")
            if payload.get("context_window") is not None
            else payload.get("contextWindow")
            if payload.get("contextWindow") is not None
            else payload.get("max_context_tokens")
        )
        try:
            context_window = int(raw_context_window) if raw_context_window is not None else None
        except (TypeError, ValueError):
            context_window = None
        if context_window is not None and context_window <= 0:
            context_window = None
        result.append(RuntimeModelProfile(
            id=model_id,
            label=str(payload.get("label") or payload.get("name") or model_id).strip() or model_id,
            provider=str(payload.get("provider") or "").strip(),
            default=bool(payload.get("default")),
            capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
            if isinstance(capabilities, list) else (),
            thinking_levels=tuple(str(item).strip() for item in thinking if str(item).strip())
            if isinstance(thinking, list) else (),
            context_window=context_window,
        ))
    return result


def runtime_model(runtime: dict[str, Any] | None, model_id: str) -> RuntimeModelProfile | None:
    metadata = runtime.get("metadata") if isinstance(runtime, dict) else None
    models = normalize_runtime_models(metadata.get("models") if isinstance(metadata, dict) else None)
    wanted = canonical_runtime_model_id(runtime, model_id)
    if not wanted:
        default_id = str(metadata.get("default_model_id") or "").strip() if isinstance(metadata, dict) else ""
        wanted = default_id
    return next((model for model in models if model.id == wanted), None)


def canonical_runtime_model_id(runtime: dict[str, Any] | None, model_id: str) -> str:
    """Resolve an adapter-declared legacy model id without provider branching."""

    wanted = str(model_id or "").strip()
    return runtime_model_migrations(runtime).get(wanted, wanted) if wanted else ""


def runtime_model_migrations(runtime: dict[str, Any] | None) -> dict[str, str]:
    """Return only adapter migrations whose targets exist in the current catalog."""

    metadata = runtime.get("metadata") if isinstance(runtime, dict) else None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("model_migrations"), dict):
        return {}
    known_ids = {model.id for model in normalize_runtime_models(metadata.get("models"))}
    return {
        str(source).strip(): str(target).strip()
        for source, target in metadata["model_migrations"].items()
        if str(source).strip() and str(target).strip() in known_ids
    }


@runtime_checkable
class ExternalAgentCatalog(Protocol):
    """External Runtime, Agent, Profile, observation and session persistence."""

    def upsert_runtime(self, runtime: dict[str, Any]) -> dict[str, Any]: ...

    def sync_runtimes(self, runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def list_runtimes(self) -> list[dict[str, Any]]: ...

    def get_runtime(self, runtime_id: str) -> dict[str, Any]: ...

    def delete_runtime(self, runtime_id: str) -> None: ...

    def create_agent(
        self,
        *,
        owner_account_id: str,
        name: str,
        runtime_id: str,
        model: str = "",
        system_prompt: str = "",
        custom_args: list[str] | None = None,
        custom_env: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def get_or_create_managed_agent(
        self,
        *,
        owner_account_id: str,
        managed_kind: str,
        managed_key: str,
        name: str,
        runtime_id: str,
        model: str = "",
        system_prompt: str = "",
    ) -> dict[str, Any]: ...

    def list_agents(
        self,
        *,
        owner_account_id: str,
        include_managed: bool = True,
    ) -> list[dict[str, Any]]: ...

    def get_agent(self, agent_id: str, *, owner_account_id: str) -> dict[str, Any]: ...

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None: ...

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def refresh_agent_profile(
        self,
        agent_id: str,
        *,
        runtime: dict[str, Any] | None = None,
        owner_account_id: str,
    ) -> dict[str, Any]: ...

    def resolve_agent_profile(
        self,
        agent_id: str,
        model_id: str,
        *,
        owner_account_id: str,
    ) -> dict[str, Any]: ...

    def record_agent_profile_observation(
        self,
        *,
        owner_account_id: str,
        external_agent_id: str,
        source_run_id: str,
        source_node_id: str,
        source_attempt_id: str,
        capabilities: list[str],
        assessment_source: str,
        outcome: str,
        quality_weight: float,
        failure_kind: str = "",
        observed_at: str | None = None,
        runtime_id: str = "",
        model_id: str = "",
        model_fingerprint: str = "",
        model_binding_source: str = "",
    ) -> dict[str, Any]: ...

    def list_agent_profile_observations(
        self,
        external_agent_id: str,
        *,
        owner_account_id: str,
    ) -> list[dict[str, Any]]: ...

    def get_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> dict[str, Any] | None: ...

    def save_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        native_session_id: str,
        cwd: str = "",
        session_profile: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]: ...

    def delete_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> None: ...

    def delete_runtime_bindings_for_session(
        self,
        crew_session_id: str,
        *,
        owner_account_id: str,
    ) -> int: ...
