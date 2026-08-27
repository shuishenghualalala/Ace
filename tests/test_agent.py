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
from crew.agent.runtime import SingleAgent
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ConfigError, ProviderError
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
async def test_compactor_summarizes_old_and_keeps_recent_from_user_boundary():
    # 构造一段超预算历史：5 轮 user/assistant + 一组 tool 配对
    msgs = []
    for i in range(6):
        msgs.append(Message.user("问题" * 500 + str(i)))
        msgs.append(Message.assistant("回答" * 500 + str(i)))
    compactor = ContextCompactor(FakeProvider(), token_budget=10, keep_recent=3)
    assert estimate_tokens(msgs) > 10
    out = await compactor.maybe_compact(msgs)
    # 第一条应是摘要 system
    assert out[0].role == "system" and "历史摘要" in out[0].content
    # recent 段从 user 边界开始
    assert out[1].role == "user"
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
        async def chat(self, messages, tools=None, *, max_tokens=None):
            seen.append(list(messages))
            max_token_values.append(max_tokens)
            return ChatResponse(text="问候")

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

        async def chat(self, messages, tools=None, *, max_tokens=None):
            self.call_order.append("title")
            return ChatResponse(text="天气查询")

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            self.call_order.append("main")
            await asyncio.sleep(0.01)
            yield StreamChunk(delta_text="完成")
            self.call_order.append("main_done")
            yield StreamChunk(done=True, finish_reason="stop")

    provider = _OrderedProvider()
    agent = _agent(provider, session_store=InMemorySessionStore(), enable_title=True)

    async for _ in agent.run(Envelope.of("帮我查天气", session_id="provider-order")):
        pass
    if agent._title_tasks:
        await asyncio.gather(*agent._title_tasks)

    assert provider.call_order == ["main", "main_done", "title"]


async def test_title_timeout_falls_back_to_first_query():
    """标题生成超时/失败：兜底用首条 user query 截断，不留空标题、不挂起。"""
    import crew.agent.auxiliary as aux
    from crew.agent.auxiliary import generate_session_title

    real_timeout = aux._TITLE_TIMEOUT
    aux._TITLE_TIMEOUT = 0.05  # 缩短超时，快速验证

    class _HangingProvider:
        async def chat(self, messages, tools=None, *, max_tokens=None):
            await asyncio.Event().wait()  # 永不返回，模拟 minimax 非流式挂起

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
    agent = _agent(FakeProvider(script=[]))
    cron_env = Envelope.of("詹姆斯goat", session_id="s1", channel="cron")
    cron_env.params["cron_job_name"] = "詹姆斯GOAT提醒"

    _static, reminder = await agent._build_prompts(cron_env, [])

    assert "定时任务触发" in reminder
    assert "詹姆斯GOAT提醒" in reminder
    assert "不要询问是否到时间" in reminder
    assert "当前时间：" in reminder            # 触发轮能看到当前时刻（不只是日期）

    # 普通渠道不注入该框架
    _s2, normal_reminder = await agent._build_prompts(
        Envelope.of("你好", session_id="s2", channel="web"), []
    )
    assert "定时任务触发" not in normal_reminder


async def test_revision_intent_gets_hidden_turn_framing():
    """队列项被提升为修订式中断后，用户原文保持正式消息，额外语义只进 reminder。"""
    agent = _agent(FakeProvider(script=[]))
    env = Envelope.of("补充 Kimi 和 GLM", session_id="s-revision", channel="web")
    env.params["client_intent"] = "revision"

    _static, reminder = await agent._build_prompts(env, [])

    assert "修订式中断" in reminder
    assert "上一条回复" in reminder
    assert "最终答案" in reminder
