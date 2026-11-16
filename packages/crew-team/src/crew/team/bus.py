"""Team Bus：异构团队内部通信与 mailbox。

Team Bus 是 Crew 的控制平面通信层，不替代 ACP/CLI 协议：
  - ACP/CLI 负责 Crew 与单个外部 agent 的执行通信；
  - Team Bus 负责团队成员之间的消息、任务通知、产物引用与审计事件。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from crew.team.models import MessageType, TeamArtifact, TeamMessage
from crew.tools.registry import Registry, tool_result


class TeamBus:
    """内存版 Team Bus。

    第一阶段先保持 in-process、可测试；后续可把同一接口替换成 SQLite/PostgreSQL。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[str, TeamMessage] = {}
        self._mailboxes: dict[tuple[str, str], list[str]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._artifacts: dict[str, TeamArtifact] = {}

    def send(
        self,
        *,
        team_session_id: str,
        sender_member_id: str,
        recipient_member_ids: list[str],
        content: str,
        message_type: MessageType = "question",
        intent: str = "",
        request_id: str = "",
        node_id: str = "",
        task_id: str = "",
        thread_id: str = "",
        reply_to: str = "",
        artifact_refs: list[str] | None = None,
        workflow_run_id: str = "",
        requires_ack: bool = False,
        priority: int = 0,
    ) -> TeamMessage:
        recipients = [str(r).strip() for r in recipient_member_ids if str(r).strip()]
        if not recipients:
            raise ValueError("TeamMessage 至少需要一个收件成员")
        message = TeamMessage(
            team_session_id=team_session_id,
            sender_member_id=sender_member_id,
            recipient_member_ids=recipients,
            content=content,
            message_type=message_type,
            intent=intent,
            request_id=request_id,
            node_id=node_id,
            task_id=task_id,
            thread_id=thread_id or node_id or task_id,
            reply_to=reply_to,
            artifact_refs=list(artifact_refs or []),
            workflow_run_id=workflow_run_id,
            requires_ack=requires_ack,
            priority=priority,
        )
        with self._lock:
            self._messages[message.message_id] = message
            for recipient in recipients:
                self._mailboxes.setdefault((team_session_id, recipient), []).append(message.message_id)
            self._events.setdefault(team_session_id, []).append({
                "type": "message_sent",
                "message": message.to_dict(),
            })
        return message

    def read(
        self,
        *,
        team_session_id: str,
        member_id: str,
        consume: bool = True,
        limit: int = 20,
    ) -> list[TeamMessage]:
        with self._lock:
            key = (team_session_id, member_id)
            ids = list(self._mailboxes.get(key, []))[:max(1, limit)]
            messages = [self._messages[mid] for mid in ids if mid in self._messages]
            if consume:
                consumed = {m.message_id for m in messages}
                self._mailboxes[key] = [
                    mid for mid in self._mailboxes.get(key, []) if mid not in consumed
                ]
                for message in messages:
                    message.status = "consumed"
                    self._events.setdefault(team_session_id, []).append({
                        "type": "message_consumed",
                        "message_id": message.message_id,
                        "member_id": member_id,
                    })
        return messages

    def list_messages(self, team_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                msg.to_dict()
                for msg in self._messages.values()
                if msg.team_session_id == team_session_id
            ]

    def update_status(self, message_id: str, status: str) -> TeamMessage:
        """更新消息生命周期状态并留下可观测事件。"""

        normalized = str(status or "").strip()
        if not normalized:
            raise ValueError("消息状态不能为空")
        with self._lock:
            message = self._messages.get(str(message_id or "").strip())
            if message is None:
                raise KeyError(f"未知 TeamMessage: {message_id}")
            previous = message.status
            message.status = normalized
            self._events.setdefault(message.team_session_id, []).append({
                "type": "message_status_changed",
                "message_id": message.message_id,
                "previous_status": previous,
                "status": normalized,
            })
            return message

    def events(self, team_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(team_session_id, []))

    def add_artifact(
        self,
        *,
        team_session_id: str,
        owner_member_id: str,
        summary: str,
        scope: str = "team",
        task_id: str = "",
        content_type: str = "text/plain",
        path: str = "",
    ) -> TeamArtifact:
        artifact = TeamArtifact(
            team_session_id=team_session_id,
            owner_member_id=owner_member_id,
            summary=summary,
            scope=scope,
            task_id=task_id,
            content_type=content_type,
            path=path,
        )
        with self._lock:
            self._artifacts[artifact.artifact_id] = artifact
            self._events.setdefault(team_session_id, []).append({
                "type": "artifact_added",
                "artifact": artifact.to_dict(),
            })
        return artifact

    def list_artifacts(self, team_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                artifact.to_dict()
                for artifact in self._artifacts.values()
                if artifact.team_session_id == team_session_id
            ]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def register_team_bus_tools(
    registry: Registry,
    bus: TeamBus,
    *,
    team_session_id: str,
    member_id: str,
    member_ids: list[str],
) -> None:
    """给成员注册 Team Bus 工具。

    工具入参只暴露 member_id，不暴露 member_session_id/external_session_id。
    """

    async def read_messages(args: dict[str, Any]) -> str:
        limit = int(args.get("limit") or 20)
        consume = bool(args.get("consume", True))
        messages = bus.read(
            team_session_id=team_session_id,
            member_id=member_id,
            consume=consume,
            limit=limit,
        )
        return _json({"ok": True, "messages": [m.to_dict() for m in messages]})

    async def add_artifact(args: dict[str, Any]) -> str:
        artifact = bus.add_artifact(
            team_session_id=team_session_id,
            owner_member_id=member_id,
            summary=str(args.get("summary") or ""),
            scope=str(args.get("scope") or "team"),
            task_id=str(args.get("task_id") or ""),
            content_type=str(args.get("content_type") or "text/plain"),
            path=str(args.get("path") or ""),
        )
        return tool_result({"ok": True, "artifact": artifact.to_dict()})

    registry.register(
        name="team_read_messages",
        toolset="team_bus",
        schema={
            "name": "team_read_messages",
            "description": "读取当前成员 mailbox 中的 Team Bus 消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "consume": {"type": "boolean"},
                },
            },
        },
        handler=read_messages,
        is_async=True,
        display_name="读取团队消息",
        ui_label_template="读取团队消息",
        result_retention="important",
    )
    registry.register(
        name="team_add_artifact",
        toolset="team_bus",
        schema={
            "name": "team_add_artifact",
            "description": "登记团队产物引用，供 Leader 或其他成员查看。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "scope": {"type": "string"},
                    "task_id": {"type": "string"},
                    "content_type": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
        handler=add_artifact,
        is_async=True,
        display_name="登记团队产物",
        ui_label_template="登记团队产物 {summary}",
    )
