"""context.resolve_browser_tab_references（@browser_tab: 发送时解析注入）测试。"""

from __future__ import annotations

from crew.agent.runtime import _format_browser_tab_references
from crew.gateway.context import (
    _BROWSER_TAB_TEXT_LIMIT,
    resolve_browser_tab_references,
)
from tests.gateway.conftest import make_browser_manager


async def test_resolve_injects_title_url_and_text():
    manager = make_browser_manager({"title": "文档", "url": "https://example.com/a", "text": "正文"})

    refs = await resolve_browser_tab_references(
        "总结一下 @browser_tab:s0123-1 的内容",
        manager=manager,
        owner_account_id="dev:dev",
        session_id="session",
    )

    assert refs == [{
        "tab_id": "s0123-1",
        "title": "文档",
        "url": "https://example.com/a",
        "text": "正文",
    }]


async def test_resolve_truncates_long_text():
    manager = make_browser_manager({"title": "", "url": "", "text": "x" * 9000})

    refs = await resolve_browser_tab_references(
        "@browser_tab:s0123-1",
        manager=manager,
        owner_account_id="dev:dev",
        session_id="session",
    )

    assert len(refs[0]["text"]) == _BROWSER_TAB_TEXT_LIMIT


async def test_resolve_missing_tab_returns_placeholder_without_raising():
    manager = make_browser_manager({}, tabs=())

    refs = await resolve_browser_tab_references(
        "看看 @browser_tab:s0123-1",
        manager=manager,
        owner_account_id="dev:dev",
        session_id="session",
    )

    assert refs[0]["tab_id"] == "s0123-1"
    assert "标签页不存在或已关闭" in refs[0]["error"]


async def test_resolve_without_manager_returns_placeholder():
    refs = await resolve_browser_tab_references(
        "@browser_tab:s0123-1",
        manager=None,
        owner_account_id="dev:dev",
        session_id="session",
    )

    assert refs[0]["error"] == "Browser Use 未启用"


async def test_resolve_without_token_returns_empty():
    assert await resolve_browser_tab_references(
        "没有引用",
        manager=None,
        owner_account_id="dev:dev",
        session_id="session",
    ) == []
    # 行中紧贴文字的 @ 不算 token（与 @file: 的边界规则一致）
    assert await resolve_browser_tab_references(
        "邮件 someone@browser_tab:s0123-1 无效",
        manager=None,
        owner_account_id="dev:dev",
        session_id="session",
    ) == []


async def test_resolve_dedupes_repeated_tab_ids():
    manager = make_browser_manager({"title": "", "url": "", "text": "正文"})

    refs = await resolve_browser_tab_references(
        "@browser_tab:s0123-1 再看 @browser_tab:s0123-1",
        manager=manager,
        owner_account_id="dev:dev",
        session_id="session",
    )

    assert len(refs) == 1


def test_format_browser_tab_references_renders_content_and_placeholder():
    block = _format_browser_tab_references([
        {"tab_id": "a", "title": "文档", "url": "https://example.com/a", "text": "正文"},
        {"tab_id": "b", "error": "标签页不存在或已关闭"},
    ])

    assert "用户引用的浏览器标签页" in block
    assert "## 文档" in block
    assert "URL: https://example.com/a" in block
    assert "正文" in block
    assert "（浏览器标签页内容不可用：标签页不存在或已关闭）" in block
    assert _format_browser_tab_references([]) == ""
    assert _format_browser_tab_references("not-a-list") == ""
