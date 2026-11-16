"""Team-specific history and task projections for gateway routes."""

from __future__ import annotations

import json
import os
from typing import Any

from crew.core.types import Message, safe_duration_seconds
from crew.state.logging import get_logger
from crew.team.result_presenter import (
    is_team_chat_noise,
    node_display_progress,
    normalize_legacy_chunked_thinking,
    result_projection,
)
from crew.team.roles import CREW_BUILTIN_AGENT_ID, LEGACY_CREW_BUILTIN_AGENT_ID, is_crew_builtin_display_id

log = get_logger("team.history_projection")


def compact_history_content(value: Any) -> str:
    return " ".join(str(value or "").split())


async def _list_runtime_tasks(crew, **kwargs: Any) -> list[dict[str, Any]]:
    """查询统一任务库：TaskRuntime 走异步门面；纯内存的测试 fake 直接同步返回。"""
    tasks = crew.tasks
    list_async = getattr(tasks, "list_tasks_async", None)
    if callable(list_async):
        return await list_async(**kwargs)
    return tasks.list_tasks(**kwargs)


def team_child_member_id(child_session_id: str) -> str | None:
    """Return the member suffix from a Team child session id.

    Team child sessions use ``{parent}::turn::{request_id}::{member_id}``.
    Ordinary Agent turns use ``{parent}::turn::{request_id}``; those are
    execution sidechains and must never be interpreted as Team members.
    """
    child_id = str(child_session_id or "").strip()
    marker = "::turn::"
    if marker not in child_id:
        return None
    turn_part = child_id.split(marker, 1)[1]
    request_id, separator, member_id = turn_part.rpartition("::")
    if not separator or not request_id.strip() or not member_id.strip():
        return None
    return member_id.strip()


def project_team_event_history(store: Any, workflow_ids: list[str]) -> list[dict[str, Any]]:
    """Project persisted Team events into the canonical internal chat shape."""

    items: list[dict[str, Any]] = []
    communication_status_by_request: dict[str, str] = {}
    visible_event_types = {
        "team_assign",
        "team_stream",
        "team_submit",
        "team_ack",
        "team_review",
        "team_decision",
        "team_summary",
        "team_communication",
    }
    for workflow_id in workflow_ids:
        try:
            events = store.list_events(workflow_id, limit=500)
        except Exception:  # noqa: BLE001
            continue
        for event in sorted(events, key=lambda item: float(item.ts or 0)):
            payload = dict(event.payload or {})
            if event.event_type not in visible_event_types:
                continue
            if event.event_type == "team_communication":
                request_id = str(payload.get("request_id") or "").strip()
                communication_kind = str(payload.get("communication_kind") or "").strip()
                if request_id and communication_kind in {"ask_lifecycle", "ask_answer"}:
                    communication_status_by_request[request_id] = str(
                        payload.get("communication_status") or ""
                    ).strip()
                if communication_kind == "ask_lifecycle":
                    continue
            text = str(payload.get("text") or "").strip()
            if is_team_chat_noise(text):
                continue
            items.append({
                "role": "team_internal",
                "content": text[:1200],
                "agent_id": str(payload.get("agent_id") or event.actor or "agent"),
                "agent_name": str(payload.get("agent_name") or payload.get("agent_id") or event.actor or "Agent"),
                "agent_role": str(payload.get("agent_role") or ""),
                "agent_tone": payload.get("agent_tone"),
                "is_leader": bool(payload.get("is_leader")),
                "source_session_id": str(payload.get("source_session_id") or ""),
                "node_id": str(payload.get("node_id") or ""),
                "event_type": str(payload.get("event_type") or event.event_type),
                "display_mode": str(payload.get("display_mode") or "chat"),
                "collapsed_title": str(payload.get("collapsed_title") or ""),
                "process_text": str(payload.get("process_text") or ""),
                "thinking": normalize_legacy_chunked_thinking(str(payload.get("thinking") or "")),
                "tool_calls": _safe_tool_calls(payload.get("tool_calls")),
                "artifacts": list(payload.get("artifacts") or []),
                "turn_file_changes": list(payload.get("turn_file_changes") or []),
                "mention_from": str(payload.get("mention_from") or ""),
                "mention_to": list(payload.get("mention_to") or []),
                "mention_intent": str(payload.get("mention_intent") or ""),
                "request_id": str(payload.get("request_id") or ""),
                "reply_to": str(payload.get("reply_to") or ""),
                "communication_kind": str(payload.get("communication_kind") or ""),
                "communication_status": str(payload.get("communication_status") or ""),
                "timestamp": float(event.ts or 0),
                **({"turn_started_at": payload.get("turn_started_at")} if payload.get("turn_started_at") is not None else {}),
                **(
                    {"turn_duration": turn_duration}
                    if (turn_duration := safe_duration_seconds(payload.get("turn_duration"))) is not None
                    else {}
                ),
            })
    for item in items:
        request_id = str(item.get("request_id") or "").strip()
        if (
            request_id
            and item.get("communication_kind") == "ask_request"
            and request_id in communication_status_by_request
        ):
            item["communication_status"] = communication_status_by_request[request_id]
    items.sort(key=lambda item: float(item.get("timestamp") or 0))
    return items


