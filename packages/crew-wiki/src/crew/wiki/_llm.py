"""Wiki 内部 LLM 调用辅助：流式优先 + 超时兜底。

部分 LLM 网关（如 minimax）对长生成的非流式请求会挂起直至客户端超时/504，
而流式请求正常返回。wiki 的编译/ lint / 摘要统一走这里的流式优先调用，
避免 ingest 卡在"LLM 分析"阶段几十分钟。
"""

from __future__ import annotations

import asyncio

from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.state.logging import get_logger

log = get_logger("wiki.llm")

# Wiki 分块只返回有界的知识单元，不应占用通用 Provider 的超长超时。
# 两分钟仍足够覆盖常规模型首字延迟，同时避免一个坏块阻塞整篇资料。
LLM_CALL_TIMEOUT = 120.0


def _is_capacity_error(exc: BaseException) -> bool:
    """识别不应在同一端点改走非流式重试的限流/容量错误。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        try:
            if int(status) in {429, 503}:
                return True
        except (TypeError, ValueError):
            pass
        message = str(current).lower()
        if (
            "error code: 429" in message
            or "error code: 503" in message
            or "http 429" in message
            or "http 503" in message
            or "并发已满" in message
            or "rate limit" in message
            or "capacity" in message
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _stream_text(
    provider: LLMProvider,
    messages: list[Message],
    *,
    max_tokens: int | None = None,
) -> str:
    parts: list[str] = []
    async for chunk in provider.stream_chat(
        messages,
        tools=None,
        max_tokens=max_tokens,
    ):
        if chunk.delta_text:
            parts.append(chunk.delta_text)
    return "".join(parts)


async def chat_text(
    provider: LLMProvider,
    messages: list[Message],
    *,
    timeout: float = LLM_CALL_TIMEOUT,
    max_tokens: int | None = None,
) -> str:
    """流式优先的文本补全；流式通道本身不可用时回退非流式 chat。

    流式传输中超时（已在出字但被掐断）时不回退——非流式在同一网关上只会
    更慢，直接抛给上层重试。429/503/并发已满同样不回退，因为同一端点的
    非流式请求不会获得额外容量。
    """
    try:
        return await asyncio.wait_for(
            _stream_text(provider, messages, max_tokens=max_tokens),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_capacity_error(exc):
            log.warning("wiki LLM 端点限流或并发已满，不向同一端点回退非流式: %s", exc)
            raise
        log.warning("wiki LLM 流式调用失败，回退非流式: %s", exc)
    resp = await asyncio.wait_for(
        provider.chat(messages, tools=None, max_tokens=max_tokens),
        timeout=timeout,
    )
    return resp.text
