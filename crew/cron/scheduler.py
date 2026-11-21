"""间隔调度器 + APScheduler 驱动的 cron 服务。"""

from __future__ import annotations

import asyncio
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crew.core.envelope import Envelope
from crew.core.interfaces import Scheduler
from crew.core.runctx import current_owner_account_id, normalize_owner_account_id
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


@dataclass
class _OwnerMount:
    scheduler: AsyncIOScheduler
    revoked: threading.Event = field(default_factory=threading.Event)
    changed_jobs: set[str] = field(default_factory=set)


class CronService:
    """定时任务引擎：把持久化任务注册给 APScheduler 并执行。"""

    def __init__(
        self,
        store: CronJobStore,
        runner: JobRunner,
        *,
        max_parallel_jobs: int = 2,
        owner_active: Callable[[str], bool] | None = None,
        mount_retry_delays: tuple[float, ...] = (5.0, 15.0, 30.0),
    ) -> None:
        self._store = store
        self._runner = runner
        self._max_parallel_jobs = max(1, int(max_parallel_jobs or 1))
        self._owner_active = owner_active
        self._mount_retry_delays = tuple(float(value) for value in mount_retry_delays)
        self._mount_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._mount_retry_attempts: dict[str, int] = {}
        self._scheduler: AsyncIOScheduler | None = None
        self._running = False
        self._start_error = ""
        self._lifecycle_lock = asyncio.Lock()
        self._mounted_owners: set[str] = set()
        self._pending_mounts: dict[str, tuple[_OwnerMount, asyncio.Task[int]]] = {}
        self._mount_tasks: set[asyncio.Task[int]] = set()
        self._mount_errors: dict[str, str] = {}
        self._mounted_job_ids: set[str] = set()
        self._mounted_job_ids_by_owner: dict[str, set[str]] = {}
        self._owner_tasks: dict[str, set[asyncio.Task]] = {}
        self._cancel_terminal_status: dict[asyncio.Task, str] = {}
        self._sem = asyncio.Semaphore(self._max_parallel_jobs)
        # Fire 到达终态后的可选回调（由装配层注入，用于通知中心等；缺席时无影响）
        self._on_run_finished: Callable[[dict], Awaitable[None] | None] | None = None

    def set_on_run_finished(
        self,
        callback: Callable[[dict], Awaitable[None] | None] | None,
    ) -> None:
        """注入 Fire 终态回调。info 含 job_id/job_name/status/error_message/owner/session_id。"""
        self._on_run_finished = callback

    @property
    def scheduler(self) -> AsyncIOScheduler | None:
        return self._scheduler

    @property
    def mounted_owner(self) -> str:
        """Compatibility view when exactly one Owner is mounted."""

        return next(iter(self._mounted_owners), "") if len(self._mounted_owners) == 1 else ""

    @property
    def mounted_owners(self) -> frozenset[str]:
        """Return all Owners whose Jobs may currently execute."""

        return frozenset(self._mounted_owners)

    @property
    def pending_mount_owners(self) -> frozenset[str]:
        """Return Owners whose asynchronous mount is still preparing."""

        return frozenset(self._pending_mounts)

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

    def _forget_job(self, job_id: str) -> None:
        self._mounted_job_ids.discard(job_id)
        for owner, job_ids in list(self._mounted_job_ids_by_owner.items()):
            job_ids.discard(job_id)
            if not job_ids:
                self._mounted_job_ids_by_owner.pop(owner, None)

    def sync_job(self, job_id: str, owner_account_id: str | None = None) -> None:
        owner = str(owner_account_id or "").strip()
        if not owner:
            row = self._store.get(job_id)
            owner = str((row or {}).get("owner_account_id") or "").strip()
        pending = self._pending_mounts.get(owner)
        if pending is not None:
            pending[0].changed_jobs.add(job_id)
            return
        if not owner or owner not in self._mounted_owners:
            return
        row = self._store.get(job_id, owner_account_id=owner)
        if row is None:
            self._remove_job(job_id)
            self._forget_job(job_id)
            return
        self._sync_job_row(row)

    def _sync_job_row(self, row: dict, *, prepared: bool = False) -> None:
        """Install one already-authorized Job row into APScheduler."""
        owner = str(row.get("owner_account_id") or "").strip()
        if not owner or owner not in self._mounted_owners:
            return
        job_id = str(row["id"])
        if not row.get("enabled"):
            self._remove_job(job_id)
            self._forget_job(job_id)
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
        self._mounted_job_ids_by_owner.setdefault(owner, set()).add(job_id)
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
            await self._store.wait_for_preparation()
            if owner_account_id not in self._mounted_owners:
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
            await self._store.wait_for_preparation()
            if not owner or owner not in self._mounted_owners:
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
            await self._store.wait_for_preparation()
            if not owner or owner not in self._mounted_owners:
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
        owner = normalize_owner_account_id(claim.get("job", {}).get("owner_account_id"))
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
                finalization = asyncio.create_task(self._finish_fire(row, run_id, status, error_message))
                try:
                    await asyncio.shield(finalization)
                except asyncio.CancelledError:
                    await finalization
                    raise

    async def _finish_fire(self, row: dict, run_id: int, status: str, error_message: str) -> None:
        await self._store.wait_for_preparation()
        if await self._store.finish_fire_async(
            str(row["id"]), run_id, status, error_message=error_message,
        ):
            self._emit_run_finished(row, status, error_message)

    def _emit_run_finished(self, row: dict, status: str, error_message: str) -> None:
        """向装配层回调 Fire 终态。任何回调异常只记日志，不影响 cron 引擎本身。"""
        callback = self._on_run_finished
        if callback is None:
            return
        info = {
            "job_id": str(row.get("id") or ""),
            "job_name": str(row.get("name") or ""),
            "status": status,
            "error_message": error_message,
            "owner_account_id": str(row.get("owner_account_id") or ""),
            "session_id": str(row.get("session_id") or ""),
        }
        try:
            result = callback(info)
        except Exception:  # noqa: BLE001
            log.exception("cron 运行结束回调失败 job=%s", info["job_id"])
            return
        if asyncio.iscoroutine(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()

    @property
    def is_running(self) -> bool:
        """Return whether the APScheduler engine has started."""

        return self._running

    @property
    def start_error(self) -> str:
        """Return the latest startup failure for health reporting."""

        return self._start_error

    @property
    def mount_failures(self) -> dict[str, str]:
        """Return Owner mount failures until that Owner mounts or unmounts."""

        return dict(self._mount_errors)

    async def tick(self, owner_account_id: str | None = None) -> int:
        """Test/compatibility hook that runs due Jobs for mounted Owners."""

        await self._store.wait_for_preparation()
        requested_owner = str(owner_account_id or "").strip()
        owners = (
            [requested_owner]
            if requested_owner and requested_owner in self._mounted_owners
            else list(self._mounted_owners) if not requested_owner else []
        )
        due = [
            (job, owner)
            for owner in owners
            for job in self._store.get_due(owner_account_id=owner)
        ]
        if not due:
            return 0
        tasks = [
            asyncio.create_task(self._execute_job(job["id"], owner))
            for job, owner in due
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

        async with self._lifecycle_lock:
            if self._running:
                return
            try:
                # Clear interrupted runs before any Owner can mount jobs.
                await self._store.recover_running_fires_on_startup()
                scheduler = self._ensure_scheduler()
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
            log.info("CronService 引擎已启动，等待 Active Owner")

    def mount_owner(self, owner_account_id: str) -> int:
        """Mount one Owner without unmounting any other Owner."""

        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 必填")
        if not self._running or self._scheduler is None:
            raise RuntimeError("CronService 尚未启动")
        if owner in self._mounted_owners:
            return len(self._mounted_job_ids_by_owner.get(owner, set()))

        if owner in self._pending_mounts:
            raise RuntimeError("CronService 正在异步挂载该 Owner")
        jobs = self._store.prepare_owner_jobs_for_mount(owner)
        return self._install_owner_jobs(owner, jobs)

    def request_owner_mount(self, owner_account_id: str) -> asyncio.Task[int] | None:
        """Schedule optional activation without delaying ordinary Owner admission."""
        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 必填")
        if not self._running or self._scheduler is None:
            raise RuntimeError("CronService 尚未启动")
        if owner in self._mounted_owners:
            return None
        pending = self._pending_mounts.get(owner)
        if pending is not None:
            return pending[1]
        self._cancel_mount_retry(owner)

        mount = _OwnerMount(self._scheduler)
        task = asyncio.create_task(self._complete_owner_mount(owner, mount), name=f"cron-mount:{owner}")
        self._pending_mounts[owner] = (mount, task)
        self._mount_tasks.add(task)
        task.add_done_callback(
            lambda done, owner=owner: self._owner_mount_finished(owner, done)
        )
        return task

    def _owner_mount_finished(self, owner: str, task: asyncio.Task[int]) -> None:
        self._mount_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            if owner in self._mounted_owners or owner in self._pending_mounts:
                # A later mount for the same Owner already succeeded or is in
                # flight; this task's failure is stale and must not degrade it.
                log.debug("忽略过期的 CronService Owner 挂载失败: owner=%s", owner, exc_info=error)
                return
            self._mount_errors[owner] = str(error)[:500]
            log.error("CronService Owner 挂载失败", exc_info=error)
            self._schedule_mount_retry(owner)

    def _cancel_mount_retry(self, owner: str) -> None:
        task = self._mount_retry_tasks.pop(owner, None)
        if task is not None and not task.done():
            task.cancel()

    def _mount_retry_finished(self, owner: str, task: asyncio.Task[None]) -> None:
        if self._mount_retry_tasks.get(owner) is task:
            self._mount_retry_tasks.pop(owner, None)

    def _schedule_mount_retry(self, owner: str) -> None:
        if not self._running or owner in self._mounted_owners or owner in self._pending_mounts:
            return
        if self._owner_active is not None and not self._owner_active(owner):
            return
        attempt = self._mount_retry_attempts.get(owner, 0)
        if attempt >= len(self._mount_retry_delays):
            return
        self._cancel_mount_retry(owner)
        delay = self._mount_retry_delays[attempt]
        self._mount_retry_attempts[owner] = attempt + 1
        task = asyncio.create_task(
            self._retry_owner_mount(owner, delay),
            name=f"cron-mount-retry:{owner}",
        )
        self._mount_retry_tasks[owner] = task
        task.add_done_callback(
            lambda done, owner=owner: self._mount_retry_finished(owner, done)
        )

    async def _retry_owner_mount(self, owner: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._running or owner in self._mounted_owners or owner in self._pending_mounts:
            return
        if self._owner_active is not None and not self._owner_active(owner):
            return
        # This task is the current retry; remove it before requesting so the
        # request path cannot cancel itself.
        self._mount_retry_tasks.pop(owner, None)
        try:
            self.request_owner_mount(owner)
        except Exception:  # noqa: BLE001 - a failed retry must not kill the timer callback
            log.exception("CronService Owner 挂载重试失败: owner=%s", owner)
            self._schedule_mount_retry(owner)

    async def mount_owner_async(self, owner_account_id: str) -> int:
        """Await the same mount as Gateway admission, even if this waiter is cancelled."""
        owner = str(owner_account_id or "").strip()
        task = self.request_owner_mount(owner)
        if task is None:
            return len(self._mounted_job_ids_by_owner.get(owner, set()))
        return await asyncio.shield(task)

    async def _complete_owner_mount(self, owner: str, mount: _OwnerMount) -> int:
        try:
            jobs = await self._store.prepare_owner_jobs_for_mount_async(owner, revoked=mount.revoked)
            # Other Owners may have started preparation on the shared connection.
            if mount.changed_jobs:
                await self._store.wait_for_preparation()
            if mount.revoked.is_set() or not self._running or self._scheduler is not mount.scheduler:
                return 0
            if mount.changed_jobs:
                jobs = [row for row in jobs if row["id"] not in mount.changed_jobs]
                for job_id in sorted(mount.changed_jobs):
                    current = self._store.get(job_id, owner_account_id=owner)
                    if current is not None and current["enabled"]:
                        jobs.append(current)
            return self._install_owner_jobs(owner, jobs)
        finally:
            pending = self._pending_mounts.get(owner)
            if pending is not None and pending[0] is mount:
                del self._pending_mounts[owner]

    def _install_owner_jobs(self, owner: str, jobs: list[dict]) -> int:
        self._mounted_owners.add(owner)
        self._mounted_job_ids_by_owner.setdefault(owner, set())
        try:
            for job in jobs:
                self._sync_job_row(job, prepared=True)
        except Exception:
            for job_id in list(self._mounted_job_ids_by_owner.get(owner, set())):
                self._remove_job(job_id)
                self._forget_job(job_id)
            self._mounted_owners.discard(owner)
            self._mounted_job_ids_by_owner.pop(owner, None)
            raise
        log.info("CronService 已挂载 Owner: owner=%s jobs=%d", owner, len(jobs))
        self._mount_errors.pop(owner, None)
        self._mount_retry_attempts.pop(owner, None)
        self._cancel_mount_retry(owner)
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
        pending = self._pending_mounts.pop(owner, None)
        if pending is not None:
            pending[0].revoked.set()
        self._mount_errors.pop(owner, None)
        self._mount_retry_attempts.pop(owner, None)
        self._cancel_mount_retry(owner)
        if not owner or owner not in self._mounted_owners:
            return 0
        self._mounted_owners.discard(owner)
        for job_id in list(self._mounted_job_ids_by_owner.get(owner, set())):
            self._remove_job(job_id)
            self._forget_job(job_id)
        self._mounted_job_ids_by_owner.pop(owner, None)

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
        log.info("CronService 已撤下 Owner: owner=%s cancelled=%d", owner, len(tasks))
        return len(tasks)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._running = False
            self._mount_errors.clear()
            self._mount_retry_attempts.clear()
            retry_tasks = list(self._mount_retry_tasks.values())
            for owner in list(self._mount_retry_tasks):
                self._cancel_mount_retry(owner)
            if retry_tasks:
                await asyncio.gather(*retry_tasks, return_exceptions=True)
            for owner in set(self._mounted_owners).union(self._pending_mounts):
                await self.unmount_owner(owner, terminal_status="abandoned")
            if self._mount_tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in self._mount_tasks), return_exceptions=True)
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
            log.info("CronService 已停止")
