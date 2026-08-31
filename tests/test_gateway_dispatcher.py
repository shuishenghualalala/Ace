"""Gateway 会话调度器 + 连接管理器测试。

覆盖：
  - 基本串行化与排队
  - 不同会话并发
  - 失败持久化
  - 忙时策略（interrupt / queue / steer）
  - 推送限流
  - 连接管理（注册/注销/死连接移除）
"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.interfaces import Channel, MessageHandler
from crew.core.mocks import InMemorySessionStore
from crew.core.runctx import current_owner_account_id
from crew.core.types import Message
from crew.gateway.channel_manager import ChannelManager
from crew.gateway.connections import ConnectionManager, _MAX_CONSECUTIVE_FAILURES
from crew.gateway.delivery import DeliveryRouter
from crew.gateway.dispatcher import BusyMode, SessionDispatcher
from crew.gateway.hooks import hook_registry

OWNER = "local"


def _env(sid: str, q: str = "hi") -> Envelope:
    return Envelope.of(q, session_id=sid, user_id=OWNER)


async def _drain(gen) -> list[ResponseChunk]:
    return [ch async for ch in gen]


class _DummyTaskRuntime:
    """满足 SessionDispatcher sidechain 流程的最小 task_runtime 桩。"""

    def __init__(self, output_root: Any, *, backgrounded: bool = False) -> None:
        self._output_root = output_root
        self._backgrounded = backgrounded
        self._tasks: dict[str, dict[str, Any]] = {}

    def create_runtime(self, **kwargs: Any) -> dict[str, Any]:
        task_id = f"task_{kwargs.get('request_id', 'r')}"
        self._tasks[task_id] = {
            "backgrounded": self._backgrounded,
            "output_ref": str(self._output_root / f"{task_id}.json"),
            "status": "running",
        }
        return {"task_id": task_id}

    def update(self, task_id: str, **kwargs: Any) -> None:
        self._tasks.setdefault(task_id, {}).update(kwargs)

    def get(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._tasks.get(task_id, {"backgrounded": False, "output_ref": "", "status": "running"})

    def mark_running(self, task_id: str) -> None:
        self._tasks.setdefault(task_id, {})["status"] = "running"

    def touch_activity(self, task_id: str, progress: Any = None, **kwargs: Any) -> None:
        pass

    def attach_worker(self, task_id: str, task: Any, *, cancel: Any) -> None:
        pass

    def finish(self, task_id: str, **kwargs: Any) -> None:
        self._tasks.setdefault(task_id, {})["status"] = kwargs.get("status", "completed")


# ---------------------------------------------------------------------------
# SessionDispatcher 基础测试
# ---------------------------------------------------------------------------


async def test_dispatcher_scopes_owner_context_and_resets_after_run():
    seen: list[str] = []

    async def inner(env):
        seen.append(current_owner_account_id.get())
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, InMemorySessionStore())
    assert current_owner_account_id.get() == ""

    await _drain(disp.run(_env("causal-owner")))

    assert seen == [OWNER]
    assert current_owner_account_id.get() == ""


async def test_same_session_serialized_and_queued():
    store = InMemorySessionStore()
    gate = asyncio.Event()
    started: list[str] = []

    async def inner(env):
        started.append(env.session_id)
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store)

    t1 = asyncio.create_task(_drain(disp.run(_env("s1"))))
    await asyncio.sleep(0.02)  # 让 t1 进入 running 并停在 gate
    assert disp.status("s1", owner_account_id=OWNER)["live"] == "running"
    assert disp.status("s1", owner_account_id=OWNER)["queue_depth"] == 0

    t2 = asyncio.create_task(_drain(disp.run(_env("s1"))))
    await asyncio.sleep(0.02)  # t2 排队中
    assert disp.status("s1", owner_account_id=OWNER)["queue_depth"] == 1
    assert started == ["s1"]  # 第二条尚未开始（串行）

    gate.set()
    r1 = await t1
    r2 = await t2
    assert started == ["s1", "s1"]  # 串行先后执行
    assert r2[0].kind == "status"  # 第二条先收到「排队中」提示帧
    assert any(c.kind == "final" for c in r1) and any(c.kind == "final" for c in r2)
    # 结束后空闲并回收
    assert disp.status("s1", owner_account_id=OWNER)["live"] == "idle"
    assert disp.status("s1", owner_account_id=OWNER)["queue_depth"] == 0


async def test_terminal_chunk_waits_until_session_lock_is_released():
    store = InMemorySessionStore()
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        yield ResponseChunk.final(env.request_id, "done")

    async def on_agent_end(event_type, context):
        if context.get("session_id") == "s-final-lock":
            hook_started.set()
            await release_hook.wait()

    hook_registry.register("agent:end", on_agent_end)
    disp = SessionDispatcher(inner, store)
    first_task = None
    second_task = None
    try:
        first_task = asyncio.create_task(_drain(disp.run(_env("s-final-lock"))))
        await asyncio.wait_for(hook_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not first_task.done()
        # running 状态在 agent:end 前清理，供结束钩子安全提升会话模型；
        # 但 session lock 仍应持有，下一轮必须排队，terminal 也尚未交付。
        assert disp.status("s-final-lock", owner_account_id=OWNER)["live"] == "idle"

        second_task = asyncio.create_task(_drain(disp.run(_env("s-final-lock", q="again"))))
        await asyncio.sleep(0.02)
        assert disp.status("s-final-lock", owner_account_id=OWNER)["queue_depth"] == 1

        release_hook.set()
        first_chunks = await asyncio.wait_for(first_task, timeout=1)
        second_chunks = await asyncio.wait_for(second_task, timeout=1)
        assert first_chunks[-1].kind == "final"
        assert second_chunks[-1].kind == "final"
        assert disp.status("s-final-lock", owner_account_id=OWNER)["live"] == "idle"
        assert second_chunks[0].kind == "status"
        assert "排队" in str(second_chunks[0].body.get("message", ""))
    finally:
        release_hook.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        hook_registry.unregister("agent:end", on_agent_end)
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_different_sessions_run_concurrently():
    store = InMemorySessionStore()
    gate = asyncio.Event()
    running: set[str] = set()

    async def inner(env):
        running.add(env.session_id)
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store)
    t1 = asyncio.create_task(_drain(disp.run(_env("a"))))
    t2 = asyncio.create_task(_drain(disp.run(_env("b"))))
    await asyncio.sleep(0.02)
    assert running == {"a", "b"}  # 不同会话互不阻塞，同时运行
    gate.set()
    await t1
    await t2


async def test_same_session_id_different_owners_run_concurrently():
    store = InMemorySessionStore()
    gate = asyncio.Event()
    running: list[str] = []

    async def inner(env):
        running.append(env.user_id)
        yield ResponseChunk.delta(env.request_id, env.user_id)
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store)
    t1 = asyncio.create_task(_drain(disp.run(Envelope.of("a", session_id="same", user_id="A:uid-a"))))
    t2 = asyncio.create_task(_drain(disp.run(Envelope.of("b", session_id="same", user_id="B:uid-b"))))
    await asyncio.sleep(0.02)

    assert set(running) == {"A:uid-a", "B:uid-b"}
    assert disp.status("same", owner_account_id="A:uid-a")["live"] == "running"
    assert disp.status("same", owner_account_id="B:uid-b")["live"] == "running"
    assert disp.status("same", owner_account_id="A:uid-a")["queue_depth"] == 0
    assert disp.status("same", owner_account_id="B:uid-b")["queue_depth"] == 0

    gate.set()
    await t1
    await t2


async def test_global_active_run_limit_across_sessions():
    """全局 max_active_runs 限制不同 session 的同时运行数量。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()
    started: list[str] = []

    async def inner(env):
        started.append(env.session_id)
        yield ResponseChunk.delta(env.request_id, env.session_id)
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store, max_active_runs=1)
    t1 = asyncio.create_task(_drain(disp.run(_env("a"))))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_drain(disp.run(_env("b"))))
    await asyncio.sleep(0.05)

    assert started == ["a"]
    assert disp.status("a", owner_account_id=OWNER)["global_active"] == 1
    # b 已出队、正在等全局并发槽：对外的 live 是 running（已受理），不是 queued；
    # 等槽状态经 waiting_for_global_slot 单独暴露。
    assert disp.status("b", owner_account_id=OWNER)["live"] == "running"
    assert disp.status("b", owner_account_id=OWNER)["queue_depth"] == 0
    assert disp.status("b", owner_account_id=OWNER)["waiting_for_global_slot"] == 1
    assert disp.runtime_status()["global_queued"] == 1

    gate.set()
    await t1
    await t2
    assert started == ["a", "b"]


