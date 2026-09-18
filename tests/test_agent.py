"""单 Agent 对话循环：用脚本化 FakeProvider 驱动一次工具调用 + 最终回答。

另含可插拔执行层（AgentExecutor）相关用例：工厂、替换、重试、压缩、标题。
"""

import asyncio

import pytest

from crew.agent.compact import ContextCompactor, estimate_tokens
from crew.agent.executor import (
    AcpExecutor,
    AgentExecutor,
    BuiltinExecutor,
    ClientExecutor,
    ExecutionContext,
    create_executor,
)
from crew.agent.executor.builtin import _RETRY_DELAY_CAP_SECONDS
from crew.agent.runtime import SingleAgent
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ConfigError, CrewErrorKind, ProviderError
from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.core.types import ChatResponse, Message, StreamChunk, ToolCall
from crew.plugins.manager import PluginManager
from crew.tools.registry import Registry, register_builtin_tools, tool_result


def _agent(provider, **kw):
    reg = Registry()
    register_builtin_tools(reg)
    return SingleAgent(
        provider=provider,
        registry=reg,
        session_store=kw.pop("session_store", InMemorySessionStore()),
        memory=NullMemory(),
        plugins=kw.pop("plugins", PluginManager()),
        max_iterations=5,
        **kw,
    )


async def test_agent_runs_tool_then_finalizes():
    provider = FakeProvider(script=[
        ChatResponse(tool_calls=[ToolCall("c1", "terminal", {"command": "echo abc"})]),
        ChatResponse(text="完成"),
    ])
    agent = _agent(provider)
    kinds = []
    final = None
    async for ch in agent.run(Envelope.of("跑一下", session_id="s1")):
        kinds.append(ch.kind)
        if ch.kind == "final":
            final = ch.body["text"]
    assert "tool" in kinds
    assert final == "完成"
    # provider 第二次调用时，messages 里应已包含工具结果
    assert any(m.role == "tool" for m in provider.calls[1])


async def test_agent_plain_answer_no_tools():
    agent = _agent(FakeProvider())  # 回声模式
    final = None
    async for ch in agent.run(Envelope.of("你好", session_id="s2")):
        if ch.kind == "final":
            final = ch.body["text"]
    assert "你好" in final


async def test_agent_repairs_orphan_tool_calls_on_cold_load():
    """冷读历史里的孤儿 tool_call（崩溃残留）先合成 error 结果再进入本轮，
    且随本轮落库持久化——再次冷读扫描为空，不重复合成。"""
    from crew.agent.loop import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN

    store = InMemorySessionStore()
    store.save("orphan-s", [
        Message.user("改文件"),
        Message.assistant("我来写", [
            ToolCall("c1", "file_write", {"path": "/tmp/a", "content": "x"}, status="running"),
            ToolCall("c2", "file_read", {"path": "/tmp/b"}),
        ]),
    ], owner_account_id="local")
    provider = FakeProvider()
    agent = _agent(provider, session_store=store)

    async for _ in agent.run(Envelope.of("继续", session_id="orphan-s")):
        pass

    history = store.load("orphan-s", owner_account_id="local")
    tool_msgs = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2"]
    assert tool_msgs[0].content.startswith(TOOL_OUTCOME_UNKNOWN)
    assert tool_msgs[1].content.startswith(TOOL_NOT_STARTED)
    # provider 本轮收到的视图里，两个 tool_call 均已有配对结果
    assert {m.tool_call_id for m in provider.calls[-1] if m.role == "tool"} == {"c1", "c2"}

    async for _ in agent.run(Envelope.of("再来", session_id="orphan-s")):
        pass
    history2 = store.load("orphan-s", owner_account_id="local")
    assert [m.tool_call_id for m in history2 if m.role == "tool"] == ["c1", "c2"]


async def test_context_preview_counts_same_l1_view_used_before_send():
    store = InMemorySessionStore()
    history: list[Message] = [Message.user("开始调研")]
    for i in range(12):
        call_id = f"browser-{i}"
        history.extend([
            Message.assistant(
                tool_calls=[ToolCall(call_id, "browser_use", {"action": "snapshot"})]
            ),
            Message.tool(call_id, "页面内容" * 10_000, name="browser_use"),
        ])
    store.save("preview-l1", history, owner_account_id="local")
    compactor = ContextCompactor(
        FakeProvider(),
        token_budget=1_000_000,
        keep_recent_tools=2,
        max_tool_result_chars=20_000,
    )
    agent = _agent(
        FakeProvider(),
        session_store=store,
        compactor=compactor,
    )

    preview = await agent.preview_context("preview-l1", owner_account_id="local")

    assert preview is not None
    assert preview["used_tokens"] < estimate_tokens(history)


async def test_agent_streaming_yields_delta():
    """Agent 使用 stream_chat() 时应逐 token yield delta chunk。"""
    agent = _agent(FakeProvider())  # 回声模式
    kinds = []
    deltas = []
    async for ch in agent.run(Envelope.of("流式测试", session_id="s3")):
        kinds.append(ch.kind)
        if ch.kind == "delta":
            deltas.append(ch.body["text"])
    # 应该收到 delta 帧
    assert "delta" in kinds
    # delta 拼接后应包含原始文本
    assert "流式测试" in "".join(deltas)
    # 最终应有 final 帧
    assert "final" in kinds


# ---------------------------------------------------------------------------
# 可插拔执行层
# ---------------------------------------------------------------------------
def test_factory_builds_executors():
    """create_executor 按 kind 返回对应执行器，未知类型抛 ConfigError。"""
    reg = Registry()
    deps = dict(provider=FakeProvider(), registry=reg, plugins=PluginManager())
    assert isinstance(create_executor("builtin", **deps), BuiltinExecutor)
    assert isinstance(create_executor("client", **deps), ClientExecutor)
    assert isinstance(create_executor("acp", **deps), AcpExecutor)
    with pytest.raises(ConfigError):
        create_executor("nope", **deps)


