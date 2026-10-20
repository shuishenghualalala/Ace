"""出站 ResponseChunk → WS JSON 帧转换（过滤、静默检测、错误分类）。"""

from __future__ import annotations

from typing import Any

from crew.core.envelope import ResponseChunk
from crew.core.errors import CrewError, CrewErrorKind, ProviderError, category_for_kind
from crew.features.response_filters import apply_text_filters, is_silent_reply


def _error_category_from_exception(exc: Exception) -> str:
    # 类型化错误优先读 kind：ProviderError 的 category 本身即 kind 的兼容别名；
    # 其余 CrewError 经统一映射取展示用 category。非类型化异常走文本兜底。
    if isinstance(exc, ProviderError):
        return exc.category
    if isinstance(exc, CrewError):
        return category_for_kind(exc.kind)
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "403" in msg or "forbidden" in msg:
        return "forbidden"
    if "401" in msg or "unauthorized" in msg:
        return "auth"
    return "unknown"


def should_skip_chunk(chunk: ResponseChunk) -> bool:
    """静默回复检测：空 body / NO_REPLY 不推帧。"""
    if chunk.kind == "final":
        return is_silent_reply(chunk.body.get("text"))
    if chunk.kind == "delta":
        text = chunk.body.get("text", "")
        # 仅跳过「纯空 delta」；流式中间空帧少见，final 才是主判断点
        return text == "" and chunk.is_final
    return False


def format_outbound_payload(
    chunk: ResponseChunk,
    *,
    session_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """将 ResponseChunk 转为 WS 出站 dict；返回 None 表示跳过该帧。"""
    if should_skip_chunk(chunk):
        return None

    ctx = dict(context or {})
    ctx.setdefault("session_id", session_id)

    body = dict(chunk.body or {})
    if chunk.kind in {"delta", "final", "thinking"} and isinstance(body.get("text"), str):
        body["text"] = apply_text_filters(body["text"], ctx)
        if chunk.kind == "delta" and chunk.sequence > 0:
            body.setdefault("delta_start", chunk.sequence)
            body.setdefault("delta_end", chunk.sequence)
    elif chunk.kind == "error" and isinstance(body.get("message"), str):
        body["message"] = apply_text_filters(body["message"], ctx)
        body.setdefault("category", body.get("category", "unknown"))
    elif chunk.kind == "status" and isinstance(body.get("message"), str):
        body["message"] = apply_text_filters(body["message"], ctx)

    if chunk.kind == "final" and is_silent_reply(body.get("text")):
        return None

    return {
        "kind": chunk.kind,
        "body": body,
        "is_final": chunk.is_final,
        "sequence": chunk.sequence,
        "request_id": chunk.request_id,
        "session_id": session_id,
    }


def enrich_error_chunk(chunk: ResponseChunk, exc: Exception | None = None) -> ResponseChunk:
    """为 error 帧补充 category 字段。

    category 来源优先级：显式 exc 的类型化分类 > body 里已有 kind code 反推 >
    "unknown"。body 中既有的 code/retryable/retry_delay 原样透传。
    """
    body = dict(chunk.body or {})
    if exc is not None:
        body["category"] = _error_category_from_exception(exc)
    elif "category" not in body:
        # 无 exc 时从 body 的 kind code 反推 category；code 缺失或非法值保持 "unknown"。
        code = body.get("code")
        category = "unknown"
        if code:
            try:
                category = category_for_kind(CrewErrorKind(str(code)))
            except ValueError:
                category = "unknown"
        body["category"] = category
    return ResponseChunk(
        request_id=chunk.request_id,
        kind=chunk.kind,
        body=body,
        sequence=chunk.sequence,
        is_final=chunk.is_final,
        status=chunk.status,
        ts=chunk.ts,
    )
