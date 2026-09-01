"""Agent loop 鲁棒性/可控性组件单测（crew/agent/loop 子包 + builtin 主循环编排）。

覆盖：迭代预算、工具防循环 guardrails、TurnControl(steer/interrupt)、
resilience(空响应重试/截断续写/溢出兜底压缩/provider 故障转移)、并行工具执行。
全部用 FakeProvider/自定义假 provider 驱动，无需真实 LLM。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest

from crew.agent.compact import estimate_prompt_tokens
from crew.agent.executor import BuiltinExecutor, ExecutionContext, FinalRequestView
from crew.agent.loop import (
    IterationBudget,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    TurnControl,
    is_context_overflow,
    is_empty_response,
    provider_chain,
    should_continue,
    should_parallelize,
)
from crew.agent.loop.tool_dispatch_helpers import (
    is_tool_parallel_safe,
    plan_tool_calls,
    segment_consecutive_safe,
    should_parallelize_tool_batch,
)
from crew.agent.loop.tool_runner import ToolRunner
from crew.core.errors import ProviderError
from crew.core.interfaces import LLMProvider
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, MediaPart, Message, StreamChunk, ToolCall, ToolOutput
from crew.plugins.manager import (
    LLM_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    PluginManager,
    RequestMiddlewareResult,
)
from crew.tools.registry import Registry, tool_error, tool_result
from crew.tools.tool_search import TOOL_SEARCH_NAME, ToolSearchConfig
from crew.state.config import Config


# --------------------------------------------------------------------------- #
# 辅助：可编排的假 provider
# --------------------------------------------------------------------------- #
class ScriptStreamProvider(FakeProvider):
    """按脚本逐次返回 ChatResponse（FakeProvider.stream_chat 复用 chat 脚本）。"""


class RaiseThenScript(LLMProvider):
    """前 fail_times 次 stream_chat 抛指定异常，之后按 script 返回。"""

    def __init__(self, exc: Exception, fail_times: int, script: list[ChatResponse]):
        self._exc = exc
        self._fail = fail_times
        self._script = list(script)
        self.stream_calls = 0

    async def chat(self, messages, tools=None):  # pragma: no cover - 未走到
        return ChatResponse(text="x")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        if self._fail > 0:
            self._fail -= 1
            raise self._exc
        resp = self._script.pop(0) if self._script else ChatResponse(text="done", finish_reason="stop")
        if resp.text:
            yield StreamChunk(delta_text=resp.text)
        yield StreamChunk(delta_text="", done=True, tool_calls=resp.tool_calls, finish_reason=resp.finish_reason)


def _ctx(messages=None, **kw) -> ExecutionContext:
    return ExecutionContext(
        session_id="s", request_id="r", system_prompt="sys",
        messages=messages if messages is not None else [Message.user("hi")],
        query="hi", **kw,
    )


def _executor(provider, registry=None, **kw) -> BuiltinExecutor:
    return BuiltinExecutor(
        provider, registry or Registry(), PluginManager(),
        max_iterations=kw.pop("max_iterations", 6),
        backoff_seconds=0, **kw,
    )


async def _collect(executor, ctx):
    chunks = []
    async for ch in executor.execute(ctx):
        chunks.append(ch)
    return chunks


class RecordingProvider(LLMProvider):
    def __init__(self) -> None:
        self.seen_messages = None
        self.seen_tools = None

    async def chat(self, messages, tools=None):  # pragma: no cover - 未走到
        return ChatResponse(text="unused")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self.seen_messages = messages
        self.seen_tools = tools
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class PausingStreamProvider(LLMProvider):
    """首轮流式吐出一段后等待，便于测试模型调用期间到达的 steer。"""

    def __init__(self) -> None:
        self.after_first_delta = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None):  # pragma: no cover - 未走到
        return ChatResponse(text="unused")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            yield StreamChunk(delta_text="原方向")
            self.after_first_delta.set()
            await self.release_first.wait()
            yield StreamChunk(delta_text="", done=True, finish_reason="stop")
            return
        yield StreamChunk(delta_text="已调整")
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


# --------------------------------------------------------------------------- #
# 1. 迭代预算
# --------------------------------------------------------------------------- #
async def test_llm_request_middleware_partial_request_keeps_tools():
    provider = RecordingProvider()
    plugins = PluginManager()

    def only_rewrite_messages(request, **kwargs):
        return {"request": {"messages": [*request["messages"], Message.user("middleware")]}}

    plugins._middleware.setdefault(LLM_REQUEST_MIDDLEWARE, []).append(only_rewrite_messages)
    executor = BuiltinExecutor(provider, Registry(), plugins, backoff_seconds=0)
    tools = [{"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}]
    result = {}
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    events = [
        ev
        async for ev in executor._call_model(
            FinalRequestView.create([Message.user("hi")], tools),
            "r",
            next_seq,
            result,
            session_id="s",
        )
    ]

    assert events == []
    assert provider.seen_tools == tools
    assert provider.seen_messages[-1].content == "middleware"


async def test_llm_execution_middleware_stream_error_does_not_become_provider_failure():
    plugins = PluginManager()

    async def broken_restore(request, next_call, **kwargs):
        stream = await next_call(request)
        async for chunk in stream:
            yield chunk
            raise RuntimeError("restore failed")

    plugins._middleware.setdefault(LLM_EXECUTION_MIDDLEWARE, []).append(broken_restore)
    executor = BuiltinExecutor(
        ScriptStreamProvider([ChatResponse(text="x", finish_reason="stop")]),
        Registry(),
        plugins,
        backoff_seconds=0,
    )
    result = {}
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    events = [
        ev
        async for ev in executor._call_model(
            FinalRequestView.create([Message.user("hi")]),
            "r",
            next_seq,
            result,
            session_id="s",
        )
    ]

    assert not result.get("error")
    assert result["text"] == "x"
    assert [ev.kind for ev in events] == ["delta"]


async def test_request_view_count_matches_execution_middleware_payload_sent_to_provider():
    provider = RecordingProvider()
    plugins = PluginManager()

    async def append_at_execution(request, next_call, **kwargs):
        changed = {
            **request,
            "messages": [*request["messages"], Message.user("execution middleware")],
        }
        return await next_call(changed)

    plugins._middleware.setdefault(LLM_EXECUTION_MIDDLEWARE, []).append(append_at_execution)
    executor = BuiltinExecutor(provider, Registry(), plugins, backoff_seconds=0)
    result = {}
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    events = [
        event
        async for event in executor._call_model(
            FinalRequestView.create([Message.user("hi")]),
            "r",
            next_seq,
            result,
            session_id="s",
        )
    ]

    assert events == []
    assert provider.seen_messages[-1].content == "execution middleware"
    assert result["usage"]["request_view_tokens"] == estimate_prompt_tokens(
        provider.seen_messages,
        provider.seen_tools,
    )


def test_iteration_budget_consume_refund():
    b = IterationBudget(2)
    assert b.consume() and b.consume()
    assert not b.consume()          # 用尽
    b.refund()
    assert b.remaining == 1 and b.consume()


# --------------------------------------------------------------------------- #
# 2. 工具防循环 guardrails（直接驱动 controller）
# --------------------------------------------------------------------------- #
def test_guardrail_blocks_repeated_exact_failure():
    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2)
    g = ToolCallGuardrailController(cfg)
    args = {"x": 1}
    # 两次相同失败累计
    assert g.before_call("foo", args).allows_execution
    g.after_call("foo", args, '{"error":"boom"}')
    assert g.before_call("foo", args).allows_execution
    g.after_call("foo", args, '{"error":"boom"}')
    # 第三次 before_call 应被拦截
    decision = g.before_call("foo", args)
    assert decision.should_halt and decision.code == "repeated_exact_failure_block"


def test_guardrail_blocks_idempotent_no_progress():
    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    g = ToolCallGuardrailController(cfg)
    args = {"path": "a.txt"}
    same = "相同结果"
    # file_read 在只读名单：连续返回相同结果累计无进展
    g.after_call("file_read", args, same)
    g.after_call("file_read", args, same)
    decision = g.before_call("file_read", args)
    assert decision.should_halt and decision.code == "idempotent_no_progress_block"


def test_guardrail_classifies_browser_use_by_action():
    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    guard = ToolCallGuardrailController(cfg)
    snapshot_args = {"action": "snapshot"}
    guard.after_call("browser_use", snapshot_args, "same page")
    guard.after_call("browser_use", snapshot_args, "same page")
    assert guard.before_call("browser_use", snapshot_args).code == "idempotent_no_progress_block"

    type_args = {"action": "type", "ref": "p1:e1", "text": "q"}
    guard.after_call("browser_use", type_args, "same page")
    guard.after_call("browser_use", type_args, "same page")
    assert guard.before_call("browser_use", type_args).allows_execution


def test_guardrail_treats_wiki_readonly_tools_as_idempotent():
    """Wiki 只读工具应被识别为幂等，连续相同结果会触发无进展拦截。"""
    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    g = ToolCallGuardrailController(cfg)
    same = '{"sources":[]}'
    g.after_call("wiki_list_sources", {}, same)
    g.after_call("wiki_list_sources", {}, same)
    decision = g.before_call("wiki_list_sources", {})
    assert decision.should_halt and decision.code == "idempotent_no_progress_block"


async def test_guardrail_halt_stops_loop():
    """循环里同参工具反复失败 → 命中 guardrail 硬停，给用户中文消息 + 给模型写 guidance。"""
    reg = Registry()
    reg.register(name="failing", toolset="x", schema={"name": "failing", "parameters": {}},
                 handler=lambda a: tool_error("总是失败"), is_async=False)

    class AlwaysToolProvider(FakeProvider):
        async def chat(self, messages, tools=None):
            self.calls.append(list(messages))
            return ChatResponse(tool_calls=[ToolCall("c", "failing", {"x": 1})])

    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2)
    ex = _executor(AlwaysToolProvider(), reg, guardrail_config=cfg)
    ctx = _ctx()
    chunks = await _collect(ex, ctx)
    final = chunks[-1]
    # 用户看到中文状态消息（替代旧版"防循环保护"文案）
    assert final.kind == "final" and "已自动停止" in final.body["text"]
    # 模型在对话历史里看到英文 guidance（采用 controlled_halt_response）
    assert any("I stopped retrying" in (m.content or "") for m in ctx.messages)


async def test_guardrail_warn_only_when_hard_stop_disabled():
    """hard_stop 默认关（采用）：反复失败只 warn 贴 guidance，不 halt，loop 跑到 max_iter。"""
    reg = Registry()
    reg.register(name="failing", toolset="x", schema={"name": "failing", "parameters": {}},
                 handler=lambda a: tool_error("总是失败"), is_async=False)

    class AlwaysToolProvider(FakeProvider):
        async def chat(self, messages, tools=None):
            self.calls.append(list(messages))
            return ChatResponse(tool_calls=[ToolCall("c", "failing", {"x": 1})])

    cfg = ToolCallGuardrailConfig(hard_stop_enabled=False)  # 默认 warn 阈值
    ex = _executor(AlwaysToolProvider(), reg, guardrail_config=cfg, max_iterations=6)
    ctx = _ctx()
    chunks = await _collect(ex, ctx)
    final = chunks[-1]
    # 跑到 max_iter，不触发 halt
    assert final.kind == "final" and "最大迭代次数" in final.body["text"]
    assert "已自动停止" not in final.body["text"]
    # warn 把 guidance 后缀贴进了 tool result 历史，模型能自行调整
    assert any("Tool loop warning" in (m.content or "") for m in ctx.messages)


def test_config_guardrail_hard_stop_default_false():
    """采用：Config 默认 hard_stop=False，日常靠 warn 让模型自行调整。"""
    cfg = Config()
    assert cfg.guardrail_hard_stop is False
    assert cfg.guardrail_enabled is True  # warn 总开关仍开


# --------------------------------------------------------------------------- #
# 3. TurnControl：steer + interrupt
# --------------------------------------------------------------------------- #
def test_turncontrol_steer_drain_and_interrupt():
    c = TurnControl()
    assert c.steer("加上A")
    assert c.steer("再加B")
    assert c.drain_steer() == "加上A\n再加B"
    assert c.drain_steer() is None
    assert not c.interrupted
    c.interrupt("停")
    assert c.interrupted and c.interrupt_message == "停"
    # 中断后 steer 作废
    c.interrupt()


async def test_loop_injects_steer_into_messages():
    control = TurnControl()
    control.steer("务必用中文")
    ex = _executor(FakeProvider())  # 回声模式，单轮即 final
    ctx = _ctx(control=control)
    await _collect(ex, ctx)
    assert any("用户补充指令" in (m.content or "") and "务必用中文" in (m.content or "")
               for m in ctx.messages)


async def test_late_steer_during_final_model_call_continues_before_final():
    """模型已开始流式输出后收到 steer：先注入补充指令，再续一轮模型调用。"""
    control = TurnControl()
    provider = PausingStreamProvider()
    ex = _executor(provider, max_iterations=3)
    ctx = _ctx(control=control)
    chunks: list[StreamChunk] = []

    async def run() -> None:
        async for chunk in ex.execute(ctx):
            chunks.append(chunk)

    task = asyncio.create_task(run())
    await asyncio.wait_for(provider.after_first_delta.wait(), timeout=1.0)
    assert control.steer("改成更谨慎的方向")
    provider.release_first.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(provider.calls) == 2
    second_call_text = "\n".join(m.content or "" for m in provider.calls[1])
    assert "用户补充指令：改成更谨慎的方向" in second_call_text
    assert [chunk.body["text"] for chunk in chunks if chunk.kind == "delta"] == ["原方向", "已调整"]
    finals = [chunk for chunk in chunks if chunk.kind == "final"]
    assert len(finals) == 1
    assert finals[0].body["text"] == "已调整"


async def test_loop_interrupt_before_run_stops_without_calling_model():
    """轮初已中断：直接空 final 结束，模型不被调用（前端据空 final 保留已有内容）。"""
    control = TurnControl()
    control.interrupt()
    provider = FakeProvider()
    ex = _executor(provider)
    chunks = await _collect(ex, _ctx(control=control))
    assert chunks[-1].kind == "final" and chunks[-1].body["text"] == ""
    assert provider.stream_calls == []


async def test_loop_interrupt_midstream_keeps_partial():
    """流式中途中断（用户点停止）：保留已吐的半截文本，作为 final 回灌。"""
    control = TurnControl()

    class InterruptingProvider(FakeProvider):
        async def stream_chat(self, messages, tools=None):
            self.stream_calls.append(list(messages))
            yield StreamChunk(delta_text="前半")
            control.interrupt()  # 模拟用户在流中途点了停止
            yield StreamChunk(delta_text="后半")
            yield StreamChunk(delta_text="", done=True, finish_reason="stop")

    ex = _executor(InterruptingProvider())
    chunks = await _collect(ex, _ctx(control=control))
    final = chunks[-1]
    assert final.kind == "final" and final.body["text"] == "前半后半"
    deltas = "".join(c.body["text"] for c in chunks if c.kind == "delta")
    assert deltas == "前半后半"


# --------------------------------------------------------------------------- #
# 4. resilience：纯判定
# --------------------------------------------------------------------------- #
def test_resilience_pure_helpers():
    assert is_empty_response("", [], "")
    assert not is_empty_response("hi", [], "")
    assert not is_empty_response("", [ToolCall("c", "t", {})], "")
    assert should_continue("length", [])
    assert not should_continue("stop", [])
    assert not should_continue("length", [ToolCall("c", "t", {})])
    assert is_context_overflow(ProviderError("maximum context length exceeded"))
    assert not is_context_overflow(ProviderError("rate limited"))
    p, f1, f2 = object(), object(), object()
    assert provider_chain(p, [f1, f2, f1]) == [p, f1, f2]  # 去重保序


async def test_loop_empty_response_then_retry():
    provider = ScriptStreamProvider(script=[
        ChatResponse(text="", finish_reason="stop"),     # 空响应
        ChatResponse(text="真正的回答", finish_reason="stop"),
    ])
    ex = _executor(provider)
    chunks = await _collect(ex, _ctx())
    final = chunks[-1]
    assert final.kind == "final" and final.body["text"] == "真正的回答"


async def test_loop_continuation_on_length_truncation():
    provider = ScriptStreamProvider(script=[
        ChatResponse(text="前半段", finish_reason="length"),  # 被截断
        ChatResponse(text="后半段", finish_reason="stop"),
    ])
    ex = _executor(provider)
    chunks = await _collect(ex, _ctx())
    assert chunks[-1].kind == "final" and chunks[-1].body["text"] == "后半段"


async def test_loop_overflow_triggers_force_compact_then_succeeds():
    class SpyCompactor:
        def __init__(self):
            self.calls = 0

        async def force_compact(self, messages, session_id=None):
            self.calls += 1
            return messages

        async def compact_view(
            self,
            messages,
            session_id=None,
            owner_account_id=None,
            prompt_overhead_tokens=0,
        ):
            # 不压缩视图，让首轮直接命中 provider 触发 overflow 兜底
            return messages

    spy = SpyCompactor()
    provider = RaiseThenScript(
        ProviderError("maximum context length exceeded", retryable=False),
        fail_times=1,
        script=[ChatResponse(text="压缩后成功", finish_reason="stop")],
    )
    ex = _executor(provider, compactor=spy)
    chunks = await _collect(ex, _ctx())
    final = chunks[-1]
    assert final.kind == "final" and final.body["text"] == "压缩后成功"
    assert spy.calls >= 1  # 命中溢出后调用了兜底压缩
    # 溢出重试不应向用户吐 error 帧
    assert not any(c.kind == "error" for c in chunks)


async def test_loop_failover_to_fallback_provider():
    class AlwaysFail(LLMProvider):
        async def chat(self, messages, tools=None):  # pragma: no cover
            return ChatResponse()

        async def stream_chat(self, messages, tools=None):
            raise ProviderError("primary down", retryable=False)
            yield  # pragma: no cover

    fallback = FakeProvider()
    fallback.model = "fallback-model"
    ex = _executor(AlwaysFail(), fallback_providers=[fallback])
    ctx = _ctx([Message.user("回声内容")])
    chunks = await _collect(ex, ctx)
    final = chunks[-1]
    assert final.kind == "final" and "回声内容" in final.body["text"]
    assert ctx.messages[-1].model == "fallback-model"


async def test_loop_recovers_from_unsupported_image_input_in_text_mode():
    class RejectImageOnce(LLMProvider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        async def chat(self, messages, tools=None):  # pragma: no cover
            return ChatResponse()

        async def stream_chat(self, messages, tools=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                raise ProviderError(
                    "Model do not support image input",
                    category="unsupported_capability",
                    capability="vision",
                )
            yield StreamChunk(delta_text="已改用网页文本信息继续")
            yield StreamChunk(delta_text="", done=True, finish_reason="stop")

    provider = RejectImageOnce()
    image = Message(
        role="user",
        content="查看网页",
        content_parts=[
            {"type": "text", "text": "查看网页"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    )
    chunks = await _collect(_executor(provider), _ctx([image]))

    assert len(provider.calls) == 2
    assert provider.calls[0][1].content_parts is not None
    second_parts = [part for message in provider.calls[1] for part in (message.content_parts or [])]
    assert not any(part.get("type") == "image_url" for part in second_parts)
    second_text = "\n".join(message.text_content for message in provider.calls[1])
    assert "不要声称已经看过图片" in second_text
    assert any(chunk.kind == "status" and "非视觉方式" in chunk.body["message"] for chunk in chunks)
    assert not any(chunk.kind == "error" for chunk in chunks)
    assert chunks[-1].body["text"] == "已改用网页文本信息继续"


async def test_loop_does_not_repeat_unsupported_capability_recovery_forever():
    provider = RaiseThenScript(
        ProviderError(
            "Model do not support image input",
            category="unsupported_capability",
            capability="vision",
        ),
        fail_times=2,
        script=[],
    )
    image = Message(
        role="user",
        content="查看图片",
        content_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    )
    chunks = await _collect(_executor(provider), _ctx([image]))

    assert provider.stream_calls == 2
    assert sum(chunk.kind == "status" for chunk in chunks) == 1
    assert chunks[-1].kind == "error"


# --------------------------------------------------------------------------- #
# 5. 并行工具执行
# --------------------------------------------------------------------------- #
def test_should_parallelize_only_readonly_batches():
    ro = [
        ToolCall("1", "file_read", {"path": "/tmp/a.txt"}),
        ToolCall("2", "file_read", {"path": "/tmp/b.txt"}),
    ]
    readonly_terminal = [
        ToolCall("1", "terminal", {"command": "rg foo /tmp/project"}),
        ToolCall("2", "terminal", {"command": "git status --short"}),
    ]
    overlap = [
        ToolCall("1", "file_read", {"path": "/tmp/a"}),
        ToolCall("2", "file_read", {"path": "/tmp/a/b.txt"}),
    ]
    mixed = [
        ToolCall("1", "file_read", {"path": "/tmp/a.txt"}),
        ToolCall("2", "file_write", {"path": "/tmp/b.txt"}),
    ]
    single = [ToolCall("1", "file_read", {})]
    assert should_parallelize(ro)
    assert should_parallelize(readonly_terminal)
    assert not should_parallelize(overlap)
    assert not should_parallelize(mixed)
    assert not should_parallelize(single)  # 单个不并行


def test_delegate_batch_parallel_only_for_distinct_members():
    distinct = [
        ToolCall("1", "delegate_to_teammate", {"member": "coder", "instruction": "A"}),
        ToolCall("2", "delegate_to_teammate", {"member": "researcher", "instruction": "B"}),
    ]
    duplicate_member = [
        ToolCall("1", "delegate_to_teammate", {"member": "coder", "instruction": "A"}),
        ToolCall("2", "delegate_to_teammate", {"member": "coder", "instruction": "B"}),
    ]
    mixed = [
        ToolCall("1", "delegate_to_teammate", {"member": "coder", "instruction": "A"}),
        ToolCall("2", "file_read", {"path": "/tmp/a.txt"}),
    ]

    assert should_parallelize_tool_batch(distinct).parallel
    assert not should_parallelize_tool_batch(duplicate_member).parallel
    assert not should_parallelize_tool_batch(mixed).parallel


def test_plan_tool_calls_deduplicates_and_caps_delegates():
    calls = [
        ToolCall("r1", "file_read", {"path": "/tmp/a.txt"}),
        ToolCall("r2", "file_read", {"path": "/tmp/a.txt"}),
        ToolCall("d1", "delegate_to_teammate", {"member": "coder", "instruction": "A"}),
        ToolCall("d2", "delegate_to_teammate", {"member": "researcher", "instruction": "B"}),
        ToolCall("d3", "delegate_to_teammate", {"member": "qa", "instruction": "C"}),
    ]

    planned = plan_tool_calls(calls, max_delegate_calls=2)
    assert [tc.id for tc in planned] == ["r1", "d1", "d2"]


async def test_parallel_tools_preserve_order():
    reg = Registry()
    reg.register(name="file_read", toolset="file",
                 schema={"name": "file_read", "parameters": {}},
                 handler=lambda a: tool_result(echo=a.get("i")), is_async=False, override=True)

    calls = [
        ToolCall("a", "file_read", {"i": 1, "path": "/tmp/order-a.txt"}),
        ToolCall("b", "file_read", {"i": 2, "path": "/tmp/order-b.txt"}),
    ]

    class TwoReadThenDone(FakeProvider):
        def __init__(self):
            super().__init__()
            self._n = 0

        async def chat(self, messages, tools=None):
            self.calls.append(list(messages))
            self._n += 1
            if self._n == 1:
                return ChatResponse(tool_calls=calls)
            return ChatResponse(text="完成", finish_reason="stop")

    ex = _executor(TwoReadThenDone(), reg)
    ctx = _ctx()
    await _collect(ex, ctx)
    # 两条 tool 结果按原始 tool_call 顺序回灌
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]


async def test_parallel_tool_execution_respects_worker_cap():
    reg = Registry()
    active = 0
    peak = 0

    async def read_handler(args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return tool_result(path=args.get("path"))
        finally:
            active -= 1

    reg.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "parameters": {}},
        handler=read_handler,
        is_async=True,
    )
    calls = [
        ToolCall(f"r{i}", "file_read", {"path": f"/tmp/{i}.txt"})
        for i in range(4)
    ]

    class FourReadsThenDone(FakeProvider):
        def __init__(self):
            super().__init__()
            self._n = 0

        async def chat(self, messages, tools=None):
            self._n += 1
            if self._n == 1:
                return ChatResponse(tool_calls=calls)
            return ChatResponse(text="完成", finish_reason="stop")

    ex = _executor(FourReadsThenDone(), reg, max_parallel_tool_calls=2)
    ctx = _ctx()
    await _collect(ex, ctx)
    assert peak == 2
    durations = [tc.duration for tc in calls if tc.duration is not None]
    assert len(durations) == 4
    assert max(durations) < 0.5  # 各工具独立计时，不应接近整回合加总


async def test_tool_calls_record_individual_durations_not_turn_total():
    """每个 ToolCall.duration 应是自身执行耗时，而不是整回合总时长。"""
    reg = Registry()

    async def slow_handler(args):
        await asyncio.sleep(0.06)
        return tool_result(tag="slow")

    async def fast_handler(args):
        await asyncio.sleep(0.01)
        return tool_result(tag="fast")

    reg.register(name="slow_tool", toolset="t", schema={"name": "slow_tool", "parameters": {}},
                 handler=slow_handler, is_async=True, override=True)
    reg.register(name="fast_tool", toolset="t", schema={"name": "fast_tool", "parameters": {}},
                 handler=fast_handler, is_async=True, override=True)

    calls = [
        ToolCall("slow", "slow_tool", {}),
        ToolCall("fast", "fast_tool", {}),
    ]

    class ToolsThenDone(FakeProvider):
        def __init__(self):
            super().__init__()
            self._n = 0

        async def chat(self, messages, tools=None):
            self._n += 1
            if self._n == 1:
                return ChatResponse(tool_calls=calls)
            return ChatResponse(text="完成", finish_reason="stop")

    ctx = _ctx()
    await _collect(_executor(ToolsThenDone(), reg), ctx)

    slow_tc, fast_tc = calls
    assert slow_tc.duration is not None and fast_tc.duration is not None
    assert slow_tc.duration > fast_tc.duration
    assert slow_tc.duration < 0.25
    assert fast_tc.duration < 0.08
    # 若误用整回合耗时，两者会几乎相等且明显大于 fast 的真实耗时
    assert slow_tc.duration - fast_tc.duration > 0.03


# --------------------------------------------------------------------------- #
# 6. 流式中断续写（Crew partial stream stub + continuation）
# --------------------------------------------------------------------------- #
class StreamInterruptProvider(LLMProvider):
    """流式中途失败的 provider：前 fail_times 次发一段文本后抛 retryable 异常，
    之后按 texts 顺序返回。用于验证续写机制。"""

    def __init__(self, fail_times: int, texts: list[str]):
        self.fail_times = fail_times
        self.texts = list(texts)
        self.stream_calls: list[list[Message]] = []
        self._call = 0

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append(list(messages))
        self._call += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            yield StreamChunk(delta_text=self.texts.pop(0))
            raise ProviderError("ReadTimeout", retryable=True, category="timeout")
        text = self.texts.pop(0)
        if text:
            yield StreamChunk(delta_text=text)
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


async def test_loop_stream_interrupt_continues():
    """流式中途 ReadTimeout：保留已 emit 文本，注入续写提示后重试，最终文本拼接完整。"""
    provider = StreamInterruptProvider(fail_times=1, texts=["前半段", "后半段"])
    ex = _executor(provider, stream_continuation_max=2)
    chunks = await _collect(ex, _ctx())

    final = chunks[-1]
    assert final.kind == "final"
    assert "前半段" in final.body["text"]
    assert "后半段" in final.body["text"]
    assert not any(c.kind == "error" for c in chunks)

    assert len(provider.stream_calls) == 2
    assert any("中断" in (m.content or "") for m in provider.stream_calls[1])
    status_messages = [c.body["message"] for c in chunks if c.kind == "status"]
    assert any("正在第 1/2 次续写" in msg for msg in status_messages)


async def test_user_interrupt_during_stream_failure_does_not_continue():
    """用户中断后 provider 抛出的流式异常不应触发续写，否则 stop/steer 会卡住当前 turn。"""
    control = TurnControl()

    class UserInterruptedStreamFailure(LLMProvider):
        def __init__(self) -> None:
            self.stream_calls = 0

        async def chat(self, messages, tools=None):  # pragma: no cover
            return ChatResponse(text="")

        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
            self.stream_calls += 1
            yield StreamChunk(delta_text="前半段")
            control.interrupt("用户引导新消息")
            raise ProviderError("ReadTimeout", retryable=True, category="timeout")

    provider = UserInterruptedStreamFailure()
    ex = _executor(provider, stream_continuation_max=2)
    chunks = await _collect(ex, _ctx(control=control))

    final = chunks[-1]
    assert final.kind == "final"
    assert final.body["text"] == "前半段"
    assert provider.stream_calls == 1
    assert not any("续写" in (m.content or "") for m in chunks if m.kind == "status")


async def test_loop_stream_interrupt_max_reached():
    """流式中断续写达上限：保留已生成文本，友好收尾。"""
    provider = StreamInterruptProvider(fail_times=3, texts=["部分"] * 3)
    ex = _executor(provider, stream_continuation_max=1)
    chunks = await _collect(ex, _ctx())

    final = chunks[-1]
    assert final.kind == "final"
    assert "部分" in final.body["text"]
    assert "多次中断" in final.body["text"]
    assert not any(c.kind == "error" for c in chunks)
    status_messages = [c.body["message"] for c in chunks if c.kind == "status"]
    assert any("正在第 1/1 次续写" in msg for msg in status_messages)


async def test_loop_stream_interrupt_non_retryable_yields_error():
    """流式中途 auth 错误：不可续写，yield 友好 error 帧。"""

    class AuthFailMidStream(LLMProvider):
        async def chat(self, messages, tools=None):  # pragma: no cover
            return ChatResponse(text="")

        async def stream_chat(self, messages, tools=None):
            yield StreamChunk(delta_text="部分")
            raise ProviderError("Unauthorized", retryable=False, category="auth")

    ex = _executor(AuthFailMidStream())
    chunks = await _collect(ex, _ctx())

    error_chunks = [c for c in chunks if c.kind == "error"]
    assert len(error_chunks) == 1
    assert "已保留" in error_chunks[0].body["message"]


# --------------------------------------------------------------------------- #
# 7. 流式工具派发（StreamingToolExecutor 思路）
# --------------------------------------------------------------------------- #
def _runner(reg, **kw) -> ToolRunner:
    plugins = kw.pop("plugins", PluginManager())
    return ToolRunner(
        reg, plugins,
        ToolCallGuardrailController(ToolCallGuardrailConfig()),
        session_id="s", **kw,
    )


def _seq_counter():
    n = 0

    def nxt() -> int:
        nonlocal n
        n += 1
        return n

    return nxt


async def test_file_delete_large_file_runs_full_loop_without_unbounded_read(tmp_path, monkeypatch):
    """file_delete 删除 >128KiB 文件走完整 run_batch 链路：不无界读、删除成功、变更降级为元数据。

    回归背景：ToolRunner 曾在 file_delete 执行前用 Path.read_text 无界读取目标文件，
    626MB 的 bundle 会阻塞 Gateway 事件循环导致重启。修复后 before/after 均走
    read_file_state 的 128 KiB 上限，超限文件只记录元数据。
    """
    from functools import partial
    from pathlib import Path as PathType

    from crew.agent.file_changes import FILE_CHANGE_MAX_BYTES
    from crew.tools.builtin import handle_file_delete

    big = tmp_path / "big.bundle"
    big.write_bytes(b"\x00" * (FILE_CHANGE_MAX_BYTES + 4096))

    read_text_calls: list[str] = []
    original_read_text = PathType.read_text

    def spy_read_text(self, *args, **kwargs):
        read_text_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(PathType, "read_text", spy_read_text)

    reg = Registry()
    reg.register(
        name="file_delete",
        toolset="file",
        schema={"name": "file_delete", "parameters": {}},
        handler=partial(handle_file_delete, workspace_store=None, security_service=None),
        is_async=True,
        override=True,
    )
    runner = _runner(reg)
    call = ToolCall("d", "file_delete", {"path": str(big)})

    messages: list[Message] = []
    chunks = [c async for c in runner.run_batch([call], messages, "rid", _seq_counter())]

    # 删除本体成功落地。
    assert not big.exists()
    # 变更链路未对目标文件做无界 read_text（修复的核心）。
    assert str(big) not in read_text_calls
    # 广播 file_changes 帧，且降级为元数据（binary、无 diff）。
    change_frames = [c for c in chunks if c.kind == "file_changes"]
    assert change_frames
    files = change_frames[-1].body["files"]
    deleted = next((f for f in files if f.get("path") == str(big)), None)
    assert deleted is not None
    assert deleted["status"] == "deleted"
    assert deleted.get("binary") is True
    assert deleted.get("diff") == []
    # 结果帧无 error，无"服务重启"类错误。
    assert not any(c.kind == "error" for c in chunks)
    assert not any("服务重启" in str(c.body) for c in chunks)


async def test_full_agent_loop_deletes_large_file_without_restart(tmp_path, monkeypatch):
    """完整 Agent 循环端到端：模型选择 file_delete 删除 >128KiB 文件，任务 completed、不无界读。

    从 provider 输出 file_delete 工具调用，经 executor 完整循环（ToolRunner.run_batch
    → _read_file_before → handle_file_delete → _file_change_event）到 final 回答，
    验证大文件删除不阻塞、不无界读、变更降级为元数据。
    """
    from functools import partial
    from pathlib import Path as PathType

    from crew.agent.file_changes import FILE_CHANGE_MAX_BYTES
    from crew.tools.builtin import handle_file_delete

    big = tmp_path / "big.bundle"
    big.write_bytes(b"\x00" * (FILE_CHANGE_MAX_BYTES + 4096))

    read_text_calls: list[str] = []
    original_read_text = PathType.read_text

    def spy_read_text(self, *args, **kwargs):
        read_text_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(PathType, "read_text", spy_read_text)

    reg = Registry()
    reg.register(
        name="file_delete",
        toolset="file",
        schema={"name": "file_delete", "parameters": {}},
        handler=partial(handle_file_delete, workspace_store=None, security_service=None),
        is_async=True,
        override=True,
    )

    provider = ScriptStreamProvider([
        ChatResponse(tool_calls=[ToolCall("d", "file_delete", {"path": str(big)})]),
        ChatResponse(text="已删除", finish_reason="stop"),
    ])
    ctx = _ctx(tool_schemas=reg.list_schemas(), enforce_tool_scope=True)
    chunks = await _collect(_executor(provider, reg), ctx)

    # 删除本体成功落地。
    assert not big.exists()
    # 变更链路未对目标文件做无界 read_text。
    assert str(big) not in read_text_calls
    # 完整循环以 final 收尾，无 error、无"服务重启"。
    assert chunks[-1].kind == "final"
    assert not any(c.kind == "error" for c in chunks)
    assert not any("服务重启" in str(c.body) for c in chunks)
    # file_changes 帧降级为元数据。
    change_frames = [c for c in chunks if c.kind == "file_changes"]
    assert change_frames
    files = change_frames[-1].body["files"]
    deleted = next((f for f in files if f.get("path") == str(big)), None)
    assert deleted is not None
    assert deleted["status"] == "deleted"
    assert deleted.get("binary") is True
    assert deleted.get("diff") == []


async def test_tool_runner_rejects_unauthorized_direct_call_before_plugins_and_prewarm(caplog):
    """即使未授权工具已全局注册且可 prewarm，也不能进入插件或 handler。"""
    handler_calls = 0
    plugin_calls: list[str] = []
    secret = "do-not-log-this-argument"

    async def handler(_args):
        nonlocal handler_calls
        handler_calls += 1
        return tool_result(ok=True)

    async def request_middleware(name, _arguments, **_kwargs):
        plugin_calls.append(name)
        raise AssertionError("未授权工具不应进入插件 middleware")

    registry = Registry()
    registry.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "parameters": {}},
        handler=handler,
        is_async=True,
        override=True,
    )
    plugins = PluginManager()
    plugins.apply_tool_request_middleware = request_middleware
    runner = _runner(
        registry,
        plugins=plugins,
        authorized_tool_names=frozenset(),
    )
    call = ToolCall("blocked", "file_read", {"path": secret})

    assert runner.prewarm(call) is False
    messages: list[Message] = []
    chunks = [chunk async for chunk in runner.run_batch([call], messages, "rid", _seq_counter())]

    assert handler_calls == 0
    assert plugin_calls == []
    assert "TOOL_NOT_AUTHORIZED" in messages[-1].content
    assert secret not in caplog.text
    assert secret not in repr(chunks)


def test_terminal_generated_files_merge_into_existing_file_changes(tmp_path):
    """terminal 间接生成的过程文件和最终二进制结果应进入同一 file_changes 列表。"""
    from crew.agent.plan import PlanModeManager
    from crew.core.types import ToolResult

    manager = PlanModeManager()
    runner = _runner(Registry(), plan_manager=manager)
    before = runner._workspace_snapshot(tmp_path)
    assert before == {}

    source = tmp_path / "slide-01.svg"
    source.write_text("<svg>\n<text>demo</text>\n</svg>\n", encoding="utf-8")
    result_file = tmp_path / "最终结果.pptx"
    result_file.write_bytes(b"PK\x03\x04pptx")

    event = runner._terminal_file_change_event(
        ToolCall("term-1", "terminal", {"command": "node build.js"}),
        (tmp_path, before),
        ToolResult("term-1", "terminal", '{"success": true, "exit_code": 0}'),
        "rid",
        _seq_counter(),
    )

    assert event is not None
    files = event.body["files"]
    assert [item["name"] for item in files] == ["slide-01.svg", "最终结果.pptx"]
    assert files[0]["added"] == 3
    assert files[1]["binary"] is True
    assert files[1]["added"] == 0
    persisted = manager.drain_turn_file_changes("s")
    assert [item["name"] for item in persisted] == ["slide-01.svg", "最终结果.pptx"]
    assert persisted[1]["binary"] is True


async def test_tool_runner_authorizes_each_batch_item_independently():
    """批量调用逐项授权；非法项不连带阻断合法 deferred 工具。"""
    calls: list[str] = []
    plugin_calls: list[str] = []

    def handler_factory(name):
        def handler(_args):
            calls.append(name)
            return tool_result(name=name)

        return handler

    async def request_middleware(name, arguments, **_kwargs):
        plugin_calls.append(name)
        return RequestMiddlewareResult(
            payload=arguments,
            original_payload=arguments,
            changed=False,
            trace=[],
        )

    registry = Registry()
    for name in ("allowed_deferred", "blocked_deferred"):
        registry.register(
            name=name,
            toolset="deferred",
            schema={"name": name, "parameters": {}},
            handler=handler_factory(name),
            is_async=False,
            override=True,
        )
    plugins = PluginManager()
    plugins.apply_tool_request_middleware = request_middleware
    runner = _runner(
        registry,
        plugins=plugins,
        authorized_tool_names=frozenset({"allowed_deferred"}),
        tool_search_schemas=registry.list_schemas(),
        tool_search_config=ToolSearchConfig(enabled="on", core_toolsets=frozenset()),
    )
    tool_calls = [
        ToolCall("blocked", "blocked_deferred", {}),
        ToolCall("allowed", "allowed_deferred", {}),
    ]
    messages: list[Message] = []

    _ = [chunk async for chunk in runner.run_batch(tool_calls, messages, "rid", _seq_counter())]

    assert calls == ["allowed_deferred"]
    assert plugin_calls == ["allowed_deferred"]
    assert "TOOL_NOT_AUTHORIZED" in messages[0].content
    assert '"name": "allowed_deferred"' in messages[1].content


def test_is_tool_parallel_safe_classification():
    assert is_tool_parallel_safe(ToolCall("1", "file_read", {"path": "/tmp/a"}))
    assert is_tool_parallel_safe(ToolCall("2", "web_search", {"q": "x"}))
    assert not is_tool_parallel_safe(ToolCall("4", "terminal", {"command": "ls -la"}))
    assert not is_tool_parallel_safe(ToolCall("4b", "terminal", {"command": "git diff --stat"}))
    # 写/复杂命令/未知工具：不安全
    assert not is_tool_parallel_safe(ToolCall("3", "file_write", {"path": "/tmp/a", "content": "x"}))
    assert not is_tool_parallel_safe(ToolCall("4c", "terminal", {"command": "echo x > out.txt"}))
    assert not is_tool_parallel_safe(ToolCall("4d", "terminal", {"command": "rg foo | head"}))
    assert not is_tool_parallel_safe(ToolCall("4e", "terminal", {"command": "git reset --hard"}))
    assert not is_tool_parallel_safe(ToolCall("4f", "terminal", {"command": "sed -i.bak s/a/b/ file.txt"}))
    assert not is_tool_parallel_safe(ToolCall("5", "delegate_to_teammate", {"member": "a"}))
    assert not is_tool_parallel_safe(ToolCall("6", "unknown_tool", {}))


def test_browser_tool_label_uses_redacted_arguments():
    reg = Registry()
    reg.register(
        name="browser_navigate",
        toolset="browser",
        schema={"name": "browser_navigate", "parameters": {}},
        handler=lambda _args: "ok",
        ui_label_template="打开网页 {url}",
    )
    runner = _runner(reg)
    secret = "credential-value"
    call = ToolCall(
        "nav",
        "browser_navigate",
        {"url": f"https://example.com/callback?token={secret}&keywords=shoes"},
    )

    event = runner._start_event(call, "rid", _seq_counter())

    assert secret not in event.body["ui_label"]
    assert secret not in event.body["args"]
    assert "keywords" in event.body["ui_label"]


def test_browser_fill_form_start_event_and_trace_use_same_safe_projection(monkeypatch):
    reg = Registry()
    reg.register(
        name="browser_use",
        toolset="browser",
        schema={"name": "browser_use", "parameters": {}},
        handler=lambda _args: "ok",
        ui_label_template="浏览器 {action}",
    )
    traced: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "crew.agent.loop.tool_runner.llm_trace",
        lambda direction, payload: traced.append((direction, payload)),
    )
    runner = _runner(reg)
    call = ToolCall(
        "fill-form",
        "browser_use",
        {
            "action": "fill_form",
            "fields": [
                {
                    "type": "textbox",
                    "ref": "p9:e1",
                    "value": "must-never-enter-event-or-trace",
                },
                {"type": "slider", "ref": "p9:e2", "value": "91"},
            ],
        },
    )

    event = runner._start_event(call, "rid", _seq_counter())

    material = repr({"event": event.body, "trace": traced})
    assert "must-never-enter-event-or-trace" not in material
    assert "'91'" not in material
    assert '"field_count": 2' in event.body["args"]
    assert traced[0][1]["arguments"]["field_types"] == {
        "textbox": 1,
        "slider": 1,
    }


def test_segment_consecutive_safe_grouping():
    calls = [
        ToolCall("r1", "file_read", {"path": "/tmp/a"}),
        ToolCall("r2", "file_read", {"path": "/tmp/b"}),
        ToolCall("w1", "file_write", {"path": "/tmp/c", "content": "x"}),
        ToolCall("r3", "file_read", {"path": "/tmp/d"}),
    ]
    segs = segment_consecutive_safe(calls)
    shape = [(safe, [tc.id for tc in c]) for safe, c in segs]
    assert shape == [(True, ["r1", "r2"]), (False, ["w1"]), (True, ["r3"])]


async def test_prewarm_runs_safe_tool_once_and_run_batch_reuses():
    """prewarm 提前跑 safe 工具，run_batch 命中缓存复用，不重复执行。"""
    reg = Registry()
    runs: list[str] = []

    async def read_handler(args):
        runs.append(args.get("path"))
        return tool_result(path=args.get("path"))

    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=read_handler, is_async=True, override=True)

    runner = _runner(reg)
    tc = ToolCall("r1", "file_read", {"path": "/tmp/a.txt"})
    runner.prewarm(tc)
    assert "r1" in runner._prewarm  # 已提前派发
    await asyncio.sleep(0)  # 让 prewarm 任务起跑

    messages: list[Message] = []
    _ = [c async for c in runner.run_batch([tc], messages, "rid", _seq_counter())]
    assert runs == ["/tmp/a.txt"]  # 只执行一次（prewarm 跑的，run_batch 复用缓存）
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["r1"]
    assert not runner._prewarm  # 收尾已清空


async def test_deferred_tool_rejects_direct_guess_but_accepts_direct_call_after_search():
    reg = Registry()
    calls: list[str] = []
    reg.register(
        name="cron_create",
        toolset="cron",
        schema={"name": "cron_create", "parameters": {"type": "object"}},
        handler=lambda _args: calls.append("cron") or tool_result(ok=True),
        should_defer=True,
    )
    original = reg.list_schemas()
    runner = _runner(
        reg,
        tool_search_schemas=original,
        tool_search_config=ToolSearchConfig(enabled="on"),
        authorized_tool_names=frozenset({"cron_create"}),
        direct_tool_names=set(),
    )

    guessed = await runner._execute_one_body(ToolCall("guessed", "cron_create", {}))
    searched = await runner._execute_one_body(
        ToolCall("search", TOOL_SEARCH_NAME, {"query": "select:cron_create"})
    )
    direct = await runner._execute_one_body(ToolCall("direct", "cron_create", {}))

    assert guessed.is_error
    assert "按需加载" in guessed.content
    assert not searched.is_error
    assert runner.discovered_tool_names == {"cron_create"}
    assert not direct.is_error
    assert calls == ["cron"]


async def test_executor_loads_search_match_schema_then_executes_direct_tool_call():
    class ToolRecordingProvider(ScriptStreamProvider):
        def __init__(self, script):
            super().__init__(script)
            self.tool_names_by_call: list[set[str]] = []
            self.messages_by_call: list[list[Message]] = []

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            self.messages_by_call.append(list(messages))
            self.tool_names_by_call.append({
                str((schema.get("function") or {}).get("name") or "")
                for schema in (tools or [])
            })
            async for chunk in super().stream_chat(messages, tools, max_tokens=max_tokens):
                yield chunk

    calls: list[str] = []
    reg = Registry()
    reg.register(
        name="cron_create",
        toolset="cron",
        schema={
            "name": "cron_create",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        handler=lambda args: calls.append(args["name"]) or tool_result(ok=True),
    )
    provider = ToolRecordingProvider([
        ChatResponse(tool_calls=[
            ToolCall("search", TOOL_SEARCH_NAME, {"query": "select:cron_create"}),
        ]),
        ChatResponse(tool_calls=[
            ToolCall("create", "cron_create", {"name": "daily"}),
        ]),
        ChatResponse(text="done", finish_reason="stop"),
    ])
    ctx = _ctx(tool_schemas=reg.list_schemas(), enforce_tool_scope=True)

    await _collect(_executor(provider, reg), ctx)

    assert "cron_create" not in provider.tool_names_by_call[0]
    assert TOOL_SEARCH_NAME in provider.tool_names_by_call[0]
    assert "cron_create" in provider.tool_names_by_call[1]
    deferred_messages = [
        message
        for message in provider.messages_by_call[0]
        if message.content.startswith("<available-deferred-tools>")
    ]
    assert len(deferred_messages) == 1
    assert deferred_messages[0].is_meta is True
    assert deferred_messages[0].content == (
        "<available-deferred-tools>\ncron_create\n</available-deferred-tools>"
    )
    assert all(
        not message.content.startswith("<available-deferred-tools>")
        for message in ctx.messages
    )
    assert calls == ["daily"]


async def test_executor_rejects_provider_guess_of_any_hidden_deferred_tool():
    reg = Registry()
    calls: list[str] = []
    reg.register(
        name="cron_create",
        toolset="cron",
        schema={"name": "cron_create", "parameters": {"type": "object"}},
        handler=lambda _args: calls.append("cron") or tool_result(ok=True),
        should_defer=True,
    )
    provider = ScriptStreamProvider([
        ChatResponse(tool_calls=[ToolCall("guessed", "cron_create", {})]),
        ChatResponse(text="done", finish_reason="stop"),
    ])
    ctx = _ctx(
        tool_schemas=reg.list_schemas(),
        enforce_tool_scope=True,
    )

    await _collect(_executor(provider, reg), ctx)

    assert calls == []
    guessed_result = next(message for message in ctx.messages if message.tool_call_id == "guessed")
    assert "按需加载" in guessed_result.content


async def test_tool_media_is_appended_only_after_complete_tool_result_batch():
    reg = Registry()
    reg.register(
        name="browser_vision",
        toolset="browser",
        schema={"name": "browser_vision", "parameters": {}},
        handler=lambda _args: ToolOutput(
            "vision metadata",
            media=[MediaPart("image/png", data_url="data:image/png;base64,AAAA", alt="browser image")],
        ),
    )
    reg.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "parameters": {}},
        handler=lambda _args: tool_result(ok=True),
    )
    runner = _runner(reg)
    calls = [ToolCall("vision", "browser_vision", {}), ToolCall("read", "file_read", {})]
    messages: list[Message] = []

    _ = [chunk async for chunk in runner.run_batch(calls, messages, "rid", _seq_counter())]

    assert [message.role for message in messages] == ["tool", "tool", "user"]
    assert [message.tool_call_id for message in messages[:2]] == ["vision", "read"]
    assert messages[-1].is_meta and isinstance(messages[-1].content_parts, list)


async def test_prewarm_ignores_unsafe_tool():
    """写工具不提前派发，留给 run_batch 顺序执行。"""
    reg = Registry()
    reg.register(name="file_write", toolset="file", schema={"name": "file_write", "parameters": {}},
                 handler=lambda a: tool_result(ok=True), is_async=False, override=True)
    runner = _runner(reg)
    runner.prewarm(ToolCall("w", "file_write", {"path": "/tmp/a", "content": "x"}))
    assert "w" not in runner._prewarm


async def test_prewarm_dedups_identical_calls():
    """同 (name,args) 的并发安全调用只提前派发一次。"""
    reg = Registry()
    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=lambda a: tool_result(ok=True), is_async=False, override=True)
    runner = _runner(reg)
    runner.prewarm(ToolCall("a", "file_read", {"path": "/tmp/x"}))
    runner.prewarm(ToolCall("b", "file_read", {"path": "/tmp/x"}))  # 同参，去重
    assert list(runner._prewarm.keys()) == ["a"]
    await runner.cancel_prewarms()


async def test_interrupt_blocks_prewarm():
    """已请求中断时不再提前派发。"""
    reg = Registry()
    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=lambda a: tool_result(ok=True), is_async=False, override=True)
    ctrl = TurnControl()
    ctrl.interrupt()
    runner = _runner(reg, control=ctrl)
    runner.prewarm(ToolCall("r", "file_read", {"path": "/tmp/x"}))
    assert not runner._prewarm


async def test_mixed_batch_preserves_order_with_prewarm():
    """[read, write, read] 经分段执行后顺序不变，safe 读已 prewarm 复用。"""
    reg = Registry()
    runs: list[str] = []

    async def read_handler(args):
        runs.append("read:" + args.get("path"))
        return tool_result(path=args.get("path"))

    def write_handler(args):
        runs.append("write:" + args.get("path"))
        return tool_result(ok=True)

    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=read_handler, is_async=True, override=True)
    reg.register(name="file_write", toolset="file", schema={"name": "file_write", "parameters": {}},
                 handler=write_handler, is_async=False, override=True)

    runner = _runner(reg)
    calls = [
        ToolCall("r1", "file_read", {"path": "/tmp/a"}),
        ToolCall("w1", "file_write", {"path": "/tmp/b", "content": "x"}),
        ToolCall("r2", "file_read", {"path": "/tmp/c"}),
    ]
    for tc in calls:  # 流式期间会逐个 prewarm（写工具被忽略）
        runner.prewarm(tc)
    await asyncio.sleep(0)

    messages: list[Message] = []
    _ = [c async for c in runner.run_batch(calls, messages, "rid", _seq_counter())]
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["r1", "w1", "r2"]


# --------------------------------------------------------------------------- #
# 7b. 提前发 tool start 事件：unsafe 工具也提前发 start（仅显示，不执行），
#     run_batch 经 started_tool_call_ids 跳过重复 start，确保工具卡及时出现。
# --------------------------------------------------------------------------- #
async def test_unsafe_tool_start_skipped_by_run_batch_when_started_id_passed():
    """unsafe 工具已提前发 start（started_tool_call_ids 含其 id），run_batch 不重复发 start，只发 result。"""
    reg = Registry()
    reg.register(name="file_write", toolset="file", schema={"name": "file_write", "parameters": {}},
                 handler=lambda a: tool_result(ok=True), is_async=False, override=True)
    runner = _runner(reg)
    tc = ToolCall("w", "file_write", {"path": "/tmp/a", "content": "x"})
    # 模拟 executor 流式期间已发 start：prewarm 对 unsafe 返回 False（不执行），但仍记入 started_ids
    assert runner.prewarm(tc) is False
    assert "w" not in runner._prewarm  # unsafe 未起跑

    messages: list[Message] = []
    chunks = [c async for c in runner.run_batch(
        [tc], messages, "rid", _seq_counter(), started_tool_call_ids={"w"})]
    phases = [c.body["phase"] for c in chunks if c.kind == "tool"]
    assert "start" not in phases  # 已提前发，run_batch 不重复
    assert phases == ["result"]


async def test_safe_tool_start_skipped_by_run_batch_when_started_id_passed():
    """safe 工具 emit_start + prewarm，run_batch 命中缓存且跳过重复 start，只发 result。"""
    reg = Registry()

    async def read_handler(args):
        return tool_result(path=args.get("path"))

    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=read_handler, is_async=True, override=True)
    runner = _runner(reg)
    tc = ToolCall("r1", "file_read", {"path": "/tmp/a"})
    assert runner.prewarm(tc) is True  # safe 起跑
    await asyncio.sleep(0)  # 让 prewarm 跑完

    messages: list[Message] = []
    chunks = [c async for c in runner.run_batch(
        [tc], messages, "rid", _seq_counter(), started_tool_call_ids={"r1"})]
    phases = [c.body["phase"] for c in chunks if c.kind == "tool"]
    assert "start" not in phases
    assert phases == ["result"]


async def test_run_batch_emits_start_when_not_started_id():
    """未提前发 start 的工具（started_tool_call_ids 不含其 id），run_batch 正常发 start + result。"""
    reg = Registry()
    reg.register(name="file_write", toolset="file", schema={"name": "file_write", "parameters": {}},
                 handler=lambda a: tool_result(ok=True), is_async=False, override=True)
    runner = _runner(reg)
    tc = ToolCall("w", "file_write", {"path": "/tmp/a", "content": "x"})

    messages: list[Message] = []
    chunks = [c async for c in runner.run_batch(
        [tc], messages, "rid", _seq_counter(), started_tool_call_ids=set())]
    phases = [c.body["phase"] for c in chunks if c.kind == "tool"]
    assert phases == ["start", "result"]


async def test_explicit_security_rejection_fences_later_tools_in_same_batch():
    reg = Registry()
    calls: list[str] = []

    async def terminal(_args):
        calls.append("terminal")
        return json.dumps({
            "success": False,
            "error": "用户拒绝了该命令",
            "error_code": "approval_rejected",
        }, ensure_ascii=False)

    async def glob_handler(_args):
        calls.append("glob")
        return tool_result(files=["secret.txt"])

    reg.register(
        name="terminal",
        toolset="terminal",
        schema={"name": "terminal", "parameters": {}},
        handler=terminal,
        is_async=True,
    )
    reg.register(
        name="glob",
        toolset="file",
        schema={"name": "glob", "parameters": {}},
        handler=glob_handler,
        is_async=True,
    )
    runner = _runner(reg, parallel_enabled=False)
    messages: list[Message] = []
    tool_calls = [
        ToolCall("t1", "terminal", {"command": "ls ~/Desktop"}),
        ToolCall("g1", "glob", {"path": "~/Desktop", "pattern": "*"}),
    ]

    _ = [chunk async for chunk in runner.run_batch(
        tool_calls, messages, "rid", _seq_counter(),
    )]

    assert calls == ["terminal"]
    assert runner.approval_rejected is True
    assert "approval_rejected_turn_stopped" in messages[-1].content


async def test_security_runtime_failure_is_a_real_tool_error():
    reg = Registry()
    reg.register(
        name="terminal",
        toolset="terminal",
        schema={"name": "terminal", "parameters": {}},
        handler=lambda _args: json.dumps({
            "success": False,
            "error": "安全运行时异常退出，命令未执行",
            "error_code": "runtime_crashed",
        }, ensure_ascii=False),
    )
    runner = _runner(reg, parallel_enabled=False)
    messages: list[Message] = []

    chunks = [chunk async for chunk in runner.run_batch(
        [ToolCall("t1", "terminal", {"command": "ls ~/Desktop"})],
        messages,
        "rid",
        _seq_counter(),
    )]

    assert runner.security_boundary_failed is True
    assert any(chunk.body.get("phase") == "result" for chunk in chunks)
    assert messages[-1].content.endswith('"runtime_crashed"}')


class ReadyStreamProvider(LLMProvider):
    """第一轮流式吐 ready_tool_call（逐个）后 done；流尾 await gate 证明工具已并行起跑。"""

    def __init__(self, tool_calls, gate: asyncio.Event):
        self._tool_calls = tool_calls
        self._gate = gate
        self._n = 0

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="完成", finish_reason="stop")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self._n += 1
        if self._n > 1:
            yield StreamChunk(delta_text="完成")
            yield StreamChunk(delta_text="", done=True, finish_reason="stop")
            return
        for tc in self._tool_calls:
            yield StreamChunk(ready_tool_call=tc)
        # 流尾阻塞，直到工具 handler set 了 gate —— 若工具没在流式期间起跑，这里会超时
        await asyncio.wait_for(self._gate.wait(), timeout=2.0)
        yield StreamChunk(delta_text="", done=True, tool_calls=self._tool_calls, finish_reason="tool_calls")


async def test_tool_executes_during_streaming_window():
    """端到端：safe 工具在流式还没结束时就已开跑（与流重叠）。"""
    reg = Registry()
    gate = asyncio.Event()

    async def read_handler(args):
        gate.set()  # 工具一旦真正执行就放行流尾
        return tool_result(path=args.get("path"))

    reg.register(name="file_read", toolset="file", schema={"name": "file_read", "parameters": {}},
                 handler=read_handler, is_async=True, override=True)

    tc = ToolCall("r1", "file_read", {"path": "/tmp/a.txt"})
    ex = _executor(ReadyStreamProvider([tc], gate), reg)
    ctx = _ctx()
    await _collect(ex, ctx)  # 不超时即证明工具在流式期间已执行
    assert [m.tool_call_id for m in ctx.messages if m.role == "tool"] == ["r1"]


class ReasoningDeltaProvider(LLMProvider):
    """流式先吐 reasoning，再吐正文，用于验证首字前 thinking 可见。"""

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="答案", reasoning_content="先分析，再结论")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(reasoning_content="先分析")
        yield StreamChunk(reasoning_content="，再结论")
        yield StreamChunk(delta_text="答案")
        yield StreamChunk(done=True, reasoning_content="先分析，再结论", finish_reason="stop")


async def test_streaming_reasoning_is_visible_before_answer_delta(caplog):
    """reasoning 增量应转成 thinking 事件，并早于正文首个 delta 到达。"""
    caplog.set_level("INFO", logger="crew.agent.executor")
    ex = _executor(ReasoningDeltaProvider())
    chunks = await _collect(ex, _ctx())

    kinds = [c.kind for c in chunks]
    assert kinds.index("thinking") < kinds.index("delta")
    assert [c.body["text"] for c in chunks if c.kind == "thinking"] == [
        "先分析",
        "先分析，再结论",
    ]
    assert [c.body["text"] for c in chunks if c.kind == "delta"] == ["答案"]
    perf_line = next(record.message for record in caplog.records if "[PERF] llm " in record.message)
    assert "first_event=" in perf_line
    assert "first_reasoning=" in perf_line
    assert "first_text=" in perf_line
    assert any("[PERF] pre_llm_hooks" in record.message for record in caplog.records)


class FinalReasoningOnlyProvider(LLMProvider):
    """仅在 done 帧给最终 reasoning，模拟不提供增量推理的兼容端点。"""

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="", reasoning_content="仅最终推理")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(done=True, reasoning_content="仅最终推理", finish_reason="stop")


async def test_final_only_reasoning_records_first_reasoning_without_duplicate(caplog):
    """done-only reasoning 应参与计时，并保持既有单帧 thinking 语义。"""
    caplog.set_level("INFO", logger="crew.agent.executor")
    chunks = await _collect(_executor(FinalReasoningOnlyProvider()), _ctx())

    assert [chunk.body["text"] for chunk in chunks if chunk.kind == "thinking"] == ["仅最终推理"]
    perf_line = next(record.message for record in caplog.records if "[PERF] llm" in record.message)
    assert "first_reasoning=-1.000s" not in perf_line


class VisibleReadyStreamProvider(LLMProvider):
    """ready_tool_call 后等待测试释放，证明 executor 会先把 tool start 交给 UI。"""

    def __init__(self, tool_call: ToolCall, release_after_start: asyncio.Event):
        self._tool_call = tool_call
        self._release_after_start = release_after_start
        self.continued_after_ready = asyncio.Event()
        self._calls = 0

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="完成", finish_reason="stop")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self._calls += 1
        if self._calls > 1:
            yield StreamChunk(delta_text="完成")
            yield StreamChunk(done=True, finish_reason="stop")
            return
        yield StreamChunk(ready_tool_call=self._tool_call)
        self.continued_after_ready.set()
        await asyncio.wait_for(self._release_after_start.wait(), timeout=2.0)
        yield StreamChunk(done=True, tool_calls=[self._tool_call], finish_reason="tool_calls")


@pytest.mark.parametrize(
    ("tool_name", "call_id", "args", "handler", "sanitized_args"),
    [
        ("file_read", "r1", {"path": "/tmp/a.txt"}, lambda a: tool_result(path=a.get("path")), None),
        (
            "file_write",
            "w1",
            {"path": "/tmp/crew_test_unsafe.html", "content": "x"},
            lambda a: tool_result(ok=True),
            '{"path": "/tmp/crew_test_unsafe.html"}',
        ),
    ],
    ids=["safe", "unsafe"],
)
async def test_ready_tool_call_emits_visible_start_before_stream_continues(
    tool_name, call_id, args, handler, sanitized_args
):
    """safe/unsafe 工具 ready 时立即显示 start；run_batch 后续只补 result，不重复 start。

    unsafe 的 start 事件与 prewarm 解耦，仅用于及时展示（参数脱敏）；实际执行仍由
    run_batch 使用完整参数完成。
    """
    reg = Registry()
    reg.register(
        name=tool_name,
        toolset="file",
        schema={"name": tool_name, "parameters": {}},
        handler=handler,
        is_async=False,
        override=True,
    )
    release_after_start = asyncio.Event()
    tc = ToolCall(call_id, tool_name, args)
    provider = VisibleReadyStreamProvider(tc, release_after_start)
    ex = _executor(provider, reg)

    stream = ex.execute(_ctx())
    try:
        # 修复后：safe/unsafe 都提前发 start，第一个 chunk 即 tool start，不等 release
        first = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
        assert first.kind == "tool"
        assert first.body["phase"] == "start"
        assert first.body["tool_call_id"] == call_id
        if sanitized_args is not None:
            assert first.body["args"] == sanitized_args
            assert "content" not in first.body["detail"]
        assert not provider.continued_after_ready.is_set()

        release_after_start.set()
        rest = [c async for c in stream]
    finally:
        release_after_start.set()
        await stream.aclose()

    tool_events = [first, *[c for c in rest if c.kind == "tool"]]
    starts = [c for c in tool_events if c.body["phase"] == "start"]
    results = [c for c in tool_events if c.body["phase"] == "result"]
    assert [c.body["tool_call_id"] for c in starts] == [call_id]
    assert [c.body["tool_call_id"] for c in results] == [call_id]


class SeenThenReadyProvider(LLMProvider):
    """先 yield tool_call_seen（legacy name seen），等 release，再 yield ready_tool_call
    （完整参数）+ done。证明 executor 将 seen 兼容映射为 generating。"""

    def __init__(self, tc_seen, tc_ready, release_after_seen):
        self._tc_seen = tc_seen
        self._tc_ready = tc_ready
        self._release = release_after_seen
        self._calls = 0

    async def chat(self, messages, tools=None):  # pragma: no cover
        return ChatResponse(text="完成", finish_reason="stop")

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        self._calls += 1
        if self._calls > 1:
            yield StreamChunk(delta_text="完成")
            yield StreamChunk(done=True, finish_reason="stop")
            return
        yield StreamChunk(tool_call_seen=self._tc_seen)
        await asyncio.wait_for(self._release.wait(), timeout=2.0)
        yield StreamChunk(ready_tool_call=self._tc_ready)
        yield StreamChunk(done=True, tool_calls=[self._tc_ready], finish_reason="tool_calls")


async def test_tool_call_seen_emits_generating_then_ready_start():
    """tool_call_seen 到达即 emit generating；ready 到达后再 emit start。"""
    reg = Registry()

    async def read_handler(args):
        return tool_result(path=args.get("path"))

    reg.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "parameters": {}},
        handler=read_handler,
        is_async=True,
        override=True,
    )
    release = asyncio.Event()
    tc_seen = ToolCall("r1", "file_read", {})  # name 出现时参数空
    tc_ready = ToolCall("r1", "file_read", {"path": "/tmp/a.txt"})  # 完整参数
    provider = SeenThenReadyProvider(tc_seen, tc_ready, release)
    ex = _executor(provider, reg)

    stream = ex.execute(_ctx())
    try:
        # seen 时立即 emit generating，不等 release/ready
        first = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
        assert first.kind == "tool" and first.body["phase"] == "generating"
        assert first.body["tool_call_id"] == "r1"
        assert not release.is_set()  # generating 在 release 之前，证明 seen 即发

        release.set()
        rest = [c async for c in stream]
    finally:
        release.set()
        await stream.aclose()

    tool_events = [first, *[c for c in rest if c.kind == "tool"]]
    generating = [c for c in tool_events if c.body["phase"] == "generating"]
    starts = [c for c in tool_events if c.body["phase"] == "start"]
    results = [c for c in tool_events if c.body["phase"] == "result"]
    assert [c.body["tool_call_id"] for c in generating] == ["r1"]
    assert [c.body["tool_call_id"] for c in starts] == ["r1"]
    assert [c.body["tool_call_id"] for c in results] == ["r1"]


# --------------------------------------------------------------------------- #
# 7. auto-compact：executor 循环内 compact_view + 主 agent 无限迭代
# --------------------------------------------------------------------------- #
class _RecordingCompactor:
    """记录 compact_view 调用；返回固定 marker 视图以验证 view/canonical 分离。

    返回单条 marker 消息，使「发给 LLM 的视图」与「ctx.messages 全量」明显可区分。
    """

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.marker = Message.user("__compacted_view__")

    async def compact_view(
        self,
        messages,
        session_id=None,
        owner_account_id=None,
        prompt_overhead_tokens=0,
    ):
        self.calls.append(len(messages))
        return [self.marker]


def _echo_registry() -> Registry:
    reg = Registry()
    reg.register(
        name="echo",
        toolset="t",
        schema={"name": "echo", "parameters": {}},
        handler=lambda a: tool_result("ok"),
        is_async=False,
    )
    return reg


class _CountingToolProvider(FakeProvider):
    """前 n 次返回工具调用，之后返回 final 文本。记录每次收到的视图。"""

    def __init__(self, tool_calls_left: int) -> None:
        super().__init__()
        self._left = tool_calls_left
        self.received_views: list[list[Message]] = []

    async def chat(self, messages, tools=None):
        self.received_views.append(list(messages))
        if self._left > 0:
            self._left -= 1
            return ChatResponse(tool_calls=[ToolCall("c", "echo", {})])
        return ChatResponse(text="done", finish_reason="stop")


async def test_midloop_compact_view_runs_each_iteration_and_preserves_canonical():
    """每轮模型调用前跑 compact_view；发给 LLM 的是压缩视图，ctx.messages 保留全量。"""
    provider = _CountingToolProvider(3)  # 3 次工具调用 + 1 次 final = 4 轮模型调用
    comp = _RecordingCompactor()
    ex = _executor(provider, _echo_registry(), compactor=comp, max_iterations=0)
    ctx = _ctx()
    chunks = await _collect(ex, ctx)

    # compact_view 每轮调一次，共 4 次
    assert len(comp.calls) == 4
    # provider 收到的始终是压缩后的 marker 视图，而非 ctx.messages 全量
    for view in provider.received_views:
        assert any(m.content == "__compacted_view__" for m in view)
    # ctx.messages 全量保留：1 user + 4 assistant（3 工具 + 1 final）+ 3 tool 结果
    roles = [m.role for m in ctx.messages]
    assert roles[0] == "user"
    assert roles.count("assistant") == 4
    assert roles.count("tool") == 3
    # 正常完成，final 是 done 文本
    finals = [c for c in chunks if c.kind == "final"]
    assert finals and finals[-1].body["text"] == "done"
    assert finals[-1].body.get("reason") != "max_iterations"


async def test_unlimited_iterations_no_hard_cap():
    """max_iterations=0 无限：循环 10 次（超旧默认 6）仍正常完成，不触 max_iterations。"""
    provider = _CountingToolProvider(10)
    ex = _executor(provider, _echo_registry(), max_iterations=0)
    chunks = await _collect(ex, _ctx())
    finals = [c for c in chunks if c.kind == "final"]
    assert finals and finals[-1].body["text"] == "done"
    assert finals[-1].body.get("reason") != "max_iterations"


async def test_max_iterations_cap_still_triggers_with_reason():
    """max_iterations=3 + 持续成功工具调用（guardrail 默认 hard_stop_enabled=False 不干扰）
    → 命中上限，final 带 reason=max_iterations。"""
    class _AlwaysTool(FakeProvider):
        async def chat(self, messages, tools=None):
            return ChatResponse(tool_calls=[ToolCall("c", "echo", {})])

    provider = _AlwaysTool()
    ex = _executor(provider, _echo_registry(), max_iterations=3)
    chunks = await _collect(ex, _ctx())
    finals = [c for c in chunks if c.kind == "final"]
    assert finals and finals[-1].body.get("reason") == "max_iterations"
