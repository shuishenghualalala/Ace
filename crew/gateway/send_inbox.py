"""Durable acceptance and at-most-once dispatch for opted-in Desktop sends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from crew.core.envelope import TurnOutcome
from crew.state.logging import get_logger

_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
log = get_logger("gateway.send_inbox")


@dataclass(frozen=True)
class SendKey:
    owner: str
    session_id: str
    request_id: str

    @property
    def values(self) -> tuple[str, str, str]:
        return self.owner, self.session_id, self.request_id

    @classmethod
    def from_frame(cls, owner: str, data: dict[str, Any]) -> SendKey | None:
        sid, rid = data.get("session_id"), data.get("request_id")
        if (
            not isinstance(sid, str) or not sid or len(sid) > 256 or sid != sid.strip()
            or not isinstance(rid, str) or _REQUEST_ID.fullmatch(rid) is None
        ):
            return None
        return cls(owner, sid, rid)


@dataclass(frozen=True)
class SendReceipt:
    key: SendKey
    status: str
    code: str = ""
    payload: dict[str, Any] | None = None
    outcome: TurnOutcome | None = None

    def frame(self) -> dict[str, Any]:
        body = {"status": self.status}
        if self.code:
            body["code"] = self.code
        if self.outcome is not None:
            body["outcome"] = self.outcome.to_body()
        return {
            "kind": "send_receipt",
            "session_id": self.key.session_id,
            "request_id": self.key.request_id,
            "sequence": 0,
            "is_final": False,
            "body": body,
        }


_SELECT = """
    SELECT status, code, generation, payload_hash, payload_json
    FROM desktop_send_inbox WHERE owner_account_id=? AND session_id=? AND request_id=?
