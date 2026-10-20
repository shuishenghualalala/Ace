"""后台子任务完成通知：待注入队列 + PROMPT 阶段 Context Contributor + 四态投递路由。

run_in_background 启动的子任务完成时，结果先经 ``SubagentNotificationRouter``
按父级状态路由（已消失→丢弃；拆除中→只入队；空闲→入队等恢复轮 drain；
忙→入队并 steer 进当前轮，steer 成功即摘除队列副本保证恰好一次）；主
agent / team member 下一轮运行时由 PROMPT 阶段 contributor 一次性 drain 并
拼成模型可见的 reminder。模型主动 collect_subagent 取走的结果会从队列摘除，
避免下一轮重复注入。

三条硬不变量（有回归测试锁死）：每会话恰好注入一次、collect 后不再注入、
文案逐字稳定。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from crew.core.envelope import Envelope
from crew.core.runctx import normalize_owner_account_id
from crew.features.context import ContextContribution
from crew.state.logging import get_logger

log = get_logger("subagent.notify")

# 每 (owner, session) 待注入结果上限，防无限堆积
MAX_PENDING_PER_KEY = 20

OwnerSessionKey = tuple[str, str]


class ParentDeliveryState(str, Enum):
    """Router classification of the parent session at settlement time."""

    GONE = "gone"          # 父会话已消失：静默丢弃并记诊断
    DRAINING = "draining"  # 宿主拆除中：只入队，不唤醒
    BUSY = "busy"          # 父本轮运行中：入队 + steer 并入当前轮
    IDLE = "idle"          # 父空闲：入队，恢复回合路径负责唤醒


class SubagentNotificationRouter:
    """四态投递路由：入队与 steer 共用同一条内容通道（格式化文案）。

    busy 态 steer 成功即从队列摘除同一份内容——正文只经一条通道进入上下文，
    不会"既在 steer 里、又在队列里"双份注入。
    """

    def __init__(self, queue: SubagentNotificationQueue) -> None:
        self._queue = queue

    @property
    def queue(self) -> SubagentNotificationQueue:
        return self._queue

    def deliver(
        self,
        session_id: str,
        result: dict[str, Any],
        *,
        state: ParentDeliveryState,
        steer: Callable[[str], bool] | None = None,
    ) -> str:
        """投递一份完成结果；返回动作名（dropped/enqueued/steered）。"""

        if state is ParentDeliveryState.GONE:
            log.info(
                "父会话 %s 已消失，丢弃后台子任务完成通知 task_id=%s",
                session_id,
                result.get("task_id", ""),
            )
            return "dropped"
        self._queue.enqueue(session_id, result)
        if state is ParentDeliveryState.BUSY and steer is not None:
            block = format_subagent_notifications([result])
            if block and steer(block):
                task_id = str(result.get("task_id") or "")
                if task_id:
                    self._queue.remove(
                        session_id,
                        task_id,
                        str(result.get("owner_account_id") or ""),
                    )
                return "steered"
        return "enqueued"


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
        if r.get("source") == "subagent-settled":
            settlement = r.get("settlement")
            if not isinstance(settlement, dict):
                settlement = {}
            stop_reason = settlement.get("stop_reason") or r.get("stop_reason", "")
            agent_message = str(r.get("agent_message") or "").strip()
            if not agent_message:
                agent_message = "（子智能体未提供自述）"
            lines.append(
                f"\n## [{agent}] status={status} 用时={dur}s"
                f" stop_reason={stop_reason}\n{agent_message}"
            )
            continue
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
