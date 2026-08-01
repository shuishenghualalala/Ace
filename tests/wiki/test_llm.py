"""crew.wiki._llm.chat_text 的单元测试。

背景：部分网关（如 minimax）对长生成的非流式请求会挂起直至超时/504，
wiki 的编译/摘要因此统一走流式优先调用。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from crew.core.interfaces import LLMProvider
from crew.core.types import ChatResponse, Message, StreamChunk
from crew.wiki._llm import chat_text
from crew.wiki.compiler import WikiCompiler
from crew.wiki.schemas import RawSource
from crew.wiki.store import FileSystemWikiStore


class _ScriptedProvider(LLMProvider):
    """可分别控制 chat / stream_chat 行为的 provider。"""

    def __init__(
        self,
        *,
        stream_chunks: list[StreamChunk] | None = None,
        stream_error: Exception | None = None,
        stream_hang: bool = False,
        chat_text: str = "",
        chat_error: Exception | None = None,
    ) -> None:
        self.stream_chunks = stream_chunks or []
        self.stream_error = stream_error
        self.stream_hang = stream_hang
        self.chat_text = chat_text
        self.chat_error = chat_error
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return ChatResponse(text=self.chat_text, finish_reason="stop")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ):
        self.stream_calls += 1
        if self.stream_error is not None:
            raise self.stream_error
        if self.stream_hang:
            await asyncio.sleep(3600)
            return
        for chunk in self.stream_chunks:
            yield chunk


@pytest.mark.asyncio
async def test_chat_text_prefers_stream():
    provider = _ScriptedProvider(
        stream_chunks=[StreamChunk(delta_text='{"a": '), StreamChunk(delta_text="1}", done=True)],
        chat_text="should-not-be-used",
    )
    text = await chat_text(provider, [Message.user("hi")])
    assert text == '{"a": 1}'
    assert provider.stream_calls == 1
    assert provider.chat_calls == 0


@pytest.mark.asyncio
async def test_chat_text_falls_back_to_chat_on_stream_error():
    provider = _ScriptedProvider(
        stream_error=RuntimeError("stream 通道不可用"),
        chat_text='{"ok": true}',
    )
    text = await chat_text(provider, [Message.user("hi")])
    assert text == '{"ok": true}'
    assert provider.stream_calls == 1
    assert provider.chat_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (429, "rate limit exceeded"),
        (503, "当前模型端点并发已满"),
    ],
)
async def test_chat_text_does_not_retry_capacity_errors_non_streaming(
    status_code: int,
    message: str,
):
    class CapacityError(RuntimeError):
        def __init__(self) -> None:
            super().__init__(message)
            self.status_code = status_code

    provider = _ScriptedProvider(
        stream_error=CapacityError(),
        chat_text="should-not-be-used",
    )
    with pytest.raises(CapacityError):
        await chat_text(provider, [Message.user("hi")])
    assert provider.stream_calls == 1
    assert provider.chat_calls == 0


@pytest.mark.asyncio
async def test_chat_text_detects_wrapped_503_message_without_status_attribute():
    provider = _ScriptedProvider(
        stream_error=RuntimeError("LLM 流式调用失败: Error code: 503 - 端点并发已满"),
        chat_text="should-not-be-used",
    )
    with pytest.raises(RuntimeError, match="503"):
        await chat_text(provider, [Message.user("hi")])
    assert provider.chat_calls == 0


@pytest.mark.asyncio
async def test_wiki_chunk_analysis_does_not_retry_503(tmp_path: Path):
    provider = _ScriptedProvider(
        stream_error=RuntimeError("Error code: 503 - 当前模型端点并发已满"),
    )
    compiler = WikiCompiler(
        store=FileSystemWikiStore(base_dir=tmp_path),
        provider=provider,
    )

    result = await compiler._analyze_chunk("已经保存的 parsed markdown")

    assert result["_chunk_failed"] is True
    assert provider.stream_calls == 1
    assert provider.chat_calls == 0


@pytest.mark.asyncio
async def test_chat_text_stream_timeout_raises_without_fallback():
    """流式传输中超时应直接抛出（不回退非流式，避免在同一网关上二次空等）。"""
    provider = _ScriptedProvider(stream_hang=True, chat_text="should-not-be-used")
    with pytest.raises(asyncio.TimeoutError):
        await chat_text(provider, [Message.user("hi")], timeout=0.05)
    assert provider.chat_calls == 0


@pytest.mark.asyncio
async def test_ingest_succeeds_via_stream_when_chat_broken(tmp_path: Path):
    """非流式 chat 被网关挂起/报错时，ingest 应能通过流式调用完成分析。"""
    analysis = {
        "entities": [{"name": "AgentRuntime", "description": "Agent 运行时。"}],
        "topics": [],
        "relationships": [],
    }
    payload = json.dumps(analysis, ensure_ascii=False)
    provider = _ScriptedProvider(
        stream_chunks=[StreamChunk(delta_text=payload, done=True)],
        chat_error=TimeoutError("非流式被网关挂起"),
    )
    store = FileSystemWikiStore(base_dir=tmp_path)
    compiler = WikiCompiler(store=store, provider=provider)

    store.save_raw(
        RawSource(
            id="s1",
            title="doc.md",
            source_type="upload",
            parsed_path=str(tmp_path / "s1.parsed.md"),
            created_at=0.0,
        )
    )
    (tmp_path / "s1.parsed.md").write_text("关于 AgentRuntime 的设计文档。", encoding="utf-8")

    result = await compiler.ingest("s1")
    assert not result.issues
    assert provider.stream_calls >= 1
    assert provider.chat_calls == 0
    titles = [p.title for p in result.pages]
    assert "AgentRuntime" in titles
