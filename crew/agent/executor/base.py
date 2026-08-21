"""执行内核的抽象与执行上下文。

会话编排器（SingleAgent）负责构建上下文、持久化、记忆、标题；
AgentExecutor 只消费 ExecutionContext，产出统一的 ResponseChunk 流。
抽象只活在 agent 模块内部，不进 core 契约（保持核心稳定）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from crew.agent.skills import SkillActivation
from crew.core.envelope import ResponseChunk
from crew.core.types import Message
from crew.tools.policy import ToolDisclosureMode

if False:  # 仅类型提示，避免运行期循环导入
    from crew.agent.loop.control import TurnControl


@dataclass
class ExecutionContext:
    """一次执行所需的全部输入。executor 各取所需。"""

    session_id: str
    request_id: str
    system_prompt: str                       # 已拼好的完整 system 文本
    messages: list[Message]                  # 历史(含本轮 user)；executor 原地 append
    query: str                               # 已规范化的本轮输入（含附件引用；外部 executor 用）
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 本轮附件只读快照
    params: dict[str, Any] = field(default_factory=dict)  # Envelope.params 的只读快照
    active_skills: tuple[SkillActivation, ...] = ()  # 本轮由用户显式激活的 Skill
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    # 本轮真实执行授权的工具名快照；与渐进披露后展示给模型的 schema 集不同。
    authorized_tool_names: frozenset[str] | None = None
    # SingleAgent sets this after applying its per-turn tool_filter.  Keep False
    # for low-level/external executor callers that historically use [] to mean
    # "schemas not supplied" rather than "no tools allowed".
    enforce_tool_scope: bool = False
    cwd: str | None = None
    max_iterations: int | None = None  # None=继承 executor 默认；0=无限
    # 本轮可控性句柄（steer / interrupt）；None 表示不接受外部干预
    control: "TurnControl | None" = None
    # 只控制“已授权工具如何向模型披露”，不改变授权范围。
    tool_disclosure_mode: ToolDisclosureMode = ToolDisclosureMode.PROGRESSIVE


@dataclass(frozen=True)
class FinalRequestView:
    """一次模型推理的统一、不可变请求快照。

    Runtime、token 计数、middleware 后的 Provider 调用和调试日志都应消费这个
    结构，避免各自重新拼装 messages/tools。字段使用 tuple，禁止装配完成后再
    增删顶层项；下一次 tool-loop step 应生成新的快照。
    """

    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...] | None = None
    max_output_tokens: int | None = None
    model: str = ""
    provider: str = ""
    base_url: str = ""
    provider_index: int = 0

    @classmethod
    def create(
        cls,
        messages: list[Message] | tuple[Message, ...],
        tools: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        *,
        max_output_tokens: int | None = None,
        model: str = "",
        provider: str = "",
        base_url: str = "",
        provider_index: int = 0,
    ) -> "FinalRequestView":
        return cls(
            messages=tuple(messages),
            tools=tuple(tools) if tools is not None else None,
            max_output_tokens=max_output_tokens,
            model=model,
            provider=provider,
            base_url=base_url,
            provider_index=provider_index,
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        fallback: "FinalRequestView",
        *,
        model: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        provider_index: int | None = None,
    ) -> "FinalRequestView":
        raw_messages = payload.get("messages", fallback.messages)
        messages = (
            raw_messages
            if isinstance(raw_messages, (list, tuple))
            else fallback.messages
        )
        raw_tools = payload.get("tools", fallback.tools)
        tools = (
            raw_tools
            if raw_tools is None or isinstance(raw_tools, (list, tuple))
            else fallback.tools
        )
        max_output_tokens = payload.get("max_tokens", fallback.max_output_tokens)
        return cls.create(
            list(messages),
            list(tools) if tools is not None else None,
            max_output_tokens=max_output_tokens,
            model=fallback.model if model is None else model,
            provider=fallback.provider if provider is None else provider,
            base_url=fallback.base_url if base_url is None else base_url,
            provider_index=fallback.provider_index if provider_index is None else provider_index,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": list(self.messages),
            "tools": list(self.tools) if self.tools is not None else None,
        }
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        return payload

    def with_messages(self, messages: list[Message]) -> "FinalRequestView":
        return FinalRequestView.create(
            messages,
            self.tools,
            max_output_tokens=self.max_output_tokens,
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            provider_index=self.provider_index,
        )

    def estimated_prompt_tokens(self) -> int:
        """按统一快照计算本地 prompt token；不再接受旁路 messages/tools。"""
        from crew.agent.compact.tokens import estimate_prompt_tokens

        return estimate_prompt_tokens(
            list(self.messages),
            list(self.tools) if self.tools is not None else None,
        )


class AgentExecutor(ABC):
    """执行内核。消费 ExecutionContext，产出 ResponseChunk 流。"""

    name: str = "executor"

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        """运行一轮（含多步工具调用），流式产出 delta/tool/thinking/final/error 帧。"""
        raise NotImplementedError
        yield  # pragma: no cover  (标记为 async generator)
