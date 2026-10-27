"""Dynamic Kanban 数据模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


WorkflowStatus = str  # active | paused | done | failed
TaskStatus = str  # pending | ready | running | done | failed | blocked


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Workflow:
    """一次 Dynamic Kanban 工作流。"""

    session_id: str
    owner_account_id: str
    title: str = ""
    status: WorkflowStatus = "active"
    context: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("wf"))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KanbanTask:
    """看板上的一个任务。"""

    workflow_id: str
    title: str
    detail: str = ""
    assignee: str | None = None
    status: TaskStatus = "pending"
    result_summary: str = ""
    artifact_paths: list[str] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 2
    claimed_by: str | None = None
    claimed_at: float | None = None
    done_at: float | None = None
    id: str = field(default_factory=lambda: _new_id("task"))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Dependency:
    """任务依赖边：parent -> child。"""

    parent_task_id: str
    child_task_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskRun:
    """一次任务执行记录。"""

    task_id: str
    agent_run_id: str
    status: str = "running"
    output: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    id: str = field(default_factory=lambda: _new_id("run"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BoardEvent:
    """看板审计事件。"""

    workflow_id: str
    event_type: str
    task_id: str | None = None
    actor: str = "system"
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("evt"))
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanNode:
    """任务图中的一个节点（Crew的稳定符号 ID）。"""

    id: str
    title: str
    detail: str = ""
    assignee: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanEdge:
    """任务图中的一条依赖边：parent -> child。"""

    parent_id: str
    child_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanResult:
    """Planner 返回的初始计划。

    新版使用显式的 nodes + edges 图结构；保留 tasks 字段作为 legacy fallback。
    """

    summary: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[PlanNode] = field(default_factory=list)
    edges: list[PlanEdge] = field(default_factory=list)

    def has_graph(self) -> bool:
        return len(self.nodes) > 0


@dataclass
class PlanDelta:
    """显式 Plan Extension 提交的新增任务与依赖。"""

    add_tasks: list[dict[str, Any]] = field(default_factory=list)
    add_dependencies: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""
