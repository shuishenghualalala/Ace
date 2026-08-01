"""SessionDispatcher session:end 钩子测试（G5）。

验证删除会话后：
  1) _sessions_started 不再残留该 sid；
  2) 同一 sid 复用时 session:start 会再次触发（每个生命周期一次）。
"""

from __future__ import annotations

import asyncio

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.gateway.dispatcher import SessionDispatcher
from crew.gateway.hooks import hook_registry
from crew.tasks.runtime import TaskRuntime

OWNER = "A:uid-a"
KEY_A = (OWNER, "sess-A")


class _FakeStore:
    """最小 SessionStore：满足 dispatcher.run() 调用的接口。"""

    def get_workspace_id(self, session_id, owner_account_id=""):
        return None

    def set_status(self, session_id, status, error="", owner_account_id=""):
        pass

    def get_status(self, session_id, owner_account_id=""):
        return ("idle", "")


def _make_inner():
    async def inner(envelope):
        yield ResponseChunk.final(envelope.request_id, "ok")
    return inner


@pytest.mark.asyncio
async def test_session_end_prunes_started_and_re_fires_start():
    """session:end 后 _sessions_started 清除；复用同 sid 时 session:start 再次触发。"""
    disp = SessionDispatcher(_make_inner(), _FakeStore())
    try:
        starts: list[str] = []

        def on_start(event_type, context):
            starts.append(context.get("session_id"))

        hook_registry.register("session:start", on_start)
        try:
            env = Envelope.of("hi", session_id="sess-A", channel="test", user_id=OWNER)
            async for _ in disp.run(env):
                pass
            assert starts == ["sess-A"]
            assert KEY_A in disp._sessions_started

            # 删除会话 → 触发 session:end（与 routers/sessions.py 同源）
            await hook_registry.emit("session:end", {"session_id": "sess-A", "owner_account_id": OWNER})
            # 关键断言：标记被清掉
            assert KEY_A not in disp._sessions_started
            assert KEY_A not in disp._tasks

            # 同 sid 复用：session:start 必须再次触发（新生命周期）
            async for _ in disp.run(Envelope.of("again", session_id="sess-A", channel="test", user_id=OWNER)):
                pass
            assert starts.count("sess-A") == 2, f"应触发两次，实际 {starts}"
        finally:
            hook_registry.unregister("session:start", on_start)
    finally:
        # 去掉 dispatcher 在 __init__ 注册的 session:end handler，避免污染其它测试
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_session_end_without_reuse_keeps_others():
    """session:end 只清自己，不影响其它会话的 start 标记。"""
    disp = SessionDispatcher(_make_inner(), _FakeStore())
    try:
        async for _ in disp.run(Envelope.of("hi", session_id="A", channel="test", user_id=OWNER)):
            pass
        async for _ in disp.run(Envelope.of("hi", session_id="B", channel="test", user_id=OWNER)):
            pass
        assert {(OWNER, "A"), (OWNER, "B")} <= disp._sessions_started

        await hook_registry.emit("session:end", {"session_id": "A", "owner_account_id": OWNER})
        assert (OWNER, "A") not in disp._sessions_started
        assert (OWNER, "B") in disp._sessions_started
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_status_exposes_running_request_id():
    """status() 返回当前持锁运行的 request_id，供前端重连后恢复回合身份。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def inner(envelope):
        started.set()
        await release.wait()
        yield ResponseChunk.final(envelope.request_id, "ok")

    disp = SessionDispatcher(inner, _FakeStore())
    try:
        task = asyncio.create_task(_drain(disp.run(Envelope.of(
            "hi",
            session_id="sess-A",
            channel="test",
            request_id="req-run",
            user_id=OWNER,
        ))))
        await started.wait()

        st = disp.status("sess-A", owner_account_id=OWNER)

        assert st["live"] == "running"
        assert st["active_request_id"] == "req-run"
        release.set()
        await task
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_status_exposes_next_queued_request_id():
    """status() 在 queued 状态返回队首 request_id，避免重连后绑定到未知首帧。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def inner(envelope):
        started.set()
        await release.wait()
        yield ResponseChunk.final(envelope.request_id, "ok")

    disp = SessionDispatcher(inner, _FakeStore())
    try:
        running = asyncio.create_task(_drain(disp.run(Envelope.of(
            "run",
            session_id="sess-A",
            channel="test",
            request_id="req-run",
            user_id=OWNER,
        ))))
        await started.wait()
        queued = asyncio.create_task(_drain(disp.run(Envelope.of(
            "queued",
            session_id="sess-A",
            channel="test",
            request_id="req-queued",
            user_id=OWNER,
        ))))

        await asyncio.sleep(0)
        st = disp.status("sess-A", owner_account_id=OWNER)

        assert st["live"] == "running"
        assert st["active_request_id"] == "req-run"
        assert disp._waiting_request_ids[KEY_A] == ["req-queued"]
        release.set()
        await asyncio.gather(running, queued)
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


@pytest.mark.asyncio
async def test_stop_cascades_to_sidechain_turn_tasks():
    class Controller:
        def __init__(self):
            self.interrupted: list[str] = []

        def interrupt(self, session_id, reason, owner_account_id=""):
            self.interrupted.append(session_id)
            return True

    controller = Controller()
    disp = SessionDispatcher(_make_inner(), _FakeStore(), controller=controller)
    try:
        parent = (OWNER, "web_parent")
        sidechain = (OWNER, "web_parent::turn::req_1")

        async def never():
            await asyncio.Event().wait()

        parent_task = asyncio.create_task(never())
        sidechain_task = asyncio.create_task(never())
        disp._tasks[parent] = {parent_task}
        disp._tasks[sidechain] = {sidechain_task}

        assert disp.stop("web_parent", owner_account_id=OWNER) is True
        assert parent_task.cancelled() or parent_task.cancelling()
        assert sidechain_task.cancelled() or sidechain_task.cancelling()
        assert "web_parent" in controller.interrupted
        assert "web_parent::turn::req_1" in controller.interrupted
    finally:
        for tasks in disp._tasks.values():
            for task in tasks:
                task.cancel()
        hook_registry.unregister("session:end", disp._on_session_end)


def test_stop_cancels_runtime_sidechain_tasks_without_memory_task(tmp_path):
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    parent = runtime.create_runtime(
        kind="agent_turn",
        session_id="web_parent",
        title="父任务",
        owner_account_id=OWNER,
    )
    sidechain = runtime.create_runtime(
        kind="team",
        session_id="web_parent::turn::req_1",
        title="团队子任务",
        owner_account_id=OWNER,
    )
    runtime.mark_running(parent["task_id"])
    runtime.mark_running(sidechain["task_id"])
    disp = SessionDispatcher(_make_inner(), _FakeStore(), task_runtime=runtime)
    try:
        assert disp.stop("web_parent", owner_account_id=OWNER) is True
        assert runtime.get(parent["task_id"], owner_account_id=OWNER)["status"] == "cancelled"
        assert runtime.get(sidechain["task_id"], owner_account_id=OWNER)["status"] == "cancelled"
    finally:
        hook_registry.unregister("session:end", disp._on_session_end)


async def _drain(iterator):
    async for _ in iterator:
        pass