async def test_failure_via_exception_persisted():
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        raise RuntimeError("boom")

    disp = SessionDispatcher(inner, store)
    out = await _drain(disp.run(_env("s1")))
    assert out[-1].kind == "error"  # 兜底产出 error 帧
    last_status, last_error = store.get_status("s1", owner_account_id="local")
    assert last_status == "failed" and "boom" in last_error


async def test_failure_via_error_chunk_persisted():
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.error(env.request_id, "bad input")

    disp = SessionDispatcher(inner, store)
    await _drain(disp.run(_env("s1")))
    assert store.get_status("s1", owner_account_id="local") == ("failed", "bad input")


async def test_success_persisted_and_status_merge():
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store)
    await _drain(disp.run(_env("s1")))
    assert store.get_status("s1", owner_account_id="local")[0] == "completed"
    st = disp.status("s1", owner_account_id="local")
    assert st["live"] == "idle"
    assert st["queue_depth"] == 0
    assert st["last_status"] == "completed"


# ---------------------------------------------------------------------------
# 忙时策略测试
# ---------------------------------------------------------------------------


async def test_interrupt_mode_cancels_running():
    """interrupt 模式：新消息取消当前运行，新消息在锁释放后执行。"""
    store = InMemorySessionStore()
    started: list[str] = []

    async def inner(env):
        started.append(env.query)
        # 第一条消息会阻塞很久，第二条不会被阻塞
        if env.query == "first":
            await asyncio.sleep(10.0)  # 长时间运行
        yield ResponseChunk.final(env.request_id, f"done:{env.query}")

    disp = SessionDispatcher(inner, store, busy_mode=BusyMode.INTERRUPT)

    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)
    assert disp.status("s1", owner_account_id=OWNER)["live"] == "running"

    # 发送第二条消息，应触发 interrupt 取消第一条
    t2 = asyncio.create_task(_drain(disp.run(_env("s1", "second"))))
    await asyncio.sleep(0.1)

    # 第二条应该能执行完成（第一条被取消后锁释放）
    r2 = await asyncio.wait_for(t2, timeout=2.0)
    assert any(c.kind == "final" for c in r2)

    # 第一条被中断
    r1 = await asyncio.wait_for(t1, timeout=2.0)
    assert any(c.kind == "error" for c in r1)


async def test_steer_mode_injects_text():
    """steer 模式：调用 steer() 注入补充指令。"""
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store, busy_mode=BusyMode.STEER)

    # 没有运行中的 agent，steer 返回 False
    assert not disp.steer("s1", "补充指令", owner_account_id=OWNER)

    # 运行中但没有 steer 方法，steer 缓存文本
    gate = asyncio.Event()

    async def slow_inner(env):
        yield ResponseChunk.delta(env.request_id, "thinking")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp2 = SessionDispatcher(slow_inner, store, busy_mode=BusyMode.STEER)
    t1 = asyncio.create_task(_drain(disp2.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)

    # steer 到不支持 steer 的 inner → 缓存文本
    steered = disp2.steer("s1", "补充指令", owner_account_id=OWNER)
    # inner 不支持 steer()，但文本被缓存
    assert steered  # 返回 True 因为文本已缓存

    gate.set()
    await t1


async def test_steer_and_interrupt_route_to_controller():
    """有 controller（CrewApp）时：steer/interrupt 路由到它，而非缓存/硬取消。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()

    async def slow_inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    class Ctrl:
        def __init__(self):
            self.steers: list[tuple[str, str]] = []
            self.interrupts: list[str] = []

        def steer(self, sid, text):
            self.steers.append((sid, text))
            return True

        def interrupt(self, sid, message=None):
            self.interrupts.append(sid)
            return True

    ctrl = Ctrl()
    disp = SessionDispatcher(slow_inner, store, busy_mode=BusyMode.QUEUE, controller=ctrl)
    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)

    # steer → controller.steer（实时注入），不进缓存
    assert disp.steer("s1", "用中文回答", owner_account_id=OWNER) is True
    assert ctrl.steers == [("s1", "用中文回答")]
    assert disp._steer_texts.get("s1") is None

    # interrupt → controller.interrupt（协作式），不硬取消 task
    assert disp.interrupt("s1", owner_account_id=OWNER) is True
    assert ctrl.interrupts == ["s1"]

    gate.set()
    await asyncio.wait_for(t1, timeout=2.0)


async def test_steer_routes_to_sidechain_agent_when_task_runtime_enabled(tmp_path):
    """普通 agent turn 走 sidechain 时，steer 应命中实际运行的 sidechain Agent。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()
    exec_sessions: list[str] = []

    async def slow_inner(env):
        exec_sessions.append(env.session_id)
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    class Ctrl:
        def __init__(self):
            self.steers: list[tuple[str, str, str]] = []

        def steer(self, sid, text, owner_account_id=""):
            self.steers.append((sid, text, owner_account_id))
            return "::turn::" in sid

    ctrl = Ctrl()
    disp = SessionDispatcher(
        slow_inner,
        store,
        controller=ctrl,
        task_runtime=_DummyTaskRuntime(tmp_path),
    )
    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)

    assert exec_sessions and exec_sessions[0].startswith("s1::turn::")
    assert disp.steer("s1", "用更谨慎的方向", owner_account_id=OWNER) is True
    assert ctrl.steers == [(exec_sessions[0], "用更谨慎的方向", OWNER)]
    assert (OWNER, "s1") not in disp._steer_texts

    gate.set()
    await asyncio.wait_for(t1, timeout=2.0)


