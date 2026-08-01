"""Dynamic Kanban SQLite 持久化存储。

复用 crew/state/sqlite.py 的连接与写辅助，实现：
- Workflow / Task / Dependency / TaskRun / BoardEvent 的 CRUD
- CAS 原子认领
- 依赖拓扑查询与 promote
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from crew.dynamickanban.models import (
    BoardEvent,
    KanbanTask,
    PlanDelta,
    TaskRun,
    Workflow,
)
from crew.dynamickanban.runtime_models import RuntimeState
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = logging.getLogger(__name__)


def _is_path_under(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 下（解析后比较，处理符号链接）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class SQLiteKanbanStore:
    """SQLite 驱动的 Dynamic Kanban 持久化。"""

    def __init__(self, db_path: str | Path, wal_enabled: bool = True) -> None:
        self._db_path = str(db_path)
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.Lock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._owner_account_id = ""
        self._owns_connection = True
        self._init_schema()

    def for_owner(self, owner_account_id: str) -> SQLiteKanbanStore:
        """Return a lightweight owner-scoped view sharing this Store connection."""

        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("Dynamic Kanban Store 必须绑定 Owner")
        current_owner = str(getattr(self, "_owner_account_id", "") or "").strip()
        if current_owner:
            if current_owner != owner:
                raise ValueError("已绑定 Owner 的 Dynamic Kanban Store 不可切换 Owner")
            return self
        scoped = object.__new__(type(self))
        scoped._db_path = self._db_path
        scoped._conn = self._conn
        scoped._lock = self._lock
        scoped._writer = self._writer
        scoped._owner_account_id = owner
        scoped._owns_connection = False
        return scoped

    def _require_owner(self) -> str:
        owner = str(self._owner_account_id or "").strip()
        if not owner:
            raise ValueError("Dynamic Kanban Store 操作缺少 Owner scope")
        return owner

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        def _create(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_workflows (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL DEFAULT '',
                    isolation_state TEXT NOT NULL DEFAULT 'owned',
                    schema_version INTEGER NOT NULL DEFAULT 2,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    context TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._migrate_workflow_ownership(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_tasks (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    assignee TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_summary TEXT NOT NULL DEFAULT '',
                    artifact_paths TEXT NOT NULL DEFAULT '[]',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 2,
                    claimed_by TEXT,
                    claimed_at REAL,
                    done_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES kanban_workflows(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_dependencies (
                    parent_task_id TEXT NOT NULL,
                    child_task_id TEXT NOT NULL,
                    PRIMARY KEY (parent_task_id, child_task_id),
                    FOREIGN KEY (parent_task_id) REFERENCES kanban_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (child_task_id) REFERENCES kanban_tasks(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    agent_run_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    output TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    FOREIGN KEY (task_id) REFERENCES kanban_tasks(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_events (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'system',
                    payload TEXT NOT NULL DEFAULT '{}',
                    ts REAL NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES kanban_workflows(id) ON DELETE CASCADE
                )
                """
            )
            # 索引
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kanban_tasks_wf ON kanban_tasks(workflow_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kanban_tasks_status ON kanban_tasks(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kanban_events_wf ON kanban_events(workflow_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kanban_runs_task ON kanban_task_runs(task_id)"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kanban_workflows_owner_session
                ON kanban_workflows(owner_account_id, session_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kanban_runtime_states (
                    workflow_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES kanban_workflows(id) ON DELETE CASCADE
                )
                """
            )

        self._writer.execute(_create)

    @staticmethod
    def _migrate_workflow_ownership(conn: sqlite3.Connection) -> None:
        """Add Owner columns and quarantine legacy rows whose owner is not provable."""

        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(kanban_workflows)").fetchall()
        }
        additions = {
            "owner_account_id": "TEXT NOT NULL DEFAULT ''",
            "isolation_state": "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE kanban_workflows ADD COLUMN {name} {declaration}"
                )

        sessions_exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'sessions'
            """
        ).fetchone()
        if sessions_exists is None:
            conn.execute(
                """
                UPDATE kanban_workflows
                SET isolation_state = 'legacy_orphaned'
                WHERE owner_account_id = ''
                """
            )
            return

        conn.execute(
            """
            UPDATE kanban_workflows AS workflow
            SET owner_account_id = (
                    SELECT MIN(session.owner_account_id)
                    FROM sessions AS session
                    WHERE session.session_id = workflow.session_id
                      AND session.owner_account_id <> ''
                ),
                isolation_state = 'owned',
                schema_version = 2
            WHERE workflow.owner_account_id = ''
              AND (
                  SELECT COUNT(DISTINCT session.owner_account_id)
                  FROM sessions AS session
                  WHERE session.session_id = workflow.session_id
                    AND session.owner_account_id <> ''
              ) = 1
            """
        )
        conn.execute(
            """
            UPDATE kanban_workflows AS workflow
            SET isolation_state = CASE
                    WHEN (
                        SELECT COUNT(DISTINCT session.owner_account_id)
                        FROM sessions AS session
                        WHERE session.session_id = workflow.session_id
                          AND session.owner_account_id <> ''
                    ) > 1 THEN 'legacy_ambiguous'
                    ELSE 'legacy_orphaned'
                END
            WHERE workflow.owner_account_id = ''
            """
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_workflow(row: sqlite3.Row) -> Workflow:
        return Workflow(
            id=row["id"],
            session_id=row["session_id"],
            owner_account_id=row["owner_account_id"],
            title=row["title"],
            status=row["status"],
            context=json.loads(row["context"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> KanbanTask:
        return KanbanTask(
            id=row["id"],
            workflow_id=row["workflow_id"],
            title=row["title"],
            detail=row["detail"],
            assignee=row["assignee"],
            status=row["status"],
            result_summary=row["result_summary"],
            artifact_paths=json.loads(row["artifact_paths"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
            done_at=row["done_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            agent_run_id=row["agent_run_id"],
            status=row["status"],
            output=row["output"],
            error=row["error"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> BoardEvent:
        return BoardEvent(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            payload=json.loads(row["payload"]),
            ts=row["ts"],
        )

    def _ensure_workflow(self, workflow_id: str) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow 不存在: {workflow_id}")
        return workflow

    def _ensure_task(self, task_id: str) -> KanbanTask:
        return self.get_task(task_id)

    # ------------------------------------------------------------------ #
    # Workflow
    # ------------------------------------------------------------------ #
    def create_workflow(
        self,
        session_id: str,
        title: str,
        context: dict[str, Any] | None = None,
    ) -> Workflow:
        owner = self._require_owner()
        wf = Workflow(
            session_id=session_id,
            owner_account_id=owner,
            title=title,
            context=context or {},
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_workflows (
                    id, session_id, owner_account_id, isolation_state, schema_version,
                    title, status, context, created_at, updated_at
                ) VALUES (?, ?, ?, 'owned', 2, ?, ?, ?, ?, ?)
                """,
                (
                    wf.id,
                    wf.session_id,
                    wf.owner_account_id,
                    wf.title,
                    wf.status,
                    json.dumps(wf.context, ensure_ascii=False),
                    wf.created_at,
                    wf.updated_at,
                ),
            )

        self._writer.execute(_insert)
        return wf

    def create_workflow_graph(
        self,
        session_id: str,
        title: str,
        *,
        context: dict[str, Any],
        nodes: list[dict[str, Any]],
        edges: list[tuple[str, str]],
        event_type: str,
        event_payload: dict[str, Any],
        actor: str = "system",
    ) -> tuple[Workflow, dict[str, KanbanTask]]:
        """Atomically persist a workflow header, graph projection and seed event."""

        owner = self._require_owner()
        workflow = Workflow(
            session_id=session_id,
            owner_account_id=owner,
            title=title,
            context=dict(context),
        )
        tasks: dict[str, KanbanTask] = {}
        for raw in nodes:
            node_id = str(raw.get("id") or raw.get("node_id") or "").strip()
            if not node_id:
                continue
            tasks[node_id] = KanbanTask(
                workflow_id=workflow.id,
                title=str(raw.get("title") or node_id),
                detail=str(raw.get("detail") or ""),
                assignee=str(raw.get("assignee") or "leader"),
                status=str(raw.get("status") or "pending"),
                max_retries=max(0, int(raw.get("max_retries") or 2)),
            )
        node_task_ids = {node_id: task.id for node_id, task in tasks.items()}
        payload = {**dict(event_payload), "node_task_ids": node_task_ids}
        event = BoardEvent(
            workflow_id=workflow.id,
            event_type=event_type,
            actor=actor,
            payload=payload,
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_workflows (
                    id, session_id, owner_account_id, isolation_state, schema_version,
                    title, status, context, created_at, updated_at
                )
                VALUES (?, ?, ?, 'owned', 2, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.session_id,
                    workflow.owner_account_id,
                    workflow.title,
                    workflow.status,
                    json.dumps(workflow.context, ensure_ascii=False),
                    workflow.created_at,
                    workflow.updated_at,
                ),
            )
            for task in tasks.values():
                conn.execute(
                    """
                    INSERT INTO kanban_tasks
                    (id, workflow_id, title, detail, assignee, status, result_summary,
                     artifact_paths, retry_count, max_retries, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.id,
                        task.workflow_id,
                        task.title,
                        task.detail,
                        task.assignee,
                        task.status,
                        task.result_summary,
                        json.dumps(task.artifact_paths, ensure_ascii=False),
                        task.retry_count,
                        task.max_retries,
                        task.created_at,
                        task.updated_at,
                    ),
                )
            for parent_node_id, child_node_id in edges:
                parent_task_id = node_task_ids.get(parent_node_id)
                child_task_id = node_task_ids.get(child_node_id)
                if parent_task_id and child_task_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO kanban_dependencies (parent_task_id, child_task_id) VALUES (?, ?)",
                        (parent_task_id, child_task_id),
                    )
            conn.execute(
                """
                INSERT INTO kanban_events (id, workflow_id, task_id, event_type, actor, payload, ts)
                VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workflow_id,
                    event.event_type,
                    event.actor,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.ts,
                ),
            )

        self._writer.execute(_insert)
        return workflow, tasks

    def save_workflow_plan_revision(
        self,
        workflow_id: str,
        workflow_plan: dict[str, Any],
        *,
        reason: str,
        delta: dict[str, Any] | None = None,
        actor: str = "team_runtime",
    ) -> Workflow:
        """Atomically replace the current plan snapshot and append its revision event."""

        event = BoardEvent(
            workflow_id=workflow_id,
            event_type="workflow_plan_revised",
            actor=actor,
            payload={
                "revision": int(workflow_plan.get("revision") or 1),
                "reason": str(reason or "计划结构调整"),
                "delta": dict(delta or {}),
            },
        )

        def _update(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT context FROM kanban_workflows WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            context = json.loads(str(row["context"] or "{}"))
            context["workflow_plan"] = dict(workflow_plan)
            context["current_revision"] = int(workflow_plan.get("revision") or 1)
            conn.execute(
                "UPDATE kanban_workflows SET context = ?, updated_at = ? WHERE id = ?",
                (json.dumps(context, ensure_ascii=False), time.time(), workflow_id),
            )
            conn.execute(
                """
                INSERT INTO kanban_events (id, workflow_id, task_id, event_type, actor, payload, ts)
                VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workflow_id,
                    event.event_type,
                    event.actor,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.ts,
                ),
            )

        self._writer.execute(_update)
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        return workflow

    def apply_workflow_graph_revision(
        self,
        workflow_id: str,
        workflow_plan: dict[str, Any],
        *,
        added_node: dict[str, Any],
        node_task_ids: dict[str, str],
        edges: list[tuple[str, str]],
        reason: str,
        delta: dict[str, Any] | None = None,
        actor: str = "team_runtime",
    ) -> tuple[Workflow, KanbanTask]:
        """Atomically add one projected node, replace graph edges and save a plan revision."""

        node_id = str(added_node.get("id") or added_node.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("added workflow node requires id")
        task = KanbanTask(
            workflow_id=workflow_id,
            title=str(added_node.get("title") or node_id),
            detail=str(added_node.get("detail") or ""),
            assignee=str(added_node.get("assignee") or "leader"),
            status=str(added_node.get("status") or "pending"),
            max_retries=max(0, int(added_node.get("max_retries") or 2)),
        )
        all_task_ids = {**dict(node_task_ids), node_id: task.id}
        event = BoardEvent(
            workflow_id=workflow_id,
            task_id=task.id,
            event_type="workflow_plan_revised",
            actor=actor,
            payload={
                "revision": int(workflow_plan.get("revision") or 1),
                "reason": str(reason or "计划结构调整"),
                "delta": dict(delta or {}),
                "node_id": node_id,
                "node_task_ids": all_task_ids,
            },
        )

        def _update(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT context FROM kanban_workflows WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            context = json.loads(str(row["context"] or "{}"))
            context["workflow_plan"] = dict(workflow_plan)
            context["current_revision"] = int(workflow_plan.get("revision") or 1)
            conn.execute(
                """
                INSERT INTO kanban_tasks
                (id, workflow_id, title, detail, assignee, status, result_summary,
                 artifact_paths, retry_count, max_retries, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.workflow_id,
                    task.title,
                    task.detail,
                    task.assignee,
                    task.status,
                    task.result_summary,
                    json.dumps(task.artifact_paths, ensure_ascii=False),
                    task.retry_count,
                    task.max_retries,
                    task.created_at,
                    task.updated_at,
                ),
            )
            conn.execute(
                """
                DELETE FROM kanban_dependencies
                WHERE parent_task_id IN (SELECT id FROM kanban_tasks WHERE workflow_id = ?)
                   OR child_task_id IN (SELECT id FROM kanban_tasks WHERE workflow_id = ?)
                """,
                (workflow_id, workflow_id),
            )
            for parent_node_id, child_node_id in edges:
                parent_task_id = all_task_ids.get(parent_node_id)
                child_task_id = all_task_ids.get(child_node_id)
                if parent_task_id and child_task_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO kanban_dependencies (parent_task_id, child_task_id) VALUES (?, ?)",
                        (parent_task_id, child_task_id),
                    )
            conn.execute(
                "UPDATE kanban_workflows SET context = ?, updated_at = ? WHERE id = ?",
                (json.dumps(context, ensure_ascii=False), time.time(), workflow_id),
            )
            conn.execute(
                """
                INSERT INTO kanban_events (id, workflow_id, task_id, event_type, actor, payload, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workflow_id,
                    event.task_id,
                    event.event_type,
                    event.actor,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.ts,
                ),
            )

        self._writer.execute(_update)
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        return workflow, task

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        owner = self._require_owner()
        row = self._conn.execute(
            """
            SELECT * FROM kanban_workflows
            WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'
            """,
            (workflow_id, owner),
        ).fetchone()
        return self._row_to_workflow(row) if row else None

    def update_workflow_status(
        self,
        workflow_id: str,
        status: str,
        *,
        title: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Workflow:
        owner = self._require_owner()

        def _update(conn: sqlite3.Connection) -> None:
            parts: list[str] = ["status = ?"]
            params: list[Any] = [status]
            if title is not None:
                parts.append("title = ?")
                params.append(title)
            if context is not None:
                parts.append("context = ?")
                params.append(json.dumps(context, ensure_ascii=False))
            parts.append("updated_at = ?")
            params.append(time.time())
            params.append(workflow_id)
            params.append(owner)
            sql = (
                f"UPDATE kanban_workflows SET {', '.join(parts)} "
                "WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'"
            )
            cursor = conn.execute(sql, params)
            if cursor.rowcount != 1:
                raise KeyError(f"workflow 不存在: {workflow_id}")

        self._writer.execute(_update)
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow 不存在: {workflow_id}")
        return workflow

    def quarantine_workflow_definition(
        self,
        workflow_id: str,
        diagnostic: dict[str, Any],
    ) -> Workflow:
        """隔离无法无损迁移的 definition，并阻止该 Workflow 再次运行。

        原 definition 保留在 context 中供人工比对；诊断与 failed 状态在同一个写事务
        落库，避免下一次请求把冲突 Workflow 当作 active 继续复用。
        """

        owner = self._require_owner()

        def _quarantine(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                """
                SELECT context FROM kanban_workflows
                WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'
                """,
                (workflow_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(f"workflow 不存在: {workflow_id}")
            context = json.loads(row["context"])
            context["workflow_definition_quarantine"] = {
                **diagnostic,
                "quarantined_at": time.time(),
            }
            conn.execute(
                """
                UPDATE kanban_workflows
                SET status = 'failed', context = ?, updated_at = ?
                WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'
                """,
                (
                    json.dumps(context, ensure_ascii=False),
                    time.time(),
                    workflow_id,
                    owner,
                ),
            )

        self._writer.execute(_quarantine)
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow 不存在: {workflow_id}")
        return workflow

    def get_latest_workflow_by_session(
        self,
        session_id: str,
        *,
        exclude_source: str | None = None,
    ) -> Workflow | None:
        """获取某 session 最近创建的工作流。"""
        owner = self._require_owner()
        rows = self._conn.execute(
            """
            SELECT * FROM kanban_workflows
            WHERE session_id = ? AND owner_account_id = ? AND isolation_state = 'owned'
            ORDER BY created_at DESC
            """,
            (session_id, owner),
        ).fetchall()
        for row in rows:
            workflow = self._row_to_workflow(row)
            if exclude_source and str((workflow.context or {}).get("source") or "") == exclude_source:
                continue
            return workflow
        return None

    def list_workflows_by_session_prefix(self, session_id: str) -> list[Workflow]:
        """列出父 session 及其 per-turn 子 session 下的工作流。"""
        owner = self._require_owner()
        sid = str(session_id or "").strip()
        if not sid:
            return []
        prefix = f"{sid}::turn::%"
        rows = self._conn.execute(
            """
            SELECT * FROM kanban_workflows
            WHERE (session_id = ? OR session_id LIKE ?)
              AND owner_account_id = ? AND isolation_state = 'owned'
            ORDER BY created_at
            """,
            (sid, prefix, owner),
        ).fetchall()
        return [self._row_to_workflow(row) for row in rows]

    def get_latest_active_workflow_by_session(
        self,
        session_id: str,
        active_statuses: set[str] | None = None,
        *,
        exclude_source: str | None = None,
    ) -> Workflow | None:
        """获取某 session 最近一个仍处于 active 状态的工作流，用于复用目录。

        默认 active 状态为 active / paused（paused 可恢复）。
        """
        owner = self._require_owner()
        statuses = active_statuses or {"active", "paused"}
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"""
            SELECT * FROM kanban_workflows
            WHERE session_id = ? AND owner_account_id = ? AND isolation_state = 'owned'
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            """,
            (session_id, owner, *statuses),
        ).fetchall()
        for row in rows:
            workflow = self._row_to_workflow(row)
            if exclude_source and str((workflow.context or {}).get("source") or "") == exclude_source:
                continue
            return workflow
        return None

    def get_board_state(self, workflow_id: str) -> dict[str, Any]:
        """返回看板完整状态（任务、依赖、事件），供前端渲染。"""
        if self.get_workflow(workflow_id) is None:
            raise KeyError(f"workflow 不存在: {workflow_id}")
        tasks = [t.to_dict() for t in self.list_tasks(workflow_id)]
        deps = []
        for t in tasks:
            for parent_id in self.get_parent_task_ids(t["id"]):
                deps.append({"parent_task_id": parent_id, "child_task_id": t["id"]})
        events = [e.to_dict() for e in self.list_events(workflow_id)]
        workflow = self.get_workflow(workflow_id)
        return {
            "workflow_id": workflow_id,
            "workflow": workflow.to_dict() if workflow is not None else None,
            "workflow_plan": dict((workflow.context or {}).get("workflow_plan") or {}) if workflow is not None else {},
            "tasks": tasks,
            "dependencies": deps,
            "events": events,
        }

    # ------------------------------------------------------------------ #
    # Task
    # ------------------------------------------------------------------ #
    def add_task(
        self,
        workflow_id: str,
        title: str,
        detail: str = "",
        assignee: str | None = None,
        parent_task_ids: list[str] | None = None,
        status: str = "pending",
        max_retries: int = 2,
        auto_promote: bool = True,
    ) -> KanbanTask:
        self._ensure_workflow(workflow_id)
        for parent_id in parent_task_ids or []:
            parent = self._ensure_task(parent_id)
            if parent.workflow_id != workflow_id:
                raise ValueError("依赖任务必须属于同一 Owner 的同一 workflow")
        task = KanbanTask(
            workflow_id=workflow_id,
            title=title,
            detail=detail,
            assignee=assignee,
            status=status,
            max_retries=max_retries,
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_tasks
                (id, workflow_id, title, detail, assignee, status, result_summary,
                 artifact_paths, retry_count, max_retries, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.workflow_id,
                    task.title,
                    task.detail,
                    task.assignee,
                    task.status,
                    task.result_summary,
                    json.dumps(task.artifact_paths, ensure_ascii=False),
                    task.retry_count,
                    task.max_retries,
                    task.created_at,
                    task.updated_at,
                ),
            )
            for parent_id in parent_task_ids or []:
                conn.execute(
                    "INSERT OR IGNORE INTO kanban_dependencies (parent_task_id, child_task_id) VALUES (?, ?)",
                    (parent_id, task.id),
                )

        self._writer.execute(_insert)
        if auto_promote:
            self._maybe_promote(task.id)
        return self.get_task(task.id)

    def get_task(self, task_id: str) -> KanbanTask:
        owner = self._require_owner()
        row = self._conn.execute(
            """
            SELECT task.*
            FROM kanban_tasks AS task
            JOIN kanban_workflows AS workflow ON workflow.id = task.workflow_id
            WHERE task.id = ? AND workflow.owner_account_id = ?
              AND workflow.isolation_state = 'owned'
            """,
            (task_id, owner),
        ).fetchone()
        if not row:
            raise KeyError(f"任务不存在: {task_id}")
        return self._row_to_task(row)

    def list_tasks(self, workflow_id: str) -> list[KanbanTask]:
        self._ensure_workflow(workflow_id)
        rows = self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE workflow_id = ? ORDER BY created_at",
            (workflow_id,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_tasks_by_status(self, workflow_id: str, status: str) -> list[KanbanTask]:
        self._ensure_workflow(workflow_id)
        rows = self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE workflow_id = ? AND status = ? ORDER BY created_at",
            (workflow_id, status),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Dependency & promote
    # ------------------------------------------------------------------ #
    def add_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        parent = self._ensure_task(parent_task_id)
        child = self._ensure_task(child_task_id)
        if parent.workflow_id != child.workflow_id:
            raise ValueError("依赖任务必须属于同一 Owner 的同一 workflow")

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO kanban_dependencies (parent_task_id, child_task_id) VALUES (?, ?)",
                (parent_task_id, child_task_id),
            )

        self._writer.execute(_insert)
        self._maybe_promote(child_task_id)

    def replace_workflow_dependencies(
        self,
        workflow_id: str,
        managed_task_ids: set[str],
        dependencies: list[tuple[str, str]],
    ) -> None:
        """原子替换 Runtime 管理任务的依赖，同时保留显式扩展任务的边。"""
        task_ids = {task.id for task in self.list_tasks(workflow_id)}
        managed_ids = set(managed_task_ids)
        if not managed_ids:
            return
        if not managed_ids <= task_ids:
            raise ValueError("Runtime 投影必须引用同一 workflow 的已知任务")
        normalized = list(dict.fromkeys(dependencies))
        for parent_id, child_id in normalized:
            if parent_id not in managed_ids or child_id not in managed_ids:
                raise ValueError("Runtime 投影依赖必须引用受管理任务")
            if parent_id == child_id:
                raise ValueError("Board 依赖不能包含自环")

        def _replace(conn: sqlite3.Connection) -> None:
            placeholders = ", ".join("?" for _ in managed_ids)
            conn.execute(
                f"""
                DELETE FROM kanban_dependencies
                WHERE child_task_id IN ({placeholders})
                """,
                tuple(managed_ids),
            )
            conn.executemany(
                "INSERT INTO kanban_dependencies (parent_task_id, child_task_id) VALUES (?, ?)",
                normalized,
            )
            # 旧列表投影可能已把错误节点提升为 ready；依赖替换后按新 DAG 重新推导。
            conn.execute(
                f"""
                UPDATE kanban_tasks SET status = 'pending'
                WHERE id IN ({placeholders}) AND status = 'ready'
                """,
                tuple(managed_ids),
            )

        self._writer.execute(_replace)
        self.promote_all_pending(workflow_id)

    def get_parent_task_ids(self, task_id: str) -> list[str]:
        owner = self._require_owner()
        self._ensure_task(task_id)
        rows = self._conn.execute(
            """
            SELECT dependency.parent_task_id
            FROM kanban_dependencies AS dependency
            JOIN kanban_tasks AS parent ON parent.id = dependency.parent_task_id
            JOIN kanban_workflows AS workflow ON workflow.id = parent.workflow_id
            WHERE dependency.child_task_id = ?
              AND workflow.owner_account_id = ? AND workflow.isolation_state = 'owned'
            """,
            (task_id, owner),
        ).fetchall()
        return [r["parent_task_id"] for r in rows]

    def get_child_task_ids(self, task_id: str) -> list[str]:
        owner = self._require_owner()
        self._ensure_task(task_id)
        rows = self._conn.execute(
            """
            SELECT dependency.child_task_id
            FROM kanban_dependencies AS dependency
            JOIN kanban_tasks AS child ON child.id = dependency.child_task_id
            JOIN kanban_workflows AS workflow ON workflow.id = child.workflow_id
            WHERE dependency.parent_task_id = ?
              AND workflow.owner_account_id = ? AND workflow.isolation_state = 'owned'
            """,
            (task_id, owner),
        ).fetchall()
        return [r["child_task_id"] for r in rows]

    def _maybe_promote(self, task_id: str) -> bool:
        """如果 task_id 的所有父任务都 done，则把它从 pending 提升为 ready。"""
        owner = self._require_owner()
        self._ensure_task(task_id)

        def _promote(conn: sqlite3.Connection) -> bool:
            # 先检查当前状态
            row = conn.execute(
                """
                SELECT task.status
                FROM kanban_tasks AS task
                JOIN kanban_workflows AS workflow ON workflow.id = task.workflow_id
                WHERE task.id = ? AND workflow.owner_account_id = ?
                  AND workflow.isolation_state = 'owned'
                """,
                (task_id, owner),
            ).fetchone()
            if not row or row["status"] != "pending":
                return False
            # 是否存在未 done 的父任务
            undone = conn.execute(
                """
                SELECT 1 FROM kanban_dependencies d
                JOIN kanban_tasks t ON t.id = d.parent_task_id
                JOIN kanban_workflows w ON w.id = t.workflow_id
                WHERE d.child_task_id = ? AND t.status != 'done'
                  AND w.owner_account_id = ? AND w.isolation_state = 'owned'
                LIMIT 1
                """,
                (task_id, owner),
            ).fetchone()
            if undone:
                return False
            cursor = conn.execute(
                """
                UPDATE kanban_tasks SET status = 'ready', updated_at = ?
                WHERE id = ? AND workflow_id IN (
                    SELECT id FROM kanban_workflows
                    WHERE owner_account_id = ? AND isolation_state = 'owned'
                )
                """,
                (time.time(), task_id, owner),
            )
            return cursor.rowcount > 0

        return self._writer.execute(_promote)

    def promote_all_pending(self, workflow_id: str) -> int:
        """扫描 workflow 中所有 pending 任务，满足依赖的 promote 为 ready。返回 promote 数量。"""
        pending = self.list_tasks_by_status(workflow_id, "pending")
        promoted = 0
        for task in pending:
            if self._maybe_promote(task.id):
                promoted += 1
        return promoted

    # ------------------------------------------------------------------ #
    # Claim & status
    # ------------------------------------------------------------------ #
    def claim_ready_task(self, task_id: str, run_id: str) -> KanbanTask | None:
        """CAS 认领：只有 status='ready' 且无 claimed_by 时才成功。"""
        owner = self._require_owner()
        self._ensure_task(task_id)

        def _claim(conn: sqlite3.Connection) -> bool:
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE kanban_tasks
                SET status = 'running', claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'ready' AND claimed_by IS NULL
                  AND workflow_id IN (
                      SELECT id FROM kanban_workflows
                      WHERE owner_account_id = ? AND isolation_state = 'owned'
                  )
                """,
                (run_id, now, now, task_id, owner),
            )
            return cursor.rowcount > 0

        if not self._writer.execute(_claim):
            return None
        return self.get_task(task_id)

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        result_summary: str | None = None,
        artifacts: list[str] | None = None,
        reset_retry: bool = False,
    ) -> KanbanTask:
        owner = self._require_owner()
        self._ensure_task(task_id)
        now = time.time()

        def _update(conn: sqlite3.Connection) -> None:
            fields = ["status = ?, updated_at = ?"]
            params: list[Any] = [status, now]
            if result_summary is not None:
                fields.append("result_summary = ?")
                params.append(result_summary)
            if artifacts is not None:
                fields.append("artifact_paths = ?")
                params.append(json.dumps(artifacts, ensure_ascii=False))
            if status == "done":
                fields.append("done_at = ?")
                params.append(now)
                fields.append("claimed_by = NULL")
                fields.append("claimed_at = NULL")
            if status in ("failed", "blocked"):
                fields.append("retry_count = retry_count + 1")
                fields.append("claimed_by = NULL")
                fields.append("claimed_at = NULL")
            if reset_retry:
                fields.append("retry_count = 0")
            params.append(task_id)
            params.append(owner)
            sql = (
                f"UPDATE kanban_tasks SET {', '.join(fields)} WHERE id = ? "
                "AND workflow_id IN ("
                "SELECT id FROM kanban_workflows "
                "WHERE owner_account_id = ? AND isolation_state = 'owned'"
                ")"
            )
            cursor = conn.execute(sql, params)
            if cursor.rowcount != 1:
                raise KeyError(f"任务不存在: {task_id}")

        self._writer.execute(_update)
        task = self.get_task(task_id)
        # 如果任务完成，尝试 promote 它的子任务
        if status == "done":
            for child_id in self.get_child_task_ids(task_id):
                self._maybe_promote(child_id)
        return task

    def reset_failed_to_ready(self, workflow_id: str) -> int:
        """把所有 failed 且未超限的任务重新置为 ready；超限则 blocked。返回改为 ready 的数量。"""
        self._ensure_workflow(workflow_id)

        def _reset(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE kanban_tasks
                SET status = CASE
                    WHEN retry_count >= max_retries THEN 'blocked'
                    ELSE 'ready'
                END,
                claimed_by = NULL,
                claimed_at = NULL,
                updated_at = ?
                WHERE workflow_id = ? AND status = 'failed'
                """,
                (time.time(), workflow_id),
            )

        self._writer.execute(_reset)
        # 再把 pending 中依赖已完成的 promote
        return self.promote_all_pending(workflow_id)

    def get_ready_tasks(self, workflow_id: str, limit: int = 50) -> list[KanbanTask]:
        self._ensure_workflow(workflow_id)
        rows = self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE workflow_id = ? AND status = 'ready' ORDER BY created_at LIMIT ?",
            (workflow_id, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def count_tasks(self, workflow_id: str) -> dict[str, int]:
        self._ensure_workflow(workflow_id)
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS c FROM kanban_tasks WHERE workflow_id = ? GROUP BY status",
            (workflow_id,),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def is_workflow_active(self, workflow_id: str) -> bool:
        counts = self.count_tasks(workflow_id)
        active_statuses = {"pending", "ready", "running", "blocked"}
        return any(counts.get(s, 0) > 0 for s in active_statuses)

    # ------------------------------------------------------------------ #
    # TaskRun
    # ------------------------------------------------------------------ #
    def start_run(self, task_id: str, agent_run_id: str) -> TaskRun:
        self._ensure_task(task_id)
        run = TaskRun(task_id=task_id, agent_run_id=agent_run_id)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_task_runs (id, task_id, agent_run_id, status, output, error, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run.id, run.task_id, run.agent_run_id, run.status, run.output, run.error, run.started_at),
            )

        self._writer.execute(_insert)
        return run

    def finish_run(self, run_id: str, status: str, output: str = "", error: str = "") -> TaskRun:
        owner = self._require_owner()

        def _update(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE kanban_task_runs
                SET status = ?, output = ?, error = ?, finished_at = ?
                WHERE id = ? AND task_id IN (
                    SELECT task.id
                    FROM kanban_tasks AS task
                    JOIN kanban_workflows AS workflow ON workflow.id = task.workflow_id
                    WHERE workflow.owner_account_id = ?
                      AND workflow.isolation_state = 'owned'
                )
                """,
                (status, output, error, time.time(), run_id, owner),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run 不存在: {run_id}")

        self._writer.execute(_update)
        row = self._conn.execute(
            """
            SELECT run.*
            FROM kanban_task_runs AS run
            JOIN kanban_tasks AS task ON task.id = run.task_id
            JOIN kanban_workflows AS workflow ON workflow.id = task.workflow_id
            WHERE run.id = ? AND workflow.owner_account_id = ?
              AND workflow.isolation_state = 'owned'
            """,
            (run_id, owner),
        ).fetchone()
        if row is None:
            raise KeyError(f"run 不存在: {run_id}")
        return self._row_to_run(row)

    def list_runs(self, task_id: str) -> list[TaskRun]:
        self._ensure_task(task_id)
        rows = self._conn.execute(
            "SELECT * FROM kanban_task_runs WHERE task_id = ? ORDER BY started_at DESC",
            (task_id,),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def add_event(
        self,
        workflow_id: str,
        event_type: str,
        task_id: str | None = None,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> BoardEvent:
        self._ensure_workflow(workflow_id)
        if task_id is not None:
            task = self._ensure_task(task_id)
            if task.workflow_id != workflow_id:
                raise ValueError("事件任务必须属于同一 Owner 的 workflow")
        evt = BoardEvent(
            workflow_id=workflow_id,
            event_type=event_type,
            task_id=task_id,
            actor=actor,
            payload=payload or {},
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_events (id, workflow_id, task_id, event_type, actor, payload, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evt.id,
                    evt.workflow_id,
                    evt.task_id,
                    evt.event_type,
                    evt.actor,
                    json.dumps(evt.payload, ensure_ascii=False),
                    evt.ts,
                ),
            )

        self._writer.execute(_insert)
        return evt

    def list_events(self, workflow_id: str, limit: int = 200) -> list[BoardEvent]:
        self._ensure_workflow(workflow_id)
        rows = self._conn.execute(
            "SELECT * FROM kanban_events WHERE workflow_id = ? ORDER BY ts DESC LIMIT ?",
            (workflow_id, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Explicit Plan Extension
    # ------------------------------------------------------------------ #
    def apply_plan_extension(self, workflow_id: str, delta: PlanDelta) -> list[KanbanTask]:
        """应用已校验的显式计划扩展，只允许新增任务和依赖。"""
        self._ensure_workflow(workflow_id)
        added: list[KanbanTask] = []

        for t in delta.add_tasks:
            added.append(
                self.add_task(
                    workflow_id=workflow_id,
                    title=t["title"],
                    detail=t.get("detail", ""),
                    assignee=t.get("assignee"),
                    parent_task_ids=t.get("parent_task_ids"),
                    status=t.get("status", "pending"),
                    max_retries=t.get("max_retries", 2),
                )
            )
        for parent_id, child_id in delta.add_dependencies:
            try:
                self.get_task(parent_id)
                self.get_task(child_id)
            except KeyError:
                log.warning("计划扩展依赖 (%s -> %s) 指向不存在任务，跳过", parent_id, child_id)
                continue
            self.add_dependency(parent_id, child_id)
        self.promote_all_pending(workflow_id)
        return added

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    def clear_session(
        self,
        session_id: str,
        *,
        allowed_root: Path | None = None,
        allowed_roots: list[Path] | None = None,
        remove_workdirs: bool = True,
    ) -> list[Path]:
        """删除某 session 下的所有 workflow 数据（级联删除任务、依赖、运行、事件）。

        可选同步删除 workflow 工作目录，目录必须位于允许根目录列表中的某一个下才会被删除，防止误删。
        ``allowed_root`` 保留用于向后兼容；``allowed_roots`` 优先级更高。
        返回被删除的目录列表。
        """
        owner = self._require_owner()
        removed: list[Path] = []
        if remove_workdirs:
            if allowed_roots is not None:
                allowed = list(allowed_roots)
            elif allowed_root is not None:
                allowed = [allowed_root]
            else:
                from crew.state.home import get_task_workspace_root

                allowed = [get_task_workspace_root(create=False)]
            # 预解析允许根目录，避免循环里重复 IO
            resolved_allowed: list[Path] = []
            for root in allowed:
                try:
                    resolved_allowed.append(Path(root).expanduser().resolve())
                except (OSError, ValueError) as exc:
                    log.warning("clear_session 允许根目录无效 %s: %s", root, exc)
            for wf in self.list_workflows_by_session(session_id):
                workdir = wf.context.get("workflow_workdir") if wf.context else None
                if not workdir:
                    continue
                path = Path(workdir).resolve()
                # 安全校验：必须落在任一允许的任务产物根目录下
                if not any(
                    _is_path_under(path, allowed_path)
                    for allowed_path in resolved_allowed
                ):
                    log.warning(
                        "workflow %s 的工作目录 %s 不在允许根 %s 下，跳过删除",
                        wf.id,
                        path,
                        allowed,
                    )
                    continue
                if path.exists():
                    try:
                        shutil.rmtree(path)
                        removed.append(path)
                        log.info("已删除 workflow %s 工作目录 %s", wf.id, path)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("删除 workflow %s 工作目录 %s 失败: %s", wf.id, path, exc)

        def _delete(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                DELETE FROM kanban_workflows
                WHERE session_id = ? AND owner_account_id = ?
                  AND isolation_state = 'owned'
                """,
                (session_id, owner),
            )

        self._writer.execute(_delete)
        return removed

    def list_workflows_by_session(self, session_id: str) -> list[Workflow]:
        """列出某 session 下的所有 workflow。"""
        owner = self._require_owner()
        rows = self._conn.execute(
            """
            SELECT * FROM kanban_workflows
            WHERE session_id = ? AND owner_account_id = ? AND isolation_state = 'owned'
            ORDER BY created_at DESC
            """,
            (session_id, owner),
        ).fetchall()
        return [self._row_to_workflow(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Runtime state (pause/resume)
    # ------------------------------------------------------------------ #
    def save_runtime_state(self, state: RuntimeState) -> None:
        """持久化 workflow runtime 状态。"""
        self._ensure_workflow(state.workflow_id)
        state.updated_at = time.time()

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO kanban_runtime_states (workflow_id, state, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (
                    state.workflow_id,
                    json.dumps(state.to_dict(), ensure_ascii=False),
                    state.updated_at,
                ),
            )

        self._writer.execute(_upsert)

    def load_runtime_state(self, workflow_id: str) -> RuntimeState | None:
        """读取 workflow runtime 状态；不存在返回 None。"""
        if self.get_workflow(workflow_id) is None:
            return None
        row = self._conn.execute(
            "SELECT state FROM kanban_runtime_states WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["state"])
            return RuntimeState.from_dict(data)
        except (json.JSONDecodeError, TypeError):
            log.warning("runtime_state 解析失败 workflow=%s", workflow_id)
            return None

    def pause_workflow(self, workflow_id: str, reason: str = "") -> Workflow:
        """Atomically pause a non-terminal workflow while preserving its context."""

        return self._set_workflow_pause_state(workflow_id, paused=True, reason=reason)

    def resume_workflow(self, workflow_id: str) -> Workflow:
        """Atomically resume a workflow while removing only ``pause_reason``."""

        return self._set_workflow_pause_state(workflow_id, paused=False)

    def _set_workflow_pause_state(
        self,
        workflow_id: str,
        *,
        paused: bool,
        reason: str = "",
    ) -> Workflow:
        """Read, merge, and persist pause state in one SQLite write transaction."""
        owner = self._require_owner()

        def _update(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                """
                SELECT status, context FROM kanban_workflows
                WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'
                """,
                (workflow_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(f"workflow 不存在: {workflow_id}")
            if str(row["status"]) in {"done", "failed"}:
                raise ValueError(f"workflow 已是终态，不能暂停或恢复: {workflow_id}")

            context = json.loads(row["context"] or "{}")
            if not isinstance(context, dict):
                raise ValueError(f"workflow context 不是对象: {workflow_id}")
            if paused:
                context["pause_reason"] = reason
            else:
                context.pop("pause_reason", None)
            conn.execute(
                """
                UPDATE kanban_workflows
                SET status = ?, context = ?, updated_at = ?
                WHERE id = ? AND owner_account_id = ? AND isolation_state = 'owned'
                """,
                (
                    "paused" if paused else "active",
                    json.dumps(context, ensure_ascii=False),
                    time.time(),
                    workflow_id,
                    owner,
                ),
            )

        self._writer.execute(_update)
        workflow = self.get_workflow(workflow_id)
        if workflow is None:  # Defensive: the row cannot disappear through this store's write lock.
            raise KeyError(f"workflow 不存在: {workflow_id}")
        return workflow
