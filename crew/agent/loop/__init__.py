"""Agent loop 的「鲁棒性 + 可控性」组件子包。

把让对话循环跑得稳、可干预的能力从主循环（``executor/builtin.py``）拆出来，
各自单文件、可独立单测：

  iteration_budget           预算计数（consume/refund/grace）
  tool_guardrails            工具防循环（失败/无进展 N 次拦截）
  tool_result_classification 工具结果成败判定
  control                    TurnControl：steer + 协作式 interrupt
  resilience                 空响应重试/截断续写/溢出检测/故障转移
  tool_runner                并行/串行工具执行
"""

from __future__ import annotations

from crew.agent.loop.control import TurnControl
from crew.agent.loop.iteration_budget import IterationBudget
from crew.agent.loop.resilience import (
    CONTINUATION_PROMPT,
    EMPTY_RETRY_NUDGE,
    ESCALATED_MAX_OUTPUT_TOKENS,
    STREAM_INTERRUPT_PROMPT,
    STREAM_INTERRUPT_STATUS_MESSAGE,
    TOOL_ARGUMENTS_RECOVERY_LIMIT,
    TOOL_ARGUMENTS_RECOVERY_PROMPT,
    has_truncated_tool_args,
    is_context_overflow,
    is_empty_response,
    is_stream_interrupt_recoverable,
    provider_chain,
    should_continue,
)
from crew.agent.loop.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
)
from crew.agent.loop.tool_dispatch_helpers import should_parallelize
from crew.agent.loop.tool_runner import ToolRunner

__all__ = [
    "IterationBudget",
    "TurnControl",
    "ToolCallGuardrailConfig",
    "ToolCallGuardrailController",
    "ToolRunner",
    "should_parallelize",
    "is_context_overflow",
    "is_empty_response",
    "is_stream_interrupt_recoverable",
    "should_continue",
    "provider_chain",
    "EMPTY_RETRY_NUDGE",
    "CONTINUATION_PROMPT",
    "ESCALATED_MAX_OUTPUT_TOKENS",
    "TOOL_ARGUMENTS_RECOVERY_LIMIT",
    "TOOL_ARGUMENTS_RECOVERY_PROMPT",
    "STREAM_INTERRUPT_PROMPT",
    "STREAM_INTERRUPT_STATUS_MESSAGE",
    "has_truncated_tool_args",
]