# ---------------------------------------------------------------------------
# ChannelManager 平台生命周期测试
# ---------------------------------------------------------------------------


async def test_channel_manager_lifecycle_dispatches_through_session_dispatcher():
    store = InMemorySessionStore()
    seen: list[Envelope] = []

    async def inner(env):
        seen.append(env)
        yield ResponseChunk.final(env.request_id, f"handled:{env.query}")

    dispatcher = SessionDispatcher(inner, store)

    class FakeChannel(Channel):
        name = "fake"

        def __init__(self):
            self.stopped = False
            self.chunks: list[ResponseChunk] = []

        async def start(self, handler: MessageHandler) -> None:
            env = Envelope.of("hello", session_id="fake:u1", channel="fake")
            self.chunks = [chunk async for chunk in handler(env)]

        async def stop(self) -> None:
            self.stopped = True

    channel = FakeChannel()
    manager = ChannelManager()
    manager.register(channel)

    await manager.start_all(dispatcher.run)

    assert manager.status() == [
        {
            "name": "fake",
            "owner_account_id": "",
            "running": True,
            "error": "",
            "operation": "",
            "reason": "",
        }
    ]
    assert seen[0].channel == "fake"
    assert seen[0].session_id == "fake:u1"
    assert channel.chunks[-1].body["text"] == "handled:hello"
    assert store.get_status("fake:u1", owner_account_id="local")[0] == "completed"

    await manager.stop_all()
    assert channel.stopped
    assert manager.status() == [
        {
            "name": "fake",
            "owner_account_id": "",
            "running": False,
            "error": "",
            "operation": "",
            "reason": "disconnected",
        }
    ]


async def test_channel_manager_starts_only_active_owner_channels():
    started: list[str] = []

    class OwnedChannel(Channel):
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self, handler: MessageHandler) -> None:
            started.append(self.name)

    async def handler(_envelope):
        if False:
            yield

    manager = ChannelManager()
    manager.register(OwnedChannel("owner-a"), owner_account_id="A:uid-a")
    manager.register(OwnedChannel("owner-b"), owner_account_id="B:uid-b")
    manager.register(OwnedChannel("global"))

    await manager.start_all(handler)
    assert started == ["global"]
    started.clear()

    await manager.start_all(handler, owner_account_id="B:uid-b")

    assert started == ["owner-b"]


async def test_channel_manager_stops_one_owner_without_affecting_another():
    stopped: list[str] = []

    class OwnedChannel(Channel):
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self, _handler: MessageHandler) -> None:
            return None

        async def stop(self) -> None:
            stopped.append(self.name)

    async def handler(_envelope):
        if False:
            yield

    manager = ChannelManager()
    manager.register(OwnedChannel("shared"), owner_account_id="A:uid-a")
    manager.register(OwnedChannel("shared"), owner_account_id="B:uid-b")
    await manager.start_all(handler, owner_account_id="A:uid-a")
    await manager.start_all(handler, owner_account_id="B:uid-b")

    assert {row["owner_account_id"] for row in manager.status("B:uid-b")} == {"B:uid-b"}
    assert await manager.stop_owner("A:uid-a", reason="login_required") == []
    assert manager.get("shared", "A:uid-a") is not None
    assert manager.status("A:uid-a")[0]["running"] is False
    assert manager.status("B:uid-b")[0]["running"] is True
    assert stopped == ["shared"]


async def test_delivery_router_keeps_same_platform_senders_owner_scoped():
    sent: list[tuple[str, str]] = []

    async def sender_a(chat_id: str, text: str, _origin) -> bool:
        sent.append(("A", f"{chat_id}:{text}"))
        return True

    async def sender_b(chat_id: str, text: str, _origin) -> bool:
        sent.append(("B", f"{chat_id}:{text}"))
        return True

    router = DeliveryRouter()
    router.register("feishu", sender_a, owner_account_id="A:uid-a")
    router.register("feishu", sender_b, owner_account_id="B:uid-b")

    assert (await router.deliver("feishu:chat-a", "from-a", owner_account_id="A:uid-a"))["ok"]
    assert (await router.deliver("feishu:chat-b", "from-b", owner_account_id="B:uid-b"))["ok"]
    assert sent == [("A", "chat-a:from-a"), ("B", "chat-b:from-b")]


def test_channel_manager_status_includes_creation_errors():
    manager = ChannelManager()

    manager.record_error("feishu", "missing appSecret")

    assert manager.status() == [
        {
            "name": "feishu",
            "owner_account_id": "",
            "running": False,
            "error": "missing appSecret",
            "operation": "",
            "reason": "error",
        }
    ]


