from __future__ import annotations

from contextlib import closing

from crew.core.types import Message
from crew.state.session_store import SQLiteSessionStore


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
