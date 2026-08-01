from __future__ import annotations

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