async def test_external_executors_current_contract():
    """Client 缺入口时报 NotImplemented；ACP 已进入错误帧契约。"""
    ctx = ExecutionContext(
        session_id="s", request_id="r", system_prompt="", messages=[], query="hi"
    )
    with pytest.raises(NotImplementedError):
        async for _ in ClientExecutor({}).execute(ctx):
            pass

    chunks = [ch async for ch in AcpExecutor({"command": "codex"}).execute(ctx)]
    assert chunks[-1].kind == "error"
    assert "external_agent_id" in chunks[-1].body["message"]


class _FixedExecutor(AgentExecutor):
    """直接产出固定 final 的假执行器。"""

    name = "fixed"

    async def execute(self, ctx: ExecutionContext):
        ctx.messages.append(Message.assistant("固定回答"))
        yield ResponseChunk.final(ctx.request_id, "固定回答", 1)


class _CapturingExecutor(_FixedExecutor):
    """记录 SingleAgent 组装的执行上下文，验证展示 schema 与授权集分离。"""

    def __init__(self) -> None:
        self.context: ExecutionContext | None = None

    async def execute(self, ctx: ExecutionContext):
        self.context = ctx
        async for chunk in super().execute(ctx):
            yield chunk


class _RunContextCapturingExecutor(_FixedExecutor):
    """记录工具层看到的内部会话和用户可见会话。"""

    def __init__(self) -> None:
        self.task_session_id = ""
        self.display_session_id = ""

    async def execute(self, ctx: ExecutionContext):
        from crew.core.runctx import current_display_session_id, current_session_id

        self.task_session_id = current_session_id.get()
        self.display_session_id = current_display_session_id.get()
        async for chunk in super().execute(ctx):
            yield chunk


class _RecordingResultFileExecutor(_FixedExecutor):
    """模拟 sidechain 执行器按 task_session_id 记录终端生成的最终产物。"""

    def __init__(self, plan_manager, result_path: str) -> None:
        self.plan_manager = plan_manager
        self.result_path = result_path

    async def execute(self, ctx: ExecutionContext):
        self.plan_manager.record_turn_file_change(
            ctx.session_id,
            {
                "path": self.result_path,
                "name": "最终结果.pptx",
                "added": 0,
                "removed": 0,
                "status": "added",
                "binary": True,
            },
            owner_account_id="local",
        )
        async for chunk in super().execute(ctx):
            yield chunk


class _ExplodingExecutor(AgentExecutor):
    name = "exploding"

    async def execute(self, ctx: ExecutionContext):
        raise RuntimeError("unexpected executor failure")
        yield  # pragma: no cover - 保持 async generator 契约


class _BlockingExecutor(AgentExecutor):
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, ctx: ExecutionContext):
        self.started.set()
        await asyncio.Event().wait()
        yield ResponseChunk.final(ctx.request_id, "unreachable")


def _outcome_recorder() -> tuple[PluginManager, list[dict]]:
    plugins = PluginManager()
    calls: list[dict] = []

    async def on_session_end(session_id, outcome, error_summary):
        calls.append({
            "session_id": session_id,
            "outcome": outcome,
            "error_summary": error_summary,
        })

    plugins._hooks["on_session_end"] = [on_session_end]
    return plugins, calls


async def test_agent_uses_injected_executor_and_persists():
    """注入自定义 executor 时，壳走它且仍正常落库。"""
    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, executor=_FixedExecutor())
    final = None
    async for ch in agent.run(Envelope.of("你好", session_id="sx")):
        if ch.kind == "final":
            final = ch.body["text"]
    assert final == "固定回答"
    saved = store.load("sx", owner_account_id="local")
    assert saved[-1].role == "assistant" and saved[-1].content == "固定回答"


async def test_agent_maps_sidechain_interactions_to_visible_session():
    executor = _RunContextCapturingExecutor()
    agent = _agent(FakeProvider(), executor=executor)
    envelope = Envelope.of("审阅", session_id="team-session::turn::req-1::leader")
    envelope.params["task_session_id"] = "team-session::turn::req-1"

    _ = [chunk async for chunk in agent.run(envelope)]

    assert executor.task_session_id == "team-session::turn::req-1"
    assert executor.display_session_id == "team-session"


def test_sidechain_persists_result_files_recorded_under_task_session_id(tmp_path):
    """重启恢复应读取到实时阶段记录的最终结果，而不只剩 tool_call 过程文件。"""
    from crew.agent.plan import PlanModeManager

    async def run_case():
        store = InMemorySessionStore()
        manager = PlanModeManager()
        result_path = str(tmp_path / "最终结果.pptx")
        executor = _RecordingResultFileExecutor(manager, result_path)
        agent = SingleAgent(
            provider=FakeProvider(),
            registry=Registry(),
            session_store=store,
            memory=NullMemory(),
            plugins=PluginManager(),
            executor=executor,
            plan_manager=manager,
        )
        envelope = Envelope.of("生成 PPT", session_id="stable::turn::req-1")
        envelope.params["task_session_id"] = "stable"

        _ = [chunk async for chunk in agent.run(envelope)]

        saved = store.load("stable::turn::req-1", owner_account_id="local")
        assistant = next(message for message in reversed(saved) if message.role == "assistant")
        assert assistant.turn_file_changes == [
            {
                "path": result_path,
                "name": "最终结果.pptx",
                "added": 0,
                "removed": 0,
                "status": "added",
                "binary": True,
                "created_in_session": True,
            }
        ]
        assert manager.drain_turn_file_changes("stable", owner_account_id="local") == []

    asyncio.run(run_case())


async def test_agent_passes_authorization_and_request_params_to_executor():
    """执行上下文同时携带最终工具授权和远端新增的请求参数。"""
    executor = _CapturingExecutor()
    agent = _agent(FakeProvider(), executor=executor, tool_filter=["terminal"])
    envelope = Envelope.of("运行命令", session_id="auth-scope")
    envelope.params["client_intent"] = "revision"

    _ = [chunk async for chunk in agent.run(envelope)]

    assert executor.context is not None
    assert executor.context.authorized_tool_names == frozenset({"terminal"})
    assert {schema["function"]["name"] for schema in executor.context.tool_schemas} == {"terminal"}
    assert executor.context.params["client_intent"] == "revision"
    assert executor.context.params["query"] == "运行命令"


