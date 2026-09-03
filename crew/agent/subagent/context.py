"""后台子任务完成通知：待注入队列 + PROMPT 阶段 Context Contributor。

run_in_background 启动的子任务完成时，结果先入队（按 (owner, 父 session) 分键、
单键限 20 条防无限堆积）；主 agent / team member 下一轮运行时由 PROMPT 阶段
contributor 一次性 drain 并拼成模型可见的 reminder。模型主动 collect_subagent
取走的结果会从队列摘除，避免下一轮重复注入。
"""

from __future__ import annotations

from typing import Any

from crew.core.envelope import Envelope
from crew.core.runctx import normalize_owner_account_id
from crew.features.context import ContextContribution

# 每 (owner, session) 待注入结果上限，防无限堆积
MAX_PENDING_PER_KEY = 20

OwnerSessionKey = tuple[str, str]


class SubagentNotificationQueue:
    """按 (owner, session) 分键的后台子任务完成结果待注入队列。"""

    def __init__(self) -> None:
        self._pending: dict[OwnerSessionKey, list[dict[str, Any]]] = {}

    @staticmethod
    def _key(session_id: str, owner_account_id: str) -> OwnerSessionKey:
        return normalize_owner_account_id(owner_account_id), session_id

    def enqueue(self, session_id: str, result: dict[str, Any]) -> None:
        """后台子任务完成结果入队（result 需带 owner_account_id）。"""
        owner = normalize_owner_account_id(result.get("owner_account_id"))
        queue = self._pending.setdefault(self._key(session_id, owner), [])
        queue.append(result)
        if len(queue) > MAX_PENDING_PER_KEY:
            del queue[:-MAX_PENDING_PER_KEY]

    def drain(self, session_id: str, owner_account_id: str) -> list[dict[str, Any]]:
        """取出并清空该 session 的待注入结果（每会话恰好注入一次）。"""
        return self._pending.pop(self._key(session_id, owner_account_id), None) or []

    def remove(self, session_id: str, task_id: str, owner_account_id: str) -> None:
        """模型已主动 collect 取走某后台结果 → 摘除，避免下一轮重复注入。"""
        key = self._key(session_id, owner_account_id)
        queue = self._pending.get(key)
        if not queue:
            return
        remaining = [r for r in queue if r.get("task_id") != task_id]
        if remaining:
            self._pending[key] = remaining
        else:
            self._pending.pop(key, None)


def format_subagent_notifications(pending: object) -> str:
    """把后台子任务的完成结果格式化为可注入上下文的 system-reminder 块。"""
    if not isinstance(pending, list) or not pending:
        return ""
    lines = [
        "# 后台子任务完成通知",
        "以下后台子智能体（你之前用 run_agent / delegate_task 的 run_in_background 启动）已完成，结果如下：",
    ]
    for r in pending:
        if not isinstance(r, dict):
            continue
        agent = r.get("agent", "子智能体")
        status = r.get("status", "")
        dur = r.get("duration_seconds", "")
        summary = str(r.get("summary", "")).strip()
        lines.append(f"\n## [{agent}] status={status} 用时={dur}s\n{summary}")
    return "\n".join(lines)


def build_subagent_notification_handler(
    queue: SubagentNotificationQueue,
):
    """构造 PROMPT 阶段 contributor handler：drain 本回合会话的待注入结果。

    drain 键与入队键对齐：team member 回合优先取 member_session_id（入队时
    经 current_subagent_notify_session 落到 member 子会话），主 agent 回合取
    envelope.session_id。上下文预览（preview_context）只读不消费，直接跳过。
    """

    async def _handler(envelope: Envelope) -> ContextContribution | None:
        if envelope.params.get("_context_preview"):
            return None
        notify_session = str(
            envelope.params.get("member_session_id") or envelope.session_id
        )
        block = format_subagent_notifications(
            queue.drain(notify_session, envelope.user_id)
        )
        if not block:
            return None
        return ContextContribution(prompt_parts=(block,))

    return _handler
