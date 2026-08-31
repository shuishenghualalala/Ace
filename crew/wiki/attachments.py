"""专用 Wiki Agent 隐藏上下文消息。"""

from __future__ import annotations

from typing import Any, Literal

from crew.core.types import Message

from .manager import WikiSessionManager
from .prompts import WIKI_AGENT_CONTEXT_REMINDER

WikiAttachmentType = Literal[
    "wiki_agent_context",
    "wiki_ingest",
]


def create_wiki_attachment_message(
    attachment_type: WikiAttachmentType,
    content: str,
    *,
    data: dict | None = None,
) -> Message:
    """创建一条 Wiki 隐藏附件消息。"""
    msg = Message.system_reminder(content)
    msg.attachment_type = attachment_type
    msg.attachment_data = data or {}
    return msg


def _build_wiki_agent_context_reminder(
    session_id: str,
    manager: WikiSessionManager,
    store: Any,
    *,
    owner_account_id: str | None = None,
) -> str:
    """生成 Wiki Agent 专用的上下文提醒，包含活跃 KB 和全部 KB 列表。"""
    owner = owner_account_id or ""
    kb_id = manager.get_kb_id(session_id, owner_account_id=owner)
    # 一次获取全部 KB 列表，同时用于找活跃 KB 名称和构建 KB 列表摘要
    try:
        kbs = store.list_kbs(owner_account_id=owner)
    except Exception:
        kbs = []
    # 查找活跃 KB 名称
    kb_name = kb_id
    for kb in kbs:
        if kb.id == kb_id:
            kb_name = kb.name or kb_id
            break
    # 构建 KB 列表摘要
    if kbs:
        kb_list = "\n".join(
            f"  - {kb.id} ({kb.name or kb.id}): "
            f"{store.count_pages(owner_account_id=owner, kb_id=kb.id)} pages, "
            f"{len(store.list_raws(owner_account_id=owner, kb_id=kb.id))} sources"
            for kb in kbs[:20]
        )
    else:
        kb_list = "  (无)"
    return WIKI_AGENT_CONTEXT_REMINDER.format(
        active_kb_id=kb_id,
        active_kb_name=kb_name,
        kb_list=kb_list,
    )


def get_wiki_agent_attachment_messages(
    session_id: str,
    manager: WikiSessionManager,
    *,
    owner_account_id: str | None = None,
) -> list[Message]:
    """返回专用 Wiki Agent 本轮需要注入的知识库上下文。"""
    store = getattr(manager, "store", None)
    if store is None:
        return []
    return [
        create_wiki_attachment_message(
            "wiki_agent_context",
            _build_wiki_agent_context_reminder(
                session_id, manager, store, owner_account_id=owner_account_id
            ),
            data={"reminderType": "agent_context"},
        )
    ]
