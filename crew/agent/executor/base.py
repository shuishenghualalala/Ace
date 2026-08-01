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
    # 专用 Wiki Agent 关闭渐进披露，直接暴露其授权工具；普通会话中的 Wiki
    # 只读工具仍按 deferred_toolsets 规则延迟加载。
    disable_tool_search: bool = False


class AgentExecutor(ABC):
    """执行内核。消费 ExecutionContext，产出 ResponseChunk 流。"""

    name: str = "executor"

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        """运行一轮（含多步工具调用），流式产出 delta/tool/thinking/final/error 帧。"""
        raise NotImplementedError
        yield  # pragma: no cover  (标记为 async generator)
