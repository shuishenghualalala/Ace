"""Versioned goals and one explicitly reserved step per conversation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

GoalAction = Literal["set", "pause", "resume", "complete"]
GoalStatus = Literal["active", "paused", "completed"]
StepTerminal = Literal["finished", "stopped", "failed", "interrupted"]
_MAX_OBJECTIVE_CHARS = 4000
STEP_REQUEST_PREFIX = "goal_"
_LIVE_STEPS = frozenset({"accepted", "running", "stopping"})


class GoalVersionConflict(ValueError):
    """The client acted on a replaced goal or session."""


class GoalStepConflict(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionGoal:
    objective: str
    status: GoalStatus


@dataclass(frozen=True, slots=True)
class GoalStep:
    request_id: str
    state: str
    code: str = ""


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    version: str
    goal: SessionGoal | None
    run: GoalStep | None = None


@dataclass(frozen=True, slots=True)
class ReservedGoalStep:
    owner: str
    session_id: str
    incarnation: str
    workspace_id: str
    request_id: str
    snapshot: GoalSnapshot


class SessionGoalStore:
    """Share the session transaction for identity, version, and step transitions."""

    def __init__(self, store):
        self._store = store

    @staticmethod
    def initialize(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_goals (
                owner_account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                version TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed')),
                PRIMARY KEY (owner_account_id, session_id),
                FOREIGN KEY (owner_account_id, session_id)
                    REFERENCES sessions (owner_account_id, session_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_goal_steps (
                owner_account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                history_incarnation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'accepted', 'running', 'stopping', 'finished', 'stopped', 'failed', 'interrupted'
                )),
                code TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (owner_account_id, session_id),
                FOREIGN KEY (owner_account_id, session_id)
                    REFERENCES session_goals (owner_account_id, session_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_goal_live_steps ON session_goal_steps(state)
            WHERE state IN ('accepted','running','stopping')
        """)

    @staticmethod
    def _read(conn, owner: str, session_id: str, incarnation: str | None = None) -> GoalSnapshot | None:
        row = conn.execute("""
            SELECT s.history_incarnation, g.version, g.objective, g.status, r.request_id, r.state, r.code
            FROM sessions AS s
            LEFT JOIN session_goals AS g USING (owner_account_id, session_id)
            LEFT JOIN session_goal_steps AS r ON r.owner_account_id=s.owner_account_id
              AND r.session_id=s.session_id AND r.history_incarnation=s.history_incarnation
            WHERE s.owner_account_id = ? AND s.session_id = ?
              AND s.session_id NOT LIKE '%::%' AND s.source_session_id = '' AND s.archived = 0
        """, (owner, session_id)).fetchone()
        if row is None or (incarnation is not None and row[0] != incarnation):
            return None
        history_id, version, objective, status, request_id, state, code = row
        return GoalSnapshot(
            version or history_id,
            SessionGoal(objective, status) if version else None,
            GoalStep(request_id, state, code) if request_id else None,
        )

    def read(self, owner: str, session_id: str, incarnation: str | None = None) -> GoalSnapshot | None:
        return self._store.read_query(lambda conn: self._read(conn, owner, session_id, incarnation))

    @staticmethod
    def _require_version(snapshot: GoalSnapshot, version: str) -> None:
        if snapshot.version != version:
            raise GoalVersionConflict("目标或会话已更新，请重新读取后再操作")

    def apply(
        self, owner: str, session_id: str, action: GoalAction, version: str, *, objective: str = "",
    ) -> GoalSnapshot | None:
        text = objective.strip()
        if action == "set" and not 1 <= len(text) <= _MAX_OBJECTIVE_CHARS:
            raise ValueError(f"目标长度必须为 1–{_MAX_OBJECTIVE_CHARS} 个字符")

        def change(conn):
            current = self._read(conn, owner, session_id)
            if current is None:
                return None
            self._require_version(current, version)
            run = current.run
            if action != "pause" and run is not None and run.state in _LIVE_STEPS:
                raise GoalStepConflict("goal_step_active", "请先暂停正在推进的目标并等待这轮收尾")
            if action == "set":
                goal = SessionGoal(text, "active")
            else:
                previous = current.goal
                if previous is None:
                    raise ValueError("会话还没有目标")
                next_status: GoalStatus = {"pause": "paused", "resume": "active", "complete": "completed"}[action]
                if previous.status == "completed" and previous.status != next_status:
                    raise ValueError("已完成的目标不能恢复，请设置新目标")
                goal = SessionGoal(previous.objective, next_status)
            if goal == current.goal:
                return current
            new_version = uuid.uuid4().hex
            conn.execute("""
                INSERT INTO session_goals (owner_account_id, session_id, version, objective, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner_account_id, session_id) DO UPDATE SET
                    version = excluded.version, objective = excluded.objective, status = excluded.status
            """, (owner, session_id, new_version, goal.objective, goal.status))
            if action == "set":
                conn.execute("DELETE FROM session_goal_steps WHERE owner_account_id=? AND session_id=?", (owner, session_id))
                run = None
            elif action == "pause" and run is not None and run.state in {"accepted", "running"}:
                state = "stopped" if run.state == "accepted" else "stopping"
                code = "paused_before_dispatch" if state == "stopped" else "pause_requested"
                conn.execute("""
                    UPDATE session_goal_steps SET state=?, code=? WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (state, code, owner, session_id, run.request_id))
                run = GoalStep(run.request_id, state, code)
            return GoalSnapshot(new_version, goal, run)

        return self._store.transaction(change)

    @staticmethod
    def _step_context(conn, owner: str, session_id: str, default_executor: str) -> tuple[str, str]:
        row = conn.execute("""
            SELECT s.history_incarnation, s.workspace_id, a.config_json, p.state_json
            FROM sessions AS s
            LEFT JOIN session_agent_config AS a USING (owner_account_id, session_id)
            LEFT JOIN plan_gateway_state AS p USING (owner_account_id, session_id)
            WHERE s.owner_account_id=? AND s.session_id=?
        """, (owner, session_id)).fetchone()
        incarnation, workspace_id, raw_config, raw_plan = row
        config = json.loads(raw_config) if raw_config else {}
        if (str(config.get("executor") or default_executor).strip().lower() != "builtin"
            or config.get("wiki_agent_session") or session_id.startswith("agent:main:")):
            raise GoalStepConflict("goal_unsupported_session", "只有内置主会话可以推进目标")
        if raw_plan and json.loads(raw_plan).get("active"):
            raise GoalStepConflict("goal_plan_active", "请先退出计划模式，再推进目标")
        pending = conn.execute("""
            SELECT 1 FROM desktop_send_inbox WHERE owner_account_id=? AND session_id=? AND status='accepted' LIMIT 1
        """, (owner, session_id)).fetchone()
        if pending:
            raise GoalStepConflict("goal_session_busy", "会话还有已接收的消息，暂不推进目标")
        return incarnation, workspace_id or "default"

    def reserve_step(
        self, owner: str, session_id: str, version: str, default_executor: str,
    ) -> ReservedGoalStep | None:
        def reserve(conn):
            current = self._read(conn, owner, session_id)
            if current is None:
                return None
            self._require_version(current, version)
            if current.run is not None and current.run.state in _LIVE_STEPS:
                raise GoalStepConflict("goal_step_active", "目标已在推进")
            if current.goal is None or current.goal.status != "active":
                raise GoalStepConflict("goal_not_active", "请先设置或恢复会话目标")
            incarnation, workspace = self._step_context(conn, owner, session_id, default_executor)
            request_id = f"{STEP_REQUEST_PREFIX}{uuid.uuid4().hex}"
            new_version = uuid.uuid4().hex
            conn.execute("""
                INSERT INTO session_goal_steps (owner_account_id,session_id,history_incarnation,request_id,state)
                VALUES (?,?,?,?,'accepted') ON CONFLICT(owner_account_id,session_id) DO UPDATE SET
                history_incarnation=excluded.history_incarnation, request_id=excluded.request_id, state='accepted', code=''
            """, (owner, session_id, incarnation, request_id))
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (new_version, owner, session_id))
            return ReservedGoalStep(owner, session_id, incarnation, workspace, request_id,
                                    GoalSnapshot(new_version, current.goal, GoalStep(request_id, "accepted")))

        return self._store.transaction(reserve)

    def claim_step(self, step: ReservedGoalStep, default_executor: str) -> None:
        def claim(conn):
            current = self._read(conn, step.owner, step.session_id, step.incarnation)
            if current is None or current.version != step.snapshot.version or current.run != step.snapshot.run:
                raise GoalStepConflict("goal_step_superseded", "目标推进已暂停或会话已变化")
            _, workspace = self._step_context(conn, step.owner, step.session_id, default_executor)
            if workspace != step.workspace_id:
                raise GoalStepConflict("goal_workspace_changed", "会话工作空间已变化，请重新推进目标")
            conn.execute("""
                UPDATE session_goal_steps SET state='running'
                WHERE owner_account_id=? AND session_id=? AND request_id=?
            """, (step.owner, step.session_id, step.request_id))

        self._store.transaction(claim)

    def finish_step(self, step: ReservedGoalStep, state: StepTerminal, code: str) -> bool:
        return self._store.transaction(lambda conn: conn.execute("""
            UPDATE session_goal_steps SET state=?, code=?
            WHERE owner_account_id=? AND session_id=? AND history_incarnation=? AND request_id=?
              AND state IN ('accepted','running','stopping')
        """, (state, code, step.owner, step.session_id, step.incarnation, step.request_id)).rowcount == 1)

    def recover_steps(self) -> list[str]:
        """Resolve abandoned steps before admission. No accepted step is replayable."""
        def recover(conn):
            conn.execute("""
                UPDATE session_goals SET status='paused', version=lower(hex(randomblob(16)))
                WHERE status='active' AND (owner_account_id,session_id) IN (
                    SELECT r.owner_account_id,r.session_id FROM session_goal_steps r
                    JOIN sessions s USING(owner_account_id,session_id)
                    WHERE r.state IN ('accepted','running','stopping') AND r.history_incarnation=s.history_incarnation
                )
            """)
            rows = conn.execute("""
                UPDATE session_goal_steps SET state='interrupted', code='gateway_restarted'
                WHERE state IN ('accepted','running','stopping') RETURNING request_id
            """).fetchall()
            return [row[0] for row in rows]

        return self._store.transaction(recover)
