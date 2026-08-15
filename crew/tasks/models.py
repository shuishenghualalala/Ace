"""统一任务数据模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]
TaskKind = Literal["shell", "subagent", "agent_turn", "team"]

_LEGACY_STATUS = {
    "in_progress": "running",
    "done": "completed",
    "timeout": "timed_out",
}


def normalize_task_status(status: str) -> TaskStatus:
    value = _LEGACY_STATUS.get(str(status or "").strip(), str(status or "").strip())
    if value not in {"pending", "running", "completed", "failed", "cancelled", "timed_out"}:
        raise ValueError(f"非法任务状态: {status}")
    return value  # type: ignore[return-value]


@dataclass
class Task:
    session_id: str
    title: str
    detail: str = ""
    assignee: str | None = None
    status: TaskStatus = "pending"
    result: str = ""
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeTask:
    """可持久化的长任务记录。"""

    task_id: str
    kind: TaskKind
    session_id: str
    title: str
    request_id: str = ""
    action_digest: str = ""
    tool_call_id: str = ""
    parent_task_id: str = ""
    status: TaskStatus = "pending"
    detail: str = ""
    assignee: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    output_ref: str = ""
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    last_activity_at: float | None = None
    last_heartbeat_at: float | None = None
    monotonic_boot_id: str = ""
    started_monotonic: float | None = None
    last_activity_monotonic: float | None = None
    execution_timeout: float = 0.0
    inactivity_timeout: float = 0.0
    backgrounded: bool = False
    auto_backgrounded: bool = False
    cancel_requested: bool = False
    notified_at: float | None = None
    resume_enqueued_at: float | None = None

    @property
    def id(self) -> str:
        return self.task_id

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.task_id
        return data
