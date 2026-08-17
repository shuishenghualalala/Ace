"""Owner 会话租约的持久化、迁移与并发契约。"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from crew.app import build_app
from crew.state.active_owner import ActiveOwnerLeaseStore
from crew.state.config import Config


def test_multiple_owners_can_claim_across_independent_connections(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    second = ActiveOwnerLeaseStore(db_path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def claim(store: ActiveOwnerLeaseStore, owner: str) -> None:
        barrier.wait()
        outcomes.append(store.claim(owner).owner_account_id)

    threads = [
        threading.Thread(target=claim, args=(first, "A:uid-a")),
        threading.Thread(target=claim, args=(second, "B:uid-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(outcomes) == ["A:uid-a", "B:uid-b"]
    assert first.get("A:uid-a") is not None
    assert second.get("B:uid-b") is not None
    assert {lease.owner_account_id for lease in first.list()} == {"A:uid-a", "B:uid-b"}
    assert first.current() is None

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
    assert store.get("A:uid-a") is not None
    assert store.release("A:uid-a") is True
    assert store.get("A:uid-a") is None

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

    updates = [sql for sql in statements if "UPDATE owner_session_lease" in sql]
    assert coalesced.verified_at == 10.0
    assert refreshed.verified_at == 11.5
    assert len(updates) == 1
    store.close()


def test_owner_claims_do_not_require_an_atomic_global_handoff(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path, refresh_interval_seconds=1.0)
    second = ActiveOwnerLeaseStore(db_path, refresh_interval_seconds=1.0)
    first.claim("A:uid-a", verified_at=10.0)
    refreshed: list[str] = []
    claimed: list[str] = []
    refresh_thread = threading.Thread(
        target=lambda: refreshed.append(first.claim("A:uid-a", verified_at=10.5).owner_account_id)
    )
    claim_thread = threading.Thread(
        target=lambda: claimed.append(second.claim("B:uid-b", verified_at=11.0).owner_account_id)
    )
    refresh_thread.start()
    claim_thread.start()
    refresh_thread.join(timeout=1)
    claim_thread.join(timeout=1)
    assert refreshed == ["A:uid-a"]
    assert claimed == ["B:uid-b"]
    assert {lease.owner_account_id for lease in second.list()} == {"A:uid-a", "B:uid-b"}
    first.close()
    second.close()


def test_active_owner_lease_survives_store_restart(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    first.claim("A:uid-a", verified_at=10.0)
    first.close()

    reopened = ActiveOwnerLeaseStore(db_path)

    assert reopened.get("A:uid-a").owner_account_id == "A:uid-a"
    assert reopened.get("A:uid-a").verified_at == 10.0
    reopened.close()


def test_restart_logout_intent_keeps_lease_until_next_process_completes_it(tmp_path):
    db_path = tmp_path / "crew.db"
    first = ActiveOwnerLeaseStore(db_path)
    first.claim("A:uid-a", verified_at=10.0)

    assert first.prepare_restart_logout("A:uid-a") is True
    assert first.get("A:uid-a").owner_account_id == "A:uid-a"
    first.close()

    reopened = ActiveOwnerLeaseStore(db_path)
    assert reopened.pending_restart_logout() == "A:uid-a"
    assert reopened.complete_restart_logout() == ["A:uid-a"]
    assert reopened.get("A:uid-a") is None
    assert reopened.pending_restart_logout() is None
    reopened.close()


def test_other_owner_cannot_prepare_restart_logout(tmp_path):
    store = ActiveOwnerLeaseStore(tmp_path / "crew.db")
    store.claim("A:uid-a")

    assert store.prepare_restart_logout("B:uid-b") is False
    assert store.pending_restart_logout() is None
    assert store.get("A:uid-a").owner_account_id == "A:uid-a"
    store.close()


def test_legacy_singleton_rows_migrate_without_blocking_new_owner(tmp_path):
    db_path = tmp_path / "crew.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE active_owner_lease (
            lease_id INTEGER PRIMARY KEY,
            owner_account_id TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            verified_at REAL NOT NULL
        );
        CREATE TABLE active_owner_logout_intent (
            lease_id INTEGER PRIMARY KEY,
            owner_account_id TEXT NOT NULL,
            requested_at REAL NOT NULL
        );
        INSERT INTO active_owner_lease VALUES (1, 'email:old@example.com', 10, 11);
        INSERT INTO active_owner_logout_intent VALUES (1, 'email:old@example.com', 12);
        """
    )
    conn.commit()
    conn.close()

    store = ActiveOwnerLeaseStore(db_path)

    assert store.get("email:old@example.com").verified_at == 11
    assert store.pending_restart_logouts() == ["email:old@example.com"]
    assert store.claim("email:new@example.com").owner_account_id == "email:new@example.com"
    assert {lease.owner_account_id for lease in store.list()} == {
        "email:old@example.com",
        "email:new@example.com",
    }
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
