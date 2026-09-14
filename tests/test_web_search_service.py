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
    assert "BOCHA_API_KEY" in message
    assert "searxng_base_url" in message


@pytest.mark.asyncio
async def test_handler_fails_closed_when_nothing_configured(monkeypatch):
    # 未配置 exa key / bocha key / searxng 地址时，工具不得触网，直接结构化报错。
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    configure_search(None)

    async def forbidden_fetch(*a, **k):
        raise AssertionError("fail-closed 路径不应发起网络请求")

    monkeypatch.setattr(web_tools, "_authorized_fetch", forbidden_fetch)
    monkeypatch.setattr(web_tools, "_authorized_json_post", forbidden_fetch)

    with pytest.raises(ToolError, match="没有可用的搜索源"):
        await web_tools.handle_web_search({"query": "example"})


@pytest.mark.asyncio
async def test_handler_uses_bocha_api_and_prefixes_untrusted_notice(monkeypatch):
    configure_search({"providers": ["bocha", "searxng"]})
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    body = {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "BoCha Result",
                        "url": "https://result.example",
                        "snippet": "snippet text",
                    }
                ]
            }
        }
    }
    posted = {}

    async def fake_post(url, payload, *, api_key, tool_name, ctx):
        posted.update(url=url, payload=payload, api_key=api_key)
        return body

    async def fail_searxng(*a, **k):
        raise AssertionError("bocha 可用时不应降级到 searxng")

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)
    monkeypatch.setattr(web_tools, "_authorized_fetch", fail_searxng)

    raw = await web_tools.handle_web_search({"query": "example", "limit": 3})
    payload = json.loads(raw)

    assert posted["url"] == web_tools._BOCHA_API_URL
    assert posted["payload"]["query"] == "example"
    assert posted["api_key"] == "test-key"
    # 不可信标记恒在结果文本最前（payload 首键）。
    assert payload["notice"] == web_tools._UNTRUSTED_CONTENT_NOTICE
    assert list(payload)[0] == "notice"
    assert payload["provider"] == "bocha"
    assert payload["results"] == [
        {"title": "BoCha Result", "url": "https://result.example", "snippet": "snippet text"}
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
# Exa provider：请求形态、highlights 映射、错误信息提取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exa_maps_first_nonblank_highlight_to_snippet(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    configure_search({"providers": ["exa"]})
    posted = {}

    async def fake_post(url, payload, *, api_key, tool_name, ctx):
        posted.update(url=url, payload=payload, api_key=api_key)
        return {
            "results": [
                {
                    "url": "https://a.example/page",
                    "title": "  First\t Result ",
                    "publishedDate": "2026-09-01",
                    "highlights": ["", "   ", "actual snippet"],
                },
                {
                    "url": "https://b.example/no-title",
                    "highlights": ["second snippet"],
                },
            ]
        }

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)

    outcome = await search_with_fallback("q", 5, _CTX)

    assert outcome.provider_id == "exa"
    assert posted["url"] == "https://api.exa.ai/search"
    assert posted["api_key"] == "exa-key"
    assert posted["payload"]["type"] == "auto"
    assert posted["payload"]["numResults"] == 5
    assert posted["payload"]["contents"] == {"highlights": {"highlightsPerUrl": 1}}
    assert outcome.results[0].title == "First Result"
    assert outcome.results[0].snippet == "actual snippet"
    # 上游无 title 时保持空串，不编造。
    assert outcome.results[1].title == ""
    assert outcome.results[1].snippet == "second snippet"


@pytest.mark.asyncio
async def test_exa_drops_entries_without_highlight(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    configure_search({"providers": ["exa"]})

    async def fake_post(url, payload, *, api_key, tool_name, ctx):
        return {
            "results": [
                {"url": "https://a.example/with", "title": "A", "highlights": ["ok"]},
                {"url": "https://b.example/without", "title": "B"},
                {"url": "https://c.example/blank", "title": "C", "highlights": ["  "]},
                {"url": "notaurl", "title": "D", "highlights": ["ok"]},
            ]
        }

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)

    outcome = await search_with_fallback("q", 5, _CTX)

    assert [item.url for item in outcome.results] == ["https://a.example/with"]


def test_exa_available_requires_key_and_valid_base_url(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    configure_search(None)
    assert web_tools._exa_available() is False

    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    assert web_tools._exa_available() is True

    configure_search({"exa_base_url": "http://127.0.0.1:8080"})
    assert web_tools._exa_available() is False


def test_post_json_url_extracts_error_detail_from_http_error_body(monkeypatch):
    from crew.security.outbound import PublicHttpError

    def raise_http_error(*a, **k):
        raise PublicHttpError(401, b'{"error":"missing authorization"}')

    monkeypatch.setattr(web_tools, "request_public_http", raise_http_error)

    with pytest.raises(ToolError, match=r"HTTP 401: missing authorization"):
        web_tools._post_json_url("https://api.exa.ai/search", {}, "key", set())


def test_post_json_url_falls_back_to_status_when_body_not_parseable(monkeypatch):
    from crew.security.outbound import PublicHttpError

    def raise_http_error(*a, **k):
        raise PublicHttpError(503, b"<html>Service Unavailable</html>")

    monkeypatch.setattr(web_tools, "request_public_http", raise_http_error)

    with pytest.raises(ToolError, match=r"HTTP 503"):
        web_tools._post_json_url("https://api.exa.ai/search", {}, "key", set())


@pytest.mark.asyncio
async def test_handler_prefers_exa_in_default_chain(monkeypatch):
    # 默认链按注册序（exa 最先）：只配置 exa key 时命中 exa。
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    configure_search(None)

    async def fake_post(url, payload, *, api_key, tool_name, ctx):
        assert url == "https://api.exa.ai/search"
        return {
            "results": [
                {
                    "url": "https://a.example",
                    "title": "Exa Result",
                    "highlights": ["snippet"],
                }
            ]
        }

    async def forbidden_get(*a, **k):
        raise AssertionError("默认链不应走到 GET 类 provider")

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)
    monkeypatch.setattr(web_tools, "_authorized_fetch", forbidden_get)

    payload = json.loads(await web_tools.handle_web_search({"query": "example"}))

    assert payload["provider"] == "exa"
    assert payload["notice"] == web_tools._UNTRUSTED_CONTENT_NOTICE
    assert payload["results"] == [
        {"title": "Exa Result", "url": "https://a.example", "snippet": "snippet"}
    ]
