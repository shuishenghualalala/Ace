"""web_search provider seam：注册、有序降级、结构化失败。"""

from __future__ import annotations

import json

import pytest

from crew.core.errors import ToolError
from crew.tools import web_tools
from crew.tools.web_search_service import (
    SearchContext,
    SearchProvider,
    SearchResult,
    configure_search,
    register_search_provider,
    reset_search_providers,
    search_with_fallback,
)

_CTX = SearchContext()


def _mcp_sse_response(text: str) -> str:
    """构造 Exa 托管 MCP 的 SSE 形态响应（event/message + 单行 data）。"""
    envelope = {
        "result": {"content": [{"type": "text", "text": text}]},
        "jsonrpc": "2.0",
        "id": 1,
    }
    return f"event: message\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"


@pytest.fixture(autouse=True)
def _restore_service():
    yield
    reset_search_providers()
    web_tools._register_default_search_providers()
    configure_search(None)


def _provider(pid: str, results=None, error: Exception | None = None, available: bool = True):
    async def search(query: str, limit: int, ctx: SearchContext):
        if error is not None:
            raise error
        return results or []

    return SearchProvider(id=pid, available=lambda: available, search=search)


@pytest.mark.asyncio
async def test_fallback_tries_chain_in_order_and_records_attempts():
    register_search_provider(
        _provider("first", error=RuntimeError("boom"))
    )
    register_search_provider(
        _provider("second", results=[SearchResult(title="T", url="https://a.example")])
    )
    configure_search({"providers": ["first", "second"]})

    outcome = await search_with_fallback("q", 5, _CTX)

    assert outcome.provider_id == "second"
    assert outcome.attempted == ("first(boom)",)
    assert outcome.results[0].title == "T"


@pytest.mark.asyncio
async def test_unavailable_provider_is_skipped_and_recorded():
    register_search_provider(_provider("nokey", available=False))
    register_search_provider(_provider("ok", results=[SearchResult(title="T", url="https://a.example")]))
    configure_search({"providers": ["nokey", "ok"]})

    outcome = await search_with_fallback("q", 5, _CTX)

    assert outcome.provider_id == "ok"
    assert outcome.attempted == ("nokey(不可用)",)


@pytest.mark.asyncio
async def test_all_failed_raises_structured_error_with_chain():
    register_search_provider(_provider("a", error=RuntimeError("timeout")))
    register_search_provider(_provider("b", error=RuntimeError("403")))
    configure_search({"providers": ["a", "b"]})

    with pytest.raises(ToolError) as excinfo:
        await search_with_fallback("q", 5, _CTX)

    message = str(excinfo.value)
    assert "所有搜索源均失败" in message
    assert "a(timeout)" in message
    assert "b(403)" in message


@pytest.mark.asyncio
async def test_configured_unknown_provider_is_rejected():
    configure_search({"providers": ["ghost"]})

    with pytest.raises(ToolError, match="未注册的 provider"):
        await search_with_fallback("q", 5, _CTX)


@pytest.mark.asyncio
async def test_default_chain_uses_available_providers_in_registration_order():
    reset_search_providers()
    register_search_provider(_provider("zeta", results=[SearchResult(title="Z", url="https://z.example")]))
    register_search_provider(_provider("alpha", available=False))

    outcome = await search_with_fallback("q", 5, _CTX)

    assert outcome.provider_id == "zeta"


@pytest.mark.asyncio
async def test_no_available_provider_fails_closed_with_config_guidance():
    reset_search_providers()
    register_search_provider(_provider("nokey", available=False))

    with pytest.raises(ToolError) as excinfo:
        await search_with_fallback("q", 5, _CTX)

    message = str(excinfo.value)
    assert "没有可用的搜索源" in message
    assert "tools.web_search" in message
    assert "searxng_base_url" in message


@pytest.mark.asyncio
async def test_handler_searches_anonymously_without_any_key(monkeypatch):
    # exa 托管 MCP 匿名免费：不设任何 key 也能搜索（本次改造的核心语义）。
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    configure_search(None)
    posted = {}

    async def fake_post(url, payload, *, tool_name, ctx):
        posted.update(url=url, payload=payload)
        return _mcp_sse_response(
            "Title: Exa Result\nURL: https://r.example\nHighlights:\n- snippet text"
        )

    async def forbidden_get(*a, **k):
        raise AssertionError("默认链命中 exa，不应发起 GET 类请求（searxng）")

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)
    monkeypatch.setattr(web_tools, "_authorized_fetch", forbidden_get)

    raw = await web_tools.handle_web_search({"query": "example", "limit": 3})
    payload = json.loads(raw)

    assert posted["url"] == web_tools._EXA_MCP_URL
    assert "exaApiKey" not in posted["url"]
    call = posted["payload"]
    assert call["method"] == "tools/call"
    assert call["params"]["name"] == "web_search_exa"
    assert call["params"]["arguments"] == {"query": "example", "numResults": 3}
    # 不可信标记恒在结果文本最前（payload 首键）。
    assert payload["notice"] == web_tools._UNTRUSTED_CONTENT_NOTICE
    assert list(payload)[0] == "notice"
    assert payload["provider"] == "exa"
    assert payload["results"] == [
        {"title": "Exa Result", "url": "https://r.example", "snippet": "snippet text"}
    ]


