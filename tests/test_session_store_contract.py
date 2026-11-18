from __future__ import annotations

from contextlib import closing
from functools import partial
from inspect import isawaitable

import pytest

from crew.core.types import Message
from crew.state.session_store import SQLiteSessionStore, SessionEventType, SessionIncarnationChanged


def test_load_is_scoped_by_owner(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    store.save("same", [Message.user("legacy")], owner_account_id="")
    store.save("same", [Message.user("a")], owner_account_id="A:uid-a")

    assert store.load("same", owner_account_id="")[0].content == "legacy"
    assert store.load("same", owner_account_id="A:uid-a")[0].content == "a"
    assert store.load("same", owner_account_id="B:uid-b") == []


def test_ensure_session_allows_same_session_id_across_owners(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    store.ensure_session("sid", owner_account_id="A:uid-a")
    store.ensure_session("sid", owner_account_id="B:uid-b")

    assert [row["session_id"] for row in store.list_sessions(owner_account_id="A:uid-a")] == ["sid"]
    assert [row["session_id"] for row in store.list_sessions(owner_account_id="B:uid-b")] == ["sid"]


def test_session_owner_lookup_is_not_part_of_contract(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))

    assert not hasattr(store, "get_owner_account_id")


async def test_save_captures_each_owners_session_incarnation(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner, other = "same", "first-owner", "second-owner"
        assert store.get_history_incarnation(sid, owner) is None
        first = await store.save_with_incarnation_async(sid, [Message.user("first")], owner_account_id=owner)
        second = await store.save_with_incarnation_async(sid, [Message.user("second")], owner_account_id=other)
        assert first and second and first != second
        assert store.get_history_incarnation(sid, owner) == first
        assert store.get_history_incarnation(sid, other) == second
        assert await store.save_async(sid, [Message.user("updated")], owner_account_id=owner) is None
        assert store.get_history_incarnation(sid, owner) == first

        store.clear(sid, owner)
        assert store.get_history_incarnation(sid, owner) is None
        fresh = await store.save_with_incarnation_async(sid, [Message.user("fresh")], owner_account_id=owner)
        assert fresh and fresh not in {first, second}
        assert store.get_history_incarnation(sid, owner) == fresh
        assert store.get_history_incarnation(sid, other) == second
        assert [m.content for m in store.load(sid, owner)] == ["fresh"]
        assert [m.content for m in store.load(sid, other)] == ["second"]


async def test_save_returns_the_incarnation_committed_before_writer_ack(tmp_path, monkeypatch):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner = "reopened", "owner"
        queue = store._get_write_queue((owner, sid))
        enqueue = queue.enqueue
        original = None

        async def recreate_after_commit(write):
            nonlocal original
            saved = await enqueue(write)
            original = store.get_history_incarnation(sid, owner)
            store.clear(sid, owner)
            store.ensure_session(sid, owner_account_id=owner)
            return saved

        monkeypatch.setattr(queue, "enqueue", recreate_after_commit)
        captured = await store.save_with_incarnation_async(sid, [Message.user("old")], owner_account_id=owner)
        assert original and captured == original
        assert store.get_history_incarnation(sid, owner) not in {None, original}
        assert store.load(sid, owner) == []


def test_convergence_rejects_recreated_session_with_same_empty_cursor(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner = "draft", "owner"
        original = store.ensure_session_incarnation(sid, owner_account_id=owner)
        _, cursor = store.accepted_followup_snapshot(sid, "old-turn", owner_account_id=owner)
        store.clear(sid, owner)
        messages = [Message.user("Earlier turn")]
        with pytest.raises(SessionIncarnationChanged):
            store.save(sid, messages, owner_account_id=owner, expected_incarnation=original)
        assert store.get_history_incarnation(sid, owner) is None

        current = store.ensure_session_incarnation(sid, owner_account_id=owner)
        _, empty_cursor = store.accepted_followup_snapshot(sid, "new-turn", owner_account_id=owner)
        assert current != original and empty_cursor == cursor
        with pytest.raises(SessionIncarnationChanged):
            store.save(sid, messages, owner_account_id=owner, expected_cursor=cursor, expected_incarnation=original)
        assert store.load(sid, owner) == []
        store.save(sid, [Message.user("Fresh turn")], owner_account_id=owner,
                   expected_cursor=cursor, expected_incarnation=current)
        assert [m.content for m in store.load(sid, owner)] == ["Fresh turn"]
        assert store.ensure_session_incarnation(sid, owner_account_id=owner) == current


@pytest.mark.parametrize("capture", [False, True])
async def test_async_history_save_checks_the_bound_session_instance(tmp_path, capture, monkeypatch):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner = "draft", "owner"
        original = store.ensure_session_incarnation(sid, owner_account_id=owner)
        store.clear(sid, owner)
        current = store.ensure_session_incarnation(sid, owner_account_id=owner)
        save = store.save_with_incarnation_async if capture else store.save_async
        attempts = []
        execute = store._writer.execute_async

        async def observed_write(operation):
            attempts.append(operation)
            return await execute(operation)

        monkeypatch.setattr(store._writer, "execute_async", observed_write)
        with pytest.raises(SessionIncarnationChanged):
            await save(sid, [Message.user("Earlier turn")], owner_account_id=owner, expected_incarnation=original)
        assert len(attempts) == 1
        assert store.load(sid, owner) == []
        result = await save(sid, [Message.user("Fresh turn")], owner_account_id=owner,
                            expected_incarnation=current)
        assert result == (current if capture else None)
        assert [m.content for m in store.load(sid, owner)] == ["Fresh turn"]


async def test_recreated_session_rejects_late_turn_metadata_and_cleanup(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner = "draft", "owner"
        old = store.ensure_session_incarnation(sid, owner_account_id=owner)
        store.clear(sid, owner, expected_incarnation=old)
        fresh = store.ensure_session_incarnation(sid, title="Fresh title", owner_account_id=owner)
        settled = []
        stale_operations = [
            partial(store.accepted_followup_snapshot, sid, "late-turn"),
            partial(store.set_title, sid, "Late title"),
            partial(store.set_status, sid, "running"),
            partial(store.set_status_async, sid, "failed"),
            partial(store.record_turn_event, sid, kind=SessionEventType.TURN_START),
            partial(store.record_durable_event_async, sid, kind=SessionEventType.TOOL_EXECUTION_INTENT),
            partial(store.record_meter_checkpoint, sid, prompt_tokens=123),
            partial(store.clear_prompt_usage, sid),
            partial(store.close_open_turn, sid),
            partial(store.reconcile_open_tool_events, sid),
            partial(store.record_turn_outcome, sid, "late-turn", {"status": "failed"}, last_status="failed",
                    settle_receipt=lambda conn: settled.append(True)),
            partial(store.clear, sid),
        ]
        for operation in stale_operations:
            with pytest.raises(SessionIncarnationChanged):
                result = operation(owner_account_id=owner, expected_incarnation=old)
                if isawaitable(result):
                    await result
        assert settled == []
        assert store.get_history_incarnation(sid, owner) == fresh
        assert store.get_status_details(sid, owner) == ("", "", None)
        assert store.list_sessions(owner_account_id=owner)[0]["title"] == "Fresh title"
        assert store.accepted_followup_snapshot(sid, "fresh-turn", owner_account_id=owner,
                                                expected_incarnation=fresh) == ([], (0, 0))
        assert store.load_meter_checkpoint(sid, owner) is None


def test_compaction_checkpoint_uses_only_its_own_turn_instance(tmp_path):
    from crew.core.runctx import current_turn_session_incarnation

    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        sid, owner = "draft", "owner"
        old = store.ensure_session_incarnation(sid, owner_account_id=owner)
        store.clear(sid, owner)
        store.ensure_session_incarnation(sid, owner_account_id=owner)
        store.ensure_session_incarnation(sid, owner_account_id="other")
        store.ensure_session_incarnation("separate", owner_account_id=owner)
        write = partial(store.record_compaction_checkpoint, summary="saved", covered_count=1, view_estimate=1)
        token = current_turn_session_incarnation.set((owner, sid, old))
        try:
            with pytest.raises(SessionIncarnationChanged):
                write(sid, owner_account_id=owner)
            write(sid, owner_account_id="other")
            write("separate", owner_account_id=owner)
        finally:
            current_turn_session_incarnation.reset(token)
        assert store.load_compaction_checkpoint(sid, owner) is None
        assert store.load_compaction_checkpoint(sid, "other")["summary"] == "saved"
        assert store.load_compaction_checkpoint("separate", owner)["summary"] == "saved"
        write(sid, owner_account_id=owner)
        assert store.load_compaction_checkpoint(sid, owner)["summary"] == "saved"


@pytest.mark.parametrize("capture", [False, True])
async def test_late_ack_preserves_recreated_projection_with_matching_cursor(tmp_path, monkeypatch, capture):
    sid, owner = "same", "owner"
    path = str(tmp_path / "crew.db")
    with closing(SQLiteSessionStore(path)) as store:
        queue = store._get_write_queue((owner, sid))
        enqueue = queue.enqueue

        async def recreate_after_commit(operation):
            saved = await enqueue(operation)
            store.clear(sid, owner)
            store.save(sid, [Message.user("New history")], owner_account_id=owner)
            return saved

        monkeypatch.setattr(queue, "enqueue", recreate_after_commit)
        save = store.save_with_incarnation_async if capture else store.save_async
        token = await save(sid, [Message.user("Old history")], owner_account_id=owner)
        if capture:
            assert token and token != store.get_history_incarnation(sid, owner)
        assert [m.content for m in store.load(sid, owner)] == ["New history"]
    with closing(SQLiteSessionStore(path)) as reopened:
        assert [m.content for m in reopened.load(sid, owner)] == ["New history"]


def test_other_connection_recreated_projection_with_same_event_cursor(tmp_path):
    path, sid, owner = str(tmp_path / "crew.db"), "session", "owner"
    with closing(SQLiteSessionStore(path)) as first:
        first.save(sid, [Message.user("Previous")], owner_account_id=owner)
        assert first.load(sid, owner)[0].content == "Previous"
        with closing(SQLiteSessionStore(path)) as second:
            second.clear(sid, owner)
            second.save(sid, [Message.user("New")], owner_account_id=owner)
        assert first.load(sid, owner)[0].content == "New"


def test_unbound_save_preserves_complete_history_after_external_session_recreation(tmp_path):
    path, sid, owner = str(tmp_path / "crew.db"), "session", "owner"
    common = Message.user("Requested prefix")
    with closing(SQLiteSessionStore(path)) as first:
        first.save(sid, [common], owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as second:
            second.clear(sid, owner)
            second.save(sid, [Message.user("Different prefix")], owner_account_id=owner)
        first.save(sid, [common, Message.assistant("Tail")], owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as cold:
        assert [m.content for m in cold.load(sid, owner)] == ["Requested prefix", "Tail"]


async def test_late_ack_leaves_evicted_projection_out_of_the_lru(tmp_path, monkeypatch):
    owner = "owner"
    with closing(SQLiteSessionStore(str(tmp_path / "crew.db"))) as store:
        monkeypatch.setattr(store, "PROJECTION_CACHE_LIMIT", 1)
        queue = store._get_write_queue((owner, "first"))
        enqueue = queue.enqueue

        async def evict_after_commit(operation):
            saved = await enqueue(operation)
            store.save("active", [Message.user("Active")], owner_account_id=owner)
            return saved

        monkeypatch.setattr(queue, "enqueue", evict_after_commit)
        await store.save_async("first", [Message.user("First")], owner_account_id=owner)
        assert list(store._projections) == [(owner, "active")]
        assert [m.content for m in store.load("first", owner)] == ["First"]


def test_projection_rebuild_reads_cursor_and_messages_from_one_snapshot(tmp_path, monkeypatch):
    path, sid, owner = str(tmp_path / "crew.db"), "session", "owner"
    with closing(SQLiteSessionStore(path)) as writer, closing(SQLiteSessionStore(path)) as reader:
        writer.save(sid, [Message.user("Before")], owner_account_id=owner)
        fetch = reader._fetch_chain_rows
        replaced = False

        def replace_after_cursor(*args):
            nonlocal replaced
            if not replaced:
                replaced = True
                writer.clear(sid, owner)
                writer.save(sid, [Message.user("After"), Message.assistant("More")], owner_account_id=owner)
            return fetch(*args)

        monkeypatch.setattr(reader, "_fetch_chain_rows", replace_after_cursor)
        assert [m.content for m in reader.load(sid, owner)] == ["Before"]
        assert replaced
        assert [m.content for m in reader.load(sid, owner)] == ["After", "More"]


def test_external_message_append_reconciles_hot_and_cold_history(tmp_path):
    path, owner, sid = str(tmp_path / "merge.db"), "owner", "session"
    with closing(SQLiteSessionStore(path)) as initial:
        initial.save(sid, [Message.user("A")], owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as first:
        previous = first.load(sid, owner)
        with closing(SQLiteSessionStore(path)) as other:
            other.save(sid, previous + [Message.assistant("B")], owner_account_id=owner)
        requested = previous + [Message.assistant("C")]
        first.save(sid, requested, owner_account_id=owner)
        assert first.load(sid, owner) == requested
        with closing(SQLiteSessionStore(path)) as cold:
            assert cold.load(sid, owner) == requested


@pytest.mark.parametrize("with_later_message", [False, True])
async def test_append_preserves_persisted_followup_reference(tmp_path, with_later_message):
    from crew.gateway.followup_receipts import FollowupKey, FollowupReceipt, FollowupReceiptJournal
    from crew.core.followup import FollowupAnswerMessage

    path, owner, sid = str(tmp_path / "followup.db"), "owner", "session"
    with closing(SQLiteSessionStore(path)) as initial:
        initial.save(sid, [Message.user("A")], owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as first:
        previous = first.load(sid, owner)
        with closing(SQLiteSessionStore(path)) as other:
            journal = FollowupReceiptJournal(other.transaction, other.read_query, other.append_followup_answer_in_transaction)
            key = FollowupKey(owner, sid, "request", "question", "submit")
            answer = FollowupAnswerMessage("followup:question", "Selected")
            assert await journal.claim(key, "digest") is None
            await journal.finish(FollowupReceipt(key, "applied", answer_history_id=answer.history_id), answer=answer)
            accepted = other.accepted_followup_answers(sid, "request", owner_account_id=owner)
            assert len(accepted) == 1
            if with_later_message:
                other.save(sid, previous + accepted + [Message.assistant("Old tail")], owner_account_id=owner)
        requested = previous + accepted + [Message.assistant("New tail")]
        first.save(sid, requested, owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as cold:
            assert first.load(sid, owner) == cold.load(sid, owner) == requested
            assert cold.accepted_followup_answers(sid, "request", owner_account_id=owner) == accepted
            count = cold.read_query(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM session_events WHERE type='user_message' AND "
                "json_extract(payload,'$.followup_history_id')=?", (answer.history_id,),
            ).fetchone()[0])
            assert count == 1


def test_marker_only_append_keeps_persisted_outcome_in_the_hot_projection(tmp_path):
    path, owner, sid = str(tmp_path / "markers.db"), "owner", "session"
    user = Message.user("A")
    user.request_id = "r"
    with closing(SQLiteSessionStore(path)) as store:
        store.save(sid, [user], owner_account_id=owner)
        previous = store.load(sid, owner)
        store.record_turn_event(sid, owner_account_id=owner, kind=SessionEventType.TURN_START)
        outcome = {"request_id": "r", "status": "completed", "message": "Saved"}
        store.record_turn_outcome(sid, "r", outcome, owner_account_id=owner, last_status="idle")
        store.save(sid, previous + [Message.assistant("C")], owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as cold:
            assert store.load(sid, owner) == cold.load(sid, owner)
            saved_outcome = cold.load(sid, owner)[0].turn_outcome
            assert {key: saved_outcome[key] for key in outcome} == outcome
            assert cold.read_query(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM session_events WHERE type IN ('turn_start','turn_outcome')"
            ).fetchone()[0]) == 2


def test_no_op_save_after_rewind_preserves_the_active_leaf(tmp_path):
    path, owner, sid = str(tmp_path / "rewind.db"), "owner", "session"
    with closing(SQLiteSessionStore(path)) as store:
        store.save(sid, [Message.user("A"), Message.user("B")], owner_account_id=owner)
        store.rewind(sid, 1, owner_account_id=owner)
        store.save(sid, store.load(sid, owner), owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as cold:
            assert [m.content for m in cold.load(sid, owner)] == ["A"]
            assert [b["tip_seq"] for b in cold.list_branches(sid, owner_account_id=owner) if b["kind"] == "tail"] == [2]


def test_cached_legacy_blob_rebuilds_once_after_event_backfill(tmp_path):
    with closing(SQLiteSessionStore(str(tmp_path / "legacy.db"))) as store:
        owner, sid = "owner", "old"
        store.ensure_session(sid, owner_account_id=owner)
        messages = [Message.user("Legacy"), Message.assistant("Reply")]
        store.transaction(lambda conn: conn.execute(
            "UPDATE sessions SET messages=? WHERE session_id=? AND owner_account_id=?",
            (store._dump(messages), sid, owner),
        ))
        assert store.load(sid, owner) == messages
        store.transaction(store._backfill_legacy_blobs)
        assert store.load(sid, owner) == messages


@pytest.mark.parametrize("retain_inherited_prefix", [True, False])
def test_stale_fork_save_anchors_only_the_complete_inherited_prefix(tmp_path, retain_inherited_prefix):
    path, owner = str(tmp_path / "fork.db"), "owner"
    inherited = [Message.user("A"), Message.assistant("X")]
    with closing(SQLiteSessionStore(path)) as creator:
        creator.save("source", inherited, owner_account_id=owner)
        fork = creator.fork("source", 2, owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as first:
        original = first.load(fork, owner)
        with closing(SQLiteSessionStore(path)) as second:
            second.save(fork, original + [Message.assistant("Old")], owner_account_id=owner)
        requested = original[:2 if retain_inherited_prefix else 1] + [Message.assistant("New")]
        first.save(fork, requested, owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as cold:
            assert cold.load(fork, owner) == requested
            seed_count = cold.read_query(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM session_events WHERE session_id=? AND type='end_seed'", (fork,),
            ).fetchone()[0])
            assert seed_count == int(retain_inherited_prefix)
            if retain_inherited_prefix:
                assert [b["tip_seq"] for b in cold.list_branches(fork, owner_account_id=owner) if b["kind"] == "tail"] == [2]


def test_stale_branch_summary_retains_committed_prefix_outcome(tmp_path):
    path, owner, sid = str(tmp_path / "outcome.db"), "owner", "session"
    user = Message.user("A")
    user.request_id = "r"
    with closing(SQLiteSessionStore(path)) as creator:
        creator.save(sid, [user], owner_account_id=owner)
    with closing(SQLiteSessionStore(path)) as first:
        previous = first.load(sid, owner)
        with closing(SQLiteSessionStore(path)) as other:
            other.record_turn_outcome(sid, "r", {"status": "completed", "message": "Saved"},
                                      owner_account_id=owner, last_status="completed")
            other.save(sid, other.load(sid, owner) + [Message.assistant("Old")], owner_account_id=owner)
        first.save(sid, previous + [Message.assistant("New")], owner_account_id=owner)
        with closing(SQLiteSessionStore(path)) as cold:
            stored = cold.load(sid, owner)
            assert [m.content for m in stored] == ["A", "New"]
            assert stored[0].turn_outcome["status"] == "completed"
            assert cold.get_status_details(sid, owner)[2] == stored[0].turn_outcome
