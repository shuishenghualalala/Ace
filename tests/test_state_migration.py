import sqlite3

from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.core.types import Message
from crew.cron import CronJobStore
from crew.state._migration import (
    CORE_MAIN_DB_TABLES,
    claim_legacy_owner_databases,
    inspect_and_backfill_legacy_owners,
    legacy_owner_scan_targets,
    orphan_main_db_tables,
)
from crew.state.session_store import SQLiteSessionStore
from crew.state.workspace_store import SQLiteWorkspaceStore
from crew.tasks import TaskRuntime


def test_claim_legacy_owner_databases_claims_empty_owner_rows(tmp_path):
    db = tmp_path / "crew.db"
    sessions = SQLiteSessionStore(str(db))
    workspaces = SQLiteWorkspaceStore(str(db))
    cron = CronJobStore(str(db))
    tasks = TaskRuntime(str(db))
    try:
        sessions.save("legacy-session", [Message.user("hi")], owner_account_id="")
        sessions.set_agent_config("legacy-session", {"executor": "builtin"}, owner_account_id="")
        workspaces.create("legacy workspace", owner_account_id="")

        cron.create(name="legacy cron", schedule="every 1m", query="hi", session_id="legacy-session", owner_account_id="")
        tasks.create_runtime(kind="team", session_id="legacy-session", title="legacy task", owner_account_id="")

        # 单库形态（拆库前/回退配置）：cron 路径与主库同文件，映射自动合并
        changed, remaining = claim_legacy_owner_databases(
            legacy_owner_scan_targets(db, db), "A:uid-a"
        )
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
    owned_job = cron.create(name="owned", schedule="every 1m", query="hi", session_id="owned", owner_account_id="")
    shared_job = cron.create(name="shared", schedule="every 1m", query="hi", session_id="shared", owner_account_id="")

    counts, backfilled = inspect_and_backfill_legacy_owners(legacy_owner_scan_targets(db, db))

    assert backfilled == 1
    # 智能回填后不再残留无主行：歧义任务归本机 local 兜底（owner 统一后无"无主"数据）。
    assert counts["cron_jobs"] == 0
    assert cron.get(owned_job["id"], owner_account_id="A:uid-a")["owner_account_id"] == "A:uid-a"
    assert cron.get(shared_job["id"], _all_owners=True, owner_account_id="")["owner_account_id"] == LOCAL_OWNER_ACCOUNT_ID


def _create_table(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f"CREATE TABLE {name} (id TEXT)")


def test_orphan_main_db_tables_reports_only_unlisted_tables(tmp_path):
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    try:
        _create_table(conn, "sessions")  # OWNER_TABLE_LABELS
        _create_table(conn, "work_items")  # WORK_DB_TABLES（拆库备份沿用原名）
        _create_table(conn, "active_owner_lease")  # CORE_MAIN_DB_TABLES
        _create_table(conn, "work_schema_version")  # *_schema_version 版本表
        _create_table(conn, "companion_profile")  # 人工未知表
        # sqlite 内部表（sqlite_stat1）由 ANALYZE 生成，检测器应忽略。
        conn.execute("CREATE INDEX idx_sessions_id ON sessions(id)")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    assert orphan_main_db_tables(db) == ["companion_profile"]


def test_orphan_main_db_tables_empty_when_all_known(tmp_path):
    db = tmp_path / "crew.db"
    conn = sqlite3.connect(db)
    try:
        for table in ("sessions", "work_items", *CORE_MAIN_DB_TABLES, "work_schema_version"):
            _create_table(conn, table)
        conn.commit()
    finally:
        conn.close()

    assert orphan_main_db_tables(db) == []