# ---------------------------------------------------------------------------
# 注册即 effect：disposer 语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispose_removes_provider_and_is_idempotent():
    provider = _provider("temp", results=[SearchResult(title="T", url="https://a.example")])
    dispose = register_search_provider(provider)
    configure_search({"providers": ["temp"]})

    dispose()
    assert dispose() is None  # 幂等：重复释放无副作用
    with pytest.raises(ToolError, match="未注册的 provider"):
        await search_with_fallback("q", 5, _CTX)


@pytest.mark.asyncio
async def test_dispose_after_reregister_does_not_remove_newer_entry():
    old = _provider("dup", results=[SearchResult(title="old", url="https://a.example")])
    old_dispose = register_search_provider(old)
    new = _provider("dup", results=[SearchResult(title="new", url="https://b.example")])
    register_search_provider(new)
    configure_search({"providers": ["dup"]})

    old_dispose()

    outcome = await search_with_fallback("q", 5, _CTX)
    assert outcome.provider_id == "dup"
    assert outcome.results[0].title == "new"


@pytest.mark.asyncio
async def test_dispose_then_reregister_restores_provider():
    provider = _provider("cycle", results=[SearchResult(title="T", url="https://a.example")])
    dispose = register_search_provider(provider)
    configure_search({"providers": ["cycle"]})
    dispose()

    register_search_provider(provider)

    outcome = await search_with_fallback("q", 5, _CTX)
    assert outcome.provider_id == "cycle"


# ---------------------------------------------------------------------------
# Exa provider：MCP 请求形态、文本块解析、SSE/JSON 双格式、错误提取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exa_maps_text_blocks_to_normalized_results(monkeypatch):
    configure_search({"providers": ["exa"]})

    async def fake_post(url, payload, *, tool_name, ctx):
        return _mcp_sse_response(
            "Title:  First\t Result\n"
            "URL: https://a.example/page\n"
            "Published: 2026-09-01\n"
            "Author: N/A\n"
            "Highlights:\n"
            "...\n"
            "actual snippet\n"
            "\n---\n\n"
            "Title: Second\n"
            "URL: https://b.example/no-meta\n"
            "Highlights:\n"
            "- second snippet\n"
        )

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)

    outcome = await search_with_fallback("q", 5, _CTX)

    assert outcome.provider_id == "exa"
    assert outcome.results[0].title == "First Result"
    assert outcome.results[0].url == "https://a.example/page"
    # 纯省略号填充行被丢弃，只留真实摘要。
    assert outcome.results[0].snippet == "actual snippet"
    # highlights 的项目符号剥离；Published/Author 元数据行不进摘要。
    assert outcome.results[1].title == "Second"
    assert outcome.results[1].snippet == "second snippet"


@pytest.mark.asyncio
async def test_exa_keeps_entry_without_highlights_and_drops_invalid_url(monkeypatch):
    configure_search({"providers": ["exa"]})

    async def fake_post(url, payload, *, tool_name, ctx):
        return _mcp_sse_response(
            "Title: A\nURL: https://a.example/with\nHighlights:\n- ok\n\n---\n\n"
            "Title: B\nURL: https://b.example/no-highlights\n\n---\n\n"
            "Title: C\nURL: notaurl\nHighlights:\n- dropped\n"
        )

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)

    outcome = await search_with_fallback("q", 5, _CTX)

    # 摘要缺失保留 title/url（snippet=None，不编造）；URL 不合法整段丢弃。
    assert [(item.url, item.snippet) for item in outcome.results] == [
        ("https://a.example/with", "ok"),
        ("https://b.example/no-highlights", None),
    ]


