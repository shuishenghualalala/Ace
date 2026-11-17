"""Shorten repeated file reads in a single outgoing model request."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from crew.agent.loop.tool_guardrails import IDEMPOTENT_NO_PROGRESS_WARNING
from crew.core.types import Message


def _read_payload(message: Message) -> dict[str, Any] | None:
    if message.name not in (None, "file_read") or message.content_parts:
        return None
    if not isinstance(message.content, str):
        return None
    try:
        payload = json.loads(message.content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True or "error" in payload:
        return None
    if "unchanged" in payload or "source_tool_call_id" in payload:
        return None
    path = payload.get("path")
    version = payload.get("file_version")
    offset = payload.get("offset")
    limit = payload.get("limit")
    content = payload.get("content")
    if (
        not isinstance(path, str) or not Path(path).is_absolute()
        or not isinstance(version, str) or not version
        or type(offset) is not int or offset == 0
        or type(limit) is not int or limit < 1
        or not isinstance(content, str) or not content
    ):
        return None
    return payload


def project_file_reads(messages: Sequence[Message]) -> list[Message]:
    """Reference earlier intact results that remain in this exact request."""
    call_counts = Counter(
        call.id
        for message in messages if message.role == "assistant"
        for call in message.tool_calls if isinstance(call.id, str)
    )
    result_counts = Counter(
        message.tool_call_id
        for message in messages if message.role == "tool" and isinstance(message.tool_call_id, str)
    )
    visible_calls: dict[str, bool] = {}
    sources: dict[str, str] = {}
    projected: list[Message] = []
    for message in messages:
        projected.append(message)
        if message.role == "assistant":
            for call in message.tool_calls:
                if (
                    call.name == "file_read" and isinstance(call.id, str) and call.id
                    and isinstance(call.arguments, dict)
                    and isinstance(call.arguments.get("path"), str) and call.arguments["path"]
                    and call_counts[call.id] == result_counts[call.id] == 1
                ):
                    visible_calls[call.id] = call.arguments.get("force_refresh") is True
            continue
        call_id = message.tool_call_id
        if message.role != "tool" or not isinstance(call_id, str) or call_id not in visible_calls:
            continue
        payload = _read_payload(message)
        if payload is None:
            continue

        forced = visible_calls[call_id] or payload.get("force_refresh") is True
        comparable = {key: value for key, value in payload.items() if key != "force_refresh"}
        warning = comparable.get("guardrail")
        if (
            isinstance(warning, dict) and warning.get("action") == "warn"
            and warning.get("code") == IDEMPOTENT_NO_PROGRESS_WARNING
            and warning.get("tool_name") == "file_read"
        ):
            comparable.pop("guardrail")
        try:
            key = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, RecursionError):
            continue
        source = sources.setdefault(key, call_id)
        if source == call_id or forced:
            continue
        payload.pop("content")
        payload.update(unchanged=True, source_tool_call_id=source)
        projected[-1] = replace(message, content=json.dumps(payload, ensure_ascii=False))
    return projected