async def test_agent_passes_normalized_attachment_context_to_executor(tmp_path):
    executor = _CapturingExecutor()
    agent = _agent(FakeProvider(), executor=executor)
    attachment = tmp_path / "需求说明.txt"
    attachment.write_text("必须支持附件路径。", encoding="utf-8")
    envelope = Envelope.of(
        "请根据附件开发",
        session_id="attachment-context",
        attachments=[{
            "name": "需求说明.txt",
            "path": str(attachment),
            "type": "file",
        }],
    )

    _ = [chunk async for chunk in agent.run(envelope)]

    assert executor.context is not None
    assert f"附件「需求说明.txt」位于: {attachment}" in executor.context.query
    assert executor.context.query.endswith("请根据附件开发")
    assert executor.context.attachments == envelope.attachments
    assert executor.context.attachments is not envelope.attachments
    assert executor.context.messages[-2].role == "user"
    assert any(
        "必须支持附件路径。" in message.content
        for message in executor.context.messages
    )


async def test_dedicated_wiki_agent_disables_tool_search_in_execution_context():
    executor = _CapturingExecutor()
    agent = _agent(
        FakeProvider(),
        executor=executor,
        tool_filter=["terminal"],
        tool_disclosure_mode="direct",
    )

    _ = [chunk async for chunk in agent.run(Envelope.of("运行命令", session_id="wiki-scope"))]

    assert executor.context is not None
    assert executor.context.tool_disclosure_mode == "direct"
    assert executor.context.authorized_tool_names == frozenset({"terminal"})


class _FlakyProvider(FakeProvider):
    """首次 stream_chat 抛可重试错误，之后正常。"""

    def __init__(self, fail_times: int, retryable: bool = True):
        super().__init__()
        self._fail_times = fail_times
        self._retryable = retryable

    async def stream_chat(self, messages, tools=None):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ProviderError("瞬时错误", retryable=self._retryable)
        yield StreamChunk(delta_text="好了")
        yield StreamChunk(delta_text="", done=True)


async def test_builtin_executor_retries_then_succeeds():
    ex = BuiltinExecutor(
        _FlakyProvider(fail_times=1), Registry(), PluginManager(),
        max_retries=2, backoff_seconds=0,
    )
    ctx = ExecutionContext(
        session_id="s", request_id="r", system_prompt="sys", messages=[Message.user("hi")], query="hi"
    )
    kinds = [ch.kind async for ch in ex.execute(ctx)]
    assert "error" not in kinds and "final" in kinds


async def test_builtin_executor_no_retry_when_fatal():
    ex = BuiltinExecutor(
        _FlakyProvider(fail_times=1, retryable=False), Registry(), PluginManager(),
        max_retries=2, backoff_seconds=0,
    )
    ctx = ExecutionContext(
        session_id="s", request_id="r", system_prompt="sys", messages=[Message.user("hi")], query="hi"
    )
    kinds = [ch.kind async for ch in ex.execute(ctx)]
    assert kinds[-1] == "error"


class _DelayFlakyProvider(FakeProvider):
    """前 fail_times 次抛同一个可重试 ProviderError（可带 retry_delay），之后正常。"""

    def __init__(self, fail_times: int, error: ProviderError):
        super().__init__()
        self._fail_times = fail_times
        self._error = error
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error
        yield StreamChunk(delta_text="好了")
        yield StreamChunk(delta_text="", done=True)


@pytest.fixture
def _recorded_sleeps(monkeypatch):
    """录下 retry 段的 asyncio.sleep 延迟并跳过真实等待。"""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("crew.agent.executor.builtin.asyncio.sleep", fake_sleep)
    return delays


def _retry_executor(provider, *, max_retries=2, backoff_seconds=10.0) -> BuiltinExecutor:
    return BuiltinExecutor(
        provider, Registry(), PluginManager(),
        max_retries=max_retries, backoff_seconds=backoff_seconds, stream_retry_jitter=False,
    )


def _retry_ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="s", request_id="r", system_prompt="sys", messages=[Message.user("hi")], query="hi"
    )


async def test_builtin_executor_retry_waits_server_retry_delay(_recorded_sleeps):
    """服务端建议的 retry_delay 优先于本地指数退避（本地 backoff=10s，实际应等 0.05s）。"""
    err = ProviderError("限流", kind=CrewErrorKind.USAGE_LIMIT, retryable=True, status=429, retry_delay=0.05)
    provider = _DelayFlakyProvider(fail_times=1, error=err)
    ex = _retry_executor(provider)
    kinds = [ch.kind async for ch in ex.execute(_retry_ctx())]
    assert kinds[-1] == "final"
    assert _recorded_sleeps == [0.05]


async def test_builtin_executor_retry_delay_capped(_recorded_sleeps):
    """异常大的 retry_delay 被封顶，避免拖死重试循环。"""
    err = ProviderError("限流", kind=CrewErrorKind.USAGE_LIMIT, retryable=True, status=429, retry_delay=7200.0)
    provider = _DelayFlakyProvider(fail_times=1, error=err)
    ex = _retry_executor(provider, backoff_seconds=0)
    kinds = [ch.kind async for ch in ex.execute(_retry_ctx())]
    assert kinds[-1] == "final"
    assert _recorded_sleeps == [_RETRY_DELAY_CAP_SECONDS]


async def test_builtin_executor_non_crew_error_not_retried(_recorded_sleeps):
    """非 CrewError 异常维持原语义：不重试、不等待，直接 error 帧。"""

    class _BoomProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            raise ValueError("boom")

    provider = _BoomProvider()
    ex = _retry_executor(provider)
    kinds = [ch.kind async for ch in ex.execute(_retry_ctx())]
    assert kinds[-1] == "error"
    assert provider.calls == 1
    assert _recorded_sleeps == []


async def test_builtin_executor_error_frame_carries_kind_fields():
    """可重试错误耗尽重试后，error 帧 body 带 kind(code)/retryable/retry_delay。"""
    err = ProviderError("超时", kind=CrewErrorKind.TIMEOUT, retryable=True, retry_delay=2.5)
    provider = _DelayFlakyProvider(fail_times=3, error=err)
    ex = _retry_executor(provider, max_retries=0)
    chunks = [ch async for ch in ex.execute(_retry_ctx())]
    error = chunks[-1]
    assert error.kind == "error"
    assert error.body["code"] == "timeout"
    assert error.body["retryable"] is True
    assert error.body["retry_delay"] == 2.5


