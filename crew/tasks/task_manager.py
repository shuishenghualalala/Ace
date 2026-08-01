"""内存任务管理器。Team 协同时记录任务流转，作为任务看板数据源。

生产可替换为 SQLite/PostgreSQL 实现，只要满足 TaskManager 接口即可。
"""

from __future__ import annotations

import time
from typing import Any

from crew.core.interfaces import TaskManager
from crew.tasks.models import Task, normalize_task_status


class InMemoryTaskManager(TaskManager):
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(
        self,
        session_id: str,
        title: str,
        detail: str = "",
        assignee: str | None = None,
        *,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        task = Task(session_id=session_id, title=title, detail=detail, assignee=assignee,
                    status="pending" if assignee is None else "in_progress")
        self._tasks[task.id] = task
        return task.to_dict()

    def assign(self, task_id: str, assignee: str) -> dict[str, Any]:
        task = self._get(task_id)
        task.assignee = assignee
        task.status = "in_progress"
        task.updated_at = time.time()
        return task.to_dict()

    def update_status(self, task_id: str, status: str, result: str = "") -> dict[str, Any]:
        task = self._get(task_id)
        normalized = normalize_task_status(status)
        # Legacy in-memory callers still render the old vocabulary.
        task.status = {"running": "in_progress", "completed": "done", "timed_out": "failed"}.get(
            normalized, normalized
        )
        if result:
            task.result = result
        task.updated_at = time.time()
        return task.to_dict()

    def get(self, task_id: str) -> dict[str, Any]:
        return self._get(task_id).to_dict()

    def list(self, session_id: str) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values() if t.session_id == session_id]

    # ---- internal ----
    def _get(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise KeyError(f"任务不存在: {task_id}")
        return self._tasks[task_id]


class LegacyTaskManagerAdapter:
    """Old Team/Subagent status vocabulary over the unified TaskRuntime."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    @staticmethod
    def _legacy(task: dict[str, Any]) -> dict[str, Any]:
        status = task.get("status")
        mapped = {
            "running": "in_progress",
            "completed": "done",
            "timed_out": "failed",
            "cancelled": "cancelled",
        }.get(status, status)
        return {**task, "status": mapped}

    def create(
        self,
        session_id: str,
        title: str,
        detail: str = "",
        assignee: str | None = None,
        *,
        owner_account_id: str = "",
    ):
        return self._legacy(
            self.runtime.create(
                session_id,
                title,
                detail,
                assignee,
                owner_account_id=owner_account_id,
            )
        )

    def create_runtime(self, **kwargs):
        return self._legacy(self.runtime.create_runtime(**kwargs))

    def mark_running(self, task_id: str):
        return self._legacy(self.runtime.mark_running(task_id))

    def assign(self, task_id: str, assignee: str):
        return self._legacy(self.runtime.assign(task_id, assignee))

    def update_status(self, task_id: str, status: str, result: str = ""):
        return self._legacy(self.runtime.update_status(task_id, status, result))

    def get(self, task_id: str, owner_account_id: str = ""):
        return self._legacy(self.runtime.get(task_id, owner_account_id=owner_account_id))

    def list(self, session_id: str, owner_account_id: str = ""):
        return [
            self._legacy(task)
            for task in self.runtime.list(session_id, owner_account_id=owner_account_id)
        ]

    def touch_activity(self, task_id: str, progress: dict[str, Any] | None = None):
        return self._legacy(self.runtime.touch_activity(task_id, progress))
