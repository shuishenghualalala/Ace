from __future__ import annotations

import asyncio

from crew.gateway.app import _GatewayRequestBodyLimitMiddleware
from crew.gateway.helpers import safe_public_error
from crew.tools.redact import redact_sensitive_display_text, redact_url_for_display


def test_safe_public_error_preserves_plain_validation_text():
    assert safe_public_error(ValueError("模型配置无效")) == "模型配置无效"


def test_safe_public_error_hides_secret_and_host_path():
    error = r"C:\private\config.yaml ACCESS_TOKEN=must-not-leak"

    public = safe_public_error(RuntimeError(error), "内部错误")

    assert public == "内部错误"
    assert "must-not-leak" not in public
    assert r"C:\private\config.yaml" not in public


def test_safe_public_error_redacts_token_query_header_env_and_key_material():
    corpus = {
        "access_token=sk-abcdef0123456789 and also Authorization: Bearer xoxb-0123456789abcdef": [
            "sk-abcdef0123456789",
            "xoxb-0123456789abcdef",
        ],
        "https://example.com/cb?token=abc123&signature=xyz789&keep=1": [
            "abc123",
            "xyz789",
        ],
        "PASSWORD=hunter2 SECRET_KEY=supersecret": ["hunter2", "supersecret"],
        "AKIAIOSFODNN7EXAMPLE": ["AKIAIOSFODNN7EXAMPLE"],
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----": [
            "MIIEvQIBADANBgkq",
        ],
    }
    for raw, must_not_leak in corpus.items():
        public = safe_public_error(RuntimeError(raw), "内部错误")
        for secret in must_not_leak:
            assert secret not in public


def test_safe_public_error_keeps_only_scheme_and_host_for_proxy_errors():
    proxy = (
        "http://proxy-user:proxy-password@proxy.example.test:3128/"
        "?access_token=proxy-query-secret"
    )
    public = safe_public_error(RuntimeError(f"proxy connect failed: {proxy}"), "内部错误")

    assert "proxy-user" not in public
    assert "proxy-password" not in public
    assert "proxy-query-secret" not in public
    assert "proxy.example.test" in public


def test_display_redaction_masks_sensitive_query_parameters():
    public = redact_sensitive_display_text(
        "GET https://host/path?api_key=sk-live-abcdefghijklmnop&limit=10",
    )
    assert "sk-live-abcdefghijklmnop" not in public
    assert "limit=10" in public


def test_display_url_redaction_drops_userinfo_and_masks_credential_shapes():
    public = redact_url_for_display(
        "https://user:password@example.test/path?token=abc&keywords=hello&q=sk-live-secret1234567890#frag-secret",
    )
    assert "user:" not in public
    assert "password" not in public
    assert "abc" not in public.split("?")[1]
    assert "keywords=hello" in public
    assert "sk-live-secret1234567890" not in public
    assert "frag-secret" not in public
    assert public.startswith("https://example.test/path?")


def test_gateway_request_body_limit_rejects_declared_and_streamed_overflow():
    async def run():
        called = False

        async def app(_scope, _receive, _send):
            nonlocal called
            called = True

        async def receive_declared():
            raise AssertionError("declared oversize must not read body")

        sent: list[dict] = []
        async def send(message):
            sent.append(message)

        await _GatewayRequestBodyLimitMiddleware(app, max_bytes=4)(
            {"type": "http", "headers": [(b"content-length", b"5")]},
            receive_declared,
            send,
        )
        assert sent[0]["status"] == 413
        assert called is False

        sent.clear()
        chunks = iter(
            [
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"45", "more_body": False},
            ]
        )

        async def receive_streamed():
            return next(chunks)

        async def body_reader(_scope, receive, _send):
            await receive()
            await receive()

        await _GatewayRequestBodyLimitMiddleware(body_reader, max_bytes=4)(
            {"type": "http", "headers": []},
            receive_streamed,
            send,
        )
        assert sent[0]["status"] == 413

    asyncio.run(run())