async def test_queue_mode_is_default():
    """默认 queue 模式：忙时排队。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()
    order: list[str] = []

    async def inner(env):
        order.append(env.query)
        yield ResponseChunk.delta(env.request_id, env.query)
        await gate.wait()
        yield ResponseChunk.final(env.request_id, env.query)

    disp = SessionDispatcher(inner, store)  # 默认 BusyMode.QUEUE
    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "a"))))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_drain(disp.run(_env("s1", "b"))))
    await asyncio.sleep(0.02)

    assert disp.status("s1", owner_account_id=OWNER)["queue_depth"] == 1
    gate.set()
    await t1
    await t2
    assert order == ["a", "b"]


async def test_queue_depth_limit_rejects_excess_messages():
    store = InMemorySessionStore()
    gate = asyncio.Event()

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store, max_queue_depth_per_session=1)
    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_drain(disp.run(_env("s1", "second"))))
    await asyncio.sleep(0.02)

    rejected = await _drain(disp.run(_env("s1", "third")))
    assert rejected[-1].kind == "error"
    assert "队列已满" in rejected[-1].body["message"]

    gate.set()
    await t1
    await t2


async def test_interrupt_demotes_to_queue_when_child_agent_active():
    store = InMemorySessionStore()
    gate = asyncio.Event()
    order: list[str] = []
    active = {"s1": [{"child_id": "child-1", "member": "coder"}]}

    async def inner(env):
        order.append(env.query)
        yield ResponseChunk.delta(env.request_id, env.query)
        await gate.wait()
        yield ResponseChunk.final(env.request_id, env.query)

    def active_children(session_id=None, *, owner_account_id=""):
        assert owner_account_id == OWNER
        if session_id is None:
            return active
        return active.get(session_id, [])

    disp = SessionDispatcher(
        inner,
        store,
        busy_mode=BusyMode.INTERRUPT,
        active_children_fn=active_children,
    )
    t1 = asyncio.create_task(_drain(disp.run(_env("s1", "first"))))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_drain(disp.run(_env("s1", "second"))))
    await asyncio.sleep(0.02)

    assert not t1.done()
    assert disp.status("s1", owner_account_id=OWNER)["queue_depth"] == 1
    assert order == ["first"]

    active.clear()
    gate.set()
    r2 = await t2
    await t1
    assert r2[0].kind == "status"
    assert "子 agent" in r2[0].body["message"]
    assert order == ["first", "second"]


async def test_stop_cancels_all_tasks():
    """stop() 取消同一 session 的所有 task。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store)
    t1 = asyncio.create_task(_drain(disp.run(_env("s1"))))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_drain(disp.run(_env("s1"))))
    await asyncio.sleep(0.02)

    stopped = disp.stop("s1", owner_account_id=OWNER)
    assert stopped

    gate.set()  # 解除阻塞（虽然已经被取消）
    # 两个 task 都应收到 error 帧
    for t in (t1, t2):
        result = await t
        assert any(c.kind == "error" for c in result)


async def test_stop_owner_cancels_only_owner_and_suppresses_logout_reply():
    """Owner logout drops every old-generation terminal frame without touching peers."""
    store = InMemorySessionStore()
    gates = {"A:uid-a": asyncio.Event(), "B:uid-b": asyncio.Event()}

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, f"started:{env.user_id}")
        await gates[env.user_id].wait()
        yield ResponseChunk.final(env.request_id, f"done:{env.user_id}")

    disp = SessionDispatcher(inner, store)
    task_a = asyncio.create_task(
        _drain(disp.run(Envelope.of("a", session_id="same", user_id="A:uid-a")))
    )
    task_b = asyncio.create_task(
        _drain(disp.run(Envelope.of("b", session_id="same", user_id="B:uid-b")))
    )
    await asyncio.sleep(0.02)

    stopped = await disp.stop_owner("A:uid-a", reason="已停止：账号退出登录")
    gates["B:uid-b"].set()

    assert stopped == 1
    result_a = await task_a
    assert result_a
    assert all(chunk.kind not in {"final", "error"} for chunk in result_a)
    result_b = await task_b
    assert result_b[-1].kind == "final"
    assert result_b[-1].body["text"] == "done:B:uid-b"


async def test_stop_reason_uses_owner_scoped_key():
    store = InMemorySessionStore()
    gate = asyncio.Event()

    async def inner(env):
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store)
    task = asyncio.create_task(_drain(disp.run(Envelope.of("a", session_id="same", user_id="A:uid-a"))))
    await asyncio.sleep(0.02)

    assert disp.stop("same", reason="owner scoped stop", owner_account_id="A:uid-a")
    gate.set()
    result = await task

    errors = [c for c in result if c.kind == "error"]
    assert errors
    assert errors[-1].body["message"] == "owner scoped stop"


# ---------------------------------------------------------------------------
# ConnectionManager 推送限流测试
# ---------------------------------------------------------------------------


def _fake_ws():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def _chunk(text: str = "hi") -> ResponseChunk:
    return ResponseChunk.delta("req1", text)


async def test_connections_push_to_registered():
    mgr = ConnectionManager()
    ws = _fake_ws()
    mgr.register("s1", ws)
    assert mgr.has_connection("s1")
    await mgr.push("s1", _chunk("hello"))
    ws.send_json.assert_awaited_once()
    payload = ws.send_json.call_args[0][0]
    assert payload["body"]["text"] == "hello"
    assert payload["session_id"] == "s1"  # 帧带 session_id，供前端按会话路由


async def test_connections_no_push_when_empty():
    mgr = ConnectionManager()
    ws = _fake_ws()
    # 未注册，push 不抛异常且 send_json 不被调用
    await mgr.push("s1", _chunk())
    ws.send_json.assert_not_awaited()
    assert mgr._consecutive_failures.get("s1", 0) == 0


async def test_connections_unregister_stops_push():
    mgr = ConnectionManager()
    ws = _fake_ws()
    mgr.register("s1", ws)
    mgr.unregister("s1", ws)
    assert not mgr.has_connection("s1")
    await mgr.push("s1", _chunk())
    ws.send_json.assert_not_awaited()


async def test_connections_unregister_all():
    mgr = ConnectionManager()
    ws = _fake_ws()
    registered = {"s1", "s2"}
    for sid in registered:
        mgr.register(sid, ws)
    mgr.unregister_all(ws, registered)
    assert not mgr.has_connection("s1")
    assert not mgr.has_connection("s2")


async def test_connections_dead_socket_removed_on_push():
    """push 时 send_json 抛异常 → 死连接被自动移除，不影响其他连接。"""
    mgr = ConnectionManager()
    dead_ws = AsyncMock()
    dead_ws.send_json = AsyncMock(side_effect=RuntimeError("closed"))
    live_ws = _fake_ws()

    mgr.register("s1", dead_ws)
    mgr.register("s1", live_ws)
    await mgr.push("s1", _chunk("test"))

    # 死连接被清除，活跃连接收到推送
    assert dead_ws not in mgr._conns.get("s1", set())
    live_ws.send_json.assert_awaited_once()


