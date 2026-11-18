"""Follow-up decisions are scoped, replayable and ordered before resumed output."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.followup import FollowupAnswerMessage, FollowupWaiter, get_followup_waiter, normalize_followup_answers, validate_questions
from crew.core.runctx import current_owner_account_id, current_request_id
from crew.core.types import Message
from crew.gateway.connections import ConnectionManager
from crew.gateway.dispatcher import SessionDispatcher
from crew.gateway.followup_receipts import (
    FollowupKey, FollowupReceipt, FollowupReceiptJournal, followup_decision_hash,
)
from crew.gateway.ws import create_ws_router
from crew.gateway.routers.sessions import read_session_history_window
from crew.plugins.manager import PluginManager
from crew.state.session_store import SQLiteSessionStore, SessionWriteConflict
from crew.tasks.runtime import TaskRuntime

_QUESTIONS = validate_questions([{
    "id": "q1", "question": "Choose", "options": [{"label": "Option B", "value": "B"}],
    "allowFreeText": False,
}])
_ANSWERS = [{"question_id": "q1", "answers": ["B"]}]


@pytest.fixture(autouse=True)
def isolate_followup_waiter(monkeypatch):
    monkeypatch.setattr("crew.core.followup._followup_waiter", FollowupWaiter())


async def test_waiter_binds_owner_turn_and_immutable_question(monkeypatch):
    waiter = FollowupWaiter()
    monkeypatch.setattr("crew.core.followup.uuid.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    questions = validate_questions([{
        "id": "q1", "question": "Choose", "options": ["B"], "allowFreeText": False,
    }])
    qid = waiter.create("shared", questions, owner_account_id="A", request_id="turn-A")
    waiter.create("shared", _QUESTIONS, owner_account_id="B", request_id="turn-B")
    questions[0]["options"][0]["value"] = "edited-after-publication"
    assert waiter.validated_answer("shared", qid, _ANSWERS, owner_account_id="A", request_id="turn-A")[0]
    assert not waiter.resolve("shared", qid, _ANSWERS, owner_account_id="A", request_id="turn-B")
    assert not waiter.resolve("shared", qid, _ANSWERS, owner_account_id="C", request_id="turn-A")
    assert not waiter.validated_answer(
        "shared", qid, [{"question_id": "q1", "answers": ["not-offered"]}],
        owner_account_id="A", request_id="turn-A",
    )[0]
    assert waiter.resolve("shared", qid, _ANSWERS, owner_account_id="A", request_id="turn-A")
    assert await waiter.wait("shared", qid, owner_account_id="A") == _ANSWERS
    assert waiter.is_waiting("shared", qid, owner_account_id="B")
    owner_token = current_owner_account_id.set("A")
    request_token = current_request_id.set("turn-A")
    try:
        records = waiter.drain_answer_records("shared")
        assert [(row.history_id, row.text) for row in records] == [(f"followup:{qid}", "已选择：B")]
        assert waiter.drain_answer_records("shared") == []
    finally:
        current_request_id.reset(request_token)
        current_owner_account_id.reset(owner_token)
        waiter.discard("shared", qid, owner_account_id="B")


@pytest.mark.parametrize("value", [
    [], [{"id": "q1", "answers": []}], [{"id": "q1", "answers": [True]}],
    [{"id": "q1", "answers": ["B", "B"]}], [{"id": "__cancelled__", "answers": ["B"]}],
    [{"id": "q1", "question_id": "q2", "answers": ["B"]}],
])
def test_answer_wire_rejects_ambiguous_or_invalid_payload(value):
    assert normalize_followup_answers(value) is None


def test_preliminary_receipt_database_migrates_event_reference(tmp_path):
    path = tmp_path / "preliminary.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE followup_receipts (owner_account_id TEXT, session_id TEXT, request_id TEXT, "
            "question_id TEXT, submission_id TEXT, decision_hash TEXT, generation TEXT, status TEXT, "
            "code TEXT DEFAULT '', answer_history_id TEXT DEFAULT '', updated_at REAL, "
            "PRIMARY KEY(owner_account_id, session_id, request_id, question_id))"
        )
        conn.commit()
    with closing(SQLiteSessionStore(str(path))) as store:
        columns = store.read_query(lambda conn: {row[1] for row in conn.execute("PRAGMA table_info(followup_receipts)")})
        assert "answer_seq" in columns


async def test_waiter_release_gate_and_cancel_clear_pending(monkeypatch):
    waiter = FollowupWaiter()
    qid = waiter.create("s", _QUESTIONS, request_id="r")
    _, answer = waiter.validated_answer(
        "s", qid, _ANSWERS, owner_account_id=current_owner_account_id.get(), request_id="r",
    )
    release = asyncio.Event()
    waiting = asyncio.create_task(waiter.wait("s", qid))
    await asyncio.sleep(0)
    assert waiter.resolve("s", qid, _ANSWERS, release=release)
    await asyncio.sleep(0)
    assert not waiter.is_waiting("s", qid) and not waiting.done()
    assert waiter.commit_resolution(
        "s", qid, owner_account_id=current_owner_account_id.get(), request_id="r", answer=answer,
    )
    release.set()
    assert await waiting == _ANSWERS
    request_token = current_request_id.set("r")
    try:
        assert len(waiter.drain_answer_records("s")) == 1
    finally:
        current_request_id.reset(request_token)

    qid = waiter.create("s", _QUESTIONS, record_history=False)
    monkeypatch.setattr("crew.core.followup._MAX_RECEIPT_RELEASE_SECONDS", 0.01)
    assert waiter.cancel("s", qid, release=asyncio.Event())
    result = await asyncio.wait_for(waiter.wait("s", qid), timeout=1)
    assert result[0]["id"] == "__cancelled__"
    assert waiter.drain_answer_records("s") == []

    qid = waiter.create("s", _QUESTIONS, record_history=False)
    assert waiter.resolve("s", qid, _ANSWERS, release=asyncio.Event())
    assert waiter.commit_resolution("s", qid, owner_account_id=current_owner_account_id.get(), request_id="")
    assert await asyncio.wait_for(waiter.wait("s", qid), timeout=1) == _ANSWERS


async def test_journal_cas_conflict_owner_restart_and_abandoned_claim(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        for owner in ("A", "B"):
            store.ensure_session("s", owner_account_id=owner)
        journal = FollowupReceiptJournal(store.transaction, store.read_query)
        key = FollowupKey("A", "s", "r", "q", "submission")
        digest = followup_decision_hash("followup_answer", _ANSWERS)
        assert (await journal.query(key)).status == "unknown"
        assert await journal.claim(key, digest) is None
        assert (await journal.query(key)).status == "unavailable"
        assert (await journal.claim(replace(key, submission_id="other"), digest)).status == "rejected"
        await journal.finish(FollowupReceipt(key, "applied", answer_history_id="followup:q"))
        journal.release(key)
        assert (await journal.query(key, digest)).frame()["body"]["answer_history_id"] == "followup:q"
        assert (await journal.query(key, "different-answer")).code == "submission_conflict"
        assert (await journal.query(replace(key, owner="B"))).status == "unknown"
        restarted = FollowupReceiptJournal(store.transaction, store.read_query)
        assert (await restarted.query(key)).status == "applied"
        pending = replace(key, question_id="pending")
        assert await journal.claim(pending, digest) is None
        assert (await restarted.query(pending)).status == "uncertain"
        journal.release(pending)
        assert (await journal.query(pending)).status == "uncertain"
        store.clear("s", owner_account_id="A")
        assert (await restarted.query(key)).status == "unknown"


def test_canonical_answer_identity_survives_reload_and_is_hidden_from_provider(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        message = Message.user("已选择：Option B")
        message.followup_history_id = "followup:question"
        store.save("s", [Message.user("Original"), message], owner_account_id="A")
        loaded = store.load("s", owner_account_id="A")
        assert loaded[-1].followup_history_id == "followup:question"
        assert loaded[-1].to_openai() == {"role": "user", "content": "已选择：Option B"}
        history = read_session_history_window(store, "s", "A", 10)
        assert history["items"][-1]["history_id"] == "followup:question"


async def test_receipt_and_answer_commit_atomically_and_survive_before_waiter_drain(tmp_path):
    path = str(tmp_path / "followup.db")
    with closing(SQLiteSessionStore(path)) as store:
        store.ensure_session("s", owner_account_id="A")
        journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
        key = FollowupKey("A", "s", "r", "q", "submit")
        digest = followup_decision_hash("followup_answer", _ANSWERS)
        answer = FollowupAnswerMessage("followup:q", "Selected B")
        assert await journal.claim(key, digest) is None

        def broken_append(*args, **kwargs):
            store.append_followup_answer_in_transaction(*args, **kwargs)
            raise OSError("fixture failure after answer insert")
        journal._persist_answer = broken_append
        with pytest.raises(OSError):
            await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        assert store.load("s", owner_account_id="A") == []
        journal._persist_answer = store.append_followup_answer_in_transaction
        await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        with closing(SQLiteSessionStore(path)) as reopened:
            saved, = reopened.load("s", owner_account_id="A")
            assert saved.content == answer.text and saved.followup_history_id == answer.history_id
            assert saved.request_id == "r"
            assert (await FollowupReceiptJournal(reopened.transaction, reopened.read_query).query(key)).status == "applied"
        assert [message.followup_history_id for message in store.accepted_followup_answers("s", "r", owner_account_id="A")] == ["followup:q"]
        assert store.accepted_followup_answers("s", "other-turn", owner_account_id="A") == []
        with pytest.raises(SessionWriteConflict):
            store.accepted_followup_answers("s", "r", owner_account_id="B")


@pytest.mark.parametrize(("cancelled", "include_answer"), [(False, True), (False, False), (True, False)])
async def test_sidechain_convergence_retains_exactly_one_accepted_answer(tmp_path, monkeypatch, cancelled, include_answer):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    with closing(SQLiteSessionStore(str(tmp_path / "history.db"))) as store, closing(TaskRuntime(str(tmp_path / "tasks.db"))) as runtime:
        ready = asyncio.Event()
        release = asyncio.Event()
        owner = "local"
        envelope = Envelope.of("Question", session_id="s", user_id=owner, request_id="r")

        async def inner(actual):
            messages = store.load(actual.session_id, owner_account_id=owner)
            messages.extend([
                replace(Message.user("Question"), request_id="r"),
                replace(Message.assistant("Before answer"), request_id="r"),
            ])
            store.save(actual.session_id, messages, owner_account_id=owner)
            ready.set()
            yield ResponseChunk.delta("r", "Before answer")
            await release.wait()
            if include_answer:
                messages.extend(store.accepted_followup_answers("s", "r", owner_account_id=owner))
            messages.append(replace(Message.assistant("After answer"), request_id="r"))
            store.save(actual.session_id, messages, owner_account_id=owner)
            yield ResponseChunk.final("r", "After answer")

        dispatcher = SessionDispatcher(inner, store, task_runtime=runtime)
        async def consume():
            return [frame async for frame in dispatcher.run(envelope)]
        task = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), timeout=2)
        journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
        key = FollowupKey(owner, "s", "r", "q", "submit")
        await journal.claim(key, followup_decision_hash("followup_answer", _ANSWERS))
        answer = FollowupAnswerMessage("followup:q", "Selected B")
        await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        if cancelled:
            await dispatcher.stop("s", owner_account_id=owner)
        else:
            release.set()
        await asyncio.wait_for(task, timeout=3)
        messages = store.load("s", owner_account_id=owner)
        assert [message.content for message in messages if message.followup_history_id] == ["Selected B"]
        assert sum(message.followup_history_id == answer.history_id for message in messages) == 1
        page = read_session_history_window(store, "s", owner, 20)
        assert sum(item.get("history_id") == answer.history_id for item in page["items"]) == 1, [
            (item["role"], item["content"], item.get("history_id")) for item in page["items"]
        ]


async def test_convergence_reprepares_when_answer_commits_after_save_is_prepared(tmp_path, monkeypatch):
    with closing(SQLiteSessionStore(str(tmp_path / "history.db"))) as store:
        question = replace(Message.user("Question"), request_id="r")
        store.save("s", [question], owner_account_id="A")
        journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
        key = FollowupKey("A", "s", "r", "q", "submit")
        await journal.claim(key, followup_decision_hash("followup_answer", _ANSWERS))
        prepared = threading.Event()
        release = threading.Event()
        execute = store._writer.execute
        attempts = []

        def pause_first_write(write):
            if not attempts:
                attempts.append(1)
                prepared.set()
                assert release.wait(timeout=3)
            return execute(write)

        monkeypatch.setattr(store._writer, "execute", pause_first_write)
        dispatcher = SessionDispatcher(lambda _: None, store)
        task = asyncio.create_task(dispatcher._store_converged_history(
            "s", "r", [question, replace(Message.assistant("Done"), request_id="r")],
            workspace_id="default", owner="A", title_fallback=None,
        ))
        try:
            assert await asyncio.to_thread(prepared.wait, 3)
            answer = FollowupAnswerMessage("followup:q", "Selected B")
            await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=3)
        with closing(SQLiteSessionStore(str(tmp_path / "history.db"))) as reopened:
            saved = reopened.load("s", owner_account_id="A")
            assert [message.content for message in saved if message.followup_history_id] == ["Selected B"]
            assert [message.content for message in saved if message.role == "assistant"] == ["Done"]


async def test_accepted_answer_uses_standard_fork_and_rewind_boundary(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "history.db"))) as store:
        store.save("s", [replace(Message.user("Question"), request_id="r")], owner_account_id="A")
        journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
        key = FollowupKey("A", "s", "r", "q", "submit")
        await journal.claim(key, followup_decision_hash("followup_answer", _ANSWERS))
        answer = FollowupAnswerMessage("followup:q", "Selected B")
        await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        rows, _, _ = store.load_window("s", owner_account_id="A", limit=20)
        cut = rows[-1][1]
        forked = store.fork("s", cut, owner_account_id="A")
        assert store.load(forked, owner_account_id="A")[-1].followup_history_id == answer.history_id
        store.rewind("s", rows[0][1], owner_account_id="A")
        assert all(message.followup_history_id is None for message in store.load("s", owner_account_id="A"))


async def test_convergence_conflict_retries_are_bounded_and_keep_committed_answer(tmp_path, monkeypatch):
    with closing(SQLiteSessionStore(str(tmp_path / "history.db"))) as store:
        store.ensure_session("s", owner_account_id="A")
        journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
        key = FollowupKey("A", "s", "r", "q", "submit")
        await journal.claim(key, followup_decision_hash("followup_answer", _ANSWERS))
        answer = FollowupAnswerMessage("followup:q", "Selected B")
        await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
        attempts = []
        def changed(*args, **kwargs):
            attempts.append(kwargs["expected_cursor"])
            raise SessionWriteConflict("fixture concurrent change")
        monkeypatch.setattr(store, "save", changed)
        dispatcher = SessionDispatcher(lambda _: None, store)
        with pytest.raises(SessionWriteConflict):
            await dispatcher._store_converged_history(
                "s", "r", [], workspace_id="default", owner="A", title_fallback=None,
            )
        assert len(attempts) == 3
        saved, = store.load("s", owner_account_id="A")
        assert saved.followup_history_id == answer.history_id and saved.content == answer.text


def _client(store, connections=None):
    connections = connections or ConnectionManager(min_interval=0)
    app = FastAPI()
    app.include_router(create_ws_router(
        SimpleNamespace(
            session_store=store, config=SimpleNamespace(),
            active_owner=SimpleNamespace(claim=lambda owner: None),
        ),
        SimpleNamespace(), connections, SimpleNamespace(),
    ))
    return TestClient(app), connections


def _receive(socket, kind):
    for _ in range(5):
        frame = socket.receive_json()
        if frame["kind"] == kind:
            return frame
    pytest.fail(f"Did not receive {kind}")


@pytest.mark.parametrize(("questions", "answers", "answer_text"), [
    pytest.param(_QUESTIONS, _ANSWERS, "已选择：Option B", id="single-choice"),
    pytest.param(
        validate_questions([
            {
                "id": "q1", "question": "Supported platforms", "multiSelect": True,
                "options": [{"label": "Windows", "value": "win"}, {"label": "Linux", "value": "linux"}],
            },
            {"id": "q2", "question": "Deployment note", "inputMode": "text"},
        ]),
        [{"question_id": "q1", "answers": ["linux", "win"]},
         {"question_id": "q2", "answers": ["保留现有会话"]}],
        "已补充：Supported platforms：Linux, Windows；Deployment note：保留现有会话",
        id="multiple-choice-and-text",
    ),
])
def test_ws_receipt_replay_and_response_precede_resumed_stream(tmp_path, questions, answers, answer_text):
    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        store.ensure_session("s", owner_account_id="local")
        client, connections = _client(store)
        waiter = get_followup_waiter()
        tasks = []
        consumed = []

        async def create():
            qid = waiter.create("s", questions, owner_account_id="local", request_id="r")

            async def resume():
                consumed.append(await waiter.wait("s", qid, owner_account_id="local"))
                await connections.push_payload("s", {
                    "kind": "delta", "request_id": "r", "body": {"text": "resumed"},
                }, owner_account_id="local")

            tasks.append(asyncio.create_task(resume()))
            return qid

        with client:
            qid = client.portal.call(create)
            command = {
                "action": "followup_answer", "session_id": "s", "request_id": "r",
                "question_id": qid, "submission_id": "submit", "answers": answers,
            }
            with client.websocket_connect("/ws") as socket:
                socket.send_json(command)
                resolution = socket.receive_json()
                receipt = socket.receive_json()
                resumed = socket.receive_json()
                assert resolution["kind"] == "followup_question"
                assert resolution["body"]["answer_text"] == answer_text
                assert receipt["kind"] == "interaction_receipt" and receipt["body"]["status"] == "applied"
                assert receipt["body"]["answer_history_id"] == f"followup:{qid}"
                assert resumed["kind"] == "delta" and consumed == [answers]
                socket.send_json(command)
                assert socket.receive_json() == receipt
                socket.send_json({**command, "answers": [{"question_id": "q1", "answers": ["altered"]}]})
                assert _receive(socket, "interaction_receipt")["body"]["code"] == "submission_conflict"
            with client.websocket_connect("/ws") as socket:
                socket.send_json({**command, "action": "followup_receipt_query"})
                assert socket.receive_json() == receipt

            async def finish():
                await asyncio.gather(*tasks)
            client.portal.call(finish)

        restarted, _ = _client(store)
        with restarted.websocket_connect("/ws") as socket:
            socket.send_json({**command, "action": "followup_receipt_query"})
            assert socket.receive_json() == receipt

        history = read_session_history_window(store, "s", "local", 20)
        recorded = [item for item in history["items"] if item.get("history_id") == f"followup:{qid}"]
        assert [(item["role"], item["content"]) for item in recorded] == [("user", answer_text)]


@pytest.mark.parametrize("modern", [False, True])
def test_ws_cancellation_is_confirmed_and_does_not_create_answer_history(tmp_path, modern):
    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        store.ensure_session("s", owner_account_id="local")
        client, _ = _client(store)
        waiter = get_followup_waiter()

        async def create():
            return waiter.create("s", _QUESTIONS, owner_account_id="local", request_id="r", record_history=False)

        with client:
            qid = client.portal.call(create)
            command = {"action": "followup_cancel", "session_id": "s", "question_id": qid, "request_id": "r"}
            if modern:
                command["submission_id"] = "cancel"
            with client.websocket_connect("/ws") as socket:
                socket.send_json(command)
                assert socket.receive_json()["body"]["status"] == "cancelled"
                if modern:
                    receipt = socket.receive_json()
                    assert receipt["body"]["status"] == "cancelled" and "answer_history_id" not in receipt["body"]

            async def verify():
                result = await waiter.wait("s", qid, owner_account_id="local")
                assert result[0]["id"] == "__cancelled__"
            client.portal.call(verify)


def test_ws_unknown_stale_and_invalid_choices_never_resolve_waiter(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        store.ensure_session("s", owner_account_id="local")
        client, _ = _client(store)
        waiter = get_followup_waiter()

        async def create():
            return waiter.create("s", _QUESTIONS, owner_account_id="local", request_id="r", record_history=False)

        with client:
            qid = client.portal.call(create)
            command = {
                "action": "followup_answer", "session_id": "s", "request_id": "r",
                "question_id": qid, "submission_id": "answer",
            }
            try:
                with client.websocket_connect("/ws") as socket:
                    socket.send_json({**command, "action": "followup_receipt_query"})
                    assert socket.receive_json()["body"]["status"] == "unknown"
                    socket.send_json({**command, "answers": [{"id": "q1", "answers": ["unoffered"]}]})
                    assert socket.receive_json()["body"]["status"] == "rejected"
                    socket.send_json({**command, "request_id": "old", "answers": _ANSWERS})
                    assert socket.receive_json()["body"]["status"] == "expired"
                    assert waiter.is_waiting("s", qid, owner_account_id="local", request_id="r")
            finally:
                waiter.discard("s", qid, owner_account_id="local")


@pytest.mark.parametrize("failure", [None, "write", "deadline"])
def test_tool_permission_runs_only_after_durable_confirmation(tmp_path, monkeypatch, failure):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.core.runctx import current_display_session_id, current_push_fn, current_session_id
    from crew.core.types import ToolCall, ToolPermissionDecision
    from crew.tools.registry import Registry

    original_finish = FollowupReceiptJournal.finish
    if failure is not None:
        async def fail_or_delay(self, receipt, **kwargs):
            if failure == "write":
                raise OSError("fixture write failed")
            await asyncio.sleep(0.08)
            await original_finish(self, receipt, **kwargs)
        monkeypatch.setattr(FollowupReceiptJournal, "finish", fail_or_delay)
        monkeypatch.setattr("crew.core.followup._MAX_RECEIPT_RELEASE_SECONDS", 0.02)

    with closing(SQLiteSessionStore(str(tmp_path / "followup.db"))) as store:
        store.ensure_session("s", owner_account_id="local")
        client, connections = _client(store)
        calls = []
        registry = Registry()
        registry.register(
            name="fixture_mutation", toolset="fixture", schema={"name": "fixture_mutation", "parameters": {}},
            handler=lambda args: calls.append(args) or "changed",
            permission_resolver=lambda args: ToolPermissionDecision("ask", allow_always=False),
        )

        async def begin():
            current_owner_account_id.set("local")
            current_request_id.set("r")
            current_display_session_id.set("s")
            current_session_id.set("s")

            async def push(sid, frame):
                await connections.push_payload(sid, frame, owner_account_id="local")
            current_push_fn.set(push)
            runner = ToolRunner(
                registry, PluginManager(),
                ToolCallGuardrailController(ToolCallGuardrailConfig()),
                session_id="s",
            )
            messages = []

            async def run():
                async for _ in runner.run_batch([ToolCall("tool", "fixture_mutation", {"path": "fixture.txt"})], messages, "r", lambda: 1):
                    pass
                return messages
            return asyncio.create_task(run())

        with client, client.websocket_connect("/ws") as socket:
            socket.send_json({
                "action": "subscribe", "session_id": "s", "sessions": ["s"],
                "gateway_cursors": {"s": {"epoch": "previous", "sequence": 0}},
            })
            _receive(socket, "gateway_resync")
            task = client.portal.call(begin)
            prompt = _receive(socket, "followup_question")
            assert prompt["body"]["record_history"] is False and prompt["request_id"] == "r"
            qid = prompt["body"]["question_id"]
            command = {
                "action": "followup_answer", "session_id": "s", "request_id": "r",
                "question_id": qid, "submission_id": "approval", "answers": [{"id": "perm", "answers": ["allow_once"]}],
            }
            socket.send_json(command)
            receipt = _receive(socket, "interaction_receipt")

            async def complete():
                return await asyncio.wait_for(task, timeout=2)
            messages = client.portal.call(complete)
            if failure is None:
                assert receipt["body"]["status"] == "applied"
                assert calls == [{"path": "fixture.txt"}]
            else:
                assert receipt["body"]["status"] == "uncertain"
                assert calls == []
                socket.send_json({**command, "action": "followup_receipt_query"})
                assert _receive(socket, "interaction_receipt")["body"]["status"] == "uncertain"
            assert all(message.role != "user" for message in messages)


async def _record_indexed_answer(store, owner, sid, request, question, text):
    journal = FollowupReceiptJournal(store.transaction, store.read_query, store.append_followup_answer_in_transaction)
    key = FollowupKey(owner, sid, request, question, "submit")
    answer = FollowupAnswerMessage(f"followup:{question}", text)
    assert await journal.claim(key, followup_decision_hash("followup_answer", _ANSWERS)) is None
    await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
    return journal, key


async def test_receipt_index_resolves_reordered_answers_in_current_event_order(tmp_path):
    path, owner, sid, request = str(tmp_path / "reorder.db"), "owner", "parent", "turn"
    with closing(SQLiteSessionStore(path)) as store:
        user = replace(Message.user("Start"), request_id=request)
        store.save(sid, [user], owner_account_id=owner)
        await _record_indexed_answer(store, owner, sid, request, "one", "First")
        await _record_indexed_answer(store, owner, sid, request, "two", "Second")
        first, second = store.accepted_followup_answers(sid, request, owner_account_id=owner)
        store.save(sid, [user, Message.assistant("Before"), second, first], owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as cold:
        answers, cursor = cold.accepted_followup_snapshot(sid, request, owner_account_id=owner)
        assert answers == [second, first]
        assert cursor == cold._read_event_cursor(owner, sid)


async def test_receipt_index_excludes_inactive_tail_and_reappears_on_return(tmp_path):
    path, owner, sid = str(tmp_path / "rewind.db"), "owner", "parent"
    with closing(SQLiteSessionStore(path)) as store:
        user = Message.user("Start")
        store.save(sid, [user], owner_account_id=owner)
        await _record_indexed_answer(store, owner, sid, "r", "q", "Choice")
        answers, original_cursor = store.accepted_followup_snapshot(sid, "r", owner_account_id=owner)
        assert len(answers) == 1
        fork = store.fork(sid, original_cursor[0], owner_account_id=owner)
        await _record_indexed_answer(store, owner, fork, "r", "q", "Fork choice")
        assert [m.content for m in store.accepted_followup_answers(fork, "r", owner_account_id=owner)] == ["Fork choice"]
        store.rewind(fork, 1, owner_account_id=owner)
        assert store.load(fork, owner)[-1] == answers[0]
        assert store.accepted_followup_answers(fork, "r", owner_account_id=owner) == []
        store.rewind(sid, 1, owner_account_id=owner)
        store.save(sid, [user, Message.assistant("Alternative")], owner_account_id=owner)
        hidden, active_cursor = store.accepted_followup_snapshot(sid, "r", owner_account_id=owner)
        assert active_cursor[0] > original_cursor[0]
        assert hidden == []
        store.rewind(sid, original_cursor[0], owner_account_id=owner)
        assert store.accepted_followup_answers(sid, "r", owner_account_id=owner) == answers


@pytest.mark.parametrize("hint", ["original", "absent", "other_request", "other_role"])
async def test_receipt_index_scopes_duplicate_ids_and_uncertain_commit(tmp_path, hint):
    with closing(SQLiteSessionStore(str(tmp_path / "ids.db"))) as store:
        owner, sid, request = "A", "parent", "r"
        for other_owner, other_sid, other_request in [(owner, sid, request), ("B", sid, request), (owner, "child", request)]:
            store.save(other_sid, [Message.user("Start")], owner_account_id=other_owner)
            journal, key = await _record_indexed_answer(store, other_owner, other_sid, other_request, "q", other_owner + other_sid)
            await journal.abandon(key)
        stored = store.load(sid, owner)
        answer = stored[-1]
        duplicate = replace(answer, content="Later copy")
        wrong_request = replace(answer, content="Another turn", request_id="other")
        wrong_role = replace(answer, role="assistant", content="Assistant event")
        store.save(sid, stored + [duplicate, wrong_request, wrong_role], owner_account_id=owner)
        if hint != "original":
            seq = {"absent": None, "other_request": 4, "other_role": 5}[hint]
            store.transaction(lambda conn: conn.execute(
                "UPDATE followup_receipts SET answer_seq=? WHERE owner_account_id=? AND session_id=? AND request_id=?",
                (seq, owner, sid, request),
            ))
        assert store.accepted_followup_answers(sid, request, owner_account_id=owner) == [answer if hint == "original" else duplicate]
        assert store.accepted_followup_answers(sid, "other", owner_account_id=owner) == []
        assert [m.content for m in store.accepted_followup_answers(sid, request, owner_account_id="B")] == ["Bparent"]
        assert [m.content for m in store.accepted_followup_answers("child", request, owner_account_id=owner)] == ["Achild"]


async def test_receipt_index_queries_only_the_pending_request_tail(tmp_path, monkeypatch):
    with closing(SQLiteSessionStore(str(tmp_path / "long.db"))) as store:
        sid, owner = "parent", "owner"
        history = [Message.user(str(i)) for i in range(1000)]
        store.save(sid, history, owner_account_id=owner)
        await _record_indexed_answer(store, owner, sid, "r", "q", "Choice")
        store.save(sid, store.load(sid, owner) + [Message.assistant("Continued")], owner_account_id=owner)
        sizes = []
        read = store._fetch_rows_desc

        def count(*args, **kwargs):
            rows = read(*args, **kwargs)
            sizes.append(len(rows))
            return rows

        monkeypatch.setattr(store, "_fetch_rows_desc", count)
        assert store.accepted_followup_answers(sid, "other", owner_account_id=owner) == []
        assert sizes == []
        assert [m.content for m in store.accepted_followup_answers(sid, "r", owner_account_id=owner)] == ["Choice"]
        assert sum(sizes) <= 2
        store.transaction(lambda conn: conn.execute(
            "UPDATE followup_receipts SET answer_history_id='' WHERE owner_account_id=? AND session_id=?",
            (owner, sid),
        ))
        sizes.clear()
        assert store.accepted_followup_answers(sid, "r", owner_account_id=owner) == []
        store.ensure_session("empty", owner_account_id=owner)
        assert store.accepted_followup_snapshot("empty", "r", owner_account_id=owner) == ([], (0, 0))
        assert sizes == []


async def test_receipt_snapshot_observes_one_cut_during_a_separate_connection_rewind(tmp_path, monkeypatch):
    path, owner, sid = str(tmp_path / "snapshot.db"), "owner", "parent"
    with closing(SQLiteSessionStore(path)) as creator:
        creator.save(sid, [Message.user("Start")], owner_account_id=owner)
        await _record_indexed_answer(creator, owner, sid, "r", "q", "Choice")
    with closing(SQLiteSessionStore(path)) as reader, closing(SQLiteSessionStore(path)) as writer:
        read_cursor = reader._read_event_cursor
        changed = False

        def rewind_after_cursor(*args, **kwargs):
            nonlocal changed
            cut = read_cursor(*args, **kwargs)
            if not changed:
                changed = True
                writer.rewind(sid, 1, owner_account_id=owner)
            return cut

        monkeypatch.setattr(reader, "_read_event_cursor", rewind_after_cursor)
        old_answers, old_cut = reader.accepted_followup_snapshot(sid, "r", owner_account_id=owner)
        assert [m.content for m in old_answers] == ["Choice"] and old_cut[0] == 2
        current_answers, current_cut = reader.accepted_followup_snapshot(sid, "r", owner_account_id=owner)
        assert current_answers == [] and current_cut[0] == 1
