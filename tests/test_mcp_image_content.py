"""MCP Client 对 ImageContent 的提取与 ToolRunner 多模态注入测试。

本文件不依赖真实 cua-driver，仅用 mock 对象验证 mcp_client._extract_text
能正确保留图片，且 tool_runner 能把图片注入为 Message.content_parts。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("mcp")  # 未装 mcp 包则跳过

from crew.agent.loop.tool_runner import ToolRunner
from crew.core.types import Message, ToolCall
from crew.tools.mcp_client import _extract_text
from crew.tools.registry import Registry


# ---------------------------------------------------------------------------
# _extract_text 单元测试
# ---------------------------------------------------------------------------

def _make_result(*blocks: Any, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), isError=is_error)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _image_block(data: str, mime_type: str = "image/png") -> SimpleNamespace:
    return SimpleNamespace(type="image", data=data, mimeType=mime_type)


def test_extract_text_plain_still_returns_text():
    """纯文本结果保持原有行为，不包装 JSON。"""
    result = _make_result(_text_block("hello"), _text_block("world"))
    assert _extract_text(result) == "hello\nworld"


def test_extract_text_empty_returns_placeholder():
    assert _extract_text(_make_result()) == "(空结果)"


def test_extract_text_error_returns_error_json():
    result = _make_result(_text_block("boom"), is_error=True)
    out = json.loads(_extract_text(result))
    assert out["error"] == "boom"


def test_extract_text_mixed_returns_json_with_images():
    result = _make_result(
        _text_block("AX tree here"),
        _image_block("iVBORw0KGgo="),
    )
    out = json.loads(_extract_text(result))
    assert out["text"] == "AX tree here"
    assert len(out["images"]) == 1
    assert out["images"][0]["mime_type"] == "image/png"
    assert out["images"][0]["data"] == "iVBORw0KGgo="
    assert out["images"][0]["url"].startswith("data:image/png;base64,")


def test_extract_text_image_only_returns_json():
    result = _make_result(_image_block("abc123", mime_type="image/jpeg"))
    out = json.loads(_extract_text(result))
    assert out["text"] == ""
    assert out["images"][0]["mime_type"] == "image/jpeg"


def test_extract_text_dict_blocks_are_tolerated():
    """某些 transport 可能把 block 序列化为 dict，需兼容。"""
    result = SimpleNamespace(
        content=[{"type": "text", "text": "hi"}, {"type": "image", "data": "x", "mimeType": "image/png"}],
        isError=False,
    )
    out = json.loads(_extract_text(result))
    assert out["text"] == "hi"
    assert len(out["images"]) == 1


# ---------------------------------------------------------------------------
# ToolRunner 多模态注入测试
# ---------------------------------------------------------------------------

def _make_registry_with_image_tool() -> Registry:
    """构造一个 Registry，其 fake__snapshot 工具返回含图片的 JSON。"""
    reg = Registry()

    async def fake_snapshot(args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "text": "window state",
                "images": [
                    {"mime_type": "image/png", "data": "iVBORw0KGgo=", "url": "data:image/png;base64,iVBORw0KGgo="}
                ],
            },
            ensure_ascii=False,
        )

    reg.register(
        name="fake__snapshot",
        toolset="mcp:fake",
        schema={
            "name": "fake__snapshot",
            "description": "fake snapshot",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=fake_snapshot,
        is_async=True,
    )
    return reg


async def test_tool_runner_attaches_image_content_parts():
    from crew.agent.loop import ToolCallGuardrailConfig, ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    reg = _make_registry_with_image_tool()
    guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig())
    runner = ToolRunner(reg, PluginManager(), guardrails)

    tc = ToolCall("tc-1", "fake__snapshot", {})
    messages: list[Message] = []
    seq = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    chunks = []
    async for chunk in runner.run_batch([tc], messages, "rid-1", next_seq):
        chunks.append(chunk)

    # 消息链中应包含 tool result + 一条带 content_parts 的 user message
    assert len(messages) == 2
    assert messages[0].role == "tool"
    assert messages[0].tool_call_id == "tc-1"
    # tool message 应被改写为纯文本骨架，避免与 content_parts 中的图片重复。
    tool_payload = json.loads(messages[0].content)
    assert tool_payload["text"] == "window state"
    assert tool_payload["images"] == []
    assert messages[1].role == "user"
    assert messages[1].is_meta is True
    assert messages[1].content_parts is not None
    assert len(messages[1].content_parts) == 2
    assert messages[1].content_parts[0]["type"] == "text"
    assert messages[1].content_parts[1]["type"] == "image_url"
    assert "data:image/png;base64" in messages[1].content_parts[1]["image_url"]["url"]


async def test_tool_runner_non_mcp_images_left_untouched():
    """非 MCP 工具即使返回 images 字段也不应触发多模态注入。"""
    from crew.agent.loop import ToolCallGuardrailConfig, ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    reg = Registry()

    async def fake_local_snapshot(args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "text": "local window",
                "images": [
                    {"mime_type": "image/png", "data": "iVBORw0KGgo=", "url": "data:image/png;base64,iVBORw0KGgo="}
                ],
            },
            ensure_ascii=False,
        )

    reg.register(
        name="local__snapshot",
        toolset="local",
        schema={
            "name": "local__snapshot",
            "description": "local snapshot",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=fake_local_snapshot,
        is_async=True,
    )

    guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig())
    runner = ToolRunner(reg, PluginManager(), guardrails)
    tc = ToolCall("tc-3", "local__snapshot", {})
    messages: list[Message] = []
    seq = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    async for _ in runner.run_batch([tc], messages, "rid-3", next_seq):
        pass

    assert len(messages) == 1
    assert messages[0].role == "tool"
    # 非 MCP 工具保留原始 content，不应被剥离图片字段。
    tool_payload = json.loads(messages[0].content)
    assert tool_payload["text"] == "local window"
    assert len(tool_payload["images"]) == 1


async def test_tool_runner_plain_text_no_extra_message():
    """纯文本 MCP 结果不追加额外 message。"""
    from crew.agent.loop import ToolCallGuardrailConfig, ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    reg = Registry()

    async def fake_text(args: dict[str, Any]) -> str:
        return "plain text"

    reg.register(
        name="fake__text",
        toolset="mcp:fake",
        schema={
            "name": "fake__text",
            "description": "fake text",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=fake_text,
        is_async=True,
    )

    guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig())
    runner = ToolRunner(reg, PluginManager(), guardrails)
    tc = ToolCall("tc-2", "fake__text", {})
    messages: list[Message] = []
    seq = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    async for _ in runner.run_batch([tc], messages, "rid-2", next_seq):
        pass

    assert len(messages) == 1
    assert messages[0].role == "tool"
