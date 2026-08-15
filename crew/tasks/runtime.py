"""SQLite-backed unified long-task runtime.

The runtime owns task state, worker cancellation, inactivity/execution timeout
monitoring, completion deduplication, and restart reconciliation. Execution
backends (shell/subagent/agent turn/team) remain in their existing modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import Any

import psutil

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.tasks.models import RuntimeTask, TaskKind, normalize_task_status
from crew.tools.process_registry import terminate_process_tree

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


def _host_boot_id() -> str:
    boot_time = int(psutil.boot_time())
    return hashlib.sha256(f"ace-task-runtime-boot:{boot_time}".encode("ascii")).hexdigest()


class TaskRuntime:
    """Persistent task manager with a compatibility surface for Team/Subagent."""

    def __init__(
        self,
        db_path: str,
        *,
        wal_enabled: bool = True,
        monitor_interval: float = 5.0,
        heartbeat_interval: float = 10.0,
        wait_timeout: float = 30.0,
        finished_retention_days: int = 7,
        monotonic_clock: Callable[[], float] = time.monotonic,
        boot_id_provider: Callable[[], str] = _host_boot_id,
    ) -> None:
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.RLock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self.monitor_interval = max(0.1, float(monitor_interval))
        self.heartbeat_interval = max(0.1, float(heartbeat_interval))
        self.wait_timeout = max(0.0, float(wait_timeout))
        self.finished_retention_days = max(0, int(finished_retention_days))
        self._monotonic_clock = monotonic_clock
        self._boot_id_provider = boot_id_provider
        self._workers: dict[str, asyncio.Task[Any]] = {}
        self._cancel_callbacks: dict[str, Callable[[str], Any]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._monitor_task: asyncio.Task[Any] | None = None
        self._event_callback: Callable[[dict[str, Any]], Any] | None = None
        self._completion_callback: Callable[[dict[str, Any]], Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._blocked_owners: set[str] = set()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_tasks (
                task_id TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                action_digest TEXT NOT NULL DEFAULT '',
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
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_tasks_session ON runtime_tasks(session_id, created_at DESC)"
        )
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runtime_tasks)").fetchall()}
        if "owner_account_id" not in cols:
            self._conn.execute("ALTER TABLE runtime_tasks ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")
        if "action_digest" not in cols:
            self._conn.execute(
                "ALTER TABLE runtime_tasks ADD COLUMN action_digest TEXT NOT NULL DEFAULT ''"
            )
        if "monotonic_boot_id" not in cols:
            self._conn.execute(
                "ALTER TABLE runtime_tasks ADD COLUMN monotonic_boot_id TEXT NOT NULL DEFAULT ''"
            )
        if "started_monotonic" not in cols:
            self._conn.execute(
                "ALTER TABLE runtime_tasks ADD COLUMN started_monotonic REAL"
            )
        if "last_activity_monotonic" not in cols:
            self._conn.execute(
                "ALTER TABLE runtime_tasks ADD COLUMN last_activity_monotonic REAL"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_tasks_owner_session "
            "ON runtime_tasks(owner_account_id, session_id, created_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_tasks_status ON runtime_tasks(status, updated_at)"
        )

    def set_callbacks(
        self,
        *,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        on_completion: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._event_callback = on_event
        self._completion_callback = on_completion

    def create_runtime(
        self,
        *,
        kind: TaskKind,
        session_id: str,
        title: str,
        request_id: str = "",
        action_digest: str = "",
        tool_call_id: str = "",
        parent_task_id: str = "",
        detail: str = "",
        assignee: str | None = None,
        output_ref: str = "",
        execution_timeout: float = 0.0,
        inactivity_timeout: float = 0.0,
        backgrounded: bool = False,
        auto_backgrounded: bool = False,
        task_id: str | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        owner_account_id = str(owner_account_id or "").strip()
        if owner_account_id in self._blocked_owners:
            raise RuntimeError("账号正在退出登录，不能创建新任务")
        now = time.time()
        record = RuntimeTask(
            task_id=task_id or f"task_{uuid.uuid4().hex[:12]}",
            kind=kind,
            session_id=session_id,
            title=title,
            request_id=request_id,
            action_digest=action_digest,
            tool_call_id=tool_call_id,
            parent_task_id=parent_task_id,
            detail=detail,
            assignee=assignee,
            output_ref=output_ref,
            execution_timeout=max(0.0, float(execution_timeout or 0)),
            inactivity_timeout=max(0.0, float(inactivity_timeout or 0)),
            backgrounded=backgrounded,
            auto_backgrounded=auto_backgrounded,
            created_at=now,
            updated_at=now,
        )
        columns = [f.name for f in fields(RuntimeTask)]
        values = [getattr(record, name) for name in columns]
        columns.insert(1, "owner_account_id")
        values.insert(1, owner_account_id)
        values[columns.index("progress")] = json.dumps(record.progress, ensure_ascii=False)
        for name in ("backgrounded", "auto_backgrounded", "cancel_requested"):
            values[columns.index(name)] = int(bool(values[columns.index(name)]))

        def _write(conn: sqlite3.Connection) -> None:
            marks = ",".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO runtime_tasks ({','.join(columns)}) VALUES ({marks})",
                values,
            )

        self._writer.execute(_write)
        self._emit(record.to_dict(), "started" if record.status == "running" else "created")
        return record.to_dict()

    def activate_owner(self, owner_account_id: str) -> None:
        """Allow task creation after a newly authenticated owner becomes active."""
        owner = str(owner_account_id or "").strip()
        if owner:
            self._blocked_owners.discard(owner)

    def block_owner(self, owner_account_id: str) -> None:
        """Prevent new runtime tasks while owner logout cleanup is in progress."""
        owner = str(owner_account_id or "").strip()
        if owner:
            self._blocked_owners.add(owner)

    # Compatibility with the old TaskManager used by Team/Subagent.
    def create(
        self,
        session_id: str,
        title: str,
        detail: str = "",
        assignee: str | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        task = self.create_runtime(
            kind="team",
            session_id=session_id,
            title=title,
            detail=detail,
            assignee=assignee,
            backgrounded=False,
            owner_account_id=owner_account_id,
        )
        if assignee:
            return self.mark_running(task["task_id"])
        return task

    def assign(self, task_id: str, assignee: str) -> dict[str, Any]:
        return self.update(
            task_id,
            owner_account_id=self._owner_for_task(task_id),
            assignee=assignee,
            status="running",
            started_at=time.time(),
        )

    def update_status(self, task_id: str, status: str, result: str = "") -> dict[str, Any]:
        normalized = normalize_task_status(status)
        owner = self._owner_for_task(task_id)
        if normalized in TERMINAL_STATUSES:
            return self.finish(
                task_id,
                owner_account_id=owner,
                status=normalized,
                result=result,
                error=result if normalized in {"failed", "timed_out"} else "",
            )
        return self.update(task_id, owner_account_id=owner, status=normalized)

    def mark_running(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        monotonic_now = self._monotonic_clock()
        owner = self._owner_for_task(task_id)
        task = self.update(
            task_id,
            owner_account_id=owner,
            status="running",
            started_at=now,
            last_activity_at=now,
            last_heartbeat_at=now,
            monotonic_boot_id=self._boot_id_provider(),
            started_monotonic=monotonic_now,
            last_activity_monotonic=monotonic_now,
        )
        self._emit(task, "started")
        return task

    def update(self, task_id: str, owner_account_id: str = "", **changes: Any) -> dict[str, Any]:
        allowed = {f.name for f in fields(RuntimeTask)} - {"task_id", "created_at"}
        clean = {k: v for k, v in changes.items() if k in allowed}
        if "status" in clean:
            clean["status"] = normalize_task_status(str(clean["status"]))
        if "progress" in clean:
            clean["progress"] = json.dumps(clean["progress"] or {}, ensure_ascii=False)
        for name in ("backgrounded", "auto_backgrounded", "cancel_requested"):
            if name in clean:
                clean[name] = int(bool(clean[name]))
        clean["updated_at"] = time.time()
        if not clean:
            return self.get(task_id, owner_account_id=owner_account_id)

        def _write(conn: sqlite3.Connection) -> None:
            sql = ", ".join(f"{key}=?" for key in clean)
            where = "task_id=?"
            params = [*clean.values(), task_id]
            where += " AND owner_account_id=?"
            params.append(owner_account_id)
            cur = conn.execute(
                f"UPDATE runtime_tasks SET {sql} WHERE {where}",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(f"任务不存在: {task_id}")

        self._writer.execute(_write)
        return self.get(task_id, owner_account_id=owner_account_id)

    def touch_activity(self, task_id: str, progress: dict[str, Any] | None = None) -> dict[str, Any]:
        owner = self._owner_for_task(task_id)
        task = self.get(task_id, owner_account_id=owner)
        changes: dict[str, Any] = {"last_activity_at": time.time()}
        if task.get("monotonic_boot_id") == self._boot_id_provider():
            changes["last_activity_monotonic"] = self._monotonic_clock()
        if progress is not None:
            changes["progress"] = progress
        task = self.update(task_id, owner_account_id=owner, **changes)
        self._emit(task, "progress")
        return task

    def heartbeat(self, task_id: str) -> dict[str, Any]:
        return self.update(task_id, owner_account_id=self._owner_for_task(task_id), last_heartbeat_at=time.time())

    def set_backgrounded(self, task_id: str, *, automatic: bool = False) -> dict[str, Any]:
        task = self.update(
            task_id,
            owner_account_id=self._owner_for_task(task_id),
            backgrounded=True,
            auto_backgrounded=automatic,
        )
        self._emit(task, "backgrounded")
        return task

    def finish(
        self,
        task_id: str,
        *,
        owner_account_id: str,
        status: str = "completed",
        result: str = "",
        error: str = "",
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically set one terminal state and run completion side effects once."""

        normalized = normalize_task_status(status)
        if normalized not in TERMINAL_STATUSES:
            raise ValueError(f"finish 不接受非终态: {normalized}")
        owner = str(owner_account_id or "").strip()
        now = time.time()
        changes: dict[str, Any] = {
            "status": normalized,
            "result": result,
            "error": error,
            "finished_at": now,
            "last_heartbeat_at": now,
        }
        if progress is not None:
            changes["progress"] = json.dumps(progress or {}, ensure_ascii=False)
        changes["updated_at"] = now
        terminal_values = sorted(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal_values)

        def _finish(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            assignments = ", ".join(f"{key}=?" for key in changes)
            cursor = conn.execute(
                f"""
                UPDATE runtime_tasks
                SET {assignments}
                WHERE task_id=? AND owner_account_id=?
                  AND status NOT IN ({placeholders})
                """,
                (*changes.values(), task_id, owner, *terminal_values),
            )
            row = conn.execute(
                "SELECT * FROM runtime_tasks WHERE task_id=? AND owner_account_id=?",
                (task_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            return self._row_to_dict(row), cursor.rowcount == 1

        task, won = self._writer.execute(_finish)
        if not won:
            return task
        event = self._events.get(task_id)
        if event is not None:
            event.set()
        self._workers.pop(task_id, None)
        self._cancel_callbacks.pop(task_id, None)
        self._emit(task, normalized)
        self._notify_completion(task)
        return task

    def _owner_for_task(self, task_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_account_id FROM runtime_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        return str(row[0] or "")

    def get(self, task_id: str, owner_account_id: str = "") -> dict[str, Any]:
        sql = "SELECT * FROM runtime_tasks WHERE task_id=?"
        sql += " AND owner_account_id=?"
        params: tuple[Any, ...] = (task_id, owner_account_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        return self._row_to_dict(row)

    def list(self, session_id: str, owner_account_id: str = "") -> list[dict[str, Any]]:
        return self.list_tasks(session_id=session_id, owner_account_id=owner_account_id)

    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
        owner_account_id: str = "",
        _all_owners: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if not _all_owners:
            clauses.append("owner_account_id=?")
            values.append(owner_account_id)
        if session_id:
            clauses.append("session_id=?")
            values.append(session_id)
        if status:
            clauses.append("status=?")
            values.append(normalize_task_status(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM runtime_tasks{where} ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def attach_worker(
        self,
        task_id: str,
        worker: asyncio.Task[Any] | None,
        *,
        cancel: Callable[[str], Any] | None = None,
    ) -> None:
        if worker is not None:
            self._workers[task_id] = worker
        if cancel is not None:
            self._cancel_callbacks[task_id] = cancel
        self._events.setdefault(task_id, asyncio.Event())

    async def wait(
        self,
        task_id: str,
        timeout: float | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        task = self.get(task_id, owner_account_id=owner_account_id)
        if task["status"] in TERMINAL_STATUSES:
            return task
        event = self._events.setdefault(task_id, asyncio.Event())
        effective = self.wait_timeout if timeout is None else max(0.0, float(timeout))
        try:
            if effective > 0:
                await asyncio.wait_for(event.wait(), timeout=effective)
            else:
                await event.wait()
        except TimeoutError:
            task = self.get(task_id, owner_account_id=owner_account_id)
            return {**task, "retrieval_status": "timeout"}
        return {**self.get(task_id, owner_account_id=owner_account_id), "retrieval_status": "success"}

    async def cancel(
        self,
        task_id: str,
        reason: str = "用户取消",
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        task = self.get(task_id, owner_account_id=owner_account_id)
        if task["status"] in TERMINAL_STATUSES:
            return task
        self.update(task_id, owner_account_id=owner_account_id, cancel_requested=True)
        callback = self._cancel_callbacks.get(task_id)
        if callback is not None:
            value = callback(reason)
            if asyncio.iscoroutine(value):
                await value
        elif task["kind"] == "shell":
            self.kill_process_group(int((task.get("progress") or {}).get("pid") or 0), reason)
        worker = self._workers.get(task_id)
        if worker is not None and not worker.done():
            worker.cancel()
        await asyncio.sleep(0.05)
        return self.finish(
            task_id,
            owner_account_id=owner_account_id,
            status="cancelled",
            error=reason,
        )

    async def cancel_owner(
        self,
        owner_account_id: str,
        *,
        reason: str = "账号退出登录",
    ) -> list[str]:
        """Cancel every non-terminal runtime owned by one account.

        New task creation is fenced before the snapshot is taken.  Individual
        cancellation races are tolerated because another worker may reach a
        terminal state while logout is draining the owner.
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            return []
        self.block_owner(owner)
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT task_id FROM runtime_tasks
                WHERE owner_account_id=? AND status NOT IN ({placeholders})
                ORDER BY created_at ASC
                """,
                (owner, *sorted(TERMINAL_STATUSES)),
            ).fetchall()
        task_ids = [str(row[0]) for row in rows]

        async def _cancel_one(task_id: str) -> str | None:
            try:
                await self.cancel(task_id, reason, owner_account_id=owner)
                return task_id
            except KeyError:
                return None

        results = await asyncio.gather(*(_cancel_one(task_id) for task_id in task_ids))
        return [task_id for task_id in results if task_id is not None]

    def mark_notified(self, task_id: str, *, owner_account_id: str) -> bool:
        """Atomically claim the one completion-notification side effect."""

        owner = str(owner_account_id or "").strip()
        now = time.time()
        terminal_values = sorted(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal_values)

        def _mark(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"""
                UPDATE runtime_tasks
                SET notified_at=?, updated_at=?
                WHERE task_id=? AND owner_account_id=? AND notified_at IS NULL
                  AND status IN ({placeholders})
                """,
                (now, now, task_id, owner, *terminal_values),
            )
            return cursor.rowcount

        return self._writer.execute(_mark) == 1

    def mark_resume_enqueued(self, task_id: str, *, owner_account_id: str) -> bool:
        """Atomically claim one post-completion automatic resume."""

        owner = str(owner_account_id or "").strip()
        now = time.time()
        terminal_values = sorted(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal_values)

        def _mark(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"""
                UPDATE runtime_tasks
                SET resume_enqueued_at=?, updated_at=?
                WHERE task_id=? AND owner_account_id=?
                  AND notified_at IS NOT NULL AND resume_enqueued_at IS NULL
                  AND status IN ({placeholders})
                """,
                (now, now, task_id, owner, *terminal_values),
            )
            return cursor.rowcount

        return self._writer.execute(_mark) == 1

    async def start(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self.reconcile_after_restart()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.monitor_interval)
            monotonic_now = self._monotonic_clock()
            boot_id = self._boot_id_provider()
            for task in self.list_tasks(status="running", limit=1000, _all_owners=True):
                reason = ""
                has_timeout = (
                    task["execution_timeout"] > 0
                    or task["inactivity_timeout"] > 0
                )
                if has_timeout and task.get("monotonic_boot_id") != boot_id:
                    reason = "单调计时身份已失效"
                started_monotonic = task.get("started_monotonic")
                activity_monotonic = task.get("last_activity_monotonic")
                if (
                    not reason
                    and task["execution_timeout"] > 0
                    and isinstance(started_monotonic, (int, float))
                ):
                    elapsed = monotonic_now - float(started_monotonic)
                    if elapsed < 0:
                        reason = "单调运行计时状态非法"
                    elif elapsed >= task["execution_timeout"]:
                        reason = f"达到运行上限 {task['execution_timeout']:.0f}s"
                if (
                    not reason
                    and task["inactivity_timeout"] > 0
                    and isinstance(activity_monotonic, (int, float))
                ):
                    idle = monotonic_now - float(activity_monotonic)
                    if idle < 0:
                        reason = "单调活动计时状态非法"
                    elif idle >= task["inactivity_timeout"]:
                        reason = f"无业务活动超过 {task['inactivity_timeout']:.0f}s"
                if (
                    not reason
                    and has_timeout
                    and (
                        not isinstance(started_monotonic, (int, float))
                        or not isinstance(activity_monotonic, (int, float))
                    )
                ):
                    reason = "单调计时状态缺失"
                if reason:
                    await self._timeout(task["task_id"], reason)
            self.prune_finished()

    async def _timeout(self, task_id: str, reason: str) -> None:
        owner = self._owner_for_task(task_id)
        task = self.get(task_id, owner_account_id=owner)
        if task["status"] != "running":
            return
        self.update(task_id, owner_account_id=owner, cancel_requested=True)
        callback = self._cancel_callbacks.get(task_id)
        if callback is not None:
            value = callback(reason)
            if asyncio.iscoroutine(value):
                try:
                    await asyncio.wait_for(value, timeout=3.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass
        elif task["kind"] == "shell":
            self.kill_process_group(int((task.get("progress") or {}).get("pid") or 0), reason)
        worker = self._workers.get(task_id)
        if worker is not None and not worker.done():
            worker.cancel()
        await asyncio.sleep(0.05)
        self.finish(
            task_id,
            owner_account_id=owner,
            status="timed_out",
            error=reason,
        )

    def reconcile_after_restart(self) -> None:
        """Non-shell coroutines cannot survive process restart."""
        for task in self.list_tasks(status="running", limit=10000, _all_owners=True):
            if task["kind"] == "shell":
                pid = int((task.get("progress") or {}).get("pid") or 0)
                if pid and self._pid_alive(pid):
                    self.update(
                        task["task_id"],
                        owner_account_id=str(task.get("owner_account_id") or ""),
                        progress={**task["progress"], "detached": True},
                        last_heartbeat_at=time.time(),
                    )
                else:
                    self.finish(
                        task["task_id"],
                        owner_account_id=str(task.get("owner_account_id") or ""),
                        status="failed",
                        error="服务重启后未发现存活进程，退出码无法恢复",
                    )
            else:
                self.finish(
                    task["task_id"],
                    owner_account_id=str(task.get("owner_account_id") or ""),
                    status="failed",
                    error="服务重启，Python 协程无法恢复",
                )
        for task in self.list_tasks(limit=10000, _all_owners=True):
            if task["status"] in TERMINAL_STATUSES and task["notified_at"] is None:
                self._notify_completion(task)

    def prune_finished(self) -> int:
        if self.finished_retention_days <= 0:
            return 0
        cutoff = time.time() - self.finished_retention_days * 86400

        # 局部列表经闭包收集，避免挂在实例字段上被并发/重入串台
        pending_refs: list[str] = []

        def _write(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                "SELECT output_ref, owner_account_id FROM runtime_tasks "
                "WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            ).fetchall()
            refs = [
                (str(r["output_ref"] or "").strip(), str(r["owner_account_id"] or ""))
                for r in rows
                if r["output_ref"]
            ]
            cur = conn.execute(
                "DELETE FROM runtime_tasks WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
            pending_refs.extend(refs)
            return int(cur.rowcount)

        deleted = self._writer.execute(_write)
        for ref, owner in pending_refs:
            self._safe_unlink_output_ref(ref, owner_account_id=owner)
        return deleted

    def unlink_session_output_files(self, session_id: str, owner_account_id: str = "") -> int:
        """删除某会话全部 runtime task 的 output_ref 磁盘文件（不删 DB 行）。"""
        tasks = self.list_tasks(session_id=session_id, owner_account_id=owner_account_id, limit=10000)
        n = 0
        for task in tasks:
            ref = str(task.get("output_ref") or "").strip()
            if ref and self._safe_unlink_output_ref(ref, owner_account_id=owner_account_id):
                n += 1
            # dispatcher 还会写 tasks/<task_id>.json
            tid = str(task.get("task_id") or "").strip()
            if tid:
                json_side = self._task_json_beside_ref(ref, tid)
                if json_side and self._safe_unlink_output_ref(
                    str(json_side),
                    owner_account_id=owner_account_id,
                ):
                    n += 1
        return n

    @staticmethod
    def _task_json_beside_ref(output_ref: str, task_id: str) -> Path | None:
        if not output_ref:
            return None
        try:
            parent = Path(output_ref).expanduser().resolve().parent
        except OSError:
            return None
        return parent / f"{task_id}.json"

    def _safe_unlink_output_ref(self, output_ref: str, *, owner_account_id: str = "") -> bool:
        """仅删除落在某账号 tasks/ 目录下的文件。"""
        raw = str(output_ref or "").strip()
        if not raw:
            return False
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            return False
        if owner_account_id:
            try:
                from crew.state.home import get_owner_runtime_home

                allowed_root = Path(get_owner_runtime_home(owner_account_id)).expanduser().resolve()
                allowed_tasks = allowed_root / "tasks"
                path.relative_to(allowed_tasks)
            except (OSError, ValueError):
                return False
        # 必须是 .../tasks/<file>，父目录名 tasks
        if path.parent.name != "tasks" or not path.is_file():
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def close(self) -> None:
        self._conn.close()

    def _emit(self, task: dict[str, Any], phase: str) -> None:
        callback = self._event_callback
        if callback is None:
            return
        if phase == "progress":
            try:
                current = self.get(
                    task["task_id"],
                    owner_account_id=str(task.get("owner_account_id") or ""),
                )
            except KeyError:
                return
            if current["status"] in TERMINAL_STATUSES:
                return
            task = current
        payload = {**task, "phase": phase}
        self._call_callback(callback, payload)

    def _notify_completion(self, task: dict[str, Any]) -> None:
        callback = self._completion_callback
        if callback is None or task.get("notified_at") is not None:
            return
        self._call_callback(callback, task)

    def _call_callback(self, callback: Callable[[dict[str, Any]], Any], payload: dict[str, Any]) -> None:
        def _run() -> None:
            value = callback(payload)
            if asyncio.iscoroutine(value):
                try:
                    asyncio.get_running_loop().create_task(value)
                except RuntimeError:
                    value.close()

        try:
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_run)
            else:
                value = callback(payload)
                if asyncio.iscoroutine(value):
                    value.close()
        except Exception:  # noqa: BLE001 - completion callbacks must not break runtime cleanup
            return

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def kill_process_group(pid: int, _reason: str = "") -> None:
        if pid <= 0:
            return
        terminate_process_tree(pid)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["progress"] = json.loads(data.get("progress") or "{}")
        except json.JSONDecodeError:
            data["progress"] = {}
        for name in ("backgrounded", "auto_backgrounded", "cancel_requested"):
            data[name] = bool(data.get(name))
        data["id"] = data["task_id"]
        return data
