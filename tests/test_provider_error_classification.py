"""provider 异常统一分类单测：CrewErrorKind 分类矩阵、Retry-After 解析、
category↔kind 双向兼容、resilience 流式中断判定的类型化收敛。

异常素材用真实 httpx 异常 + 真实 openai SDK 异常（带 response_headers/body），
以及仅有类名/status_code 属性的轻量假类，覆盖「不硬依赖 SDK 类路径」的契约。
"""

from __future__ import annotations

import httpx
import pytest

from crew.agent.loop.resilience import is_stream_interrupt_recoverable
from crew.core.errors import (
    CrewError,
    CrewErrorKind,
    ProviderError,
    category_for_kind,
    kind_for_category,
)
from crew.providers.classify import classify_provider_error

_REQUEST = httpx.Request("POST", "https://api.example.com/v1/chat")


def _sdk_error(cls, status: int, *, headers: dict | None = None, body=None):
    """构造真实 openai SDK 异常：response_headers/status_code/body 一应俱全。"""
    response = httpx.Response(status, headers=headers or {}, request=_REQUEST)
    return cls("upstream refused", response=response, body=body)


def _bare(status: int | None = None):
    """只有 status_code 属性的假异常：模拟类名不在白名单内的 SDK 包装。"""
    exc = Exception("boom")
    if status is not None:
        exc.status_code = status  # type: ignore[attr-defined]
    return exc


# --------------------------------------------------------------------------- #
# 1. kind 分类矩阵
# --------------------------------------------------------------------------- #

def test_classify_429_with_retry_after():
    import openai

    exc = _sdk_error(
        openai.RateLimitError, 429,
        headers={"retry-after": "2.5"},
        body={"error": {"message": "rate limit exceeded"}},
    )
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.USAGE_LIMIT
    assert c.status == 429
    assert c.retryable is True
    assert c.retry_delay == 2.5
    assert c.category == "rate_limit"


def test_classify_burst_protection_as_retryable_usage_limit():
    """突发流量保护：无状态码、SDK 类名为通用 APIError，仅靠消息文本识别。"""

    class APIError(Exception):
        pass

    exc = APIError(
        "System protection triggered by request burst. "
        "Please slow down traffic growth and increase requests gradually "
        "before retrying. Request id: 0217905776367534b8de4e4bcc885b4197a5e365a"
    )
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.USAGE_LIMIT
    assert c.retryable is True
    assert c.status is None
    assert c.category == "rate_limit"

    # 端到端：包装成 ProviderError 后，executor 重试与流中断续写判定均放行。
    err = ProviderError(str(exc), retryable=c.retryable, kind=c.kind, status=c.status)
    assert err.is_retryable() is True
    assert is_stream_interrupt_recoverable(err) is True


def test_classify_401_auth_not_retryable():
    import openai

    exc = _sdk_error(openai.AuthenticationError, 401)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.AUTH
    assert c.retryable is False
    assert c.retry_delay is None


def test_classify_timeout():
    exc = httpx.ReadTimeout("read timed out", request=_REQUEST)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.TIMEOUT
    assert c.status is None
    assert c.retryable is True
    assert c.category == "timeout"


def test_classify_stream_disconnect():
    exc = httpx.RemoteProtocolError("peer closed connection", request=_REQUEST)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.STREAM
    assert c.retryable is True
    assert c.category == "connection"


def test_classify_context_overflow():
    exc = Exception("maximum context length exceeded: 128000 tokens")
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.CONTEXT_WINDOW_EXCEEDED
    assert c.retryable is False


def test_classify_quota_on_429_not_retryable():
    """同一 429，欠费与限流语义不同：quota 优先于状态映射，且不可重试。"""
    exc = Exception("insufficient_quota: you have run out of credits")
    exc.status_code = 429  # type: ignore[attr-defined]
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.QUOTA_EXCEEDED
    assert c.retryable is False
    assert c.category == "rate_limit"


def test_classify_503_overloaded_not_retryable():
    exc = _bare(503)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.SERVER_OVERLOADED
    assert c.retryable is False
    assert c.category == "server"


def test_classify_500_internal_retryable():
    exc = _bare(500)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.INTERNAL
    assert c.retryable is True
    assert c.category == "server"


def test_classify_unknown_400():
    exc = _bare(400)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.UNKNOWN
    assert c.retryable is False
    assert c.category == "provider"


def test_classify_sdk_wrapped_httpx_cause():
    """SDK 把底层 httpx 异常挂在 __cause__ 上时仍能判为断流。"""
    exc = Exception("API error")
    exc.__cause__ = httpx.ConnectError("connection refused", request=_REQUEST)
    c = classify_provider_error(exc)
    assert c.kind is CrewErrorKind.STREAM
    assert c.retryable is True


