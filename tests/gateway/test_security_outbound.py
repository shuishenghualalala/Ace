"""Gateway 安全基线：密钥过滤与静默回复检测。"""

from crew.core.errors import CrewErrorKind, ProviderError, ToolError
from crew.features.outbound import (
    enrich_error_chunk,
    format_outbound_payload,
    should_skip_chunk,
)
from crew.gateway.helpers import status_frame
from crew.features.response_filters import is_silent_reply, redact_secrets
from crew.core.envelope import ResponseChunk


def test_redact_secrets_sk_prefix():
    text = "key is sk-abcdefghijklmnopqrstuvwxyz123456"
    result = redact_secrets(text, {})
    assert "sk-" not in result
    assert "[REDACTED]" in result


def test_is_silent_reply_empty_and_no_reply():
    assert is_silent_reply("") is True
    assert is_silent_reply("   ") is True
    assert is_silent_reply("NO_REPLY") is True
    assert is_silent_reply("hello") is False


def test_format_outbound_skips_silent_final():
    chunk = ResponseChunk.final("req1", "NO_REPLY")
    assert format_outbound_payload(chunk, session_id="s1") is None


def test_format_outbound_redacts_secrets():
    chunk = ResponseChunk.final("req1", "token sk-abc1234567890xyz")
    payload = format_outbound_payload(chunk, session_id="s1")
    assert payload is not None
    assert "sk-" not in payload["body"]["text"]
    assert "[REDACTED]" in payload["body"]["text"]


def test_format_outbound_includes_request_id():
    chunk = ResponseChunk.delta("req-visible", "hello", 1)
    payload = format_outbound_payload(chunk, session_id="s1")
    assert payload is not None
    assert payload["request_id"] == "req-visible"


def test_status_frame_marks_control_status():
    payload = status_frame("s1", "已停止")
    assert payload["kind"] == "status"
    assert payload["body"]["control"] is True


def test_should_skip_chunk_final_empty():
    chunk = ResponseChunk.final("req1", "")
    assert should_skip_chunk(chunk) is True


def test_enrich_error_chunk_provider_error_uses_kind_category():
    exc = ProviderError("超时", kind=CrewErrorKind.TIMEOUT, retryable=True, retry_delay=2.0)
    chunk = ResponseChunk.error(
        "r1", "超时", code=exc.kind.value, retryable=True, retry_delay=2.0
    )
    enriched = enrich_error_chunk(chunk, exc)
    assert enriched.body["category"] == "timeout"
    assert enriched.body["code"] == "timeout"
    assert enriched.body["retryable"] is True
    assert enriched.body["retry_delay"] == 2.0


def test_enrich_error_chunk_tool_error_category_tool():
    enriched = enrich_error_chunk(ResponseChunk.error("r1", "工具失败"), ToolError("工具失败"))
    assert enriched.body["category"] == "tool"


def test_enrich_error_chunk_plain_exception_text_fallback():
    """非类型化异常保留文本兜底分类。"""
    enriched = enrich_error_chunk(
        ResponseChunk.error("r1", "x"), ValueError("timed out waiting")
    )
    assert enriched.body["category"] == "timeout"


def test_enrich_error_chunk_derives_category_from_kind_code():
    """无 exc 时，body 里已有的 kind code 反推 category，结构化字段透传不被破坏。"""
    chunk = ResponseChunk.error("r1", "限流", code="usage_limit", retryable=True, retry_delay=5.0)
    enriched = enrich_error_chunk(chunk)
    assert enriched.body["category"] == "rate_limit"
    assert enriched.body["code"] == "usage_limit"
    assert enriched.body["retryable"] is True
    assert enriched.body["retry_delay"] == 5.0
