"""Owner-scoped Gateway logout coordination.

Logout fences and cleans one authenticated Owner at a time.  A slow channel SDK
or a running task for one account must not turn into a Gateway-wide login lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from crew.core.runctx import current_owner_account_id
from crew.state.logging import get_logger

log = get_logger("gateway.logout")


class LogoutCleanupError(RuntimeError):
    """Raised when critical owner cleanup fails before lease release."""


@dataclass(frozen=True)
class LogoutResult:
    owner_account_id: str
    stopped_dispatches: int
    cancelled_tasks: int
    closed_sockets: int
    released: bool
    requires_gateway_restart: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LogoutCoordinator:
    """Serialize cleanup per Owner while allowing other Owners to continue."""

    def __init__(
        self,
        *,
        active_owner: Any,
        dispatcher: Any,
        task_runtime: Any,
        channel_manager: Any,
        connections: Any,
        channel_handler: Any,
        cron_service: Any | None = None,
        team_manager: Any | None = None,
        interaction_bridge: Any | None = None,
        security_service: Any | None = None,
        logout_timeout_seconds: float = 10.0,
    ) -> None:
        self._active_owner = active_owner
        self._dispatcher = dispatcher
        self._task_runtime = task_runtime
        self._channel_manager = channel_manager
        self._connections = connections
        self._channel_handler = channel_handler
        self._cron_service = cron_service
        self._team_manager = team_manager
        self._interaction_bridge = interaction_bridge
        self._security_service = security_service
        self._logout_timeout_seconds = max(0.001, float(logout_timeout_seconds))
        self._lock = asyncio.Lock()
        self._draining_owners: set[str] = set()
        self._channel_owners: set[str] = set()
        self._activation_tasks: dict[str, asyncio.Task[Any]] = {}
        self._restart_fenced_owners: set[str] = set()

    @property
    def draining_owner(self) -> str:
        """Compatibility view for diagnostics when exactly one Owner drains."""

        return next(iter(self._draining_owners), "")

    def is_draining(self, owner_account_id: str = "") -> bool:
        owner = str(owner_account_id or "").strip()
        return bool(self._draining_owners if not owner else owner in self._draining_owners)

    def allows_work(self, owner_account_id: str) -> bool:
        """Check the in-memory drain fence and this Owner's session lease."""

        owner = str(owner_account_id or "").strip()
        if not owner or owner in self._draining_owners:
            return False
        lease = self._owner_lease(owner)
        return lease is not None and lease.owner_account_id == owner

    def activate_owner(self, owner_account_id: str) -> None:
        """Open local admission and start only this Owner's resources."""

        owner = str(owner_account_id or "").strip()
        if not owner or owner in self._draining_owners:
            return
        token = current_owner_account_id.set(owner)
        try:
            if self._cron_service is not None and self._cron_service.is_running:
                self._cron_service.mount_owner(owner)
            self._dispatcher.activate_owner(owner)
            self._task_runtime.activate_owner(owner)
            task = self._activation_tasks.get(owner)
            if owner in self._channel_owners or (task is not None and not task.done()):
                return
            task = asyncio.create_task(
                self._activate_channels(owner),
                name=f"activate-channels:{owner}",
            )
            self._activation_tasks[owner] = task
        finally:
            current_owner_account_id.reset(token)

    async def _activate_channels(self, owner: str) -> None:
        token = current_owner_account_id.set(owner)
        try:
            await self._channel_manager.start_all(
                self._channel_handler,
                owner_account_id=owner,
            )
            if owner not in self._draining_owners:
                self._channel_owners.add(owner)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - channel errors remain visible in channel state
            log.exception("Owner 渠道激活失败 owner=%s", owner)
        finally:
            self._activation_tasks.pop(owner, None)
            current_owner_account_id.reset(token)

    async def _cancel_activation(self, owner: str | None = None) -> None:
        owners = [owner] if owner else list(self._activation_tasks)
        tasks = [self._activation_tasks.pop(item, None) for item in owners]
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def logout(self, owner_account_id: str) -> LogoutResult:
        """Drain one Owner within one deadline, or retain only its lease."""

        owner = str(owner_account_id or "").strip()
        if not owner:
            raise LogoutCleanupError("缺少退出账号")
        token = current_owner_account_id.set(owner)
        try:
            cleanup = asyncio.create_task(
                self._logout_owned(owner),
                name=f"logout-cleanup:{owner}",
            )
            done, _pending = await asyncio.wait(
                {cleanup},
                timeout=self._logout_timeout_seconds,
            )
            if cleanup in done:
                return cleanup.result()

            self._restart_fenced_owners.add(owner)
            self._draining_owners.add(owner)
            self._task_runtime.block_owner(owner)
            if self._interaction_bridge is not None:
                self._interaction_bridge.remove_owner(owner)
            if self._security_service is not None:
                self._security_service.revoke_owner(owner)
            if not self._active_owner.prepare_restart_logout(owner):
                raise LogoutCleanupError("Logout 超时且无法持久化 Owner 重启退出意图")
            self._channel_owners.discard(owner)
            await self._cancel_activation(owner)
            cleanup.cancel()
            await asyncio.sleep(0)
            if cleanup.done() and not cleanup.cancelled():
                return cleanup.result()
            if not cleanup.done():
                cleanup.add_done_callback(self._consume_cleanup_result)
            log.warning(
                "Logout 超过 %.3fs 总预算，保留 Owner 会话并请求受控重启 owner=%s",
                self._logout_timeout_seconds,
                owner,
            )
            return LogoutResult(
                owner_account_id=owner,
                stopped_dispatches=0,
                cancelled_tasks=0,
                closed_sockets=0,
                released=False,
                requires_gateway_restart=True,
            )
        finally:
            current_owner_account_id.reset(token)

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[Any]) -> None:
        """Consume a late cleanup outcome after the restart response is fixed."""

        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001 - late cleanup cannot change the fixed response
            log.exception("Logout 超时后的清理任务异常")

    def _owner_lease(self, owner: str) -> Any:
        getter = getattr(self._active_owner, "get", None)
        if callable(getter):
            return getter(owner)
        try:
            lease = self._active_owner.current(owner)
        except TypeError:
            lease = self._active_owner.current()
        return lease if lease is not None and lease.owner_account_id == owner else None

    async def _stop_owner_channels(self, owner: str) -> list[str]:
        stop_owner = getattr(self._channel_manager, "stop_owner", None)
        if callable(stop_owner):
            return list(await stop_owner(owner, reason="login_required"))
        # Old injected managers have no owner-aware lifecycle. Production
        # ChannelManager always supplies stop_owner; this fallback is only for
        # integrations that have not adopted the new interface yet.
        return list(await self._channel_manager.stop_all(reason="login_required"))

    async def _logout_owned(self, owner: str) -> LogoutResult:
        """Perform ordered cleanup after the logout Owner has been validated."""

        async with self._lock:
            lease = self._owner_lease(owner)
            if lease is None:
                self._draining_owners.discard(owner)
                return LogoutResult(
                    owner_account_id=owner,
                    stopped_dispatches=0,
                    cancelled_tasks=0,
                    closed_sockets=0,
                    released=True,
                    requires_gateway_restart=False,
                )

            self._draining_owners.add(owner)
            self._task_runtime.block_owner(owner)
            if self._interaction_bridge is not None:
                self._interaction_bridge.remove_owner(owner)
            if self._security_service is not None:
                self._security_service.revoke_owner(owner)
            errors: list[str] = []
            stopped_dispatches = 0
            cancelled_tasks: list[str] = []
            closed_sockets = 0
            requires_gateway_restart = False

            await self._cancel_activation(owner)
            if self._cron_service is not None:
                try:
                    await self._cron_service.unmount_owner(owner)
                except Exception as exc:  # noqa: BLE001 - keep lease when Cron cleanup fails
                    log.exception("Logout 撤下 Cron 失败 owner=%s", owner)
                    errors.append(f"cron: {exc}")
            try:
                stopped_dispatches = await self._dispatcher.stop_owner(owner)
            except Exception as exc:  # noqa: BLE001 - continue remaining cleanup, keep lease
                log.exception("Logout 停止调度失败 owner=%s", owner)
                errors.append(f"dispatcher: {exc}")
            if self._team_manager is not None:
                try:
                    await self._team_manager.cancel_owner(owner)
                except Exception as exc:  # noqa: BLE001 - detached Team 未停则不能释放租约
                    log.exception("Logout 停止 Team 失败 owner=%s", owner)
                    errors.append(f"team: {exc}")
            try:
                cancelled_tasks = await self._task_runtime.cancel_owner(owner)
            except Exception as exc:  # noqa: BLE001 - continue remaining cleanup, keep lease
                log.exception("Logout 取消运行任务失败 owner=%s", owner)
                errors.append(f"tasks: {exc}")
            try:
                failed_channels = await self._stop_owner_channels(owner)
                if failed_channels:
                    errors.append(f"channels: {','.join(sorted(failed_channels))}")
            except Exception as exc:  # noqa: BLE001 - continue socket cleanup, keep lease
                log.exception("Logout 停止 Owner 渠道失败 owner=%s", owner)
                errors.append(f"channels: {exc}")
            try:
                closed_sockets = await self._connections.close_owner(owner)
            except Exception as exc:  # noqa: BLE001 - a live old socket is a release blocker
                log.exception("Logout 关闭连接失败 owner=%s", owner)
                errors.append(f"connections: {exc}")

            if errors:
                raise LogoutCleanupError("; ".join(errors))
            if owner in self._restart_fenced_owners:
                return LogoutResult(
                    owner_account_id=owner,
                    stopped_dispatches=stopped_dispatches,
                    cancelled_tasks=len(cancelled_tasks),
                    closed_sockets=closed_sockets,
                    released=False,
                    requires_gateway_restart=True,
                )
            if not self._active_owner.release(owner):
                raise LogoutCleanupError("Owner 会话租约释放失败")

            self._channel_owners.discard(owner)
            self._draining_owners.discard(owner)
            return LogoutResult(
                owner_account_id=owner,
                stopped_dispatches=stopped_dispatches,
                cancelled_tasks=len(cancelled_tasks),
                closed_sockets=closed_sockets,
                released=True,
                requires_gateway_restart=requires_gateway_restart,
            )

    async def shutdown(self) -> None:
        """Cancel all background channel activations during Gateway shutdown."""

        await self._cancel_activation()
