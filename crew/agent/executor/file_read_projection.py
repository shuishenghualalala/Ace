"""Shorten repeated file reads in a single outgoing model request."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

from crew.agent.compact.microcompact import file_read_result_payload, preview_file_read_result
from crew.agent.loop.tool_guardrails import IDEMPOTENT_NO_PROGRESS_WARNING
from crew.core.types import Message


def project_file_reads(messages: Sequence[Message], *, max_content_chars: int | None = None) -> list[Message]:
    """Reference intact results, or bound a rereadable result during budget recovery."""
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
        payload = file_read_result_payload(message)
        if payload is None:
            continue

        forced = visible_calls[call_id] or payload.get("force_refresh") is True
        if payload.get("context_truncated") is True:
            if not forced and max_content_chars is not None:
                shortened = preview_file_read_result(payload, max_content_chars)
                if shortened is not None:
                    projected[-1] = replace(message, content=shortened)
            continue
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
        source = sources.get(key)
        if source is not None and not forced:
            payload.pop("content")
            payload.update(unchanged=True, source_tool_call_id=source)
            projected[-1] = replace(message, content=json.dumps(payload, ensure_ascii=False))
            continue
        if not forced and max_content_chars is not None:
            shortened = preview_file_read_result(payload, max_content_chars)
            if shortened is not None:
                projected[-1] = replace(message, content=shortened)
                continue
        sources.setdefault(key, call_id)
    return projected
