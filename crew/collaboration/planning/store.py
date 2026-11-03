"""Owner-scoped SQLite storage for planning previews."""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


class PlanningStore:
    def __init__(self, path: str, *, wal_enabled: bool = True) -> None:
        self._conn = connect_sqlite(path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.Lock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(lambda conn: conn.execute("""CREATE TABLE IF NOT EXISTS collab_planning_tasks (
            task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, session_id TEXT NOT NULL, snapshot TEXT NOT NULL,
            state TEXT NOT NULL, plan TEXT, error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(owner_id, request_id))"""))
        self._writer.execute(lambda conn: conn.execute("""CREATE TABLE IF NOT EXISTS collab_plan_versions (
            owner_id TEXT NOT NULL, task_id TEXT NOT NULL, version INTEGER NOT NULL,
            plan_id TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(owner_id, task_id, version), UNIQUE(owner_id, plan_id),
            FOREIGN KEY(task_id) REFERENCES collab_planning_tasks(task_id))"""))

    def create(self, owner: str, task: dict[str, Any], request_hash: str) -> dict[str, Any]:
        if not owner or not task.get("request_id"):
            raise ValueError("缺少账户或请求标识")
        now = time.time()
        def write(conn: Any) -> None:
            row = conn.execute("SELECT request_hash FROM collab_planning_tasks WHERE owner_id=? AND request_id=?",
                               (owner, task["request_id"])).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ValueError("请求标识已用于不同的任务")
                return
            conn.execute("INSERT INTO collab_planning_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                task["task_id"], owner, task["request_id"], request_hash, task["session_id"],
                json.dumps(task, ensure_ascii=False), "planning", None, "", now, now))
        self._writer.execute(write)
        result = self.get_by_request(owner, task["request_id"])
        assert result is not None
        return result

    def get_by_request(self, owner: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM collab_planning_tasks WHERE owner_id=? AND request_id=?",
                                     (owner, request_id)).fetchone()
        return self._decode(row)

    def get(self, owner: str, task_id: str) -> dict[str, Any] | None:
        if not owner:
            raise ValueError("缺少账户")
        with self._lock:
            row = self._conn.execute("SELECT * FROM collab_planning_tasks WHERE owner_id=? AND task_id=?",
                                     (owner, task_id)).fetchone()
        return self._decode(row)

    @staticmethod
    def _decode(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"task": json.loads(row["snapshot"]), "state": row["state"],
                "plan": json.loads(row["plan"]) if row["plan"] else None, "error": row["error"]}

    def finish(self, owner: str, task_id: str, *, plan: dict[str, Any] | None = None, error: str = "") -> None:
        if not owner or (plan is None) == (not error):
            raise ValueError("必须提供规划结果或失败原因")
        state = plan["status"] if plan is not None else "failed"
        def write(conn: Any) -> None:
            cursor = conn.execute("UPDATE collab_planning_tasks SET state=?, plan=?, error=?, updated_at=? "
                                  "WHERE owner_id=? AND task_id=? AND state='planning'", (
                                      state, json.dumps(plan, ensure_ascii=False) if plan else None,
                                      error, time.time(), owner, task_id))
            if cursor.rowcount != 1:
                raise ValueError("任务不存在或规划状态已改变")
            if plan is not None:
                conn.execute("INSERT INTO collab_plan_versions VALUES (?,?,?,?,?,?,?)", (
                    owner, task_id, int(plan["version"]), plan["plan_id"], state,
                    json.dumps(plan, ensure_ascii=False), time.time()))
        self._writer.execute(write)

    def answer(self, owner: str, task_id: str, decision_id: str, member_id: str) -> dict[str, Any]:
        """Persist one selection atomically; identical retries are idempotent."""
        def write(conn: Any) -> None:
            row = conn.execute("SELECT state,plan FROM collab_planning_tasks WHERE owner_id=? AND task_id=?",
                               (owner, task_id)).fetchone()
            if not row or not row["plan"]:
                raise ValueError("任务或决策不存在")
            plan = json.loads(row["plan"])
            decision = next((d for d in plan["decisions"] if d["decision_id"] == decision_id), None)
            if not decision:
                raise ValueError("决策不存在")
            if decision["state"] == "answered":
                if decision.get("answer") != member_id:
                    raise ValueError("该决策已有不同答案")
                return
            if row["state"] != "waiting_user" or decision.get("kind") != "assignment" or decision["state"] != "open" or member_id not in {v["member_id"] for v in decision["options"]}:
                raise ValueError("决策已失效或所选成员不在候选中")
            decision.update(state="answered", answer=member_id)
            plan["assignments"].append({"assignment_id": uuid.uuid4().hex, "unit_id": decision["unit_id"],
                                        "member_id": member_id, "source": "user_decision",
                                        "reason": "用户选择", "responsibility_ref": "",
                                        "capability_status": "unverified"})
            plan["status"] = "waiting_user" if any(d["state"] == "open" for d in plan["decisions"]) else "planned"
            conn.execute("UPDATE collab_planning_tasks SET state=?, plan=?, updated_at=? WHERE owner_id=? AND task_id=?",
                         (plan["status"], json.dumps(plan, ensure_ascii=False), time.time(), owner, task_id))
            conn.execute("UPDATE collab_plan_versions SET status=?,snapshot=? WHERE owner_id=? AND task_id=? AND plan_id=?",
                         (plan["status"], json.dumps(plan, ensure_ascii=False), owner, task_id, plan["plan_id"]))
        if not owner:
            raise ValueError("缺少账户")
        self._writer.execute(write)
        result = self.get(owner, task_id)
        assert result is not None
        return result

    def answer_clarification(self, owner: str, task_id: str, decision_id: str, answer: str) -> tuple[dict[str, Any], bool]:
        """Persist the answer and claim replanning in the same transaction."""
        if not owner or not answer.strip():
            raise ValueError("账户和补充内容不能为空")
        def write(conn: Any) -> bool:
            row = conn.execute("SELECT snapshot,state,plan FROM collab_planning_tasks WHERE owner_id=? AND task_id=?",
                               (owner, task_id)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            task = json.loads(row["snapshot"])
            previous = next((item for item in task.get("clarifications", []) if item.get("decision_id") == decision_id), None)
            if previous:
                if previous.get("answer") != answer.strip():
                    raise ValueError("该问题已有不同答案")
                return False
            plan = json.loads(row["plan"]) if row["plan"] else {}
            decision = next((item for item in plan.get("decisions", []) if item.get("decision_id") == decision_id), None)
            if row["state"] != "waiting_user" or not decision or decision.get("kind") != "clarification" or decision.get("state") != "open":
                raise ValueError("问题不存在或已失效")
            task.setdefault("clarifications", []).append({"decision_id": decision_id,
                "question": decision["question"], "answer": answer.strip()})
            decision.update(state="answered", answer=answer.strip())
            plan["status"] = "superseded"
            conn.execute("UPDATE collab_plan_versions SET status=?,snapshot=? WHERE owner_id=? AND task_id=? AND plan_id=?",
                         ("superseded", json.dumps(plan, ensure_ascii=False), owner, task_id, plan["plan_id"]))
            conn.execute("UPDATE collab_planning_tasks SET snapshot=?,state='planning',plan=NULL,error='',updated_at=? "
                         "WHERE owner_id=? AND task_id=? AND state='waiting_user'",
                         (json.dumps(task, ensure_ascii=False), time.time(), owner, task_id))
            return True
        claimed = self._writer.execute(write)
        result = self.get(owner, task_id)
        assert result is not None
        return result, claimed

    def plan_history(self, owner: str, task_id: str) -> list[dict[str, Any]]:
        if not owner:
            raise ValueError("缺少账户")
        with self._lock:
            rows = self._conn.execute("SELECT snapshot FROM collab_plan_versions WHERE owner_id=? AND task_id=? ORDER BY version",
                                      (owner, task_id)).fetchall()
        return [json.loads(row["snapshot"]) for row in rows]

    def claim_handoff(self, owner: str, task_id: str, plan_id: str) -> bool:
        """Claim one committed plan exactly once before handing it to a scheduler."""
        if not owner or not task_id or not plan_id:
            raise ValueError("缺少任务、计划或账户")
        def write(conn: Any) -> bool:
            row = conn.execute("SELECT state,plan FROM collab_planning_tasks WHERE owner_id=? AND task_id=?",
                               (owner, task_id)).fetchone()
            if not row or row["state"] != "planned" or not row["plan"]:
                return False
            if json.loads(row["plan"]).get("plan_id") != plan_id:
                return False
            cursor = conn.execute("UPDATE collab_planning_tasks SET state='handoff_claimed',updated_at=? "
                                  "WHERE owner_id=? AND task_id=? AND state='planned'",
                                  (time.time(), owner, task_id))
            return cursor.rowcount == 1
        return self._writer.execute(write)
