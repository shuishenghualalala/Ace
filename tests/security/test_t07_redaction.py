from crew.gateway.response_filters import apply_text_filters
from crew.tools.redact import redact_sensitive_display_text, safe_public_error


def test_display_redaction_covers_cli_headers_and_query_credentials() -> None:
    value = (
        "run --token ordinary-token -H 'Authorization: Bearer header-token' "
        "https://user:password@example.test/callback?token=query-token"
    )

    redacted = redact_sensitive_display_text(value)

    assert "ordinary-token" not in redacted
    assert "header-token" not in redacted
    assert "password" not in redacted
    assert "query-token" not in redacted
    assert "example.test" in redacted


def test_public_error_sanitizes_an_untrusted_fallback() -> None:
    secret = "sk-error-fallback-canary-123456"
    result = safe_public_error(
        RuntimeError(r"C:\Users\alice\internal.txt"),
        fallback=f"internal {secret}",
    )

    assert secret not in result
    assert "C:\\Users\\alice" not in result


def test_response_filter_chain_fails_closed_and_redacts_untrusted_output() -> None:
    value = "Authorization: Bearer sk-response-canary-123456"

    filtered = apply_text_filters(value)

    assert "sk-response-canary-123456" not in filtered
