import sqlite3

from crew.core.types import Message
from crew.cron import CronJobStore
from crew.state._migration import claim_legacy_owner_database, inspect_and_backfill_legacy_owners
from crew.state.session_store import SQLiteSessionStore
from crew.state.workspace_store import SQLiteWorkspaceStore
from crew.tasks import TaskRuntime


def test_claim_legacy_owner_database_claims_empty_owner_rows(tmp_path):
    db = tmp_path / "crew.db"
    sessions = SQLiteSessionStore(str(db))
    workspaces = SQLiteWorkspaceStore(str(db))
    cron = CronJobStore(str(db))
    tasks = TaskRuntime(str(db))
    try:
        sessions.save("legacy-session", [Message.user("hi")])
        sessions.set_agent_config("legacy-session", {"executor": "builtin"})
        workspaces.create("legacy workspace")
        cron.create(name="legacy cron", schedule="every 1m", query="hi", session_id="legacy-session")
        tasks.create_runtime(kind="team", session_id="legacy-session", title="legacy task")

        changed, remaining = claim_legacy_owner_database(str(db), "A:uid-a")
    finally:
        tasks.close()

    assert changed["sessions"] == 1
    assert changed["session_agent_config"] == 1
    assert changed["workspaces"] == 1
    assert changed["cron_jobs"] == 1
    assert changed["runtime_tasks"] == 1
    assert all(count == 0 for count in remaining.values())

    with sqlite3.connect(db) as conn:
        for table in ("sessions", "session_agent_config", "workspaces", "cron_jobs", "runtime_tasks"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_account_id = ''").fetchone()[0] == 0


def test_startup_migration_backfills_only_unambiguous_cron_owner(tmp_path):
    db = tmp_path / "crew.db"
    sessions = SQLiteSessionStore(str(db))
    cron = CronJobStore(str(db))

    sessions.save("owned", [Message.user("owned")], owner_account_id="A:uid-a")
    sessions.save("shared", [Message.user("a")], owner_account_id="A:uid-a")
    sessions.save("shared", [Message.user("b")], owner_account_id="B:uid-b")
    owned_job = cron.create(name="owned", schedule="every 1m", query="hi", session_id="owned")
    shared_job = cron.create(name="shared", schedule="every 1m", query="hi", session_id="shared")

    counts, backfilled = inspect_and_backfill_legacy_owners(str(db))

    assert backfilled == 1
    assert counts["cron_jobs"] == 1
    assert cron.get(owned_job["id"], owner_account_id="A:uid-a")["owner_account_id"] == "A:uid-a"
    assert cron.get(shared_job["id"], _all_owners=True)["owner_account_id"] == ""
