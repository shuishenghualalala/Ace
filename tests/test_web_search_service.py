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
async def test_handler_marks_degraded_bing_html_result(monkeypatch):
    configure_search({"providers": ["bing_html"]})
    html = (
        '<ol><li class="b_algo"><h2>'
        '<a href="https://example.com/docs">Example Docs</a>'
        "</h2></li></ol>"
    )
    monkeypatch.setattr(
        web_tools,
        "_authorized_fetch",
        lambda *a, **k: _async_value(("https://cn.bing.com/search?q=x", html)),
    )

    payload = json.loads(await web_tools.handle_web_search({"query": "example"}))

    assert payload["provider"] == "bing_html"
    assert payload["degraded"] is True
    assert payload["results"] == [{"title": "Example Docs", "url": "https://example.com/docs"}]


@pytest.mark.asyncio
async def test_handler_uses_bocha_api_and_reports_fallback_chain(monkeypatch):
    configure_search({"providers": ["bocha", "bing_html"]})
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

    async def fail_bing(*a, **k):
        raise ToolError("不应走到兜底")

    monkeypatch.setattr(web_tools, "_authorized_json_post", fake_post)
    monkeypatch.setattr(web_tools, "_authorized_fetch", fail_bing)

    payload = json.loads(await web_tools.handle_web_search({"query": "example", "limit": 3}))

    assert posted["url"] == web_tools._BOCHA_API_URL
    assert posted["payload"]["query"] == "example"
    assert posted["api_key"] == "test-key"
    assert payload["provider"] == "bocha"
    assert "degraded" not in payload
    assert payload["results"] == [
        {"title": "BoCha Result", "url": "https://result.example", "snippet": "snippet text"}
    ]


async def _async_value(value):
    return value
