"""web_extract 清洗/markdown 化/分层截断。"""

from __future__ import annotations

import json

import pytest

from crew.tools import web_tools
from crew.tools.web_extract_markdown import (
    MAX_NESTING_DEPTH,
    OMITTED,
    render_html,
)
from crew.tools.web_tools import _TRUNCATION_FOOTER, _UNTRUSTED_CONTENT_NOTICE


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
