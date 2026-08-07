"""委派工具：Leader 用它把子任务交给 Teammate 执行。

使用 Crew 工具格式：
  build schema -> async handler(args) -> registry.register(...)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ToolError
from crew.core.interfaces import Agent, TaskManager
from crew.core.runctx import current_agent_id, current_owner_account_id, current_workspace_id
from crew.state.logging import get_logger
from crew.team.bus import TeamBus
from crew.team.capabilities import CAPABILITIES
from crew.tools.registry import Registry, tool_result

log = get_logger("team")

TEAM_RESULT_STATUSES = ("pass", "fail", "blocked")


def require_team_result_status(intent: str, value: Any) -> str:
    """Validate the structured outcome carried by a Team result submission."""

    if str(intent or "").strip() != "submit":
        return ""
    status = str(value or "").strip().lower()
    if status not in TEAM_RESULT_STATUSES:
        raise ToolError(
            "team_mention(submit) 必须提供 result_status：pass、fail 或 blocked"
        )
    return status


def build_delegate_schema(member_names: list[str]) -> dict[str, Any]:
    members = ", ".join(member_names)
    return {
        "name": "delegate_to_teammate",
        "description": f"把一个子任务委派给指定队友执行并返回其结果。可选队友: {members}",
        "parameters": {
            "type": "object",
            "properties": {
                "member": {
                    "type": "string",
                    "enum": member_names,
                    "description": "执行该子任务的队友名称",
                },
                "instruction": {
                    "type": "string",
                    "description": "交给该队友的具体任务说明",
                },
                "plan_node_id": {
                    "type": "string",
                    "description": "可选：该派活绑定的 TeamPlan 节点 ID",
                },
            },
            "required": ["member", "instruction"],
        },
    }


def build_plan_change_schema(member_names: list[str]) -> dict[str, Any]:
    members = ", ".join(member_names)
    return {
        "name": "request_plan_change",
        "description": (
            "当当前 TeamPlan DAG 不足以完成用户目标时，向 Crew Runtime 请求受控变更。"
            "本阶段仅支持 add_node：新增一个真实成员执行节点，由 Runtime 后续按 DAG 派发。"
            f"可选成员: {members}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "change_type": {
                    "type": "string",
                    "enum": ["add_node"],
                    "description": "计划变更类型；本阶段只支持 add_node",
                },
                "node_id": {
                    "type": "string",
                    "description": "可选：新增节点 ID；不传时由 Runtime 根据 assignee/title 生成",
                },
                "title": {"type": "string", "description": "新增节点标题"},
                "detail": {"type": "string", "description": "新增节点的具体执行要求与交付物"},
                "assignee": {
                    "type": "string",
                    "enum": member_names,
                    "description": "新增节点的主责成员",
                },
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CAPABILITIES)},
                    "minItems": 1,
                    "description": "新增节点完成工作所需的标准能力 key；由 Runtime 用于画像匹配和补员判断",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新增节点依赖的现有 TeamPlan 节点 ID；不传时默认依赖 leader_plan（若存在）",
                },
                "before": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新增节点完成后才能继续的现有 TeamPlan 节点 ID；不传时默认阻塞 leader_summary（若存在）",
                },
                "reason": {"type": "string", "description": "为什么当前 DAG 需要新增该节点"},
            },
            "required": ["change_type", "title", "detail", "assignee", "required_capabilities"],
        },
    }


def build_mention_schema(member_names: list[str], *, allow_user: bool = True) -> dict[str, Any]:
    base_targets = ["leader", "all", *member_names]
    if allow_user:
        base_targets.insert(1, "user")
    targets = list(dict.fromkeys(base_targets))
    return {
        "name": "team_mention",
        "description": (
            "向团队中的指定对象发送 @mention 协作事件。"
            "@leader 用于提交/请求审阅，@成员用于派活或补充，@all 用于团队广播，"
            "@user 只能由 Leader 在必须用户确认时使用并触发追问交互。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string", "enum": targets},
                    "description": "mention 目标，可包含 leader、user、all 或具体成员",
                },
                "intent": {
                    "type": "string",
                    "enum": [
                        "assign",
                        "submit",
                        "review",
                        "ask",
                        "broadcast",
                        "handoff",
                        "user_followup",
                    ],
                    "description": "协作意图",
                },
                "content": {"type": "string", "description": "mention 正文"},
                "node_id": {"type": "string", "description": "可选：关联 TeamPlan 节点 ID"},
                "result_status": {
                    "type": "string",
                    "enum": list(TEAM_RESULT_STATUSES),
                    "description": "submit 时必填：当前节点的结构化验收状态",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "可选：关联产物卡片",
                },
                "questions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "当 to 包含 user 且 intent=user_followup 时，传给 ask_followup_question 的问题数组",
                },
                "title": {"type": "string", "description": "用户追问卡片标题，可选"},
            },
            "required": ["to", "intent", "content"],
        },
    }


def _normalize_mention_targets(raw: Any, *, member_names: list[str]) -> list[str]:
    if isinstance(raw, str):
        targets = [raw]
    else:
        targets = list(raw or [])
    allowed = {"leader", "user", "all", *member_names}
    normalized: list[str] = []
    for item in targets:
        target = str(item or "").strip().lstrip("@")
        if target and target in allowed and target not in normalized:
            normalized.append(target)
    return normalized


def _mention_text(targets: list[str], content: str) -> str:
    prefix = " ".join(_mention_markdown(target) for target in targets)
    body = str(content or "").strip()
    if not prefix:
        return body
    return body if body.startswith("[@") or body.startswith("@") else f"{prefix} {body}".strip()


def _mention_markdown(target: str) -> str:
    value = str(target or "").strip().lstrip("@")
    if not value:
        return ""
    mention_type = "team" if value == "all" else "user" if value == "user" else "member"
    safe_label = value.replace("[", "\\[").replace("]", "\\]")
    safe_id = value.replace(")", "%29")
    return f"[@{safe_label}](mention://{mention_type}/{safe_id})"


def make_delegate_handler(
    teammates: dict[str, Agent],
    tasks: TaskManager,
    session_id: str,
    *,
    bus: TeamBus | None = None,
    before_delegate: Callable[[dict[str, Any]], None] | None = None,
    execute_delegate: Callable[[dict[str, Any]], Any] | None = None,
    on_child_start: Callable[[dict[str, Any]], None] | None = None,
    on_child_done: Callable[[str, str, str], None] | None = None,
    on_child_chunk: Callable[[str, ResponseChunk], None] | None = None,
    on_task_created: Callable[[dict[str, Any]], None] | None = None,
    on_task_finished: Callable[[dict[str, Any]], None] | None = None,
):
    async def handle_delegate(args: dict[str, Any]) -> str:
        member = str(args.get("member", ""))
        instruction = str(args.get("instruction", "")).strip()
        if current_agent_id.get() != "leader":
            raise ToolError(
                "delegate_to_teammate 只允许 Crew Team 内部 Leader 在 team 模式使用；"
                "外部 agent 请通过 MCP 工具 team_request_delegate 发起派活请求。"
            )
        plan_node_id = str(args.get("plan_node_id") or "").strip()
        if before_delegate is not None:
            before_delegate({
                "member": member,
                "instruction": instruction,
                "plan_node_id": plan_node_id,
            })
        if execute_delegate is not None:
            result = execute_delegate({
                "member": member,
                "instruction": instruction,
                "plan_node_id": plan_node_id,
            })
            if asyncio.iscoroutine(result):
                result = await result
            return str(result or "")
        return await run_delegate_to_teammate(
            teammates,
            tasks,
            session_id,
            member=member,
            instruction=instruction,
            plan_node_id=plan_node_id,
            bus=bus,
            on_child_start=on_child_start,
            on_child_done=on_child_done,
            on_child_chunk=on_child_chunk,
            on_task_created=on_task_created,
            on_task_finished=on_task_finished,
        )

    return handle_delegate


def make_plan_change_handler(
    *,
    on_plan_change: Callable[[dict[str, Any]], Any],
):
    async def handle_plan_change(args: dict[str, Any]) -> str:
        if current_agent_id.get() != "leader":
            raise ToolError("request_plan_change 只允许 Crew Team 内部 Leader 在 team 模式使用。")
        result = on_plan_change(dict(args or {}))
        if asyncio.iscoroutine(result):
            result = await result
        return tool_result(result if isinstance(result, dict) else {"ok": True, "result": result})

    return handle_plan_change


def make_mention_handler(
    *,
    bus: TeamBus,
    session_id: str,
    member_id: str,
    member_names: list[str],
    on_mention: Callable[[dict[str, Any]], Any] | None = None,
):
    async def handle_mention(args: dict[str, Any]) -> str:
        sender = current_agent_id.get() or member_id
        targets = _normalize_mention_targets(args.get("to"), member_names=member_names)
        if not targets:
            raise ToolError("to 不能为空，且必须是 leader、user、all 或团队成员")
        intent = str(args.get("intent") or "broadcast").strip()
        result_status = require_team_result_status(intent, args.get("result_status"))
        content = str(args.get("content") or "").strip()
        if not content:
            raise ToolError("content 不能为空")
        if "user" in targets and sender != "leader":
            raise ToolError("只有 leader 可以 @user；成员需要用户信息时请先 @leader")
        if "user" in targets and intent != "user_followup":
            intent = "user_followup"
        expanded_targets = [
            target
            for target in targets
            if target != "user"
            for target in (["leader", *member_names] if target == "all" else [target])
        ]
        expanded_targets = list(dict.fromkeys(expanded_targets))
        event = {
            "from": sender,
            "to": targets,
            "intent": intent,
            "result_status": result_status,
            "node_id": str(args.get("node_id") or ""),
            "text": _mention_text(targets, content),
            "content": content,
            "artifacts": list(args.get("artifacts") or []),
            "questions": list(args.get("questions") or []),
            "title": str(args.get("title") or ""),
            "message": None,
        }
        result: Any = None
        if on_mention is not None:
            maybe = on_mention(event) if intent == "assign" else None
            if asyncio.iscoroutine(maybe):
                result = await maybe
            elif maybe is not None:
                result = maybe
        if intent != "assign" and expanded_targets:
            message = bus.send(
                team_session_id=session_id,
                sender_member_id=sender,
                recipient_member_ids=expanded_targets,
                content=content,
                message_type={
                    "submit": "result",
                    "ask": "decision_request",
                    "review": "answer",
                    "handoff": "handoff",
                    "user_followup": "decision_request",
                }.get(intent, "progress"),  # type: ignore[arg-type]
                task_id=str(args.get("task_id") or ""),
                thread_id=str(args.get("node_id") or args.get("thread_id") or ""),
                artifact_refs=[
                    str(item.get("path") or item.get("artifact_id") or item.get("id") or "")
                    for item in list(args.get("artifacts") or [])
                    if isinstance(item, dict)
                ],
            )
            event["message"] = message.to_dict()
            if on_mention is not None:
                maybe = on_mention(event)
                if asyncio.iscoroutine(maybe):
                    result = await maybe
                elif maybe is not None:
                    result = maybe
        return tool_result({"ok": True, "mention": event, "result": result or {}})

    return handle_mention


async def run_delegate_to_teammate(
    teammates: dict[str, Agent],
    tasks: TaskManager,
    session_id: str,
    *,
    member: str,
    instruction: str,
    requester_member_id: str = "leader",
    plan_node_id: str = "",
    bus: TeamBus | None = None,
    on_child_start: Callable[[dict[str, Any]], None] | None = None,
    on_child_done: Callable[[str, str, str], None] | None = None,
    on_child_chunk: Callable[[str, ResponseChunk], None] | None = None,
    on_task_created: Callable[[dict[str, Any]], None] | None = None,
    on_task_finished: Callable[[dict[str, Any]], None] | None = None,
    owner_account_id: str = "",
    task_payload_meta: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    owner = owner_account_id or current_owner_account_id.get()
    if member not in teammates:
        raise ToolError(f"未知队友: {member}，可选: {list(teammates)}")
    if not instruction:
        raise ToolError("instruction 不能为空")
    title = next((line.strip() for line in instruction.splitlines() if line.strip()), instruction)

    task = tasks.create(
        session_id,
        title=title[:40],
        detail=instruction,
        assignee=member,
        owner_account_id=owner,
    )
    progress = {
        "plan_node_id": plan_node_id,
        "requester_member_id": requester_member_id,
        "member": member,
    }
    touch_activity = getattr(tasks, "touch_activity", None)
    if callable(touch_activity):
        task = touch_activity(task["id"], progress)
    if on_task_created is not None:
        on_task_created({**task, "plan_node_id": plan_node_id})
    log.info("[Team] Leader 派发任务 #%s 给 %s: %s", task["id"], member, instruction[:60])

    teammate = teammates[member]
    child_session_id = f"{session_id}::{member}"
    child_id = f"{task['id']}::{member}"
    from crew.security.launch import current_process_launch

    child_params = {
        "task_session_id": session_id,
        "team_session_id": session_id,
        "member_session_id": child_session_id,
        "agent_id": member,
        **(task_payload_meta or {}),
    }
    # Team delegation runs the child Agent in-process. Carry the immutable
    # parent launch decision forward so external ACP/CLI execution cannot lose
    # the managed boundary at the child envelope.
    launch = current_process_launch.get()
    if launch is not None:
        child_params["_security_process_launch"] = launch
    if bus is not None:
        bus.send(
            team_session_id=session_id,
            sender_member_id=requester_member_id,
            recipient_member_ids=[member],
            content=instruction,
            message_type="assign",
            task_id=task["id"],
            thread_id=plan_node_id or task["id"],
        )
    sub_env = Envelope.of(
        instruction,
        session_id=child_session_id,
        params=child_params,
        channel="team",
        mode="agent",
        workspace_id=current_workspace_id.get(),
        user_id=owner,
        attachments=[
            dict(attachment)
            for attachment in (attachments or [])
            if isinstance(attachment, dict)
        ],
    )
    final_text = ""
    if on_child_start is not None:
        on_child_start({
            "child_id": child_id,
            "parent_session_id": session_id,
            "session_id": child_session_id,
            "member": member,
            "task_id": task["id"],
            "instruction": instruction,
            "started_at": time.time(),
            "agent": teammate,
            "owner_account_id": owner,
        })
    try:
        async for chunk in teammate.run(sub_env):
            if on_child_chunk is not None:
                on_child_chunk(member, chunk)
            if chunk.kind == "final":
                final_text = chunk.body.get("text", "")
            elif chunk.kind == "error":
                tasks.update_status(task["id"], "failed", chunk.body.get("message", ""))
                if on_task_finished is not None:
                    on_task_finished({
                        **task,
                        "plan_node_id": plan_node_id,
                        "status": "failed",
                        "result": chunk.body.get("message", ""),
                    })
                raise ToolError(chunk.body.get("message", "队友执行出错"))
    except asyncio.CancelledError:
        tasks.update_status(task["id"], "cancelled", "cancelled")
        if on_task_finished is not None:
            on_task_finished({
                **task,
                "plan_node_id": plan_node_id,
                "status": "cancelled",
                "result": "cancelled",
            })
        raise
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        tasks.update_status(task["id"], "failed", str(exc))
        if on_task_finished is not None:
            on_task_finished({
                **task,
                "plan_node_id": plan_node_id,
                "status": "failed",
                "result": str(exc),
            })
        raise ToolError(f"队友 {member} 执行异常: {exc}") from exc
    finally:
        if on_child_done is not None:
            on_child_done(session_id, child_id, owner)

    tasks.update_status(task["id"], "done", final_text)
    if on_task_finished is not None:
        on_task_finished({
            **task,
            "plan_node_id": plan_node_id,
            "status": "completed",
            "result": final_text,
        })
    if bus is not None:
        bus.send(
            team_session_id=session_id,
            sender_member_id=member,
            recipient_member_ids=["leader"],
            content=final_text,
            message_type="task_notification",
            task_id=task["id"],
        )
    log.info("[Team] 队友 %s 完成任务 #%s", member, task["id"])
    return final_text


def register_delegate_tool(
    registry: Registry,
    teammates: dict[str, Agent],
    tasks: TaskManager,
    session_id: str,
    *,
    bus: TeamBus | None = None,
    before_delegate: Callable[[dict[str, Any]], None] | None = None,
    execute_delegate: Callable[[dict[str, Any]], Any] | None = None,
    on_child_start: Callable[[dict[str, Any]], None] | None = None,
    on_child_done: Callable[[str, str, str], None] | None = None,
    on_child_chunk: Callable[[str, ResponseChunk], None] | None = None,
    on_task_created: Callable[[dict[str, Any]], None] | None = None,
    on_task_finished: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    registry.register(
        name="delegate_to_teammate",
        toolset="delegation",
        schema=build_delegate_schema(list(teammates.keys())),
        handler=make_delegate_handler(
            teammates,
            tasks,
            session_id,
            bus=bus,
            before_delegate=before_delegate,
            execute_delegate=execute_delegate,
            on_child_start=on_child_start,
            on_child_done=on_child_done,
            on_child_chunk=on_child_chunk,
            on_task_created=on_task_created,
            on_task_finished=on_task_finished,
        ),
        is_async=True,
        display_name="委派队友",
        ui_label_template="委派给 {member}",
    )


def register_plan_change_tool(
    registry: Registry,
    *,
    member_names: list[str],
    on_plan_change: Callable[[dict[str, Any]], Any],
) -> None:
    registry.register(
        name="request_plan_change",
        toolset="delegation",
        schema=build_plan_change_schema(member_names),
        handler=make_plan_change_handler(on_plan_change=on_plan_change),
        is_async=True,
        display_name="变更计划",
        ui_label_template="变更 TeamPlan",
    )


def register_team_mention_tool(
    registry: Registry,
    *,
    bus: TeamBus,
    session_id: str,
    member_id: str,
    member_names: list[str],
    allow_user: bool = True,
    on_mention: Callable[[dict[str, Any]], Any] | None = None,
) -> None:
    registry.register(
        name="team_mention",
        toolset="team_bus",
        schema=build_mention_schema(member_names, allow_user=allow_user),
        handler=make_mention_handler(
            bus=bus,
            session_id=session_id,
            member_id=member_id,
            member_names=member_names,
            on_mention=on_mention,
        ),
        is_async=True,
    )
