"""出错自愈：让 loop 在「模型输出异常 / provider 出错」时自我修复，而非直接 final 或崩。

四件事，用于 ``conversation_loop.py`` 的相应机制（用小函数替代其分散在 4836 行里的
重试计数器与 1300 行的 ``error_classifier``，只取最高价值的判定）：

1. 空响应重试   —— 一轮既无文本也无工具调用 → 注入 nudge 重试（Crew ``_empty_content_retries``）。
2. 截断续写     —— ``finish_reason == "length"`` 且无工具调用 → 追加续写提示再跑一轮
                   （移植 Crew ``_get_continuation_prompt``）。
3. 上下文溢出   —— 对 provider 报错消息做子串匹配，命中则交由 ``ContextCompactor.force_compact``
                   兜底压缩后重试一次。
4. provider 故障转移 —— 见 ``provider_chain``：主 provider 失败时依次切备用。

本模块只做「纯判定 + 文案」，控制流（计数、重试、切换）留在 ``BuiltinExecutor``，便于单测。
"""

from __future__ import annotations

from typing import Sequence

from crew.core.errors import CONTEXT_OVERFLOW_MARKERS, CrewError, CrewErrorKind


# --------------------------------------------------------------------------- #
# 1. 空响应重试
# --------------------------------------------------------------------------- #
EMPTY_RETRY_NUDGE = (
    "（系统提示：上一步没有产生任何文本或工具调用。请直接给出回答，"
    "或调用合适的工具继续完成任务。）"
)


def is_empty_response(text: str, tool_calls: Sequence, reasoning: str = "") -> bool:
    """一轮的产出是否「空」：无可见文本、无工具调用、无推理内容。"""
    return not (text and text.strip()) and not tool_calls and not (reasoning and reasoning.strip())


# --------------------------------------------------------------------------- #
# 2. 截断续写
# --------------------------------------------------------------------------- #
CONTINUATION_PROMPT = (
    "（系统提示：上一条回复因长度上限被截断。请从被截断处继续，"
    "不要重复已经输出的内容。）"
)


def should_continue(finish_reason: str | None, tool_calls: Sequence) -> bool:
    """是否应触发续写：因长度截断且本轮没有发起工具调用。"""
    return is_max_tokens_finish(finish_reason) and not tool_calls


# 各家 provider 对「输出撞上长度上限」的 finish_reason 命名。
MAX_TOKENS_FINISH_REASONS = frozenset({"length", "max_tokens", "model_context_window_exceeded"})


def is_max_tokens_finish(finish_reason: str | None) -> bool:
    """本轮是否因输出长度上限被截断（stop_reason == length 一族）。"""
    return finish_reason in MAX_TOKENS_FINISH_REASONS


# --------------------------------------------------------------------------- #
# 2b. 工具调用被截断的 fail-closed 检测
# --------------------------------------------------------------------------- #
ESCALATED_MAX_OUTPUT_TOKENS = 64_000
TOOL_ARGUMENTS_RECOVERY_LIMIT = 3
TOOL_ARGUMENTS_RECOVERY_PROMPT = (
    "（系统提示：上一次输出触及长度上限，工具参数没有生成完整。请直接继续，不要道歉，"
    "不要复述已经完成的工作。如果仍需调用工具，请重新发起完整调用，并把剩余工作拆成更小的步骤。）"
)

# 现象：模型在生成 file_write 等工具的 arguments 时撞上 max_output_tokens，
#   arguments JSON 在 content 字符串中途断裂 → provider 的 json.loads 失败 →
#   走 _raw 兜底（openai_provider.py / anthropic_provider.py 的 flush 阶段），
#   最终交给 executor 的 ToolCall.arguments = {"_raw": "<半截 JSON>"}，
#   finish_reason="length"。若直接派发，handler 拿不到 path/content → 静默写空/失败。
# Executor 在同时持有 finish_reason 与归一化 ToolCall 的位置调用该检测，
# 命中后不执行工具；executor 先提高输出额度，再做有限次数的隐藏续写，耗尽后才报错。


def has_truncated_tool_args(tool_calls: Sequence, finish_reason: str | None) -> bool:
    """本轮工具调用是否被 max_output_tokens 截断。

    判据：finish_reason 指示长度截断（OpenAI 为 ``length``，Anthropic 为
    ``max_tokens``；上下文输出耗尽也走同一恢复路径），且存在某个 tool_call 的
    arguments 只剩 ``_raw`` 键（provider json.loads 失败的兜底标记）。两者同时
    命中才认定为截断，避免把模型正常产出的 ``_raw``（极少见）误判为截断。
    """
    if not is_max_tokens_finish(finish_reason):
        return False
    for tc in tool_calls or []:
        args = getattr(tc, "arguments", None)
        if isinstance(args, dict) and set(args.keys()) == {"_raw"}:
            return True
    return False


# --------------------------------------------------------------------------- #
# 3. 上下文溢出检测
# --------------------------------------------------------------------------- #
# 报错关键词清单收敛在 crew.core.errors，provider 分类（crew.providers.classify）
# 与这里的溢出检测共用同一份，避免两处漂移。


def is_context_overflow(exc: Exception) -> bool:
    """provider 报错是否属于「上下文超长」类——命中则可压缩后重试。"""
    msg = str(exc).lower()
    return any(marker in msg for marker in CONTEXT_OVERFLOW_MARKERS)


# --------------------------------------------------------------------------- #
# 4. provider 故障转移
# --------------------------------------------------------------------------- #
def provider_chain(primary, fallbacks: Sequence | None) -> list:
    """组装尝试顺序：[主 provider, *备用 provider]，去重保序、去空。"""
    chain: list = [primary]
    for fb in fallbacks or []:
        if fb is not None and fb not in chain:
            chain.append(fb)
    return chain


# --------------------------------------------------------------------------- #
# 5. 流式中断续写（Crew的 partial stream stub + continuation）
# --------------------------------------------------------------------------- #
STREAM_INTERRUPT_PROMPT = (
    "（系统提示：上一条回复因网络或模型响应中断而被截断。请从被截断处继续，"
    "不要重复已经输出的内容。如果之前正在准备工具调用，请重新发起。）"
)

STREAM_INTERRUPT_STATUS_MESSAGE = "模型响应中断，已保留已生成内容"

# 判定：异常是否属于"流式中途可续写"类型
# 续写要求异常可重试，且 kind 不属于鉴权/配额/取消等「重试无意义」类别。
STREAM_INTERRUPT_FATAL_KINDS = frozenset({
    CrewErrorKind.AUTH,
    CrewErrorKind.FORBIDDEN,
    CrewErrorKind.CANCELLED,
    CrewErrorKind.CONTEXT_WINDOW_EXCEEDED,
    CrewErrorKind.UNSUPPORTED_CAPABILITY,
    CrewErrorKind.CONFIG,
    CrewErrorKind.TOOL,
    CrewErrorKind.UNKNOWN,
})


def is_stream_interrupt_recoverable(exc: Exception) -> bool:
    """流式中断后是否可尝试续写（保留已 emit 文本再发一次请求）。

    CrewError 实例走类型化判定（is_retryable + kind）；裸异常（httpx / SDK
    异常未经 provider 包装直接冒泡）退回类名启发式兜底。
    """
    if isinstance(exc, CrewError):
        return exc.is_retryable() and exc.kind not in STREAM_INTERRUPT_FATAL_KINDS
    name = type(exc).__name__
    return name in (
        "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        "TimeoutException", "APITimeoutError", "APIConnectionError",
        "RemoteProtocolError", "ConnectError", "InternalServerError",
        "RateLimitError",
    )