async def test_agent_session_end_reports_final_and_provider_failure_once():
    completed_plugins, completed_calls = _outcome_recorder()
    completed_agent = _agent(FakeProvider(), plugins=completed_plugins)
    completed_chunks = [
        chunk async for chunk in completed_agent.run(Envelope.of("ok", session_id="completed"))
    ]

    failed_plugins, failed_calls = _outcome_recorder()
    failed_agent = _agent(
        _FlakyProvider(fail_times=1, retryable=False),
        plugins=failed_plugins,
    )
    failed_chunks = [
        chunk async for chunk in failed_agent.run(Envelope.of("fail", session_id="failed"))
    ]

    assert completed_chunks[-1].kind == "final"
    assert completed_calls == [
        {"session_id": "completed", "outcome": "completed", "error_summary": ""}
    ]
    assert failed_chunks[-1].kind == "error"
    assert len(failed_calls) == 1
    assert failed_calls[0]["outcome"] == "failed"
    assert failed_calls[0]["error_summary"]


async def test_agent_session_end_reports_unknown_exception_as_failed_once():
    plugins, calls = _outcome_recorder()
    agent = _agent(FakeProvider(), plugins=plugins, executor=_ExplodingExecutor())

    with pytest.raises(RuntimeError, match="unexpected executor failure"):
        _ = [chunk async for chunk in agent.run(Envelope.of("boom", session_id="exception"))]

    assert len(calls) == 1
    assert calls[0]["outcome"] == "failed"
    assert "RuntimeError" in calls[0]["error_summary"]


async def test_agent_session_end_reports_cancellation_as_interrupted_once():
    plugins, calls = _outcome_recorder()
    executor = _BlockingExecutor()
    agent = _agent(FakeProvider(), plugins=plugins, executor=executor)

    async def drain() -> None:
        _ = [chunk async for chunk in agent.run(Envelope.of("wait", session_id="cancelled"))]

    task = asyncio.create_task(drain())
    await executor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [
        {"session_id": "cancelled", "outcome": "interrupted", "error_summary": ""}
    ]


async def test_builtin_executor_rejects_length_truncated_tool_arguments():
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return tool_result(ok=True)

    registry = Registry()
    registry.register(
        name="file_write",
        toolset="file",
        schema={"name": "file_write", "parameters": {}},
        handler=handler,
        is_async=False,
    )
    provider = FakeProvider(
        script=[
            ChatResponse(
                tool_calls=[ToolCall(f"truncated-{index}", "file_write", {"_raw": '{"path":'})],
                finish_reason="length",
            )
            for index in range(5)
        ]
    )
    executor = BuiltinExecutor(provider, registry, PluginManager())
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="sys",
        messages=[Message.user("write")],
        query="write",
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]

    assert calls == 0
    assert chunks[-1].kind == "error"
    assert "TOOL_ARGUMENTS_INCOMPLETE" in chunks[-1].body["message"]
    assert sum(chunk.kind == "error" for chunk in chunks) == 1
    assert len(provider.stream_calls) == 5
    assert not any(message.role == "tool" for message in ctx.messages)
    assert not any(message.tool_calls for message in ctx.messages if message.role == "assistant")


async def test_builtin_executor_recovers_truncated_tool_arguments_with_escalated_limit():
    received = []

    def handler(arguments):
        received.append(arguments)
        return tool_result(ok=True)

    class _CapturingProvider(FakeProvider):
        def __init__(self):
            super().__init__(script=[
                ChatResponse(
                    tool_calls=[ToolCall("truncated", "file_write", {"_raw": '{"path":'})],
                    finish_reason="length",
                ),
                ChatResponse(
                    tool_calls=[ToolCall("complete", "file_write", {"path": "a.txt"})],
                    finish_reason="tool_calls",
                ),
                ChatResponse(text="done", finish_reason="stop"),
            ])
            self.max_token_values = []

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            self.max_token_values.append(max_tokens)
            async for chunk in super().stream_chat(messages, tools, max_tokens=max_tokens):
                yield chunk

    registry = Registry()
    registry.register(
        name="file_write",
        toolset="file",
        schema={"name": "file_write", "parameters": {}},
        handler=handler,
        is_async=False,
    )
    provider = _CapturingProvider()
    executor = BuiltinExecutor(provider, registry, PluginManager())
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="sys",
        messages=[Message.user("write")],
        query="write",
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]

    assert received == [{"path": "a.txt"}]
    assert provider.max_token_values == [None, 64_000, 64_000]
    assert not any(chunk.kind == "error" for chunk in chunks)
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == "done"


async def test_builtin_executor_injects_split_recovery_after_escalation_fails():
    class _CapturingProvider(FakeProvider):
        def __init__(self):
            super().__init__(script=[
                ChatResponse(
                    tool_calls=[ToolCall("truncated-1", "file_write", {"_raw": '{"path":'})],
                    finish_reason="length",
                ),
                ChatResponse(
                    tool_calls=[ToolCall("truncated-2", "file_write", {"_raw": '{"path":'})],
                    finish_reason="length",
                ),
                ChatResponse(text="recovered", finish_reason="stop"),
            ])
            self.max_token_values = []

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            self.max_token_values.append(max_tokens)
            async for chunk in super().stream_chat(messages, tools, max_tokens=max_tokens):
                yield chunk

    provider = _CapturingProvider()
    executor = BuiltinExecutor(provider, Registry(), PluginManager())
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="sys",
        messages=[Message.user("write")],
        query="write",
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]

    assert provider.max_token_values == [None, 64_000, None]
    assert any(
        message.is_meta and "拆成更小的步骤" in (message.content or "")
        for message in provider.stream_calls[-1]
    )
    assert not any(chunk.kind == "error" for chunk in chunks)
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == "recovered"


