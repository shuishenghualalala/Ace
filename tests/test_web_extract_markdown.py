"""web_extract 清洗/markdown 化/分层截断。"""

from __future__ import annotations

import json

import pytest

from crew.tools import web_fetch_cache, web_tools
from crew.tools.web_extract_markdown import (
    MAX_NESTING_DEPTH,
    OMITTED,
    render_html,
)
from crew.tools.web_tools import (
    _MAX_INLINE_CHARS,
    _TRUNCATION_FOOTER,
    _UNTRUSTED_CONTENT_NOTICE,
    _truncate_middle,
)


@pytest.fixture(autouse=True)
def _clean_fetch_cache():
    # 多个用例共用 https://example.com，不隔离会跨用例假命中
    web_fetch_cache.clear_web_fetch_cache()
    yield
    web_fetch_cache.clear_web_fetch_cache()


def test_strips_non_content_and_hidden_elements():
    source = """
    <html><head><title>T</title><style>.x{}</style></head><body>
      <script>evil()</script><noscript>ns</noscript>
      <iframe src="x"></iframe><object></object><embed src="y">
      <template><p>tpl</p></template>
      <p>visible</p>
      <p hidden>hidden-attr</p>
      <p aria-hidden="true">aria-hidden</p>
      <p style="display:none">display-none</p>
      <p style="visibility: hidden">visibility-hidden</p>
      <input type="hidden" value="h">
      <p>after</p>
    </body></html>
    """
    text = render_html(source).text
    assert "visible" in text
    assert "after" in text
    for leaked in ("evil", "ns", "tpl", "hidden-attr", "aria-hidden", "display-none", "visibility-hidden"):
        assert leaked not in text
    assert "<script" not in text and "<p" not in text


def test_renders_headings_paragraphs_lists_links_and_emphasis():
    source = """
    <h1>Title</h1>
    <p>Intro with <strong>bold</strong> and <em>em</em> and <a href="https://a.example">a link</a>.</p>
    <ul><li>one</li><li>two</li></ul>
    <ol><li>first</li></ol>
    <blockquote><p>quoted</p></blockquote>
    <pre><code>raw &lt;b&gt;code&lt;/b&gt;</code></pre>
    """
    text = render_html(source).text
    assert "# Title" in text
    assert "**bold**" in text
    assert "*em*" in text
    assert "[a link](https://a.example)" in text
    assert "- one" in text and "- two" in text
    assert "1. first" in text
    assert "> quoted" in text
    assert "```\nraw <b>code</b>\n```" in text


def test_empty_list_items_are_dropped_not_bare_markers():
    source = """
    <ul><li></li><li>   </li><li>real</li><li><span style="display:none">x</span></li></ul>
    <ol><li></li><li>second</li></ol>
    """
    text = render_html(source).text
    # 空列表项（装饰占位）整条丢弃，输出只剩真实内容两行；
    # ol 编号跟 HTML 位置语义：空项跳过但第二项仍是 2.
    assert [line for line in text.splitlines() if line.strip()] == ["- real", "2. second"]


def test_tables_render_gfm_without_colspan_expansion():
    source = """
    <table>
      <tr><th>Name</th><th>Value</th></tr>
      <tr><td>a|b</td><td colspan="10000">only one cell</td></tr>
    </table>
    """
    text = render_html(source).text
    assert "| Name | Value |" in text
    assert "|---|---|" in text
    assert "a\\|b" in text
    # colspan 不得展开成爆炸级数量的列
    assert text.count("only one cell") == 1
    assert text.count("|") < 20


def test_excessive_nesting_returns_omitted_marker():
    source = "<div>" * (MAX_NESTING_DEPTH + 2) + "deep" + "</div>" * (MAX_NESTING_DEPTH + 2)
    result = render_html(source)
    assert result.text == OMITTED


def test_malformed_html_does_not_leak_raw_tags():
    result = render_html("<p>unclosed <b>bold <i>italic")
    assert "<b>" not in result.text
    assert "unclosed" in result.text


def test_char_layer_truncation_is_flagged():
    source = "<p>" + "x" * 50 + "</p>"
    result = render_html(source, max_chars=20)
    assert result.source_truncated is True
    assert "xxxx" in result.text


@pytest.mark.asyncio
async def test_handler_prefixes_untrusted_notice_and_markdown(monkeypatch):
    source = (
        "<html><head><title>Safe</title></head>"
        "<body><h1>Heading</h1><p>Body</p></body></html>"
    )
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert payload["title"] == "Safe"
    assert payload["truncated"] is False
    assert payload["text"].startswith(_UNTRUSTED_CONTENT_NOTICE)
    assert "# Heading" in payload["text"]
    assert "Body" in payload["text"]


@pytest.mark.asyncio
async def test_handler_appends_truncation_footer(monkeypatch):
    source = "<p>" + "word " * 20000 + "</p>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert payload["truncated"] is True
    assert payload["text"].endswith(_TRUNCATION_FOOTER)


