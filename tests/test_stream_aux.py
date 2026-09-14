"""stream_aux 统一辅助 LLM 入口的契约测试（全 mock，不打真实 API）。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from crew.core.errors import ProviderError
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, Message, StreamChunk
from crew.providers import stream_aux


class _FlakyStreamProvider(FakeProvider):
    """前 N 次流式以瞬时 ProviderError 失败，之后按脚本/回声返回。"""

    def __init__(self, script: list[ChatResponse] | None = None, *, fail_times: int = 0) -> None:
        super().__init__(script)
        self.fail_times = fail_times
        self.stream_calls_made = 0

    async def stream_chat(self, messages, tools=None, **kwargs):
        self.stream_calls_made += 1
        if self.stream_calls_made <= self.fail_times:
            raise ProviderError("gateway hiccup", retryable=True)
        async for chunk in super().stream_chat(messages, tools, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_stream_aux_aggregates_stream_into_result() -> None:
    provider = FakeProvider(script=[ChatResponse(text="你好，世界", finish_reason="stop")])
    result = await stream_aux(
        provider,
        [Message.system("s"), Message.user("u")],
        purpose="session-title",
        timeout=5.0,
    )
    assert result.text == "你好，世界"
    assert result.finish_reason == "stop"
    assert len(provider.stream_calls) == 1


@pytest.mark.asyncio
async def test_stream_aux_forwards_max_tokens_and_purpose_to_provider() -> None:
    provider = FakeProvider()

    class _Recorder(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen: dict | None = None

        async def stream_chat(
            self,
            messages,
            tools=None,
            *,
            max_tokens=None,
            response_format=None,
            reasoning_mode=None,
        ):
            self.seen = {
                "tools": tools,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "reasoning_mode": reasoning_mode,
            }
            async for chunk in super().stream_chat(messages, tools):
                yield chunk

    recorder = _Recorder()
    await stream_aux(
        recorder,
        [Message.user("hi")],
        purpose="wiki-completion",
        timeout=5.0,
        max_tokens=64,
        retry=0,
    )
    assert recorder.seen["max_tokens"] == 64
    assert recorder.seen["tools"] is None


@pytest.mark.asyncio
async def test_stream_aux_retries_transient_provider_error() -> None:
    provider = _FlakyStreamProvider(fail_times=1)
    result = await stream_aux(
        provider,
        [Message.user("hello")],
        purpose="skill-metadata",
        timeout=5.0,
        retry=1,
    )
    assert result.text == "[fake] 收到: hello"
    assert provider.stream_calls_made == 2


@pytest.mark.asyncio
async def test_stream_aux_does_not_retry_non_retryable_error() -> None:
    class _AuthFail(FakeProvider):
        calls = 0

        async def stream_chat(self, messages, tools=None, **kwargs):
            type(self).calls += 1
            raise ProviderError("invalid key", retryable=False)
            yield  # 让本函数保持 async generator 形态

    provider = _AuthFail()
    with pytest.raises(ProviderError):
        await stream_aux(provider, [Message.user("x")], purpose="skill-metadata", timeout=5.0, retry=2)
    assert _AuthFail.calls == 1


@pytest.mark.asyncio
async def test_stream_aux_timeout_raises_and_cancels() -> None:
    class _Slow(FakeProvider):
        async def stream_chat(self, messages, tools=None, **kwargs):
            await asyncio.sleep(10)
            yield StreamChunk(delta_text="late", done=True)

    provider = _Slow()
    with pytest.raises(TimeoutError):
        await stream_aux(provider, [Message.user("x")], purpose="session-title", timeout=0.2, retry=0)


@pytest.mark.asyncio
async def test_stream_aux_emits_metering_log_with_purpose(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider(script=[ChatResponse(text="t")])
    with caplog.at_level(logging.INFO, logger="crew.providers.aux"):
        await stream_aux(provider, [Message.user("x")], purpose="team-turn-decision", timeout=5.0, retry=0)
    records = [r for r in caplog.records if r.name == "crew.providers.aux"]
    assert len(records) == 1
    assert "purpose=team-turn-decision" in records[0].getMessage()


@pytest.mark.asyncio
async def test_stream_aux_respects_zero_retry() -> None:
    provider = _FlakyStreamProvider(fail_times=1)
    with pytest.raises(ProviderError):
        await stream_aux(provider, [Message.user("x")], purpose="team-planning", timeout=5.0, retry=0)
    assert provider.stream_calls_made == 1


@pytest.mark.asyncio
async def test_stream_aux_drops_unsupported_kwargs_for_narrow_signature() -> None:
    """窄签名 provider（不认 response_format 等 kwarg）按名剔除后原样重试。"""

    class _Narrow(FakeProvider):
        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            async for chunk in super().stream_chat(messages, tools):
                yield chunk

    provider = _Narrow()
    result = await stream_aux(
        provider,
        [Message.user("hi")],
        purpose="wiki-completion",
        timeout=5.0,
        response_format={"type": "json_object"},
        retry=0,
    )
    assert result.text == "[fake] 收到: hi"
