"""Owner logout ordering and fail-closed regression tests."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID, current_owner_account_id
from crew.cron.jobs import CronJobStore
from crew.cron.scheduler import CronService
from crew.gateway.logout import LogoutCleanupError, LogoutCoordinator


class _Lease:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner = "A:uid-a"

    def current(self):
        return SimpleNamespace(owner_account_id=self.owner) if self.owner else None

    def release(self, owner: str) -> bool:
        self.events.append("release")
        if owner != self.owner:
            return False
        self.owner = ""
        return True

    def prepare_restart_logout(self, owner: str) -> bool:
        self.events.append("prepare-restart-logout")
        return owner == self.owner


class _Dispatcher:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def activate_owner(self, owner: str) -> None:
        self.events.append(f"activate-dispatcher:{owner}")

    async def stop_owner(self, owner: str) -> int:
        self.events.append(f"stop-dispatcher:{owner}")
        return 2


class _Tasks:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def activate_owner(self, owner: str) -> None:
        self.events.append(f"activate-tasks:{owner}")

    def block_owner(self, owner: str) -> None:
        self.events.append(f"block-tasks:{owner}")

    async def cancel_owner(self, owner: str):
        self.events.append(f"cancel-tasks:{owner}")
        return ["t1"]


class _Team:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def cancel_owner(self, owner: str) -> int:
        self.events.append(f"cancel-team:{owner}")
        return 1


class _Cron:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.is_running = True

    def mount_owner(self, owner: str) -> None:
        self.events.append(f"mount-cron:{owner}")

    async def unmount_owner(self, owner: str) -> int:
        self.events.append(f"unmount-cron:{owner}")
        return 1


class _Channels:
    def __init__(
        self,
        events: list[str],
        failed: bool = False,
        requires_restart: bool = False,
    ) -> None:
        self.events = events
        self.failed = failed
        self.channels: dict[str, object] = {"feishu": object()} if requires_restart else {}

    async def start_all(self, handler, *, owner_account_id: str) -> None:
        self.events.append("start-channels")

    async def stop_all(self, *, reason: str = "disconnected") -> list[str]:
        self.events.append(f"stop-channels:{reason}")
        return ["feishu"] if self.failed else []


class _Connections:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close_owner(self, owner: str) -> int:
        self.events.append(f"close-connections:{owner}")
        return 3


class _InteractionBridge:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def remove_owner(self, owner: str) -> int:
        self.events.append(f"revoke-interactions:{owner}")
        return 1


def _coordinator(
    events: list[str],
    *,
    channel_failure: bool = False,
    requires_restart: bool = False,
    cron_service: CronService | None = None,
):
    lease = _Lease(events)
    coordinator = LogoutCoordinator(
        active_owner=lease,
        dispatcher=_Dispatcher(events),
        task_runtime=_Tasks(events),
        channel_manager=_Channels(
            events,
            failed=channel_failure,
            requires_restart=requires_restart,
        ),
        connections=_Connections(events),
        channel_handler=object(),
        cron_service=cron_service if cron_service is not None else _Cron(events),
        team_manager=_Team(events),
        interaction_bridge=_InteractionBridge(events),
    )
    return coordinator, lease


@pytest.mark.asyncio
async def test_logout_fences_work_then_releases_lease_last():
    events: list[str] = []
    coordinator, lease = _coordinator(events)

    result = await coordinator.logout("A:uid-a")

    assert result.released is True
    assert result.stopped_dispatches == 2
    assert result.cancelled_tasks == 1
    assert result.closed_sockets == 3
    assert events == [
        "block-tasks:A:uid-a",
        "revoke-interactions:A:uid-a",
        "unmount-cron:A:uid-a",
        "stop-dispatcher:A:uid-a",
        "cancel-team:A:uid-a",
        "cancel-tasks:A:uid-a",
        "stop-channels:login_required",
        "close-connections:A:uid-a",
        "release",
    ]
    assert lease.current() is None


@pytest.mark.asyncio
async def test_logout_cleanup_keeps_original_owner_context_and_resets_afterward():
    events: list[str] = []
    coordinator, _lease = _coordinator(events)
    seen: list[str] = []
    original_stop_owner = coordinator._dispatcher.stop_owner

    async def observe_stop_owner(owner: str) -> int:
        seen.append(current_owner_account_id.get())
        return await original_stop_owner(owner)

    coordinator._dispatcher.stop_owner = observe_stop_owner
    assert current_owner_account_id.get() == LOCAL_OWNER_ACCOUNT_ID

    await coordinator.logout("A:uid-a")

    assert seen == ["A:uid-a"]
    assert current_owner_account_id.get() == LOCAL_OWNER_ACCOUNT_ID


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_lease_and_drain_fence_for_retry():
    events: list[str] = []
    coordinator, lease = _coordinator(events, channel_failure=True)

    with pytest.raises(LogoutCleanupError, match="channels: feishu"):
        await coordinator.logout("A:uid-a")

    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "release" not in events


@pytest.mark.asyncio
async def test_team_cleanup_failure_keeps_active_owner_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events)

    async def fail_team(_owner: str) -> int:
        raise RuntimeError("still running")

    coordinator._team_manager.cancel_owner = fail_team

    with pytest.raises(LogoutCleanupError, match="team: still running"):
        await coordinator.logout("A:uid-a")

    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "release" not in events


@pytest.mark.asyncio
async def test_owner_channel_logout_does_not_require_gateway_restart():
    events: list[str] = []
    coordinator, lease = _coordinator(events, requires_restart=True)

    result = await coordinator.logout("A:uid-a")

    assert result.requires_gateway_restart is False
    assert result.released is True
    assert lease.current() is None
    assert events[-1] == "release"


@pytest.mark.asyncio
async def test_owner_activation_is_nonblocking_and_reopens_local_admission():
    events: list[str] = []
    coordinator, _lease = _coordinator(events)
    seen: list[str] = []
    original_mount_owner = coordinator._cron_service.mount_owner

    def observe_mount_owner(owner: str) -> None:
        seen.append(current_owner_account_id.get())
        original_mount_owner(owner)

    coordinator._cron_service.mount_owner = observe_mount_owner

    coordinator.activate_owner("A:uid-a")
    await asyncio.sleep(0)

    assert events[:3] == [
        "activate-dispatcher:A:uid-a",
        "activate-tasks:A:uid-a",
        "mount-cron:A:uid-a",
    ]
    assert seen == ["A:uid-a"]
    assert current_owner_account_id.get() == LOCAL_OWNER_ACCOUNT_ID
    await coordinator.shutdown()


def test_cron_mount_write_wait_does_not_delay_admission_or_survive_logout(tmp_path, monkeypatch):
    owner = "A:uid-a"
    events: list[str] = []
    db = tmp_path / "cron.db"
    started = threading.Event()
    writer_locked = threading.Event()
    preparation_started = threading.Event()
    preparation_finished = threading.Event()
    logout_finished = threading.Event()

    async def run_job(_envelope):
        pytest.fail("a future job ran unexpectedly")

    with closing(CronJobStore(db)) as store, closing(sqlite3.connect(db)) as writer:
        job = store.create(
            name="future job",
            schedule="every 1h",
            query="kept across logout",
            session_id="remaining-session",
            owner_account_id=owner,
        )
        cron = CronService(store, run_job)
        coordinator, lease = _coordinator(events, cron_service=cron)
        execute_once = store._writer._execute_once

        def observe_mount_write(operation):
            if not writer_locked.is_set():
                return execute_once(operation)
            preparation_started.set()
            try:
                return execute_once(operation)
            finally:
                preparation_finished.set()

        monkeypatch.setattr(store._writer, "_execute_once", observe_mount_write)

        async def admit_and_logout():
            await cron.start()
            try:
                started.set()
                assert await asyncio.to_thread(writer_locked.wait, 5)
                coordinator.activate_owner(owner)
                assert await asyncio.to_thread(preparation_started.wait, 5)
                assert f"activate-dispatcher:{owner}" in events
                assert f"activate-tasks:{owner}" in events
                assert coordinator.allows_work(owner)
                assert owner not in cron.mounted_owners
                assert not preparation_finished.is_set()
                pending_mount = cron.request_owner_mount(owner)
                assert pending_mount is not None

                result = await coordinator.logout(owner)
                assert result.released
                assert lease.current() is None
                assert events[-1] == "release"
                assert not preparation_finished.is_set()
                logout_finished.set()

                assert await asyncio.to_thread(preparation_finished.wait, 5)
                assert await asyncio.wait_for(pending_mount, timeout=5) == 0
                assert owner not in cron.mounted_owners
                assert not coordinator.allows_work(owner)
                assert store.get(job["id"], owner_account_id=owner) == job
            finally:
                await coordinator.shutdown()
                await cron.stop()

        with ThreadPoolExecutor(max_workers=1) as gateway:
            scenario = gateway.submit(asyncio.run, admit_and_logout())
            try:
                assert started.wait(timeout=5)
                writer.execute("BEGIN IMMEDIATE")
                writer_locked.set()
                assert logout_finished.wait(timeout=3), "Cron writer blocked owner admission or logout"
            finally:
                writer.rollback()
                writer_locked.set()
            scenario.result(timeout=5)


@pytest.mark.asyncio
async def test_logout_deadline_returns_restart_without_releasing_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events)
    coordinator._logout_timeout_seconds = 0.02
    cancelled = asyncio.Event()

    async def hang_dispatcher(_owner: str) -> int:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate an SDK cleanup that suppresses cancellation and returns
            # after the coordinator has already fixed the restart response.
            await asyncio.sleep(0.03)
            return 2
        finally:
            cancelled.set()

    coordinator._dispatcher.stop_owner = hang_dispatcher

    started = asyncio.get_running_loop().time()
    result = await coordinator.logout("A:uid-a")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1
    assert result.released is False
    assert result.requires_gateway_restart is True
    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "prepare-restart-logout" in events
    assert "release" not in events
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0.02)
    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "release" not in events
