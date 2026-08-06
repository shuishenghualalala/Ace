"""Wiki 知识库摘要生成与缓存。

summary 作为 KnowledgeBase 元数据的一部分持久化到 .kb.json，
Wiki 页面读取时无需实时调用 LLM。
文档上传 / ingest / compile 完成后后台评估是否需要刷新。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any, Callable

from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.state.logging import get_logger

from .schemas import HomeIntro, KBSummary, WikiPage
from .store import WikiStore
from ._llm import chat_text

log = get_logger("wiki.summary")

# 内容截断长度，控制 prompt 大小
_SUMMARY_PAGE_SNIPPET_CHARS = 800
# 最多取 N 个页面生成摘要
_SUMMARY_MAX_PAGES = 30
# 摘要状态 TTL（秒），超过认为陈旧
_SUMMARY_TTL_SECONDS = 24 * 3600
# 生成规则变化时更新版本，使旧缓存按新提示词自动失效。
_SUMMARY_PROMPT_VERSION = "2026-07-31-home-questions-v1"

_SUMMARY_PROMPT = """你是一位知识库整理专家。请根据下面 Wiki 知识库的页面列表与内容片段，
用 180-300 字中文生成一段整体摘要，让读者快速理解这个知识库讲了什么、解决什么问题。

摘要应包含：
1. 知识库主题/核心内容。
2. 主要内容板块及其关系。
3. 最值得关注的知识、结论或实际价值。

写作限制：
- 不要提供建议追问、示例问题或操作建议。
- 不要说明引用了哪些页面、文件或来源，也不要罗列页面标题。
- 不要写“欢迎打开”“你可以”等产品引导语。
- 使用紧凑、客观、自然的说明文字，避免堆砌细节。

知识库页面（共 {page_count} 个页面，{source_count} 个来源）：
---
{context}
---

请直接输出摘要文本，不要 JSON、不要 markdown 代码块、不要标题编号。"""

_EMPTY_SUMMARY_TEXT = "这个知识库还没有页面。上传文档或粘贴文字后，AI 会自动整理并生成摘要。"

# Home.md「内容导读」：与 KBSummary 相互独立，专为首页撰写，
# 只在页面/来源内容 hash 变化时重新生成（见 generate_home_intro）。
_HOME_INTRO_PROMPT = """你是一位知识库策展人。请为知识库「{kb_name}」写一段首页内容导读，
帮助第一次打开这个库的读者迅速建立整体认知。

根据下方页面清单与内容片段，写 180-320 字中文导读，要求：
1. 开头直接点明知识库覆盖的领域、目标和核心价值；
2. 将内容归纳为 2-4 个彼此连贯的板块，概括各板块的重点；
3. 提炼最值得关注的知识、结论或可复用信息；
4. 使用自然、客观、紧凑的说明文字，不要分点列表、不要标题、不要 emoji。

导读写作限制：
- 导读正文里不要建议用户继续问什么，不要给示例问题或后续操作建议；
- 不要说明引用、参考或整合了哪些页面、文件、素材和来源；
- 不要使用 [[页面标题]] 或其他链接，不要逐个罗列页面名称；
- 不要写“欢迎打开”“你可以”等产品引导语。

导读之后，再给出 3 个推荐问题，帮助读者基于这个知识库继续提问，要求：
- 问题必须结合本库的具体内容（涉及库里的主题、概念或结论），不要泛泛的“主要内容是什么”；
- 每个问题一句话，15-40 字，以问号结尾；
- 每个问题单独一行，不要编号、不要引号、不要任何前后缀。

知识库页面（共 {page_count} 个页面，{source_count} 个来源）：
---
{context}
---

输出格式（严格遵守）：
先输出导读文本，然后单独一行输出分隔符 {questions_marker}，再逐行输出 3 个推荐问题。
不要 JSON、不要 markdown 代码块、不要标题编号。"""

# 导读与推荐问题之间的分隔符（见 _HOME_INTRO_PROMPT 输出格式约定）。
_HOME_QUESTIONS_MARKER = "---推荐问题---"
# 推荐问题数量与长度约束（解析时兜底过滤）。
_HOME_QUESTIONS_COUNT = 3
_HOME_QUESTION_MAX_CHARS = 60


def _split_home_intro(raw: str) -> tuple[str, list[str]]:
    """把 LLM 输出拆成 (导读文本, 推荐问题列表)。

    没有分隔符时整段视为导读、问题为空；问题行做保守清洗
    （去编号/引号/空行，限长限量），不合格的输出不至于污染首页。
    """
    text, sep, tail = raw.partition(_HOME_QUESTIONS_MARKER)
    intro = text.strip()
    if not sep:
        return intro, []
    questions: list[str] = []
    for line in tail.splitlines():
        q = line.strip().strip('"“”\'').strip()
        q = re.sub(r"^[-*\d]+[.、)）]?\s*", "", q).strip()
        if not q or len(q) > _HOME_QUESTION_MAX_CHARS:
            continue
        questions.append(q)
        if len(questions) >= _HOME_QUESTIONS_COUNT:
            break
    return intro, questions


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 输出可能携带的 markdown 代码围栏。"""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


