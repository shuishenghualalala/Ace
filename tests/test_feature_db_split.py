"""ADR-0038 Feature 拆库契约测试（cron 试点 + work 第二批 + kanban 第三批
+ external/team 第四批 + sites/tasks/notifications 收尾批）。

覆盖拆库的三类契约：
1. 路径与装配：Config.<feature>_db_path 读取/归一；新装 Feature 表只出现在
   各自独立库；
2. copy-on-first-activate：存量复制、幂等零重复、目标非空/旧库缺表跳过、
   复制失败单事务整体回滚不半写；
3. 跨表工具适配：claim-legacy 与启动期 owner 巡检按库归属扫描；CLI migrate
   feature 的版本表 stamp 到各自独立库。kanban 域另锁定隔离语义：
   legacy_ambiguous 歧义行分库后仍保持 owner='' 隔离，通用回填不可改写。
   external/team 双库另锁定快照回填语义：成员快照回填（唯一跨命名空间读点）
   在分库复制前移到旧库执行，team 库内守卫跳过。sites 域两半（store/blueprint）
   同住 sites.db 各自复制；tasks/notifications 照 cron/work 语义接入通用巡检。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from crew.agent.external.catalog import CREW_BUILTIN_AGENT_ID
from crew.agent.external.store import ExternalAgentStore
from crew.app import build_app
from crew.core.interfaces import Notification
from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.core.types import Message
from crew.cron import CronJobStore
from crew.cron.jobs import CRON_SCHEMA_FEATURE, CRON_SCHEMA_VERSION
from crew.dynamickanban.runtime_models import RuntimeState
from crew.dynamickanban.store import (
    KANBAN_SCHEMA_FEATURE,
    KANBAN_SCHEMA_VERSION,
    SQLiteKanbanStore,
)
from crew.notifications.store import (
    NOTIFICATIONS_SCHEMA_FEATURE,
    NOTIFICATIONS_SCHEMA_VERSION,
    NotificationStore,
)
from crew.sites.blueprint import BlueprintStore
from crew.sites.store import (
    SITES_SCHEMA_FEATURE,
    SITES_SCHEMA_VERSION,
    SQLiteSiteStore,
)
from crew.state._migration import (
    CRON_DB_TABLES,
    EXTERNAL_DB_TABLES,
    KANBAN_DB_TABLES,
    NOTIFICATIONS_DB_TABLES,
    SITES_DB_TABLES,
    TEAM_DB_TABLES,
    TASKS_DB_TABLES,
    WORK_DB_TABLES,
    claim_legacy_owner_databases,
    inspect_and_backfill_legacy_owners,
    legacy_owner_scan_targets,
)
from crew.state.config import Config, load_config
from crew.state.schema_version import copy_legacy_feature_rows
from crew.state.session_store import SQLiteSessionStore
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.tasks import TaskRuntime
from crew.tasks.runtime import TASKS_SCHEMA_FEATURE, TASKS_SCHEMA_VERSION
from crew.team.external_store import (
    EXTERNAL_SCHEMA_FEATURE,
    EXTERNAL_SCHEMA_VERSION,
    TEAM_SCHEMA_FEATURE,
    TEAM_SCHEMA_VERSION,
    TeamExternalAgentStore,
)
from crew.work.briefs import WorkBriefStore
from crew.work.feature import WORK_SCHEMA_FEATURE, WORK_SCHEMA_VERSION, build_work_feature
from crew.work.items import WorkItemStore
from crew.work.knowledge import WorkKnowledgeStore
from crew.work.preferences import WorkPreferenceStore
from crew.work.references import WorkReferenceStore
from crew.work.settings import WorkSettingsStore
from crew.work.sources import SourceRecordInput, SourceSyncBatch, WorkSourceStore
from crew.work.templates import WorkTemplateStore


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _table_names(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _count(db_path, table: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_legacy_cron_jobs(legacy_db, *, with_run: bool = False) -> list[dict]:
    """在旧主库中预置带数据的 cron 两表（走真实 store 语义）。"""

    legacy = CronJobStore(str(legacy_db))
    try:
        job_a = legacy.create(
            name="every", schedule="every 1m", query="q", session_id="s1",
            owner_account_id="A:u1",
        )
        job_b = legacy.create(
            name="once", schedule="in 10m", query="q", session_id="s1",
            owner_account_id="A:u1",
        )
        if with_run:
            assert legacy.claim_manual_fire(job_a["id"], owner_account_id="A:u1") is not None
        return [job_a, job_b]
    finally:
        legacy.close()


# ---------------------------------------------------------------------------
# 路径与装配
# ---------------------------------------------------------------------------

def test_load_config_reads_and_normalizes_cron_db_path(tmp_path, monkeypatch):
    """runtime.cron_db_path 与 db_path 同法：可配置，相对路径归一到 crew_home。"""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "runtime:\n  cron_db_path: custom/cron.db\n", encoding="utf-8"
    )
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.cron_db_path == str(tmp_path / "home" / "custom" / "cron.db")


def test_load_config_cron_db_path_defaults_next_to_main_db(tmp_path, monkeypatch):
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.cron_db_path == str(tmp_path / "home" / "crew_data" / "cron.db")


def test_fresh_install_cron_tables_live_only_in_cron_db(tmp_path):
    """验收 1：新装环境 cron 表只出现在 cron.db，crew.db 无 cron 表。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    crew = build_app(
        config=Config(db_path=str(main_db), cron_db_path=str(cron_db), cron_enabled=False),
        enable_team=False,
    )
    crew.cron_store.close()

    main_tables = _table_names(main_db)
    cron_tables = _table_names(cron_db)
    # 主库确实是 core 状态库（确认看的是对的文件）
    assert "sessions" in main_tables
    assert {"cron_jobs", "cron_job_runs", f"{CRON_SCHEMA_FEATURE}_schema_version"} <= cron_tables
    assert main_tables.isdisjoint(
        {"cron_jobs", "cron_job_runs", f"{CRON_SCHEMA_FEATURE}_schema_version"}
    )
    with closing(sqlite3.connect(cron_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {CRON_SCHEMA_FEATURE}_schema_version WHERE singleton = 1"
        ).fetchone()[0]
    assert version == CRON_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# copy-on-first-activate
# ---------------------------------------------------------------------------

def test_legacy_cron_rows_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 启动后 cron.db 行数一致、旧行保留、重复启动零重复。"""

    legacy_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    job_a, job_b = _seed_legacy_cron_jobs(legacy_db, with_run=True)

    store = CronJobStore(str(cron_db), legacy_db_path=str(legacy_db))
    try:
        jobs = store.list(owner_account_id="A:u1")
        assert sorted(job["id"] for job in jobs) == sorted([job_a["id"], job_b["id"]])
        assert store.get_job_run_summary(job_a["id"])["total"] == 1
        assert store.get(job_a["id"], owner_account_id="A:u1")["trigger_payload"]["seconds"] == 60
    finally:
        store.close()

    # crew.db 旧行保留（回退备份，不删）
    assert _count(legacy_db, "cron_jobs") == 2
    assert _count(legacy_db, "cron_job_runs") == 1
    assert _count(cron_db, "cron_jobs") == 2
    assert _count(cron_db, "cron_job_runs") == 1

    # 重复启动幂等零重复
    again = CronJobStore(str(cron_db), legacy_db_path=str(legacy_db))
    try:
        assert len(again.list(owner_account_id="A:u1")) == 2
        assert _count(cron_db, "cron_jobs") == 2
        assert _count(cron_db, "cron_job_runs") == 1
    finally:
        again.close()


def test_build_app_migrates_legacy_cron_rows(tmp_path):
    """验收 2 走真实装配线：build_app 传入 legacy 路径触发 copy-on-first-activate。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    _seed_legacy_cron_jobs(main_db, with_run=True)

    crew = build_app(
        config=Config(db_path=str(main_db), cron_db_path=str(cron_db), cron_enabled=False),
        enable_team=False,
    )
    crew.cron_store.close()

    assert _count(cron_db, "cron_jobs") == 2
    assert _count(cron_db, "cron_job_runs") == 1
    assert _count(main_db, "cron_jobs") == 2  # 旧行保留（回退备份）
    with closing(sqlite3.connect(cron_db)) as conn:
        names = [str(row[0]) for row in conn.execute("SELECT name FROM cron_jobs ORDER BY name")]
    assert names == ["every", "once"]


def test_copy_skipped_when_target_has_rows(tmp_path):
    """目标库已有行即整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    _seed_legacy_cron_jobs(legacy_db)

    target = CronJobStore(str(cron_db))
    try:
        target.create(
            name="fresh", schedule="every 1m", query="q", session_id="s",
            owner_account_id="B:u2",
        )
    finally:
        target.close()

        store = CronJobStore(str(cron_db), legacy_db_path=str(legacy_db))
    try:
        jobs = store.list(owner_account_id="B:u2")
        assert [job["name"] for job in jobs] == ["fresh"]
        assert _count(cron_db, "cron_jobs") == 1
    finally:
        store.close()
    # 旧库未被触碰
    assert _count(legacy_db, "cron_jobs") == 2


def test_copy_skipped_when_legacy_missing_or_lacks_cron_tables(tmp_path):
    """旧库文件缺失或旧库没有 cron 表：安全跳过，不报错不复制。"""

    cron_db = tmp_path / "cron.db"
    store = CronJobStore(str(cron_db), legacy_db_path=str(tmp_path / "not_exists.db"))
    try:
        assert store.list(owner_account_id="", _all_owners=True) == []
    finally:
        store.close()

    legacy_only_sessions = tmp_path / "legacy_main.db"
    sessions = SQLiteSessionStore(str(legacy_only_sessions))
    sessions.save("s1", [Message.user("hi")], owner_account_id="A:u1")
    sessions.close()

    other = CronJobStore(str(tmp_path / "cron2.db"), legacy_db_path=str(legacy_only_sessions))
    try:
        assert other.list(owner_account_id="", _all_owners=True) == []
    finally:
        other.close()
    assert _count(legacy_only_sessions, "sessions") == 1


def test_copy_legacy_feature_rows_skips_missing_table_per_table(tmp_path):
    """表级 gate：旧库缺某表只跳过该表，其余表正常复制。"""

    legacy_db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "CREATE TABLE cron_jobs ("
            "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "next_run_at REAL NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO cron_jobs VALUES ('job-a', 'once', 5.0, 1.0)")
        conn.commit()

    target_db = tmp_path / "cron.db"
    CronJobStore(str(target_db)).close()
    conn = connect_sqlite(str(target_db))
    try:
        copied = copy_legacy_feature_rows(legacy_db, conn, CRON_DB_TABLES)
        assert copied == {"cron_jobs": 1, "cron_job_runs": 0}
    finally:
        conn.close()
    assert _count(target_db, "cron_jobs") == 1


def test_copy_failure_rolls_back_whole_transaction(tmp_path):
    """跨库事务边界：第二张表约束冲突时，第一张表已复制的行一并回滚（不半写）。"""

    legacy_db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "CREATE TABLE cron_jobs ("
            "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "next_run_at REAL NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO cron_jobs VALUES ('job-a', 'once', 5.0, 1.0)")
        # status 列在目标库是 NOT NULL：这行会让 cron_job_runs 的复制失败
        conn.execute(
            "CREATE TABLE cron_job_runs ("
            "id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, "
            "started_at REAL NOT NULL, status TEXT)"
        )
        conn.execute("INSERT INTO cron_job_runs VALUES (1, 'job-a', 0.0, NULL)")
        conn.commit()

    target_db = tmp_path / "cron.db"
    CronJobStore(str(target_db)).close()
    conn = connect_sqlite(str(target_db))
    try:
        writer = SQLiteWriteHelper(conn, threading.Lock())
        with pytest.raises(sqlite3.IntegrityError):
            writer.execute(
                lambda c: copy_legacy_feature_rows(legacy_db, c, CRON_DB_TABLES)
            )
        # 复制失败不半写：先成功的 cron_jobs 行也被回滚
        assert conn.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cron_job_runs").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# claim-legacy / 启动期 owner 巡检跨库适配
# ---------------------------------------------------------------------------

def test_claim_legacy_spans_main_and_cron_databases(tmp_path):
    """验收 4：claim-legacy 按表归属跨库扫描（cron 表读 cron 库）。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"

    sessions = SQLiteSessionStore(str(main_db))
    sessions.save("s-legacy", [Message.user("hi")], owner_account_id="")
    sessions.close()
    cron = CronJobStore(str(cron_db))
    job = cron.create(
        name="legacy", schedule="every 1m", query="q", session_id="s-legacy",
        owner_account_id="",
    )
    cron.close()

    targets = legacy_owner_scan_targets(main_db, cron_db)
    assert set(targets) == {main_db, cron_db}

    # dry-run 只统计不写入
    dry_changed, _ = claim_legacy_owner_databases(targets, "A:uid-1", dry_run=True)
    assert dry_changed["sessions"] == 1
    assert dry_changed["cron_jobs"] == 1
    with closing(sqlite3.connect(cron_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cron_jobs WHERE owner_account_id = 'A:uid-1'"
        ).fetchone()[0] == 0

    changed, remaining = claim_legacy_owner_databases(targets, "A:uid-1")
    assert changed["sessions"] == 1
    assert changed["cron_jobs"] == 1
    assert remaining["sessions"] == 0
    assert remaining["cron_jobs"] == 0

    with closing(sqlite3.connect(main_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE owner_account_id = ''"
        ).fetchone()[0] == 0
    with closing(sqlite3.connect(cron_db)) as conn:
        assert conn.execute(
            "SELECT owner_account_id FROM cron_jobs WHERE id = ?", (job["id"],)
        ).fetchone()[0] == "A:uid-1"


def test_same_file_targets_merge_all_tables(tmp_path):
    """回退配置（Feature 库指回 crew.db）时映射合并到同一文件。"""

    db = tmp_path / "crew.db"
    targets = legacy_owner_scan_targets(db, db)
    assert set(targets) == {db}
    assert set(targets[db]) >= {"sessions", "cron_jobs", "cron_job_runs"}

    # work 批（ADR-0038 第二批）：全部库指回同一文件时同样合并
    merged = legacy_owner_scan_targets(db, db, db)
    assert set(merged) == {db}
    assert set(merged[db]) >= {"sessions", "cron_jobs", "cron_job_runs", *WORK_DB_TABLES}


def test_startup_backfill_smart_attribution_across_databases(tmp_path):
    """启动期巡检：cron 无主任务的智能回填跨库读 sessions。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"

    sessions = SQLiteSessionStore(str(main_db))
    sessions.save("s-owned", [Message.user("a")], owner_account_id="email:alice")
    sessions.save("s-shared", [Message.user("a")], owner_account_id="email:alice")
    sessions.save("s-shared", [Message.user("b")], owner_account_id="email:bob")
    sessions.close()
    cron = CronJobStore(str(cron_db))
    job_owned = cron.create(
        name="owned", schedule="every 1m", query="q", session_id="s-owned",
        owner_account_id="",
    )
    job_shared = cron.create(
        name="shared", schedule="every 1m", query="q", session_id="s-shared",
        owner_account_id="",
    )
    cron.close()

    counts, backfilled = inspect_and_backfill_legacy_owners(
        legacy_owner_scan_targets(main_db, cron_db)
    )

    assert backfilled == 1
    assert counts["cron_jobs"] == 0
    with closing(sqlite3.connect(cron_db)) as conn:
        owners = dict(conn.execute("SELECT id, owner_account_id FROM cron_jobs"))
    assert owners[job_owned["id"]] == "email:alice"
    # 歧义会话无处归属 → local 兜底
    assert owners[job_shared["id"]] == LOCAL_OWNER_ACCOUNT_ID


# ---------------------------------------------------------------------------
# CLI migrate feature cron
# ---------------------------------------------------------------------------

def test_migrate_cron_feature_stamps_cron_db(tmp_path):
    """`migrate feature cron` 的版本表 stamp 到 cron 库，不触碰主库。"""

    from crew.cli.management import _migrate_cron

    app = SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            cron_db_path=str(tmp_path / "cron.db"),
            sqlite_wal=False,
        )
    )

    report = _migrate_cron(app)

    assert report.feature == CRON_SCHEMA_FEATURE
    assert report.current_version == CRON_SCHEMA_VERSION
    assert report.target_version == CRON_SCHEMA_VERSION
    assert (tmp_path / "cron.db").exists()
    assert not (tmp_path / "crew.db").exists()
    with closing(sqlite3.connect(tmp_path / "cron.db")) as conn:
        version = conn.execute(
            "SELECT version FROM cron_schema_version WHERE singleton = 1"
        ).fetchone()[0]
    assert version == CRON_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# work 批（ADR-0038 第二批）：work 域 15 表迁 crew_data/work.db
# ---------------------------------------------------------------------------

_WORK_OWNER = "A:u1"


class _OneRecordAdapter:
    """最小同步适配器：每次刷新产出一条外部记录（种子 work_source_records）。"""

    def fetch(self, cursor: str | None) -> SourceSyncBatch:
        record = SourceRecordInput(
            external_id="ext-1",
            external_version="v1",
            title="外部工单",
            kind="ticket",
            source_status="open",
        )
        return SourceSyncBatch(records=(record,), next_cursor=None)


def _expected_work_row_counts() -> dict[str, int]:
    return dict.fromkeys(WORK_DB_TABLES, 1)


def _seed_legacy_work_rows(legacy_db) -> None:
    """在旧主库中预置带数据的 work 域 15 表（真实 store 语义，覆盖 FK 链）。

    work_item_events / work_session_links / work_references 层层外键指向
    work_items，恰好验证整表复制的父表先于子表顺序。
    """
    owner = _WORK_OWNER
    items = WorkItemStore(str(legacy_db))
    item = items.create(owner_account_id=owner, title="旧工单", workspace_id="w1")
    items.close()

    sources = WorkSourceStore(
        str(legacy_db), approved_source_keys={"crm"}, adapters={"crm": _OneRecordAdapter()}
    )
    sources.set_enabled(owner, "crm", True)
    sources.refresh(owner, "crm")
    sources.close()

    refs = WorkReferenceStore(str(legacy_db), session_store=object())
    refs.link_session(
        owner_account_id=owner, session_id="s-legacy", product_mode="work"
    )
    refs.create_reference(
        owner_account_id=owner,
        target_session_id="s-legacy",
        reference_type="work_item",
        source_id=item.item_id,
        target_item_id=item.item_id,
    )
    refs.close()

    prefs = WorkPreferenceStore(str(legacy_db))
    prefs.set_auto_learning_enabled(owner, True)
    prefs.create(owner_account_id=owner, category="沟通", content="先给结论")
    prefs.record_candidate(
        owner_account_id=owner,
        session_id="s-legacy",
        category="沟通",
        content="先给结论",
        evidence_summary="历史证据",
    )
    prefs.close()

    briefs = WorkBriefStore(str(legacy_db))
    briefs.put_for_date(
        owner_account_id=owner,
        business_date="2026-09-11",
        content={"done": 1},
        input_version="v1",
    )
    briefs.archive_period_report(
        owner_account_id=owner,
        period="week",
        period_start="2026-09-07",
        period_end="2026-09-13",
        metrics={"done": 1},
    )
    briefs.close()

    knowledge = WorkKnowledgeStore(str(legacy_db))
    knowledge.request_publish(owner, page_id="page-1", target="org")
    knowledge.set_index_status(owner, "w1", enabled=True)
    knowledge.close()

    settings_store = WorkSettingsStore(str(legacy_db), workspace_store=object())
    settings_store.update_account_settings(
        owner, dnd_enabled=True, dnd_start="22:00", dnd_end="07:00"
    )
    settings_store.close()

    templates = WorkTemplateStore(str(legacy_db))
    templates.create(owner_account_id=owner, name="周报模板")
    templates.close()


def _build_work_bundle(db_path, *, legacy_db_path=None):
    """工厂级构建：与 build_app 同一条 _store_factory 路径，不带网关宿主。"""

    host = SimpleNamespace(
        session_store=object(),
        workspace_store=object(),
        work_service=None,
        plugins=None,
        _notify_owner_fn=None,
    )
    return build_work_feature(
        host,
        db_path=str(db_path),
        wal_enabled=False,
        legacy_db_path=str(legacy_db_path) if legacy_db_path is not None else None,
        hook_registry=object(),
    )


def test_load_config_reads_and_normalizes_work_db_path(tmp_path, monkeypatch):
    """runtime.work_db_path 与 cron_db_path 同法：可配置，相对路径归一到 crew_home。"""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "runtime:\n  work_db_path: custom/work.db\n", encoding="utf-8"
    )
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.work_db_path == str(tmp_path / "home" / "custom" / "work.db")


def test_load_config_work_db_path_defaults_next_to_main_db(tmp_path, monkeypatch):
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.work_db_path == str(tmp_path / "home" / "crew_data" / "work.db")


def test_fresh_install_work_tables_live_only_in_work_db(tmp_path):
    """验收 1：新装环境 work 表只出现在 work.db，crew.db 无 work 表。"""

    main_db = tmp_path / "crew.db"
    work_db = tmp_path / "work.db"
    crew = build_app(
        config=Config(db_path=str(main_db), work_db_path=str(work_db), cron_enabled=False),
        enable_team=False,
    )
    crew.work_service.close()

    main_tables = _table_names(main_db)
    work_tables = _table_names(work_db)
    assert "sessions" in main_tables
    expected = set(WORK_DB_TABLES) | {f"{WORK_SCHEMA_FEATURE}_schema_version"}
    assert expected <= work_tables
    assert main_tables.isdisjoint(expected)
    with closing(sqlite3.connect(work_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {WORK_SCHEMA_FEATURE}_schema_version WHERE singleton = 1"
        ).fetchone()[0]
    assert version == WORK_SCHEMA_VERSION


def test_legacy_work_rows_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 激活后 work.db 行数一致、旧行保留、重复激活零重复。"""

    legacy_db = tmp_path / "crew.db"
    work_db = tmp_path / "work.db"
    _seed_legacy_work_rows(legacy_db)
    expected = _expected_work_row_counts()

    bundle = _build_work_bundle(work_db, legacy_db_path=legacy_db)
    try:
        assert len(bundle.service.items.list(_WORK_OWNER)) == 1
    finally:
        bundle.service.close()
    for table, count in expected.items():
        assert _count(work_db, table) == count, table
        assert _count(legacy_db, table) == count, table  # 旧行保留（回退备份）

    # 重复激活幂等零重复
    again = _build_work_bundle(work_db, legacy_db_path=legacy_db)
    try:
        assert len(again.service.items.list(_WORK_OWNER)) == 1
    finally:
        again.service.close()
    for table, count in expected.items():
        assert _count(work_db, table) == count, table


def test_build_app_migrates_legacy_work_rows(tmp_path):
    """验收 2 走真实装配线：build_app 传入 legacy 路径触发 copy-on-first-activate。"""

    main_db = tmp_path / "crew.db"
    work_db = tmp_path / "work.db"
    _seed_legacy_work_rows(main_db)

    crew = build_app(
        config=Config(db_path=str(main_db), work_db_path=str(work_db), cron_enabled=False),
        enable_team=False,
    )
    crew.work_service.close()

    assert _count(work_db, "work_items") == 1
    assert _count(work_db, "work_references") == 1
    assert _count(work_db, "work_source_records") == 1
    assert _count(main_db, "work_items") == 1  # 旧行保留（回退备份）


def test_work_copy_skipped_when_target_has_rows(tmp_path):
    """目标库已有行即整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    work_db = tmp_path / "work.db"
    _seed_legacy_work_rows(legacy_db)

    first = _build_work_bundle(work_db)
    first.service.items.create(owner_account_id="B:u2", title="新装工单")
    first.service.close()

    again = _build_work_bundle(work_db, legacy_db_path=legacy_db)
    try:
        assert [i.title for i in again.service.items.list("B:u2")] == ["新装工单"]
        assert again.service.items.list(_WORK_OWNER) == []
    finally:
        again.service.close()
    assert _count(work_db, "work_items") == 1
    # 旧库未被触碰
    assert _count(legacy_db, "work_items") == 1


def test_work_copy_skipped_when_legacy_missing_or_lacks_work_tables(tmp_path):
    """旧库文件缺失或旧库没有 work 表：安全跳过，不报错不复制。"""

    work_db = tmp_path / "work.db"
    bundle = _build_work_bundle(work_db, legacy_db_path=tmp_path / "not_exists.db")
    try:
        assert bundle.service.items.list(_WORK_OWNER) == []
    finally:
        bundle.service.close()

    legacy_only_sessions = tmp_path / "legacy_main.db"
    sessions = SQLiteSessionStore(str(legacy_only_sessions))
    sessions.save("s1", [Message.user("hi")], owner_account_id="A:u1")
    sessions.close()

    other = _build_work_bundle(tmp_path / "work2.db", legacy_db_path=legacy_only_sessions)
    try:
        assert other.service.items.list(_WORK_OWNER) == []
    finally:
        other.service.close()
    assert _count(legacy_only_sessions, "sessions") == 1
    assert _count(tmp_path / "work2.db", "work_items") == 0


def test_work_copy_legacy_feature_rows_skips_missing_table_per_table(tmp_path):
    """表级 gate：旧库缺某表只跳过该表，其余表正常复制。"""

    legacy_db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "CREATE TABLE work_templates ("
            "owner_account_id TEXT NOT NULL, template_id TEXT NOT NULL, "
            "name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (owner_account_id, template_id))"
        )
        conn.execute(
            "INSERT INTO work_templates VALUES ('A:u1', 'wt_1', '旧模板', 1.0, 1.0)"
        )
        conn.commit()

    target_db = tmp_path / "work.db"
    bundle = _build_work_bundle(target_db)
    bundle.service.close()
    conn = connect_sqlite(str(target_db), wal_enabled=False)
    try:
        copied = copy_legacy_feature_rows(legacy_db, conn, WORK_DB_TABLES)
        assert copied["work_templates"] == 1
        assert all(count == 0 for name, count in copied.items() if name != "work_templates")
        assert sum(copied.values()) == 1
    finally:
        conn.close()
    assert _count(target_db, "work_templates") == 1


def test_work_copy_failure_rolls_back_whole_transaction(tmp_path):
    """跨库事务边界：第二张表约束冲突时，第一张表已复制的行一并回滚（不半写）。"""

    legacy_db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "CREATE TABLE work_items ("
            "owner_account_id TEXT NOT NULL, item_id TEXT NOT NULL, "
            "title TEXT NOT NULL, business_status TEXT NOT NULL, "
            "execution_status TEXT NOT NULL, sync_status TEXT NOT NULL, "
            "priority TEXT NOT NULL, disposition TEXT NOT NULL, "
            "version INTEGER NOT NULL, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL, PRIMARY KEY (owner_account_id, item_id))"
        )
        conn.execute(
            "INSERT INTO work_items VALUES "
            "('A:u1', 'item-a', '旧工单', 'pending', 'not_started', "
            "'not_applicable', 'unset', 'active', 1, 1.0, 1.0)"
        )
        # created_at 列在目标库是 NOT NULL：这行会让 work_item_events 的复制失败
        conn.execute(
            "CREATE TABLE work_item_events ("
            "owner_account_id TEXT NOT NULL, event_id TEXT NOT NULL, "
            "item_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "actor TEXT NOT NULL, created_at REAL, "
            "PRIMARY KEY (owner_account_id, event_id))"
        )
        conn.execute(
            "INSERT INTO work_item_events VALUES ('A:u1', 'evt-1', 'item-a', "
            "'created', 'user', NULL)"
        )
        conn.commit()

    target_db = tmp_path / "work.db"
    bundle = _build_work_bundle(target_db)
    bundle.service.close()
    conn = connect_sqlite(str(target_db), wal_enabled=False)
    try:
        writer = SQLiteWriteHelper(conn, threading.Lock())
        with pytest.raises(sqlite3.IntegrityError):
            writer.execute(
                lambda c: copy_legacy_feature_rows(legacy_db, c, WORK_DB_TABLES)
            )
        # 复制失败不半写：先成功的 work_items 行也被回滚
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_claim_legacy_spans_main_and_work_databases(tmp_path):
    """验收 4：claim-legacy 按表归属跨库扫描（work 表读 work 库）。"""

    main_db = tmp_path / "crew.db"
    work_db = tmp_path / "work.db"

    sessions = SQLiteSessionStore(str(main_db))
    sessions.save("s-legacy", [Message.user("hi")], owner_account_id="")
    sessions.close()
    bundle = _build_work_bundle(work_db)
    bundle.service.close()
    # work 表 store 层禁止空 owner；存量异常数据经原生 SQL 构造
    with closing(sqlite3.connect(work_db)) as conn:
        conn.execute(
            "INSERT INTO work_templates (owner_account_id, template_id, name, "
            "blueprint_json, version, usage_count, created_at, updated_at) "
            "VALUES ('', 'wt_legacy', '无主模板', '{}', 1, 0, 0, 0)"
        )
        conn.commit()

    targets = legacy_owner_scan_targets(main_db, None, work_db)
    assert set(targets) == {main_db, work_db}

    dry_changed, _ = claim_legacy_owner_databases(targets, "A:uid-1", dry_run=True)
    assert dry_changed["sessions"] == 1
    assert dry_changed["work_templates"] == 1

    changed, remaining = claim_legacy_owner_databases(targets, "A:uid-1")
    assert changed["sessions"] == 1
    assert changed["work_templates"] == 1
    assert remaining["sessions"] == 0
    assert remaining["work_templates"] == 0

    with closing(sqlite3.connect(work_db)) as conn:
        assert conn.execute(
            "SELECT owner_account_id FROM work_templates WHERE template_id = 'wt_legacy'"
        ).fetchone()[0] == "A:uid-1"


def test_migrate_work_feature_stamps_work_db(tmp_path):
    """`migrate feature work` 的版本表 stamp 到 work 库，不触碰主库。"""

    from crew.cli.management import _migrate_work

    app = SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            work_db_path=str(tmp_path / "work.db"),
            sqlite_wal=False,
        )
    )

    report = _migrate_work(app)

    assert report.feature == WORK_SCHEMA_FEATURE
    assert report.current_version == WORK_SCHEMA_VERSION
    assert report.target_version == WORK_SCHEMA_VERSION
    assert (tmp_path / "work.db").exists()
    assert not (tmp_path / "crew.db").exists()
    with closing(sqlite3.connect(tmp_path / "work.db")) as conn:
        version = conn.execute(
            "SELECT version FROM work_schema_version WHERE singleton = 1"
        ).fetchone()[0]
    assert version == WORK_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# kanban 批（ADR-0038 第三批）：kanban 域 6 表迁 crew_data/kanban.db
# ---------------------------------------------------------------------------


def _seed_legacy_kanban_rows(legacy_db):
    """在旧主库中预置带数据的 kanban 域 6 表（真实 store 语义，覆盖 FK 链）。

    kanban_tasks / kanban_dependencies / kanban_task_runs / kanban_events /
    kanban_runtime_states 逐层外键指向 kanban_workflows / kanban_tasks，
    恰好验证整表复制的父表先于子表顺序。另按 isolation_state 稳态语义构造
    一条 legacy_ambiguous 歧义行（多 owner 会话无法证明归属，store 保持
    owner='' 隔离留给人工认领），锁定分库复制不改写隔离态。歧义行的归属
    证据（多 owner sessions）留在真实主库，这里直接以最终隔离态构造——
    语义载体是 isolation_state 本身，不依赖种子库里的 sessions 表。
    """
    store = SQLiteKanbanStore(str(legacy_db), wal_enabled=False)
    try:
        owner = store.for_owner("A:u1")
        wf = owner.create_workflow("s-legacy", "旧看板")
        task_a = owner.add_task(wf.id, "旧任务甲", auto_promote=False)
        task_b = owner.add_task(
            wf.id, "旧任务乙", parent_task_ids=[task_a.id], auto_promote=False
        )
        owner.start_run(task_b.id, "run-1")
        owner.add_event(wf.id, "task_created", task_id=task_a.id)
        owner.save_runtime_state(RuntimeState(workflow_id=wf.id))
    finally:
        store.close()
    # 歧义行按真实稳态经原生 SQL 构造（store 层禁止空 owner）：
    # 与 _migrate_workflow_ownership 留下的隔离态完全一致。
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "INSERT INTO kanban_workflows (id, session_id, owner_account_id, "
            "isolation_state, schema_version, title, status, context, "
            "created_at, updated_at) VALUES "
            "('wf-ambiguous', 's-amb', '', 'legacy_ambiguous', 2, '歧义看板', "
            "'active', '{}', 1, 1)"
        )
        conn.commit()
    return SimpleNamespace(
        workflow_id=wf.id,
        expected={
            "kanban_workflows": 2,
            "kanban_tasks": 2,
            "kanban_dependencies": 1,
            "kanban_task_runs": 1,
            "kanban_events": 1,
            "kanban_runtime_states": 1,
        },
    )


def _ambiguous_row(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT owner_account_id, isolation_state FROM kanban_workflows "
            "WHERE id = 'wf-ambiguous'"
        ).fetchone()


def test_load_config_reads_and_normalizes_kanban_db_path(tmp_path, monkeypatch):
    """runtime.kanban_db_path 与 cron/work 同法：可配置，相对路径归一到 crew_home。"""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "runtime:\n  kanban_db_path: custom/kanban.db\n", encoding="utf-8"
    )
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.kanban_db_path == str(tmp_path / "home" / "custom" / "kanban.db")


def test_load_config_kanban_db_path_defaults_next_to_main_db(tmp_path, monkeypatch):
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.kanban_db_path == str(tmp_path / "home" / "crew_data" / "kanban.db")


def test_fresh_install_kanban_tables_live_only_in_kanban_db(tmp_path):
    """验收 1：新装环境 kanban 6 表 + 版本表只出现在 kanban.db，crew.db 无 kanban 表。"""

    main_db = tmp_path / "crew.db"
    kanban_db = tmp_path / "kanban.db"
    crew = build_app(
        config=Config(
            db_path=str(main_db), kanban_db_path=str(kanban_db), cron_enabled=False
        ),
        enable_team=False,
    )
    crew.dynamic_kanban.store.close()

    main_tables = _table_names(main_db)
    kanban_tables = _table_names(kanban_db)
    # 主库确实是 core 状态库（确认看的是对的文件）
    assert "sessions" in main_tables
    expected = set(KANBAN_DB_TABLES) | {f"{KANBAN_SCHEMA_FEATURE}_schema_version"}
    assert expected <= kanban_tables
    assert main_tables.isdisjoint(expected)
    with closing(sqlite3.connect(kanban_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {KANBAN_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    assert version == KANBAN_SCHEMA_VERSION


def test_legacy_kanban_rows_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 激活后 kanban.db 行数一致、旧行保留、重复激活零重复；
    legacy_ambiguous 歧义行复制后保持隔离，重启（重跑分类迁移）不被改写。"""

    legacy_db = tmp_path / "crew.db"
    kanban_db = tmp_path / "kanban.db"
    seed = _seed_legacy_kanban_rows(legacy_db)

    store = SQLiteKanbanStore(
        str(kanban_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert store.for_owner("A:u1").get_workflow(seed.workflow_id) is not None
        # 歧义行对任何 owner 不可见（owner='' 隔离）
        assert store.for_owner("A:u1").get_workflow("wf-ambiguous") is None
    finally:
        store.close()

    for table, count in seed.expected.items():
        assert _count(kanban_db, table) == count, table
        assert _count(legacy_db, table) == count, table  # 旧行保留（回退备份）
    assert _ambiguous_row(kanban_db) == ("", "legacy_ambiguous")

    # 重复激活幂等零重复；重启重跑 _init_schema 分类不得改写歧义行
    again = SQLiteKanbanStore(
        str(kanban_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert again.for_owner("A:u1").get_workflow(seed.workflow_id) is not None
        assert _ambiguous_row(kanban_db) == ("", "legacy_ambiguous")
    finally:
        again.close()
    for table, count in seed.expected.items():
        assert _count(kanban_db, table) == count, table


def test_build_app_migrates_legacy_kanban_rows(tmp_path):
    """验收 2 走真实装配线：build_app 传入 legacy 路径触发 copy-on-first-activate。"""

    main_db = tmp_path / "crew.db"
    kanban_db = tmp_path / "kanban.db"
    seed = _seed_legacy_kanban_rows(main_db)

    crew = build_app(
        config=Config(
            db_path=str(main_db), kanban_db_path=str(kanban_db), cron_enabled=False
        ),
        enable_team=False,
    )
    crew.dynamic_kanban.store.close()

    assert _count(kanban_db, "kanban_workflows") == 2
    assert _count(kanban_db, "kanban_tasks") == 2
    assert _count(kanban_db, "kanban_task_runs") == 1
    assert _count(main_db, "kanban_workflows") == 2  # 旧行保留（回退备份）
    assert _ambiguous_row(kanban_db) == ("", "legacy_ambiguous")
    with closing(sqlite3.connect(kanban_db)) as conn:
        title = conn.execute(
            "SELECT title FROM kanban_workflows WHERE id = ?", (seed.workflow_id,)
        ).fetchone()[0]
    assert title == "旧看板"


def test_kanban_copy_skipped_when_target_has_rows(tmp_path):
    """目标库已有行即整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    kanban_db = tmp_path / "kanban.db"
    _seed_legacy_kanban_rows(legacy_db)

    first = SQLiteKanbanStore(str(kanban_db), wal_enabled=False)
    try:
        fresh = first.for_owner("B:u2").create_workflow("s-fresh", "新装看板")
    finally:
        first.close()

    again = SQLiteKanbanStore(
        str(kanban_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert again.for_owner("B:u2").get_workflow(fresh.id) is not None
        assert again.for_owner("A:u1").list_workflows_by_session("s-legacy") == []
    finally:
        again.close()
    assert _count(kanban_db, "kanban_workflows") == 1
    # 旧库未被触碰
    assert _count(legacy_db, "kanban_workflows") == 2


def test_kanban_copy_skipped_when_legacy_missing_or_lacks_kanban_tables(tmp_path):
    """旧库文件缺失或旧库没有 kanban 表：安全跳过，不报错不复制。"""

    kanban_db = tmp_path / "kanban.db"
    store = SQLiteKanbanStore(
        str(kanban_db), wal_enabled=False, legacy_db_path=str(tmp_path / "not_exists.db")
    )
    store.close()

    legacy_only_sessions = tmp_path / "legacy_main.db"
    sessions = SQLiteSessionStore(str(legacy_only_sessions))
    sessions.save("s1", [Message.user("hi")], owner_account_id="A:u1")
    sessions.close()

    other = SQLiteKanbanStore(
        str(tmp_path / "kanban2.db"), wal_enabled=False, legacy_db_path=str(legacy_only_sessions)
    )
    try:
        assert other.for_owner("A:u1").list_workflows_by_session("s1") == []
    finally:
        other.close()
    assert _count(legacy_only_sessions, "sessions") == 1


def test_kanban_copy_failure_rolls_back_whole_transaction(tmp_path):
    """跨库事务边界：子表外键冲突时，父表已复制的行一并回滚（不半写）。"""

    legacy_db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "CREATE TABLE kanban_workflows ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', "
            "context TEXT NOT NULL DEFAULT '{}', "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO kanban_workflows VALUES ('wf-a', 's1', '旧看板', 'active', '{}', 1, 1)"
        )
        # workflow_id 指向不存在的 workflow：这行会让 kanban_tasks 的复制失败
        conn.execute(
            "CREATE TABLE kanban_tasks ("
            "id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, title TEXT NOT NULL, "
            "detail TEXT NOT NULL DEFAULT '', assignee TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "result_summary TEXT NOT NULL DEFAULT '', "
            "artifact_paths TEXT NOT NULL DEFAULT '[]', "
            "retry_count INTEGER NOT NULL DEFAULT 0, "
            "max_retries INTEGER NOT NULL DEFAULT 2, "
            "claimed_by TEXT, claimed_at REAL, done_at REAL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO kanban_tasks (id, workflow_id, title, created_at, updated_at) "
            "VALUES ('task-a', 'wf-missing', '孤儿任务', 1, 1)"
        )
        conn.commit()

    target_db = tmp_path / "kanban.db"
    SQLiteKanbanStore(str(target_db), wal_enabled=False).close()
    conn = connect_sqlite(str(target_db), wal_enabled=False)
    try:
        writer = SQLiteWriteHelper(conn, threading.Lock())
        with pytest.raises(sqlite3.IntegrityError):
            writer.execute(
                lambda c: copy_legacy_feature_rows(legacy_db, c, KANBAN_DB_TABLES)
            )
        # 复制失败不半写：先成功的 kanban_workflows 行也被回滚
        assert conn.execute("SELECT COUNT(*) FROM kanban_workflows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_legacy_owner_scan_targets_route_kanban_tables(tmp_path):
    """claim-legacy 扫描结构：kanban 6 表按归属路由到 kanban 库；
    回退配置（kanban 库指回 crew.db）时合并到同一文件条目。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    work_db = tmp_path / "work.db"
    kanban_db = tmp_path / "kanban.db"

    targets = legacy_owner_scan_targets(main_db, cron_db, work_db, kanban_db)
    assert set(targets) == {main_db, cron_db, work_db, kanban_db}
    assert set(targets[kanban_db]) == set(KANBAN_DB_TABLES)
    # kanban 表不进主库条目（`table not in KANBAN_DB_TABLES` 守卫）
    assert set(targets[main_db]).isdisjoint(KANBAN_DB_TABLES)

    db = tmp_path / "fallback.db"
    merged = legacy_owner_scan_targets(db, db, db, db)
    assert set(merged) == {db}
    assert set(merged[db]) >= {"sessions", "cron_jobs", *KANBAN_DB_TABLES}


def test_startup_backfill_leaves_kanban_quarantine_intact(tmp_path):
    """生产形状的启动期巡检（不含 kanban 路径）不触碰 kanban 歧义行：
    owner='' + legacy_ambiguous 的隔离留给人工认领，不被通用回填改写。"""

    kanban_db = tmp_path / "kanban.db"
    _seed_legacy_kanban_rows(kanban_db)

    main_db = tmp_path / "crew.db"
    targets = legacy_owner_scan_targets(
        main_db, tmp_path / "cron.db", tmp_path / "work.db"
    )
    assert set(targets[main_db]).isdisjoint(KANBAN_DB_TABLES)

    counts, _backfilled = inspect_and_backfill_legacy_owners(targets)

    assert counts.get("kanban_workflows") is None
    assert _ambiguous_row(kanban_db) == ("", "legacy_ambiguous")


def test_migrate_kanban_feature_stamps_kanban_db(tmp_path):
    """`migrate feature kanban` 的版本表 stamp 到 kanban 库，不触碰主库。"""

    from crew.cli.management import _migrate_kanban

    app = SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            kanban_db_path=str(tmp_path / "kanban.db"),
            sqlite_wal=False,
        )
    )

    report = _migrate_kanban(app)

    assert report.feature == KANBAN_SCHEMA_FEATURE
    assert report.current_version == KANBAN_SCHEMA_VERSION
    assert report.target_version == KANBAN_SCHEMA_VERSION
    assert (tmp_path / "kanban.db").exists()
    assert not (tmp_path / "crew.db").exists()
    with closing(sqlite3.connect(tmp_path / "kanban.db")) as conn:
        version = conn.execute(
            "SELECT version FROM kanban_schema_version WHERE singleton = 1"
        ).fetchone()[0]
    assert version == KANBAN_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# external + team 批（ADR-0038 第四批）：external 域 4 表迁 external.db、
# team 域 2 表迁 team.db（同一门面一次双库）
# ---------------------------------------------------------------------------

_EXTERNAL_RUNTIME = {
    "id": "runtime-a",
    "provider": "kimi",
    "name": "Kimi",
    "executable_path": "/bin/kimi",
    "version": "1.2.3",
}


def _seed_legacy_external_and_team_rows(legacy_db):
    """在旧主库中预置带数据的 external 4 表 + team 2 表（真实 store 语义）。

    external_agent / observations / bindings 逐层外键指向 external_runtime
    与 external_agent，external_team_member 指向 external_team，恰好验证两域
    整表复制的父表先于子表顺序。返回 agent/team 标识与各表期望行数。
    """
    catalog = ExternalAgentStore(str(legacy_db))
    catalog.upsert_runtime(dict(_EXTERNAL_RUNTIME))
    agent = catalog.create_agent(
        owner_account_id="A:u1", name="外援甲", runtime_id="runtime-a"
    )
    catalog.save_runtime_session_binding(
        owner_account_id="A:u1",
        crew_session_id="s-legacy",
        external_agent_id=agent["id"],
        runtime_id="runtime-a",
        adapter_id="acp-stdio",
        native_session_id="native-1",
    )
    catalog.record_agent_profile_observation(
        owner_account_id="A:u1",
        external_agent_id=agent["id"],
        source_run_id="run-1",
        source_node_id="node-1",
        source_attempt_id="attempt-1",
        capabilities=["implementation"],
        assessment_source="probe",
        outcome="success",
        quality_weight=0.5,
    )
    facade = TeamExternalAgentStore(str(legacy_db))
    team = facade.create_team(
        owner_account_id="A:u1",
        name="旧团队",
        leader_agent_id=agent["id"],
        members=[{"agent_id": agent["id"], "role": "Leader"}],
    )
    return SimpleNamespace(
        agent_id=agent["id"],
        team_id=team["id"],
        expected={
            "external_runtime": 1,
            "external_agent": 1,
            "external_agent_profile_observation": 1,
            "external_runtime_session_binding": 1,
            "external_team": 1,
            "external_team_member": 1,
        },
    )


def test_load_config_reads_and_normalizes_external_and_team_db_paths(
    tmp_path, monkeypatch
):
    """runtime.external_db_path / team_db_path 与前几批同法：可配置，相对路径归一。"""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "runtime:\n"
        "  external_db_path: custom/external.db\n"
        "  team_db_path: custom/team.db\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.external_db_path == str(tmp_path / "home" / "custom" / "external.db")
    assert cfg.team_db_path == str(tmp_path / "home" / "custom" / "team.db")


def test_load_config_external_and_team_db_paths_default_next_to_main_db(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.external_db_path == str(tmp_path / "home" / "crew_data" / "external.db")
    assert cfg.team_db_path == str(tmp_path / "home" / "crew_data" / "team.db")


def test_fresh_install_external_and_team_tables_live_in_their_own_dbs(tmp_path):
    """验收 1：新装环境 external 4 表 + 版本表只在 external.db，team 2 表 +
    版本表只在 team.db，主库两域皆无。"""

    main_db = tmp_path / "crew.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    build_app(
        config=Config(
            db_path=str(main_db),
            external_db_path=str(external_db),
            team_db_path=str(team_db),
            cron_enabled=False,
        ),
        enable_team=False,
    )

    main_tables = _table_names(main_db)
    assert "sessions" in main_tables  # 主库确实是 core 状态库
    external_expected = set(EXTERNAL_DB_TABLES) | {
        f"{EXTERNAL_SCHEMA_FEATURE}_schema_version"
    }
    team_expected = set(TEAM_DB_TABLES) | {f"{TEAM_SCHEMA_FEATURE}_schema_version"}
    assert external_expected <= _table_names(external_db)
    assert team_expected <= _table_names(team_db)
    assert main_tables.isdisjoint(external_expected | team_expected)
    with closing(sqlite3.connect(external_db)) as conn:
        external_version = conn.execute(
            f"SELECT version FROM {EXTERNAL_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    with closing(sqlite3.connect(team_db)) as conn:
        team_version = conn.execute(
            f"SELECT version FROM {TEAM_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    assert external_version == EXTERNAL_SCHEMA_VERSION
    assert team_version == TEAM_SCHEMA_VERSION


def test_legacy_external_and_team_rows_copied_on_first_activate_and_idempotent(
    tmp_path,
):
    """验收 2：存量 crew.db 构造分库门面后两域行数一致、旧行保留、
    重复构造幂等零重复；team 库内跨命名空间回填守卫不触表报错。"""

    legacy_db = tmp_path / "crew.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    seed = _seed_legacy_external_and_team_rows(legacy_db)

    store = TeamExternalAgentStore(
        str(external_db), team_db_path=str(team_db), legacy_db_path=str(legacy_db)
    )
    assert store.get_agent(seed.agent_id, owner_account_id="A:u1")["name"] == "外援甲"
    assert store.get_team(seed.team_id, owner_account_id="A:u1")["name"] == "旧团队"

    for table, count in seed.expected.items():
        target_db = team_db if table in TEAM_DB_TABLES else external_db
        assert _count(target_db, table) == count, table
        assert _count(legacy_db, table) == count, table  # 旧行保留（回退备份）

    # 重复构造幂等零重复（catalog_factory 每次激活重建门面的真实形状）
    again = TeamExternalAgentStore(
        str(external_db), team_db_path=str(team_db), legacy_db_path=str(legacy_db)
    )
    assert again.get_team(seed.team_id, owner_account_id="A:u1") is not None
    for table, count in seed.expected.items():
        target_db = team_db if table in TEAM_DB_TABLES else external_db
        assert _count(target_db, table) == count, table


def test_build_app_migrates_legacy_external_and_team_rows(tmp_path):
    """验收 2 走真实装配线：build_app 传入双库路径 + legacy 触发分域复制；
    current_external_catalog 动态解析链在分库后照常工作。"""

    main_db = tmp_path / "crew.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    seed = _seed_legacy_external_and_team_rows(main_db)

    crew = build_app(
        config=Config(
            db_path=str(main_db),
            external_db_path=str(external_db),
            team_db_path=str(team_db),
            cron_enabled=False,
        ),
        enable_team=False,
    )

    assert _count(external_db, "external_agent") == 1
    assert _count(team_db, "external_team") == 1
    assert _count(main_db, "external_agent") == 1  # 旧行保留（回退备份）
    catalog = crew.current_external_catalog()
    assert catalog is not None
    assert catalog.get_agent(seed.agent_id, owner_account_id="A:u1")["name"] == "外援甲"
    assert catalog.get_team(seed.team_id, owner_account_id="A:u1")["name"] == "旧团队"


def test_external_team_copy_skipped_when_target_has_rows(tmp_path):
    """域级 gate 相互独立：team 库已有行则 team 域整体跳过（不合并旧行、
    不触碰旧库），external 库为空则 external 域照常复制。"""

    legacy_db = tmp_path / "crew.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    seed = _seed_legacy_external_and_team_rows(legacy_db)

    first = TeamExternalAgentStore(str(external_db), team_db_path=str(team_db))
    fresh_team = first.create_team(
        owner_account_id="B:u2",
        name="新装团队",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[],
    )

    again = TeamExternalAgentStore(
        str(external_db), team_db_path=str(team_db), legacy_db_path=str(legacy_db)
    )
    assert again.get_team(fresh_team["id"], owner_account_id="B:u2") is not None
    with pytest.raises(KeyError):
        again.get_team(seed.team_id, owner_account_id="A:u1")  # 旧 team 行未并入
    assert _count(team_db, "external_team") == 1
    assert _count(legacy_db, "external_team") == 1  # 旧库未被触碰
    # external 库此场景为空：external 域独立照常复制
    assert _count(external_db, "external_agent") == 1


def test_external_team_copy_skipped_when_legacy_missing_or_lacks_tables(tmp_path):
    """旧库文件缺失或旧库没有两域表：安全跳过，不报错不复制。"""

    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    TeamExternalAgentStore(
        str(external_db),
        team_db_path=str(team_db),
        legacy_db_path=str(tmp_path / "not_exists.db"),
    )
    assert _count(team_db, "external_team") == 0

    legacy_only_sessions = tmp_path / "legacy_main.db"
    sessions = SQLiteSessionStore(str(legacy_only_sessions))
    sessions.save("s1", [Message.user("hi")], owner_account_id="A:u1")
    sessions.close()

    TeamExternalAgentStore(
        str(tmp_path / "external2.db"),
        team_db_path=str(tmp_path / "team2.db"),
        legacy_db_path=str(legacy_only_sessions),
    )
    assert _count(tmp_path / "team2.db", "external_team") == 0
    assert _count(tmp_path / "external2.db", "external_agent") == 0
    assert _count(legacy_only_sessions, "sessions") == 1


def test_split_copy_backfills_member_snapshots_against_legacy_first(tmp_path):
    """快照回填处置（迁移时序方案）的行为锁定：分库复制前先在旧库完成
    成员快照回填——两命名空间此刻仍同库，回填语义与拆库前一致；复制进
    team 库的成员行因此与同库布局等价（空快照行被补齐）。"""

    legacy_db = tmp_path / "crew.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    seed = _seed_legacy_external_and_team_rows(legacy_db)
    # 制造 legacy 空快照行（模拟快照列引入前写入的成员行）
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.execute(
            "UPDATE external_team_member SET agent_name = '', agent_provider = ''"
        )
        conn.commit()

    store = TeamExternalAgentStore(
        str(external_db), team_db_path=str(team_db), legacy_db_path=str(legacy_db)
    )

    member = store.get_team(seed.team_id, owner_account_id="A:u1")["members"][0]
    assert member["agent_name"] == "外援甲"
    assert member["agent_provider"] == "kimi"
    # 旧库行同步被回填（复制前的等价执行点，幂等只填空快照）
    with closing(sqlite3.connect(legacy_db)) as conn:
        legacy_row = conn.execute(
            "SELECT agent_name, agent_provider FROM external_team_member"
        ).fetchone()
    assert legacy_row == ("外援甲", "kimi")


def test_team_schema_init_skips_cross_namespace_backfill_when_split(tmp_path):
    """分库后 team 库没有 external_agent 表：快照回填守卫按 sqlite_master
    存在性跳过，重复构造不再触发跨命名空间 SQL（旧实现会抛 no such table）；
    无从解析的空快照行保持原样，读侧 builtin 回退语义不变。"""

    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"
    store = TeamExternalAgentStore(str(external_db), team_db_path=str(team_db))
    team = store.create_team(
        owner_account_id="A:u1",
        name="内置团队",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[],
    )
    with closing(sqlite3.connect(team_db)) as conn:
        conn.execute(
            "UPDATE external_team_member SET agent_name = '', agent_provider = ''"
        )
        conn.commit()

    again = TeamExternalAgentStore(
        str(external_db),
        team_db_path=str(team_db),
        legacy_db_path=str(tmp_path / "missing.db"),
    )
    member = again.get_team(team["id"], owner_account_id="A:u1")["members"][0]
    assert member["agent_id"] == CREW_BUILTIN_AGENT_ID
    with closing(sqlite3.connect(team_db)) as conn:
        row = conn.execute(
            "SELECT agent_name, agent_provider FROM external_team_member"
        ).fetchone()
    assert row == ("", "")


def test_legacy_owner_scan_targets_route_external_and_team_tables(tmp_path):
    """claim-legacy 扫描结构：external/team 两域按 kanban 显式语义登记——
    显式传参才有条目、缺省不进任何条目、回退配置并入同一文件条目。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    work_db = tmp_path / "work.db"
    kanban_db = tmp_path / "kanban.db"
    external_db = tmp_path / "external.db"
    team_db = tmp_path / "team.db"

    targets = legacy_owner_scan_targets(
        main_db, cron_db, work_db, kanban_db, external_db, team_db
    )
    assert set(targets) == {main_db, cron_db, work_db, kanban_db, external_db, team_db}
    assert set(targets[external_db]) == set(EXTERNAL_DB_TABLES)
    assert set(targets[team_db]) == set(TEAM_DB_TABLES)
    domain_tables = set(EXTERNAL_DB_TABLES) | set(TEAM_DB_TABLES)
    assert set(targets[main_db]).isdisjoint(domain_tables)

    # 生产形状（不传 external/team 路径）：两域不登记进任何条目
    default_targets = legacy_owner_scan_targets(main_db, cron_db, work_db)
    assert set(default_targets) == {main_db, cron_db, work_db}
    assert all(
        set(tables).isdisjoint(domain_tables) for tables in default_targets.values()
    )

    # 回退配置（两域指回主库同一文件）：按回退语义并入该条目
    db = tmp_path / "fallback.db"
    merged = legacy_owner_scan_targets(db, db, db, db, db, db)
    assert set(merged) == {db}
    assert set(merged[db]) >= {"sessions", *EXTERNAL_DB_TABLES, *TEAM_DB_TABLES}


def test_migrate_external_and_team_feature_stamp_own_dbs(tmp_path):
    """`migrate feature external` / `team` 的版本表各自 stamp 到自己的库。"""

    from crew.cli.management import _migrate_external, _migrate_team

    app = SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            external_db_path=str(tmp_path / "external.db"),
            team_db_path=str(tmp_path / "team.db"),
            sqlite_wal=False,
        )
    )

    external_report = _migrate_external(app)
    assert external_report.feature == EXTERNAL_SCHEMA_FEATURE
    assert external_report.current_version == EXTERNAL_SCHEMA_VERSION
    assert external_report.target_version == EXTERNAL_SCHEMA_VERSION

    team_report = _migrate_team(app)
    assert team_report.feature == TEAM_SCHEMA_FEATURE
    assert team_report.current_version == TEAM_SCHEMA_VERSION
    assert team_report.target_version == TEAM_SCHEMA_VERSION

    assert (tmp_path / "external.db").exists()
    assert (tmp_path / "team.db").exists()
    assert not (tmp_path / "crew.db").exists()
    with closing(sqlite3.connect(tmp_path / "external.db")) as conn:
        assert (
            conn.execute(
                "SELECT version FROM external_schema_version WHERE singleton = 1"
            ).fetchone()[0]
            == EXTERNAL_SCHEMA_VERSION
        )
    with closing(sqlite3.connect(tmp_path / "team.db")) as conn:
        assert (
            conn.execute(
                "SELECT version FROM team_schema_version WHERE singleton = 1"
            ).fetchone()[0]
            == TEAM_SCHEMA_VERSION
        )


# ---------------------------------------------------------------------------
# sites + tasks + notifications 收尾批（ADR-0038 第五批）：三小域各迁独立库。
# sites 域 10 表（store 4 + blueprint 6）同住 sites.db 两半各自复制；
# tasks/notifications 各 1 表，照 cron/work 语义接入通用巡检/认领。
# ---------------------------------------------------------------------------

def _seed_legacy_sites_rows(legacy_db):
    """在旧主库中预置带数据的 sites 10 表（走真实 store 语义）。

    store 半域经 upsert_site / release / 两类 annotation 各造一行，
    blueprint 半域造一个 canvas。返回 site_id 与各表期望行数。
    """
    store = SQLiteSiteStore(str(legacy_db))
    try:
        site = store.upsert_site(
            owner="A:u1", workspace_id="ws1", session_id="s1", name="旧站点",
            source_path="src", build_command="npm run build", output_directory="dist",
        )
        release = store.create_release("A:u1", site["id"])
        store.finish_release("A:u1", site["id"], release["id"], status="ready")
        store.create_annotation("A:u1", site["id"], release["id"], {"comment": "改这里"})
        store.create_inspiration_annotation(
            "A:u1", "insp-1", "widget", "rev-1", {"comment": "灵感笔记"}
        )
    finally:
        store.close()
    blueprint = BlueprintStore(str(legacy_db))
    try:
        blueprint.create_canvas("A:u1", "ws1", "s1", "旧看板", "回顾")
    finally:
        blueprint.close()
    expected = {
        "sites": 1,
        "site_releases": 1,
        "site_annotations": 1,
        "inspiration_annotations": 2,
        "site_canvases": 1,
    }
    return SimpleNamespace(site_id=site["id"], expected=expected)


def test_load_config_reads_and_normalizes_sites_db_path(tmp_path, monkeypatch):
    """runtime.sites_db_path 与 db_path 同法：可配置，相对路径归一到 crew_home。"""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "runtime:\n  sites_db_path: custom/sites.db\n", encoding="utf-8"
    )
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.sites_db_path == str(tmp_path / "home" / "custom" / "sites.db")


def test_load_config_three_small_domain_db_paths_default_next_to_main_db(tmp_path, monkeypatch):
    """收尾批三库缺省与 cron/work/kanban 同层：crew_data/<feature>.db。"""

    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))

    cfg = load_config()

    data_dir = tmp_path / "home" / "crew_data"
    assert cfg.sites_db_path == str(data_dir / "sites.db")
    assert cfg.tasks_db_path == str(data_dir / "tasks.db")
    assert cfg.notifications_db_path == str(data_dir / "notifications.db")


def test_fresh_install_sites_tables_live_only_in_sites_db(tmp_path):
    """验收 1：新装环境 sites 10 表 + 版本表只出现在 sites.db，crew.db 无 sites 表。"""

    main_db = tmp_path / "crew.db"
    sites_db = tmp_path / "sites.db"
    crew = build_app(
        config=Config(
            db_path=str(main_db), sites_db_path=str(sites_db), cron_enabled=False
        ),
        enable_team=False,
    )
    crew.sites.store.close()
    crew.sites.blueprint.store.close()

    main_tables = _table_names(main_db)
    sites_tables = _table_names(sites_db)
    # 主库确实是 core 状态库（确认看的是对的文件）
    assert "sessions" in main_tables
    expected = set(SITES_DB_TABLES) | {f"{SITES_SCHEMA_FEATURE}_schema_version"}
    assert expected <= sites_tables
    assert main_tables.isdisjoint(expected)
    with closing(sqlite3.connect(sites_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {SITES_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    assert version == SITES_SCHEMA_VERSION


def test_build_app_migrates_legacy_sites_rows(tmp_path):
    """验收 2 走真实装配线：build_app 经 Manager 工厂传入 legacy 路径触发
    copy-on-first-activate，store 与 blueprint 两半各自复制。"""

    main_db = tmp_path / "crew.db"
    sites_db = tmp_path / "sites.db"
    seed = _seed_legacy_sites_rows(main_db)

    crew = build_app(
        config=Config(
            db_path=str(main_db), sites_db_path=str(sites_db), cron_enabled=False
        ),
        enable_team=False,
    )
    assert crew.sites.store.get_site("A:u1", seed.site_id)["name"] == "旧站点"
    crew.sites.store.close()
    crew.sites.blueprint.store.close()

    for table, count in seed.expected.items():
        assert _count(sites_db, table) == count, table
        assert _count(main_db, table) == count, table  # 旧行保留（回退备份）
    assert _count(sites_db, "site_canvases") == 1


def test_legacy_sites_rows_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 激活后 sites.db 两半行数一致、旧行保留、重复激活零重复。"""

    legacy_db = tmp_path / "crew.db"
    sites_db = tmp_path / "sites.db"
    seed = _seed_legacy_sites_rows(legacy_db)

    store = SQLiteSiteStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    blueprint = BlueprintStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert store.get_site("A:u1", seed.site_id)["name"] == "旧站点"
        assert len(blueprint.list_canvases("A:u1")) == 1
    finally:
        blueprint.close()
        store.close()

    for table, count in seed.expected.items():
        assert _count(sites_db, table) == count, table
        assert _count(legacy_db, table) == count, table  # 旧行保留（回退备份）

    # 重复激活幂等零重复
    again = SQLiteSiteStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    blueprint_again = BlueprintStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert again.get_site("A:u1", seed.site_id) is not None
        assert len(blueprint_again.list_canvases("A:u1")) == 1
    finally:
        blueprint_again.close()
        again.close()
    for table, count in seed.expected.items():
        assert _count(sites_db, table) == count, table


def test_sites_copy_skipped_when_target_has_rows(tmp_path):
    """目标库任一半域已有行即该半域整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    sites_db = tmp_path / "sites.db"
    _seed_legacy_sites_rows(legacy_db)

    first = SQLiteSiteStore(str(sites_db), wal_enabled=False)
    try:
        first.upsert_site(
            owner="B:u2", workspace_id="ws2", session_id="s2", name="新装站点",
            source_path="src", build_command="", output_directory="dist",
        )
    finally:
        first.close()

    store = SQLiteSiteStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert [s["name"] for s in store.list_sites("B:u2")] == ["新装站点"]
        assert _count(sites_db, "sites") == 1
        assert _count(sites_db, "inspiration_annotations") == 0
    finally:
        store.close()
    # 旧库未被触碰
    assert _count(legacy_db, "sites") == 1

    # blueprint 半域独立 gate：目标为空则照常复制
    blueprint = BlueprintStore(
        str(sites_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert len(blueprint.list_canvases("A:u1")) == 1
    finally:
        blueprint.close()
    assert _count(sites_db, "site_canvases") == 1


def test_legacy_owner_scan_targets_route_sites_tables(tmp_path):
    """claim-legacy 扫描结构：sites 表不在 OWNER_TABLE_LABELS，按 external/team
    显式语义登记——显式传参才有条目、缺省不进任何条目、回退配置并入同一文件。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    work_db = tmp_path / "work.db"
    sites_db = tmp_path / "sites.db"

    # 生产形状（不传 sites 路径）：sites 表不登记进任何条目
    default_targets = legacy_owner_scan_targets(main_db, cron_db, work_db)
    assert all(
        set(tables).isdisjoint(SITES_DB_TABLES)
        for tables in default_targets.values()
    )

    targets = legacy_owner_scan_targets(
        main_db, cron_db, work_db, sites_db_path=sites_db
    )
    assert set(targets) == {main_db, cron_db, work_db, sites_db}
    assert set(targets[sites_db]) == set(SITES_DB_TABLES)
    assert set(targets[main_db]).isdisjoint(SITES_DB_TABLES)

    # 回退配置（sites 库指回主库同一文件）：按回退语义并入该条目
    db = tmp_path / "fallback.db"
    merged = legacy_owner_scan_targets(db, sites_db_path=db)
    assert set(merged) == {db}
    assert set(merged[db]) >= set(SITES_DB_TABLES)


def test_fresh_install_runtime_tasks_live_only_in_tasks_db(tmp_path):
    """验收 1：新装环境 runtime_tasks + 版本表只出现在 tasks.db，crew.db 无该表。"""

    main_db = tmp_path / "crew.db"
    tasks_db = tmp_path / "tasks.db"
    crew = build_app(
        config=Config(
            db_path=str(main_db), tasks_db_path=str(tasks_db), cron_enabled=False
        ),
        enable_team=False,
    )
    crew.tasks.close()

    main_tables = _table_names(main_db)
    tasks_tables = _table_names(tasks_db)
    assert "sessions" in main_tables
    expected = set(TASKS_DB_TABLES) | {f"{TASKS_SCHEMA_FEATURE}_schema_version"}
    assert expected <= tasks_tables
    assert main_tables.isdisjoint(expected)
    with closing(sqlite3.connect(tasks_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {TASKS_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    assert version == TASKS_SCHEMA_VERSION


def test_legacy_runtime_tasks_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 激活后 tasks.db 行数一致、旧行保留、重复激活零重复。"""

    legacy_db = tmp_path / "crew.db"
    tasks_db = tmp_path / "tasks.db"
    legacy = TaskRuntime(str(legacy_db))
    try:
        seeded = legacy.create_runtime(
            kind="team", session_id="s-legacy", title="旧任务",
            owner_account_id="A:u1",
        )
    finally:
        legacy.close()

    store = TaskRuntime(str(tasks_db), wal_enabled=False, legacy_db_path=str(legacy_db))
    try:
        assert store.get(seeded["task_id"], owner_account_id="A:u1")["title"] == "旧任务"
    finally:
        store.close()
    assert _count(legacy_db, "runtime_tasks") == 1  # 旧行保留（回退备份）
    assert _count(tasks_db, "runtime_tasks") == 1

    again = TaskRuntime(str(tasks_db), wal_enabled=False, legacy_db_path=str(legacy_db))
    try:
        assert len(again.list_tasks(owner_account_id="A:u1")) == 1
    finally:
        again.close()
    assert _count(tasks_db, "runtime_tasks") == 1


def test_tasks_copy_skipped_when_target_has_rows(tmp_path):
    """目标库已有行即整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    tasks_db = tmp_path / "tasks.db"
    legacy = TaskRuntime(str(legacy_db))
    try:
        legacy.create_runtime(
            kind="team", session_id="s-legacy", title="旧任务",
            owner_account_id="A:u1",
        )
    finally:
        legacy.close()

    first = TaskRuntime(str(tasks_db), wal_enabled=False)
    try:
        fresh = first.create_runtime(
            kind="team", session_id="s-fresh", title="新装任务",
            owner_account_id="B:u2",
        )
    finally:
        first.close()

    store = TaskRuntime(str(tasks_db), wal_enabled=False, legacy_db_path=str(legacy_db))
    try:
        assert [
            t["task_id"] for t in store.list_tasks(owner_account_id="B:u2")
        ] == [fresh["task_id"]]
    finally:
        store.close()
    assert _count(tasks_db, "runtime_tasks") == 1
    # 旧库未被触碰
    assert _count(legacy_db, "runtime_tasks") == 1


def test_fresh_install_notifications_live_only_in_notifications_db(tmp_path):
    """验收 1：新装环境 notifications 表 + 版本表只在 notifications.db。"""

    main_db = tmp_path / "crew.db"
    notifications_db = tmp_path / "notifications.db"
    build_app(
        config=Config(
            db_path=str(main_db),
            notifications_db_path=str(notifications_db),
            cron_enabled=False,
        ),
        enable_team=False,
    )

    main_tables = _table_names(main_db)
    notification_tables = _table_names(notifications_db)
    assert "sessions" in main_tables
    expected = set(NOTIFICATIONS_DB_TABLES) | {
        f"{NOTIFICATIONS_SCHEMA_FEATURE}_schema_version"
    }
    assert expected <= notification_tables
    assert main_tables.isdisjoint(expected)
    with closing(sqlite3.connect(notifications_db)) as conn:
        version = conn.execute(
            f"SELECT version FROM {NOTIFICATIONS_SCHEMA_FEATURE}_schema_version "
            "WHERE singleton = 1"
        ).fetchone()[0]
    assert version == NOTIFICATIONS_SCHEMA_VERSION


def test_legacy_notifications_copied_on_first_activate_and_idempotent(tmp_path):
    """验收 2：存量 crew.db 激活后 notifications.db 行数一致、旧行保留、幂等。"""

    legacy_db = tmp_path / "crew.db"
    notifications_db = tmp_path / "notifications.db"
    legacy = NotificationStore(str(legacy_db))
    try:
        legacy.insert(
            Notification(
                owner_account_id="A:u1", source="test", kind="info", title="旧通知"
            )
        )
    finally:
        legacy.close()

    store = NotificationStore(
        str(notifications_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert [n.title for n in store.list("A:u1")] == ["旧通知"]
    finally:
        store.close()
    assert _count(legacy_db, "notifications") == 1  # 旧行保留（回退备份）
    assert _count(notifications_db, "notifications") == 1

    again = NotificationStore(
        str(notifications_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert len(again.list("A:u1")) == 1
    finally:
        again.close()
    assert _count(notifications_db, "notifications") == 1


def test_notifications_copy_skipped_when_target_has_rows(tmp_path):
    """目标库已有行即整体跳过：绝不覆盖/合并进已存在的目标数据。"""

    legacy_db = tmp_path / "crew.db"
    notifications_db = tmp_path / "notifications.db"
    legacy = NotificationStore(str(legacy_db))
    try:
        legacy.insert(
            Notification(
                owner_account_id="A:u1", source="test", kind="info", title="旧通知"
            )
        )
    finally:
        legacy.close()

    first = NotificationStore(str(notifications_db), wal_enabled=False)
    try:
        first.insert(
            Notification(
                owner_account_id="B:u2", source="test", kind="info", title="新装通知"
            )
        )
    finally:
        first.close()

    store = NotificationStore(
        str(notifications_db), wal_enabled=False, legacy_db_path=str(legacy_db)
    )
    try:
        assert [n.title for n in store.list("B:u2")] == ["新装通知"]
        assert store.list("A:u1") == []
    finally:
        store.close()
    assert _count(notifications_db, "notifications") == 1
    # 旧库未被触碰
    assert _count(legacy_db, "notifications") == 1


def test_legacy_owner_scan_targets_route_tasks_and_notifications_tables(tmp_path):
    """claim-legacy 扫描结构：tasks/notifications 两域在 OWNER_TABLE_LABELS，
    照 cron/work 语义接入——缺省并入主库条目（回退），显式传参得独立条目。"""

    main_db = tmp_path / "crew.db"
    cron_db = tmp_path / "cron.db"
    work_db = tmp_path / "work.db"
    tasks_db = tmp_path / "tasks.db"
    notifications_db = tmp_path / "notifications.db"

    targets = legacy_owner_scan_targets(
        main_db,
        cron_db,
        work_db,
        tasks_db_path=tasks_db,
        notifications_db_path=notifications_db,
    )
    assert set(targets) == {
        main_db,
        cron_db,
        work_db,
        tasks_db,
        notifications_db,
    }
    assert set(targets[tasks_db]) == set(TASKS_DB_TABLES)
    assert set(targets[notifications_db]) == set(NOTIFICATIONS_DB_TABLES)
    # 主库条目只剩 core 表（tasks/notifications 已从主库清单摘除）
    assert set(targets[main_db]).isdisjoint(
        set(TASKS_DB_TABLES) | set(NOTIFICATIONS_DB_TABLES)
    )

    # 回退配置（两库指回主库同一文件）：按回退语义并入该条目
    db = tmp_path / "fallback.db"
    merged = legacy_owner_scan_targets(
        db, tasks_db_path=db, notifications_db_path=db
    )
    assert set(merged) == {db}
    assert set(merged[db]) >= {
        "sessions",
        "cron_jobs",
        *TASKS_DB_TABLES,
        *NOTIFICATIONS_DB_TABLES,
    }


def test_claim_legacy_spans_main_tasks_and_notifications_databases(tmp_path):
    """验收 4：claim-legacy 按表归属跨库扫描，三库无主行各自认领。"""

    main_db = tmp_path / "crew.db"
    tasks_db = tmp_path / "tasks.db"
    notifications_db = tmp_path / "notifications.db"

    sessions = SQLiteSessionStore(str(main_db))
    sessions.save("s-legacy", [Message.user("hi")], owner_account_id="")
    sessions.close()
    tasks = TaskRuntime(str(tasks_db))
    try:
        tasks.create_runtime(
            kind="team", session_id="s-legacy", title="旧任务", owner_account_id=""
        )
    finally:
        tasks.close()
    notifications = NotificationStore(str(notifications_db))
    try:
        notifications.insert(
            Notification(owner_account_id="", source="test", kind="info", title="旧通知")
        )
    finally:
        notifications.close()

    targets = legacy_owner_scan_targets(
        main_db, tasks_db_path=tasks_db, notifications_db_path=notifications_db
    )
    changed, remaining = claim_legacy_owner_databases(targets, "A:uid-1")
    assert changed["sessions"] == 1
    assert changed["runtime_tasks"] == 1
    assert changed["notifications"] == 1
    assert remaining["runtime_tasks"] == 0
    assert remaining["notifications"] == 0

    with closing(sqlite3.connect(tasks_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_tasks WHERE owner_account_id = 'A:uid-1'"
        ).fetchone()[0] == 1
    with closing(sqlite3.connect(notifications_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE owner_account_id = 'A:uid-1'"
        ).fetchone()[0] == 1


def test_migrate_small_domain_features_stamp_own_dbs(tmp_path):
    """`migrate feature sites/tasks/notifications` 的版本表各自 stamp 到自己的库。"""

    from crew.cli.management import (
        _migrate_notifications,
        _migrate_sites,
        _migrate_tasks,
    )

    app = SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            sites_db_path=str(tmp_path / "sites.db"),
            tasks_db_path=str(tmp_path / "tasks.db"),
            notifications_db_path=str(tmp_path / "notifications.db"),
            sqlite_wal=False,
        )
    )

    sites_report = _migrate_sites(app)
    assert sites_report.feature == SITES_SCHEMA_FEATURE
    assert sites_report.current_version == SITES_SCHEMA_VERSION

    tasks_report = _migrate_tasks(app)
    assert tasks_report.feature == TASKS_SCHEMA_FEATURE
    assert tasks_report.current_version == TASKS_SCHEMA_VERSION

    notifications_report = _migrate_notifications(app)
    assert notifications_report.feature == NOTIFICATIONS_SCHEMA_FEATURE
    assert notifications_report.current_version == NOTIFICATIONS_SCHEMA_VERSION

    assert (tmp_path / "sites.db").exists()
    assert (tmp_path / "tasks.db").exists()
    assert (tmp_path / "notifications.db").exists()
    assert not (tmp_path / "crew.db").exists()
    for db_name, feature, version in (
        ("sites.db", SITES_SCHEMA_FEATURE, SITES_SCHEMA_VERSION),
        ("tasks.db", TASKS_SCHEMA_FEATURE, TASKS_SCHEMA_VERSION),
        ("notifications.db", NOTIFICATIONS_SCHEMA_FEATURE, NOTIFICATIONS_SCHEMA_VERSION),
    ):
        with closing(sqlite3.connect(tmp_path / db_name)) as conn:
            assert (
                conn.execute(
                    f"SELECT version FROM {feature}_schema_version WHERE singleton = 1"
                ).fetchone()[0]
                == version
            )


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__])
