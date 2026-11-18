"""Plan decisions commit their exact snapshot and accepted execution together."""

from __future__ import annotations

import asyncio
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crew.agent.plan import PlanModeManager, PlanVersionConflict, plan_path, write_plan
from crew.core.envelope import Envelope, ResponseChunk
from crew.gateway.connections import ConnectionManager
from crew.gateway.dispatcher import SessionDispatcher
from crew.gateway.plan_receipts import PlanReceiptJournal, PlanSubmissionKey, plan_submission_hash
from crew.gateway.send_inbox import DesktopSendInbox, SendKey
from crew.gateway.ws import create_ws_router
from crew.state.session_store import SQLiteSessionStore


@pytest.fixture
def plan_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    with closing(SQLiteSessionStore(str(tmp_path / "state.db"))) as store:
        store.ensure_session("s", owner_account_id="local")
        manager = PlanModeManager(session_store=store)
        manager.enter("s", owner_account_id="local")
        write_plan("s", "original plan", owner_account_id="local")
        manager.submit_review("s", owner_account_id="local")
        inbox = DesktopSendInbox(
            store.transaction, store.read_query, store.ensure_session_in_transaction, store.desktop_send_store_id,
        )
        yield store, manager, inbox


def _command(manager, action="plan_approve", **kwargs):
    return {
        "action": action, "session_id": "s", "submission_id": "decision",
        "request_id": "execution", "expected_version": manager.snapshot("s", "local").version,
        **kwargs,
    }


async def test_plan_decision_commits_state_and_execution_idempotently(plan_fixture):
    store, manager, inbox = plan_fixture
    command = _command(manager, plan="edited plan")
    key = PlanSubmissionKey.from_frame("local", "s", command)
    journal = PlanReceiptJournal(store, inbox)
    transition = manager.prepare_transition("s", key.expected_version, "plan_approve", "edited plan", "local")
    payload = {"session_id": "s", "request_id": "execution", "query": "execute approved plan"}
    receipt = await journal.commit(key, plan_submission_hash(command), transition, payload)
    assert receipt.status == "applied" and receipt.execution_request_id == "execution"
    assert store.load_plan_state("local", "s")["plan"] == "edited plan"
    assert store.pending_plan_execution("local", "s", transition.target.version)["execution_status"] == "accepted"
    assert (await inbox.query(SendKey("local", "s", "execution"))).status == "accepted"
    replay = await journal.commit(key, plan_submission_hash(command), transition, payload)
    assert replay == receipt
    changed = await journal.query(key, plan_submission_hash({**command, "plan": "different"}))
    assert changed.status == "rejected" and changed.code == "submission_conflict"
    assert await journal.execution_state("local", "s", "execution") == transition.target.to_body()
    assert await journal.execution_state("foreign", "s", "execution") is None
    restored = PlanModeManager(session_store=store)
    assert restored.snapshot("s", "local").to_body() == transition.target.to_body()
    store.clear("s", owner_account_id="local")
    assert store.load_plan_state("local", "s") is None
    assert (await journal.query(key)).status == "unknown"
    assert store.pending_plan_execution("local", "s", transition.target.version) == {}


async def test_concurrent_plan_change_cannot_enqueue_stale_approval(plan_fixture):
    store, manager, inbox = plan_fixture
    command = _command(manager)
    key = PlanSubmissionKey.from_frame("local", "s", command)
    transition = manager.prepare_transition("s", key.expected_version, "plan_approve", owner_account_id="local")
    manager.update_plan("s", "newer plan", owner_account_id="local")
    current = manager.snapshot("s", "local")
    receipt = await PlanReceiptJournal(store, inbox).commit(
        key, plan_submission_hash(command), transition,
        {"session_id": "s", "request_id": "execution", "query": "must not execute"},
    )
    assert receipt.status == "expired" and receipt.code == "stale_plan"
    assert manager.snapshot("s", "local") == current
    assert (await inbox.query(SendKey("local", "s", "execution"))).status == "unknown"