def is_duplicate_team_parent_final(
    item: dict[str, Any],
    internal_items: list[dict[str, Any]],
) -> bool:
    if item.get("role") not in {"assistant", "team_internal"}:
        return False
    content = compact_history_content(item.get("content"))
    if not content:
        return False
    request_id = str(item.get("request_id") or "").strip()
    if request_id:
        return any(
            str(internal.get("event_type") or "") == "team_summary"
            and str(internal.get("request_id") or "").strip() == request_id
            for internal in internal_items
        )
    content_head = content[:240]
    for internal in internal_items:
        if str(internal.get("event_type") or "") != "team_summary":
            continue
        leader_content = compact_history_content(internal.get("content"))
        if not leader_content:
            continue
        leader_head = leader_content[:240]
        if content_head in leader_content or leader_head in content:
            return True
    return False


def direct_mention_request_ids(
    internal_items: list[dict[str, Any]],
) -> set[str]:
    """Return direct user-mention request ids represented by Team history.

    A direct user mention still runs inside the generic dispatcher and may
    therefore leave a parent ``agent_turn`` runtime record.  That record is
    operational state, not a second conversational reply.  The member
    ``team_internal`` item is the canonical visible answer; the request id is
    the stable correlation key between the two projections.
    """

    return {
        str(item.get("request_id") or "").strip()
        for item in internal_items
        if item.get("role") == "team_internal"
        and item.get("communication_kind") == "user_mention_answer"
        and str(item.get("request_id") or "").strip()
    }


