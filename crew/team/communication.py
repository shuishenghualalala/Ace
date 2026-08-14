"""Team 通信路由。

这一层只负责把已经通过权限校验的 ``team_mention`` 事件路由到统一的
Team Bus，并调用现有的 TeamManager 回调。它不启动 Agent、不创建任务，
也不改变 TeamPlan；ask 的自动执行由后续 AskCoordinator 阶段接入。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from crew.core.envelope import Envelope, ResponseChunk
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
        ask_coordinator: TeamAskCoordinator | None = None,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.member_names = list(member_names)
        self.on_mention = on_mention
        self.ask_coordinator = ask_coordinator

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
        intent = str(event.get("intent") or "broadcast").strip()
        if intent == "ask" and self.ask_coordinator is not None:
            inherited_path = self.ask_coordinator.active_path_for(
                str(event.get("from") or "").strip()
            )
            if inherited_path and not event.get("communication_path"):
                event["communication_path"] = inherited_path
        expanded = list(
            expanded_targets
            if expanded_targets is not None
            else expand_mention_targets(targets, member_names=self.member_names)
        )
        event["expanded_to"] = expanded
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
            if intent == "ask":
                self.bus.update_status(message.message_id, "waiting_reply")
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
            result = await cast(Awaitable[Any], result)
        if intent == "ask" and self.ask_coordinator is not None:
            answer = await self.ask_coordinator.answer(event)
            event["answer"] = answer
            if isinstance(result, dict) and result:
                return {**result, "answer": answer}
            return answer
        return result


class TeamAskCoordinator:
    """把 ask 消息转换成一次目标 Agent 的临时通信回合。"""

    def __init__(
        self,
        *,
        bus: TeamBus,
        session_id: str,
        resolve_agent: Callable[[str], Any | None],
        owner_account_id: str = "",
        on_chunk: Callable[[str, ResponseChunk], Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.resolve_agent = resolve_agent
        self.owner_account_id = owner_account_id
        self.on_chunk = on_chunk
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_paths: dict[str, tuple[str, ...]] = {}

    def active_path_for(self, member_id: str) -> list[str]:
        return list(self._active_paths.get(str(member_id or "").strip(), ()))

    async def answer(self, event: dict[str, Any]) -> dict[str, Any]:
        targets = [
            str(target or "").strip()
            for target in list(event.get("expanded_to") or [])
            if str(target or "").strip()
        ]
        if len(targets) != 1:
            raise ValueError("team_mention(ask) 必须且只能 @ 一个团队成员")
        target = targets[0]
        sender = str(event.get("from") or "agent").strip() or "agent"
        request_id = str(event.get("request_id") or "").strip()
        request_message = dict(event.get("message") or {})
        request_message_id = str(request_message.get("message_id") or "").strip()
        if not request_id or not request_message_id:
            raise ValueError("ask 请求缺少 request_id 或 message_id")

        path = [
            str(item or "").strip()
            for item in list(event.get("communication_path") or [])
            if str(item or "").strip()
        ]
        if sender not in path:
            path.append(sender)
        if target in path:
            return self._publish_failure(
                event,
                target=target,
                sender=sender,
                request_id=request_id,
                request_message_id=request_message_id,
                reason=f"检测到 Team 通信环路：{' → '.join([*path, target])}",
                status="failed",
            )
        event["communication_path"] = [*path, target]

        agent = self.resolve_agent(target)
        if agent is None:
            return self._publish_failure(
                event,
                target=target,
                sender=sender,
                request_id=request_id,
                request_message_id=request_message_id,
                reason=f"目标成员不可用：{target}",
            )

        turn_session_id = f"{self.session_id}::turn::{request_id}::{target}"
        member_session_id = f"{self.session_id}::{target}"
        owner = str(event.get("owner_account_id") or self.owner_account_id or "local")
        workspace_id = str(event.get("workspace_id") or "default")
        node_id = str(event.get("node_id") or "")
        task_id = str(event.get("task_id") or "")
        question = str(event.get("content") or event.get("text") or "").strip()
        prompt = "\n".join([
            "这是一次团队内部通信回合，不是新的工作任务。",
            f"发起成员：{sender}",
            f"当前节点：{node_id or '未关联节点'}",
            f"问题：{question}",
            "请直接回答问题；不要派发任务、修改 TeamPlan、创建产物，也不要继续调用 team_mention。",
        ])
        envelope = Envelope.of(
            prompt,
            session_id=turn_session_id,
            request_id=request_id,
            channel="team_communication",
            user_id=owner,
            workspace_id=workspace_id,
            mode="agent",
            params={
                "task_session_id": self.session_id,
                "team_session_id": self.session_id,
                "member_session_id": member_session_id,
                "agent_id": target,
                "communication_kind": "ask_answer",
                "communication_request_id": request_id,
                "reply_to": request_message_id,
                "team_node_id": node_id,
                "task_id": task_id,
                "communication_path": [*path, target],
                "workspace_instructions": (
                    "当前是只读的 Team ask 回答回合。只回答发起成员的问题，"
                    "不要把它当作新的 DAG 节点或正式任务。"
                ),
            },
        )
        lock = self._locks.setdefault(target, asyncio.Lock())
        queued = lock.locked()
        if queued:
            event["communication_status"] = "queued"
            self.bus.update_status(request_message_id, "queued")
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.timeout_seconds)
        except TimeoutError:
            return self._publish_failure(
                event,
                target=target,
                sender=sender,
                request_id=request_id,
                request_message_id=request_message_id,
                reason=f"等待 {target} 的通信回合超时",
                status="expired",
            )

        try:
            event["communication_status"] = "delivered"
            self.bus.update_status(request_message_id, "delivered")
            self._active_paths[target] = tuple(event["communication_path"])
            final_text = ""
            delta_text: list[str] = []
            error_text = ""

            async def _run() -> None:
                nonlocal final_text, error_text
                async for chunk in agent.run(envelope):
                    if self.on_chunk is not None:
                        maybe = self.on_chunk(target, chunk)
                        if inspect.isawaitable(maybe):
                            await cast(Awaitable[Any], maybe)
                    if chunk.kind == "delta":
                        delta_text.append(str(chunk.body.get("text") or ""))
                    elif chunk.kind == "final":
                        final_text = str(chunk.body.get("text") or "").strip()
                    elif chunk.kind == "error":
                        error_text = str(chunk.body.get("message") or "目标成员回答失败").strip()

            try:
                await asyncio.wait_for(_run(), timeout=self.timeout_seconds)
            except TimeoutError:
                return self._publish_failure(
                    event,
                    target=target,
                    sender=sender,
                    request_id=request_id,
                    request_message_id=request_message_id,
                    reason=f"{target} 的通信回合执行超时",
                    status="expired",
                )
            except asyncio.CancelledError:
                self.bus.update_status(request_message_id, "cancelled")
                raise

            answer_text = final_text or "".join(delta_text).strip()
            if error_text or not answer_text:
                return self._publish_failure(
                    event,
                    target=target,
                    sender=sender,
                    request_id=request_id,
                    request_message_id=request_message_id,
                    reason=error_text or "目标成员未返回有效回答",
                    status="failed",
                )

            answer_message = self.bus.send(
                team_session_id=self.session_id,
                sender_member_id=target,
                recipient_member_ids=[sender],
                content=answer_text,
                message_type="answer",
                intent="answer",
                request_id=request_id,
                node_id=node_id,
                task_id=task_id,
                thread_id=str(event.get("thread_id") or node_id or task_id),
                reply_to=request_message_id,
            )
            self.bus.update_status(request_message_id, "answered")
            return {
                "status": "answered",
                "request_id": request_id,
                "answer": answer_text,
                "reply_to": request_message_id,
                "message": answer_message.to_dict(),
            }
        finally:
            if self._active_paths.get(target) == tuple(event.get("communication_path") or ()):
                self._active_paths.pop(target, None)
            lock.release()

    def _publish_failure(
        self,
        event: dict[str, Any],
        *,
        target: str,
        sender: str,
        request_id: str,
        request_message_id: str,
        reason: str,
        status: str = "failed",
    ) -> dict[str, Any]:
        self.bus.update_status(request_message_id, status)
        answer_message = self.bus.send(
            team_session_id=self.session_id,
            sender_member_id=target,
            recipient_member_ids=[sender],
            content=reason,
            message_type="answer",
            intent="answer",
            request_id=request_id,
            node_id=str(event.get("node_id") or ""),
            task_id=str(event.get("task_id") or ""),
            reply_to=request_message_id,
        )
        return {
            "status": status,
            "request_id": request_id,
            "answer": reason,
            "reply_to": request_message_id,
            "message": answer_message.to_dict(),
        }


__all__ = [
    "MENTION_MESSAGE_TYPES",
    "TeamCommunicationRouter",
    "expand_mention_targets",
    "normalize_mention_targets",
]