async def test_failed_state_commit_rolls_back_queued_execution(plan_fixture, monkeypatch):
    store, manager, inbox = plan_fixture
    command = _command(manager)
    key = PlanSubmissionKey.from_frame("local", "s", command)
    transition = manager.prepare_transition("s", key.expected_version, "plan_approve", owner_account_id="local")
    journal = PlanReceiptJournal(store, inbox)
    original = store.write_plan_state_in_transaction

    def fail_after_state_write(*args):
        original(*args)
        raise OSError("injected state commit failure")

    monkeypatch.setattr(store, "write_plan_state_in_transaction", fail_after_state_write)
    with pytest.raises(OSError, match="injected"):
        await journal.commit(key, plan_submission_hash(command), transition, {
            "session_id": "s", "request_id": "execution", "query": "must not execute",
        })
    assert (await journal.query(key)).status == "unknown"
    assert (await inbox.query(SendKey("local", "s", "execution"))).status == "unknown"
    assert store.load_plan_state("local", "s")["version"] == key.expected_version


def _client(store, manager, inbox, calls):
    async def inner(envelope):
        calls.append(envelope)
        yield ResponseChunk.final(envelope.request_id, "executed")

    dispatcher = SessionDispatcher(inner, store, controller=SimpleNamespace(plan_manager=manager))
    crew = SimpleNamespace(
        session_store=store, plan_manager=manager, dispatch=dispatcher.run,
        config=SimpleNamespace(), active_owner=SimpleNamespace(claim=lambda owner: None),
    )
    app = FastAPI()
    app.include_router(create_ws_router(
        crew, dispatcher, ConnectionManager(min_interval=0), SimpleNamespace(status=lambda owner: []), send_inbox=inbox,
    ))
    return TestClient(app)


def _receive(socket, kind, rid):
    for _ in range(12):
        frame = socket.receive_json()
        if frame["kind"] == kind and frame.get("request_id") == rid:
            return frame
        if frame["kind"] == "error" and frame.get("request_id") == rid:
            pytest.fail(f"Gateway execution failed: {frame['body']}")
    pytest.fail(f"missing {kind}")


def test_ws_approval_uses_frozen_snapshot_once(plan_fixture):
    store, manager, inbox = plan_fixture
    calls = []
    command = _command(manager, plan="approved exact text")
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        assert socket.receive_json()["body"]["plan_receipts"] == 1
        socket.send_json(command)
        receipt = _receive(socket, "interaction_receipt", "execution")
        assert receipt["body"]["status"] == "applied"
        assert receipt["body"]["plan_state"]["plan"] == "approved exact text"
        _receive(socket, "final", "execution")
        assert len(calls) == 1
        assert calls[0].params["approved_plan"]["plan"] == "approved exact text"
        socket.send_json(command)
        assert _receive(socket, "interaction_receipt", "execution")["body"]["status"] == "applied"
        socket.send_json({**command, "action": "plan_receipt_query"})
        assert _receive(socket, "interaction_receipt", "execution")["body"]["status"] == "applied"
        assert len(calls) == 1


def test_ws_stale_edited_approval_keeps_current_plan(plan_fixture):
    store, manager, inbox = plan_fixture
    calls = []
    stale = _command(manager, plan="stale draft")
    manager.update_plan("s", "newer visible plan", owner_account_id="local")
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        socket.receive_json()
        socket.send_json(stale)
        result = _receive(socket, "interaction_receipt", "execution")["body"]
        assert result["status"] == "expired" and result["code"] == "stale_plan"
        assert result["plan_state"]["plan"] == "newer visible plan"
        assert calls == []
        assert manager.snapshot("s", "local").text == "newer visible plan"


def test_ws_query_of_committed_approval_does_not_start_execution(plan_fixture):
    store, manager, inbox = plan_fixture
    command = _command(manager)
    key = PlanSubmissionKey.from_frame("local", "s", command)
    transition = manager.prepare_transition("s", key.expected_version, "plan_approve", owner_account_id="local")
    asyncio.run(PlanReceiptJournal(store, inbox).commit(key, plan_submission_hash(command), transition, {
        "session_id": "s", "request_id": "execution", "query": "resume after confirmation",
        "_plan_submission_id": "decision",
    }))
    calls = []
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        socket.receive_json()
        socket.send_json({**command, "action": "plan_receipt_query"})
        assert _receive(socket, "interaction_receipt", "execution")["body"]["execution_request_id"] == "execution"
        assert calls == []
        socket.send_json({"action": "send_receipt_query", "session_id": "s", "request_id": "execution"})
        _receive(socket, "final", "execution")
        assert len(calls) == 1


