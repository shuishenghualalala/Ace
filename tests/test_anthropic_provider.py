"""AnthropicProvider Messages API adapter tests."""

from __future__ import annotations

import json

import httpx
import pytest

from crew.agent.executor import BuiltinExecutor, ExecutionContext
from crew.core.types import Message
from crew.plugins.manager import PluginManager
from crew.providers.anthropic_provider import AnthropicProvider
from crew.tools.registry import Registry, tool_result

pytestmark = pytest.mark.asyncio


def _provider(handler) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="sk-ant", base_url="https://anthropic.test", model="claude-test")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


async def test_chat_maps_messages_tools_and_response():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )

    provider = _provider(handler)
    response = await provider.chat(
        [Message.system("sys"), Message.user("hi")],
        tools=[{"type": "function", "function": {"name": "search", "description": "s", "parameters": {"type": "object"}}}],
    )

    assert seen["headers"]["x-api-key"] == "sk-ant"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert seen["body"]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert seen["body"]["tools"][0]["input_schema"] == {"type": "object"}
    assert response.text == "ok"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.usage["prompt_tokens"] == 7
    assert response.usage["completion_tokens"] == 3
    assert response.usage["cache_creation_input_tokens"] == 0
    assert response.usage["cache_read_input_tokens"] == 0


async def test_stream_emits_text_ready_tool_and_done():
    async def stream_body():
        events = [
            ("message_start", {"message": {"usage": {"input_tokens": 2}}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "he"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "llo"}}),
            ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"}}),
            ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'}}),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
            ("message_stop", {}),
        ]
        for event, data in events:
            yield f"event: {event}\n".encode()
            yield f"data: {json.dumps(data)}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is True
        return httpx.Response(200, content=stream_body())

    provider = _provider(handler)
    chunks = [chunk async for chunk in provider.stream_chat([Message.user("hi")])]

    assert "".join(chunk.delta_text for chunk in chunks) == "hello"
    ready = [chunk.ready_tool_call for chunk in chunks if chunk.ready_tool_call is not None]
    assert ready[0].id == "toolu_1"
    assert ready[0].arguments == {"q": "x"}
    done = next(chunk for chunk in chunks if chunk.done)
    assert done.finish_reason == "tool_use"
    assert done.tool_calls[0].name == "search"


async def test_stream_never_marks_incomplete_tool_arguments_ready():
    async def stream_body():
        events = [
            ("message_start", {"message": {"usage": {"input_tokens": 2}}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_bad", "name": "file_read"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"half'}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 4096}}),
            ("message_stop", {}),
        ]
        for event, data in events:
            yield f"event: {event}\n".encode()
            yield f"data: {json.dumps(data)}\n\n".encode()

    provider = _provider(lambda _request: httpx.Response(200, content=stream_body()))
    chunks = [chunk async for chunk in provider.stream_chat([Message.user("read")])]
    await provider.aclose()

    assert not any(chunk.ready_tool_call is not None for chunk in chunks)
    done = next(chunk for chunk in chunks if chunk.done)
    assert done.finish_reason == "max_tokens"
    assert done.tool_calls[0].arguments == {"_raw": '{"path":"half'}


async def test_long_tool_arguments_recover_end_to_end_through_provider_and_executor():
    """真实 SSE 分片经 Provider 解析后，Executor 应提高额度并只执行完整长参数。"""
    long_content = "x" * 20_000
    complete_input = json.dumps(
        {"path": "large.txt", "content": long_content},
        ensure_ascii=False,
    )
    request_max_tokens: list[int] = []
    request_count = 0

    def sse_response(events):
        async def stream_body():
            for event, data in events:
                yield f"event: {event}\n".encode()
                yield f"data: {json.dumps(data)}\n\n".encode()

        return httpx.Response(200, content=stream_body())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        body = json.loads(request.content.decode("utf-8"))
        request_max_tokens.append(body["max_tokens"])
        if request_count == 1:
            tool_input = complete_input[: len(complete_input) // 2]
            stop_reason = "max_tokens"
        elif request_count == 2:
            tool_input = complete_input
            stop_reason = "tool_use"
        else:
            return sse_response([
                ("message_start", {"message": {"usage": {"input_tokens": 2}}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "done"}}),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
                ("message_stop", {}),
            ])
        return sse_response([
            ("message_start", {"message": {"usage": {"input_tokens": 2}}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"toolu_{request_count}", "name": "file_write"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": tool_input}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 4096}}),
            ("message_stop", {}),
        ])

    provider = _provider(handler)
    received: list[dict] = []
    registry = Registry()
    registry.register(
        name="file_write",
        toolset="file",
        schema={"name": "file_write", "parameters": {}},
        handler=lambda arguments: received.append(arguments) or tool_result(ok=True),
        is_async=False,
    )
    executor = BuiltinExecutor(provider, registry, PluginManager())
    ctx = ExecutionContext(
        session_id="long-tool",
        request_id="request",
        system_prompt="system",
        messages=[Message.user("write a long file")],
        query="write a long file",
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]
    await provider.aclose()

    assert request_max_tokens == [4096, 64_000, 64_000]
    assert len(received) == 1
    assert received[0]["path"] == "large.txt"
    assert received[0]["content"] == long_content
    assert not any(chunk.kind == "error" for chunk in chunks)
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == "done"


async def test_multimodal_message_after_tool_result_uses_anthropic_image_block():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "看到了"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = _provider(handler)
    image = Message(
        role="user",
        content="浏览器截图",
        is_meta=True,
        content_parts=[
            {"type": "text", "text": "浏览器截图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    )
    await provider.chat([Message.tool("toolu_1", "snapshot complete", name="browser_vision"), image])

    messages = seen["body"]["messages"]
    assert messages[0]["content"][0]["type"] == "tool_result"
    assert messages[1]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
