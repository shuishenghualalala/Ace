"""Owner-scoped learning state stored in Ace's existing SQLite database."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from crew.core.errors import ToolError
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clean_strings(values: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _level(average: float) -> str:
    if average < 0.4:
        return "weak"
    if average < 0.7:
        return "developing"
    if average < 0.9:
        return "proficient"
    return "mastered"


class WikiLearningStore:
    """Plugin-owned schema on a separate connection to the shared crew.db."""

    def __init__(self, db_path: str | Path, *, wal_enabled: bool = True) -> None:
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.Lock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._closed = False
        self._init_schema()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS wiki_learning_schema (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wiki_learning_episodes (
            id TEXT PRIMARY KEY,
            owner_account_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            page_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_learning_active_episode
            ON wiki_learning_episodes(owner_account_id, session_id, kb_id)
            WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS wiki_learning_activities (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            owner_account_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            prompt TEXT NOT NULL,
            evidence_page_ids_json TEXT NOT NULL,
            evidence_fingerprints_json TEXT NOT NULL,
            knowledge_keys_json TEXT NOT NULL,
            reveal_policy TEXT NOT NULL DEFAULT 'on_assess',
            public_payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            created_request_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_learning_activity_episode
            ON wiki_learning_activities(episode_id, created_at);
        CREATE TABLE IF NOT EXISTS wiki_learning_assessments (
            id TEXT PRIMARY KEY,
            activity_id TEXT NOT NULL UNIQUE,
            episode_id TEXT NOT NULL,
            owner_account_id TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            response_text TEXT NOT NULL,
            response_hash TEXT NOT NULL,
            response_chars INTEGER NOT NULL,
            summary TEXT NOT NULL,
            score REAL NOT NULL,
            strengths_json TEXT NOT NULL DEFAULT '[]',
            gaps_json TEXT NOT NULL DEFAULT '[]',
            signals_json TEXT NOT NULL DEFAULT '{}',
            evidence_page_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_learning_assessment_request
            ON wiki_learning_assessments(owner_account_id, request_id)
            WHERE request_id != '';
        CREATE TABLE IF NOT EXISTS wiki_learning_mastery_events (
            assessment_id TEXT NOT NULL,
            owner_account_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            knowledge_key TEXT NOT NULL,
            score REAL NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (assessment_id, knowledge_key)
        );
        CREATE TABLE IF NOT EXISTS wiki_learning_mastery_state (
            owner_account_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            knowledge_key TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            total_score REAL NOT NULL,
            average_score REAL NOT NULL,
            level TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (owner_account_id, kb_id, knowledge_key)
        );
        """

        def write(conn: sqlite3.Connection) -> None:
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                """INSERT INTO wiki_learning_schema(component, version, updated_at)
                   VALUES('wiki_learning', 1, ?)
                   ON CONFLICT(component) DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at""",
                (time.time(),),
            )

        self._writer.execute(write)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._conn.close()

    @staticmethod
    def _episode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "kb_id": row["kb_id"],
            "goal": row["goal"],
            "constraints": _loads(row["constraints_json"], {}),
            "page_ids": _loads(row["page_ids_json"], []),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _activity(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "episode_id": row["episode_id"],
            "kb_id": row["kb_id"],
            "activity_type": row["activity_type"],
            "prompt": row["prompt"],
            "evidence_page_ids": _loads(row["evidence_page_ids_json"], []),
            "evidence_fingerprints": _loads(row["evidence_fingerprints_json"], {}),
            "knowledge_keys": _loads(row["knowledge_keys_json"], []),
            "reveal_policy": row["reveal_policy"],
            "public_payload": _loads(row["public_payload_json"], {}),
            "status": row["status"],
            "created_request_id": row["created_request_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def active_episode(self, owner: str, session_id: str, kb_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM wiki_learning_episodes
               WHERE owner_account_id=? AND session_id=? AND kb_id=? AND status='active'""",
            (owner, session_id, kb_id),
        ).fetchone()
        return self._episode(row)

    def get_episode(self, episode_id: str, owner: str, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM wiki_learning_episodes WHERE id=? AND owner_account_id=? AND session_id=?",
            (episode_id, owner, session_id),
        ).fetchone()
        return self._episode(row)

    def open_episode(
        self,
        owner: str,
        session_id: str,
        kb_id: str,
        *,
        goal: str = "",
        constraints: dict[str, Any] | None = None,
        page_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        episode_id = _new_id("learn")
        pages = _clean_strings(page_ids)

        def write(conn: sqlite3.Connection) -> str:
            current = conn.execute(
                """SELECT id FROM wiki_learning_episodes
                   WHERE owner_account_id=? AND session_id=? AND kb_id=? AND status='active'""",
                (owner, session_id, kb_id),
            ).fetchone()
            if current:
                return str(current["id"])
            conn.execute(
                """INSERT INTO wiki_learning_episodes
                   (id, owner_account_id, session_id, kb_id, goal, constraints_json,
                    page_ids_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    episode_id,
                    owner,
                    session_id,
                    kb_id,
                    goal.strip(),
                    _json(constraints or {}),
                    _json(pages),
                    now,
                    now,
                ),
            )
            return episode_id

        resolved = self._writer.execute(write)
        episode = self.get_episode(resolved, owner, session_id)
        if episode is None:
            raise ToolError("学习会话创建失败")
        return episode

    def resume_episode(self, episode_id: str, owner: str, session_id: str) -> dict[str, Any]:
        episode = self.get_episode(episode_id, owner, session_id)
        if episode is None:
            raise ToolError("学习会话不存在或无权访问")
        if episode["status"] != "active":
            raise ToolError("该学习会话已结束")
        return episode

    def update_episode(
        self,
        episode_id: str,
        owner: str,
        session_id: str,
        *,
        goal: str | None = None,
        constraints: dict[str, Any] | None = None,
        page_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        episode = self.resume_episode(episode_id, owner, session_id)
        next_goal = episode["goal"] if goal is None else goal.strip()
        next_constraints = episode["constraints"] if constraints is None else constraints
        next_pages = episode["page_ids"] if page_ids is None else _clean_strings(page_ids)
        now = time.time()

        def write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """UPDATE wiki_learning_episodes
                   SET goal=?, constraints_json=?, page_ids_json=?, updated_at=?
                   WHERE id=? AND owner_account_id=? AND session_id=? AND status='active'""",
                (
                    next_goal,
                    _json(next_constraints),
                    _json(next_pages),
                    now,
                    episode_id,
                    owner,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolError("学习会话不存在、已结束或无权访问")

        self._writer.execute(write)
        updated = self.get_episode(episode_id, owner, session_id)
        assert updated is not None
        return updated

    def finish_episode(self, episode_id: str, owner: str, session_id: str) -> dict[str, Any]:
        now = time.time()

        def write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """UPDATE wiki_learning_episodes SET status='finished', updated_at=?, finished_at=?
                   WHERE id=? AND owner_account_id=? AND session_id=? AND status='active'""",
                (now, now, episode_id, owner, session_id),
            )
            if cursor.rowcount != 1:
                raise ToolError("学习会话不存在、已结束或无权访问")
            conn.execute(
                """UPDATE wiki_learning_activities SET status='closed', updated_at=?
                   WHERE episode_id=? AND owner_account_id=? AND session_id=? AND status='open'""",
                (now, episode_id, owner, session_id),
            )

        self._writer.execute(write)
        episode = self.get_episode(episode_id, owner, session_id)
        assert episode is not None
        return episode

    def create_activity(
        self,
        episode_id: str,
        owner: str,
        session_id: str,
        kb_id: str,
        *,
        activity_type: str,
        prompt: str,
        evidence_page_ids: Iterable[str],
        evidence_fingerprints: dict[str, str],
        knowledge_keys: Iterable[str],
        reveal_policy: str,
        public_payload: dict[str, Any] | None,
        request_id: str,
    ) -> dict[str, Any]:
        episode = self.resume_episode(episode_id, owner, session_id)
        if episode["kb_id"] != kb_id:
            raise ToolError("学习活动与学习会话不属于同一知识库")
        now = time.time()
        activity_id = _new_id("activity")
        pages = _clean_strings(evidence_page_ids)
        keys = _clean_strings(knowledge_keys)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO wiki_learning_activities
                   (id, episode_id, owner_account_id, session_id, kb_id, activity_type, prompt,
                    evidence_page_ids_json, evidence_fingerprints_json, knowledge_keys_json,
                    reveal_policy, public_payload_json, status, created_request_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (
                    activity_id,
                    episode_id,
                    owner,
                    session_id,
                    kb_id,
                    activity_type.strip(),
                    prompt.strip(),
                    _json(pages),
                    _json(evidence_fingerprints),
                    _json(keys),
                    reveal_policy,
                    _json(public_payload or {}),
                    request_id,
                    now,
                    now,
                ),
            )

        self._writer.execute(write)
        activity = self.get_activity(activity_id, owner, session_id)
        assert activity is not None
        return activity

    def get_activity(self, activity_id: str, owner: str, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM wiki_learning_activities WHERE id=? AND owner_account_id=? AND session_id=?",
            (activity_id, owner, session_id),
        ).fetchone()
        return self._activity(row)

    def close_activity(self, activity_id: str, owner: str, session_id: str) -> dict[str, Any]:
        now = time.time()

        def write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """UPDATE wiki_learning_activities SET status='closed', updated_at=?
                   WHERE id=? AND owner_account_id=? AND session_id=?""",
                (now, activity_id, owner, session_id),
            )
            if cursor.rowcount != 1:
                raise ToolError("学习活动不存在或无权访问")

        self._writer.execute(write)
        activity = self.get_activity(activity_id, owner, session_id)
        assert activity is not None
        return activity

    def mastery_snapshot(self, owner: str, kb_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT knowledge_key, attempts, average_score, level, updated_at
               FROM wiki_learning_mastery_state WHERE owner_account_id=? AND kb_id=?
               ORDER BY updated_at DESC, knowledge_key""",
            (owner, kb_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_assessment(
        self,
        activity_id: str,
        owner: str,
        session_id: str,
        *,
        request_id: str,
        response_text: str,
        response_hash: str,
        summary: str,
        score: float,
        strengths: Iterable[str] | None,
        gaps: Iterable[str] | None,
        signals: dict[str, float],
        evidence_page_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        activity = self.get_activity(activity_id, owner, session_id)
        if activity is None:
            raise ToolError("学习活动不存在或无权访问")
        if activity["status"] != "open":
            existing = self._conn.execute(
                "SELECT * FROM wiki_learning_assessments WHERE activity_id=? AND owner_account_id=?",
                (activity_id, owner),
            ).fetchone()
            if existing and request_id and existing["request_id"] == request_id:
                return self._assessment_public(existing)
            raise ToolError("该学习活动已完成评估")
        if activity["created_request_id"] and activity["created_request_id"] == request_id:
            raise ToolError("不能在出题的同一回合评估答案，请等待用户作答")
        if not response_text.strip():
            raise ToolError("当前回合没有可评估的用户回答")
        allowed_keys = set(activity["knowledge_keys"])
        unknown = sorted(set(signals) - allowed_keys)
        if unknown:
            raise ToolError(f"掌握度信号包含未登记的知识点: {', '.join(unknown)}")
        assessment_evidence = _clean_strings(evidence_page_ids)
        unknown_pages = sorted(set(assessment_evidence) - set(activity["evidence_page_ids"]))
        if unknown_pages:
            raise ToolError(f"评估引用了活动之外的证据页面: {', '.join(unknown_pages)}")
        if not assessment_evidence:
            assessment_evidence = list(activity["evidence_page_ids"])
        now = time.time()
        assessment_id = _new_id("assessment")

        def write(conn: sqlite3.Connection) -> dict[str, Any]:
            if request_id:
                duplicate = conn.execute(
                    "SELECT * FROM wiki_learning_assessments WHERE owner_account_id=? AND request_id=?",
                    (owner, request_id),
                ).fetchone()
                if duplicate:
                    if duplicate["activity_id"] == activity_id:
                        return self._assessment_public(duplicate)
                    raise ToolError("当前回答已用于另一个学习活动")
            existing = conn.execute(
                "SELECT * FROM wiki_learning_assessments WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            if existing:
                raise ToolError("该学习活动已完成评估")
            conn.execute(
                """INSERT INTO wiki_learning_assessments
                   (id, activity_id, episode_id, owner_account_id, request_id, response_text,
                    response_hash, response_chars, summary, score, strengths_json, gaps_json,
                    signals_json, evidence_page_ids_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    assessment_id,
                    activity_id,
                    activity["episode_id"],
                    owner,
                    request_id,
                    response_text,
                    response_hash,
                    len(response_text),
                    summary.strip(),
                    score,
                    _json(_clean_strings(strengths)),
                    _json(_clean_strings(gaps)),
                    _json(signals),
                    _json(assessment_evidence),
                    now,
                ),
            )
            conn.execute(
                "UPDATE wiki_learning_activities SET status='assessed', updated_at=? WHERE id=?",
                (now, activity_id),
            )
            for key, key_score in signals.items():
                conn.execute(
                    """INSERT INTO wiki_learning_mastery_events
                       (assessment_id, owner_account_id, kb_id, knowledge_key, score, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (assessment_id, owner, activity["kb_id"], key, key_score, now),
                )
                current = conn.execute(
                    """SELECT attempts, total_score FROM wiki_learning_mastery_state
                       WHERE owner_account_id=? AND kb_id=? AND knowledge_key=?""",
                    (owner, activity["kb_id"], key),
                ).fetchone()
                attempts = int(current["attempts"]) + 1 if current else 1
                total = float(current["total_score"]) + key_score if current else key_score
                average = total / attempts
                conn.execute(
                    """INSERT INTO wiki_learning_mastery_state
                       (owner_account_id, kb_id, knowledge_key, attempts, total_score,
                        average_score, level, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(owner_account_id, kb_id, knowledge_key) DO UPDATE SET
                         attempts=excluded.attempts, total_score=excluded.total_score,
                         average_score=excluded.average_score, level=excluded.level,
                         updated_at=excluded.updated_at""",
                    (owner, activity["kb_id"], key, attempts, total, average, _level(average), now),
                )
            row = conn.execute(
                "SELECT * FROM wiki_learning_assessments WHERE id=?", (assessment_id,)
            ).fetchone()
            assert row is not None
            return self._assessment_public(row)

        return self._writer.execute(write)

    @staticmethod
    def _assessment_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "activity_id": row["activity_id"],
            "episode_id": row["episode_id"],
            "response_hash": row["response_hash"],
            "response_chars": row["response_chars"],
            "summary": row["summary"],
            "score": row["score"],
            "strengths": _loads(row["strengths_json"], []),
            "gaps": _loads(row["gaps_json"], []),
            "knowledge_signals": _loads(row["signals_json"], {}),
            "evidence_page_ids": _loads(row["evidence_page_ids_json"], []),
            "created_at": row["created_at"],
        }
