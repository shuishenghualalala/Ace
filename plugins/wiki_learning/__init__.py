"""Pluggable Wiki learning coach for Ace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from crew.core.runctx import current_owner_account_id
from crew.wiki._utils import is_wiki_agent_session
from crew.wiki.store._filesystem import FileSystemWikiStore
from crew.wiki.store._ids import normalize_kb_id

from . import context
from .store import WikiLearningStore
from .tools import ACTIVITY_SCHEMA, ASSESS_SCHEMA, STATE_SCHEMA, WikiLearningTools

_LEARNING_INTENT = re.compile(
    r"(?:学习|复习|整理重点|知识点|考考我|出题|测验|测试|刷题|错题|闪卡|记忆卡|面试|模拟面试|"
    r"quiz|flashcard|interview|study|learn|review|practice|test\s+me)",
    re.IGNORECASE,
)


def _skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, separator, rest = text[3:].partition("---")
        if separator:
            return rest.strip()
    return text.strip()


def register(ctx: Any) -> None:
    config = ctx.services.get("config")
    if config is None:
        raise RuntimeError("wiki_learning 插件缺少 config 服务")

    store = WikiLearningStore(config.db_path, wal_enabled=config.sqlite_wal)
    wiki_store = FileSystemWikiStore(storage_root=config.wiki.storage.resolved_root())
    tools = WikiLearningTools(store, wiki_store)
    skill_path = Path(__file__).parent / "skills" / "crew-wiki-learning-coach" / "SKILL.md"
    skill_context = _skill_body(skill_path)

    def pre_llm_call(session_id: str, messages: list[Any]) -> str | None:
        context.capture_turn(messages)
        if not is_wiki_agent_session(session_id):
            return None
        latest = context.latest_user_text()
        should_activate = bool(_LEARNING_INTENT.search(latest))
        owner = current_owner_account_id.get().strip()
        kb_id = context.active_kb_id().strip()
        if not should_activate and owner and kb_id:
            try:
                should_activate = (
                    store.active_episode(owner, session_id, normalize_kb_id(kb_id)) is not None
                )
            except ValueError:
                should_activate = False
        return skill_context if should_activate else None

    ctx.register_tool(
        name="wiki_learning_state",
        toolset="wiki.manage",
        schema=STATE_SCHEMA,
        handler=tools.state,
        emoji="🎯",
        display_name="学习计划",
        ui_label_template="{action} 学习计划",
        search_hint="wiki study learning review quiz interview mastery plan",
    )
    ctx.register_tool(
        name="wiki_learning_activity",
        toolset="wiki.manage",
        schema=ACTIVITY_SCHEMA,
        handler=tools.activity,
        emoji="🧠",
        display_name="学习活动",
        ui_label_template="{action} 学习活动",
        search_hint="wiki quiz question interview flashcard practice activity",
    )
    ctx.register_tool(
        name="wiki_learning_assess",
        toolset="wiki.manage",
        schema=ASSESS_SCHEMA,
        handler=tools.assess,
        emoji="✅",
        display_name="评估回答",
        ui_label_template="评估学习回答",
        search_hint="wiki assess answer score feedback mastery",
    )
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_skill_root("skills")

    def dispose() -> None:
        wiki_store.close()
        store.close()

    ctx.register_disposer(dispose)