def test_ws_revision_preserves_exact_plan_and_feedback(plan_fixture):
    store, manager, inbox = plan_fixture
    command = _command(manager, action="plan_reject", plan="plan to revise", feedback="change the final step")
    calls = []
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        socket.receive_json()
        socket.send_json(command)
        receipt = _receive(socket, "interaction_receipt", "execution")
        assert receipt["body"]["status"] == "applied"
        assert receipt["body"]["plan_state"]["phase"] == "revising"
        _receive(socket, "final", "execution")
        assert len(calls) == 1
        assert calls[0].query == "change the final step"
        assert calls[0].params["plan_revision"]["plan"] == "plan to revise"
        assert "approved_plan" not in calls[0].params


def test_ws_superseded_queued_plan_reports_terminal_failure(plan_fixture):
    store, manager, inbox = plan_fixture
    command = _command(manager)
    key = PlanSubmissionKey.from_frame("local", "s", command)
    transition = manager.prepare_transition("s", key.expected_version, "plan_approve", owner_account_id="local")
    asyncio.run(PlanReceiptJournal(store, inbox).commit(key, plan_submission_hash(command), transition, {
        "session_id": "s", "request_id": "execution", "query": "superseded approval",
        "_plan_submission_id": "decision",
    }))
    manager.enter("s", owner_account_id="local")
    calls = []
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        socket.receive_json()
        socket.send_json({"action": "send_receipt_query", "session_id": "s", "request_id": "execution"})
        error = _receive(socket, "error", "execution")
        assert error["body"]["outcome"]["reason"] == "plan_superseded"
        assert not calls
    status, _, outcome = store.get_status_details("s", "local")
    assert status == "failed" and outcome["request_id"] == "execution"
    assert asyncio.run(inbox.query(SendKey("local", "s", "execution"))).status == "failed"


def test_external_edit_after_commit_preserves_receipt_and_cancels_old_execution(plan_fixture, monkeypatch):
    store, manager, inbox = plan_fixture
    command = _command(manager)
    calls = []

    def external_change(transition):
        plan_path("s", "local").write_text("externally changed", encoding="utf-8", newline="")
        raise PlanVersionConflict(transition.target)

    monkeypatch.setattr(manager, "apply_committed_transition", external_change)
    with _client(store, manager, inbox, calls).websocket_connect("/ws?send_receipts=1") as socket:
        socket.receive_json()
        socket.send_json(command)
        receipt = _receive(socket, "interaction_receipt", "execution")["body"]
        assert receipt["status"] == "applied"
        assert receipt["plan_state"]["plan"] == "externally changed"
        assert receipt["plan_state"]["phase"] == "active"
        assert _receive(socket, "error", "execution")["body"]["code"] == "plan_superseded"
        socket.send_json({**command, "action": "plan_receipt_query"})
        assert _receive(socket, "interaction_receipt", "execution")["body"]["status"] == "applied"
        assert not calls


async def test_queued_plan_is_revalidated_at_dispatch_admission(plan_fixture):
    store, manager, _ = plan_fixture
    manager.approve("s", owner_account_id="local")
    approved = manager.snapshot("s", "local").to_body()
    blocked = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def inner(envelope):
        calls.append(envelope.request_id)
        if envelope.request_id == "first":
            blocked.set()
            await release.wait()
        yield ResponseChunk.final(envelope.request_id, "finished")

    dispatcher = SessionDispatcher(inner, store, controller=SimpleNamespace(plan_manager=manager))

    async def drain(envelope):
        return [frame async for frame in dispatcher.run(envelope)]

    first = asyncio.create_task(drain(Envelope.of("hold", "s", user_id="local", request_id="first")))
    await asyncio.wait_for(blocked.wait(), 2)
    second = asyncio.create_task(drain(Envelope.of(
        "approved action", "s", user_id="local", request_id="second", params={"approved_plan": approved},
    )))
    await asyncio.sleep(0)
    manager.enter("s", owner_account_id="local")
    release.set()
    await first
    frames = await second
    assert calls == ["first"]
    assert frames[-1].body["outcome"]["reason"] == "plan_superseded"
