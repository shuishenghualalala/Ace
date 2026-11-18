"""Desktop acknowledgements persist independently of the sending socket."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crew.core.envelope import Envelope, ResponseChunk, TurnOutcome
from crew.core.runctx import current_turn_execution_incarnation, current_turn_session_incarnation
from crew.core.types import Message
from crew.gateway.connections import ConnectionManager
from crew.gateway.dispatcher import SessionDispatcher
from crew.gateway.send_inbox import DesktopSendInbox, SendExecution, SendKey, current_send_execution
from crew.gateway.ws import create_ws_router
from crew.state.session_store import SQLiteSessionStore, SessionWriteConflict
from crew.tasks.runtime import TaskRuntime


def _new_inbox(store):
    return DesktopSendInbox(
        store.transaction, store.read_query, store.ensure_session_in_transaction, store.desktop_send_store_id,
    )


@pytest.mark.parametrize("boundary", ["provisional", "convergence_retry", "outcome"])
async def test_replaced_foreground_history_settles_only_original_receipt(tmp_path, monkeypatch, boundary):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    inbox = _new_inbox(store)
    owner, sid, rid = "local", "session", "request"
    key = SendKey(owner, sid, rid)
    await inbox.accept(key, _payload(rid, sid))
    original = store.get_history_incarnation(sid, owner)
    save = store.save
    record = store.record_turn_outcome
    parent_saves = 0
    seen_binding = []

    def new_session():
        store.clear(sid, owner_account_id=owner)
        store.ensure_session(sid, owner_account_id=owner)
        save(sid, [Message.user("New conversation")], owner_account_id=owner)
        store.set_status(sid, "running", owner_account_id=owner)

    def save_at_boundary(session_id, *args, **kwargs):
        nonlocal parent_saves
        if session_id == sid:
            parent_saves += 1
            if boundary == "convergence_retry" and parent_saves == 2:
                raise SessionWriteConflict("Concurrent answer write")
            if (boundary, parent_saves) in {("provisional", 1), ("convergence_retry", 3)}:
                new_session()
        return save(session_id, *args, **kwargs)

    def record_at_boundary(*args, **kwargs):
        if boundary == "outcome":
            new_session()
        return record(*args, **kwargs)

    async def inner(envelope):
        seen_binding.append(current_turn_session_incarnation.get())
        assert current_turn_execution_incarnation.get() == (
            owner, envelope.session_id, store.get_history_incarnation(envelope.session_id, owner),
        )
        save(envelope.session_id, [Message.user("Old conversation"), Message.assistant("Old reply")],
             owner_account_id=owner)
        yield ResponseChunk.final(rid, "Old reply")

    monkeypatch.setattr(store, "save", save_at_boundary)
    monkeypatch.setattr(store, "record_turn_outcome", record_at_boundary)
    dispatcher = SessionDispatcher(inner, store, task_runtime=runtime)
    parent_binding = ("another-owner", "another-session", "another-incarnation")
    binding_token = current_turn_session_incarnation.set(parent_binding)
    execution_token = current_turn_execution_incarnation.set(parent_binding)
    send_token = current_send_execution.set(SendExecution(inbox, key))
    try:
        envelope = Envelope.of("Old conversation", session_id=sid, user_id=owner, request_id=rid)
        frames = [frame async for frame in dispatcher.run(envelope)]
        assert current_turn_session_incarnation.get() == parent_binding
        assert current_turn_execution_incarnation.get() == parent_binding
        assert seen_binding == ([] if boundary == "provisional" else [(owner, sid, original)])
        assert store.get_history_incarnation(sid, owner) != original
        assert [m.content for m in store.load(sid, owner_account_id=owner)] == ["New conversation"]
        assert store.get_status_details(sid, owner_account_id=owner) == ("running", "", None)
        assert not store.session_belongs_to(f"{sid}::turn::{rid}", owner)
        terminal, = [frame for frame in frames if frame.is_final]
        assert terminal.body["outcome"]["code"] == "session_replaced"
        receipt = await inbox.query(key)
        assert (receipt.status, receipt.code) == ("cancelled", "session_replaced")
        assert receipt.outcome and receipt.outcome.status == "stopped"
        assert dispatcher.live_status(sid, owner) == "idle"
    finally:
        current_send_execution.reset(send_token)
        current_turn_execution_incarnation.reset(execution_token)
        current_turn_session_incarnation.reset(binding_token)
        await dispatcher.shutdown()
        runtime.close()
        store.close()


async def test_sidechain_cleanup_keeps_replacement_transcript(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    sid, rid, owner = "session", "request", "local"
    child = f"{sid}::turn::{rid}"
    clear = store.clear

    def replace_before_clear(session_id, **kwargs):
        if session_id == child:
            clear(child, owner_account_id=owner)
            store.save(child, [Message.user("Separate transcript")], owner_account_id=owner)
        return clear(session_id, **kwargs)

    async def inner(envelope):
        store.save(envelope.session_id, [Message.assistant("Old reply")], owner_account_id=owner)
        yield ResponseChunk.final(rid, "Old reply")

    monkeypatch.setattr(store, "clear", replace_before_clear)
    dispatcher = SessionDispatcher(inner, store, task_runtime=runtime)
    try:
        envelope = Envelope.of("Hello", session_id=sid, request_id=rid, user_id=owner)
        frames = [frame async for frame in dispatcher.run(envelope)]
        assert frames[-1].body["outcome"]["status"] == "completed"
        assert [message.content for message in store.load(child, owner)] == ["Separate transcript"]
    finally:
        await dispatcher.shutdown()
        runtime.close()
        store.close()


def _payload(rid, sid="chat"):
    return {"session_id": sid, "request_id": rid, "query": f"hello {rid}", "send_receipt": True}


def _command(action, rid, sid="chat"):
    return {"action": action, "session_id": sid, "request_id": rid}


def _receive(socket, kind, rid):
    for _ in range(10):
        frame = socket.receive_json()
        if frame["kind"] == kind and frame.get("request_id") == rid:
            return frame
    pytest.fail(f"Gateway did not send {kind} for {rid}")


def _app(store, inbox, inner, runtime=None):
    dispatcher = SessionDispatcher(inner, store, task_runtime=runtime)
    crew = SimpleNamespace(
        active_owner=SimpleNamespace(claim=lambda _owner: None),
        session_store=store,
        config=SimpleNamespace(),
        dispatch=dispatcher.run,
    )
    app = FastAPI()
    app.state.dispatcher = dispatcher
    app.include_router(create_ws_router(
        crew, dispatcher, ConnectionManager(min_interval=0),
        SimpleNamespace(status=lambda _owner: []), send_inbox=inbox,
    ))
    return app


async def test_accept_dedupe_cancel_tombstone_and_generation(tmp_path):
    path = tmp_path / "send.db"
    store = SQLiteSessionStore(path)
    owner = "account-a"
    key = SendKey(owner, "chat", "request")
    revoked = SendKey(owner, "chat", "revoked")
    other_owner = SendKey("account-b", "chat", "request")
    try:
        inbox = _new_inbox(store)
        store_id = UUID(inbox.store_id)
        assert (await inbox.query(key)).status == "unknown"
        assert (await inbox.accept(key, _payload("request"))).status == "accepted"
        assert store.session_belongs_to("chat", owner)
        assert (await inbox.accept(key, {**_payload("request"), "query": "changed"})).code == "request_id_conflict"
        assert (await inbox.query(other_owner)).status == "unknown"
        assert (await inbox.cancel(revoked)).status == "cancelled"
        assert (await inbox.accept(revoked, _payload("revoked"))).status == "cancelled"
        assert await inbox.begin(key)
        assert not await inbox.begin(key)
        assert (await inbox.cancel(key)).status == "running"
    finally:
        store.close()
    store = SQLiteSessionStore(path)
    try:
        replacement = _new_inbox(store)
        assert UUID(replacement.store_id) == store_id
        assert (await replacement.query(key)).status == "uncertain"
        assert (await replacement.accept(key, _payload("request"))).status == "uncertain"
        assert not await replacement.begin(key)
        assert (await replacement.query(revoked)).status == "cancelled"
    finally:
        store.close()
    independent = SQLiteSessionStore(tmp_path / "another.db")
    try:
        assert UUID(independent.desktop_send_store_id) != store_id
    finally:
        independent.close()


async def test_delete_session_tombstones_accepted_receipt(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    try:
        inbox = _new_inbox(store)
        key = SendKey("local", "chat", "request")
        assert (await inbox.accept(key, _payload("request"))).status == "accepted"
        store.clear("chat", owner_account_id="local")
        assert (await inbox.query(key)).code == "session_deleted"
        assert not await inbox.begin(key)
    finally:
        store.close()


async def test_overlapping_gateways_claim_exact_request_only_once(tmp_path):
    path = tmp_path / "send.db"
    first = SQLiteSessionStore(path)
    second = SQLiteSessionStore(path)
    try:
        inboxes = [_new_inbox(first), _new_inbox(second)]
        key = SendKey("local", "chat", "same-request")
        assert [result.status for result in await asyncio.gather(
            *(inbox.accept(key, _payload("same-request")) for inbox in inboxes),
        )] == ["accepted", "accepted"]
        wins = await asyncio.gather(*(inbox.begin(key) for inbox in inboxes))
        assert wins.count(True) == 1
        winner = inboxes[wins.index(True)]
        observing = inboxes[wins.index(False)]
        assert (await observing.query(key)).status == "uncertain"
        assert (await winner.query(key)).status == "running"
        await winner.finish(key, "completed", "", started=True)
        assert (await observing.query(key)).status == "completed"
    finally:
        second.close()
        first.close()


async def test_receipt_query_does_not_require_reserved_sqlite_writer(tmp_path):
    path = tmp_path / "send.db"
    first = SQLiteSessionStore(path)
    second = SQLiteSessionStore(path)
    entered = threading.Event()
    release = threading.Event()

    def hold_transaction(_conn):
        entered.set()
        assert release.wait(timeout=10)

    write = asyncio.create_task(asyncio.to_thread(second.transaction, hold_transaction))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        key = SendKey("local", "chat", "different")
        receipt = await asyncio.wait_for(_new_inbox(first).query(key), timeout=3)
        assert receipt.status == "unknown"
    finally:
        release.set()
        await write
        second.close()
        first.close()


async def test_cancelling_claim_wait_recovers_committed_dispatch_marker(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    committed = threading.Event()
    release = threading.Event()
    key = SendKey("local", "chat", "claim")

    def paused_transaction(write):
        result = store.transaction(write)
        if not committed.is_set():
            committed.set()
            assert release.wait(timeout=10)
        return result

    inbox = DesktopSendInbox(
        paused_transaction, store.read_query, store.ensure_session_in_transaction, store.desktop_send_store_id,
    )
    claim = None
    try:
        assert (await _new_inbox(store).accept(key, _payload("claim"))).status == "accepted"
        execution = SendExecution(inbox, key)
        claim = asyncio.create_task(execution.begin())
        assert await asyncio.to_thread(committed.wait, 5)
        claim.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await claim
        assert execution.started
        await execution.finish("uncertain", "dispatch_outcome_unverified")
        assert (await inbox.query(key)).status == "uncertain"
    finally:
        release.set()
        if claim is not None and not claim.done():
            await asyncio.gather(claim, return_exceptions=True)
        store.close()


def test_websocket_recovers_accepted_before_start_but_never_reexecutes_running(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    previous = _new_inbox(store)
    accepted = SendKey("local", "chat", "accepted")
    started = SendKey("local", "chat", "started")
    withdrawing = SendKey("local", "chat", "withdrawing")
    invoked = []

    async def seed():
        await previous.accept(accepted, _payload(accepted.request_id))
        await previous.accept(started, _payload(started.request_id))
        await previous.begin(started)
        await previous.accept(withdrawing, _payload(withdrawing.request_id))

    async def execute(envelope):
        invoked.append(envelope.request_id)
        yield ResponseChunk.final(envelope.request_id, "processed")

    try:
        asyncio.run(seed())
        client = TestClient(_app(store, _new_inbox(store), execute))
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            capability = socket.receive_json()
            assert capability["kind"] == "send_receipt_capability"
            assert capability["body"] == {"version": 1, "store_id": previous.store_id, "followup_receipts": 1, "request_stop": 1}
            # A persisted offline withdrawal wins before any query resumes work.
            socket.send_json(_command("send_receipt_cancel", "withdrawing"))
            assert _receive(socket, "send_receipt", "withdrawing")["body"]["status"] == "cancelled"
            socket.send_json(_command("send_receipt_query", "accepted"))
            assert _receive(socket, "send_receipt", "accepted")["body"]["status"] == "accepted"
            assert _receive(socket, "final", "accepted")["body"]["text"] == "processed"
            socket.send_json(_command("send_receipt_query", "started"))
            assert _receive(socket, "send_receipt", "started")["body"]["status"] == "uncertain"
            socket.send_json(_payload("accepted"))
            assert _receive(socket, "send_receipt", "accepted")["body"]["status"] == "completed"
            assert invoked == ["accepted"]
    finally:
        store.close()


def test_websocket_acceptance_precedes_execution_and_stop_tombstones_busy_queue(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    inbox = _new_inbox(store)
    release = asyncio.Event()
    invoked = []

    async def execute(envelope):
        invoked.append(envelope.request_id)
        yield ResponseChunk.delta(envelope.request_id, "running")
        await release.wait()
        yield ResponseChunk.final(envelope.request_id, "processed")

    try:
        client = TestClient(_app(store, inbox, execute))
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            assert socket.receive_json()["kind"] == "send_receipt_capability"
            socket.send_json(_payload("first"))
            first = socket.receive_json()
            assert first["kind"] == "send_receipt" and first["body"]["status"] == "accepted"
            _receive(socket, "delta", "first")
            socket.send_json(_payload("second"))
            assert _receive(socket, "send_receipt", "second")["body"]["status"] == "accepted"
            socket.send_json(_command("send_receipt_cancel", "second"))
            assert _receive(socket, "send_receipt", "second")["body"]["status"] == "cancelled"
            # Stop can win before the original message reaches the Gateway.
            socket.send_json(_command("send_receipt_cancel", "third"))
            assert _receive(socket, "send_receipt", "third")["body"]["status"] == "cancelled"
            socket.send_json(_payload("third"))
            assert _receive(socket, "send_receipt", "third")["body"]["status"] == "cancelled"
            socket.portal.call(release.set)
            _receive(socket, "final", "first")
            assert invoked == ["first"]
    finally:
        store.close()


@pytest.mark.parametrize(("outcome", "wire_status", "kind"), [
    (TurnOutcome("completed"), "completed", "final"),
    (TurnOutcome("partial", reason="iteration_limit"), "failed", "final"),
    (TurnOutcome("awaiting_input", reason="plan_review"), "completed", "final"),
    (TurnOutcome("failed", reason="empty_response"), "failed", "error"),
    (TurnOutcome("stopped", reason="user_stop"), "cancelled", "error"),
])
def test_turn_outcome_matches_durable_history_and_v1_receipt(tmp_path, outcome, wire_status, kind):
    path = tmp_path / "send.db"
    store = SQLiteSessionStore(path)
    request_id = "turn"
    key = SendKey("local", "chat", request_id)

    async def execute(envelope):
        if kind == "error":
            yield ResponseChunk.error(envelope.request_id, "Turn closed", outcome=outcome)
        else:
            text = "" if outcome.status == "awaiting_input" else "Durable result"
            yield ResponseChunk.final(envelope.request_id, text, outcome=outcome)

    try:
        client = TestClient(_app(store, _new_inbox(store), execute))
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            assert socket.receive_json()["body"]["version"] == 1
            socket.send_json(_payload(request_id))
            assert _receive(socket, "send_receipt", request_id)["body"]["status"] == "accepted"
            terminal = _receive(socket, kind, request_id)["body"]["outcome"]
            assert terminal["status"] == outcome.status
            assert terminal.get("reason") == outcome.reason
            socket.send_json(_command("send_receipt_query", request_id))
            receipt = _receive(socket, "send_receipt", request_id)["body"]
            assert receipt["status"] == wire_status
            assert receipt["outcome"] == terminal
    finally:
        store.close()

    reopened = SQLiteSessionStore(path)
    try:
        receipt = asyncio.run(_new_inbox(reopened).query(key))
        assert receipt.frame()["body"]["outcome"] == terminal
        status, _, latest = reopened.get_status_details("chat", "local")
        assert status == outcome.status
        assert latest == {**terminal, "request_id": request_id, "timestamp": latest["timestamp"]}
        page = reopened.read_history_page("chat", owner_account_id="local", limit=10)
        assert page.rows[-1][2].turn_outcome == latest
        assert reopened.load("chat", owner_account_id="local") == []
    finally:
        reopened.close()


def test_failed_atomic_settlement_leaves_receipt_uncertain_and_no_false_history(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "send.db")
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    notifications = []
    runtime.set_callbacks(on_completion=notifications.append)
    inbox = _new_inbox(store)
    finish = inbox._finish_in_transaction

    def fail_after_receipt_write(conn, key, status, code, started, outcome):
        updated = finish(conn, key, status, code, started, outcome)
        if outcome is not None:
            raise RuntimeError("injected settlement failure")
        return updated

    monkeypatch.setattr(inbox, "_finish_in_transaction", fail_after_receipt_write)

    async def execute(envelope):
        yield ResponseChunk.final(envelope.request_id, "Produced but unconfirmed")

    try:
        client = TestClient(_app(store, inbox, execute, runtime))
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            socket.receive_json()
            socket.send_json(_payload("unconfirmed"))
            _receive(socket, "send_receipt", "unconfirmed")
            final = _receive(socket, "final", "unconfirmed")["body"]["outcome"]
            assert final["status"] == "failed"
            assert final["reason"] == "history_commit_unverified"
            socket.send_json(_command("send_receipt_query", "unconfirmed"))
            receipt = _receive(socket, "send_receipt", "unconfirmed")["body"]
            assert receipt == {"status": "uncertain", "code": "history_commit_unverified"}
        assert store.get_status_details("chat", "local")[2] is None
        history = store.read_history_page("chat", owner_account_id="local", limit=10)
        assert all(row[2].turn_outcome is None for row in history.rows)
        assert len(notifications) == 1
        assert notifications[0]["progress"]["turn_outcome"]["reason"] == "history_commit_unverified"
        assert notifications[0]["status"] == "failed"
    finally:
        runtime.close()
        store.close()


def test_graceful_shutdown_keeps_accepted_queued_turn_for_next_gateway(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    inbox = _new_inbox(store)
    invoked = []

    async def execute(envelope):
        invoked.append(envelope.request_id)
        yield ResponseChunk.delta(envelope.request_id, "running")
        await asyncio.Event().wait()

    try:
        app = _app(store, inbox, execute)
        client = TestClient(app)
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            assert socket.receive_json()["kind"] == "send_receipt_capability"
            socket.send_json(_payload("running"))
            _receive(socket, "send_receipt", "running")
            _receive(socket, "delta", "running")
            socket.send_json(_payload("queued"))
            assert _receive(socket, "send_receipt", "queued")["body"]["status"] == "accepted"
            _receive(socket, "status", "queued")
            socket.portal.call(app.state.dispatcher.shutdown)
            socket.send_json(_command("send_receipt_query", "queued"))
            assert _receive(socket, "send_receipt", "queued")["body"]["status"] == "accepted"
        assert invoked == ["running"]
    finally:
        store.close()


def test_stop_targets_only_the_requested_turn_across_queue_and_late_delivery(tmp_path):
    store = SQLiteSessionStore(tmp_path / "send.db")
    release = asyncio.Event()
    invoked = []

    async def execute(envelope):
        invoked.append(envelope.request_id)
        yield ResponseChunk.delta(envelope.request_id, "start")
        if envelope.request_id == "older":
            await asyncio.Event().wait()
        await release.wait()
        yield ResponseChunk.final(envelope.request_id, "done")

    def stop_reply(socket, rid):
        for _ in range(25):
            frame = socket.receive_json()
            receipt = frame.get("body", {}).get("stop")
            if receipt and receipt["request_id"] == rid:
                return receipt["state"]
        pytest.fail("No targeted stop receipt")

    try:
        app = _app(store, _new_inbox(store), execute)
        with TestClient(app).websocket_connect("/ws?send_receipts=1") as socket:
            assert socket.receive_json()["body"]["request_stop"] == 1
            socket.send_json(_payload("older"))
            _receive(socket, "delta", "older")
            socket.send_json(_payload("newer"))
            _receive(socket, "send_receipt", "newer")
            socket.send_json(_command("stop", "older"))
            assert stop_reply(socket, "older") == "requested"
            _receive(socket, "delta", "newer")
            socket.send_json(_command("stop", "older"))
            assert stop_reply(socket, "older") == "not_active"
            socket.portal.call(release.set)
            _receive(socket, "final", "newer")
            assert invoked == ["older", "newer"]
    finally:
        store.close()