@pytest.mark.asyncio
async def test_exa_respects_limit_when_truncating_text_blocks(monkeypatch):
    configure_search({"providers": ["exa"]})

    async def fake_post(url, payload, *, tool_name, ctx):
        return _mcp_sse_response(
            "Title: A\nURL: https://a.example\n\n---\n\n"
            "Title: B\nURL: https://b.example\n"
        )

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)

    outcome = await search_with_fallback("q", 1, _CTX)

    assert [item.url for item in outcome.results] == ["https://a.example"]


def test_mcp_result_text_parses_plain_json_and_sse():
    envelope = {"result": {"content": [{"type": "text", "text": "hello"}]}}

    assert web_tools._mcp_result_text(json.dumps(envelope)) == "hello"
    assert web_tools._mcp_result_text(_mcp_sse_response("hello")) == "hello"


def test_mcp_result_text_raises_on_is_error():
    envelope = {
        "result": {"isError": True, "content": [{"type": "text", "text": "rate limited"}]}
    }

    with pytest.raises(ToolError, match="rate limited"):
        web_tools._mcp_result_text(json.dumps(envelope))


def test_exa_mcp_url_anonymous_without_key_and_appends_key_when_set(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    configure_search(None)
    assert web_tools._exa_mcp_url() == "https://mcp.exa.ai/mcp"
    # 匿名即默认可用。
    assert web_tools._exa_available() is True

    monkeypatch.setenv("EXA_API_KEY", "k ey/1")
    assert web_tools._exa_mcp_url() == "https://mcp.exa.ai/mcp?exaApiKey=k%20ey%2F1"

    configure_search({"exa_base_url": "http://127.0.0.1:8080"})
    assert web_tools._exa_available() is False


def test_post_json_url_extracts_error_detail_from_http_error_body(monkeypatch):
    from crew.security.outbound import PublicHttpError

    def raise_http_error(*a, **k):
        raise PublicHttpError(401, b'{"error":"missing authorization"}')

    monkeypatch.setattr(web_tools, "request_public_http", raise_http_error)

    with pytest.raises(ToolError, match=r"HTTP 401: missing authorization"):
        web_tools._post_json_url("https://mcp.exa.ai/mcp", {}, set())


def test_post_json_url_falls_back_to_status_when_body_not_parseable(monkeypatch):
    from crew.security.outbound import PublicHttpError

    def raise_http_error(*a, **k):
        raise PublicHttpError(503, b"<html>Service Unavailable</html>")

    monkeypatch.setattr(web_tools, "request_public_http", raise_http_error)

    with pytest.raises(ToolError, match=r"HTTP 503"):
        web_tools._post_json_url("https://mcp.exa.ai/mcp", {}, set())


def test_post_json_url_retries_transient_connection_failure(monkeypatch):
    from crew.security.outbound import PublicHttpResponse

    calls = {"n": 0}

    def flaky_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # 代理链路偶发 TLS 重置：连接层 OSError 包装成的连接失败。
            raise ValueError("URL 连接失败") from OSError("tls reset")
        return PublicHttpResponse(
            url="https://mcp.exa.ai/mcp", body=b"ok",
            content_type="text/event-stream", charset="utf-8", status=200,
        )

    monkeypatch.setattr(web_tools, "request_public_http", flaky_connect)
    monkeypatch.setattr(web_tools.time, "sleep", lambda _s: None)

    assert web_tools._post_json_url("https://mcp.exa.ai/mcp", {}, set()) == "ok"
    assert calls["n"] == 2


def test_post_json_url_does_not_retry_deterministic_rejection(monkeypatch):
    calls = {"n": 0}

    def always_reject(*a, **k):
        calls["n"] += 1
        raise ValueError("禁止访问私网、链路本地或保留地址")

    monkeypatch.setattr(web_tools, "request_public_http", always_reject)

    with pytest.raises(ToolError, match="搜索服务连接失败"):
        web_tools._post_json_url("https://mcp.exa.ai/mcp", {}, set())
    # 无 OSError 根因的确定性失败不重试。
    assert calls["n"] == 1


def test_fetch_url_retries_transient_connection_failure(monkeypatch):
    # web_extract 的 GET 路径与 MCP POST 共用同一重试策略（对称）。
    calls = {"n": 0}

    def flaky_fetch(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("URL 连接失败") from OSError("tls reset")
        return ("https://a.example/final", b"<html></html>", "text/html", "utf-8")

    monkeypatch.setattr(web_tools, "fetch_public_http", flaky_fetch)
    monkeypatch.setattr(web_tools.time, "sleep", lambda _s: None)

    final_url, source = web_tools._fetch_url("https://a.example")

    assert final_url == "https://a.example/final"
    assert source == "<html></html>"
    assert calls["n"] == 2
