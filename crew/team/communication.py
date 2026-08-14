"""Team 通信路由。

这一层只负责把已经通过权限校验的 ``team_mention`` 事件路由到统一的
Team Bus，并调用现有的 TeamManager 回调。它不启动 Agent、不创建任务，
也不改变 TeamPlan；ask 的自动执行由后续 AskCoordinator 阶段接入。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from crew.team.bus import TeamBus
from crew.team.models import MessageType, new_id

MENTION_MESSAGE_TYPES: dict[str, MessageType] = {
    "submit": "result",
    "ask": "decision_request",
    "review": "answer",
    "handoff": "handoff",
    "user_followup": "decision_request",
}


def normalize_mention_targets(raw: Any, *, member_names: list[str]) -> list[str]:
    """Normalize mention targets against the server-side team roster."""

    targets = [raw] if isinstance(raw, str) else list(raw or [])
    allowed = {"leader", "user", "all", *member_names}
    normalized: list[str] = []
    for item in targets:
        target = str(item or "").strip().lstrip("@")
        if target and target in allowed and target not in normalized:
            normalized.append(target)
    return normalized


def expand_mention_targets(targets: list[str], *, member_names: list[str]) -> list[str]:
    """Expand ``all`` while keeping ``user`` outside the Team Bus mailbox."""

    expanded: list[str] = []
    for target in targets:
        if target == "user":
            continue
        candidates = ["leader", *member_names] if target == "all" else [target]
        for candidate in candidates:
            if candidate not in expanded:
                expanded.append(candidate)
    return expanded


class TeamCommunicationRouter:
    """统一 Team mention 的消息投递入口。

    ``on_mention`` 是现有 TeamManager 业务回调。路由器只负责公共通信
    契约，因此内置 Agent 工具和外部 Runtime 入口可以共享同一条路径。
    """

    def __init__(
        self,
        *,
        bus: TeamBus,
        session_id: str,
        member_names: list[str],
        on_mention: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.member_names = list(member_names)
        self.on_mention = on_mention

    async def route(
        self,
        event: dict[str, Any],
        *,
        expanded_targets: list[str] | None = None,
    ) -> Any:
        """投递一次 mention，并交给现有 TeamManager 处理业务语义。"""

        targets = normalize_mention_targets(
            event.get("to"),
            member_names=self.member_names,
        )
        if not targets:
            raise ValueError("to 不能为空，且必须是 leader、user、all 或团队成员")
        event["to"] = targets
        expanded = list(
            expanded_targets
            if expanded_targets is not None
            else expand_mention_targets(targets, member_names=self.member_names)
        )
        event["expanded_to"] = expanded
        intent = str(event.get("intent") or "broadcast").strip()
        event.setdefault("message", None)

        if intent in {"ask", "review"} and not str(event.get("request_id") or "").strip():
            event["request_id"] = new_id("comm")

        if intent != "assign" and expanded:
            message = self.bus.send(
                team_session_id=self.session_id,
                sender_member_id=str(event.get("from") or "agent"),
                recipient_member_ids=expanded,
                content=str(event.get("content") or event.get("text") or ""),
                message_type=MENTION_MESSAGE_TYPES.get(intent, "progress"),
                intent=intent,
                request_id=str(event.get("request_id") or ""),
                task_id=str(event.get("task_id") or ""),
                node_id=str(event.get("node_id") or ""),
                thread_id=str(event.get("thread_id") or ""),
                workflow_run_id=str(event.get("workflow_run_id") or ""),
                artifact_refs=list(event.get("artifact_refs") or []),
                requires_ack=intent in {"ask", "review"},
                priority=int(event.get("priority") or 0),
            )
            event["message"] = message.to_dict()
            event["communication_status"] = "published"
        elif intent == "assign":
            event["communication_status"] = "execution_routed"
        elif intent == "user_followup":
            event["communication_status"] = "followup_routed"

        if self.on_mention is None:
            return {}
        result = self.on_mention(event)
        if inspect.isawaitable(result):
            return await cast(Awaitable[Any], result)
        return result


__all__ = [
    "MENTION_MESSAGE_TYPES",
    "TeamCommunicationRouter",
    "expand_mention_targets",
    "normalize_mention_targets",
]
