"""Owner-wide Gateway logout coordination.

Logout is an ordered safety boundary: fence new work, invalidate and cancel the
old execution generation, stop external channels, detach sockets, then release
the persistent Active Owner lease.  A failed critical cleanup keeps the lease
and admission fence in place so another account cannot overlap old work.
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
    """Serialize owner activation/logout around the single Active Owner lease."""

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
        process_registry: Any | None = None,
        runtime_tool_registry: Any | None = None,
        agent_manager: Any | None = None,
        credential_provider_manager: Any | None = None,
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
        self._process_registry = process_registry
        self._runtime_tool_registry = runtime_tool_registry
        self._agent_manager = agent_manager
        self._credential_provider_manager = credential_provider_manager
        self._logout_timeout_seconds = max(0.001, float(logout_timeout_seconds))
        self._lock = asyncio.Lock()
        self._draining_owner = ""
        self._channel_owner = ""
        self._activation_task: asyncio.Task[Any] | None = None
        self._restart_fenced_owners: set[str] = set()

    @property
    def draining_owner(self) -> str:
        return self._draining_owner

    def is_draining(self, owner_account_id: str = "") -> bool:
        owner = str(owner_account_id or "").strip()
        return bool(self._draining_owner and (not owner or self._draining_owner == owner))

    def allows_work(self, owner_account_id: str) -> bool:
        """Check both the in-memory drain fence and persistent lease owner."""
        owner = str(owner_account_id or "").strip()
        if not owner or self._draining_owner:
            return False
        lease = self._active_owner.current()
        return lease is not None and lease.owner_account_id == owner

    def activate_owner(
        self,
        owner_account_id: str,
        *,
        process_authorization_generation: str = "",
        process_authorization_expires_at: float = 0.0,
    ) -> None:
        """Open local admission and connect channels without delaying the HTTP response."""
        owner = str(owner_account_id or "").strip()
        if not owner or self._draining_owner:
            return
        token = current_owner_account_id.set(owner)
        try:
            if (
                self._process_registry is not None
                and process_authorization_generation
                and process_authorization_expires_at > 0
            ):
                self._process_registry.activate_owner(
                    owner,
                    authorization_generation=process_authorization_generation,
                    authorization_expires_at=process_authorization_expires_at,
                )
            if self._cron_service is not None and self._cron_service.is_running:
                self._cron_service.mount_owner(owner)
            self._dispatcher.activate_owner(owner)
            self._task_runtime.activate_owner(owner)
            if self._channel_owner == owner:
                return
            if self._activation_task is not None and not self._activation_task.done():
                return
            self._activation_task = asyncio.create_task(self._activate_channels(owner))
        finally:
            current_owner_account_id.reset(token)

    async def _activate_channels(self, owner: str) -> None:
        token = current_owner_account_id.set(owner)
        try:
            await self._channel_manager.start_all(
                self._channel_handler,
                owner_account_id=owner,
            )
            if not self._draining_owner:
                self._channel_owner = owner
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - channel errors remain visible in channel state
            log.exception("Owner 渠道激活失败 owner=%s", owner)
        finally:
            current_owner_account_id.reset(token)

    async def _cancel_activation(self) -> None:
        task = self._activation_task
        self._activation_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _revoke_runtime_tool_owner(self, owner: str) -> None:
        revoke = getattr(
            self._runtime_tool_registry,
            "revoke_runtime_tool_owner",
            None,
        )
        if not callable(revoke):
            return
        revoke_task = asyncio.create_task(revoke(owner))
        try:
            await asyncio.shield(revoke_task)
        except asyncio.CancelledError:
            revoke_task.add_done_callback(self._consume_cleanup_result)
            raise

    async def logout(self, owner_account_id: str) -> LogoutResult:
        """Drain one owner within one deadline, or retain the lease for restart."""
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

            # Do not wait indefinitely for a cancellation-resistant SDK.  The
            # retained lease and drain fence prevent overlap until Desktop
            # performs the controlled process restart.
            self._restart_fenced_owners.add(owner)
            self._draining_owner = owner
            self._task_runtime.block_owner(owner)
            runtime_cleanup = asyncio.create_task(
                self._revoke_runtime_tool_owner(owner)
            )
            runtime_cleanup.add_done_callback(self._consume_cleanup_result)
            if self._interaction_bridge is not None:
                self._interaction_bridge.remove_owner(owner)
            if self._security_service is not None:
                self._security_service.revoke_owner(owner)
            if self._process_registry is not None:
                try:
                    self._process_registry.revoke_owner(
                        owner,
                        reason="OWNER_LOGOUT",
                    )
                except Exception:
                    log.exception(
                        "Logout 超时后后台进程仍由持久 fence 接管 owner=%s",
                        owner,
                    )
            if not self._active_owner.prepare_restart_logout(owner):
                raise LogoutCleanupError("Logout 超时且无法持久化 Gateway 重启退出意图")
            self._channel_owner = ""
            cleanup.cancel()
            await asyncio.sleep(0)
            if cleanup.done() and not cleanup.cancelled():
                return cleanup.result()
            if not cleanup.done():
                cleanup.add_done_callback(self._consume_cleanup_result)
            log.warning(
                "Logout 超过 %.3fs 总预算，保留租约并请求受控重启 owner=%s",
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

    async def _logout_owned(self, owner: str) -> LogoutResult:
        """Perform ordered cleanup after the logout Owner has been validated."""
        async with self._lock:
            lease = self._active_owner.current()
            if lease is None:
                self._draining_owner = ""
                return LogoutResult(
                    owner_account_id=owner,
                    stopped_dispatches=0,
                    cancelled_tasks=0,
                    closed_sockets=0,
                    released=True,
                    requires_gateway_restart=False,
                )
            if lease.owner_account_id != owner:
                raise LogoutCleanupError("当前账号不是 Active Owner")

            self._draining_owner = owner
            self._task_runtime.block_owner(owner)
            errors: list[str] = []
            try:
                await self._revoke_runtime_tool_owner(owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retain lease on failed process cleanup
                log.exception("Logout 运行期工具清理失败 owner=%s", owner)
                errors.append(f"runtime_tools: {exc}")
            if self._interaction_bridge is not None:
                self._interaction_bridge.remove_owner(owner)
            if self._security_service is not None:
                self._security_service.revoke_owner(owner)
            if self._process_registry is not None:
                try:
                    self._process_registry.revoke_owner(
                        owner,
                        reason="OWNER_LOGOUT",
                    )
                except Exception as exc:
                    log.exception("Logout 后台进程清理失败 owner=%s", owner)
                    errors.append(f"processes: {exc}")
            stopped_dispatches = 0
            cancelled_tasks: list[str] = []
            closed_sockets = 0
            requires_gateway_restart = "feishu" in self._channel_manager.channels

            await self._cancel_activation()
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
            if self._agent_manager is not None:
                try:
                    await self._agent_manager.drop_owner_and_wait(
                        owner,
                        timeout=min(5.0, self._logout_timeout_seconds),
                    )
                except Exception as exc:  # noqa: BLE001 - credential clients must close
                    log.exception("Logout 关闭 Owner 凭据客户端失败 owner=%s", owner)
                    errors.append(f"credential_clients: {exc}")
            if self._team_manager is not None:
                try:
                    await self._team_manager.cancel_owner(owner)
                except Exception as exc:  # noqa: BLE001 - detached Team 未停则不能释放租约
                    log.exception("Logout 停止 Team 失败 owner=%s", owner)
                    errors.append(f"team: {exc}")
            if self._credential_provider_manager is not None:
                try:
                    await self._credential_provider_manager.close_owner_credential_providers(
                        owner
                    )
                except Exception as exc:  # noqa: BLE001 - owner key clients must close
                    log.exception("Logout 关闭 Owner Provider 失败 owner=%s", owner)
                    errors.append(f"credential_providers: {exc}")
            try:
                cancelled_tasks = await self._task_runtime.cancel_owner(owner)
            except Exception as exc:  # noqa: BLE001 - continue remaining cleanup, keep lease
                log.exception("Logout 取消运行任务失败 owner=%s", owner)
                errors.append(f"tasks: {exc}")
            try:
                failed_channels = await self._channel_manager.stop_all(reason="login_required")
                if failed_channels:
                    errors.append(f"channels: {','.join(sorted(failed_channels))}")
            except Exception as exc:  # noqa: BLE001 - continue socket cleanup, keep lease
                log.exception("Logout 停止渠道失败 owner=%s", owner)
                errors.append(f"channels: {exc}")
            try:
                closed_sockets = await self._connections.close_owner(owner)
            except Exception as exc:  # noqa: BLE001 - a live old socket is a release blocker
                log.exception("Logout 关闭连接失败 owner=%s", owner)
                errors.append(f"connections: {exc}")

            if errors:
                raise LogoutCleanupError("; ".join(errors))
            if owner in self._restart_fenced_owners:
                # A fixed timeout response has already committed this Owner to
                # process restart.  Late cleanup may finish resource work, but
                # must never release the lease or reopen admission.
                self._channel_owner = ""
                return LogoutResult(
                    owner_account_id=owner,
                    stopped_dispatches=stopped_dispatches,
                    cancelled_tasks=len(cancelled_tasks),
                    closed_sockets=closed_sockets,
                    released=False,
                    requires_gateway_restart=True,
                )
            if requires_gateway_restart:
                if not self._active_owner.prepare_restart_logout(owner):
                    raise LogoutCleanupError("无法持久化 Gateway 重启退出意图")
                self._channel_owner = ""
                return LogoutResult(
                    owner_account_id=owner,
                    stopped_dispatches=stopped_dispatches,
                    cancelled_tasks=len(cancelled_tasks),
                    closed_sockets=closed_sockets,
                    released=False,
                    requires_gateway_restart=True,
                )
            if not self._active_owner.release(owner):
                raise LogoutCleanupError("Active Owner 租约释放失败")

            self._channel_owner = ""
            self._draining_owner = ""
            return LogoutResult(
                owner_account_id=owner,
                stopped_dispatches=stopped_dispatches,
                cancelled_tasks=len(cancelled_tasks),
                closed_sockets=closed_sockets,
                released=True,
                requires_gateway_restart=requires_gateway_restart,
            )

    async def shutdown(self) -> None:
        """Cancel a background channel activation during Gateway shutdown."""
        await self._cancel_activation()