async def test_push_rate_limiting():
    """推送限流：连续推送间隔 < min_interval 时缓存帧。"""
    mgr = ConnectionManager(min_interval=0.1)  # 100ms 限流
    ws = _fake_ws()
    mgr.register("s1", ws)

    # 第一次推送应立即发送
    await mgr.push("s1", _chunk("a"))
    assert ws.send_json.call_count == 1

    # 立即第二次推送，应被限流（缓存）
    await mgr.push("s1", _chunk("b"))
    # 仍然只有 1 次（限流中，缓存了第二帧）
    assert ws.send_json.call_count == 1

    # 等限流窗口过去
    await asyncio.sleep(0.15)
    # 延迟推送 task 应该已经执行
    # 检查缓存是否已清空
    assert not mgr._pending_payloads.get("s1")


async def test_push_final_frame_not_rate_limited():
    """is_final=True 的帧不受限流影响。"""
    mgr = ConnectionManager(min_interval=10.0)  # 极长限流
    ws = _fake_ws()
    mgr.register("s1", ws)

    final_chunk = ResponseChunk.final("req1", "done")
    await mgr.push("s1", final_chunk)
    assert ws.send_json.call_count == 1  # final 帧立即推送


async def test_push_degrades_after_consecutive_failures():
    """连续推送失败后降级为静默。"""
    mgr = ConnectionManager(min_interval=0)  # 不限流
    key = ("", "s1")

    # 连续推送失败
    for _ in range(_MAX_CONSECUTIVE_FAILURES):
        dead_ws = AsyncMock()
        dead_ws.send_json = AsyncMock(side_effect=RuntimeError("closed"))
        mgr.register("s1", dead_ws)
        await mgr.push("s1", _chunk())

    # 超过阈值后降级
    assert mgr._consecutive_failures.get(key, 0) >= _MAX_CONSECUTIVE_FAILURES

    # 后续推送静默（不尝试推送）
    dead_ws = AsyncMock()
    dead_ws.send_json = AsyncMock(side_effect=RuntimeError("closed"))
    mgr.register("s1", dead_ws)
    dead_ws.send_json.reset_mock()
    await mgr.push("s1", _chunk())
    dead_ws.send_json.assert_not_awaited()


async def test_unregister_cleans_auxiliary_state():
    """unregister 清理 _last_push_ts / _consecutive_failures，防止 dict 无限增长。"""
    mgr = ConnectionManager(min_interval=0)
    ws = _fake_ws()
    mgr.register("s1", ws)
    key = ("", "s1")

    # 产生一些辅助状态
    await mgr.push("s1", _chunk("a"))
    assert key in mgr._last_push_ts
    assert key in mgr._consecutive_failures

    # unregister 后应清理
    mgr.unregister("s1", ws)
    assert key not in mgr._last_push_ts
    assert key not in mgr._consecutive_failures
    assert key not in mgr._pending_payloads
    assert key not in mgr._flush_tasks


async def test_unregister_cancels_pending_flush_task():
    """unregister 时若有未完成的延迟推送 task，应取消它。"""
    mgr = ConnectionManager(min_interval=10.0)  # 长限流 → 第二帧必定缓存
    ws = _fake_ws()
    mgr.register("s1", ws)
    key = ("", "s1")

    await mgr.push("s1", _chunk("a"))  # 第一帧立即推送，设置 _last_push_ts
    await mgr.push("s1", _chunk("b"))  # 第二帧被限流，创建 flush task
    assert key in mgr._flush_tasks
    flush_task = mgr._flush_tasks[key]
    assert not flush_task.done()

    mgr.unregister("s1", ws)
    assert key not in mgr._flush_tasks
    assert key not in mgr._pending_payloads
    # cancel() 已调用，让事件循环处理取消
    await asyncio.sleep(0)
    assert flush_task.cancelled() or flush_task.done()


async def test_unregister_keeps_state_when_other_connections_remain():
    """session 还有其他连接时，unregister 不应清理辅助状态。"""
    mgr = ConnectionManager(min_interval=0)
    ws1 = _fake_ws()
    ws2 = _fake_ws()
    mgr.register("s1", ws1)
    mgr.register("s1", ws2)
    key = ("", "s1")

    await mgr.push("s1", _chunk("a"))

    mgr.unregister("s1", ws1)  # 还有 ws2
    assert key in mgr._last_push_ts  # 状态保留
    assert mgr.has_connection("s1")


async def test_unregister_uses_socket_session_index_for_lock_cleanup():
    """同一 socket 订阅多个 session 时，只在最后一个 session 注销后清 send lock。"""
    mgr = ConnectionManager(min_interval=0)
    ws = _fake_ws()
    mgr.register("s1", ws)
    mgr.register("s2", ws)

    await mgr.send_socket(ws, {"kind": "ping"})
    assert ws in mgr._send_locks
    assert mgr._ws_to_sessions[ws] == {("", "s1"), ("", "s2")}

    mgr.unregister("s1", ws)
    assert ws in mgr._send_locks
    assert mgr._ws_to_sessions[ws] == {("", "s2")}

    mgr.unregister("s2", ws)
    assert ws not in mgr._send_locks
    assert ws not in mgr._ws_to_sessions


async def test_push_payload_no_connection_does_not_degrade():
    """没有活跃连接时，push_payload 应静默跳过，不累计失败。"""
    mgr = ConnectionManager(min_interval=0)

    await mgr.push_payload(
        "s1",
        {
            "kind": "delta",
            "body": {"text": "hello"},
            "is_final": False,
            "sequence": 1,
            "session_id": "s1",
        },
    )

    assert mgr._consecutive_failures.get("s1", 0) == 0
    assert "s1" not in mgr._last_push_ts


async def test_push_payload_rate_limit_merges_delta_text():
    """限流缓存的 delta 需拼接文本，避免前端按增量追加时丢字。"""
    mgr = ConnectionManager(min_interval=10.0)
    ws = _fake_ws()
    mgr.register("s1", ws)

    await mgr.push_payload(
        "s1",
        {"kind": "delta", "body": {"text": "a"}, "is_final": False, "sequence": 1, "session_id": "s1"},
    )
    await mgr.push_payload(
        "s1",
        {"kind": "delta", "body": {"text": "b"}, "is_final": False, "sequence": 2, "session_id": "s1"},
    )
    await mgr.push_payload(
        "s1",
        {"kind": "delta", "body": {"text": "c"}, "is_final": False, "sequence": 3, "session_id": "s1"},
    )

    assert ws.send_json.call_count == 1
    await asyncio.sleep(0)
    await mgr._flush_pending(("", "s1"))
    sent = [call.args[0] for call in ws.send_json.await_args_list]
    assert sent[-1]["body"]["text"] == "bc"


