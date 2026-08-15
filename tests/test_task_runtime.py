"""Unified long-task runtime regression tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.mocks import InMemorySessionStore
from crew.core.runctx import (
    current_owner_account_id,
    current_request_id,
    current_session_id,
    current_tool_call_id,
)
from crew.gateway.dispatcher import SessionDispatcher
from crew.security.context import SecurityContext
from crew.security.launch import (
    ProcessLaunch,
    current_process_launch,
    issue_process_launch,
)
from crew.security.models import PermissionProfile, PermissionProfileKind
from crew.state.home import get_owner_runtime_home
from crew.tasks.runtime import TaskRuntime
from crew.tools.builtin import handle_terminal
from crew.tools.process_registry import process_registry


def _runtime(tmp_path, **kwargs) -> TaskRuntime:
    runtime = TaskRuntime(
        str(tmp_path / "tasks.db"),
        monitor_interval=kwargs.get("monitor_interval", 0.02),
        heartbeat_interval=kwargs.get("heartbeat_interval", 0.02),
        wait_timeout=kwargs.get("wait_timeout", 0.05),
        monotonic_clock=kwargs.get("monotonic_clock", time.monotonic),
        boot_id_provider=kwargs.get("boot_id_provider", lambda: "test-boot"),
    )
    runtime.auto_background_after = kwargs.get("auto_background_after", 0.1)
    runtime.defaults = {
        "shell_inactivity": kwargs.get("shell_inactivity", 5.0),
        "shell_execution": 0.0,
    }
    return runtime


def test_output_ref_unlink_is_owner_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    runtime = _runtime(tmp_path)
    owner_a = "A:uid-a"
    owner_b = "B:uid-b"
    session_id = "same"

    home_a = get_owner_runtime_home(owner_a)
    home_b = get_owner_runtime_home(owner_b)
    tasks_a = home_a / "tasks"
    tasks_b = home_b / "tasks"
    tasks_a.mkdir(parents=True)
    tasks_b.mkdir(parents=True)
    file_a = tasks_a / "a.json"
    file_b = tasks_b / "b.json"
    file_a.write_text("A", encoding="utf-8")
    file_b.write_text("B", encoding="utf-8")

    task_a = runtime.create_runtime(
        kind="shell",
        session_id=session_id,
        title="task A",
        owner_account_id=owner_a,
    )
    task_b = runtime.create_runtime(
        kind="shell",
        session_id=session_id,
        title="task B",
        owner_account_id=owner_b,
    )
    runtime.update(task_a["task_id"], owner_account_id=owner_a, output_ref=str(file_a))
    # Malicious opaque ref: B's row points at A's file. Owner-bound cleanup must
    # refuse to delete it even though the parent directory is named "tasks".
    runtime.update(task_b["task_id"], owner_account_id=owner_b, output_ref=str(file_a))

    runtime.unlink_session_output_files(session_id, owner_account_id=owner_b)
    assert file_a.exists()

    runtime.update(task_b["task_id"], owner_account_id=owner_b, output_ref=str(file_b))
    runtime.unlink_session_output_files(session_id, owner_account_id=owner_b)
    assert not file_b.exists()
    assert file_a.exists()


def test_output_refs_are_task_scoped_within_same_owner_and_session(
    tmp_path,
    monkeypatch,
):
    """同一 owner/session 的两个 task 必须持有独立、不可串台的 output ref。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    runtime = _runtime(tmp_path)
    owner = "A:uid-a"
    session = "same"
    tasks_dir = get_owner_runtime_home(owner) / "tasks"
    tasks_dir.mkdir(parents=True)
    file_a = tasks_dir / "task-a.log"
    file_b = tasks_dir / "task-b.log"

    task_a = runtime.create_runtime(
        kind="shell",
        session_id=session,
        title="task A",
        owner_account_id=owner,
    )
    task_b = runtime.create_runtime(
        kind="shell",
        session_id=session,
        title="task B",
        owner_account_id=owner,
    )
    runtime.update(task_a["task_id"], owner_account_id=owner, output_ref=str(file_a))
    runtime.update(task_b["task_id"], owner_account_id=owner, output_ref=str(file_b))

    assert runtime.get(task_a["task_id"], owner_account_id=owner)["output_ref"] == str(file_a)
    assert runtime.get(task_b["task_id"], owner_account_id=owner)["output_ref"] == str(file_b)
    # 再更新 A 不得改写 B 的 ref
    runtime.update(task_a["task_id"], owner_account_id=owner, output_ref=str(file_a))
    assert runtime.get(task_b["task_id"], owner_account_id=owner)["output_ref"] == str(file_b)


