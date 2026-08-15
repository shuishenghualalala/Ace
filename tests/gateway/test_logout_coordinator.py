"""Owner logout ordering and fail-closed regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crew.core.runctx import current_owner_account_id
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


class _Agents:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def drop_owner_and_wait(self, owner: str, *, timeout: float) -> None:
        assert timeout > 0
        self.events.append(f"close-agents:{owner}")


class _Providers:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close_owner_credential_providers(self, owner: str) -> None:
        self.events.append(f"close-providers:{owner}")


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

    async def start_all(self, handler) -> None:
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


class _Processes:
    def __init__(self, events: list[str], *, failed: bool = False) -> None:
        self.events = events
        self.failed = failed

    def activate_owner(self, owner: str, **_kwargs) -> None:
        self.events.append(f"activate-processes:{owner}")

    def revoke_owner(self, owner: str, *, reason: str) -> int:
        assert reason == "OWNER_LOGOUT"
        self.events.append(f"revoke-processes:{owner}")
        if self.failed:
            raise RuntimeError("cleanup pending")
        return 1


def _coordinator(
    events: list[str],
    *,
    channel_failure: bool = False,
    process_failure: bool = False,
    requires_restart: bool = False,
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
        cron_service=_Cron(events),
        team_manager=_Team(events),
        interaction_bridge=_InteractionBridge(events),
        agent_manager=_Agents(events),
        credential_provider_manager=_Providers(events),
        process_registry=_Processes(events, failed=process_failure),
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
        "revoke-processes:A:uid-a",
        "unmount-cron:A:uid-a",
        "stop-dispatcher:A:uid-a",
        "close-agents:A:uid-a",
        "cancel-team:A:uid-a",
        "close-providers:A:uid-a",
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
    assert current_owner_account_id.get() == ""

    await coordinator.logout("A:uid-a")

    assert seen == ["A:uid-a"]
    assert current_owner_account_id.get() == ""


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
async def test_process_cleanup_failure_keeps_active_owner_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events, process_failure=True)

    with pytest.raises(LogoutCleanupError, match="processes: cleanup pending"):
        await coordinator.logout("A:uid-a")

    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "revoke-processes:A:uid-a" in events
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
async def test_credential_client_close_failure_keeps_active_owner_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events)

    async def fail_close(_owner: str, *, timeout: float) -> None:
        assert timeout > 0
        raise RuntimeError("client still holds key")

    coordinator._agent_manager.drop_owner_and_wait = fail_close

    with pytest.raises(LogoutCleanupError, match="credential_clients"):
        await coordinator.logout("A:uid-a")

    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "release" not in events


@pytest.mark.asyncio
async def test_credential_provider_close_failure_keeps_active_owner_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events)

    async def fail_close(_owner: str) -> None:
        raise RuntimeError("provider still holds key")

    coordinator._credential_provider_manager.close_owner_credential_providers = (
        fail_close
    )

    with pytest.raises(LogoutCleanupError, match="credential_providers"):
        await coordinator.logout("A:uid-a")

    assert lease.current().owner_account_id == "A:uid-a"
    assert coordinator.is_draining("A:uid-a") is True
    assert "release" not in events


@pytest.mark.asyncio
async def test_restart_required_logout_persists_intent_before_retaining_lease():
    events: list[str] = []
    coordinator, lease = _coordinator(events, requires_restart=True)

    result = await coordinator.logout("A:uid-a")

    assert result.requires_gateway_restart is True
    assert result.released is False
    assert lease.current().owner_account_id == "A:uid-a"
    assert events[-1] == "prepare-restart-logout"
    assert "release" not in events


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
        "mount-cron:A:uid-a",
        "activate-dispatcher:A:uid-a",
        "activate-tasks:A:uid-a",
    ]
    assert seen == ["A:uid-a"]
    assert current_owner_account_id.get() == ""
    await coordinator.shutdown()


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
