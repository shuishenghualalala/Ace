"""Background task and continuable subagent identity accounting."""

from __future__ import annotations

import json

import pytest

from crew.agent.subagent.runtime import LegacySubagentBackgroundRuntime
from crew.agent.subagent.service import SubagentResult


class _Tasks:
    def __init__(self):
        self.progress = []
        self.status = None

    def create_runtime(self, **_kwargs):
        return {"task_id": "task-accounting"}

    def mark_running(self, _task_id):
        return None

    def touch_activity(self, task_id, progress):
        self.progress.append((task_id, progress))
        return {"task_id": task_id, "progress": progress}

    def update_status(self, task_id, status, result):
        self.status = (task_id, status, json.loads(result))
        return {"task_id": task_id, "status": status}


class _Service:
    continuable_enabled = True

    async def execute(self, request, *, runtime_options=None):
        assert request.subagent_id == "sub::worker::fixed"
        return SubagentResult(
            output="done",
            stop_reason="completed",
            subagent_id=request.subagent_id or "",
        )


@pytest.mark.asyncio
async def test_background_task_keeps_task_and_subagent_ids(tmp_path):
    tasks = _Tasks()
    pending = []
    runtime = LegacySubagentBackgroundRuntime(
        _Service(),
        tasks=tasks,
        launch_background=pending.append,
        idle_timeout=1,
        max_runtime=1,
    )

    launched = await runtime.launch(
        item={"label": "worker", "goal_text": "do work", "subagent_id": "sub::worker::fixed"},
        parent_session_id="parent",
        parent_task_id=None,
        owner_account_id="owner",
        workspace_id=None,
    )
    assert launched == {
        "status": "launched",
        "task_id": "task-accounting",
        "subagent_id": "sub::worker::fixed",
        "agent": "worker",
        "hint": "用 collect_subagent(task_id) 取结果；完成时也会推送通知",
    }
    assert tasks.progress == [("task-accounting", {"subagent_id": "sub::worker::fixed"})]

    await pending[0]
    assert tasks.status is not None
    task_id, status, result = tasks.status
    assert task_id == "task-accounting"
    assert status == "done"
    assert result["task_id"] == "task-accounting"
    assert result["subagent_id"] == "sub::worker::fixed"
