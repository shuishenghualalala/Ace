"""provider 异常统一分类：把 SDK / httpx 异常解析成 kind/status/retryable/retry_delay。

openai 与 anthropic 两个 provider 共用这一份判定（此前各自维护一份类名启发式，
同一错误两家的结论可能不同）。判定顺序：httpx 原生异常 isinstance → __cause__
递归 → 消息文本 → 类名 + HTTP status。Retry-After 优先取响应头（仅支持秒数
格式），body 的 retry_delay 字段兜底；解析失败静默回落 None。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from crew.core.errors import CONTEXT_OVERFLOW_MARKERS, CrewError, CrewErrorKind, category_for_kind

# 各家 provider 在「配额耗尽」时返回的典型报错关键词（小写匹配）。
# 先于 429 状态映射检查：同一 429 可能是限流也可能是欠费，语义不同。
_QUOTA_MESSAGES = ("quota", "insufficient balance", "billing", "credit", "余额", "欠费")

_TIMEOUT_MESSAGES = ("read timed out", "timed out", "timeout")
_STREAM_MESSAGES = ("peer closed", "incomplete chunked read", "connection", "read error", "remote protocol")

# 历史可重试类名白名单：SDK 把底层 httpx 断连/超时包装后常以这些名字出现。
_RETRYABLE_NAMES = frozenset({
    "RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError",
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "RemoteProtocolError", "ConnectError",
})

# 明确不可重试的 kind：重试这类错误要么无意义（鉴权/配额/上下文超长），
# 要么会加重症状（服务端过载）。
_NON_RETRYABLE_KINDS = frozenset({
    CrewErrorKind.QUOTA_EXCEEDED,
    CrewErrorKind.AUTH,
    CrewErrorKind.FORBIDDEN,
    CrewErrorKind.CANCELLED,
    CrewErrorKind.CONTEXT_WINDOW_EXCEEDED,
    CrewErrorKind.UNSUPPORTED_CAPABILITY,
    CrewErrorKind.CONFIG,
    CrewErrorKind.TOOL,
    CrewErrorKind.SERVER_OVERLOADED,
})


@dataclass(frozen=True)
class ErrorClassification:
    """provider 异常的统一分类结果，供包装成 ProviderError 时携带。"""

    kind: CrewErrorKind
    status: int | None
    retryable: bool
    retry_delay: float | None
    category: str


def classify_provider_error(exc: Exception, *, status: int | None = None) -> ErrorClassification:
    """把任意 provider 调用异常分类为结构化的 kind/status/retryable/retry_delay。

    status 显式传入时优先（如 anthropic 从 HTTPStatusError.response 里取），
    否则读异常的 status_code 属性；都不是则为 None（裸连接错误）。
    """
    resolved_status = _resolve_status(exc, status)
    kind = _kind_for(exc, resolved_status)
    return ErrorClassification(
        kind=kind,
        status=resolved_status,
        retryable=_retryable_for(exc, kind, resolved_status),
        retry_delay=_retry_delay_for(exc, resolved_status),
        category=category_for_kind(kind),
    )


def _resolve_status(exc: Exception, status: int | None) -> int | None:
    if isinstance(status, int):
        return status
    attr_status = getattr(exc, "status_code", None)
    return attr_status if isinstance(attr_status, int) else None


def _kind_for(exc: Exception, status: int | None) -> CrewErrorKind:
    # httpx 原生异常优先 isinstance：SDK 流式消费时常让底层异常原样冒泡，
    # 类名字符串匹配会漏判。
    if isinstance(exc, httpx.TimeoutException):
        return CrewErrorKind.TIMEOUT
    if isinstance(exc, httpx.TransportError):  # 含 RemoteProtocolError / ReadError / ConnectError 等
        return CrewErrorKind.STREAM
    # SDK 常把底层 httpx 断连/超时包成 APIError，原异常挂在 __cause__ 上。
    cause = exc.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return CrewErrorKind.TIMEOUT
    if isinstance(cause, httpx.TransportError):
        return CrewErrorKind.STREAM
    msg = str(exc).lower()
    if any(marker in msg for marker in _TIMEOUT_MESSAGES):
        return CrewErrorKind.TIMEOUT
    if any(marker in msg for marker in _STREAM_MESSAGES):
        return CrewErrorKind.STREAM
    name = type(exc).__name__
    if status == 401 or name == "AuthenticationError":
        return CrewErrorKind.AUTH
    if status == 403 or name == "PermissionDeniedError":
        return CrewErrorKind.FORBIDDEN
    if any(marker in msg for marker in CONTEXT_OVERFLOW_MARKERS):
        return CrewErrorKind.CONTEXT_WINDOW_EXCEEDED
    if any(marker in msg for marker in _QUOTA_MESSAGES):
        return CrewErrorKind.QUOTA_EXCEEDED
    if status == 429 or name == "RateLimitError":
        return CrewErrorKind.USAGE_LIMIT
    # 过载与内部错误分开：过载重试无意义（见 _NON_RETRYABLE_KINDS），
    # 其余 5xx（500/502/504）按内部瞬时错误处理。
    if status == 503 or name == "OverloadedError":
        return CrewErrorKind.SERVER_OVERLOADED
    if name in ("APITimeoutError", "TimeoutError"):
        return CrewErrorKind.TIMEOUT
    if name == "APIConnectionError" or name in (
        "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        "RemoteProtocolError", "ConnectError",
    ):
        return CrewErrorKind.STREAM
    if name == "InternalServerError" or (status is not None and status >= 500):
        return CrewErrorKind.INTERNAL
    return CrewErrorKind.UNKNOWN


def _retryable_for(exc: Exception, kind: CrewErrorKind, status: int | None) -> bool:
    if kind in CrewError.RETRYABLE_KINDS:
        return True
    if kind in _NON_RETRYABLE_KINDS:
        return False
    # USAGE_LIMIT / UNKNOWN：没有更细信息时沿用状态码 + 类名 + 消息文本的瞬时性判定。
    if status == 429 or (status is not None and status >= 500):
        return True
    if type(exc).__name__ in _RETRYABLE_NAMES:
        return True
    msg = str(exc).lower()
    if any(marker in msg for marker in _TIMEOUT_MESSAGES + _STREAM_MESSAGES):
        return True
    return False


def _retry_delay_for(exc: Exception, status: int | None) -> float | None:
    # Retry-After 只对限流/过载有意义；body 的 retry_delay 字段不限状态兜底。
    delay = _header_retry_delay(exc) if status in (429, 503) else None
    if delay is None:
        delay = _body_retry_delay(exc)
    return delay


def _header_retry_delay(exc: Exception) -> float | None:
    # openai SDK 异常带 response_headers；httpx 异常（如 HTTPStatusError）带 response。
    headers: Any = getattr(exc, "response_headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    # 只支持秒数格式；HTTP-date 形式需要与服务端时钟对比，此处不解析，回落 None。
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _body_retry_delay(exc: Exception) -> float | None:
    body: Any = getattr(exc, "body", None)
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    error_body = body.get("error")
    candidates = (body.get("retry_delay"), error_body.get("retry_delay") if isinstance(error_body, dict) else None)
    for value in candidates:
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None