async def test_push_payload_final_flushes_pending_before_final():
    """final 到来前先刷掉 pending delta，避免旧帧晚到。"""
    mgr = ConnectionManager(min_interval=10.0)
    ws = _fake_ws()
    mgr.register("s1", ws)

    await mgr.push_payload(
        "s1",
        {"kind": "delta", "body": {"text": "a"}, "is_final": False, "sequence": 1, "session_id": "s1"},
    )
    await mgr.push_payload(
        "s1",
        {"kind": "delta", "body": {"text": "b"}, "is_final": False, "sequence": 2, "session_id": "s1"},
    )
    await mgr.push_payload(
        "s1",
        {"kind": "final", "body": {"text": "ab"}, "is_final": True, "sequence": 3, "session_id": "s1"},
    )

    sent = [call.args[0] for call in ws.send_json.await_args_list]
    assert [payload["kind"] for payload in sent] == ["delta", "delta", "final"]
    assert sent[1]["body"]["text"] == "b"
    assert not mgr._pending_payloads.get("s1")


# ---------------------------------------------------------------------------
# session:start 生命周期钩子（每个会话首次运行触发一次）
# ---------------------------------------------------------------------------


async def test_session_start_hook_fires_once_per_session():
    """session:start 只在每个会话生命周期内首次运行时触发；同会话第二次不再触发。"""
    from crew.gateway.hooks import hook_registry

    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store)
    fired: list[str] = []

    async def on_session_start(_event_type, context):
        fired.append(context.get("session_id"))

    hook_registry.register("session:start", on_session_start)
    try:
        await _drain(disp.run(_env("s1")))
        await _drain(disp.run(_env("s1")))  # 同会话第二次：不应再触发
        await _drain(disp.run(_env("s2")))  # 新会话：应触发
    finally:
        hook_registry.clear("session:start")

    assert fired == ["s1", "s2"]


async def test_agent_start_hook_includes_owner_account_id():
    """agent:start hook 须携带 owner_account_id，供渠道会话实时广播时对齐订阅桶。"""
    from crew.gateway.hooks import hook_registry

    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store)
    contexts: list[dict] = []

    async def on_agent_start(_event_type, context):
        contexts.append(context)

    hook_registry.register("agent:start", on_agent_start)
    try:
        await _drain(disp.run(Envelope.of("hi", session_id="s1", user_id="O:uid-o")))
    finally:
        hook_registry.clear("agent:start")

    assert len(contexts) == 1
    assert contexts[0].get("session_id") == "s1"
    assert contexts[0].get("owner_account_id") == "O:uid-o"
    assert contexts[0].get("channel") == "cli"


# ---------------------------------------------------------------------------
# sidechain 转写隔离 + 收敛特征化（task_runtime 启用时每轮在 {sid}::turn::{rid} 隔离运行）
#
# 这一组测试锁定 sidechain 流程的当前行为，为后续提取 SidechainTranscript 提供
# 行为基线：隔离 session、复制 base history、收敛回填、后台化跳过回填、output_ref
# 落盘、dynamic_kanban 不隔离、收敛后清理。标题经 task_session_id 直达外层父会话；
# dispatcher 自带的 sidechain 标题回填是死代码（list_sessions 排除 '::' 会话），见
# test_dispatcher_sidechain_title_fallback_is_inert。
# ---------------------------------------------------------------------------


def _sidechain_inner(
    store,
    *,
    parent_title: str | None = None,
    sidechain_title: str | None = None,
    record: dict | None = None,
):
    """模拟 agent：进入时记录 sidechain 初始历史，再把本轮 user+assistant 写入 sidechain。

    真实 agent 会在隔离转写上追加本轮对话；stub inner 默认不写历史，会让收敛回填把
    base history 原样盖回外层 session，无法体现真实流转，因此这里显式 append。

    标题语义对照真实 agent（runtime.py: title_sid = task_session_id）：
      - parent_title：写到 env.params['task_session_id']（外层父会话），即真实标题链路。
      - sidechain_title：写到隔离转写 env.session_id，用于验证 dispatcher 自带的标题回填
        是死代码（list_sessions 排除含 '::' 的 sidechain，回填永远拿不到 sidechain_meta）。
    """

    async def inner(env):
        if record is not None:
            record["exec_session"] = env.session_id
            record["entry_history"] = list(store.load(env.session_id, owner_account_id=OWNER))
        store.append(
            env.session_id,
            [Message.user("q"), Message.assistant("a")],
            owner_account_id=OWNER,
        )
        if parent_title:
            # 真实 agent 通过 task_session_id 把摘要标题写回外层父会话
            target = str(env.params.get("task_session_id") or env.session_id)
            store.set_title(target, parent_title, owner_account_id=OWNER)
        if sidechain_title:
            store.set_title(env.session_id, sidechain_title, owner_account_id=OWNER)
        yield ResponseChunk.final(env.request_id, "a")

    return inner


