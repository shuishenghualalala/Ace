"""LLM Provider 层。当前实现 OpenAI 兼容与 Anthropic Messages 接口。

扩展点：新增厂商只需新建一个实现 LLMProvider 的类，在 app.py 注册即可。

stream_aux 是本层的统一辅助调用入口：标题、路由、摘要、wiki、看板等旁路
LLM 请求全部经它发起，与主对话共用同一个流式管线（内部聚合流为最终结果），
并统一携带 purpose 标记（遥测/审计区分）、超时与重试。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from crew.core.errors import ProviderError
from crew.state.logging import llm_trace

if TYPE_CHECKING:
    from crew.core.interfaces import LLMProvider
    from crew.core.types import Message

from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider

__all__ = ["AnthropicProvider", "AuxResult", "AuxPurpose", "OpenAIProvider", "stream_aux"]

log = logging.getLogger("crew.providers.aux")

#: 辅助调用的 purpose 分类：遥测/审计按此区分旁路请求与主对话。
#: "compaction" 预留给压缩摘要（crew.agent.compact）接入。
AuxPurpose = Literal[
    "session-title",
    "team-turn-decision",
    "team-status-summary",
    "team-planning",
    "workflow-definition",
    "workflow-repair",
    "workflow-summary",
    "skill-metadata",
    "wiki-completion",
    "preference-extraction",
    "formation-audit",
    "compaction",
]

#: 辅助调用默认整体超时（秒）：旁路请求不允许无限挂起。
DEFAULT_AUX_TIMEOUT = 30.0

#: 重试退避基数（秒）：第 n 次重试前等待 min(0.25 * n, 1.0)。
_RETRY_BACKOFF_BASE = 0.25
_RETRY_BACKOFF_CAP = 1.0


@dataclass(frozen=True)
class AuxResult:
    """stream_aux 的聚合结果：与 ChatResponse 同构的文本/用量视图。"""

    text: str
    finish_reason: str | None = None
    reasoning_content: str = ""
    usage: dict[str, int] = field(default_factory=dict)


async def _collect(
    provider: LLMProvider,
    messages: list[Message],
    *,
    max_tokens: int | None,
    response_format: dict[str, Any] | None,
    reasoning_mode: str | None,
) -> AuxResult:
    kwargs: dict[str, Any] = {
        "tools": None,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "reasoning_mode": reasoning_mode,
    }
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, int] = {}
    finish_reason: str | None = None
    while True:
        text_parts.clear()
        reasoning_parts.clear()
        usage = {}
        finish_reason = None
        try:
            async for chunk in provider.stream_chat(messages, **kwargs):
                if chunk.delta_text:
                    text_parts.append(chunk.delta_text)
                if chunk.reasoning_content:
                    reasoning_parts.append(chunk.reasoning_content)
                if chunk.done:
                    finish_reason = chunk.finish_reason
                    if chunk.usage:
                        usage = dict(chunk.usage)
            break
        except TypeError as exc:
            # 窄签名 provider（测试桩/外部适配器）不认某个 kwarg 时，按名剔除后原样重试。
            message = str(exc)
            dropped = next(
                (name for name in kwargs if name in message),
                None,
            )
            if dropped is None:
                raise
            kwargs.pop(dropped)
    return AuxResult(
        text="".join(text_parts),
        finish_reason=finish_reason,
        reasoning_content="".join(reasoning_parts),
        usage=usage,
    )


async def stream_aux(
    provider: LLMProvider,
    messages: list[Message],
    *,
    purpose: AuxPurpose,
    timeout: float = DEFAULT_AUX_TIMEOUT,
    max_tokens: int | None = None,
    retry: int = 1,
    response_format: dict[str, Any] | None = None,
    reasoning_mode: str | None = None,
) -> AuxResult:
    """发起一次带 purpose 标记的辅助 LLM 调用，内部走流式管线并聚合结果。

    契约：
    - 与主对话同一个 stream_chat 管线，只是聚合为最终结果返回；
    - ``timeout`` 作用于单次尝试的整体耗时（含首 token 等待），超时抛 ``TimeoutError``；
    - ``retry`` 为额外重试次数，仅对瞬时错误（ProviderError.retryable / 超时）生效，
      重试前做线性退避；调用方自带重试循环时应传 ``retry=0``；
    - 每次尝试写 llm_trace（带 purpose），每次调用写一条计量日志（purpose、
      耗时、usage、尝试次数），供遥测/审计按 purpose 区分辅助调用。
    """
    attempts = max(0, int(retry)) + 1
    started = time.perf_counter()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        model = getattr(provider, "model", "")
        llm_trace("aux_request", {
            "purpose": purpose,
            "model": model,
            "attempt": attempt,
            "max_tokens": max_tokens,
        })
        try:
            result = await asyncio.wait_for(
                _collect(
                    provider,
                    messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    reasoning_mode=reasoning_mode,
                ),
                timeout=max(0.2, float(timeout)),
            )
        except TimeoutError as exc:
            last_exc = exc
            log.warning("辅助 LLM 调用超时 purpose=%s attempt=%d/%d", purpose, attempt, attempts)
        except ProviderError as exc:
            last_exc = exc
            if not exc.retryable:
                raise
            log.warning("辅助 LLM 调用失败 purpose=%s attempt=%d/%d err=%s", purpose, attempt, attempts, exc)
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            llm_trace("aux_response", {
                "purpose": purpose,
                "model": model,
                "attempt": attempt,
                "elapsed_ms": elapsed_ms,
                "finish_reason": result.finish_reason,
                "usage": result.usage,
            })
            log.info(
                "aux_llm purpose=%s model=%s elapsed_ms=%d attempts=%d finish=%s usage=%s",
                purpose, model, elapsed_ms, attempt, result.finish_reason, result.usage or "-",
            )
            return result
        if attempt < attempts:
            await asyncio.sleep(min(_RETRY_BACKOFF_BASE * attempt, _RETRY_BACKOFF_CAP))
    assert last_exc is not None
    raise last_exc
