"""Wiki 知识库摘要生成与缓存测试。"""

from __future__ import annotations

import pytest

from crew.core.mocks import FakeProvider
from crew.core.types import Message
from crew.wiki import HomeIntro, KBSummary, KnowledgeBase, WikiPage
from crew.wiki.store import FileSystemWikiStore
from crew.wiki.summary import WikiSummarizer, _EMPTY_SUMMARY_TEXT


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def store(tmp_path):
    return FileSystemWikiStore(base_dir=tmp_path)


@pytest.fixture
def summarizer(store, provider):
    return WikiSummarizer(store, provider)


def test_kb_summary_schema_roundtrip():
    s = KBSummary(
        summary="hello",
        page_count=3,
        source_count=1,
        content_hash="abc",
        generated_at=123.0,
        status="ready",
    )
    data = s.to_dict()
    restored = KBSummary.from_dict(data)
    assert restored.summary == "hello"
    assert restored.page_count == 3
    assert restored.status == "ready"


def test_knowledge_base_schema_roundtrip():
    kb = KnowledgeBase(
        id="default",
        name="默认",
        created_at=1.0,
        updated_at=2.0,
        summary=KBSummary(summary="s", page_count=1, status="ready"),
    )
    data = kb.to_dict()
    restored = KnowledgeBase.from_dict(data)
    assert restored.summary.summary == "s"
    assert restored.summary.page_count == 1


@pytest.mark.asyncio
async def test_empty_kb_returns_empty_summary(summarizer, store):
    summary = await summarizer.generate_kb_summary("", "default")
    assert summary.status == "empty"
    assert summary.summary == _EMPTY_SUMMARY_TEXT
    # 元数据已写入
    cached = store.get_kb_summary("", "default")
    assert cached.status == "empty"


@pytest.mark.asyncio
async def test_summary_generation_caches_result(summarizer, store, provider):
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="测试主题",
            content="这是测试主题的内容，用来验证摘要生成。",
            file_path="",
        ),
        "",
        "default",
    )
    summary = await summarizer.generate_kb_summary("", "default")
    assert summary.status == "ready"
    assert summary.page_count == 1
    assert summary.summary.startswith("[fake]")
    assert summary.content_hash

    # 再次调用应直接返回缓存，不增加 provider 调用次数
    calls_before = len(provider.calls)
    cached = await summarizer.generate_kb_summary("", "default")
    assert cached.status == "ready"
    assert len(provider.calls) == calls_before


@pytest.mark.asyncio
async def test_page_change_triggers_refresh(summarizer, store, provider):
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="旧主题",
            content="旧内容。",
            file_path="",
        ),
        "",
        "default",
    )
    await summarizer.generate_kb_summary("", "default")
    calls_after_first = len(provider.calls)

    # 新增页面，应触发重新生成
    store.save_page(
        WikiPage(
            id="topic_2",
            page_type="topic",
            title="新主题",
            content="新内容。",
            file_path="",
        ),
        "",
        "default",
    )
    second = await summarizer.generate_kb_summary("", "default")
    assert second.page_count == 2
    assert len(provider.calls) == calls_after_first + 1


@pytest.mark.asyncio
async def test_force_regenerates_even_when_unchanged(summarizer, store, provider):
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="主题",
            content="内容。",
            file_path="",
        ),
        "",
        "default",
    )
    await summarizer.generate_kb_summary("", "default")
    calls_after_first = len(provider.calls)
    await summarizer.generate_kb_summary("", "default", force=True)
    assert len(provider.calls) == calls_after_first + 1


@pytest.mark.asyncio
async def test_source_count_excludes_unparsed_raws(summarizer, store, provider):
    """pending/failed 的 raw 不会编译成页面，source_count 只统计 parsed。"""
    from crew.wiki.schemas import RawSource

    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="主题",
            content="内容。",
            file_path="",
        ),
        "",
        "default",
    )
    store.save_raw(
        RawSource(id="s1", title="已解析.pdf", source_type="upload", parsed_path="x", parse_status="parsed"),
        "",
        "default",
    )
    store.save_raw(
        RawSource(id="s2", title="待解析.pdf", source_type="upload", parsed_path="", parse_status="pending"),
        "",
        "default",
    )
    store.save_raw(
        RawSource(id="s3", title="解析失败.pdf", source_type="upload", parsed_path="", parse_status="failed"),
        "",
        "default",
    )
    summary = await summarizer.generate_kb_summary("", "default")
    assert summary.source_count == 1


@pytest.mark.asyncio
async def test_maybe_refresh_is_safe_when_provider_fails(store):
    class FailingProvider(FakeProvider):
        async def chat(self, messages: list[Message], tools=None, *, max_tokens=None):
            raise RuntimeError("boom")

    summarizer = WikiSummarizer(store, FailingProvider())
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="主题",
            content="内容。",
            file_path="",
        ),
        "",
        "default",
    )
    await summarizer.maybe_refresh("", "default")
    cached = store.get_kb_summary("", "default")
    assert cached.status == "stale"


# --------------------------------------------------------------------------- #
# Home.md 导读
# --------------------------------------------------------------------------- #


def test_home_intro_schema_roundtrip():
    intro = HomeIntro(text="导读", content_hash="h", generated_at=1.0, status="ready")
    restored = HomeIntro.from_dict(intro.to_dict())
    assert restored.text == "导读"
    assert restored.content_hash == "h"
    assert restored.status == "ready"


@pytest.mark.asyncio
async def test_home_intro_empty_kb(summarizer, store, provider):
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert intro.status == "empty"
    assert changed is False
    assert not provider.calls


@pytest.mark.asyncio
async def test_home_intro_generates_and_caches(summarizer, store, provider):
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="测试主题",
            content="这是测试主题的内容。",
            file_path="",
        ),
        "",
        "default",
    )
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is True
    assert intro.status == "ready"
    assert intro.text.startswith("[fake]")
    assert intro.content_hash
    # 缓存已写入 .kb.json
    assert store.get_home_intro("", "default").text == intro.text

    # 内容 hash 未变：直接返回缓存，不再调用 LLM
    calls_before = len(provider.calls)
    again, changed = await summarizer.generate_home_intro("", "default")
    assert changed is False
    assert again.text == intro.text
    assert len(provider.calls) == calls_before


@pytest.mark.asyncio
async def test_home_intro_regenerates_on_content_change(summarizer, store, provider):
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="旧主题",
            content="旧内容。",
            file_path="",
        ),
        "",
        "default",
    )
    await summarizer.generate_home_intro("", "default")
    calls_after_first = len(provider.calls)

    store.save_page(
        WikiPage(
            id="topic_2",
            page_type="topic",
            title="新主题",
            content="新内容。",
            file_path="",
        ),
        "",
        "default",
    )
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is True
    assert len(provider.calls) == calls_after_first + 1


@pytest.mark.asyncio
async def test_home_intro_is_safe_when_provider_fails(store):
    class FailingProvider(FakeProvider):
        async def chat(self, messages: list[Message], tools=None, *, max_tokens=None):
            raise RuntimeError("boom")

    summarizer = WikiSummarizer(store, FailingProvider())
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="主题",
            content="内容。",
            file_path="",
        ),
        "",
        "default",
    )
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is False
    assert intro.status == "empty"