"""


class DesktopSendInbox:
    def __init__(
        self,
        transaction: Callable[..., Any],
        read_query: Callable[..., Any],
        ensure_session: Callable[..., None],
        store_id: str,
    ) -> None:
        self._transaction = transaction
        self._read_query = read_query
        self._ensure_session = ensure_session
        self.store_id = store_id
        self._generation = uuid4().hex
        self._scheduled: dict[SendKey, asyncio.Task[None]] = {}

    async def _execute(self, write: Callable[..., Any]) -> Any:
        return await asyncio.to_thread(self._transaction, write)

    def _receipt(self, key: SendKey, row: Any) -> SendReceipt:
        if row is None:
            return SendReceipt(key, "unknown")
        status, code, generation, _digest, payload = row
        if status == "running" and generation != self._generation:
            return SendReceipt(key, "uncertain", "interrupted_after_dispatch")
        if status == "accepted" and payload:
            return SendReceipt(key, status, code, json.loads(payload))
        outcome = TurnOutcome.from_body(json.loads(payload)) if payload and status != "running" else None
        return SendReceipt(key, status, code, outcome=outcome)

    async def accept(self, key: SendKey, payload: dict[str, Any]) -> SendReceipt:
        try:
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            wire = canonical.encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return SendReceipt(key, "rejected", "invalid_payload")
        if len(wire) > _MAX_PAYLOAD_BYTES:
            return SendReceipt(key, "rejected", "payload_too_large")
        digest = hashlib.sha256(wire).hexdigest()

        def write(conn):
            row = conn.execute(_SELECT, key.values).fetchone()
            if row is not None:
                if row[3] is not None and row[3] != digest:
                    return SendReceipt(key, "rejected", "request_id_conflict")
                return self._receipt(key, row)
            conn.execute("""
                INSERT INTO desktop_send_inbox
                    (owner_account_id, session_id, request_id, status, payload_hash, payload_json, updated_at)
                VALUES (?, ?, ?, 'accepted', ?, ?, ?)
            """, (*key.values, digest, canonical, time.time()))
            workspace_id = str(payload.get("workspace_id") or "default")
            self._ensure_session(conn, key.session_id, workspace_id, "", key.owner)
            return SendReceipt(key, "accepted", payload=json.loads(canonical))

        return await self._execute(write)

    async def query(self, key: SendKey) -> SendReceipt:
        row = await asyncio.to_thread(
            self._read_query, lambda conn: conn.execute(_SELECT, key.values).fetchone(),
        )
        return self._receipt(key, row)

    async def cancel(self, key: SendKey) -> SendReceipt:
        def write(conn):
            conn.execute("""
                INSERT INTO desktop_send_inbox
                    (owner_account_id, session_id, request_id, status, code, updated_at)
                VALUES (?, ?, ?, 'cancelled', 'cancelled_before_dispatch', ?)
                ON CONFLICT(owner_account_id, session_id, request_id) DO UPDATE SET
                    status='cancelled', code='cancelled_before_dispatch', payload_json=NULL, updated_at=excluded.updated_at
                WHERE desktop_send_inbox.status='accepted'
            """, (*key.values, time.time()))
            return self._receipt(key, conn.execute(_SELECT, key.values).fetchone())

        return await self._execute(write)

    async def cancel_pending(self, owner: str, session_id: str) -> None:
        await self._execute(lambda conn: conn.execute("""
            UPDATE desktop_send_inbox
            SET status='cancelled', code='cancelled_before_dispatch', payload_json=NULL, updated_at=?
            WHERE owner_account_id=? AND session_id=? AND status='accepted'
        """, (time.time(), owner, session_id)))

    async def begin(self, key: SendKey) -> bool:
        return await self._execute(lambda conn: conn.execute("""
            UPDATE desktop_send_inbox SET status='running', generation=?, updated_at=?
            WHERE owner_account_id=? AND session_id=? AND request_id=? AND status='accepted'
        """, (self._generation, time.time(), *key.values)).rowcount == 1)

    async def defer(self, key: SendKey) -> None:
        await self._execute(lambda conn: conn.execute("""
            UPDATE desktop_send_inbox SET status='accepted', generation='', updated_at=?
            WHERE owner_account_id=? AND session_id=? AND request_id=? AND status='running' AND generation=?
        """, (time.time(), *key.values, self._generation)))

    async def finish(
        self, key: SendKey, status: str, code: str, *, started: bool, outcome: TurnOutcome | None = None,
    ) -> None:
        await self._execute(lambda conn: self._finish_in_transaction(conn, key, status, code, started, outcome))

    def _finish_in_transaction(
        self, conn: Any, key: SendKey, status: str, code: str, started: bool, outcome: TurnOutcome | None,
    ) -> bool:
        # Once execution settles, replace the input envelope with its outcome.
        payload = json.dumps(outcome.to_body(), ensure_ascii=False) if outcome is not None else None
        return conn.execute("""
            UPDATE desktop_send_inbox SET status=?, code=?, payload_json=?, updated_at=?
            WHERE owner_account_id=? AND session_id=? AND request_id=? AND status=? AND generation=?
        """, (
            status, code, payload, time.time(), *key.values,
            "running" if started else "accepted", self._generation if started else "",
        )).rowcount == 1

    def reserve(self, receipt: SendReceipt, run: Callable[[SendReceipt], Awaitable[None]]) -> None:
        if receipt.status != "accepted" or receipt.payload is None or receipt.key in self._scheduled:
            return
        task = asyncio.create_task(run(receipt), name="desktop-send-inbox")
        self._scheduled[receipt.key] = task

        def settled(done: asyncio.Task[None]) -> None:
            self._scheduled.pop(receipt.key, None)
            if not done.cancelled() and (error := done.exception()) is not None:
                log.error("Desktop send task failed request=%s", receipt.key.request_id, exc_info=error)

        task.add_done_callback(settled)

    def stop_preparation(self, owner: str, session_id: str, dispatcher_tasks: set[asyncio.Task]) -> bool:
        """Stop work still outside Dispatcher, which owns cancellation from then on."""
        stopped = False
        for key, task in self._scheduled.items():
            if (
                key.owner == owner and key.session_id == session_id and task not in dispatcher_tasks
                and not task.done() and not task.cancelling()
            ):
                task.cancel()
                stopped = True
        return stopped


@dataclass
class SendExecution:
    inbox: DesktopSendInbox
    key: SendKey
    started: bool = False
    finished: bool = False

    async def begin(self) -> bool:
        if not self.started:
            claim = asyncio.create_task(self.inbox.begin(self.key))
            try:
                self.started = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # The SQLite worker can still commit after its caller is cancelled.
                # Resolve that claim before the runner settles its receipt.
                self.started = await claim
                raise
        return self.started

    async def finish(self, status: str, code: str = "", *, outcome: TurnOutcome | None = None) -> None:
        await self.inbox.finish(self.key, status, code, started=self.started, outcome=outcome)
        self.finished = True

    def finish_in_transaction(self, conn: Any, status: str, code: str, outcome: TurnOutcome) -> None:
        if not self.inbox._finish_in_transaction(conn, self.key, status, code, self.started, outcome):
            raise RuntimeError("Desktop send changed before turn settlement")

    async def defer(self) -> None:
        if self.started:
            await self.inbox.defer(self.key)
            self.started = False


current_send_execution: ContextVar[SendExecution | None] = ContextVar("current_send_execution", default=None)
