"""Rebuild model-visible tool messages from the last interrupted request's journal."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from crew.core.types import Message, ToolCall
from crew.tools.pipeline import restore_tool_result_reference

_UNVERIFIED = "工具调用已经开始，退出前没有完整的结果记录。请先核对实际状态和已有改动，再决定是否重试。"
_NOT_STARTED = "工具调用参数已生成，但未记录执行意图。该调用没有开始执行。"
_LEGACY_UNKNOWN = "旧版记录缺少完整调用参数和执行凭据，无法确认是否已执行。请先核对实际状态。"
_MISSING_RESULT = "旧版记录确认工具已返回，但没有保存返回内容。请先核对已有结果，再决定是否重试。"


@dataclass
class _Call:
    name: str
    arguments: dict | None = None
    intent: bool = False
    result: dict | None = None
    conflict: bool = False


@dataclass
class _Step:
    text: str = ""
    model: str | None = None
    calls: dict[str, _Call] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def recover_tool_messages(
    rows: list[tuple[str, int, str, str]], session_id: str, request_id: str,
) -> tuple[list[Message], bool]:
    """Pair events strictly inside one user request and model step, retaining the model's call order."""
    steps: dict[int, _Step] = {}
    in_request = False
    for source, _seq, kind, raw in rows:
        if source != session_id:
            continue
        if kind == "user_message":
            user = json.loads(raw)
            if not user.get("is_meta") and user.get("request_id"):
                in_request = user.get("request_id") == request_id
                steps.clear()
            continue
        if kind in {"turn_end", "turn_outcome"} and in_request:
            if json.loads(raw).get("request_id", request_id) == request_id:
                steps.clear()
                in_request = False
        if not in_request or kind not in {"assistant_tool_calls", "tool_execution_intent", "tool_result_commit"}:
            continue
        item = json.loads(raw)
        if item.get("request_id", request_id) != request_id:
            continue
        execution_id = item.get("execution_session_id")
        if execution_id is not None and execution_id not in {session_id, f"{session_id}::turn::{request_id}"}:
            continue
        step = steps.setdefault(int(item.get("step") or 0), _Step())
        if kind == "assistant_tool_calls":
            step.text = item.get("text") or ""
            step.model = item.get("model") or None
            records = item.get("tool_calls", [])
        else:
            records = [item]
        for record in records:
            cid, name = record.get("tool_call_id"), record.get("name")
            if not isinstance(cid, str) or not cid or not isinstance(name, str) or not name:
                continue
            call = step.calls.setdefault(cid, _Call(name))
            args = record.get("arguments")
            if call.name != name or (isinstance(args, dict) and call.arguments is not None and args != call.arguments):
                call.conflict = True
            if isinstance(args, dict):
                call.arguments = args
            if kind == "assistant_tool_calls" and cid not in step.order:
                step.order.append(cid)
            elif kind == "tool_execution_intent":
                call.intent = True
            elif kind == "tool_result_commit":
                if call.result is not None and any(
                    call.result.get(key) != item.get(key) for key in ("result", "result_file_digest", "media", "status")
                ):
                    call.conflict = True
                call.result = item

    recovered: list[Message] = []
    unresolved = False
    for _number, step in sorted(steps.items()):
        calls, outputs, images = [], [], []
        for cid in dict.fromkeys([*step.order, *step.calls]):
            entry = step.calls[cid]
            completed = entry.result or {}
            result = completed.get("result")
            verified = isinstance(result, str) and entry.arguments is not None and not entry.conflict
            if verified:
                digest = completed.get("result_file_digest")
                content = restore_tool_result_reference(cid, result, digest) if digest else result
                for image in completed.get("media", []):
                    url = image.get("data_url")
                    if isinstance(url, str) and url.startswith("data:image/"):
                        images.append(Message.tool_image(cid, entry.name, url, image.get("alt", ""), image.get("detail", "auto")))
                if completed.get("missing_media"):
                    content += "\n[先前工具的文本结果已恢复，部分附加媒体在记录时不可用。]"
            elif completed.get("status") in {"ok", "error"} and not entry.conflict:
                content = _MISSING_RESULT
            elif entry.arguments is None and not entry.intent:
                content = _LEGACY_UNKNOWN
            else:
                content = _UNVERIFIED if entry.intent or completed or entry.conflict else _NOT_STARTED
            unresolved |= not verified and (entry.intent or bool(completed) or entry.conflict or entry.arguments is None)
            tc = ToolCall(cid, entry.name, entry.arguments or {})
            if not verified:
                tc.status, tc.result = "error", content
            elif completed.get("status") != "ok":
                tc.status = "error"
            exit_confirmed = (completed.get("details") or {}).get("exit_confirmed")
            if isinstance(exit_confirmed, bool):
                tc.exit_confirmed = exit_confirmed
            if completed.get("code") == "tool_exit_unconfirmed":
                tc.exit_confirmed = False
                tc.result = "已请求停止工具，实际退出尚未确认。"
                unresolved = True
            calls.append(tc)
            outputs.append(Message.tool(cid, content, name=entry.name))
        if calls:
            recovered.append(Message.assistant(step.text, calls, model=step.model))
            recovered.extend(outputs)
            recovered.extend(images)
    return recovered, unresolved


def merge_tool_messages(history: list[Message], user: Message, additions: list[Message]) -> list[Message]:
    """Keep an already published assistant/tool block and fill any missing results at that block."""
    merged = list(history)
    cursor = next(i for i, item in enumerate(merged) if item is user) + 1
    index = 0
    while index < len(additions):
        assistant = additions[index]
        end = index + 1 + len(assistant.tool_calls)
        outputs = additions[index + 1:end]
        index = end
        while index < len(additions) and additions[index].attachment_type == "tool_media":
            index += 1
        images = additions[end:index]
        key = [(c.id, c.name, c.arguments) for c in assistant.tool_calls]
        match = next((i for i in range(cursor, len(merged)) if merged[i].tool_calls
                      and [(c.id, c.name, c.arguments) for c in merged[i].tool_calls] == key), None)
        if match is None:
            position = next((i for i in range(cursor, len(merged)) if merged[i].role == "assistant"), len(merged))
            block = [assistant, *outputs, *images]
            merged[position:position] = block
            cursor = position + len(block)
            continue
        start = cursor = match + 1
        existing = {}
        while cursor < len(merged) and merged[cursor].role == "tool":
            previous = merged[cursor]
            existing[previous.tool_call_id] = previous
            cursor += 1
        ordered = [existing.pop(output.tool_call_id, output) for output in outputs]
        ordered.extend(existing.values())
        merged[start:cursor] = ordered
        cursor = start + len(ordered)
        existing_media = []
        while cursor < len(merged) and merged[cursor].attachment_type == "tool_media":
            existing_media.append(merged[cursor])
            cursor += 1
        new_media = [media for media in images if media not in existing_media]
        merged[cursor:cursor] = new_media
        cursor += len(new_media)
    return merged
