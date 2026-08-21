"""Team-specific history and task projections for gateway routes."""

from __future__ import annotations

from typing import Any

from crew.core.types import Message
from crew.team.result_presenter import is_team_chat_noise, node_display_progress, result_projection
from crew.team.roles import CREW_BUILTIN_AGENT_ID, LEGACY_CREW_BUILTIN_AGENT_ID, is_crew_builtin_display_id


def compact_history_content(value: Any) -> str:
    return " ".join(str(value or "").split())


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


def team_visible_history_items(
    crew,
    session_id: str,
    owner_account_id: str = "",
    config: dict[str, Any] | None = None,
    has_child_team_sessions: bool = False,
    suppressed_request_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        tasks = crew.tasks.list_tasks(limit=1000, owner_account_id=owner_account_id)
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
            **({"duration": tc.duration} if tc.duration is not None else {}),
        })
    return tool_calls


def _team_internal_member_profiles(
    crew,
    config: dict[str, Any] | None,
    *,
    owner_account_id: str = "",
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
        if "::turn::" not in child_id:
            continue
        identity = _team_internal_agent_identity(child_id.rsplit("::", 1)[-1], profiles)
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


def team_internal_history_items(
    crew,
    session_id: str,
    child_sessions: list[tuple[str, list[Message]]],
    owner_account_id: str = "",
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
    if items:
        normalized_items = [
            _normalize_communication_identity(item, profiles)
            for item in items
        ]
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
        tasks = crew.tasks.list_tasks(limit=1000, owner_account_id=owner_account_id)
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


def team_tasks_with_plan_projection(
    crew,
    session_id: str,
    status: str | None,
    limit: int,
    owner_account_id: str = "",
) -> list[dict[str, Any]]:
    all_tasks = crew.tasks.list_tasks(
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
