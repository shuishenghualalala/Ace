"""Active Owner 排他租约的持久化与并发契约。"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from crew.app import build_app
from crew.state.active_owner import ActiveOwnerConflict, ActiveOwnerLeaseStore
from crew.state.config import Config


def test_only_one_owner_can_claim_across_independent_connections(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    second = ActiveOwnerLeaseStore(db_path)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def claim(store: ActiveOwnerLeaseStore, owner: str) -> None:
        barrier.wait()
        try:
            lease = store.claim(owner)
        except ActiveOwnerConflict:
            outcomes.append((owner, "conflict"))
        else:
            outcomes.append((lease.owner_account_id, "claimed"))

    threads = [
        threading.Thread(target=claim, args=(first, "A:uid-a")),
        threading.Thread(target=claim, args=(second, "B:uid-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(status for _, status in outcomes) == ["claimed", "conflict"]
    winner = next(owner for owner, status in outcomes if status == "claimed")
    assert first.current().owner_account_id == winner
    assert second.current().owner_account_id == winner

    first.close()
    second.close()


def test_same_owner_reuses_lease_and_only_owner_can_release(tmp_path):
    db_path = tmp_path / "crew.db"
    store = ActiveOwnerLeaseStore(db_path)

    initial = store.claim("A:uid-a", verified_at=10.0)
    refreshed = store.claim("A:uid-a", verified_at=20.0)

    assert refreshed.owner_account_id == initial.owner_account_id
    assert refreshed.claimed_at == initial.claimed_at
    assert refreshed.verified_at == 20.0
    assert store.release("B:uid-b") is False
    assert store.current() is not None
    assert store.release("A:uid-a") is True
    assert store.current() is None

    store.close()


def test_same_owner_claim_coalesces_verified_at_writes_within_refresh_interval(tmp_path):
    store = ActiveOwnerLeaseStore(
        tmp_path / "crew.db",
        refresh_interval_seconds=1.0,
    )
    store.claim("A:uid-a", verified_at=10.0)
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)

    coalesced = store.claim("A:uid-a", verified_at=10.5)
    refreshed = store.claim("A:uid-a", verified_at=11.5)

    updates = [sql for sql in statements if "UPDATE active_owner_lease" in sql]
    assert coalesced.verified_at == 10.0
    assert refreshed.verified_at == 11.5
    assert len(updates) == 1
    store.close()


def test_coalesced_claim_cannot_return_across_an_atomic_owner_handoff(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path, refresh_interval_seconds=1.0)
    second = ActiveOwnerLeaseStore(db_path, refresh_interval_seconds=1.0)
    first.claim("A:uid-a", verified_at=10.0)
    entered = threading.Event()
    resume = threading.Event()
    handoff_done = threading.Event()
    original = first._lease_from_row

    def pause_after_select(row):
        lease = original(row)
        entered.set()
        assert resume.wait(timeout=1)
        return lease

    first._lease_from_row = pause_after_select
    refreshed: list[str] = []

    refresh_thread = threading.Thread(
        target=lambda: refreshed.append(first.claim("A:uid-a", verified_at=10.5).owner_account_id)
    )

    def handoff() -> None:
        assert second.release("A:uid-a") is True
        second.claim("B:uid-b", verified_at=11.0)
        handoff_done.set()

    refresh_thread.start()
    assert entered.wait(timeout=1)
    handoff_thread = threading.Thread(target=handoff)
    handoff_thread.start()
    time.sleep(0.03)

    assert handoff_done.is_set() is False
    resume.set()
    refresh_thread.join(timeout=1)
    handoff_thread.join(timeout=1)
    assert refreshed == ["A:uid-a"]
    assert second.current().owner_account_id == "B:uid-b"
    first.close()
    second.close()


def test_active_owner_lease_survives_store_restart(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    first.claim("A:uid-a", verified_at=10.0)
    first.close()

    reopened = ActiveOwnerLeaseStore(db_path)

    assert reopened.current().owner_account_id == "A:uid-a"
    assert reopened.current().verified_at == 10.0
    reopened.close()


def test_restart_logout_intent_keeps_lease_until_next_process_completes_it(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    first.claim("A:uid-a", verified_at=10.0)

    assert first.prepare_restart_logout("A:uid-a") is True
    assert first.current().owner_account_id == "A:uid-a"
    first.close()

    reopened = ActiveOwnerLeaseStore(db_path)
    assert reopened.pending_restart_logout() == "A:uid-a"
    assert reopened.complete_restart_logout() == "A:uid-a"
    assert reopened.current() is None
    assert reopened.pending_restart_logout() is None
    reopened.close()


def test_other_owner_cannot_prepare_restart_logout(tmp_path):
    store = ActiveOwnerLeaseStore(tmp_path / "crew.db")
    store.claim("A:uid-a")

    assert store.prepare_restart_logout("B:uid-b") is False
    assert store.pending_restart_logout() is None
    assert store.current().owner_account_id == "A:uid-a"
    store.close()


@pytest.mark.asyncio
async def test_build_app_owns_and_closes_active_owner_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        Config(db_path=str(tmp_path / "crew.db"), plugins_enabled=[]),
        enable_team=False,
    )

    lease = crew.active_owner.claim("A:uid-a", verified_at=10.0)

    assert lease.owner_account_id == "A:uid-a"
    await crew.shutdown()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        crew.active_owner.current()