async def test_builtin_executor_allows_non_length_raw_tool_arguments():
    received = []

    def handler(arguments):
        received.append(arguments)
        return tool_result(ok=True)

    registry = Registry()
    registry.register(
        name="raw_echo",
        toolset="test",
        schema={"name": "raw_echo", "parameters": {}},
        handler=handler,
        is_async=False,
    )
    provider = FakeProvider(
        script=[
            ChatResponse(
                tool_calls=[ToolCall("raw", "raw_echo", {"_raw": "valid payload"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(text="done"),
        ]
    )
    executor = BuiltinExecutor(provider, registry, PluginManager())
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="sys",
        messages=[Message.user("echo")],
        query="echo",
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]

    assert received == [{"_raw": "valid payload"}]
    assert chunks[-1].kind == "final"


# ---------------------------------------------------------------------------
# 硬停（dispatcher.stop → task.cancel）仍落库
# ---------------------------------------------------------------------------
class _CancelMidExecutor(AgentExecutor):
    """模拟硬停：已产出 assistant+工具调用、但工具结果未回填即被取消。"""

    name = "cancelmid"

    async def execute(self, ctx: ExecutionContext):
        ctx.messages.append(
            Message.assistant("正在搜索", [ToolCall("c1", "web_search", {"q": "x"})])
        )
        yield ResponseChunk.delta(ctx.request_id, "正在搜索", 1)
        raise asyncio.CancelledError()


async def test_hard_cancel_still_persists_user_message():
    """硬停（CancelledError）时，本轮 user 消息必须已落库，否则刷新即丢、下一轮无上下文。"""
    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, executor=_CancelMidExecutor())
    with pytest.raises(asyncio.CancelledError):
        async for _ in agent.run(Envelope.of("帮我搜一下资料", session_id="sc1")):
            pass
    saved = store.load("sc1", owner_account_id="local")
    # 用户的搜索任务消息已持久化
    assert any(m.role == "user" and m.content == "帮我搜一下资料" for m in saved)
    # 悬空的 assistant.tool_calls（缺配对 tool 结果）已被清洗，不污染下一轮
    assert not any(m.role == "assistant" and m.tool_calls for m in saved)


def test_drop_dangling_tool_calls():
    """尾部缺配对 tool 结果的 assistant.tool_calls 被剥离；已配对的保留。"""
    drop = SingleAgent._drop_dangling_tool_calls
    # 悬空：assistant 带 tool_call 但无 tool 结果 → 整条丢弃
    assert drop([Message.assistant("", [ToolCall("c1", "t", {})])]) == []
    # 已配对：保留
    paired = [Message.assistant("", [ToolCall("c1", "t", {})]), Message.tool("c1", "ok")]
    assert drop(paired) == paired
    # 混合：前一组已配对保留，尾部悬空组剥离
    msgs = [
        Message.user("q"),
        Message.assistant("", [ToolCall("c1", "t", {})]),
        Message.tool("c1", "ok"),
        Message.assistant("", [ToolCall("c2", "t", {})]),
    ]
    assert drop(msgs) == msgs[:3]
    # ACP 外部工具调用结果以内嵌 ToolCall.result 形式保存，不再追加 Message.tool；
    # 这类完整展示记录不能当成 dangling tool_call 删除。
    acp_done = [Message.assistant("已处理", [ToolCall("c3", "external", {}, result="ok")])]
    assert drop(acp_done) == acp_done
    # 无工具的普通 assistant 不受影响
    plain = [Message.user("q"), Message.assistant("答")]
    assert drop(plain) == plain


# ---------------------------------------------------------------------------
# 上下文压缩
# ---------------------------------------------------------------------------
async def test_compactor_summarizes_old_and_keeps_recent_from_safe_boundary():
    # 构造一段超预算历史：6 轮 user/assistant
    msgs = []
    for i in range(6):
        msgs.append(Message.user("问题" * 500 + str(i)))
        msgs.append(Message.assistant("回答" * 500 + str(i)))
    compactor = ContextCompactor(
        FakeProvider(script=[ChatResponse(text="历史摘要：此前讨论了六个问题")]),
        token_budget=10, keep_recent=3,
    )
    assert estimate_tokens(msgs) > 10
    out = await compactor.maybe_compact(msgs)
    # 第一条应是摘要 system
    assert out[0].role == "system" and "历史摘要" in out[0].content
    # recent 段从安全边界开始（user 或 assistant），绝不以 tool 开头切断配对
    assert out[1].role in ("user", "assistant")
    # 压缩后更短
    assert len(out) < len(msgs)


async def test_compaction_does_not_destroy_persisted_history():
    """压缩只作用于发给 LLM 的视图，DB 里应保留完整原始历史 + 本轮新增。"""
    store = InMemorySessionStore()
    # 预置一段超预算的旧历史
    seed = []
    for i in range(6):
        seed.append(Message.user("问题" * 500 + str(i)))
        seed.append(Message.assistant("回答" * 500 + str(i)))
    store.save("sc", seed, owner_account_id="local")

    compactor = ContextCompactor(FakeProvider(), token_budget=10, keep_recent=3)
    agent = _agent(FakeProvider(), session_store=store, compactor=compactor)
    async for _ in agent.run(Envelope.of("新一轮提问", session_id="sc")):
        pass

    saved = store.load("sc", owner_account_id="local")
    # 原始 12 条全部保留（未被摘要覆盖），且新 user 消息在其后
    for original in seed:
        assert original in saved
    assert any(m.role == "user" and m.content == "新一轮提问" for m in saved)
    assert len(saved) > len(seed)


# ---------------------------------------------------------------------------
# overflow 后 compact-retry-once（投影前进才重试）
# ---------------------------------------------------------------------------
def _seed_long_store(n_pairs: int = 25) -> InMemorySessionStore:
    store = InMemorySessionStore()
    seed: list[Message] = []
    for i in range(n_pairs):
        seed.append(Message.user(f"第{i:03d}轮问题 " + "长文本占位" * 200))
        seed.append(Message.assistant(f"第{i:03d}轮回答 " + "长文本占位" * 200))
    store.save("ov", seed, owner_account_id="local")
    return store


def _always_overflow(provider: FakeProvider) -> dict[str, int]:
    """把 stream_chat 换成「每次必抛上下文溢出」，返回计数 dict。"""
    state = {"requests": 0}

    async def _overflow(messages, tools=None, **kwargs):  # noqa: ANN001
        state["requests"] += 1
        raise ProviderError("maximum context length exceeded", retryable=False)
        yield  # noqa: E501 -- 让本函数成为 async generator，异常在迭代时被分类

    provider.stream_chat = _overflow
    return state


class ShortSummaryProvider(FakeProvider):
    """chat 永远返回短摘要（不受 script 限制），stream 由调用方覆盖。"""

    async def chat(self, messages, tools=None, **kw):  # noqa: ANN001
        self.calls.append(list(messages))
        return ChatResponse(text="压缩摘要")


async def test_overflow_compacts_then_retries_once_then_errors():
    """overflow → 兜底压缩使投影前进 → 重试一次 → 再溢出则报错（不无限重试）。"""
    store = _seed_long_store()
    provider = ShortSummaryProvider()
    state = _always_overflow(provider)
    compactor = ContextCompactor(provider, token_budget=10, keep_recent=4)
    agent = _agent(provider, session_store=store, compactor=compactor)

    chunks = []
    async for ch in agent.run(Envelope.of("继续", session_id="ov")):
        chunks.append(ch)

    errors = [c for c in chunks if c.kind == "error"]
    assert errors and "上下文超长" in errors[-1].body.get("message", "")
    # 只重试一次：两次 LLM 请求（原始 + 重试），第三次溢出不再压缩重试
    assert state["requests"] == 2
    # 摘要调用 = 预检 maybe_compact 1 次 + overflow 兜底压缩 1 次
    assert len(provider.calls) == 2


async def test_overflow_no_progress_no_retry():
    """兜底压缩未使投影前进（压缩器关闭）→ 不重试、不再发请求，直接报错。"""
    store = _seed_long_store()
    provider = FakeProvider()
    state = _always_overflow(provider)
    compactor = ContextCompactor(provider, token_budget=10, keep_recent=4, enabled=False)
    agent = _agent(provider, session_store=store, compactor=compactor)

    chunks = []
    async for ch in agent.run(Envelope.of("继续", session_id="ov")):
        chunks.append(ch)

    errors = [c for c in chunks if c.kind == "error"]
    assert errors and "上下文超长" in errors[-1].body.get("message", "")
    assert state["requests"] == 1  # 投影未前进：没有重试请求


async def test_file_manifest_persisted_into_canonical_history():
    """文件清单作为 is_meta 持久会话信息写入 canonical：跨轮继承，压缩后仍回注视图。"""
    from crew.agent.compact.file_manifest import is_file_manifest_message

    store = InMemorySessionStore()
    seed = _history_with_file_ops_seed()
    store.save("fm", seed, owner_account_id="local")

    agent = _agent(FakeProvider(), session_store=store)
    async for _ in agent.run(Envelope.of("继续", session_id="fm")):
        pass

    saved = store.load("fm", owner_account_id="local")
    manifests = [m for m in saved if is_file_manifest_message(m)]
    assert len(manifests) == 1, "canonical 应持久化恰好一条文件清单"
    assert manifests[0].is_meta
    assert "/a.py" in (manifests[0].content or "")


def _history_with_file_ops_seed() -> list[Message]:
    return [
        Message.user("看下文件"),
        Message.assistant(
            "读取",
            tool_calls=[ToolCall(id="r1", name="file_read", arguments={"path": "/a.py"})],
        ),
        Message.tool("r1", "内容", name="file_read"),
    ]


# ---------------------------------------------------------------------------
# 会话标题
# ---------------------------------------------------------------------------
async def test_new_session_title_generated_and_readable():
    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, enable_title=True)
    async for _ in agent.run(Envelope.of("帮我查一下天气", session_id="st")):
        pass
    # 标题生成已后台异步化（不阻塞 final 帧）：等后台 task 跑完再断言
    if agent._title_tasks:
        await asyncio.gather(*agent._title_tasks)
    titles = {s["session_id"]: s["title"] for s in store.list_sessions(owner_account_id="local")}
    # 标题来自模型生成（FakeProvider 回声），非默认首条 user 截断
    assert titles["st"] and titles["st"] != "帮我查一下天气"


async def test_channel_session_title_generated():
    """渠道会话（agent:main:*）首轮结束后同样生成自动摘要标题。

    复现 bug：_session_needs_title 走 list_sessions 默认排除渠道会话，
    渠道会话永远拿不到摘要标题，侧栏一直显示占位「新对话」。
    """
    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, enable_title=True)
    sid = "agent:main:weixin:dm:u1"
    async for _ in agent.run(Envelope.of("帮我查一下天气", session_id=sid)):
        pass
    if agent._title_tasks:
        await asyncio.gather(*agent._title_tasks)
    titles = {
        s["session_id"]: s["title"]
        for s in store.list_sessions(owner_account_id="local", exclude_channel_sessions=False)
    }
    assert titles[sid] and titles[sid] != "帮我查一下天气"


