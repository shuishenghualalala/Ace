"""Durable delivery receipts for responses to live follow-up prompts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
from crew.core.followup import FollowupAnswerMessage

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SELECT = """
    SELECT submission_id, decision_hash, generation, status, code, answer_history_id
    FROM followup_receipts WHERE owner_account_id=? AND session_id=? AND request_id=? AND question_id=?
"""


@dataclass(frozen=True)
class FollowupKey:
    owner: str
    session_id: str
    request_id: str
    question_id: str
    submission_id: str

    @classmethod
    def from_frame(cls, owner: str, session_id: str, data: dict[str, Any]) -> FollowupKey | None:
        request, question, submission = (data.get(name) for name in ("request_id", "question_id", "submission_id"))
        if all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (request, question, submission)):
            return cls(owner, session_id, request, question, submission)
        return None

    @property
    def scope(self) -> tuple[str, str, str, str]:
        return self.owner, self.session_id, self.request_id, self.question_id


@dataclass(frozen=True)
class FollowupReceipt:
    key: FollowupKey
    status: str
    code: str = ""
    answer_history_id: str = ""

    def frame(self) -> dict[str, Any]:
        body = {
            "type": "followup",
            "interaction_id": self.key.question_id,
            "submission_id": self.key.submission_id,
            "status": self.status,
        }
        if self.code:
            body["code"] = self.code
        if self.answer_history_id:
            body["answer_history_id"] = self.answer_history_id
        return {
            "kind": "interaction_receipt",
            "session_id": self.key.session_id,
            "request_id": self.key.request_id,
            "sequence": 0,
            "is_final": False,
            "body": body,
        }


def followup_decision_hash(action: str, answers: list[dict[str, Any]] | None) -> str:
    canonical = json.dumps([action, answers], ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class FollowupReceiptJournal:
    def __init__(self, transaction, read_query, persist_answer=None):
        self._transaction = transaction
        self._read_query = read_query
        self._generation = uuid4().hex
        self._persist_answer = persist_answer
        self._active_claims: dict[tuple[str, str, str, str], asyncio.Task] = {}

    def _decode(self, key: FollowupKey, row, digest: str | None) -> FollowupReceipt:
        if row is None:
            return FollowupReceipt(key, "unknown")
        submission_id, recorded_hash, generation, status, code, history_id = row
        if submission_id != key.submission_id or (digest is not None and recorded_hash != digest):
            return FollowupReceipt(key, "rejected", "submission_conflict")
        if status == "claimed":
            task = self._active_claims.get(key.scope)
            return (
                FollowupReceipt(key, "unavailable", "interaction_in_progress")
                if generation == self._generation and task is not None and not task.done()
                else FollowupReceipt(key, "uncertain", "interaction_interrupted")
            )
        return FollowupReceipt(key, status, code, history_id)

    async def query(self, key: FollowupKey, digest: str | None = None) -> FollowupReceipt:
        row = await asyncio.to_thread(self._read_query, lambda conn: conn.execute(_SELECT, key.scope).fetchone())
        return self._decode(key, row, digest)

    async def claim(self, key: FollowupKey, digest: str) -> FollowupReceipt | None:
        task = asyncio.current_task()
        if task is not None:
            self._active_claims.setdefault(key.scope, task)
        def write(conn) -> FollowupReceipt | None:
            row = conn.execute(_SELECT, key.scope).fetchone()
            if row is not None:
                return self._decode(key, row, digest)
            result = conn.execute(
                "INSERT INTO followup_receipts "
                "(owner_account_id,session_id,request_id,question_id,submission_id,decision_hash,generation,status,updated_at) "
                "SELECT ?,?,?,?,?,?,?,'claimed',? WHERE EXISTS "
                "(SELECT 1 FROM sessions WHERE owner_account_id=? AND session_id=?)",
                (*key.scope, key.submission_id, digest, self._generation, time.time(), key.owner, key.session_id),
            )
            return None if result.rowcount == 1 else FollowupReceipt(key, "expired", "interaction_expired")

        return await asyncio.to_thread(self._transaction, write)

    def release(self, key: FollowupKey) -> None:
        if self._active_claims.get(key.scope) is asyncio.current_task():
            self._active_claims.pop(key.scope, None)

    async def finish(self, receipt: FollowupReceipt, *, answer: FollowupAnswerMessage | None = None) -> None:
        key = receipt.key

        def write(conn) -> None:
            result = conn.execute(
                "UPDATE followup_receipts SET status=?,code=?,answer_history_id=?,updated_at=? "
                "WHERE owner_account_id=? AND session_id=? AND request_id=? AND question_id=? "
                "AND submission_id=? AND generation=? AND status='claimed'",
                (receipt.status, receipt.code, receipt.answer_history_id, time.time(),
                 *key.scope, key.submission_id, self._generation),
            )
            if result.rowcount != 1:
                raise RuntimeError("Follow-up receipt claim changed before settlement")
            if answer is not None:
                if self._persist_answer is None:
                    raise RuntimeError("Durable follow-up answers are unavailable")
                sequence = self._persist_answer(
                    conn, key.session_id, key.request_id, answer.history_id, answer.text,
                    owner_account_id=key.owner,
                )
                conn.execute(
                    "UPDATE followup_receipts SET answer_seq=? "
                    "WHERE owner_account_id=? AND session_id=? AND request_id=? AND question_id=?",
                    (sequence, *key.scope),
                )

        await asyncio.to_thread(self._transaction, write)

    async def abandon(self, key: FollowupKey) -> None:
        await asyncio.to_thread(self._transaction, lambda conn: conn.execute(
            "UPDATE followup_receipts SET status='uncertain',code='interaction_interrupted',updated_at=? "
            "WHERE owner_account_id=? AND session_id=? AND request_id=? AND question_id=? "
            "AND submission_id=? AND generation=? AND status IN ('claimed','applied','cancelled')",
            (time.time(), *key.scope, key.submission_id, self._generation),
        ))
