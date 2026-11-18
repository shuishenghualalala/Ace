"""Atomic Plan decisions and their accepted execution requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from crew.agent.plan import PlanTransition
from crew.gateway.send_inbox import DesktopSendInbox, SendKey

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SELECT = """
    SELECT request_id,expected_version,payload_hash,status,code,state_json,execution_request_id
    FROM plan_receipts WHERE owner_account_id=? AND session_id=? AND submission_id=?
"""


@dataclass(frozen=True)
class PlanSubmissionKey:
    owner: str
    session_id: str
    submission_id: str
    request_id: str
    expected_version: str

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.owner, self.session_id, self.submission_id

    @classmethod
    def from_frame(cls, owner: str, session_id: str, data: dict[str, Any]) -> PlanSubmissionKey | None:
        values = [data.get(name) for name in ("submission_id", "request_id", "expected_version")]
        if all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in values):
            return cls(owner, session_id, *values)
        return None


@dataclass(frozen=True)
class PlanReceipt:
    key: PlanSubmissionKey
    status: str
    code: str = ""
    state: dict[str, Any] | None = None
    execution_request_id: str = ""

    def with_state(self, state: dict[str, Any]) -> PlanReceipt:
        return replace(self, state=state)

    def frame(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "plan", "submission_id": self.key.submission_id,
            "status": self.status,
        }
        if self.code:
            body["code"] = self.code
        if self.state is not None:
            body.update(current_version=self.state["version"], plan_state=self.state)
        if self.execution_request_id:
            body["execution_request_id"] = self.execution_request_id
        return {
            "kind": "interaction_receipt", "session_id": self.key.session_id,
            "request_id": self.key.request_id, "body": body, "is_final": False, "sequence": 0,
        }


def plan_submission_hash(data: dict[str, Any]) -> str:
    payload = {
        name: data[name] for name in ("action", "plan", "feedback", "mode", "workspace_id") if name in data
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


class PlanReceiptJournal:
    def __init__(self, store, inbox: DesktopSendInbox):
        self._store = store
        self._inbox = inbox

    @staticmethod
    def _decode(key: PlanSubmissionKey, row, digest: str | None = None) -> PlanReceipt:
        if row is None:
            return PlanReceipt(key, "unknown")
        request_id, version, saved_digest, status, code, raw_state, execution_request_id = row
        if request_id != key.request_id or version != key.expected_version or (
            digest is not None and digest != saved_digest
        ):
            return PlanReceipt(key, "rejected", "submission_conflict")
        return PlanReceipt(key, status, code, json.loads(raw_state), execution_request_id)

    async def query(self, key: PlanSubmissionKey, digest: str | None = None) -> PlanReceipt:
        row = await asyncio.to_thread(
            self._store.read_query, lambda conn: conn.execute(_SELECT, key.scope).fetchone(),
        )
        return self._decode(key, row, digest)

    async def commit(
        self, key: PlanSubmissionKey, digest: str, transition: PlanTransition,
        execution: dict[str, Any] | None,
    ) -> PlanReceipt:
        if (transition.base.owner_account_id, transition.base.session_id, transition.base.version) != (
            key.owner, key.session_id, key.expected_version,
        ) or (transition.target.owner_account_id, transition.target.session_id) != (key.owner, key.session_id):
            raise ValueError("Plan transition does not match the submitted identity")
        target = transition.target.to_body()

        def write(conn) -> PlanReceipt:
            existing = conn.execute(_SELECT, key.scope).fetchone()
            if existing is not None:
                return self._decode(key, existing, digest)
            session = conn.execute(
                "SELECT 1 FROM sessions WHERE owner_account_id=? AND session_id=?",
                (key.owner, key.session_id),
            ).fetchone()
            if session is None:
                return PlanReceipt(key, "expired", "session_expired")
            row = conn.execute(
                "SELECT state_json FROM plan_gateway_state WHERE owner_account_id=? AND session_id=?",
                (key.owner, key.session_id),
            ).fetchone()
            state = json.loads(row[0]) if row is not None else None
            if state is None or state.get("version") != key.expected_version:
                return PlanReceipt(key, "expired", "stale_plan", state)
            execution_id = ""
            if execution is not None:
                send_key = SendKey(key.owner, key.session_id, key.request_id)
                collision = conn.execute(
                    "SELECT 1 FROM desktop_send_inbox WHERE owner_account_id=? AND session_id=? AND request_id=?",
                    send_key.values,
                ).fetchone()
                if collision is not None:
                    return PlanReceipt(key, "rejected", "execution_request_conflict", state)
                accepted = self._inbox.accept_in_transaction(conn, send_key, execution)
                if accepted.status != "accepted":
                    return PlanReceipt(key, "rejected", accepted.code, state)
                execution_id = key.request_id
            self._store.write_plan_state_in_transaction(conn, key.owner, key.session_id, target)
            conn.execute(
                "INSERT INTO plan_receipts(owner_account_id,session_id,submission_id,request_id,expected_version,"
                "payload_hash,status,state_json,execution_request_id,updated_at) VALUES(?,?,?,?,?,?,'applied',?,?,?)",
                (*key.scope, key.request_id, key.expected_version, digest, json.dumps(target, ensure_ascii=False),
                 execution_id, time.time()),
            )
            return PlanReceipt(key, "applied", state=target, execution_request_id=execution_id)

        return await asyncio.to_thread(self._store.transaction, write)

    async def execution_state(self, owner: str, session_id: str, request_id: str) -> dict[str, Any] | None:
        row = await asyncio.to_thread(self._store.read_query, lambda conn: conn.execute(
            "SELECT state_json FROM plan_receipts "
            "WHERE owner_account_id=? AND session_id=? AND execution_request_id=? AND status='applied'",
            (owner, session_id, request_id),
        ).fetchone())
        return json.loads(row[0]) if row is not None else None