async def test_title_generation_does_not_block_final():
    """标题生成后台化：final 不被阻塞，消费者随即关闭流也不能丢标题。

    复现线上 bug：原实现 SingleAgent.run 在 yield final 后同步 await 标题生成，
    minimax 非流式标题请求挂起时 final 帧被暂存但发不出去，前端卡运行中 ~2 分钟。
    """
    store = InMemorySessionStore()
    agent = _agent(
        FakeProvider(script=[ChatResponse(text="完成")]),
        session_store=store, enable_title=True,
    )
    release = asyncio.Event()
    import crew.agent.runtime as _rt
    real = _rt.generate_session_title

    async def _hanging_title(provider, messages):
        await release.wait()
        return "后台摘要标题"

    _rt.generate_session_title = _hanging_title
    try:
        final_kind = None
        stream = agent.run(Envelope.of("hi", session_id="tf"))
        async for ch in stream:
            final_kind = ch.kind
            if ch.kind == "final":
                break  # final 到达即退出，此时后台标题 task 仍挂起
        assert final_kind == "final"  # 标题挂起未阻塞 final
        await stream.aclose()
        assert len(agent._title_tasks) == 1
        await asyncio.sleep(0)  # 让后台 task 进入 monkeypatch 的挂起标题函数
    finally:
        release.set()
        if agent._title_tasks:
            await asyncio.gather(*agent._title_tasks, return_exceptions=True)
        _rt.generate_session_title = real
    titles = {row["session_id"]: row["title"] for row in store.list_sessions(owner_account_id="local")}
    assert titles["tf"] == "后台摘要标题"