async def test_sidechain_isolates_turn_into_dedicated_session(tmp_path):
    """有 task_runtime 时 inner 收到 {sid}::turn::{rid}，不是外层 sid。"""
    store = InMemorySessionStore()
    record: dict = {}
    disp = SessionDispatcher(
        _sidechain_inner(store, record=record), store, task_runtime=_DummyTaskRuntime(tmp_path)
    )
    try:
        await _drain(disp.run(_env("s1")))
        assert record["exec_session"].startswith("s1::turn::")
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_copies_base_history_into_isolated_transcript(tmp_path):
    """sidechain 启动时继承外层 session 的 base history。"""
    store = InMemorySessionStore()
    store.save("s1", [Message.user("old"), Message.assistant("resp")], owner_account_id=OWNER)
    record: dict = {}
    disp = SessionDispatcher(
        _sidechain_inner(store, record=record), store, task_runtime=_DummyTaskRuntime(tmp_path)
    )
    try:
        await _drain(disp.run(_env("s1", "new")))
        # inner 进入时 sidechain 已含完整 base history（user+assistant 两条）
        assert [m.role for m in record["entry_history"]] == ["user", "assistant"]
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_history_folded_back_into_outer_session(tmp_path):
    """收敛后外层 session 历史 = sidechain 历史（本轮 user+assistant 被带回外层）。"""
    store = InMemorySessionStore()
    store.save("s1", [Message.user("old")], owner_account_id=OWNER)
    disp = SessionDispatcher(
        _sidechain_inner(store), store, task_runtime=_DummyTaskRuntime(tmp_path)
    )
    try:
        await _drain(disp.run(_env("s1", "new")))
        outer = [m.role for m in store.load("s1", owner_account_id=OWNER)]
        # base(user) + 本轮(user, assistant)
        assert outer == ["user", "user", "assistant"]
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_cleared_after_convergence(tmp_path):
    """收敛后隔离转写被清理，避免 list_sessions 污染与无界增长。"""
    store = InMemorySessionStore()
    record: dict = {}
    disp = SessionDispatcher(
        _sidechain_inner(store, record=record), store, task_runtime=_DummyTaskRuntime(tmp_path)
    )
    try:
        await _drain(disp.run(_env("s1")))
        assert store.load(record["exec_session"], owner_account_id=OWNER) == []
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_output_ref_written_as_json(tmp_path):
    """收敛时把 sidechain 历史（OpenAI 格式）写入 task 的 output_ref 路径。"""
    import json

    store = InMemorySessionStore()
    rt = _DummyTaskRuntime(tmp_path)
    disp = SessionDispatcher(_sidechain_inner(store), store, task_runtime=rt)
    try:
        await _drain(disp.run(_env("s1", "write me")))
        # dispatcher 通过 update() 把 output_ref 改写为 runtime home 下的真实路径，
        # sidechain 历史落盘到那里（不是 tmp_path），从 rt 实例取最终 output_ref 校验。
        refs = [t.get("output_ref") for t in rt._tasks.values() if t.get("output_ref")]
        assert refs, "task_runtime 未记录 output_ref"
        path = Path(refs[0])
        assert path.exists(), f"output_ref 未落盘：{path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and data
        # OpenAI 格式：每条带 role/content
        assert {item.get("role") for item in data} >= {"user", "assistant"}
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)
        for t in rt._tasks.values():
            ref = t.get("output_ref") or ""
            if ref:
                Path(ref).unlink(missing_ok=True)  # 清理落盘到 runtime home 的测试文件


async def test_dynamic_kanban_mode_skips_sidechain_isolation(tmp_path):
    """dynamic_kanban 自管外层历史，即便有 task_runtime 也不创建 sidechain。"""
    store = InMemorySessionStore()
    seen: list[str] = []

    async def inner(env):
        seen.append(env.session_id)
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store, task_runtime=_DummyTaskRuntime(tmp_path))
    try:
        await _drain(disp.run(Envelope.of("go", session_id="s1", user_id=OWNER, mode="dynamic_kanban")))
        assert seen == ["s1"]  # 直接命中外层 sid，没有 ::turn:: 隔离
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_backgrounded_turn_skips_foldback_into_outer_session(tmp_path):
    """后台化的轮次不把 sidechain 历史盖回外层（本轮 assistant 不进入外层）。"""
    store = InMemorySessionStore()
    store.save("s1", [Message.user("old")], owner_account_id=OWNER)
    disp = SessionDispatcher(
        _sidechain_inner(store),
        store,
        task_runtime=_DummyTaskRuntime(tmp_path, backgrounded=True),
    )
    try:
        await _drain(disp.run(_env("s1", "bg")))
        outer = store.load("s1", owner_account_id=OWNER)
        # 后台化 → 收敛跳过 save(s1, sidechain_history)；外层只有 base + 入站 user meta，
        # sidechain 里生成的 assistant 回答不会盖回外层（保留在 output_ref 里由后台任务承接）。
        assert not any(m.role == "assistant" for m in outer)
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_convergence_preserves_placeholder_when_title_auto(tmp_path):
    """title_auto 开启时，sidechain 收敛不得用首条 user 原话抢占父会话占位标题。"""
    from crew.state.session_store import is_placeholder_title

    store = InMemorySessionStore()
    store.save("s1", [], owner_account_id=OWNER)
    store.set_title("s1", "", owner_account_id=OWNER)
    disp = SessionDispatcher(
        _sidechain_inner(store),
        store,
        task_runtime=_DummyTaskRuntime(tmp_path),
        controller=type("_Cfg", (), {"config": type("_C", (), {"title_auto": True})()})(),
    )
    try:
        await _drain(disp.run(_env("s1", "写一篇关于量子计算的长文章")))
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        assert is_placeholder_title(row["title"])
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_parent_title_not_preempted_on_sqlite(tmp_path):
    """SQLite 路径：sidechain 启动 append 与收敛 save 均不得抢占占位标题（InMemory mock 测不到）。"""
    from crew.state.session_store import SQLiteSessionStore, is_placeholder_title

    store = SQLiteSessionStore(str(tmp_path / "title.db"))
    store.ensure_session("s1", title="", owner_account_id=OWNER)
    disp = SessionDispatcher(
        _sidechain_inner(store),
        store,
        task_runtime=_DummyTaskRuntime(tmp_path),
        controller=type("_Cfg", (), {"config": type("_C", (), {"title_auto": True})()})(),
    )
    try:
        await _drain(disp.run(_env("s1", "你好吗？")))
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        assert is_placeholder_title(row["title"]), f"expected placeholder, got {row['title']!r}"
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_async_summary_title_reaches_parent_after_sidechain_convergence(tmp_path):
    """模拟 _spawn_title_task：收敛完成后异步 set_title 应能写入父会话摘要。"""
    from crew.state.session_store import is_placeholder_title

    store = InMemorySessionStore()
    store.save("s1", [], owner_account_id=OWNER)
    store.set_title("s1", "", owner_account_id=OWNER)
    release = asyncio.Event()

    async def inner(env):
        store.append(
            env.session_id,
            [Message.user("q"), Message.assistant("a")],
            owner_account_id=OWNER,
        )

        async def _late_title() -> None:
            await release.wait()
            target = str(env.params.get("task_session_id") or env.session_id)
            row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == target)
            if not row.get("manual_title") and is_placeholder_title(str(row.get("title") or "")):
                store.set_title(target, "量子计算科普", owner_account_id=OWNER)

        asyncio.create_task(_late_title())
        yield ResponseChunk.final(env.request_id, "a")

    disp = SessionDispatcher(
        inner,
        store,
        task_runtime=_DummyTaskRuntime(tmp_path),
        controller=type("_Cfg", (), {"config": type("_C", (), {"title_auto": True})()})(),
    )
    try:
        await _drain(disp.run(_env("s1", "写一篇关于量子计算的长文章")))
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        assert is_placeholder_title(row["title"])
        release.set()
        await asyncio.sleep(0.05)
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        assert row["title"] == "量子计算科普"
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_title_flows_to_parent_via_task_session_id_param(tmp_path):
    """真实标题链路：dispatcher 把 task_session_id=外层sid 注入 sidechain envelope，
    agent 据此把摘要标题直接写到外层父会话（runtime.py title_sid = task_session_id）。"""
    store = InMemorySessionStore()
    store.save("s1", [Message.user("old")], owner_account_id=OWNER)
    disp = SessionDispatcher(
        _sidechain_inner(store, parent_title="摘要标题"),
        store,
        task_runtime=_DummyTaskRuntime(tmp_path),
    )
    try:
        await _drain(disp.run(_env("s1", "new")))
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        assert row["title"] == "摘要标题"  # agent 经 task_session_id 写到外层父会话
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_sidechain_title_does_not_leak_to_parent(tmp_path):
    """特征化（锁定当前行为）：sidechain 上的标题不会回填到父会话。

    sidechain_id 形如 "{sid}::turn::{rid}"，被 list_sessions 排除（含 '::'），因此 dispatcher
    收敛段读不到 sidechain 标题；dispatcher 也不再尝试回填（旧版一段据此回填、且带 manual_title
    守卫的死代码已删除——它永远不会触发）。sidechain 摘要标题只会随 sidechain 被 clear 一起丢弃，
    父会话标题由 Agent 经 task_session_id 参数直接写入（见 test_title_flows_to_parent_via_task_session_id_param）。
    本用例锁定：收敛后父标题保持占位，不被 sidechain 摘要覆盖。
    """
    store = InMemorySessionStore()
    store.save("s1", [Message.user("old")], owner_account_id=OWNER)
    store.set_title("s1", "", owner_account_id=OWNER)  # 外层占位 + 非手动
    disp = SessionDispatcher(
        _sidechain_inner(store, sidechain_title="sidechain 摘要"),
        store,
        task_runtime=_DummyTaskRuntime(tmp_path),
    )
    try:
        await _drain(disp.run(_env("s1", "new")))
        row = next(r for r in store.list_sessions(owner_account_id=OWNER) if r["session_id"] == "s1")
        # 回填未触发：外层标题仍是占位，没有被 sidechain 上的「sidechain 摘要」覆盖
        assert row["title"] == ""
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


