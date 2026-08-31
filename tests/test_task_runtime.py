"""Unified long-task runtime regression tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from crew.agent.loop.tool_runner import ToolRunner
from crew.agent.subagent.tools import _run_children
from crew.core.runctx import (
    current_request_id,
    current_session_id,
    current_tool_call_id,
    current_tool_progress_fn,
    emit_tool_progress,
    touch_current_task_activity,
)
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.mocks import InMemorySessionStore
from crew.core.types import ToolCall
from crew.gateway.dispatcher import SessionDispatcher
from crew.tasks.runtime import TaskRuntime
from crew.tools.builtin import handle_terminal
from crew.tools.process_registry import process_registry


def _runtime(tmp_path, **kwargs) -> TaskRuntime:
    runtime = TaskRuntime(
        str(tmp_path / "tasks.db"),
        monitor_interval=kwargs.get("monitor_interval", 0.02),
        heartbeat_interval=kwargs.get("heartbeat_interval", 0.02),
        wait_timeout=kwargs.get("wait_timeout", 0.05),
    )
    runtime.auto_background_after = kwargs.get("auto_background_after", 0.1)
    runtime.defaults = {
        "shell_inactivity": kwargs.get("shell_inactivity", 5.0),
        "shell_execution": 0.0,
    }
    return runtime


def _agent_turn_controller(*, inactivity_timeout: float) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            tasks_agent_turn_inactivity_timeout_seconds=inactivity_timeout,
            tasks_agent_turn_execution_timeout_seconds=0,
        )
    )


def _agent_turn_tasks(runtime: TaskRuntime, *, owner: str = "owner") -> list[dict]:
    return runtime.list_tasks(session_id="s1", owner_account_id=owner)


@pytest.mark.asyncio
async def test_activity_and_heartbeat_are_independent(tmp_path):
    runtime = _runtime(tmp_path, monitor_interval=0.01)
    await runtime.start()
    try:
        task = runtime.create_runtime(
            kind="subagent",
            session_id="s1",
            title="idle",
            inactivity_timeout=0.08,
            execution_timeout=0,
            backgrounded=True,
        )
        runtime.mark_running(task["task_id"])
        for _ in range(4):
            runtime.heartbeat(task["task_id"])
            await asyncio.sleep(0.03)
        for _ in range(10):
            if runtime.get(task["task_id"])["status"] == "timed_out":
                break
            await asyncio.sleep(0.02)
        assert runtime.get(task["task_id"])["status"] == "timed_out"
    finally:
        await runtime.stop()
        runtime.close()


@pytest.mark.asyncio
async def test_agent_turn_tool_progress_keeps_watchdog_alive(tmp_path):
    """工具旁路进度也必须刷新 parent agent_turn 的业务活动时间。"""
    runtime = _runtime(tmp_path, monitor_interval=0.1)
    store = InMemorySessionStore()

    async def inner(envelope):
        runner = ToolRunner(None, None, None, session_id=envelope.session_id)
        token = runner._install_progress_sink(ToolCall("tool-1", "terminal", {}))
        try:
            for index in range(12):
                await emit_tool_progress(f"running {index}")
                await asyncio.sleep(0.025)
        finally:
            current_tool_progress_fn.reset(token)
        yield ResponseChunk.final(envelope.request_id, "done")

    dispatcher = SessionDispatcher(
        inner,
        store,
        controller=_agent_turn_controller(inactivity_timeout=0.08),
        task_runtime=runtime,
    )
    await runtime.start()
    try:
        chunks = [
            chunk
            async for chunk in dispatcher.run(
                Envelope.of("run", session_id="s1", user_id="owner", mode="dynamic_kanban")
            )
        ]
        task = _agent_turn_tasks(runtime)[0]
        assert task["status"] == "completed"
        assert task["last_activity_at"] > task["started_at"]
        assert chunks[-1].kind == "final"
    finally:
        await runtime.stop()
        runtime.close()


@pytest.mark.asyncio
async def test_synchronous_subagent_progress_keeps_parent_watchdog_alive(tmp_path):
    """同步 delegate_task 等待子 agent 时，其子 chunk 也属于父回合活动。"""
    runtime = _runtime(tmp_path, monitor_interval=0.1)
    store = InMemorySessionStore()

    class StreamingChild:
        async def run(self, envelope):
            for index in range(12):
                await asyncio.sleep(0.025)
                yield ResponseChunk.delta(envelope.request_id, f"step {index}")
            yield ResponseChunk.final(envelope.request_id, "child done")

        async def aclose(self):
            return None

    async def inner(envelope):
        await _run_children(
            [{"label": "child", "goal_text": "work", "spec": {}}],
            build_child=lambda _spec: StreamingChild(),
            max_concurrent=1,
            active=None,
            idle_timeout=1,
            max_runtime=1,
            progress_callback=touch_current_task_activity,
        )
        yield ResponseChunk.final(envelope.request_id, "done")

    dispatcher = SessionDispatcher(
        inner,
        store,
        controller=_agent_turn_controller(inactivity_timeout=0.08),
        task_runtime=runtime,
    )
    await runtime.start()
    try:
        chunks = [
            chunk
            async for chunk in dispatcher.run(
                Envelope.of("run", session_id="s1", user_id="owner", mode="dynamic_kanban")
            )
        ]
        task = _agent_turn_tasks(runtime)[0]
        assert task["status"] == "completed"
        assert task["last_activity_at"] > task["started_at"]
        assert chunks[-1].kind == "final"
    finally:
        await runtime.stop()
        runtime.close()


@pytest.mark.asyncio
async def test_agent_turn_watchdog_preserves_timeout_reason_and_terminal_state(tmp_path, monkeypatch):
    """watchdog 取消回合后，最终状态与前端错误都必须保留超时原因。"""
    runtime = _runtime(tmp_path, monitor_interval=0.1)
    store = InMemorySessionStore()
    blocked = asyncio.Event()

    async def inner(envelope):
        await blocked.wait()
        yield ResponseChunk.final(envelope.request_id, "unreachable")

    dispatcher = SessionDispatcher(
        inner,
        store,
        controller=_agent_turn_controller(inactivity_timeout=0.08),
        task_runtime=runtime,
    )
    monkeypatch.setattr("crew.state.home.get_owner_runtime_home", lambda _owner: tmp_path)
    await runtime.start()
    try:
        chunks = [
            chunk
            async for chunk in dispatcher.run(
                Envelope.of("run", session_id="s1", user_id="owner")
            )
        ]
        task = _agent_turn_tasks(runtime)[0]
        status, error = store.get_status("s1", owner_account_id="owner")
        assert task["status"] == "timed_out"
        assert task["error"].startswith("无业务活动超过")
        assert chunks[-1].kind == "error"
        assert chunks[-1].body["message"] == task["error"]
        assert status == "failed"
        assert error == task["error"]
    finally:
        await runtime.stop()
        runtime.close()


@pytest.mark.asyncio
async def test_agent_turn_cancel_preserves_cancelled_terminal_state(tmp_path):
    """通过 TaskRuntime 取消正在运行的回合时，不能被 dispatcher 覆盖为 failed。"""
    runtime = _runtime(tmp_path, monitor_interval=0.1)
    store = InMemorySessionStore()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def inner(envelope):
        started.set()
        await blocked.wait()
        yield ResponseChunk.final(envelope.request_id, "unreachable")

    dispatcher = SessionDispatcher(
        inner,
        store,
        controller=_agent_turn_controller(inactivity_timeout=10),
        task_runtime=runtime,
    )
    await runtime.start()
    try:
        async def drain() -> list[ResponseChunk]:
            return [
                chunk
                async for chunk in dispatcher.run(
                    Envelope.of("run", session_id="s1", user_id="owner", mode="dynamic_kanban")
                )
            ]

        run_task = asyncio.create_task(drain())
        await started.wait()
        task_id = _agent_turn_tasks(runtime)[0]["task_id"]
        cancelled = await runtime.cancel(task_id, reason="测试取消", owner_account_id="owner")
        chunks = await run_task
        task = runtime.get(task_id, owner_account_id="owner")
        status, error = store.get_status("s1", owner_account_id="owner")
        assert cancelled["status"] == "cancelled"
        assert task["status"] == "cancelled"
        assert task["error"] == "测试取消"
        assert chunks[-1].kind == "error"
        assert chunks[-1].body["message"] == "测试取消"
        assert status == "stopped"
        assert error == "测试取消"
    finally:
        await runtime.stop()
        runtime.close()


@pytest.mark.asyncio
async def test_wait_timeout_does_not_cancel_task(tmp_path):
    runtime = _runtime(tmp_path, wait_timeout=0.02)
    task = runtime.create_runtime(kind="team", session_id="s1", title="wait")
    runtime.mark_running(task["task_id"])
    result = await runtime.wait(task["task_id"])
    assert result["retrieval_status"] == "timeout"
    assert runtime.get(task["task_id"])["status"] == "running"
    runtime.finish(
        task["task_id"],
        owner_account_id="",
        status="completed",
        result="ok",
    )
    runtime.close()


@pytest.mark.asyncio
async def test_shell_auto_background_reuses_running_process(tmp_path):
    runtime = _runtime(tmp_path, auto_background_after=0.05)
    process_registry.configure_task_runtime(runtime)
    tok_sid = current_session_id.set("shell-session")
    tok_rid = current_request_id.set("req-1")
    tok_tid = current_tool_call_id.set("tool-1")
    try:
        payload = json.loads(
            await handle_terminal(
                {
                    "command": "echo start; sleep 0.2; echo end"
                }
            )
        )
        assert payload["background"] is True
        assert payload["auto_backgrounded"] is True
        task_id = payload["task_id"]
        pid = payload["pid"]
        completed = await runtime.wait(task_id, timeout=2)
        assert completed["status"] == "completed"
        assert completed["progress"]["pid"] == pid
        assert "end" in completed["result"]
    finally:
        current_tool_call_id.reset(tok_tid)
        current_request_id.reset(tok_rid)
        current_session_id.reset(tok_sid)
        process_registry.configure_task_runtime(None)
        runtime.close()


@pytest.mark.asyncio
async def test_restart_reconciliation(tmp_path):
    runtime = _runtime(tmp_path)
    agent = runtime.create_runtime(kind="agent_turn", session_id="s1", title="agent")
    runtime.mark_running(agent["task_id"])
    shell = runtime.create_runtime(kind="shell", session_id="s1", title="shell")
    runtime.mark_running(shell["task_id"])
    # Windows 无 sleep 可执行文件；用当前解释器挂起进程以模拟长驻 shell。
    process_options = (
        {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        }
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        **process_options,
    )
    runtime.touch_activity(shell["task_id"], {"pid": process.pid})
    runtime.close()

    recovered = _runtime(tmp_path)
    recovered.reconcile_after_restart()
    assert recovered.get(agent["task_id"])["status"] == "failed"
    shell_row = recovered.get(shell["task_id"])
    assert shell_row["status"] == "running"
    assert shell_row["progress"]["detached"] is True
    await recovered.cancel(shell["task_id"], "restart cancel")
    await asyncio.wait_for(process.wait(), timeout=2)
    assert recovered.get(shell["task_id"])["status"] == "cancelled"
    recovered.close()


@pytest.mark.asyncio
async def test_background_agent_turn_releases_foreground_via_sidechain(tmp_path):
    runtime = _runtime(tmp_path)
    store = InMemorySessionStore()
    gate = asyncio.Event()
    started: list[str] = []

    async def inner(envelope):
        started.append(envelope.session_id)
        yield ResponseChunk.delta(envelope.request_id, "working")
        await gate.wait()
        yield ResponseChunk.final(envelope.request_id, "done")

    dispatcher = SessionDispatcher(inner, store, task_runtime=runtime)

    async def drain(envelope):
        return [chunk async for chunk in dispatcher.run(envelope)]

    first = asyncio.create_task(drain(Envelope.of("first", session_id="s1", user_id="local")))
    await asyncio.sleep(0.03)
    first_task_id = dispatcher.background("s1", owner_account_id="local")
    assert first_task_id
    second = asyncio.create_task(drain(Envelope.of("second", session_id="s1", user_id="local")))
    await asyncio.sleep(0.03)
    assert len(started) == 2
    assert all(value.startswith("s1::turn::") for value in started)
    gate.set()
    await asyncio.gather(first, second)
    assert runtime.get(first_task_id, owner_account_id="local")["backgrounded"] is True
    runtime.close()


def test_task_get_is_owner_scoped(tmp_path):
    runtime = _runtime(tmp_path)
    task = runtime.create_runtime(
        kind="team",
        session_id="same",
        title="owned",
        owner_account_id="A:uid-a",
    )

    assert runtime.get(task["task_id"], owner_account_id="A:uid-a")["title"] == "owned"
    with pytest.raises(KeyError):
        runtime.get(task["task_id"], owner_account_id="B:uid-b")
    with pytest.raises(KeyError):
        runtime.get(task["task_id"])
    runtime.close()


@pytest.mark.asyncio
async def test_cancel_owner_cancels_all_nonterminal_tasks_without_cross_owner_effect(tmp_path):
    runtime = _runtime(tmp_path)
    owner_a = "A:uid-a"
    owner_b = "B:uid-b"
    task_a1 = runtime.create_runtime(
        kind="team", session_id="same", title="a1", owner_account_id=owner_a
    )
    task_a2 = runtime.create_runtime(
        kind="subagent", session_id="other", title="a2", owner_account_id=owner_a
    )
    task_b = runtime.create_runtime(
        kind="team", session_id="same", title="b", owner_account_id=owner_b
    )
    for task in (task_a1, task_a2, task_b):
        runtime.mark_running(task["task_id"])

    cancelled = await runtime.cancel_owner(owner_a, reason="账号退出登录")

    assert set(cancelled) == {task_a1["task_id"], task_a2["task_id"]}
    assert runtime.get(task_a1["task_id"], owner_account_id=owner_a)["status"] == "cancelled"
    assert runtime.get(task_a2["task_id"], owner_account_id=owner_a)["status"] == "cancelled"
    assert runtime.get(task_b["task_id"], owner_account_id=owner_b)["status"] == "running"
    runtime.close()


def test_finish_has_one_database_winner_and_one_completion_side_effect(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    owner = "A:uid-a"
    runtime_a = TaskRuntime(db_path)
    task = runtime_a.create_runtime(
        kind="team",
        session_id="same",
        title="atomic",
        owner_account_id=owner,
    )
    runtime_a.mark_running(task["task_id"])
    runtime_b = TaskRuntime(db_path)
    terminal_events: list[str] = []
    completions: list[str] = []
    runtime_a.set_callbacks(
        on_event=lambda payload: terminal_events.append(payload["status"]),
        on_completion=lambda payload: completions.append(payload["status"]),
    )
    runtime_b.set_callbacks(
        on_event=lambda payload: terminal_events.append(payload["status"]),
        on_completion=lambda payload: completions.append(payload["status"]),
    )
    barrier = threading.Barrier(2)

    def finish(runtime: TaskRuntime, status: str):
        barrier.wait()
        return runtime.finish(
            task["task_id"],
            owner_account_id=owner,
            status=status,
            result=status,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(finish, runtime_a, "completed"),
            pool.submit(finish, runtime_b, "failed"),
        ]
        results = [future.result() for future in futures]

    assert results[0]["status"] == results[1]["status"]
    assert terminal_events == [results[0]["status"]]
    assert completions == [results[0]["status"]]
    runtime_a.close()
    runtime_b.close()


def test_notification_and_resume_claims_are_atomic_across_connections(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    owner = "A:uid-a"
    runtime_a = TaskRuntime(db_path)
    task = runtime_a.create_runtime(
        kind="shell",
        session_id="same",
        title="notify",
        owner_account_id=owner,
    )
    runtime_a.finish(task["task_id"], owner_account_id=owner)
    runtime_b = TaskRuntime(db_path)

    def race(method_name: str) -> list[bool]:
        barrier = threading.Barrier(2)

        def claim(runtime: TaskRuntime) -> bool:
            barrier.wait()
            method = getattr(runtime, method_name)
            return method(task["task_id"], owner_account_id=owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(claim, (runtime_a, runtime_b)))

    assert sorted(race("mark_notified")) == [False, True]
    assert sorted(race("mark_resume_enqueued")) == [False, True]
    runtime_a.close()
    runtime_b.close()


def test_late_completion_cannot_overwrite_logout_cancellation(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    owner = "A:uid-a"
    runtime_a = TaskRuntime(db_path)
    task = runtime_a.create_runtime(
        kind="subagent",
        session_id="same",
        title="logout",
        owner_account_id=owner,
    )
    runtime_a.mark_running(task["task_id"])
    runtime_b = TaskRuntime(db_path)

    cancelled = runtime_a.finish(
        task["task_id"],
        owner_account_id=owner,
        status="cancelled",
        error="账号退出登录",
    )
    late = runtime_b.finish(
        task["task_id"],
        owner_account_id=owner,
        status="completed",
        result="late result",
    )

    assert cancelled["status"] == "cancelled"
    assert late["status"] == "cancelled"
    assert late["error"] == "账号退出登录"
    assert late["result"] == ""
    runtime_a.close()
    runtime_b.close()
