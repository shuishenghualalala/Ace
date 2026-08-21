"""计划任务调度 + cron 引擎（持久化任务 / schedule 解析 / agent 工具）。"""

import asyncio
import contextlib
import json
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from crew.core.types import ToolCall
from crew.core.runctx import current_owner_account_id, current_session_source
from crew.cron import CronJobStore, CronService, parse_schedule
from crew.cron.jobs import BJ_TZ, format_bj_timestamp, parse_duration
from crew.cron.scheduler import IntervalScheduler
from crew.tools.cron_tools import register_cron_tools
from crew.tools.registry import Registry

OWNER = "A:uid-a"


async def test_scheduler_fires_job():
    hits = []

    async def cb():
        hits.append(1)

    sch = IntervalScheduler()
    sch.add_job("tick", interval_seconds=0.05, callback=cb)
    await sch.start()
    await asyncio.sleep(0.17)
    await sch.stop()
    assert len(hits) >= 2


async def test_cron_fire_scopes_owner_context_and_resets_after_run(tmp_path):
    store = CronJobStore(str(tmp_path / "causal-owner.db"))
    seen: list[str] = []

    async def runner(env):
        seen.append(current_owner_account_id.get())

    service = CronService(store, runner)
    await service.start()
    service.mount_owner(OWNER)
    store.create(
        name="causal-owner",
        schedule="in 0s",
        query="run",
        session_id="s-causal",
        owner_account_id=OWNER,
    )

    assert current_owner_account_id.get() == ""
    await service.tick()

    assert seen == [OWNER]
    assert current_owner_account_id.get() == ""
    await service.stop()


# ---- schedule 解析 ----

def test_parse_duration_units():
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("10分钟") == 600
    assert parse_duration("2小时") == 7200


def test_parse_schedule_kinds():
    interval = parse_schedule("every 30m")
    assert interval["kind"] == "interval"
    assert interval["trigger_type"] == "interval"
    assert interval["interval_seconds"] == 1800

    once = parse_schedule("in 30s")
    assert once["kind"] == "once"
    assert once["trigger_type"] == "date"
    assert once["delay_seconds"] == 30

    zh_once = parse_schedule("10分钟后")
    assert zh_once["kind"] == "once"
    assert zh_once["trigger_type"] == "date"
    assert "run_at" in zh_once["trigger_payload"]
    assert zh_once["trigger_payload"]["run_at"].endswith("+08:00")

    daily = parse_schedule("每天9点")
    assert daily == {
        "kind": "cron",
        "trigger_type": "cron",
        "trigger_payload": {"hour": 9, "minute": 0},
    }

    weekly = parse_schedule("每周一8点")
    assert weekly == {
        "kind": "cron",
        "trigger_type": "cron",
        "trigger_payload": {"day_of_week": "mon", "hour": 8, "minute": 0},
    }


def test_parse_schedule_cron_expr_rejected():
    with pytest.raises(ValueError):
        parse_schedule("*/5 * * * *")


# ---- 任务存储 ----