# ---------------------------------------------------------------------------
# session:end 钩子：回收本会话的 start 标记 / task 集合 / exec session 映射
# ---------------------------------------------------------------------------


async def test_session_end_resets_start_marker_so_restart_refires_session_start():
    """session:end 清掉 _sessions_started 标记 → 同 session_id 复用时 session:start 再次触发。"""
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store)
    fired: list[str] = []

    async def on_session_start(_event_type, context):
        fired.append(context.get("session_id"))

    hook_registry.register("session:start", on_session_start)
    try:
        await _drain(disp.run(_env("s1")))
        await _drain(disp.run(_env("s1")))  # 同会话第二轮：标记命中，不再触发
        assert fired == ["s1"]
        await hook_registry.emit("session:end", {"session_id": "s1", "owner_account_id": OWNER})
        assert (OWNER, "s1") not in disp._sessions_started  # 标记被回收
        await _drain(disp.run(_env("s1")))  # 复用：标记已清，session:start 再次触发
        assert fired == ["s1", "s1"]
    finally:
        hook_registry.clear("session:start")
        hook_registry.unregister("session:end", disp._on_session_end)


async def test_session_end_clears_active_exec_session_mapping():
    """session:end 清掉 _active_exec_session_ids，避免 steer/interrupt 命中陈旧 sidechain。"""
    store = InMemorySessionStore()

    async def inner(env):
        yield ResponseChunk.final(env.request_id, "ok")

    disp = SessionDispatcher(inner, store, task_runtime=_DummyTaskRuntime(Path("/tmp")))
    try:
        await _drain(disp.run(_env("s1")))
        key = (OWNER, "s1")
        # 运行结束已自行清掉 exec 映射（finally 里 remaining==0 分支），但 session:end 应幂等
        await hook_registry.emit("session:end", {"session_id": "s1", "owner_account_id": OWNER})
        assert key not in disp._active_exec_session_ids
        assert key not in disp._sessions_started
        assert key not in disp._tasks
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


# ---------------------------------------------------------------------------
# 全局并发槽：取消时释放，避免 max_active_runs 被永久占用
# ---------------------------------------------------------------------------


async def test_global_slot_released_when_run_cancelled():
    """运行中的请求被 stop 取消后，全局槽释放，排队的其它 session 可立即运行。"""
    store = InMemorySessionStore()
    gate = asyncio.Event()
    started: list[str] = []

    async def inner(env):
        started.append(env.session_id)
        yield ResponseChunk.delta(env.request_id, "x")
        await gate.wait()
        yield ResponseChunk.final(env.request_id, "done")

    disp = SessionDispatcher(inner, store, max_active_runs=1)
    t_a = asyncio.create_task(_drain(disp.run(_env("a"))))
    await asyncio.sleep(0.02)
    assert started == ["a"]

    t_b = asyncio.create_task(_drain(disp.run(_env("b"))))
    await asyncio.sleep(0.05)
    assert started == ["a"]  # 全局槽被 a 占用，b 在等
    assert disp.status("b", owner_account_id=OWNER)["waiting_for_global_slot"] == 1

    # 取消 a → 槽释放 → b 获得
    assert disp.stop("a", owner_account_id=OWNER)
    await asyncio.sleep(0.05)
    assert started == ["a", "b"]
    gate.set()
    await t_a
    await t_b


async def test_dispatcher_shutdown_cancels_active_turns_and_rejects_new_admission():
    store = InMemorySessionStore()
    started = asyncio.Event()

    async def inner(env):
        started.set()
        await asyncio.Event().wait()
        yield ResponseChunk.final(env.request_id, "unreachable")

    disp = SessionDispatcher(inner, store)
    active = asyncio.create_task(_drain(disp.run(_env("shutdown-active"))))
    await started.wait()

    snapshot = disp.active_tasks_snapshot()
    assert active in snapshot
    await disp.shutdown()

    assert active.done()
    assert await _drain(disp.run(_env("shutdown-late"))) == []
