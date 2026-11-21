"""A single user-owned goal on an existing session incarnation."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

GoalAction = Literal["set", "pause", "resume", "complete"]
GoalStatus = Literal["active", "paused", "completed"]

_MAX_OBJECTIVE_CHARS = 4000


class GoalVersionConflict(ValueError):
    """The client acted on a replaced goal or session."""


@dataclass(frozen=True, slots=True)
class SessionGoal:
    objective: str
    status: GoalStatus


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    version: str
    goal: SessionGoal | None


class SessionGoalStore:
    """Share the session transaction so goal versions cannot survive deletion or race updates."""

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

    @staticmethod
    def _read(conn, owner: str, session_id: str, incarnation: str | None = None) -> GoalSnapshot | None:
        row = conn.execute("""
            SELECT s.history_incarnation, g.version, g.objective, g.status
            FROM sessions AS s
            LEFT JOIN session_goals AS g
              ON g.owner_account_id = s.owner_account_id AND g.session_id = s.session_id
            WHERE s.owner_account_id = ? AND s.session_id = ?
              AND s.session_id NOT LIKE '%::%' AND s.source_session_id = '' AND s.archived = 0
        """, (owner, session_id)).fetchone()
        if row is None or (incarnation is not None and row[0] != incarnation):
            return None
        history_id, version, objective, status = row
        return GoalSnapshot(
            version or history_id,
            SessionGoal(objective, status) if version else None,
        )

    def read(self, owner: str, session_id: str, incarnation: str | None = None) -> GoalSnapshot | None:
        return self._store.read_query(lambda conn: self._read(conn, owner, session_id, incarnation))

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
            if current.version != version:
                raise GoalVersionConflict("目标或会话已更新，请重新读取后再操作")
            if action == "set":
                goal = SessionGoal(text, "active")
            else:
                previous = current.goal
                if previous is None:
                    raise ValueError("会话还没有目标")
                next_status: GoalStatus = {"pause": "paused", "resume": "active", "complete": "completed"}[action]
                if previous.status == next_status:
                    return current
                if previous.status == "completed":
                    raise ValueError("已完成的目标不能恢复，请设置新目标")
                goal = SessionGoal(previous.objective, next_status)
            if goal == current.goal:
                return current
            changed = GoalSnapshot(uuid.uuid4().hex, goal)
            conn.execute("""
                INSERT INTO session_goals (owner_account_id, session_id, version, objective, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner_account_id, session_id) DO UPDATE SET
                    version = excluded.version, objective = excluded.objective, status = excluded.status
            """, (owner, session_id, changed.version, goal.objective, goal.status))
            return changed

        return self._store.transaction(change)