def test_runtime_migrates_and_persists_action_digest(tmp_path):
    db_path = tmp_path / "legacy-tasks.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runtime_tasks (
                task_id TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                tool_call_id TEXT NOT NULL DEFAULT '',
                parent_task_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                assignee TEXT,
                progress TEXT NOT NULL DEFAULT '{}',
                output_ref TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                last_activity_at REAL,
                last_heartbeat_at REAL,
                monotonic_boot_id TEXT NOT NULL DEFAULT '',
                started_monotonic REAL,
                last_activity_monotonic REAL,
                execution_timeout REAL NOT NULL DEFAULT 0,
                inactivity_timeout REAL NOT NULL DEFAULT 0,
                backgrounded INTEGER NOT NULL DEFAULT 0,
                auto_backgrounded INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                notified_at REAL,
                resume_enqueued_at REAL
            )
            """
        )

    runtime = TaskRuntime(str(db_path))
    try:
        columns = {
            row[1]
            for row in runtime._conn.execute("PRAGMA table_info(runtime_tasks)").fetchall()
        }
        assert "action_digest" in columns
        task = runtime.create_runtime(
            kind="agent_turn",
            session_id="s1",
            title="digest",
            action_digest="sha256:test",
        )
        assert runtime.get(task["task_id"])["action_digest"] == "sha256:test"
    finally:
        runtime.close()


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
async def test_execution_timeout_uses_persisted_monotonic_clock_not_wall_clock(
    tmp_path,
    monkeypatch,
):
    monotonic_now = [100.0]
    runtime = _runtime(
        tmp_path,
        monitor_interval=0.01,
        monotonic_clock=lambda: monotonic_now[0],
        boot_id_provider=lambda: "stable-boot",
    )
    task = runtime.create_runtime(
        kind="subagent",
        session_id="s1",
        title="monotonic",
        execution_timeout=10,
    )
    await runtime.start()
    runtime.mark_running(task["task_id"])
    monkeypatch.setattr("crew.tasks.runtime.time.time", lambda: -10_000_000.0)
    monotonic_now[0] = 111.0

    try:
        for _ in range(20):
            if runtime.get(task["task_id"])["status"] == "timed_out":
                break
            await asyncio.sleep(0.01)
        assert runtime.get(task["task_id"])["status"] == "timed_out"
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
    tok_owner = current_owner_account_id.set("owner-a")
    tok_sid = current_session_id.set("shell-session")
    tok_rid = current_request_id.set("req-1")
    tok_tid = current_tool_call_id.set("tool-1")
    tok_launch = current_process_launch.set(
        issue_process_launch(
            SecurityContext(
                os_user="host-user",
                owner_account_id="owner-a",
                workspace_id="workspace-a",
                workspace_root=tmp_path,
                session_id="shell-session",
                request_id="req-1",
                task_id="tool-1",
                cwd=tmp_path,
            ),
            PermissionProfile(PermissionProfileKind.DISABLED),
        )
    )
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
        completed = await runtime.wait(
            task_id,
            timeout=2,
            owner_account_id="owner-a",
        )
        assert completed["status"] == "completed"
        assert completed["progress"]["pid"] == pid
        assert "end" in completed["result"]
    finally:
        current_process_launch.reset(tok_launch)
        current_tool_call_id.reset(tok_tid)
        current_request_id.reset(tok_rid)
        current_session_id.reset(tok_sid)
        current_owner_account_id.reset(tok_owner)
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