async def team_visible_history_items(
    crew,
    session_id: str,
    owner_account_id: str,
    config: dict[str, Any] | None = None,
    has_child_team_sessions: bool = False,
    suppressed_request_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        tasks = await _list_runtime_tasks(crew, limit=1000, owner_account_id=owner_account_id)
    except Exception:  # noqa: BLE001
        return []
    parent_turns = [
        task for task in tasks
        if str(task.get("kind") or "") == "agent_turn"
        and str(task.get("session_id") or "") == session_id
    ]
    child_team_session_prefix = f"{session_id}::turn::"
    has_child_team_tasks = any(
        str(task.get("kind") or "") == "team"
        and str(task.get("session_id") or "").startswith(child_team_session_prefix)
        for task in tasks
    )
    items: list[dict[str, Any]] = []
    profiles = _team_internal_member_profiles(
        crew,
        config,
        owner_account_id=owner_account_id,
    )
    leader_identity = _team_internal_agent_identity("leader", profiles)
    suppressed = {str(item or "").strip() for item in (suppressed_request_ids or set())}
    for task in sorted(parent_turns, key=lambda item: float(item.get("created_at") or 0)):
        prompt = str(task.get("detail") or task.get("title") or "").strip()
        if prompt:
            items.append({
                "role": "user",
                "content": prompt,
                "source_session_id": session_id,
                "timestamp": float(task.get("created_at") or task.get("started_at") or 0),
            })
        result = str(task.get("result") or "").strip()
        error = str(task.get("error") or "").strip()
        request_id = str(task.get("request_id") or "").strip()
        # Direct @Agent 的父 agent_turn 只用于运行时保活/收口；成员子回合
        # 已经提供 canonical 回复，不能再把父结果投影成 Crew/Leader 消息。
        if request_id and request_id in suppressed:
            continue
        if result or error:
            role = "assistant" if has_child_team_tasks or has_child_team_sessions else "team_internal"
            items.append({
                "role": role,
                "content": result or error,
                "source_session_id": session_id,
                "timestamp": float(task.get("updated_at") or task.get("finished_at") or task.get("created_at") or 0),
                **({} if role == "assistant" else leader_identity),
                **({"turn_started_at": task.get("started_at")} if task.get("started_at") is not None else {}),
            })
    return items


def _message_tool_calls(message) -> list[dict[str, Any]]:
    tool_calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        tool_calls.append({
            "id": tc.id,
            "name": tc.name,
            "arguments": tc.arguments,
            "result": tc.result,
            "status": tc.status,
            **({"ui_label": tc.ui_label} if tc.ui_label else {}),
            **({"started_at": tc.started_at} if tc.started_at is not None else {}),
            **(
                {"duration": duration}
                if (duration := safe_duration_seconds(tc.duration)) is not None
                else {}
            ),
        })
    return tool_calls


def _safe_tool_calls(value: Any) -> list[dict[str, Any]]:
    """Normalize persisted Team tool calls before exposing them to history UI."""

    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        call = dict(raw)
        if "duration" in call:
            duration = safe_duration_seconds(call.get("duration"))
            if duration is None:
                call.pop("duration", None)
            else:
                call["duration"] = duration
        calls.append(call)
    return calls


def _team_internal_member_profiles(
    crew,
    config: dict[str, Any] | None,
    *,
    owner_account_id: str,
) -> dict[str, dict[str, Any]]:
    external_team_id = str(((config or {}).get("team") or {}).get("external_team_id") or "").strip()
    store = getattr(crew, "external_agents", None)
    if not external_team_id or store is None:
        return {}
    try:
        team = store.get_team(external_team_id, owner_account_id=owner_account_id)
    except Exception:  # noqa: BLE001
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    leader_agent_id = str(team.get("leader_agent_id") or "").strip()
    members = list(team.get("members") or [])
    ordered_members = [
        *[member for member in members if str(member.get("agent_id") or "").strip() == leader_agent_id],
        *[member for member in members if str(member.get("agent_id") or "").strip() != leader_agent_id],
    ]
    for index, member in enumerate(ordered_members):
        agent_id = str(member.get("agent_id") or "").strip()
        if not agent_id:
            continue
        agent_name = str(member.get("agent_name") or agent_id).strip()
        is_leader = agent_id == leader_agent_id
        display_is_builtin = is_crew_builtin_display_id(agent_id)
        profile = {
            "agent_id": CREW_BUILTIN_AGENT_ID if display_is_builtin else agent_id,
            "agent_name": "Crew" if display_is_builtin else agent_name,
            "agent_role": "leader" if is_leader else str(member.get("role_label") or member.get("role") or "").strip(),
            "is_leader": is_leader,
            "agent_tone": index % 6,
        }
        profiles[agent_id] = profile
        if display_is_builtin:
            profiles[LEGACY_CREW_BUILTIN_AGENT_ID] = profile
            profiles[CREW_BUILTIN_AGENT_ID] = profile
        profiles[agent_name] = profile
        profiles[agent_name.lower()] = profile
    if leader_agent_id and leader_agent_id not in profiles:
        profiles[leader_agent_id] = {
            "agent_id": leader_agent_id,
            "agent_name": "Crew" if leader_agent_id == CREW_BUILTIN_AGENT_ID else leader_agent_id,
            "agent_role": "leader",
            "is_leader": True,
            "agent_tone": 0,
        }
    profiles["leader"] = profiles.get(leader_agent_id, {
        "agent_id": leader_agent_id or "leader",
        "agent_name": "Crew" if leader_agent_id == CREW_BUILTIN_AGENT_ID else (leader_agent_id or "Leader"),
        "agent_role": "leader",
        "is_leader": True,
        "agent_tone": 0,
    })
    return profiles


def _team_internal_agent_identity(
    raw_member_id: str,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    member_id = str(raw_member_id or "").strip()
    lookup = profiles or {}
    profile = lookup.get(member_id) or lookup.get(member_id.lower())
    if profile:
        return dict(profile)
    if is_crew_builtin_display_id(member_id):
        return {
            "agent_id": CREW_BUILTIN_AGENT_ID,
            "agent_name": "Crew",
            "agent_role": "",
            "is_leader": False,
            "agent_tone": 0,
        }
    return {
        "agent_id": member_id or "agent",
        "agent_name": member_id or "Agent",
        "agent_role": "",
        "is_leader": False,
        "agent_tone": 0,
    }


def _normalize_communication_identity(
    item: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Use the persisted communication sender to restore a member identity.

    Older Team communication events could persist ``agent_id=crew::builtin``
    while the direct mention sender remained in ``mention_from``.  The answer
    text is still correct in that case, but history replay would render the
    member with Crew's avatar.  Communication sender metadata is the canonical
    identity for answer events; the display name is only a fallback for older
    events that lack ``mention_from``.
    """

    kind = str(item.get("communication_kind") or "").strip()
    if kind not in {"user_mention_answer", "ask_answer"}:
        return item
    raw_member_id = str(item.get("mention_from") or "").strip()
    if not raw_member_id:
        raw_member_id = str(item.get("agent_name") or item.get("agent_id") or "").strip()
    if not raw_member_id:
        return item
    normalized = dict(item)
    normalized.update(_team_internal_agent_identity(raw_member_id, profiles))
    return normalized


def _child_session_history_items(
    child_sessions: list[tuple[str, list[Message]]],
    profiles: dict[str, dict[str, Any]],
    *,
    communication_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Project child Agent turns, optionally selecting one communication kind."""

    items: list[dict[str, Any]] = []
    for child_session_id, child_messages in child_sessions:
        child_id = str(child_session_id)
        member_id = team_child_member_id(child_id)
        if not member_id:
            continue
        identity = _team_internal_agent_identity(member_id, profiles)
        for message in child_messages:
            if message.is_meta or message.role != "assistant":
                continue
            if communication_kind and message.communication_kind != communication_kind:
                continue
            content = str(message.content or "").strip()
            if is_team_chat_noise(content):
                continue
            tool_calls = _message_tool_calls(message)
            items.append({
                "role": "team_internal",
                "content": content[:1200],
                **identity,
                "source_session_id": child_id,
                "timestamp": message.timestamp,
                **({"thinking": message.thinking} if message.thinking else {}),
                **({"tool_calls": tool_calls} if tool_calls else {}),
                **({"turn_file_changes": message.turn_file_changes} if message.turn_file_changes else {}),
                **({"communication_kind": message.communication_kind} if message.communication_kind else {}),
                **({"communication_status": message.communication_status} if message.communication_status else {}),
                **({"request_id": message.request_id} if message.request_id else {}),
                **({"reply_to": message.reply_to} if message.reply_to else {}),
                **({"communication_request_text": message.communication_request_text} if message.communication_request_text else {}),
            })
    return items


def _enrich_team_submissions_with_member_records(
    items: list[dict[str, Any]],
    child_sessions: list[tuple[str, list[Message]]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the member's durable reply and tool/file record on its submit event.

    Team events are the durable timeline for a workflow, while each member
    child session contains the canonical Agent turn details. Join them by the
    source session and request written on ``team_submit`` so history replay
    can show the actual member work instead of only the Leader's synthesis.
    """

    child_items = _child_session_history_items(child_sessions, profiles)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for child_item in child_items:
        source = str(child_item.get("source_session_id") or "").strip()
        if source:
            by_source.setdefault(source, []).append(child_item)

    enriched: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("event_type") or "") != "team_submit":
            enriched.append(item)
            continue
        source = str(item.get("source_session_id") or "").strip()
        request_id = str(item.get("request_id") or "").strip()
        records = by_source.get(source, [])
        # Workflow submit events historically used ``{parent}::{member}``,
        # while the durable Agent transcript is stored in
        # ``{parent}::turn::{request_id}::{member}``. Resolve that canonical
        # child session before falling back to a loose request-id match.
        member_id = str(item.get("mention_from") or item.get("agent_id") or "").strip()
        member_suffix = f"::{member_id}" if member_id else ""
        if not records and request_id and member_suffix and source.endswith(member_suffix):
            parent_session_id = source[:-len(member_suffix)]
            canonical_source = f"{parent_session_id}::turn::{request_id}::{member_id}"
            records = by_source.get(canonical_source, [])
        if request_id:
            matched = [
                record for record in records
                if not str(record.get("request_id") or "").strip()
                or str(record.get("request_id") or "").strip() == request_id
            ]
            records = matched
        if not records:
            enriched.append(item)
            continue

        submission = dict(item)
        final_record = next(
            (record for record in reversed(records) if str(record.get("content") or "").strip()),
            None,
        )
        if final_record is not None:
            submission["content"] = str(final_record.get("content") or submission.get("content") or "")

        tool_calls: list[dict[str, Any]] = []
        seen_tools: set[str] = set()
        for record in records:
            for call in record.get("tool_calls") or []:
                call_id = str(call.get("id") or "").strip()
                key = call_id or repr(sorted(call.items()))
                if key not in seen_tools:
                    seen_tools.add(key)
                    tool_calls.append(dict(call))
        if tool_calls:
            submission["tool_calls"] = tool_calls

        file_changes: list[dict[str, Any]] = []
        seen_files: set[tuple[str, str]] = set()
        for record in records:
            for change in record.get("turn_file_changes") or []:
                key = (str(change.get("path") or ""), str(change.get("status") or ""))
                if key not in seen_files:
                    seen_files.add(key)
                    file_changes.append(dict(change))
        if file_changes:
            submission["turn_file_changes"] = file_changes
        enriched.append(submission)
    return enriched


def _merge_stream_text(previous: str, incoming: str) -> str:
    if not incoming or previous.endswith(incoming):
        return previous
    if incoming.startswith(previous):
        return incoming
    for overlap in range(min(len(previous), len(incoming), 4096), 0, -1):
        if previous.endswith(incoming[:overlap]):
            return previous + incoming[overlap:]
    return previous + incoming


def _attempt_activity(attempt: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], bool]:
    thinking = ""
    stream = ""
    calls: dict[str, dict[str, Any]] = {}
    truncated = False
    for index, event in enumerate(attempt.get("activity") or []):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        if kind == "truncated":
            truncated = True
        elif kind == "thinking":
            thinking = _merge_stream_text(thinking, str(body.get("text") or ""))
        elif kind == "delta":
            stream = _merge_stream_text(stream, str(body.get("text") or ""))
        elif kind == "tool":
            name = str(body.get("name") or "工具")
            phase = str(body.get("phase") or "")
            key = str(body.get("tool_call_id") or "")
            if not key and phase in {"result", "error"}:
                key = next((call_id for call_id, call in reversed(list(calls.items()))
                            if call["name"] == name and call["status"] == "running"), "")
            key = key or f"recorded-{index}"
            call = calls.setdefault(key, {"id": key, "name": name, "arguments": {}, "status": "running"})
            raw_args = str(body.get("args") or "")
            if raw_args:
                try:
                    parsed = json.loads(raw_args)
                    call["arguments"] = parsed if isinstance(parsed, dict) else {"input": parsed}
                except (TypeError, ValueError):
                    call["arguments"] = {"input": raw_args}
            if body.get("ui_label"):
                call["ui_label"] = str(body["ui_label"])
            if phase in {"result", "error"}:
                call["result"] = str(body.get("detail") or "")
                call["status"] = "error" if phase == "error" else "done"
    return thinking.strip(), stream.strip(), list(calls.values()), truncated


def project_collaboration_history(
    executions: list[dict[str, Any]],
    child_sessions: list[tuple[str, list[Message]]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replay real assignments and per-attempt outputs from the durable journal."""
    children = {str(session): messages for session, messages in child_sessions}
    items: list[dict[str, Any]] = []
    for execution in executions:
        turn = str(execution.get("session_id") or "").strip()
        if not turn:
            continue
        request_id = turn.split("::turn::", 1)[1].split("::", 1)[0] if "::turn::" in turn else ""
        snapshot = execution.get("team_snapshot") or {}
        members = {str(member.get("member_id") or ""): member for member in snapshot.get("members", []) if isinstance(member, dict)}
        leader_id = str(snapshot.get("leader_id") or "leader")
        leader = _team_internal_agent_identity("leader", profiles)
        if not profiles.get("leader"):
            leader.update(agent_id=leader_id, agent_name=str(members.get(leader_id, {}).get("name") or "Crew"), is_leader=True, agent_role="leader")
        units = {str(unit.get("id") or ""): unit for unit in execution.get("units", []) if isinstance(unit, dict)}
        for attempt in execution.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            node_id = str(attempt.get("unit_id") or "")
            unit = units.get(node_id)
            member_id = str(attempt.get("planned_member") or "").strip()
            started = float(attempt.get("started_at") or 0)
            if not unit or not member_id or not started:
                continue
            member = members.get(member_id, {})
            name = str(member.get("name") or (profiles.get(member_id) or {}).get("agent_name") or member_id)
            title = str(unit.get("title") or "协作任务").strip()
            common = {"role": "team_internal", "node_id": node_id, "request_id": request_id}
            items.append({**common, **leader, "event_type": "team_assign", "mention_from": "leader",
                          "mention_to": [member_id], "mention_intent": "assign", "source_session_id": f"{turn}::leader",
                          "content": f"@{name} {title}", "timestamp": started})
            result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
            text = str(result.get("text") or "").strip()
            executor = str(attempt.get("actual_executor") or result.get("executor") or "").strip()
            ended = float(attempt.get("ended_at") or 0)
            recorded_thinking, recorded_stream, recorded_tools, truncated = _attempt_activity(attempt)
            if not text or not executor or not ended:
                if attempt.get("state") in {"failed", "unknown"} and ended:
                    error = str(attempt.get("error") or "执行未完成").strip()
                    if recorded_thinking or recorded_stream or recorded_tools:
                        identity = _team_internal_agent_identity(member_id, profiles)
                        if member_id not in profiles and member.get("name"):
                            identity["agent_name"] = name
                        failure = {**common, **identity, "event_type": "team_submit", "source_session_id": f"{turn}::{member_id}",
                                   "content": f"执行未完成：{error}", "timestamp": ended}
                        if recorded_thinking:
                            failure["thinking"] = recorded_thinking
                        if recorded_tools:
                            failure["tool_calls"] = recorded_tools
                        if recorded_stream or truncated:
                            failure["process_text"] = "\n\n".join(part for part in [recorded_stream, "执行过程较长，仅保留了部分实时记录。" if truncated else ""] if part)
                            failure["collapsed_title"] = "执行过程"
                        items.append(failure)
                    else:
                        items.append({**common, **leader, "event_type": "team_decision", "source_session_id": f"{turn}::leader",
                                      "content": f"@{name} 的任务未完成：{error}", "timestamp": ended})
                continue
            identity = _team_internal_agent_identity(executor, profiles)
            if executor not in profiles and executor == member_id and member.get("name"):
                identity["agent_name"] = name
            item = {**common, **identity, "event_type": "team_submit", "mention_from": executor,
                    "mention_to": ["leader"], "mention_intent": "submit", "source_session_id": f"{turn}::{member_id}",
                    "content": text, "timestamp": ended, "turn_started_at": started,
                    "turn_duration": max(0.0, ended - started)}
            # A member may execute several nodes in one child session. Only
            # associate messages with this attempt when their timestamps fit.
            records = [record for record in children.get(f"{turn}::{member_id}", [])
                       if record.role == "assistant" and not record.is_meta and record.timestamp is not None
                       and started <= float(record.timestamp) <= ended]
            thoughts = [recorded_thinking] if recorded_thinking else [str(record.thinking).strip() for record in records if record.thinking]
            if thoughts:
                item["thinking"] = "\n\n".join(dict.fromkeys(thoughts))
            tool_calls = recorded_tools or [call for record in records for call in _message_tool_calls(record)]
            if tool_calls:
                item["tool_calls"] = tool_calls
            process = [str(record.content or "").strip() for record in records
                       if str(record.content or "").strip() and str(record.content or "").strip() != text]
            if recorded_stream and recorded_stream != text:
                process.insert(0, recorded_stream)
            if truncated:
                process.append("执行过程较长，仅保留了部分实时记录。")
            if process:
                item["process_text"] = "\n\n".join(dict.fromkeys(process))
                item["collapsed_title"] = "执行过程"
            items.append(item)
    return items


def _merge_collaboration_history(existing: list[dict[str, Any]], journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing)
    for item in journal:
        source = str(item.get("source_session_id") or "")
        turn = _turn_group_id(source)
        def same_turn(candidate: dict[str, Any]) -> bool:
            candidate_source = str(candidate.get("source_session_id") or "")
            if "::turn::" in candidate_source:
                return _turn_group_id(candidate_source) == turn
            request_id = str(item.get("request_id") or "")
            return bool(request_id and request_id == str(candidate.get("request_id") or ""))
        match = next((candidate for candidate in merged
                      if candidate.get("event_type") == item.get("event_type")
                      and candidate.get("node_id") == item.get("node_id")
                      and same_turn(candidate)
                      and abs(float(candidate.get("timestamp") or 0) - float(item.get("timestamp") or 0)) <= 5), None)
        if match is None:
            merged.append(item)
        elif item.get("event_type") == "team_submit":
            # Journal output is complete; event history may have truncated it.
            match["content"] = item["content"]
            for key in ("thinking", "tool_calls", "process_text", "collapsed_title", "turn_started_at", "turn_duration"):
                if item.get(key) and not match.get(key):
                    match[key] = item[key]
    return merged


def _collaboration_history_executions(crew: Any, session_id: str, owner_account_id: str) -> list[dict[str, Any]]:
    manager = getattr(crew, "team", None)
    store = getattr(manager, "_collaboration_execution_store", None)
    if store is not None:
        return store.for_session(owner_account_id, session_id)
    path = getattr(getattr(crew, "config", None), "db_path", None)
    if not path or not os.path.isfile(path):
        return []
    from crew.collaboration.runtime.store import ExecutionStore
    store = ExecutionStore(path, wal_enabled=getattr(crew.config, "sqlite_wal", True))
    try:
        return store.for_session(owner_account_id, session_id)
    finally:
        store.close()


async def team_internal_history_items(
    crew,
    session_id: str,
    child_sessions: list[tuple[str, list[Message]]],
    owner_account_id: str,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    profiles = _team_internal_member_profiles(
        crew,
        config,
        owner_account_id=owner_account_id,
    )
    event_history_fn = getattr(getattr(crew, "team", None), "event_history_for_session", None)
    if callable(event_history_fn):
        try:
            items.extend(event_history_fn(session_id, owner_account_id=owner_account_id))
        except Exception:  # noqa: BLE001
            pass
    try:
        executions = _collaboration_history_executions(crew, session_id, owner_account_id)
        journal_items = project_collaboration_history(executions, child_sessions, profiles)
    except Exception as exc:  # noqa: BLE001 - legacy event history remains usable
        log.warning("协作执行历史读取失败 session=%s error=%s", session_id, exc)
        journal_items = []
    if items:
        normalized_items = [
            _normalize_communication_identity(item, profiles)
            for item in items
        ]
        normalized_items = _enrich_team_submissions_with_member_records(
            normalized_items,
            child_sessions,
            profiles,
        )
        normalized_items = _merge_collaboration_history(normalized_items, journal_items)
        # Team workflow events remain the primary source.  Direct mention
        # answers are persisted in their member child sessions as the
        # canonical transcript, however, and must survive alongside older
        # workflow events in the same parent session.
        event_request_ids = {
            str(item.get("request_id") or "").strip()
            for item in normalized_items
            if item.get("communication_kind") == "user_mention_answer"
            and str(item.get("request_id") or "").strip()
        }
        direct_child_items = _child_session_history_items(
            child_sessions,
            profiles,
            communication_kind="user_mention_answer",
        )
        normalized_items.extend(
            item for item in direct_child_items
            if str(item.get("request_id") or "").strip() not in event_request_ids
        )
        return sorted(normalized_items, key=lambda value: float(value.get("timestamp") or 0))
    if journal_items:
        direct_child_items = _child_session_history_items(
            child_sessions, profiles, communication_kind="user_mention_answer",
        )
        return sorted([*journal_items, *direct_child_items], key=lambda value: float(value.get("timestamp") or 0))
    has_team_workflow_fn = getattr(getattr(crew, "team", None), "has_team_workflow_for_session", None)
    if callable(has_team_workflow_fn):
        try:
            if has_team_workflow_fn(session_id, owner_account_id=owner_account_id):
                # A Team workflow may coexist with a direct user mention.
                # The workflow has no event for the child answer in some
                # older/runtime-isolated sessions, so retain that canonical
                # communication transcript instead of dropping it wholesale.
                return sorted(
                    _child_session_history_items(
                        child_sessions,
                        profiles,
                        communication_kind="user_mention_answer",
                    ),
                    key=lambda value: float(value.get("timestamp") or 0),
                )
        except Exception:  # noqa: BLE001
            pass
    items.extend(_child_session_history_items(child_sessions, profiles))
    try:
        tasks = await _list_runtime_tasks(crew, limit=1000, owner_account_id=owner_account_id)
    except Exception:  # noqa: BLE001
        tasks = []
    prefix = f"{session_id}::turn::"
    for task in tasks:
        kind = str(task.get("kind") or "")
        if kind != "team":
            continue
        task_session_id = str(task.get("session_id") or "")
        if not task_session_id.startswith(prefix):
            continue
        content = str(task.get("result") or task.get("error") or "").strip()
        if is_team_chat_noise(content):
            continue
        identity = _team_internal_agent_identity(
            str(task.get("assignee") or (task.get("progress") or {}).get("member") or "agent").strip(),
            profiles,
        )
        items.append({
            "role": "team_internal",
            "content": content[:1200],
            **identity,
            "source_session_id": task_session_id,
            "timestamp": float(task.get("updated_at") or task.get("finished_at") or task.get("created_at") or 0),
        })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in sorted(items, key=lambda value: float(value.get("timestamp") or 0)):
        key = (
            str(item.get("agent_name") or ""),
            str(item.get("content") or "")[:160],
            str(item.get("communication_kind") or ""),
            str(item.get("communication_status") or ""),
            str(
                item.get("request_id")
                or item.get("reply_to")
                or item.get("source_session_id")
                or ""
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _turn_group_id(session_id: str) -> str:
    sid = str(session_id or "")
    marker = "::turn::"
    if marker not in sid:
        return sid
    head, rest = sid.split(marker, 1)
    request_id = rest.split("::", 1)[0]
    return f"{head}{marker}{request_id}"


async def team_tasks_with_plan_projection(
    crew,
    session_id: str,
    status: str | None,
    limit: int,
    owner_account_id: str,
) -> list[dict[str, Any]]:
    all_tasks = await _list_runtime_tasks(
        crew,
        status=status,
        limit=max(int(limit or 200), 1000),
        owner_account_id=owner_account_id,
    )
    prefix = f"{session_id}::turn::"
    concrete = [
        task for task in all_tasks
        if task.get("session_id") == session_id
        or str(task.get("session_id") or "").startswith(prefix)
    ]
    projection_fn = getattr(getattr(crew, "team", None), "task_projection_for_session", None)
    plan_tasks: list[dict[str, Any]] = []
    if callable(projection_fn):
        plan_tasks = projection_fn(
            session_id,
            owner_account_id=owner_account_id,
            limit=max(int(limit or 200), 200),
        )
    if plan_tasks:
        represented_turns = {
            str((task.get("progress") or {}).get("turn_session_id") or "").strip()
            for task in plan_tasks
            if str((task.get("progress") or {}).get("turn_session_id") or "").strip()
        }
        represented_turn_titles = {
            compact_history_content((task.get("progress") or {}).get("turn_title"))
            for task in plan_tasks
            if compact_history_content((task.get("progress") or {}).get("turn_title"))
        }
        team_turn_task_titles = {
            str(task.get("detail") or task.get("title") or "").strip()
            for task in concrete
            if str(task.get("kind") or "") == "team"
        }

        def _matches_team_turn(title: str) -> bool:
            if not title:
                return False
            return any(title in team_title or team_title in title for team_title in team_turn_task_titles if team_title)

        def _is_display_extra(task: dict[str, Any]) -> bool:
            kind = str(task.get("kind") or "")
            task_session_id = str(task.get("session_id") or "")
            if kind == "team":
                return True
            if kind != "agent_turn" or task_session_id != session_id:
                return False
            request_id = str(task.get("request_id") or "").strip()
            if request_id and f"{session_id}::turn::{request_id}" in represented_turns:
                return False
            if request_id:
                return True
            title = str(task.get("detail") or task.get("title") or "").strip()
            if compact_history_content(title) in represented_turn_titles:
                return False
            return not _matches_team_turn(title)

        represented = {
            str(item.get("progress", {}).get("delegate_task_id") or "")
            for item in plan_tasks
            if str(item.get("progress", {}).get("delegate_task_id") or "")
        }
        represented_nodes = {
            str(item.get("progress", {}).get("plan_node_id") or "")
            for item in plan_tasks
            if str(item.get("progress", {}).get("plan_node_id") or "")
        }
        extra = [
            task for task in concrete
            if _is_display_extra(task)
            and str(task.get("task_id") or task.get("id") or "") not in represented
            and str((task.get("progress") or {}).get("plan_node_id") or "") not in represented_nodes
        ]
        items = [*plan_tasks, *extra]
    else:
        items = [
            task for task in concrete
            if str(task.get("kind") or "") == "agent_turn"
            and str(task.get("session_id") or "") == session_id
        ]
    db_path = getattr(getattr(crew, "config", None), "db_path", None)
    if db_path:
        from crew.collaboration.runtime.store import ExecutionStore
        from crew.collaboration.runtime.projection import project_tasks
        journal = ExecutionStore(db_path, wal_enabled=getattr(crew.config, "sqlite_wal", True))
        try:
            items = project_tasks(journal.for_session(owner_account_id, session_id), items)
        finally:
            journal.close()
    if status:
        items = [item for item in items if item.get("status") == status]
    for item in items:
        progress = dict(item.get("progress") or {})
        if str(item.get("kind") or "") == "agent_turn" and str(item.get("session_id") or "") == session_id:
            task_id = str(item.get("task_id") or item.get("id") or "")
            progress.setdefault("turn_session_id", f"{session_id}::direct::{task_id}")
            progress.setdefault("turn_title", str(item.get("detail") or item.get("title") or "团队直答")[:120])
        else:
            progress.setdefault("turn_session_id", _turn_group_id(str(item.get("session_id") or session_id)))
            progress.setdefault("turn_title", str(item.get("detail") or item.get("title") or "团队任务")[:120])
        node_id = str(progress.get("plan_node_id") or item.get("task_id") or item.get("id") or "")
        display_progress = node_display_progress(
            node_id=node_id,
            title=str(item.get("title") or ""),
            assignee=str(item.get("assignee") or progress.get("assignee") or progress.get("member") or ""),
            error=str(item.get("error") or ""),
            result=str(item.get("result") or ""),
            metadata=progress,
        )
        for key, value in display_progress.items():
            progress.setdefault(key, value)
        result_progress = result_projection(str(item.get("error") or item.get("result") or ""))
        for key, value in result_progress.items():
            progress.setdefault(key, value)
        item["progress"] = progress
    items.sort(key=lambda item: float(item.get("created_at") or item.get("started_at") or 0))
    return items[:max(1, int(limit or 200))]
