"""B4：网关 agent:end / idle 结算语义测试。

固化原则：
  1. 决策链必须 await 完成才算回合封口 —— agent:end 监听者全部执行完毕后，
     排队中的下一条消息才会出队执行（dispatcher 在 running 计数清理、状态
     落库之后才 await emit agent:end，并在 emit 完成后才释放会话锁）。
  2. 决策监听者观察到的是终态 —— agent:end 触发时 status 已落库为 completed、
     会话 live 已为 idle、running_depth 为 0。
  3. 观察类监听者错误隔离 —— 单个 handler 异常不影响其余 handler、
     不影响回合收尾与后续排队消息。
"""

from __future__ import annotations

import asyncio

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.features.hooks import hook_registry
from crew.gateway.dispatcher import SessionDispatcher

OWNER = "A:uid-b4"
SID = "sess-b4"


class _RecordingStore:
    """最小 SessionStore：记录 set_status 调用序列。"""

    def __init__(self):
        self.states: list[tuple[str, str]] = []

    def get_workspace_id(self, session_id, owner_account_id=""):
        return None

    def set_status(self, session_id, status, error="", owner_account_id=""):
        self.states.append((status, error))

    def get_status(self, session_id, owner_account_id=""):
        return (self.states[-1][0], self.states[-1][1]) if self.states else ("idle", "")


def _make_inner(order: list[str], started: asyncio.Event | None = None, started_query: str = ""):
    async def inner(envelope):
        order.append(f"inner_start:{envelope.query}")
        if started is not None and envelope.query == started_query:
            started.set()
        yield ResponseChunk.final(envelope.request_id, "ok")
    return inner


async def _drain(iterator):
    async for _ in iterator:
        pass


@pytest.mark.asyncio
async def test_agent_end_hook_observes_settled_terminal_state():
    """agent:end 触发时：状态已落库 completed、live 已为 idle、running_depth 为 0。"""
    store = _RecordingStore()
    order: list[str] = []
    disp = SessionDispatcher(_make_inner(order), store)
    snapshots: list[tuple[int, str, str]] = []

    async def spy(event_type, context):
        snapshots.append((
            context.get("running_depth"),
            disp.status(SID, owner_account_id=OWNER)["live"],
            store.states[-1][0] if store.states else "",
        ))

    hook_registry.register("agent:end", spy)
    try:
        async for _ in disp.run(Envelope.of("hi", session_id=SID, channel="test", user_id=OWNER)):
            pass
        assert snapshots == [(0, "idle", "completed")]
    finally:
        hook_registry.unregister("agent:end", spy)
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_agent_end_decision_hook_gates_queued_turn():
    """慢的决策监听者会拦住排队消息：agent:end 未完成前，同会话下一条不开始执行。"""
    store = _RecordingStore()
    order: list[str] = []
    hook_entered = asyncio.Event()
    hook_release = asyncio.Event()
    second_started = asyncio.Event()
    disp = SessionDispatcher(
        _make_inner(order, started=second_started, started_query="第二条"),
        store,
    )

    async def slow_decision_hook(event_type, context):
        if context.get("message") != "第一条":
            return
        hook_entered.set()
        await hook_release.wait()
        order.append("agent_end_done")

    hook_registry.register("agent:end", slow_decision_hook)
    try:
        first = asyncio.create_task(_drain(disp.run(
            Envelope.of("第一条", session_id=SID, channel="test", user_id=OWNER))))
        await asyncio.wait_for(hook_entered.wait(), timeout=1.0)

        # 决策链未走完：同会话下一条消息已排队但绝不能出队执行
        second = asyncio.create_task(_drain(disp.run(
            Envelope.of("第二条", session_id=SID, channel="test", user_id=OWNER))))
        await asyncio.sleep(0.05)
        assert not second_started.is_set()
        assert order == ["inner_start:第一条"]

        hook_release.set()
        await asyncio.gather(first, second)
        # 决策链完成后排队消息才执行
        assert order == ["inner_start:第一条", "agent_end_done", "inner_start:第二条"]
    finally:
        hook_registry.unregister("agent:end", slow_decision_hook)
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_agent_end_observer_failure_does_not_break_settlement():
    """单个监听者异常被隔离：其余监听者照常执行，回合收尾与排队消息不受影响。"""
    store = _RecordingStore()
    order: list[str] = []
    observed: list[str] = []
    disp = SessionDispatcher(_make_inner(order), store)

    async def failing_observer(event_type, context):
        raise RuntimeError("intentional observer failure")

    async def healthy_observer(event_type, context):
        observed.append(context.get("session_id"))

    hook_registry.register("agent:end", failing_observer)
    hook_registry.register("agent:end", healthy_observer)
    try:
        chunks = []
        async for ch in disp.run(Envelope.of("hi", session_id=SID, channel="test", user_id=OWNER)):
            chunks.append(ch)
        assert any(ch.kind == "final" for ch in chunks)
        assert observed == [SID]

        async for _ in disp.run(Envelope.of("再来", session_id=SID, channel="test", user_id=OWNER)):
            pass
        assert order == ["inner_start:hi", "inner_start:再来"]
        assert store.states[-1][0] == "completed"
    finally:
        hook_registry.unregister("agent:end", failing_observer)
        hook_registry.unregister("agent:end", healthy_observer)
        hook_registry.unregister("session:end", disp._on_session_end)
