import tempfile
from pathlib import Path

import pytest

from crew.wiki.manager import WikiSessionManager


@pytest.fixture
def manager(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr("crew.wiki.manager.get_crew_home", lambda: Path(tmp))
        yield WikiSessionManager()


def test_pending_cards(manager: WikiSessionManager):
    manager.add_pending_cards("s1", [{"id": "p1"}])
    manager.add_pending_cards("s1", [{"id": "p2"}])
    cards = manager.take_pending_cards("s1")
    assert len(cards) == 2
    assert manager.take_pending_cards("s1") == []


def test_kb_id_default(manager: WikiSessionManager):
    assert manager.get_kb_id("s1") == "default"


def test_kb_id_set_and_get(manager: WikiSessionManager):
    manager.set_kb_id("s1", "kb_test")
    assert manager.get_kb_id("s1") == "kb_test"
    manager.set_kb_id("s1", "")
    assert manager.get_kb_id("s1") == "default"


def test_kb_id_persistence(manager: WikiSessionManager):
    manager.set_kb_id("s1", "kb_test", owner_account_id="owner")
    other = WikiSessionManager()
    assert other.get_kb_id("s1", owner_account_id="owner") == "kb_test"


def test_confirmation_is_scoped_and_consumed_once(manager: WikiSessionManager):
    issued = manager.issue_confirmation(
        "s1",
        action="delete_pages",
        kb_id="kb1",
        payload={"page_ids": ["p1"]},
        summary="删除页面",
        impact={"count": 1},
        owner_account_id="owner1",
    )
    cid = issued["confirmation_id"]
    assert manager.consume_confirmation(
        "s1", cid, action="delete_pages", kb_id="kb1", owner_account_id="owner2"
    ) is None
    assert manager.consume_confirmation(
        "other", cid, action="delete_pages", kb_id="kb1", owner_account_id="owner1"
    ) is None
    assert manager.consume_confirmation(
        "s1", cid, action="archive_page", kb_id="kb1", owner_account_id="owner1"
    ) is None
    assert manager.consume_confirmation(
        "s1", cid, action="delete_pages", kb_id="other", owner_account_id="owner1"
    ) is None
    assert manager.consume_confirmation(
        "s1", cid, action="delete_pages", kb_id="kb1", owner_account_id="owner1"
    ) == {"page_ids": ["p1"]}
    assert manager.consume_confirmation(
        "s1", cid, action="delete_pages", kb_id="kb1", owner_account_id="owner1"
    ) is None


def test_confirmation_expiry_and_cancel(manager: WikiSessionManager, monkeypatch):
    now = 1000.0
    monkeypatch.setattr("crew.wiki.manager.time.time", lambda: now)
    expired = manager.issue_confirmation(
        "s1", action="delete_source", kb_id="kb1", payload={}, summary="x", impact={}, ttl_seconds=60
    )
    now += 61
    assert manager.consume_confirmation(
        "s1", expired["confirmation_id"], action="delete_source", kb_id="kb1"
    ) is None

    active = manager.issue_confirmation(
        "s1", action="delete_source", kb_id="kb1", payload={}, summary="x", impact={}
    )
    assert manager.cancel_confirmation("s1", active["confirmation_id"])
    assert not manager.cancel_confirmation("s1", active["confirmation_id"])