async def test_manual_title_rename_not_overwritten_by_late_summary():
    """用户在后台摘要生成完成前重命名时，迟到摘要不得覆盖用户标题。"""
    store = InMemorySessionStore()
    agent = _agent(
        FakeProvider(script=[ChatResponse(text="完成")]),
        session_store=store,
        enable_title=True,
    )
    release = asyncio.Event()
    import crew.agent.runtime as _rt
    real = _rt.generate_session_title

    async def _slow_title(provider, messages):
        await release.wait()
        return "模型摘要标题"

    _rt.generate_session_title = _slow_title
    try:
        async for ch in agent.run(Envelope.of("帮我做一份PPT", session_id="manual-title")):
            pass
        store.set_title("manual-title", "用户手动标题", owner_account_id="local")
        store.mark_title_manual("manual-title", owner_account_id="local", manual=True)
        release.set()
        if agent._title_tasks:
            await asyncio.gather(*agent._title_tasks)
    finally:
        release.set()
        _rt.generate_session_title = real

    titles = {s["session_id"]: s["title"] for s in store.list_sessions(owner_account_id="local")}
    assert titles["manual-title"] == "用户手动标题"


async def test_generate_session_title_user_only_ignores_assistant():
    """user_only=True 时标题 prompt 不含 assistant 片段。"""
    from crew.agent.auxiliary import generate_session_title

    seen: list[list[Message]] = []
    max_token_values: list[int | None] = []

    class _CapturingProvider:
        async def stream_chat(self, messages, tools=None, *, max_tokens=None, **kwargs):
            seen.append(list(messages))
            max_token_values.append(max_tokens)
            yield StreamChunk(delta_text="问候")
            yield StreamChunk(delta_text="", done=True, finish_reason="stop")

    title = await generate_session_title(
        _CapturingProvider(),
        [Message.user("你好吗？"), Message.assistant("我很好")],
        user_only=True,
    )
    assert title == "问候"
    assert len(seen) == 1
    user_payload = seen[0][-1].content or ""
    assert "你好吗？" in user_payload
    assert "助手" not in user_payload
    assert max_token_values == [32]


async def test_title_task_deduplicated_while_inflight():
    """同一 session 的重复后台调度不得并发两次标题 LLM。"""
    import crew.agent.runtime as _rt

    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, enable_title=True)
    release = asyncio.Event()
    gen_calls = 0

    async def _slow_gen(provider, messages, *, user_only=False):
        nonlocal gen_calls
        gen_calls += 1
        await release.wait()
        return "并行标题"

    real = _rt.generate_session_title
    _rt.generate_session_title = _slow_gen
    try:
        agent._spawn_title_task("s1", "local", [Message.user("hi")], None)
        await asyncio.sleep(0)  # 让首个 task 进入 generate_session_title
        agent._spawn_title_task(
            "s1",
            "local",
            [Message.user("hi"), Message.assistant("ok")],
            None,
        )
        assert gen_calls == 1
        release.set()
        if agent._title_tasks:
            await asyncio.gather(*agent._title_tasks)
    finally:
        _rt.generate_session_title = real


async def test_cancel_title_task_supersedes_inflight_generation():
    """supersede：取消在途标题任务后，迟到的旧标题不得落库。"""
    import crew.agent.runtime as _rt

    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, enable_title=True)
    started = asyncio.Event()
    release = asyncio.Event()
    written: list[str] = []
    real_set_title = store.set_title

    def _spy_set_title(session_id, title, owner_account_id=""):
        written.append(title)
        return real_set_title(session_id, title, owner_account_id=owner_account_id)

    store.set_title = _spy_set_title

    async def _hanging(provider, messages, *, user_only=False):
        started.set()
        await release.wait()
        return "过期标题"

    real = _rt.generate_session_title
    _rt.generate_session_title = _hanging
    try:
        agent._spawn_title_task("s1", "local", [Message.user("hi")], None)
        await started.wait()
        assert ("local", "s1") in agent._title_inflight
        agent._cancel_title_task("s1", "local")
        release.set()
        if agent._title_tasks:
            await asyncio.gather(*agent._title_tasks, return_exceptions=True)
        assert written == []
    finally:
        release.set()
        _rt.generate_session_title = real


async def test_new_user_turn_supersedes_inflight_title_task():
    """新用户回合开始即在途标题生成被 abort；本回合的标题随后正常生成。"""
    import crew.agent.runtime as _rt

    store = InMemorySessionStore()
    agent = _agent(
        FakeProvider(script=[ChatResponse(text="完成"), ChatResponse(text="完成2")]),
        session_store=store,
        enable_title=True,
    )
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    written: list[str] = []
    real_set_title = store.set_title

    def _spy_set_title(session_id, title, owner_account_id=""):
        written.append(title)
        return real_set_title(session_id, title, owner_account_id=owner_account_id)

    store.set_title = _spy_set_title
    calls = {"n": 0}

    async def _hanging(provider, messages, *, user_only=False):
        calls["n"] += 1
        if calls["n"] == 1:
            first_started.set()
            await first_release.wait()
            return "过期标题"
        return "新标题"

    real = _rt.generate_session_title
    _rt.generate_session_title = _hanging
    try:
        async for _ch in agent.run(Envelope.of("问题一", session_id="sup")):
            pass
        await first_started.wait()
        assert ("local", "sup") in agent._title_inflight

        async def _drain(stream):
            async for _ in stream:
                pass

        turn2 = asyncio.ensure_future(_drain(agent.run(Envelope.of("问题二", session_id="sup"))))
        # run() 开头即 supersede：在途标题任务同步出表
        for _ in range(100):
            if ("local", "sup") not in agent._title_inflight:
                break
            await asyncio.sleep(0.01)
        assert ("local", "sup") not in agent._title_inflight
        first_release.set()
        await turn2
        if agent._title_tasks:
            await asyncio.gather(*agent._title_tasks, return_exceptions=True)
        assert "过期标题" not in written
        assert written == ["新标题"]
    finally:
        first_release.set()
        _rt.generate_session_title = real


