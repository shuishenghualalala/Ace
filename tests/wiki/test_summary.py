"""Wiki Home.md 导读生成与缓存测试。"""

from __future__ import annotations

import pytest

from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, Message
from crew.wiki import HomeIntro, KnowledgeBase, WikiPage
from crew.wiki.store import FileSystemWikiStore
from crew.wiki.summary import (
    WikiSummarizer,
    _HOME_INTRO_PROMPT,
    _HOME_QUESTIONS_MARKER,
    _split_home_intro,
)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def store(tmp_path):
    return FileSystemWikiStore(base_dir=tmp_path)


@pytest.fixture
def summarizer(store, provider):
    return WikiSummarizer(store, provider)


class _FailingProvider(FakeProvider):
    async def chat(self, messages: list[Message], tools=None, *, max_tokens=None):
        raise RuntimeError("boom")


def _save_topic_page(store, title, content, page_id="topic_1"):
    """往 default KB 写入一个 topic 页面（intro 用例的公共前置数据）。"""
    store.save_page(
        WikiPage(
            id=page_id,
            page_type="topic",
            title=title,
            content=content,
            file_path="",
        ),
        "",
        "default",
    )


def test_knowledge_base_schema_roundtrip():
    kb = KnowledgeBase(
        id="default",
        name="默认",
        created_at=1.0,
        updated_at=2.0,
    )
    data = kb.to_dict()
    restored = KnowledgeBase.from_dict(data)
    assert restored.id == "default"
    assert restored.name == "默认"
    assert restored.created_at == 1.0


def test_home_intro_prompt_focuses_on_content_instead_of_follow_up_suggestions():
    prompt = _HOME_INTRO_PROMPT
    assert "不要建议用户继续问什么" in prompt or "不要提供建议追问" in prompt
    assert "不要说明引用" in prompt or "不要说明引用了哪些页面" in prompt
    assert "示例问题或后续操作建议" in prompt or "示例问题或操作建议" in prompt


# --------------------------------------------------------------------------- #
# Home.md 导读
# --------------------------------------------------------------------------- #


def test_home_intro_schema_roundtrip():
    intro = HomeIntro(
        text="导读",
        questions=["问题一？", "问题二？"],
        content_hash="h",
        generated_at=1.0,
        status="ready",
    )
    restored = HomeIntro.from_dict(intro.to_dict())
    assert restored.text == "导读"
    assert restored.questions == ["问题一？", "问题二？"]
    assert restored.content_hash == "h"
    assert restored.status == "ready"
    # 旧版缓存没有 questions 字段：兼容为空列表
    legacy = HomeIntro.from_dict({"text": "旧", "content_hash": "h", "status": "ready"})
    assert legacy.questions == []


def test_split_home_intro_parses_questions():
    raw = (
        "这是一段导读。\n\n"
        f"{_HOME_QUESTIONS_MARKER}\n"
        "1. 如何配置多智能体团队？\n"
        "- SubAgent 和 Agent Teams 有什么区别？\n"
        "“Token 消耗过快怎么办？”\n"
        "再多一条也不会收录\n"
    )
    intro, questions = _split_home_intro(raw)
    assert intro == "这是一段导读。"
    assert questions == [
        "如何配置多智能体团队？",
        "SubAgent 和 Agent Teams 有什么区别？",
        "Token 消耗过快怎么办？",
    ]
    # 没有分隔符：整段视为导读，问题为空
    intro_only, no_questions = _split_home_intro("只有导读，没有分隔符。")
    assert intro_only == "只有导读，没有分隔符。"
    assert no_questions == []


@pytest.mark.asyncio
async def test_home_intro_empty_kb(summarizer, store, provider):
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert intro.status == "empty"
    assert changed is False
    assert not provider.calls


@pytest.mark.asyncio
async def test_home_intro_generates_and_caches(summarizer, store, provider):
    _save_topic_page(store, "测试主题", "这是测试主题的内容。")
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
async def test_home_intro_generates_questions_from_marked_output(store):
    provider = FakeProvider(
        script=[
            ChatResponse(
                text=(
                    "这个知识库聚焦多智能体协作。\n\n"
                    f"{_HOME_QUESTIONS_MARKER}\n"
                    "如何从零配置一个多智能体团队？\n"
                    "SubAgent 和 Agent Teams 有什么区别？\n"
                    "如何降低 Token 消耗？\n"
                )
            )
        ]
    )
    summarizer = WikiSummarizer(store, provider)
    _save_topic_page(store, "测试主题", "这是测试主题的内容。")
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is True
    assert intro.text == "这个知识库聚焦多智能体协作。"
    assert intro.questions == [
        "如何从零配置一个多智能体团队？",
        "SubAgent 和 Agent Teams 有什么区别？",
        "如何降低 Token 消耗？",
    ]
    # 缓存里的 questions 也一并持久化
    assert store.get_home_intro("", "default").questions == intro.questions


@pytest.mark.asyncio
async def test_home_intro_regenerates_on_content_change(summarizer, store, provider):
    _save_topic_page(store, "旧主题", "旧内容。")
    await summarizer.generate_home_intro("", "default")
    calls_after_first = len(provider.calls)

    _save_topic_page(store, "新主题", "新内容。", page_id="topic_2")
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is True
    assert len(provider.calls) == calls_after_first + 1


@pytest.mark.asyncio
async def test_home_intro_regenerates_when_cached_prompt_version_is_old(summarizer, store, provider):
    _save_topic_page(store, "测试主题", "这是测试主题的内容。")
    store.set_home_intro(
        HomeIntro(
            text="旧版导读",
            content_hash="legacy-content-hash",
            generated_at=1.0,
            status="ready",
        ),
        "",
        "default",
    )

    intro, changed = await summarizer.generate_home_intro("", "default")

    assert changed is True
    assert intro.text.startswith("[fake]")
    assert intro.content_hash != "legacy-content-hash"


@pytest.mark.asyncio
async def test_home_intro_is_safe_when_provider_fails(store):
    summarizer = WikiSummarizer(store, _FailingProvider())
    _save_topic_page(store, "主题", "内容。")
    intro, changed = await summarizer.generate_home_intro("", "default")
    assert changed is False
    assert intro.status == "empty"