class WikiSummarizer:
    """生成并缓存知识库级摘要。"""

    def __init__(
        self,
        store: WikiStore,
        provider: LLMProvider,
        provider_for_owner: Callable[[str], LLMProvider] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.provider_for_owner = provider_for_owner
        # 防止同一 KB 并发生成
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _provider_for_owner(self, owner_account_id: str = "") -> LLMProvider:
        resolver = self.provider_for_owner
        if callable(resolver):
            resolved = resolver(str(owner_account_id or ""))
            if resolved is not None:
                return resolved
        return self.provider

    def _key(self, owner_account_id: str, kb_id: str) -> tuple[str, str]:
        return (owner_account_id or "", kb_id or "default")

    def _lock(self, owner_account_id: str, kb_id: str) -> asyncio.Lock:
        key = self._key(owner_account_id, kb_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _compute_content_hash(pages: list[WikiPage], raws: list[Any]) -> str:
        """基于页面标题+类型+截断内容 + source 标题计算稳定 hash。"""
        parts: list[str] = [_SUMMARY_PROMPT_VERSION]
        for page in pages[:_SUMMARY_MAX_PAGES]:
            parts.append(page.title)
            parts.append(page.page_type)
            parts.append(page.content[:_SUMMARY_PAGE_SNIPPET_CHARS])
        for raw in raws:
            parts.append(raw.title)
        joined = "\n".join(parts).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()[:32]

    def _build_context(self, pages: list[WikiPage], raws: list[Any]) -> str:
        """构造给 LLM 的页面上下文。"""
        parts: list[str] = []
        for i, page in enumerate(pages[:_SUMMARY_MAX_PAGES], 1):
            snippet = page.content[:_SUMMARY_PAGE_SNIPPET_CHARS].replace("\n", " ")
            tags = ", ".join(page.tags) if page.tags else "无"
            parts.append(
                f"[{i}] {page.title} ({page.page_type})\n标签: {tags}\n{snippet}"
            )
        if raws:
            parts.append("")
            parts.append("来源文件：")
            for raw in raws:
                parts.append(f"- {raw.title}")
        return "\n\n".join(parts)

    def _should_refresh(
        self,
        current: KBSummary,
        pages: list[WikiPage],
        raws: list[Any],
        force: bool = False,
    ) -> bool:
        """判断是否需要重新生成摘要。"""
        if force:
            return True
        if not pages:
            # 空知识库：如果当前不是 empty 或没有 summary，需要更新为 empty 提示
            return current.status != "empty" or not current.summary
        if current.status in ("generating",):
            return False
        if not current.summary:
            return True
        if current.page_count != len(pages):
            return True
        if current.source_count != len(raws):
            return True
        new_hash = self._compute_content_hash(pages, raws)
        if current.content_hash != new_hash:
            return True
        if current.generated_at and (time.time() - current.generated_at) > _SUMMARY_TTL_SECONDS:
            return True
        return False

    async def generate_kb_summary(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        force: bool = False,
    ) -> KBSummary:
        """生成或刷新知识库摘要，并写回 store。"""
        key = self._key(owner_account_id, kb_id)
        async with self._lock(owner_account_id, kb_id):
            current = self.store.get_kb_summary(owner_account_id, kb_id)
            pages = self.store.list_all(limit=10000, owner_account_id=owner_account_id, kb_id=kb_id)
            # 只统计解析成功的 source：pending/failed 的 raw 不会编译成页面，
            # 前端各视图（原始资料列表/图谱）也看不到，计入会让 summary 的
            # "N 个来源" 与前端实际显示的数量不一致。
            raws = [
                r
                for r in self.store.list_raws(owner_account_id, kb_id)
                if (r.parse_status or "pending") == "parsed"
            ]

            if not self._should_refresh(current, pages, raws, force=force):
                return current

            if not pages:
                empty = KBSummary(
                    summary=_EMPTY_SUMMARY_TEXT,
                    page_count=0,
                    source_count=0,
                    content_hash="",
                    generated_at=time.time(),
                    status="empty",
                )
                self.store.set_kb_summary(empty, owner_account_id, kb_id)
                return empty

            # 标记生成中，避免前端重复请求时再次触发
            generating = KBSummary(
                summary=current.summary,
                page_count=current.page_count,
                source_count=current.source_count,
                content_hash=current.content_hash,
                generated_at=current.generated_at,
                status="generating",
            )
            self.store.set_kb_summary(generating, owner_account_id, kb_id)

            try:
                context = self._build_context(pages, raws)
                prompt = _SUMMARY_PROMPT.format(
                    page_count=len(pages),
                    source_count=len(raws),
                    context=context,
                )
                response_text = await chat_text(
                    self._provider_for_owner(owner_account_id),
                    [Message.user(prompt)],
                )
                summary_text = response_text.strip()
                if not summary_text:
                    summary_text = current.summary or _EMPTY_SUMMARY_TEXT

                new_summary = KBSummary(
                    summary=summary_text,
                    page_count=len(pages),
                    source_count=len(raws),
                    content_hash=self._compute_content_hash(pages, raws),
                    generated_at=time.time(),
                    status="ready",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("生成知识库摘要失败 %s: %s", key, exc)
                # 保留旧摘要，标记为 stale
                new_summary = KBSummary(
                    summary=current.summary or _EMPTY_SUMMARY_TEXT,
                    page_count=len(pages),
                    source_count=len(raws),
                    content_hash=current.content_hash,
                    generated_at=current.generated_at,
                    status="stale",
                )

            self.store.set_kb_summary(new_summary, owner_account_id, kb_id)
            return new_summary

    def get_summary(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> KBSummary:
        """直接读取缓存的摘要（不触发 LLM）。"""
        return self.store.get_kb_summary(owner_account_id, kb_id)

    # ---- Home.md 导读 ----

    def get_home_intro(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> HomeIntro:
        """直接读取缓存的 Home 导读（不触发 LLM）。"""
        return self.store.get_home_intro(owner_account_id, kb_id)

    async def generate_home_intro(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        force: bool = False,
    ) -> tuple[HomeIntro, bool]:
        """生成或刷新 Home.md 导读，返回 (导读, 是否重新生成)。

        只有页面/来源内容 hash 变化（或 force）时才调用 LLM；
        hash 未变直接返回缓存，保证「知识库内容有更新才更新」。
        """
        key = self._key(owner_account_id, kb_id)
        async with self._lock(owner_account_id, kb_id):
            current = self.store.get_home_intro(owner_account_id, kb_id)
            pages = self.store.list_all(limit=10000, owner_account_id=owner_account_id, kb_id=kb_id)
            # 与 generate_kb_summary 一致：只统计解析成功的 source。
            raws = [
                r
                for r in self.store.list_raws(owner_account_id, kb_id)
                if (r.parse_status or "pending") == "parsed"
            ]

            if not pages:
                if current.status != "empty" or current.text:
                    empty = HomeIntro(status="empty", generated_at=time.time())
                    self.store.set_home_intro(empty, owner_account_id, kb_id)
                    return empty, True
                return current, False

            new_hash = self._compute_content_hash(pages, raws)
            if (
                not force
                and current.text
                and current.content_hash == new_hash
                and current.status in ("ready", "stale")
            ):
                return current, False

            try:
                context = self._build_context(pages, raws)
                prompt = _HOME_INTRO_PROMPT.format(
                    kb_name=self._kb_name(owner_account_id, kb_id),
                    page_count=len(pages),
                    source_count=len(raws),
                    context=context,
                    questions_marker=_HOME_QUESTIONS_MARKER,
                )
                raw_text = _strip_code_fence(
                    (
                        await chat_text(
                            self._provider_for_owner(owner_account_id),
                            [Message.user(prompt)],
                        )
                    ).strip()
                )
                text, questions = _split_home_intro(raw_text)
                if not text:
                    return current, False
                intro = HomeIntro(
                    text=text,
                    questions=questions,
                    content_hash=new_hash,
                    generated_at=time.time(),
                    status="ready",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("生成 Home 导读失败 %s: %s", key, exc)
                fallback = HomeIntro(
                    text=current.text,
                    questions=current.questions,
                    content_hash=current.content_hash,
                    generated_at=current.generated_at,
                    status="stale" if current.text else "empty",
                )
                self.store.set_home_intro(fallback, owner_account_id, kb_id)
                return fallback, False

            self.store.set_home_intro(intro, owner_account_id, kb_id)
            return intro, True

    def _kb_name(self, owner_account_id: str, kb_id: str) -> str:
        for kb in self.store.list_kbs(owner_account_id):
            if kb.id == kb_id:
                return kb.name or kb_id
        return kb_id
