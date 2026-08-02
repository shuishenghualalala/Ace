"""契约层：类型序列化与信封工厂。"""

import json

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, Message, ToolCall


def test_message_to_openai_assistant_with_tool_calls():
    msg = Message.assistant("", [ToolCall(id="c1", name="terminal", arguments={"command": "ls"})])
    d = msg.to_openai()
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "terminal"
    assert "ls" in d["tool_calls"][0]["function"]["arguments"]


def test_message_to_openai_empty_user_content_is_empty_string_not_null():
    """空 user/system 不得序列化为 content:null——OpenAI SDK / MiniMax 会 400。

    回归：plan 反馈修改时历史若混入空 user，会报
    ChatCompletionDeveloperMessageParam.content validation errors。
    """
    assert Message.user("").to_openai() == {"role": "user", "content": ""}
    assert Message.system("").to_openai() == {"role": "system", "content": ""}
    # assistant + tool_calls 仍允许 content 为 null（OpenAI 惯例）
    d = Message.assistant("", [ToolCall(id="c1", name="exit_plan_mode", arguments={})]).to_openai()
    assert d["content"] is None
    assert d["tool_calls"]


def test_messages_for_openai_drops_blank_user_keeps_tool_assistant():
    """历史里的空 user 不得进入 OpenAI payload（MiniMax 会拒空 role:user）。"""
    from crew.providers.openai_provider import _messages_for_openai

    msgs = [
        Message.system("sys"),
        Message.user("hi"),
        Message.assistant("", [ToolCall(id="c1", name="exit_plan_mode", arguments={})]),
        Message.tool("c1", "ok"),
        Message.user(""),  # 污染：应被丢弃
        Message.user("请根据以下反馈修改计划：\n去掉粒子"),
    ]
    out = _messages_for_openai(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[-1]["content"].startswith("请根据以下反馈")
    assert all(
        not (isinstance(m.get("content"), str) and m["content"] == "" and m["role"] == "user")
        for m in out
    )


def test_message_tool_role():
    d = Message.tool("c1", "result text").to_openai()
    assert d == {"role": "tool", "content": "result text", "tool_call_id": "c1"}


def test_envelope_of_sets_query_and_defaults():
    env = Envelope.of("hi", session_id="s1", mode="team")
    assert env.query == "hi"
    assert env.mode == "team"
    assert env.request_id.startswith("req_")


def test_response_chunk_factories():
    rid = "req_x"
    assert ResponseChunk.final(rid, "done").is_final is True
    assert ResponseChunk.error(rid, "boom").status == "failed"
    assert ResponseChunk.tool_event(rid, "terminal", "start").kind == "tool"


def test_file_write_tool_event_omits_content_from_frontend_payload():
    rid = "req_x"
    chunk = ResponseChunk.tool_event(
        rid,
        "file_write",
        "start",
        "{'path': '/tmp/a.html', 'content': 'large'}",
        args=json.dumps({"path": "/tmp/a.html", "content": "large", "append": True}),
    )
    assert json.loads(chunk.body["args"]) == {"path": "/tmp/a.html", "append": True}
    assert "content" not in chunk.body["args"]
    assert "content" not in chunk.body["detail"]


def test_browser_fill_form_envelope_reprojects_nested_private_values():
    private = "nested-private-form-value"
    raw = {
        "action": "fill_form",
        "fields": [
            {"type": "textbox", "ref": "p3:e1", "value": private},
            {
                "type": "combobox",
                "ref": "p3:e2",
                "value": "private-label",
                "select_by": "label",
            },
            {"type": "slider", "ref": "p3:e3", "value": "88"},
        ],
    }
    chunk = ResponseChunk.tool_event(
        "req_x",
        "browser_use",
        "start",
        json.dumps(raw, ensure_ascii=False),
        args=json.dumps(raw, ensure_ascii=False),
    )
    args = json.loads(chunk.body["args"])
    assert args["action"] == "fill_form"
    assert args["field_count"] == 3
    assert args["field_types"]["slider"] == 1
    encoded = json.dumps(chunk.body, ensure_ascii=False)
    assert private not in encoded
    assert "private-label" not in encoded
    assert '"88"' not in encoded


@pytest.mark.asyncio
async def test_fake_provider_stream_chat_splits_text():
    """FakeProvider.stream_chat 把预设响应拆成字符块发出。"""
    provider = FakeProvider(script=[ChatResponse(text="hello", finish_reason="stop")])
    chunks = []
    async for ch in provider.stream_chat([Message.user("hi")]):
        chunks.append(ch)
    # 应有中间 delta chunk + 最终 done chunk
    assert len(chunks) >= 2
    assert any(ch.delta_text for ch in chunks)
    final = chunks[-1]
    assert final.done is True
    assert final.finish_reason == "stop"


@pytest.mark.asyncio
async def test_fake_provider_stream_chat_preserves_tool_calls():
    """FakeProvider.stream_chat 在最终 chunk 携带完整 tool_calls。"""
    provider = FakeProvider(script=[
        ChatResponse(tool_calls=[ToolCall("c1", "terminal", {"command": "ls"})]),
    ])
    chunks = []
    async for ch in provider.stream_chat([Message.user("run")]):
        chunks.append(ch)
    final = chunks[-1]
    assert final.done is True
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].name == "terminal"