def test_classify_uses_status_param_over_attr():
    """显式 status 参数优先（anthropic 从 HTTPStatusError.response 取值的路径）。"""
    exc = _bare(500)
    c = classify_provider_error(exc, status=429)
    assert c.status == 429
    assert c.kind is CrewErrorKind.USAGE_LIMIT


# --------------------------------------------------------------------------- #
# 2. Retry-After / retry_delay 解析
# --------------------------------------------------------------------------- #

def test_retry_after_header_seconds():
    import openai

    exc = _sdk_error(openai.RateLimitError, 429, headers={"retry-after": "3"})
    assert classify_provider_error(exc).retry_delay == 3.0


def test_retry_after_header_missing_falls_back_to_body():
    import openai

    exc = _sdk_error(
        openai.RateLimitError, 429,
        body={"error": {"message": "slow down"}, "retry_delay": 1.5},
    )
    assert classify_provider_error(exc).retry_delay == 1.5


def test_retry_after_missing_everywhere_is_none():
    import openai

    exc = _sdk_error(openai.RateLimitError, 429)
    assert classify_provider_error(exc).retry_delay is None


def test_retry_after_http_date_not_supported_falls_back():
    """HTTP-date 形式的 Retry-After 不解析（需时钟对比），回落 body 兜底。"""
    import openai

    exc = _sdk_error(
        openai.InternalServerError, 503,
        headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
        body={"retry_delay": 7},
    )
    assert classify_provider_error(exc).retry_delay == 7


def test_retry_after_httpx_response_headers():
    """httpx 异常路径：response.headers（anthropic 裸 httpx 客户端）。"""
    response = httpx.Response(
        429, headers={"retry-after": "4"}, request=_REQUEST,
    )
    exc = httpx.HTTPStatusError("rate limited", request=_REQUEST, response=response)
    assert classify_provider_error(exc, status=429).retry_delay == 4.0


def test_retry_after_non_numeric_header_silent_none():
    import openai

    exc = _sdk_error(openai.RateLimitError, 429, headers={"retry-after": "soon"})
    assert classify_provider_error(exc).retry_delay is None


def test_retry_after_only_for_429_503():
    """非限流/过载状态不读 Retry-After 头；body retry_delay 仍兜底。"""
    import openai

    exc = _sdk_error(openai.AuthenticationError, 401, headers={"retry-after": "9"})
    assert classify_provider_error(exc).retry_delay is None


# --------------------------------------------------------------------------- #
# 3. category ↔ kind 双向兼容
# --------------------------------------------------------------------------- #

def test_category_to_kind_mapping():
    err = ProviderError("x", category="timeout")
    assert err.kind is CrewErrorKind.TIMEOUT
    assert err.category == "timeout"


def test_kind_to_category_mapping():
    err = ProviderError("x", kind=CrewErrorKind.USAGE_LIMIT)
    assert err.category == "rate_limit"
    assert err.kind is CrewErrorKind.USAGE_LIMIT


def test_default_kind_unknown_category_provider():
    err = ProviderError("x")
    assert err.kind is CrewErrorKind.UNKNOWN
    assert err.category == "provider"
    assert err.status is None
    assert err.retry_delay is None


def test_explicit_category_and_kind_kept():
    """两个都显式传入时各自保留（调用方负责一致性）。"""
    err = ProviderError("x", category="custom_cat", kind=CrewErrorKind.AUTH)
    assert err.category == "custom_cat"
    assert err.kind is CrewErrorKind.AUTH


@pytest.mark.parametrize(
    ("category", "kind"),
    [
        ("timeout", CrewErrorKind.TIMEOUT),
        ("connection", CrewErrorKind.STREAM),
        ("auth", CrewErrorKind.AUTH),
        ("forbidden", CrewErrorKind.FORBIDDEN),
        ("rate_limit", CrewErrorKind.USAGE_LIMIT),
        ("server", CrewErrorKind.INTERNAL),
        ("unsupported_capability", CrewErrorKind.UNSUPPORTED_CAPABILITY),
        ("provider", CrewErrorKind.UNKNOWN),
    ],
)
def test_category_kind_roundtrip(category: str, kind: CrewErrorKind):
    assert kind_for_category(category) is kind
    assert category_for_kind(kind) == category
    err = ProviderError("x", category=category)
    assert err.kind is kind
    assert err.category == category