def test_store_create_interval_and_compute_next_run(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(name="t", schedule="every 30m", query="hi", session_id="s1")
    assert job["kind"] == "interval"
    assert job["interval_seconds"] == 1800
    assert job["trigger_type"] == "interval"
    after = store.compute_next_run(job, after_current=True)
    assert after is not None and after > job["next_run_at"]


def test_store_supports_natural_language_schedule(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(name="daily", schedule="每天早上9点", query="go", session_id="s1")
    assert job["kind"] == "cron"
    assert job["trigger_type"] == "cron"
    assert job["trigger_payload"] == {"hour": 9, "minute": 0}


def test_format_bj_timestamp():
    ts = 1717243200  # 2024-06-01 12:00:00 UTC = 2024-06-01 20:00:00 CST
    assert format_bj_timestamp(ts) == "2024-06-01 20:00:00 CST"
    assert format_bj_timestamp(None) == ""


def test_store_list_and_delete(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    store.create(name="a", schedule="every 1m", query="x", session_id="s1")
    store.create(name="b", schedule="every 1m", query="y", session_id="s2")
    assert len(store.list()) == 2
    assert len(store.list(session_id="s1")) == 1
    jid = store.list(session_id="s2")[0]["id"]
    assert store.delete(jid) is True
    assert len(store.list()) == 1


def test_scheduled_fire_claim_has_one_database_winner(tmp_path):
    db_path = str(tmp_path / "c.db")
    store_a = CronJobStore(db_path)
    job = store_a.create(
        name="atomic",
        schedule="in 0s",
        query="once",
        session_id="s1",
        owner_account_id="A:uid-a",
    )
    store_b = CronJobStore(db_path)
    barrier = threading.Barrier(2)

    def claim(store: CronJobStore):
        barrier.wait()
        return store.claim_scheduled_fire(
            job["id"],
            owner_account_id=OWNER,
            now=job["next_run_at"] + 1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (store_a, store_b)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["fire_kind"] == "scheduled"
    runs = store_a.get_job_runs(job["id"])
    assert len(runs) == 1
    assert runs[0]["fire_key"].startswith("scheduled:")


def test_legacy_run_migration_backfills_fire_identity_idempotently(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE cron_jobs (
            id TEXT PRIMARY KEY,
            owner_account_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            interval_seconds REAL NOT NULL DEFAULT 0,
            query TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT 'default',
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at REAL NOT NULL,
            last_run_at REAL NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT '',
            deliver TEXT NOT NULL DEFAULT '',
            origin_source TEXT NOT NULL DEFAULT '{}',
            trigger_type TEXT NOT NULL DEFAULT '',
            trigger_payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE TABLE cron_job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL DEFAULT 'running',
            error_message TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        """
        INSERT INTO cron_jobs (
            id, owner_account_id, kind, next_run_at, created_at
        ) VALUES ('job-1', 'A:uid-a', 'once', 100, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO cron_job_runs (
            job_id, started_at, finished_at, status
        ) VALUES ('job-1', 10, 11, 'completed')
        """
    )
    conn.commit()
    conn.close()

    first = CronJobStore(str(db_path)).get_job_runs("job-1")
    second = CronJobStore(str(db_path)).get_job_runs("job-1")

    assert first == second
    assert first[0]["owner_account_id"] == "A:uid-a"
    assert first[0]["fire_kind"] == "legacy"
    assert first[0]["fire_key"] == "legacy:1"
    assert first[0]["scheduled_for"] == 10


def test_legacy_fire_terminal_statuses_are_normalized_conservatively(tmp_path):
    db_path = str(tmp_path / "legacy-status.db")
    store = CronJobStore(db_path)
    job = store.create(
        name="legacy-status",
        schedule="every 1h",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    failed = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    cancelled = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    assert failed is not None and cancelled is not None
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE cron_job_runs SET status = 'failed: old boom' WHERE id = ?",
        (failed["id"],),
    )
    conn.execute(
        "UPDATE cron_job_runs SET status = 'cancelled', error_message = 'cancelled' WHERE id = ?",
        (cancelled["id"],),
    )
    conn.commit()
    conn.close()

    migrated = CronJobStore(db_path).get_job_runs(job["id"])

    by_id = {run["id"]: run for run in migrated}
    assert by_id[failed["id"]]["status"] == "failed"
    assert by_id[failed["id"]]["error_message"] == "old boom"
    assert by_id[cancelled["id"]]["status"] == "abandoned"
    assert by_id[cancelled["id"]]["error_message"] == "legacy_cancel_reason_unknown"


def test_manual_fire_does_not_change_periodic_schedule(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(
        name="manual",
        schedule="every 1h",
        query="now",
        session_id="s1",
        owner_account_id="A:uid-a",
    )

    fire = store.claim_manual_fire(job["id"], owner_account_id="A:uid-a")

    assert fire is not None
    assert fire["fire_kind"] == "manual"
    refreshed = store.get(job["id"], owner_account_id="A:uid-a")
    assert refreshed["next_run_at"] == job["next_run_at"]
    assert refreshed["enabled"] == job["enabled"]
    assert store.finish_job_run(fire["id"], "completed") is True
    assert store.finish_job_run(fire["id"], "failed", "late") is False
    assert store.get_job_runs(job["id"])[0]["status"] == "completed"


def test_recover_running_fire_marks_abandoned_without_replay(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(
        name="crashed",
        schedule="in 0s",
        query="side effect",
        session_id="s1",
        owner_account_id=OWNER,
    )
    fire = store.claim_scheduled_fire(
        job["id"],
        owner_account_id=OWNER,
        now=job["next_run_at"] + 1,
    )
    assert fire is not None

    assert store.recover_running_fires_as_abandoned() == 1
    assert store.recover_running_fires_as_abandoned() == 0

    run = store.get_job_runs(job["id"])[0]
    assert run["status"] == "abandoned"
    assert run["error_message"] == "process_restarted_before_terminal_state"
    assert store.finish_job_run(fire["id"], "completed") is False
    assert store.get(job["id"], owner_account_id=OWNER)["enabled"] == 0
    assert store.get_due(owner_account_id=OWNER) == []


def test_retry_fire_is_linked_and_rejects_non_retryable_or_wrong_owner(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(
        name="retry",
        schedule="every 1h",
        query="retry me",
        session_id="s1",
        owner_account_id=OWNER,
    )
    source = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    assert source is not None
    assert store.finish_job_run(source["id"], "failed", "boom") is True

    retry = store.claim_retry_fire(source["id"], owner_account_id=OWNER)

    assert retry is not None
    assert retry["fire_kind"] == "retry"
    assert retry["retry_of_fire_id"] == source["id"]
    assert store.claim_retry_fire(retry["id"], owner_account_id=OWNER) is None
    assert store.claim_retry_fire(source["id"], owner_account_id="B:uid-b") is None


def test_older_fire_cannot_overwrite_newer_job_summary(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(
        name="summary-order",
        schedule="every 1h",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    older = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    newer = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    assert older is not None and newer is not None

    assert store.finish_job_run(newer["id"], "completed") is True
    store.mark_fire_finished(job["id"], newer["id"], "completed")
    assert store.finish_job_run(older["id"], "failed", "late") is True
    store.mark_fire_finished(job["id"], older["id"], "failed")

    assert store.get(job["id"], owner_account_id=OWNER)["last_status"] == "completed"


def test_late_interval_claim_advances_to_strictly_future_occurrence(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    job = store.create(
        name="late",
        schedule="every 1h",
        query="once",
        session_id="s1",
    )
    claim_time = job["next_run_at"] + 5 * 3600 + 1

    assert store.claim_scheduled_fire(
        job["id"],
        owner_account_id="",
        now=claim_time,
    ) is not None

    refreshed = store.get(job["id"])
    assert refreshed["next_run_at"] > claim_time


# ---- CronService ----

async def test_cron_service_tick_runs_due_job(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    calls = []

    async def runner(env):
        calls.append((env.session_id, env.query))

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    store.create(
        name="t",
        schedule="in 0s",
        query="do it",
        session_id="s1",
        owner_account_id=OWNER,
    )
    n = await svc.tick()
    await svc.stop()
    assert n == 1
    assert calls == [("s1", "do it")]
    assert store.get_due(owner_account_id=OWNER) == []
    assert store.list(owner_account_id=OWNER)[0]["enabled"] == 0


async def test_two_cron_services_execute_one_scheduled_fire(tmp_path):
    db_path = str(tmp_path / "c.db")
    store_a = CronJobStore(db_path)
    store_b = CronJobStore(db_path)
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(env):
        calls.append(env.query)
        started.set()
        await release.wait()

    service_a = CronService(store_a, runner)
    service_b = CronService(store_b, runner)
    await service_a.start()
    await service_b.start()
    service_a.mount_owner(OWNER)
    service_b.mount_owner(OWNER)
    store_a.create(
        name="t",
        schedule="in 0s",
        query="once",
        session_id="s1",
        owner_account_id=OWNER,
    )

    first = asyncio.create_task(service_a.tick())
    await started.wait()
    second = asyncio.create_task(service_b.tick())
    await asyncio.sleep(0)
    release.set()

    assert sum(await asyncio.gather(first, second)) == 1
    assert calls == ["once"]
    await service_a.stop()
    await service_b.stop()


async def test_cron_service_without_mounted_owner_does_not_run_due_job(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    calls = []

    async def runner(env):
        calls.append(env.query)

    svc = CronService(store, runner)
    store.create(
        name="t",
        schedule="in 0s",
        query="do it",
        session_id="s1",
        owner_account_id=OWNER,
    )

    assert await svc.tick() == 0
    assert calls == []
    await svc.start()
    try:
        assert await svc.tick() == 0
    finally:
        await svc.stop()
    assert calls == []


async def test_cron_service_passes_deliver_and_origin_source(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    seen = []
    origin = {"platform": "feishu", "chat_id": "u1", "chat_type": "dm", "user_id": "u1"}

    async def runner(env):
        seen.append(env)

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    store.create(
        name="t",
        schedule="in 0s",
        query="do it",
        session_id="s1",
        origin_source=origin,
        owner_account_id=OWNER,
    )
    await svc.tick()
    await svc.stop()

    assert seen[0].params["cron_deliver"] == "origin"
    assert seen[0].params["cron_origin_source"] == origin


async def test_cron_service_isolates_job_failure(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))

    async def runner(env):
        raise RuntimeError("boom")

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    job = store.create(
        name="t",
        schedule="in 0s",
        query="x",
        session_id="s1",
        owner_account_id=OWNER,
    )
    await svc.tick()  # 不抛出
    await svc.stop()
    assert store.get(job["id"], owner_account_id=OWNER)["last_status"].startswith("failed")


async def test_cron_service_respects_parallel_job_cap(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    active = 0
    peak = 0
    calls = []

    async def runner(env):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(env.session_id)
        await asyncio.sleep(0.03)
        active -= 1

    svc = CronService(store, runner, max_parallel_jobs=2)
    await svc.start()
    svc.mount_owner(OWNER)
    for i in range(3):
        store.create(
            name=f"t{i}",
            schedule="in 0s",
            query="x",
            session_id=f"s{i}",
            owner_account_id=OWNER,
        )

    n = await svc.tick()
    await svc.stop()
    assert n == 3
    assert sorted(calls) == ["s0", "s1", "s2"]
    assert peak == 2


async def test_cron_service_start_registers_enabled_jobs(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))

    async def runner(env):
        return None

    job = store.create(
        name="t",
        schedule="every 1m",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    svc = CronService(store, runner)
    await svc.start()
    try:
        svc.mount_owner(OWNER)
        scheduled = svc.scheduler.get_job(job["id"])
        assert scheduled is not None
        assert scheduled.next_run_time is not None
        assert store.get(job["id"], owner_account_id=OWNER)["next_run_at"] == pytest.approx(
            scheduled.next_run_time.timestamp()
        )
    finally:
        await svc.stop()


async def test_cron_service_reloads_active_owner_jobs_after_gateway_restart(tmp_path):
    database_path = str(tmp_path / "c.db")
    store = CronJobStore(database_path)

    async def runner(env):
        return None

    job = store.create(
        name="survives-restart",
        schedule="every 1h",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    first_service = CronService(store, runner)
    await first_service.start()
    first_service.mount_owner(OWNER)
    await first_service.stop()

    restarted_service = CronService(CronJobStore(database_path), runner)
    await restarted_service.start()
    try:
        restarted_service.mount_owner(OWNER)
        assert restarted_service.mounted_owner == OWNER
        assert [scheduled.id for scheduled in restarted_service.scheduler.get_jobs()] == [job["id"]]
    finally:
        await restarted_service.stop()


async def test_cron_service_releases_failed_scheduler_for_clean_shutdown(tmp_path, monkeypatch):
    store = CronJobStore(str(tmp_path / "c.db"))

    async def runner(env):
        return None

    service = CronService(store, runner)
    scheduler = service._ensure_scheduler()

    def fail_start() -> None:
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(scheduler, "start", fail_start)
    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        await service.start()

    assert service.start_error == "scheduler unavailable"
    assert service.scheduler is None
    await service.stop()


def test_prepare_owner_jobs_skips_offline_occurrences_without_creating_fire(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    recurring = store.create(
        name="recurring",
        schedule="every 1h",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    expired_once = store.create(
        name="once",
        schedule="in 1h",
        query="once",
        session_id="s1",
        owner_account_id=OWNER,
    )
    login_at = max(recurring["next_run_at"], expired_once["next_run_at"]) + 5 * 3600

    jobs = store.prepare_owner_jobs_for_mount(OWNER, now=login_at)

    assert [job["id"] for job in jobs] == [recurring["id"]]
    refreshed = store.get(recurring["id"], owner_account_id=OWNER)
    assert refreshed["next_run_at"] > login_at
    assert store.get(expired_once["id"], owner_account_id=OWNER)["enabled"] == 0
    assert store.get_job_runs(recurring["id"]) == []
    assert store.get_job_runs(expired_once["id"]) == []


async def test_cron_service_mounts_only_active_owner_jobs(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))

    async def runner(env):
        return None

    job_a = store.create(
        name="A",
        schedule="every 1h",
        query="a",
        session_id="same",
        owner_account_id=OWNER,
    )
    owner_b = "B:uid-b"
    job_b = store.create(
        name="B",
        schedule="every 1h",
        query="b",
        session_id="same",
        owner_account_id=owner_b,
    )
    service = CronService(store, runner)
    await service.start()
    assert service.scheduler.get_jobs() == []

    service.mount_owner(OWNER)
    assert service.mounted_owner == OWNER
    assert [job.id for job in service.scheduler.get_jobs()] == [job_a["id"]]

    await service.unmount_owner(OWNER)
    service.mount_owner(owner_b)
    assert service.mounted_owner == owner_b
    assert [job.id for job in service.scheduler.get_jobs()] == [job_b["id"]]
    await service.stop()


async def test_mount_owner_reuses_prepared_rows_without_reading_each_job(tmp_path, monkeypatch):
    store = CronJobStore(str(tmp_path / "c.db"))
    for index in range(3):
        store.create(
            name=f"job-{index}",
            schedule="every 1h",
            query="go",
            session_id="same",
            owner_account_id=OWNER,
        )
    reads: list[str] = []
    original_get = store.get

    def counted_get(job_id: str, *, owner_account_id: str | None = None):
        reads.append(job_id)
        return original_get(job_id, owner_account_id=owner_account_id)

    monkeypatch.setattr(store, "get", counted_get)

    async def runner(env):
        return None

    service = CronService(store, runner)
    await service.start()
    service.mount_owner(OWNER)

    assert reads == []
    assert len(service.scheduler.get_jobs()) == 3
    await service.stop()


async def test_unmount_owner_cancels_running_scheduled_fire(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    started = asyncio.Event()

    async def runner(env):
        started.set()
        await asyncio.Event().wait()

    service = CronService(store, runner)
    await service.start()
    service.mount_owner(OWNER)
    job = store.create(
        name="running",
        schedule="in 0s",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    tick_task = asyncio.create_task(service.tick())
    await started.wait()

    assert await service.unmount_owner(OWNER) == 1
    with pytest.raises(asyncio.CancelledError):
        await tick_task
    runs = store.get_job_runs(job["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "cancelled_by_logout"
    assert runs[0]["error_message"] == "owner_logged_out"
    await service.stop()


async def test_service_stop_marks_running_fire_abandoned(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    started = asyncio.Event()

    async def runner(env):
        started.set()
        await asyncio.Event().wait()

    service = CronService(store, runner)
    await service.start()
    service.mount_owner(OWNER)
    job = store.create(
        name="shutdown",
        schedule="in 0s",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    tick_task = asyncio.create_task(service.tick())
    await started.wait()

    await service.stop()
    with pytest.raises(asyncio.CancelledError):
        await tick_task

    run = store.get_job_runs(job["id"])[0]
    assert run["status"] == "abandoned"
    assert run["error_message"] == "execution_cancelled_with_unknown_side_effects"


async def test_service_retry_creates_new_linked_fire(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    seen = []

    async def runner(env):
        seen.append(env)

    job = store.create(
        name="retry-service",
        schedule="every 1h",
        query="go",
        session_id="s1",
        owner_account_id=OWNER,
    )
    source = store.claim_manual_fire(job["id"], owner_account_id=OWNER)
    assert source is not None
    assert store.finish_job_run(source["id"], "abandoned", "unknown") is True
    before = store.get(job["id"], owner_account_id=OWNER)["next_run_at"]
    service = CronService(store, runner)
    await service.start()
    service.mount_owner(OWNER)

    retry = await service.retry_fire(source["id"], owner_account_id=OWNER)

    assert retry is not None
    assert seen[0].params["cron_fire_kind"] == "retry"
    assert seen[0].params["cron_retry_of_fire_id"] == source["id"]
    runs = store.get_job_runs(job["id"])
    assert runs[0]["status"] == "completed"
    assert runs[0]["retry_of_fire_id"] == source["id"]
    assert runs[1]["status"] == "abandoned"
    assert store.get(job["id"], owner_account_id=OWNER)["next_run_at"] == before
    await service.stop()


# ---- agent 工具 ----

async def test_cron_tools_via_registry(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    reg = Registry()
    register_cron_tools(reg, store)

    create_res = await reg.execute(
        ToolCall("c1", "cron_create", {"name": "汇报", "schedule": "每天9点", "query": "总结", "session_id": "s1"})
    )
    assert not create_res.is_error
    create_payload = json.loads(create_res.content)
    job_id = create_payload["id"]
    assert create_payload["timezone"] == "Asia/Shanghai"
    assert create_payload["next_run_at_bj"].endswith("CST")

    list_res = await reg.execute(ToolCall("c2", "cron_list", {"session_id": "s1"}))
    list_payload = json.loads(list_res.content)
    assert list_payload["count"] == 1
    assert list_payload["timezone"] == "Asia/Shanghai"
    assert list_payload["jobs"][0]["next_run_at_bj"].endswith("CST")

    del_res = await reg.execute(ToolCall("c3", "cron_delete", {"id": job_id}))
    assert json.loads(del_res.content)["deleted"] is True
    assert store.list() == []


async def test_cron_list_defaults_to_all_user_jobs(tmp_path):
    """cron_list 不传 session_id 时应返回当前用户的全部任务，而不是仅当前会话。"""
    store = CronJobStore(str(tmp_path / "c.db"))
    reg = Registry()
    register_cron_tools(reg, store)

    await reg.execute(ToolCall("c1", "cron_create", {"name": "任务1", "schedule": "每天9点", "query": "总结", "session_id": "s1"}))
    await reg.execute(ToolCall("c2", "cron_create", {"name": "任务2", "schedule": "每天10点", "query": "汇报", "session_id": "s2"}))

    # 不传 session_id：应返回全部 2 个任务
    all_res = await reg.execute(ToolCall("c3", "cron_list", {}))
    all_payload = json.loads(all_res.content)
    assert all_payload["count"] == 2

    # 显式传 session_id：只返回该会话的任务
    filtered_res = await reg.execute(ToolCall("c4", "cron_list", {"session_id": "s1"}))
    filtered_payload = json.loads(filtered_res.content)
    assert filtered_payload["count"] == 1
    assert filtered_payload["jobs"][0]["name"] == "任务1"


async def test_cron_tools_sync_running_scheduler(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    reg = Registry()

    async def runner(env):
        return None

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    owner_token = current_owner_account_id.set(OWNER)
    try:
        register_cron_tools(reg, store, svc)
        create_res = await reg.execute(
            ToolCall("c1", "cron_create", {"name": "提醒", "schedule": "每10分钟", "query": "ping", "session_id": "s1"})
        )
        job_id = json.loads(create_res.content)["id"]
        assert svc.scheduler.get_job(job_id) is not None

        pause_res = await reg.execute(ToolCall("c2", "cron_pause", {"id": job_id}))
        assert json.loads(pause_res.content)["paused"] is True
        assert svc.scheduler.get_job(job_id) is None

        resume_res = await reg.execute(ToolCall("c3", "cron_resume", {"id": job_id}))
        resume_payload = json.loads(resume_res.content)
        assert resume_payload["resumed"] is True
        assert resume_payload["timezone"] == "Asia/Shanghai"
        assert resume_payload["next_run_at_bj"].endswith("CST")
        assert svc.scheduler.get_job(job_id) is not None

        del_res = await reg.execute(ToolCall("c4", "cron_delete", {"id": job_id}))
        assert json.loads(del_res.content)["deleted"] is True
        assert svc.scheduler.get_job(job_id) is None
    finally:
        current_owner_account_id.reset(owner_token)
        await svc.stop()


async def test_cron_service_passes_source_session_id(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))
    seen = []

    async def runner(env):
        seen.append(env)

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    store.create(
        name="t",
        schedule="in 0s",
        query="do it",
        session_id="s1",
        owner_account_id=OWNER,
    )
    await svc.tick()
    await svc.stop()

    assert len(seen) == 1
    assert seen[0].params["cron_source_session_id"] == "s1"


@contextlib.asynccontextmanager
async def _cron_runner_app(reply_text="done", auto_start=True):
    """cron runner 集成测试骨架：临时 DB + build_app + mock dispatch/notify_owner。

    产出 (app, dispatched_envs, notified_payloads)；退出时自动 shutdown。
    auto_start=False 时由用例自行决定 start/mount_owner 的时机。
    """
    from crew.app import build_app
    from crew.state.config import load_config

    # Windows 上 SQLite 连接在 TemporaryDirectory 清理时可能仍被占用
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = load_config()
        cfg.db_path = os.path.join(tmp, "crew.db")
        cfg.memory_db_path = os.path.join(tmp, "memory.db")
        cfg.cron_enabled = True
        cfg.gateway_admin_accounts = ["tester"]
        cfg.gateway_dev_mode = False
        app = build_app(cfg, enable_team=False)
        if auto_start:
            await app.cron_service.start()
            app.cron_service.mount_owner("u1")

        dispatched_envs = []
        notified_payloads = []

        async def _mock_dispatch(env):
            dispatched_envs.append(env)
            from crew.core.envelope import ResponseChunk

            yield ResponseChunk.final(env.request_id, reply_text)

        async def _mock_notify_owner(owner_account_id, payload):
            notified_payloads.append((owner_account_id, payload))

        # 替换 dispatch，避免真实调用 LLM；_cron_runner 内部会先创建新会话再 dispatch
        app.dispatch = _mock_dispatch
        app._notify_owner_fn = _mock_notify_owner

        try:
            yield app, dispatched_envs, notified_payloads
        finally:
            await app.shutdown()


async def test_cron_runner_creates_new_session():
    """集成测试：验证 _cron_runner 在 deliver 为空/new_session 时会创建新本地会话并广播事件。"""
    async with _cron_runner_app() as (app, dispatched_envs, notified_payloads):
        job = app.cron_store.create(
            name="测试提醒",
            schedule="in 0s",
            query="提醒我今天开会",
            session_id="source_session_123",
            workspace_id="default",
            owner_account_id="u1",
        )
        await app.cron_service.tick()

        assert len(dispatched_envs) == 1
        new_sid = dispatched_envs[0].session_id
        assert new_sid.startswith("cron_")
        assert dispatched_envs[0].params["cron_source_session_id"] == "source_session_123"
        # 验证新会话已被创建并属于同一 owner
        sessions = app.session_store.list_sessions(owner_account_id="u1")
        assert any(s["session_id"] == new_sid for s in sessions)
        # 验证通过 notify_owner 广播了 cron_session_created 事件（新会话尚未被订阅，不能按 session 推送）
        assert len(notified_payloads) == 1
        notify_owner, payload = notified_payloads[0]
        assert notify_owner == "u1"
        assert payload["kind"] == "cron_session_created"
        assert payload["session_id"] == new_sid
        assert payload["body"]["job_id"] == job["id"]
        assert payload["body"]["job_name"] == "测试提醒"
        assert payload["body"]["source_session_id"] == "source_session_123"


@pytest.mark.parametrize(
    ("platform", "session_id", "job_name"),
    [
        ("local", "local_session_456", "本地 origin 提醒"),
        ("web", "web_session_789", "web origin 提醒"),
    ],
    ids=["local", "web"],
)
async def test_cron_runner_origin_fallback_to_new_session(platform, session_id, job_name):
    """local/web 会话来源的 deliver=origin 不应静默成功，须 fallback 为新建会话。"""
    async with _cron_runner_app(reply_text="提醒完成") as (app, dispatched_envs, notified_payloads):
        app.cron_store.create(
            name=job_name,
            schedule="in 0s",
            query="提醒我开会",
            session_id=session_id,
            workspace_id="default",
            deliver="origin",
            origin_source={"platform": platform, "chat_id": session_id, "chat_type": "private"},
            owner_account_id="u1",
        )
        await app.cron_service.tick()

        assert len(dispatched_envs) == 1
        new_sid = dispatched_envs[0].session_id
        assert new_sid.startswith("cron_")
        # local/web 来源的 origin 被 fallback 为新建会话，须广播 cron_session_created
        assert len(notified_payloads) == 1
        assert notified_payloads[0][1]["kind"] == "cron_session_created"
        assert notified_payloads[0][1]["session_id"] == new_sid


async def test_cron_runner_local_deliver_notifies_owner():
    """deliver=local 时应保留原会话，并向 owner 广播 cron_session_updated 事件供前端标未读。"""
    async with _cron_runner_app(reply_text="提醒完成") as (app, dispatched_envs, notified_payloads):
        source_sid = "local_session_456"
        job = app.cron_store.create(
            name="本地会话提醒",
            schedule="in 0s",
            query="提醒我开会",
            session_id=source_sid,
            workspace_id="default",
            deliver="local",
            origin_source={"platform": "local", "chat_id": source_sid, "chat_type": "private"},
            owner_account_id="u1",
        )
        await app.cron_service.tick()

        assert len(dispatched_envs) == 1
        assert dispatched_envs[0].session_id == source_sid
        # local 投递须广播 cron_session_updated，让前端刷新会话列表并标记未读
        assert len(notified_payloads) == 1
        notify_owner, payload = notified_payloads[0]
        assert notify_owner == "u1"
        assert payload["kind"] == "cron_session_updated"
        assert payload["session_id"] == source_sid
        assert payload["body"]["job_id"] == job["id"]
        assert payload["body"]["job_name"] == "本地会话提醒"
        assert payload["body"]["source_session_id"] == source_sid


# ---- manual Fire run-now ----

async def test_cron_service_run_now_requires_mounted_owner(tmp_path):
    """Manual Fire cannot run before the Owner is mounted."""
    store = CronJobStore(str(tmp_path / "c.db"))
    calls = []

    async def runner(env):
        calls.append((env.session_id, env.query))

    svc = CronService(store, runner)
    job = store.create(
        name="t",
        schedule="every 1h",
        query="do it",
        session_id="s1",
        owner_account_id=OWNER,
    )

    assert await svc.run_now(job["id"], owner_account_id=OWNER) is None
    assert calls == []


async def test_cron_service_run_now_returns_none_for_missing_job(tmp_path):
    store = CronJobStore(str(tmp_path / "c.db"))

    async def runner(env):
        return None

    svc = CronService(store, runner)
    await svc.start()
    svc.mount_owner(OWNER)
    assert await svc.run_now("not_exists", owner_account_id=OWNER) is None
    await svc.stop()


async def test_cron_runner_reuses_delivery_session_across_fires():
    """同一任务多次触发复用固定投递会话（<job_id>_feed），不再每次刷同名新会话。"""
    async with _cron_runner_app(auto_start=False) as (app, dispatched_envs, notified_payloads):
        job = app.cron_store.create(
            name="测试提醒",
            schedule="every 1h",
            query="提醒我开会",
            session_id="source_session_123",
            workspace_id="default",
            owner_account_id="u1",
        )
        await app.cron_service.start()
        app.cron_service.mount_owner("u1")
        await app.cron_service.run_now(job["id"], owner_account_id="u1")
        await app.cron_service.run_now(job["id"], owner_account_id="u1")

        assert len(dispatched_envs) == 2
        assert dispatched_envs[0].session_id == f"{job['id']}_feed"
        assert dispatched_envs[1].session_id == f"{job['id']}_feed"
        kinds = [payload["kind"] for _, payload in notified_payloads]
        assert kinds == ["cron_session_created", "cron_session_updated"]
        # 侧栏只出现一个投递会话
        sessions = app.session_store.list_sessions(owner_account_id="u1")
        feed_sessions = [s for s in sessions if s["session_id"] == f"{job['id']}_feed"]
        assert len(feed_sessions) == 1
        assert feed_sessions[0]["title"] == "[定时] 测试提醒"


async def test_cron_create_origin_on_local_source_rewritten_with_note(tmp_path):
    """本地会话来源指定 deliver=origin 时，创建期就改写为 new_session 并附带说明。"""
    store = CronJobStore(str(tmp_path / "c.db"))
    reg = Registry()
    register_cron_tools(reg, store)

    token = current_session_source.set({"platform": "local", "chat_id": "s1", "chat_type": "private"})
    try:
        res = await reg.execute(
            ToolCall(
                "c1",
                "cron_create",
                {"name": "提醒", "schedule": "每天9点", "query": "总结", "session_id": "s1", "deliver": "origin"},
            )
        )
    finally:
        current_session_source.reset(token)

    assert not res.is_error
    payload = json.loads(res.content)
    assert payload["deliver"] == "new_session"
    assert "origin 不可用" in payload["note"]
    assert store.list()[0]["deliver"] == "new_session"


async def test_cron_service_run_now_creates_manual_fire_and_new_session():
    """Manual Fire uses the normal cron runner and broadcasts the new session."""
    async with _cron_runner_app(auto_start=False) as (app, dispatched_envs, notified_payloads):
        job = app.cron_store.create(
            name="测试提醒",
            schedule="every 1h",
            query="提醒我今天开会",
            session_id="source_session_123",
            workspace_id="default",
            owner_account_id="u1",
        )
        await app.cron_service.start()
        app.cron_service.mount_owner("u1")
        fire = await app.cron_service.run_now(job["id"], owner_account_id="u1")

        assert fire is not None
        assert fire["fire_kind"] == "manual"
        assert app.cron_store.get_job_runs(job["id"])[0]["status"] == "completed"
        assert len(dispatched_envs) == 1
        new_sid = dispatched_envs[0].session_id
        assert new_sid.startswith("cron_")
        assert len(notified_payloads) == 1
        assert notified_payloads[0][1]["kind"] == "cron_session_created"
        assert notified_payloads[0][1]["session_id"] == new_sid


# ---- 自然语言相对日期解析 ----

@pytest.mark.parametrize(
    ("text", "expected", "expect_once"),
    [
        # 基准 now = 2026-07-13 08:00（周一）
        ("明天9点", datetime(2026, 7, 14, 9, 0, 0), True),
        ("明天下午3点30分", datetime(2026, 7, 14, 15, 30, 0), False),
        ("后天8点", datetime(2026, 7, 15, 8, 0, 0), False),
        # 下周三是 7 月 15 日
        ("下周三9点", datetime(2026, 7, 15, 9, 0, 0), False),
        # 已过当天 8:00，应取下下周一（7 月 20 日）
        ("下周一9点", datetime(2026, 7, 20, 9, 0, 0), False),
    ],
    ids=["tomorrow", "tomorrow_afternoon", "day_after_tomorrow", "next_weekday", "same_day_rolls_to_next_week"],
)
def test_parse_schedule_relative_days(text, expected, expect_once):
    now = datetime(2026, 7, 13, 8, 0, 0, tzinfo=BJ_TZ)
    spec = parse_schedule(text, now=now)
    if expect_once:
        assert spec["kind"] == "once"
        assert spec["trigger_type"] == "date"
    run_at = datetime.fromisoformat(spec["trigger_payload"]["run_at"])
    assert run_at == expected.replace(tzinfo=BJ_TZ)
