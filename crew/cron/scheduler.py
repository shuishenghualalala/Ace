"""间隔调度器 + APScheduler 驱动的 cron 服务。"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crew.core.envelope import Envelope
from crew.core.interfaces import Scheduler
from crew.core.runctx import current_owner_account_id
from crew.cron.jobs import BJ_TZ, CronJobStore, build_trigger
from crew.state.logging import get_logger

log = get_logger("cron")


@dataclass
class _Job:
    name: str
    interval: float
    callback: Callable[[], Awaitable[None]]
    task: asyncio.Task | None = None


class IntervalScheduler(Scheduler):
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._running = False

    def add_job(self, name: str, interval_seconds: float, callback: Callable[[], Awaitable[None]]) -> str:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = _Job(name=name, interval=interval_seconds, callback=callback)
        log.info("注册定时任务: %s (每 %ss)", name, interval_seconds)
        if self._running:
            self._spawn(job_id)
        return job_id

    def _spawn(self, job_id: str) -> None:
        job = self._jobs[job_id]

        async def loop() -> None:
            while self._running:
                await asyncio.sleep(job.interval)
                if not self._running:
                    break
                try:
                    await job.callback()
                except Exception:  # noqa: BLE001
                    log.exception("定时任务 %s 执行失败", job.name)

        job.task = asyncio.create_task(loop())

    async def start(self) -> None:
        self._running = True
        for job_id in self._jobs:
            self._spawn(job_id)
        log.info("调度器已启动，%d 个任务", len(self._jobs))

    async def stop(self) -> None:
        self._running = False
        for job in self._jobs.values():
            if job.task:
                job.task.cancel()
        log.info("调度器已停止")


# 执行一个到期任务：把 Envelope 跑完（结果自然落该会话历史）
JobRunner = Callable[[Envelope], Awaitable[None]]


class CronService:
    """定时任务引擎：把持久化任务注册给 APScheduler 并执行。"""

    def __init__(
        self,
        store: CronJobStore,
        runner: JobRunner,
        *,
        max_parallel_jobs: int = 2,
    ) -> None:
        self._store = store
        self._runner = runner
        self._max_parallel_jobs = max(1, int(max_parallel_jobs or 1))
        self._scheduler: AsyncIOScheduler | None = None
        self._running = False
        self._start_error = ""
        self._mounted_owner = ""
        self._mounted_job_ids: set[str] = set()
        self._owner_tasks: dict[str, set[asyncio.Task]] = {}
        self._cancel_terminal_status: dict[asyncio.Task, str] = {}
        self._sem = asyncio.Semaphore(self._max_parallel_jobs)

    @property
    def scheduler(self) -> AsyncIOScheduler | None:
        return self._scheduler

    @property
    def mounted_owner(self) -> str:
        """Return the only Owner whose Jobs may currently execute."""

        return self._mounted_owner

    def _ensure_scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=BJ_TZ)
        return self._scheduler

    def _remove_job(self, job_id: str) -> None:
        if self._scheduler is None:
            return
        with suppress(JobLookupError):
            self._scheduler.remove_job(job_id)

    def _sync_next_run_from_scheduler(self, job_id: str) -> None:
        if self._scheduler is None:
            return
        job = self._scheduler.get_job(job_id)
        if job is None:
            self._store.update_next_run(job_id, None, enabled=False)
            return
        next_run = job.next_run_time.timestamp() if job.next_run_time is not None else None
        self._store.update_next_run(job_id, next_run, enabled=next_run is not None)

    def sync_job(self, job_id: str) -> None:
        owner = self._mounted_owner
        if not owner:
            return
        row = self._store.get(job_id, owner_account_id=owner)
        if row is None:
            self._remove_job(job_id)
            self._mounted_job_ids.discard(job_id)
            return
        self._sync_job_row(row)

    def _sync_job_row(self, row: dict, *, prepared: bool = False) -> None:
        """Install one already-authorized Job row into APScheduler."""
        owner = self._mounted_owner
        if not owner or str(row.get("owner_account_id") or "") != owner:
            return
        job_id = str(row["id"])
        if not row.get("enabled"):
            self._remove_job(job_id)
            self._mounted_job_ids.discard(job_id)
            return
        if self._scheduler is None:
            return
        trigger = build_trigger(row["trigger_type"], row["trigger_payload"])
        next_run_options = {}
        if prepared and row.get("next_run_at"):
            next_run_options["next_run_time"] = datetime.fromtimestamp(
                float(row["next_run_at"]),
                tz=BJ_TZ,
            )
        self._scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            args=[job_id, owner],
            **next_run_options,
        )
        self._mounted_job_ids.add(job_id)
        if not prepared:
            self._sync_next_run_from_scheduler(job_id)

    def _track_current_task(self, owner_account_id: str) -> asyncio.Task | None:
        task = asyncio.current_task()
        if task is not None:
            self._owner_tasks.setdefault(owner_account_id, set()).add(task)
        return task

    def _untrack_task(self, owner_account_id: str, task: asyncio.Task | None) -> None:
        if task is None:
            return
        tasks = self._owner_tasks.get(owner_account_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._owner_tasks.pop(owner_account_id, None)

    async def _execute_job(self, job_id: str, owner_account_id: str) -> bool:
        task = self._track_current_task(owner_account_id)
        try:
            if self._mounted_owner != owner_account_id:
                return False
            claim = self._store.claim_scheduled_fire(
                job_id,
                owner_account_id=owner_account_id,
            )
            if claim is None:
                return False
            await self._execute_claimed_fire(claim)
            return True
        finally:
            self._untrack_task(owner_account_id, task)

    async def run_now(self, job_id: str, *, owner_account_id: str) -> dict | None:
        """Create and execute one manual Fire without mutating the Job schedule."""

        owner = str(owner_account_id or "").strip()
        task = self._track_current_task(owner)
        try:
            if not owner or self._mounted_owner != owner:
                return None
            claim = self._store.claim_manual_fire(
                job_id,
                owner_account_id=owner,
            )
            if claim is None:
                return None
            await self._execute_claimed_fire(claim)
            return claim
        finally:
            self._untrack_task(owner, task)

    async def retry_fire(
        self,
        source_fire_id: int,
        *,
        owner_account_id: str,
    ) -> dict | None:
        """Create and execute one linked retry Fire for the Active Owner."""

        owner = str(owner_account_id or "").strip()
        task = self._track_current_task(owner)
        try:
            if not owner or self._mounted_owner != owner:
                return None
            claim = self._store.claim_retry_fire(
                source_fire_id,
                owner_account_id=owner,
            )
            if claim is None:
                return None
            await self._execute_claimed_fire(claim)
            return claim
        finally:
            self._untrack_task(owner, task)

    async def _execute_claimed_fire(self, claim: dict) -> None:
        """Execute a claimed Fire under its persisted causal Owner context."""
        owner = str(claim.get("job", {}).get("owner_account_id") or "").strip()
        token = current_owner_account_id.set(owner)
        try:
            await self._run_claimed_fire(claim)
        finally:
            current_owner_account_id.reset(token)

    async def _run_claimed_fire(self, claim: dict) -> None:
        row = claim["job"]
        job_id = str(row["id"])
        run_id = int(claim["id"])
        async with self._sem:
            error_message = ""
            try:
                env = Envelope.of(
                    row["query"],
                    session_id=row["session_id"],
                    workspace_id=row.get("workspace_id", "default"),
                    channel="cron",
                    user_id=str(row.get("owner_account_id") or ""),
                )
                env.params["cron_job_id"] = str(row.get("id") or job_id)
                env.params["cron_job_name"] = str(row.get("name") or "")
                env.params["cron_source_session_id"] = str(row.get("session_id") or "")
                env.params["cron_fire_id"] = run_id
                env.params["cron_fire_kind"] = str(claim.get("fire_kind") or "")
                env.params["cron_scheduled_for"] = float(claim.get("scheduled_for") or 0)
                if claim.get("retry_of_fire_id") is not None:
                    env.params["cron_retry_of_fire_id"] = int(claim["retry_of_fire_id"])
                deliver_target = str(row.get("deliver") or "").strip()
                if deliver_target:
                    env.params["cron_deliver"] = deliver_target
                origin_source = row.get("origin_source") or {}
                if isinstance(origin_source, dict) and origin_source:
                    env.params["cron_origin_source"] = origin_source
                await self._runner(env)
                status = "completed"
            except asyncio.CancelledError:
                current = asyncio.current_task()
                status = self._cancel_terminal_status.get(current, "abandoned")
                error_message = (
                    "owner_logged_out"
                    if status == "cancelled_by_logout"
                    else "execution_cancelled_with_unknown_side_effects"
                )
                raise
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                error_message = str(exc)
                log.exception("定时任务 %s 执行失败", job_id)
            finally:
                if self._store.finish_job_run(
                    run_id,
                    status,
                    error_message=error_message,
                ):
                    self._store.mark_fire_finished(job_id, run_id, status)

    @property
    def is_running(self) -> bool:
        """Return whether the APScheduler engine has started."""

        return self._running

    @property
    def start_error(self) -> str:
        """Return the latest startup failure for health reporting."""

        return self._start_error

    async def tick(self) -> int:
        """Test/compatibility hook that runs due Jobs for the mounted Owner."""

        owner = self._mounted_owner
        if not owner:
            return 0
        due = self._store.get_due(owner_account_id=owner)
        if not due:
            return 0
        tasks = [
            asyncio.create_task(self._execute_job(job["id"], owner))
            for job in due
        ]
        try:
            claimed = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return sum(1 for won in claimed if won)

    async def start(self) -> None:
        """Start the empty scheduler and record a reusable health failure on error."""

        if self._running:
            return
        try:
            scheduler = self._ensure_scheduler()
            recovered = self._store.recover_running_fires_as_abandoned()
            scheduler.start()
        except Exception as exc:
            self._start_error = str(exc) or type(exc).__name__
            if self._scheduler is not None:
                with suppress(Exception):
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
            raise
        self._start_error = ""
        self._running = True
        log.info("CronService 引擎已启动，等待 Active Owner，abandoned=%d", recovered)

    def mount_owner(self, owner_account_id: str) -> int:
        """Mount only one Active Owner and skip every offline occurrence."""

        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 必填")
        if not self._running or self._scheduler is None:
            raise RuntimeError("CronService 尚未启动")
        if self._mounted_owner == owner:
            return len(self._mounted_job_ids)
        if self._mounted_owner:
            raise RuntimeError("CronService 已挂载其他 Active Owner")

        jobs = self._store.prepare_owner_jobs_for_mount(owner)
        self._mounted_owner = owner
        try:
            for job in jobs:
                self._sync_job_row(job, prepared=True)
        except Exception:
            for job_id in list(self._mounted_job_ids):
                self._remove_job(job_id)
            self._mounted_job_ids.clear()
            self._mounted_owner = ""
            raise
        log.info("CronService 已挂载 Active Owner: owner=%s jobs=%d", owner, len(jobs))
        return len(jobs)

    async def unmount_owner(
        self,
        owner_account_id: str,
        *,
        terminal_status: str = "cancelled_by_logout",
    ) -> int:
        """Fence one Owner and cancel its running Fire tasks with an explicit reason."""

        owner = str(owner_account_id or "").strip()
        if terminal_status not in {"cancelled_by_logout", "abandoned"}:
            raise ValueError(f"非法 Cron 取消终态: {terminal_status}")
        if not owner or self._mounted_owner != owner:
            return 0
        self._mounted_owner = ""
        for job_id in list(self._mounted_job_ids):
            self._remove_job(job_id)
        self._mounted_job_ids.clear()

        current = asyncio.current_task()
        tasks = [
            task
            for task in self._owner_tasks.get(owner, set())
            if task is not current and not task.done()
        ]
        for task in tasks:
            self._cancel_terminal_status[task] = terminal_status
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._cancel_terminal_status.pop(task, None)
        log.info("CronService 已撤下 Active Owner: owner=%s cancelled=%d", owner, len(tasks))
        return len(tasks)

    async def stop(self) -> None:
        if self._mounted_owner:
            await self.unmount_owner(self._mounted_owner, terminal_status="abandoned")
        self._running = False
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        log.info("CronService 已停止")
