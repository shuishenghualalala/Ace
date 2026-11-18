"""File read references must retain a full source in the provider request."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from crew.agent.compact import estimate_prompt_tokens
from crew.agent.compact.microcompact import micro_compact, prune_tool_results
from crew.agent.executor import BuiltinExecutor, FinalRequestView
from crew.agent.executor.file_read_projection import project_file_reads
from crew.agent.loop.tool_guardrails import ToolCallGuardrailController, append_toolguard_guidance
from crew.core.mocks import FakeProvider
from crew.core.types import Message, StreamChunk, ToolCall
from crew.plugins.manager import LLM_EXECUTION_MIDDLEWARE, PluginManager
from crew.tools.registry import Registry


@pytest.fixture
def read_payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "success": True,
        "path": str(tmp_path / "notes.txt"),
        "file_version": "3f10",
        "offset": 2,
        "limit": 2,
        "content": "   2: second\n   3: third",
        "total_lines": 10,
        "shown_lines": 2,
        "truncated": True,
        "read_complete": False,
        "next_offset": 4,
    }


def _call_and_read(call_id: str, payload: dict[str, Any]) -> tuple[Message, Message]:
    call = ToolCall(call_id, "file_read", {"path": str(payload.get("path", "notes.txt"))})
    return Message.assistant(tool_calls=[call]), Message.tool(call_id, json.dumps(payload), name="file_read")


def test_repeat_inside_parallel_batch_references_only_original(read_payload):
    first_call, first = _call_and_read("first", read_payload)
    second_call, second = _call_and_read("second", read_payload)
    third_call, third = _call_and_read("third", read_payload)
    assistant = Message.assistant(tool_calls=first_call.tool_calls + second_call.tool_calls + third_call.tool_calls)
    canonical = [assistant, first, second, third]

    projected = project_file_reads(canonical)
    assert projected[:2] == canonical[:2]
    expected = {key: value for key, value in read_payload.items() if key != "content"}
    expected.update(unchanged=True, source_tool_call_id="first")
    for original, shortened in zip(canonical[2:], projected[2:]):
        assert json.loads(original.content) == read_payload
        assert json.loads(shortened.content) == expected
        assert shortened.tool_call_id == original.tool_call_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_version", "new-revision"),
        ("offset", -2),
        ("limit", 3),
        ("content", "different content"),
        ("next_offset", 5),
        ("shown_lines", True),
    ],
)
def test_changes_in_read_payload_keep_content(read_payload, field, value):
    first = _call_and_read("first", read_payload)
    second = _call_and_read("second", {**read_payload, field: value})
    assert project_file_reads([*first, *second])[-1] is second[-1]


def test_canonical_path_is_part_of_read_identity(read_payload):
    first = _call_and_read("first", read_payload)
    other = str(Path(read_payload["path"]).with_name("other.txt"))
    second = _call_and_read("second", {**read_payload, "path": other})
    assert project_file_reads([*first, *second])[-1] is second[-1]


@pytest.mark.parametrize("source_kind", ["orphan", "out_of_order", "ambiguous_id", "stub", "pruned", "other_tool"])
def test_nonvisible_or_invalid_source_cannot_anchor_reference(read_payload, source_kind):
    call, result = _call_and_read("first", read_payload)
    second = _call_and_read("second", read_payload)
    prefix = [call, result]
    if source_kind == "orphan":
        prefix = [result]
    elif source_kind == "out_of_order":
        prefix = [result, call]
    elif source_kind == "ambiguous_id":
        prefix.append(call)
    elif source_kind == "stub":
        stub = {**read_payload, "unchanged": True, "source_tool_call_id": "old"}
        prefix[-1] = replace(result, content=json.dumps(stub))
    elif source_kind == "pruned":
        prefix[-1] = replace(result, content="[file_read] compacted result")
    else:
        prefix[-1] = replace(result, name="grep")
    assert project_file_reads([*prefix, *second])[-1] is second[-1]


def test_forced_result_keeps_text_and_can_anchor_later_read(read_payload):
    previous = _call_and_read("previous", read_payload)
    forced = _call_and_read("forced", {**read_payload, "force_refresh": True})
    previous_view = project_file_reads([*previous, *forced])
    assert previous_view[-1] is forced[-1]

    current = _call_and_read("current", read_payload)
    projected = project_file_reads([*forced, Message.user("Continue"), *current])
    assert json.loads(projected[-1].content)["source_tool_call_id"] == "forced"


def test_budget_shortened_result_cannot_anchor_a_duplicate(read_payload):
    read_payload["content"] *= 500
    first = _call_and_read("first", read_payload)
    second = _call_and_read("second", read_payload)

    projected = project_file_reads([*first, *second], max_content_chars=100)

    for index in (1, 3):
        result = json.loads(projected[index].content)
        assert result["context_truncated"] is True
        assert result["context_original_content_chars"] == len(read_payload["content"])
        assert len(result["content"]) == 100
        assert result["path"] == read_payload["path"]
        assert result["file_version"] == read_payload["file_version"]
        assert "source_tool_call_id" not in result

    forced = _call_and_read("forced", {**read_payload, "force_refresh": True})
    projected = project_file_reads([*forced, *second], max_content_chars=100)
    assert projected[1] is forced[1]
    assert json.loads(projected[3].content)["source_tool_call_id"] == "forced"


def test_microcompact_preserves_file_result_json_for_final_budget(read_payload):
    read_payload["content"] = ('first\n"slash\\汉字"\nlast' * 2200)
    assistant, original = _call_and_read("big-file", read_payload)

    compacted = micro_compact([assistant, original], max_tool_result_chars=20_000)
    budgeted = project_file_reads(compacted, max_content_chars=500)
    overflow_pruned = prune_tool_results([assistant, original])

    assert len(compacted[-1].content) <= 20_000
    for projected in (compacted[-1], budgeted[-1], overflow_pruned[-1]):
        visible = json.loads(projected.content)
        assert projected.tool_call_id == "big-file"
        assert visible["content"].startswith("first")
        assert visible["content"].endswith("last")
        assert visible["offset"] == read_payload["offset"]
        assert visible["file_version"] == read_payload["file_version"]
        assert visible["context_original_content_chars"] == len(read_payload["content"])
        assert visible["context_truncated"] is True
    assert json.loads(original.content) == read_payload
    assert len(json.loads(budgeted[-1].content)["content"]) == 500


def test_projected_repeat_keeps_runtime_loop_warning(read_payload):
    guardrails = ToolCallGuardrailController()
    messages = []
    for call_id in ("first", "second", "third"):
        assistant, result = _call_and_read(call_id, read_payload)
        decision = guardrails.after_call("file_read", assistant.tool_calls[0].arguments, result.content)
        messages.extend([assistant, replace(result, content=append_toolguard_guidance(result.content, decision))])

    projected = project_file_reads(messages)
    assert projected[1] is messages[1]
    for index, count in ((3, 2), (5, 3)):
        original = json.loads(messages[index].content)
        duplicate = json.loads(projected[index].content)
        assert original["content"] == read_payload["content"]
        assert duplicate["guardrail"] == original["guardrail"]
        assert duplicate["guardrail"]["count"] == count
        assert duplicate["guardrail"]["code"] == "idempotent_no_progress_warning"
        assert duplicate["source_tool_call_id"] == "first" and "content" not in duplicate


def test_tool_trailing_text_is_not_interpreted_as_a_warning(read_payload):
    first_call, first = _call_and_read("first", read_payload)
    second_call, second = _call_and_read("second", read_payload)
    with_trailing_text = replace(
        second, content=second.content + "\n\n[Tool loop warning: idempotent_no_progress_warning; count=2; plugin text]",
    )
    assert project_file_reads([first_call, first, second_call, with_trailing_text])[-1] is with_trailing_text
    assert project_file_reads([second_call, with_trailing_text, first_call, first])[-1] is first


class _RecordingProvider(FakeProvider):
    async def stream_chat(self, messages, tools=None, **kwargs):
        self.sent_messages = messages
        yield StreamChunk(done=True, finish_reason="stop")


@pytest.mark.parametrize("keep_source", [True, False])
async def test_projection_is_done_after_execution_middleware(read_payload, keep_source):
    provider = _RecordingProvider()
    plugins = PluginManager()
    first = _call_and_read("first", read_payload)
    second = _call_and_read("second", read_payload)

    async def choose_messages(request, next_call, **_kwargs):
        assert json.loads(request["messages"][-1].content) == read_payload
        return await next_call(request if keep_source else {**request, "messages": request["messages"][2:]})

    plugins._middleware.setdefault(LLM_EXECUTION_MIDDLEWARE, []).append(choose_messages)
    executor = BuiltinExecutor(provider, Registry(), plugins, backoff_seconds=0)
    original = FinalRequestView.create([*first, *second])
    response = {}
    events = [event async for event in executor._call_model(original, "request", lambda: 1, response)]

    assert not events
    actual = json.loads(provider.sent_messages[-1].content)
    if keep_source:
        assert actual["source_tool_call_id"] == "first" and "content" not in actual
    else:
        assert actual == read_payload
    assert json.loads(original.messages[-1].content) == read_payload
    assert response["usage"]["request_view_tokens"] == estimate_prompt_tokens(provider.sent_messages, None)


async def test_budget_projects_rereadable_result_without_mutating_history(read_payload):
    provider = _RecordingProvider()
    provider.context_window = 1100
    executor = BuiltinExecutor(provider, Registry(), PluginManager(), backoff_seconds=0)
    read_payload["content"] *= 500
    assistant, tool = _call_and_read("recent", read_payload)
    view = FinalRequestView.create([Message.user("use the result"), assistant, tool])
    response = {}

    events = [event async for event in executor._call_model(view, "request", lambda: 1, response)]

    assert not events
    assert provider.sent_messages[1] is assistant
    sent = provider.sent_messages[-1]
    assert sent.tool_call_id == "recent"
    assert json.loads(sent.content)["context_truncated"] is True
    assert json.loads(tool.content) == read_payload
    assert response["usage"]["request_view_tokens"] == estimate_prompt_tokens(provider.sent_messages, None)