@pytest.mark.asyncio
async def test_handler_conversion_failure_returns_omitted_marker(monkeypatch):
    source = "<div>" * 600 + "deep" + "</div>" * 600
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert OMITTED in payload["text"]
    assert payload["text"].startswith(_UNTRUSTED_CONTENT_NOTICE)
    assert "<div>" not in payload["text"]


@pytest.mark.asyncio
async def test_handler_cache_hit_skips_network_and_returns_identical_text(monkeypatch):
    source = (
        "<html><head><title>Safe</title></head>"
        "<body><h1>Heading</h1><p>Body</p></body></html>"
    )
    calls: list[str] = []

    def fetch(url: str, *_args) -> tuple[str, str]:
        calls.append(url)
        return url, source

    monkeypatch.setattr(web_tools, "_fetch_url", fetch)

    first = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))
    second = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert calls == ["https://example.com"]  # 第二次没走网络
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["text"] == first["text"]
    assert second["title"] == first["title"]


@pytest.mark.asyncio
async def test_handler_cache_isolated_by_url(monkeypatch):
    source = "<html><title>Safe</title><body>Body</body></html>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    await web_tools.handle_web_extract({"url": "https://example.com/a"})

    calls: list[str] = []

    def counting_fetch(url: str, *_args) -> tuple[str, str]:
        calls.append(url)
        return url, source

    monkeypatch.setattr(web_tools, "_fetch_url", counting_fetch)
    await web_tools.handle_web_extract({"url": "https://example.com/b"})

    assert calls == ["https://example.com/b"]  # 同 host 不同 path 不共用缓存


@pytest.mark.asyncio
async def test_handler_under_budget_result_is_not_persisted(monkeypatch):
    source = "<html><title>Safe</title><body><p>Body</p></body></html>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert payload["cached"] is False
    assert "full_text_path" not in payload
    assert "content_chars" not in payload
    assert "tool-results" not in payload["text"]


@pytest.mark.asyncio
async def test_handler_over_budget_persists_full_text_and_hints_path(monkeypatch):
    # ~80k 字符源码（不触发 100k 源码截断），渲染后正文 > 40k 内联预算
    source = "<p>STARTMARK " + "fill " * 16000 + " ENDMARK</p>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert payload["truncated"] is True
    path = payload["full_text_path"]
    assert payload["text"].index(path) < 400  # 提示行在正文前部，overflow 兜底剪不掉
    with open(path, encoding="utf-8") as fh:
        full_text = fh.read()
    assert payload["content_chars"] == len(full_text)
    assert "省略" in payload["text"]
    assert "省略 0 字符" not in payload["text"]


@pytest.mark.asyncio
async def test_handler_middle_truncation_keeps_head_and_tail(monkeypatch):
    source = "<p>STARTMARK " + "fill " * 16000 + " ENDMARK</p>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert "STARTMARK" in payload["text"]  # 头部保留
    assert "ENDMARK" in payload["text"]  # 尾部保留


@pytest.mark.asyncio
async def test_handler_over_budget_result_respects_stage6_cap(monkeypatch):
    # 换行密集页：48k 正文 JSON 转义后 > 50k，预算自收敛必须兜住
    source = "<p>ab</p>" * 12000
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    raw = await web_tools.handle_web_extract({"url": "https://example.com"})

    assert len(raw) <= 50_000
    assert "--- 预览（开头" not in raw  # 未被 Stage-6 二次截断


@pytest.mark.asyncio
async def test_handler_persist_failure_degrades_to_inline_truncation(monkeypatch):
    source = "<p>STARTMARK " + "fill " * 16000 + " ENDMARK</p>"
    monkeypatch.setattr(web_tools, "_fetch_url", lambda url, *_a: (url, source))

    def boom(tool_call_id: str, content: str):
        raise OSError("disk full")

    monkeypatch.setattr(web_tools, "persist_tool_result", boom)

    payload = json.loads(await web_tools.handle_web_extract({"url": "https://example.com"}))

    assert payload["truncated"] is True
    assert "full_text_path" not in payload
    assert "tool-results" not in payload["text"]
    assert "STARTMARK" in payload["text"] and "ENDMARK" in payload["text"]


def test_truncate_middle_short_text_unchanged():
    text = "hello world"
    assert _truncate_middle(text, 100) == text
    assert _truncate_middle(text, len(text)) == text


def test_truncate_middle_is_idempotent_and_within_budget():
    text = "x" * 100_000
    out = _truncate_middle(text, _MAX_INLINE_CHARS)

    assert len(out) <= _MAX_INLINE_CHARS
    assert _truncate_middle(out, _MAX_INLINE_CHARS) == out  # 幂等
    assert out.startswith("x") and out.endswith("x")  # 保头尾
