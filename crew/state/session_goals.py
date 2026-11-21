"""Versioned goals and one explicitly reserved step per conversation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

GoalAction = Literal["set", "pause", "resume", "complete"]
GoalStatus = Literal["active", "paused", "completed"]
StepTerminal = Literal["finished", "stopped", "failed", "interrupted"]
VerificationTerminal = Literal["passed", "incomplete", "unconfirmed", "interrupted"]
ContinuationTerminal = Literal["passed", "exhausted", "stopped", "yielded", "failed", "interrupted"]
_MAX_OBJECTIVE_CHARS = 4000
_VERIFICATION_HISTORY_LIMIT = 24
STEP_REQUEST_PREFIX = "goal_"
_CONTINUE_REQUEST_PREFIX = "continue_"
MAX_CONTINUE_BUDGET = 12
DEFAULT_CONTINUE_BUDGET = 4
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
class GoalVerification:
    request_id: str
    step_request_id: str
    state: str
    code: str = ""
    evidence_sequences: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class GoalContinuation:
    request_id: str
    state: str
    code: str = ""
    iterations: int = 0
    budget: int = 0
    step_request_id: str = ""
    verification_request_id: str = ""


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    version: str
    goal: SessionGoal | None
    run: GoalStep | None = None
    verification: GoalVerification | None = None
    continuation: GoalContinuation | None = None


@dataclass(frozen=True, slots=True)
class GoalEvidence:
    seq: int
    role: str
    text: str
    step_reply: bool = False


@dataclass(frozen=True, slots=True)
class ReservedGoalVerification:
    owner: str
    session_id: str
    incarnation: str
    workspace_id: str
    model_profile_id: str
    owner_lease: float
    history_stamp: tuple[int, int, int]
    version: str
    objective: str
    check: GoalVerification
    evidence: tuple[GoalEvidence, ...]


@dataclass(frozen=True, slots=True)
class ReservedGoalStep:
    owner: str
    session_id: str
    incarnation: str
    workspace_id: str
    request_id: str
    snapshot: GoalSnapshot


@dataclass(frozen=True, slots=True)
class ReservedGoalContinuation:
    owner: str
    session_id: str
    incarnation: str
    request_id: str
    budget: int
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_goal_verifications (
                owner_account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                step_request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('running','passed','incomplete','unconfirmed','interrupted')),
                code TEXT NOT NULL DEFAULT '',
                evidence_sequences TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (owner_account_id, session_id),
                FOREIGN KEY (owner_account_id, session_id)
                    REFERENCES session_goal_steps (owner_account_id, session_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_goal_live_verifications ON session_goal_verifications(state)
            WHERE state='running'
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_goal_continues (
                owner_account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                history_incarnation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'running', 'passed', 'exhausted', 'stopped', 'yielded', 'failed', 'interrupted'
                )),
                code TEXT NOT NULL DEFAULT '',
                iterations INTEGER NOT NULL DEFAULT 0,
                budget INTEGER NOT NULL,
                step_request_id TEXT NOT NULL DEFAULT '',
                verification_request_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (owner_account_id, session_id),
                FOREIGN KEY (owner_account_id, session_id)
                    REFERENCES session_goals (owner_account_id, session_id) ON DELETE CASCADE
            )
        """)

    @staticmethod
    def _read(conn, owner: str, session_id: str, incarnation: str | None = None) -> GoalSnapshot | None:
        row = conn.execute("""
            SELECT s.history_incarnation, g.version, g.objective, g.status, r.request_id, r.state, r.code,
                   v.request_id, v.state, v.code, v.evidence_sequences,
                   c.request_id, c.state, c.code, c.iterations, c.budget, c.step_request_id,
                   c.verification_request_id
            FROM sessions AS s
            LEFT JOIN session_goals AS g USING (owner_account_id, session_id)
            LEFT JOIN session_goal_steps AS r ON r.owner_account_id=s.owner_account_id
              AND r.session_id=s.session_id AND r.history_incarnation=s.history_incarnation
            LEFT JOIN session_goal_verifications AS v ON v.owner_account_id=r.owner_account_id
              AND v.session_id=r.session_id AND v.step_request_id=r.request_id
            LEFT JOIN session_goal_continues AS c ON c.owner_account_id=s.owner_account_id
              AND c.session_id=s.session_id AND c.history_incarnation=s.history_incarnation
            WHERE s.owner_account_id = ? AND s.session_id = ?
              AND s.session_id NOT LIKE '%::%' AND s.source_session_id = '' AND s.archived = 0
        """, (owner, session_id)).fetchone()
        if row is None or (incarnation is not None and row[0] != incarnation):
            return None
        (history_id, version, objective, status, request_id, state, code, check_id, check_state, check_code,
         evidence, continue_id, continue_state, continue_code, continue_iterations, continue_budget,
         continue_step, continue_check) = row
        return GoalSnapshot(
            version or history_id,
            SessionGoal(objective, status) if version else None,
            GoalStep(request_id, state, code) if request_id else None,
            GoalVerification(check_id, request_id, check_state, check_code, tuple(json.loads(evidence))) if check_id else None,
            GoalContinuation(continue_id, continue_state, continue_code, continue_iterations, continue_budget,
                             continue_step, continue_check) if continue_id else None,
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
            verification = current.verification
            continuation = current.continuation
            if action != "pause":
                if run is not None and run.state in _LIVE_STEPS:
                    raise GoalStepConflict("goal_step_active", "请先暂停正在推进的目标并等待这轮收尾")
                if verification is not None and verification.state == "running":
                    raise GoalStepConflict("goal_verification_active", "请等待验收完成或先暂停目标")
                if continuation is not None and continuation.state == "running":
                    raise GoalStepConflict("goal_continue_active", "请先停止自动推进目标")
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
                conn.execute("DELETE FROM session_goal_continues WHERE owner_account_id=? AND session_id=?", (owner, session_id))
            elif verification is not None and verification.state == "running":
                conn.execute("""
                    UPDATE session_goal_verifications SET state='interrupted', code='goal_changed'
                    WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (owner, session_id, verification.request_id))
            if action == "pause" and continuation is not None and continuation.state == "running":
                conn.execute("""
                    UPDATE session_goal_continues SET state='stopped', code='goal_continue_paused'
                    WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (owner, session_id, continuation.request_id))
            if action == "pause" and run is not None and run.state in {"accepted", "running"}:
                state = "stopped" if run.state == "accepted" else "stopping"
                code = "paused_before_dispatch" if state == "stopped" else "pause_requested"
                conn.execute("""
                    UPDATE session_goal_steps SET state=?, code=? WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (state, code, owner, session_id, run.request_id))
            return self._read(conn, owner, session_id)

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
            if current.verification is not None and current.verification.state == "running":
                raise GoalStepConflict("goal_verification_active", "目标正在验收，请等待或暂停")
            if current.goal is None or current.goal.status != "active":
                raise GoalStepConflict("goal_not_active", "请先设置或恢复会话目标")
            incarnation, workspace = self._step_context(conn, owner, session_id, default_executor)
            request_id = f"{STEP_REQUEST_PREFIX}{uuid.uuid4().hex}"
            new_version = uuid.uuid4().hex
            conn.execute("DELETE FROM session_goal_verifications WHERE owner_account_id=? AND session_id=?", (owner, session_id))
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

    @staticmethod
    def _verification_history_stamp(conn, owner: str, session_id: str) -> tuple[int, int, int]:
        leaf, generation, rewinds = conn.execute("""
            SELECT leaf_seq, events_generation, history_rewinds FROM sessions
            WHERE owner_account_id=? AND session_id=?
        """, (owner, session_id)).fetchone()
        return leaf, generation, rewinds

    @staticmethod
    def _verification_owner_stamp(conn, owner: str) -> float | None:
        # The Gateway lease and session store share the same SQLite database.
        row = conn.execute("""
            SELECT claimed_at FROM owner_session_lease WHERE owner_account_id=?
              AND NOT EXISTS (SELECT 1 FROM owner_logout_intent WHERE owner_account_id=?)
        """, (owner, owner)).fetchone()
        return row[0] if row is not None else None

    @staticmethod
    def archive_goal_in_transaction(conn, owner: str, session_id: str) -> None:
        changed = conn.execute("""
            UPDATE session_goal_verifications SET state='interrupted', code='goal_session_archived'
            WHERE owner_account_id=? AND session_id=? AND state='running'
        """, (owner, session_id)).rowcount
        changed += conn.execute("""
            UPDATE session_goal_continues SET state='interrupted', code='goal_session_archived'
            WHERE owner_account_id=? AND session_id=? AND state='running'
        """, (owner, session_id)).rowcount
        if changed:
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (uuid.uuid4().hex, owner, session_id))

    def reserve_verification(
        self, owner: str, session_id: str, version: str, default_executor: str,
    ) -> tuple[ReservedGoalVerification, GoalSnapshot] | None:
        def reserve(conn):
            current = self._read(conn, owner, session_id)
            if current is None:
                return None
            self._require_version(current, version)
            if current.goal is None or current.goal.status != "active":
                raise GoalStepConflict("goal_not_active", "请先设置或恢复会话目标")
            if current.run is None or current.run.state != "finished":
                raise GoalStepConflict("goal_verification_no_step", "请先完成一轮目标推进")
            if current.verification is not None and current.verification.state == "running":
                raise GoalStepConflict("goal_verification_active", "目标正在验收")
            incarnation, workspace = self._step_context(conn, owner, session_id, default_executor)
            stamp = self._verification_history_stamp(conn, owner, session_id)
            owner_lease = self._verification_owner_stamp(conn, owner)
            if owner_lease is None:
                raise GoalStepConflict("goal_verification_scope_lost", "账号已退出，无法验收目标")
            config = self._store.get_agent_config(session_id, owner_account_id=owner) or {}
            rows, _, _ = self._store.load_window(session_id, owner_account_id=owner, limit=_VERIFICATION_HISTORY_LIMIT)
            step_id = current.run.request_id
            evidence = tuple(
                GoalEvidence(seq, message.role, message.content,
                             message.role == "assistant" and message.request_id == step_id)
                for source, seq, message in rows
                if source == session_id and not message.is_meta and message.content.strip()
                and message.role in {"assistant", "user", "tool"}
            )
            check = GoalVerification(f"verify_{uuid.uuid4().hex}", step_id, "running")
            new_version = uuid.uuid4().hex
            conn.execute("""
                INSERT INTO session_goal_verifications (owner_account_id,session_id,request_id,step_request_id,state)
                VALUES (?,?,?,?,'running') ON CONFLICT(owner_account_id,session_id) DO UPDATE SET
                request_id=excluded.request_id, step_request_id=excluded.step_request_id,
                state='running', code='', evidence_sequences='[]'
            """, (owner, session_id, check.request_id, step_id))
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (new_version, owner, session_id))
            reservation = ReservedGoalVerification(owner, session_id, incarnation, workspace,
                                                   str(config.get("model_profile_id") or "inherit"), owner_lease, stamp,
                                                   new_version, current.goal.objective, check, evidence)
            return reservation, replace(current, version=new_version, verification=check)

        return self._store.transaction(reserve)

    def finish_verification(
        self, check: ReservedGoalVerification, state: VerificationTerminal, code: str,
        evidence_sequences: tuple[int, ...], default_executor: str, cancelled: Callable[[], bool],
    ) -> GoalSnapshot | None:
        def finish(conn):
            owner, sid = check.owner, check.session_id
            current = self._read(conn, owner, sid, check.incarnation)
            if (current is None or current.version != check.version or current.verification != check.check):
                return current
            terminal, reason, citations = state, code, evidence_sequences
            try:
                _, workspace = self._step_context(conn, owner, sid, default_executor)
                same_history = self._verification_history_stamp(conn, owner, sid) == check.history_stamp
                if not same_history or workspace != check.workspace_id:
                    terminal, reason, citations = "interrupted", "goal_evidence_changed", ()
            except GoalStepConflict as exc:
                terminal, reason, citations = "interrupted", exc.code, ()
            if cancelled() or self._verification_owner_stamp(conn, owner) != check.owner_lease:
                terminal, reason, citations = "interrupted", "goal_verification_scope_lost", ()
            if terminal == "passed":
                permitted = {item.seq for item in check.evidence if item.role in {"assistant", "tool"}}
                current_step = {item.seq for item in check.evidence if item.step_reply}
                if not set(citations) <= permitted or not current_step.intersection(citations):
                    terminal, reason, citations = "unconfirmed", "goal_verification_bad_citations", ()
            conn.execute("""
                UPDATE session_goal_verifications SET state=?, code=?, evidence_sequences=?
                WHERE owner_account_id=? AND session_id=? AND request_id=?
            """, (terminal, reason, json.dumps(citations), owner, sid, check.check.request_id))
            conn.execute("""
                UPDATE session_goals SET version=?, status=CASE WHEN ? THEN 'completed' ELSE status END
                WHERE owner_account_id=? AND session_id=?
            """, (uuid.uuid4().hex, terminal == "passed", owner, sid))
            return self._read(conn, owner, sid)

        return self._store.transaction(finish)

    def start_continuation(
        self, owner: str, session_id: str, version: str, budget: int, default_executor: str,
    ) -> tuple[ReservedGoalContinuation, GoalSnapshot] | None:
        def start(conn):
            current = self._read(conn, owner, session_id)
            if current is None:
                return None
            self._require_version(current, version)
            if current.goal is None or current.goal.status != "active":
                raise GoalStepConflict("goal_not_active", "请先设置或恢复会话目标")
            if current.run is not None and current.run.state in _LIVE_STEPS:
                raise GoalStepConflict("goal_step_active", "本轮目标还在推进，请先等待或暂停")
            if current.verification is not None and current.verification.state == "running":
                raise GoalStepConflict("goal_verification_active", "目标正在验收，请等待或暂停")
            if current.continuation is not None and current.continuation.state == "running":
                raise GoalStepConflict("goal_continue_active", "目标已在自动推进")
            incarnation, _ = self._step_context(conn, owner, session_id, default_executor)
            request_id = f"{_CONTINUE_REQUEST_PREFIX}{uuid.uuid4().hex}"
            bounded = max(1, min(int(budget), MAX_CONTINUE_BUDGET))
            new_version = uuid.uuid4().hex
            conn.execute("""
                INSERT INTO session_goal_continues
                    (owner_account_id, session_id, history_incarnation, request_id, state, iterations, budget)
                VALUES (?, ?, ?, ?, 'running', 0, ?)
                ON CONFLICT (owner_account_id, session_id) DO UPDATE SET
                    history_incarnation=excluded.history_incarnation, request_id=excluded.request_id,
                    state='running', code='', iterations=0, budget=excluded.budget,
                    step_request_id='', verification_request_id=''
            """, (owner, session_id, incarnation, request_id, bounded))
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (new_version, owner, session_id))
            continuation = GoalContinuation(request_id, "running", "", 0, bounded)
            snapshot = replace(current, version=new_version, continuation=continuation)
            return ReservedGoalContinuation(owner, session_id, incarnation, request_id, bounded, snapshot), snapshot

        return self._store.transaction(start)

    def record_continuation(
        self, continuation: ReservedGoalContinuation, iterations: int, step_request_id: str,
        verification_request_id: str,
    ) -> None:
        self._store.transaction(lambda conn: conn.execute("""
            UPDATE session_goal_continues SET iterations=?, step_request_id=?, verification_request_id=?
            WHERE owner_account_id=? AND session_id=? AND history_incarnation=? AND request_id=? AND state='running'
        """, (iterations, step_request_id, verification_request_id, continuation.owner, continuation.session_id,
              continuation.incarnation, continuation.request_id)))

    def stop_continuation(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        def stop(conn):
            current = self._read(conn, owner, session_id)
            if current is None:
                return None
            self._require_version(current, version)
            continuation = current.continuation
            if continuation is None or continuation.state != "running":
                raise GoalStepConflict("goal_continue_inactive", "目标没有正在自动推进")
            run = current.run
            if run is not None and run.state in {"accepted", "running"}:
                state = "stopped" if run.state == "accepted" else "stopping"
                code = "stopped_before_dispatch" if state == "stopped" else "stop_requested"
                conn.execute("""
                    UPDATE session_goal_steps SET state=?, code=?
                    WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (state, code, owner, session_id, run.request_id))
            if current.verification is not None and current.verification.state == "running":
                conn.execute("""
                    UPDATE session_goal_verifications SET state='interrupted', code='goal_continue_cancelled'
                    WHERE owner_account_id=? AND session_id=? AND request_id=?
                """, (owner, session_id, current.verification.request_id))
            conn.execute("""
                UPDATE session_goal_continues SET state='stopped', code='goal_continue_cancelled'
                WHERE owner_account_id=? AND session_id=? AND request_id=?
            """, (owner, session_id, continuation.request_id))
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (uuid.uuid4().hex, owner, session_id))
            return self._read(conn, owner, session_id)

        return self._store.transaction(stop)

    def finish_continuation(
        self, continuation: ReservedGoalContinuation, state: ContinuationTerminal, code: str,
        iterations: int, step_request_id: str, verification_request_id: str,
    ) -> GoalSnapshot | None:
        def finish(conn):
            owner, sid = continuation.owner, continuation.session_id
            current = self._read(conn, owner, sid, continuation.incarnation)
            if current is None or current.continuation is None:
                return current
            if current.continuation.request_id != continuation.request_id or current.continuation.state != "running":
                return current
            conn.execute("""
                UPDATE session_goal_continues
                SET state=?, code=?, iterations=?, step_request_id=?, verification_request_id=?
                WHERE owner_account_id=? AND session_id=? AND request_id=?
            """, (state, code, iterations, step_request_id, verification_request_id,
                  owner, sid, continuation.request_id))
            conn.execute("UPDATE session_goals SET version=? WHERE owner_account_id=? AND session_id=?",
                         (uuid.uuid4().hex, owner, sid))
            return self._read(conn, owner, sid)

        return self._store.transaction(finish)

    def recover_continuations(self) -> list[str]:
        """Stop every automatic run loop. A restarted Gateway never replays one."""
        def recover(conn):
            conn.execute("""
                UPDATE session_goals SET version=lower(hex(randomblob(16)))
                WHERE (owner_account_id,session_id) IN (
                    SELECT owner_account_id,session_id FROM session_goal_continues WHERE state='running'
                )
            """)
            rows = conn.execute("""
                UPDATE session_goal_continues SET state='interrupted', code='gateway_restarted'
                WHERE state='running' RETURNING request_id
            """).fetchall()
            return [row[0] for row in rows]

        return self._store.transaction(recover)

    def recover_verifications(self) -> list[str]:
        def recover(conn):
            conn.execute("""
                UPDATE session_goals SET status='paused', version=lower(hex(randomblob(16)))
                WHERE status='active' AND (owner_account_id,session_id) IN (
                    SELECT owner_account_id,session_id FROM session_goal_verifications WHERE state='running'
                )
            """)
            rows = conn.execute("""
                UPDATE session_goal_verifications SET state='interrupted', code='gateway_restarted'
                WHERE state='running' RETURNING request_id
            """).fetchall()
            return [row[0] for row in rows]

        return self._store.transaction(recover)

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
