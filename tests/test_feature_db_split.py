"""ADR-0038 Feature 拆库契约测试（cron 试点 + work 第二批）。

覆盖拆库的三类契约：
1. 路径与装配：Config.<feature>_db_path 读取/归一；新装 Feature 表只出现在
   各自独立库；
2. copy-on-first-activate：存量复制、幂等零重复、目标非空/旧库缺表跳过、
   复制失败单事务整体回滚不半写；
3. 跨表工具适配：claim-legacy 与启动期 owner 巡检按库归属扫描；CLI migrate
   feature 的版本表 stamp 到各自独立库。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from crew.app import build_app
from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.core.types import Message
from crew.cron import CronJobStore
from crew.cron.jobs import CRON_SCHEMA_FEATURE, CRON_SCHEMA_VERSION
from crew.state._migration import (
    CRON_DB_TABLES,
    WORK_DB_TABLES,
    claim_legacy_owner_databases,
    inspect_and_backfill_legacy_owners,
    legacy_owner_scan_targets,
)
from crew.state.config import Config, load_config
from crew.state.schema_version import copy_legacy_feature_rows
from crew.state.session_store import SQLiteSessionStore
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
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


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__])
