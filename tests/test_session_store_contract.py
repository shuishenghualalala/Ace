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
