"""Turn-local context captured before the Wiki learning tools run."""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Iterable

from crew.core.types import Message

_latest_user_text: ContextVar[str] = ContextVar("wiki_learning_latest_user_text", default="")
_active_kb_id: ContextVar[str] = ContextVar("wiki_learning_active_kb_id", default="")
_KB_PATTERN = re.compile(r"当前活跃知识库（active_kb_id）：\s*([^\s<]+)")


def capture_turn(messages: Iterable[Message]) -> None:
    """Capture private raw user input and the active KB without exposing either as tool args."""
    items = list(messages)
    latest = next(
        (
            str(message.content or "").strip()
            for message in reversed(items)
            if message.role == "user" and not message.is_meta and str(message.content or "").strip()
        ),
        "",
    )
    _latest_user_text.set(latest)

    kb_id = ""
    for message in reversed(items):
        if message.attachment_type != "wiki_agent_context" and "active_kb_id" not in str(
            message.content or ""
        ):
            continue
        data = message.attachment_data if isinstance(message.attachment_data, dict) else {}
        candidate = str(data.get("active_kb_id") or "").strip()
        if not candidate:
            match = _KB_PATTERN.search(str(message.content or ""))
            candidate = match.group(1).strip() if match else ""
        if candidate:
            kb_id = candidate
            break
    _active_kb_id.set(kb_id)


def latest_user_text() -> str:
    return _latest_user_text.get()


def active_kb_id() -> str:
    return _active_kb_id.get()