def test_kind_for_category_unknown_string():
    assert kind_for_category("nonexistent") is CrewErrorKind.UNKNOWN
    assert kind_for_category(None) is CrewErrorKind.UNKNOWN


# --------------------------------------------------------------------------- #
# 4. retryable 白名单语义
# --------------------------------------------------------------------------- #

def test_retryable_whitelist_by_kind():
    assert ProviderError("t", kind=CrewErrorKind.TIMEOUT).is_retryable() is True
    assert ProviderError("s", kind=CrewErrorKind.STREAM).is_retryable() is True
    assert ProviderError("i", kind=CrewErrorKind.INTERNAL).is_retryable() is True
    for kind in (
        CrewErrorKind.USAGE_LIMIT, CrewErrorKind.QUOTA_EXCEEDED,
        CrewErrorKind.AUTH, CrewErrorKind.FORBIDDEN, CrewErrorKind.CANCELLED,
        CrewErrorKind.CONTEXT_WINDOW_EXCEEDED,
        CrewErrorKind.UNSUPPORTED_CAPABILITY, CrewErrorKind.TOOL,
        CrewErrorKind.CONFIG, CrewErrorKind.SERVER_OVERLOADED,
        CrewErrorKind.UNKNOWN,
    ):
        assert ProviderError("x", kind=kind).is_retryable() is False, kind


def test_explicit_retryable_overrides_whitelist():
    assert ProviderError("x", kind=CrewErrorKind.TIMEOUT, retryable=False).is_retryable() is False
    assert ProviderError("x", retryable=True).is_retryable() is True


def test_subclass_default_kinds():
    from crew.core.errors import ConfigError, ToolError

    assert ToolError("x").kind is CrewErrorKind.TOOL
    assert ConfigError("x").kind is CrewErrorKind.CONFIG
    assert ToolError("x").is_retryable() is False


def test_retry_delay_carried():
    err = ProviderError("x", kind=CrewErrorKind.USAGE_LIMIT, retryable=True, status=429, retry_delay=2.0)
    assert err.retry_delay == 2.0
    assert err.status == 429


# --------------------------------------------------------------------------- #
# 5. resilience 流式中断判定收敛
# --------------------------------------------------------------------------- #

def test_resilience_typed_crew_error():
    assert is_stream_interrupt_recoverable(
        ProviderError("t", retryable=True, category="timeout")
    ) is True
    assert is_stream_interrupt_recoverable(
        ProviderError("u", retryable=False, category="auth")
    ) is False
    # 可重试但 kind 未知：不续写（与旧的 category 白名单语义一致）。
    assert is_stream_interrupt_recoverable(ProviderError("?", retryable=True)) is False
    # 503 过载：即使带了 Retry-After 也不续写。
    assert is_stream_interrupt_recoverable(
        ProviderError("busy", retryable=False, kind=CrewErrorKind.SERVER_OVERLOADED)
    ) is False


def test_resilience_bare_exception_fallback():
    assert is_stream_interrupt_recoverable(
        httpx.ReadTimeout("t", request=_REQUEST)
    ) is True
    assert is_stream_interrupt_recoverable(
        httpx.RemoteProtocolError("peer closed", request=_REQUEST)
    ) is True
    assert is_stream_interrupt_recoverable(ValueError("random")) is False


def test_resilience_plain_crew_error_with_retryable_kind():
    """非 ProviderError 的 CrewError 也走类型化判定（旧实现按类名必然 False）。"""
    assert is_stream_interrupt_recoverable(CrewError("断流", kind=CrewErrorKind.STREAM)) is True
    assert is_stream_interrupt_recoverable(CrewError("取消", kind=CrewErrorKind.CANCELLED)) is False


# --------------------------------------------------------------------------- #
# 6. provider 包装集成：429 + Retry-After 不再丢失
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_openai_provider_carries_classification(monkeypatch):
    """openai provider 包装路径：429 的 status / Retry-After 随 ProviderError 携带。"""
    import openai

    from crew.core.types import Message
    from crew.providers.openai_provider import OpenAIProvider

    p = OpenAIProvider(api_key="sk-test", model="gpt-test")
    exc = _sdk_error(
        openai.RateLimitError, 429,
        headers={"retry-after": "2"},
        body={"error": {"message": "rate limit"}},
    )

    async def fake_create(**kwargs):
        raise exc

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    with pytest.raises(ProviderError) as ei:
        await p.chat([Message.user("hi")])
    err = ei.value
    assert err.kind is CrewErrorKind.USAGE_LIMIT
    assert err.status == 429
    assert err.retry_delay == 2.0
    assert err.retryable is True
    assert err.category == "rate_limit"