async def test_title_spawn_scheduled_only_after_main_response(monkeypatch):
    """自动标题必须移出主推理窗口，并在主 final 后最多调度一次。"""
    store = InMemorySessionStore()
    agent = _agent(FakeProvider(), session_store=store, enable_title=True)
    events: list[str] = []
    real_spawn = SingleAgent._spawn_title_task

    def _spy(self, title_sid, owner, history, push_fn):
        events.append("title")
        return real_spawn(self, title_sid, owner, history, push_fn)

    monkeypatch.setattr(SingleAgent, "_spawn_title_task", _spy)
    async for chunk in agent.run(Envelope.of("帮我查天气", session_id="deferred-title")):
        if chunk.kind == "final":
            events.append("final")
    if agent._title_tasks:
        await asyncio.gather(*agent._title_tasks)

    assert events == ["final", "title"]


async def test_main_stream_gets_provider_before_title_request():
    """容量受限的同一 Provider 中，标题请求不得抢在主流式请求前。"""

    class _OrderedProvider:
        def __init__(self) -> None:
            self.call_order: list[str] = []

        async def stream_chat(self, messages, tools=None, *, max_tokens=None, **kwargs):
            is_title = any(
                "不超过 12 个字" in (m.content or "")
                for m in messages
                if m.role == "system"
            )
            self.call_order.append("title" if is_title else "main")
            await asyncio.sleep(0.01)
            yield StreamChunk(delta_text="完成")
            self.call_order.append("title_done" if is_title else "main_done")
            yield StreamChunk(done=True, finish_reason="stop")

    provider = _OrderedProvider()
    agent = _agent(provider, session_store=InMemorySessionStore(), enable_title=True)

    async for _ in agent.run(Envelope.of("帮我查天气", session_id="provider-order")):
        pass
    if agent._title_tasks:
        await asyncio.gather(*agent._title_tasks)

    assert provider.call_order == ["main", "main_done", "title", "title_done"]


async def test_title_timeout_falls_back_to_first_query():
    """标题生成超时/失败：兜底用首条 user query 截断，不留空标题、不挂起。"""
    import crew.agent.auxiliary as aux
    from crew.agent.auxiliary import generate_session_title

    real_timeout = aux._TITLE_TIMEOUT
    aux._TITLE_TIMEOUT = 0.05  # 缩短超时，快速验证

    class _HangingProvider:
        async def stream_chat(self, messages, tools=None, *, max_tokens=None, **kwargs):
            await asyncio.Event().wait()  # 永不返回，模拟网关流式挂起
            yield  # 让本函数保持 async generator 形态

    try:
        title = await generate_session_title(
            _HangingProvider(),
            [Message.user("帮我做一份PPT"), Message.assistant("好的")],
        )
    finally:
        aux._TITLE_TIMEOUT = real_timeout
    assert title == "帮我做一份PPT"  # 超时兜底：首条 user query


async def test_cron_fired_turn_gets_scheduled_task_framing():
    # 定时任务触发轮：reminder 应明确"此刻在执行定时任务"+ 带任务名 + 当前时间，
    # 避免 agent 把注入的 query 当成用户提前发来的消息而反问"是否到时间"。
    from crew.cron import contribute_cron_trigger_reminder
    from crew.features import (
        ContextContributor,
        ContextContributorRegistry,
        ContextPhase,
        FeatureGeneration,
        FeatureScope,
    )

    context_registry = ContextContributorRegistry()
    scope = FeatureScope(FeatureGeneration("cron-context-test", 1))
    context_registry.register(
        scope,
        ContextContributor(
            "cron.trigger.reminder",
            contribute_cron_trigger_reminder,
            phase=ContextPhase.PROMPT,
        ),
    )
    scope.activate()
    agent = _agent(
        FakeProvider(script=[]),
        context_contributors=context_registry,
    )
    cron_env = Envelope.of("詹姆斯goat", session_id="s1", channel="cron")
    cron_env.params["cron_job_name"] = "詹姆斯GOAT提醒"

    await agent._contribute_prompt_context(cron_env)
    _static, reminder = await agent._build_prompts(cron_env, [])

    assert "定时任务触发" in reminder
    assert "詹姆斯GOAT提醒" in reminder
    assert "不要询问是否到时间" in reminder
    assert "当前时间：" in reminder            # 触发轮能看到当前时刻（不只是日期）

    # 普通渠道不注入该框架
    normal_env = Envelope.of("你好", session_id="s2", channel="web")
    await agent._contribute_prompt_context(normal_env)
    _s2, normal_reminder = await agent._build_prompts(normal_env, [])
    assert "定时任务触发" not in normal_reminder
    await scope.dispose()


async def test_revision_intent_gets_hidden_turn_framing():
    """队列项被提升为修订式中断后，用户原文保持正式消息，额外语义只进 reminder。"""
    agent = _agent(FakeProvider(script=[]))
    env = Envelope.of("补充 Kimi 和 GLM", session_id="s-revision", channel="web")
    env.params["client_intent"] = "revision"

    _static, reminder = await agent._build_prompts(env, [])

    assert "修订式中断" in reminder
    assert "上一条回复" in reminder
    assert "最终答案" in reminder


async def test_feature_context_fragments_are_dynamic_not_static():
    """Feature runtime context stays out of the prefix-stable system prompt."""
    agent = _agent(FakeProvider(script=[]))
    env = Envelope.of("hello", session_id="s-context", channel="web")
    env.params["_context_prompt_parts"] = ["## 当前会话上下文\n**来源:** Web"]

    system_static, reminder = await agent._build_prompts(env, [])

    assert "当前会话上下文" not in system_static
    assert "当前会话上下文" in reminder


async def test_read_attachment_rejects_oversized_file(tmp_path, monkeypatch):
    """附件读取对超大文件应读前拒绝，而非整读进内存。"""
    import crew.agent.runtime as rt

    big = tmp_path / "big.md"
    big.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(rt, "MAX_READ_FILE_BYTES", 10)

    result = await rt._read_attachment(str(big))

    assert "文件过大" in result


async def test_read_attachment_reads_small_file(tmp_path):
    """小附件正常读取（路径经 resolve 后读取，避免符号链接组件报错）。"""
    import crew.agent.runtime as rt

    small = tmp_path / "small.md"
    small.write_text("hello attachment", encoding="utf-8")

    result = await rt._read_attachment(str(small))

    assert "hello attachment" in result
