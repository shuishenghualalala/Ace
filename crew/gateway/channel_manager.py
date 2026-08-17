"""按平台与 Owner 管理接入渠道实例、状态和生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from crew.core.interfaces import Channel, MessageHandler
from crew.gateway.hooks import hook_registry
from crew.state.logging import get_logger

log = get_logger("gateway")
ChannelKey = tuple[str, str]


@dataclass
class ChannelState:
    running: bool = False
    error: str = ""
    operation: str = ""
    reason: str = ""


class ChannelManager:
    """同一平台可以拥有多个 Owner 独立实例。"""

    def __init__(self) -> None:
        self._channels: dict[ChannelKey, Channel] = {}
        self._states: dict[ChannelKey, ChannelState] = {}
        self._locks: dict[ChannelKey, asyncio.Lock] = {}

    @staticmethod
    def _key(name: str, owner_account_id: str = "") -> ChannelKey:
        return str(name or "").strip().lower(), str(owner_account_id or "").strip()

    @property
    def channels(self) -> MappingProxyType[str, Channel]:
        """兼容旧调用；新代码应使用 ``get`` 或 ``iter_channels`` 指定 Owner。"""

        view: dict[str, Channel] = {}
        for (name, owner), channel in self._channels.items():
            if owner == "" or name not in view:
                view[name] = channel
        return MappingProxyType(view)

    def get(self, name: str, owner_account_id: str = "") -> Channel | None:
        return self._channels.get(self._key(name, owner_account_id))

    def iter_channels(self, owner_account_id: str | None = None) -> list[tuple[str, str, Channel]]:
        owner = None if owner_account_id is None else str(owner_account_id or "").strip()
        return [
            (name, channel_owner, channel)
            for (name, channel_owner), channel in self._channels.items()
            if owner is None
            or channel_owner == owner
            or (channel_owner == "" and owner in {"local", "dev:dev"})
        ]

    def register(self, channel: Channel, *, owner_account_id: str = "") -> None:
        name, owner = self._key(channel.name, owner_account_id)
        try:
            setattr(channel, "_gateway_owner_account_id", owner)
        except Exception:
            pass
        self._channels[(name, owner)] = channel
        self._states.setdefault((name, owner), ChannelState())
        log.info("注册渠道: %s owner=%s", name, owner or "global")

    def unregister(self, name: str, owner_account_id: str = "") -> None:
        key = self._key(name, owner_account_id)
        self._channels.pop(key, None)
        self._states.pop(key, None)

    def record_error(self, name: str, error: str, owner_account_id: str = "") -> None:
        key = self._key(name, owner_account_id)
        self._states[key] = ChannelState(running=False, error=error, reason="error")
        try:
            asyncio.get_running_loop().create_task(
                self._emit_state_change(name, owner_account_id, running=False, error=error)
            )
        except RuntimeError:
            pass

    def is_busy(self, name: str, owner_account_id: str = "") -> bool:
        """返回指定渠道实例是否正在执行互斥生命周期操作。"""

        if str(owner_account_id or "").strip():
            lock = self._locks.get(self._key(name, owner_account_id))
            return bool(lock and lock.locked())
        return any(
            lock.locked()
            for (platform, _owner), lock in self._locks.items()
            if platform == str(name or "").strip().lower()
        )

    def lock_for(self, name: str, owner_account_id: str = "") -> asyncio.Lock:
        """返回指定 ``(platform, owner)`` 的互斥锁。"""

        return self._locks.setdefault(self._key(name, owner_account_id), asyncio.Lock())

    async def _emit_state_change(
        self,
        name: str,
        owner_account_id: str,
        *,
        running: bool,
        error: str = "",
    ) -> None:
        await hook_registry.emit(
            "channel:state_change",
            {
                "name": name,
                "owner_account_id": str(owner_account_id or "").strip(),
                "running": running,
                "error": error,
            },
        )

    @staticmethod
    def _owner_handler(handler: MessageHandler, owner_account_id: str) -> MessageHandler:
        owner = str(owner_account_id or "").strip()

        def bound(envelope):
            if owner:
                params = dict(getattr(envelope, "params", {}) or {})
                params["gateway_owner_account_id"] = owner
                envelope.params = params
            return handler(envelope)

        return bound

    async def start_all(self, handler: MessageHandler, *, owner_account_id: str = "") -> None:
        """启动全局渠道和指定 Owner 的渠道，已运行实例保持不变。"""

        owner = str(owner_account_id or "").strip()
        for name, channel_owner, channel in list(self.iter_channels()):
            if channel_owner not in {"", owner}:
                continue
            if channel_owner == "" and owner not in {"", "local", "dev:dev"}:
                continue
            key = (name, channel_owner)
            state = self._states.setdefault(key, ChannelState())
            if state.running:
                continue
            state.operation = "connecting"
            state.reason = ""
            try:
                await channel.start(self._owner_handler(handler, channel_owner))
                state.running = True
                state.error = ""
                log.info("渠道已启动: %s owner=%s", name, channel_owner or "global")
                await self._emit_state_change(name, channel_owner, running=True, error="")
            except Exception as exc:  # noqa: BLE001 - 每个平台实例独立收敛
                state.running = False
                state.error = str(exc)
                state.reason = "error"
                log.exception("渠道启动失败: %s owner=%s", name, channel_owner or "global")
                await self._emit_state_change(name, channel_owner, running=False, error=str(exc))
            finally:
                state.operation = ""

    async def restart_one(
        self,
        name: str,
        channel: Channel,
        handler: MessageHandler,
        *,
        owner_account_id: str = "",
    ) -> ChannelState:
        """只替换指定 Owner 的渠道实例。"""

        platform, owner = self._key(name, owner_account_id)
        key = (platform, owner)
        lock = self.lock_for(platform, owner)
        async with lock:
            state = self._states.setdefault(key, ChannelState())
            state.operation = "reconnecting"
            old = self._channels.get(key)
            old_state = self._states.get(key, ChannelState())
            if old is not None:
                stop = getattr(old, "stop", None)
                try:
                    if callable(stop):
                        await stop()
                except Exception as exc:  # noqa: BLE001 - 新配置仍有机会修复旧连接
                    log.warning("渠道热重连时停止旧实例失败: %s owner=%s: %s", platform, owner, exc)

            try:
                setattr(channel, "_gateway_owner_account_id", owner)
            except Exception:
                pass
            self._channels[key] = channel
            try:
                await channel.start(self._owner_handler(handler, owner))
                state.running = True
                state.error = ""
                state.operation = ""
                state.reason = ""
                log.info("渠道已热重连: %s owner=%s", platform, owner or "global")
                await self._emit_state_change(platform, owner, running=True, error="")
            except Exception as exc:  # noqa: BLE001 - 仅回滚当前 Owner 实例
                self._channels.pop(key, None)
                if old is not None:
                    self._channels[key] = old
                    old_state.running = False
                    old_state.error = str(exc)
                    old_state.operation = ""
                    old_state.reason = "error"
                    self._states[key] = old_state
                else:
                    state.running = False
                    state.error = str(exc)
                    state.operation = ""
                    state.reason = "error"
                log.exception("渠道热重连失败: %s owner=%s", platform, owner or "global")
                await self._emit_state_change(platform, owner, running=False, error=str(exc))
            return self._states[key]

    async def stop_one_locked(
        self,
        name: str,
        owner_account_id: str = "",
        *,
        operation: str = "disconnecting",
    ) -> ChannelState:
        """停止并移除一个 Owner 的渠道；调用方必须持有同一资源锁。"""

        platform, owner = self._key(name, owner_account_id)
        key = (platform, owner)
        state = self._states.setdefault(key, ChannelState())
        state.operation = operation
        channel = self._channels.pop(key, None)
        stop = getattr(channel, "stop", None)
        try:
            if callable(stop):
                await stop()
            state.running = False
            state.error = ""
            state.reason = "disconnected"
        except Exception as exc:  # noqa: BLE001
            state.running = False
            state.error = str(exc)
            state.reason = "error"
            log.exception("渠道停止失败: %s owner=%s", platform, owner or "global")
        finally:
            state.operation = ""
        await self._emit_state_change(platform, owner, running=False, error=state.error)
        return state

    async def stop_one(self, name: str, owner_account_id: str = "") -> ChannelState:
        lock = self.lock_for(name, owner_account_id)
        async with lock:
            return await self.stop_one_locked(name, owner_account_id)

    async def stop_owner(self, owner_account_id: str, *, reason: str = "disconnected") -> list[str]:
        """停止指定 Owner 的全部渠道，不影响全局渠道或其它 Owner。"""

        owner = str(owner_account_id or "").strip()
        failed: list[str] = []
        for name, channel_owner, _channel in list(self.iter_channels()):
            if channel_owner != owner:
                continue
            state = await self.stop_one(name, owner)
            # 退出只断开连接，保留已加载的 Owner 配置实例；该账号重新登录
            # 时可以直接由 start_all 重启，不需要重新构造渠道对象。
            self._channels.setdefault((name, owner), _channel)
            if not state.error:
                state.reason = reason
            else:
                failed.append(name)
        return failed

    async def stop_all(self, *, reason: str = "disconnected") -> list[str]:
        """停止所有已注册渠道实例。"""

        failed: list[str] = []
        for name, owner, channel in reversed(list(self.iter_channels())):
            key = (name, owner)
            state = self._states.setdefault(key, ChannelState())
            stop = getattr(channel, "stop", None)
            try:
                if callable(stop):
                    await stop()
                state.error = ""
            except Exception as exc:  # noqa: BLE001 - 停止循环按实例隔离
                state.error = str(exc)
                state.reason = "error"
                failed.append(f"{name}:{owner}" if owner else name)
                log.exception("渠道停止失败: %s owner=%s", name, owner or "global")
            finally:
                state.running = False
                if not state.error:
                    state.reason = reason
                await self._emit_state_change(name, owner, running=False, error=state.error)
        return failed

    def status(self, owner_account_id: str | None = None) -> list[dict[str, Any]]:
        owner = None if owner_account_id is None else str(owner_account_id or "").strip()
        keys = list(dict.fromkeys([*self._channels.keys(), *self._states.keys()]))
        rows: list[dict[str, Any]] = []
        for name, channel_owner in keys:
            if (
                owner is not None
                and channel_owner != owner
                and not (channel_owner == "" and owner in {"local", "dev:dev"})
            ):
                continue
            state = self._states.get((name, channel_owner), ChannelState())
            row: dict[str, Any] = {
                "name": name,
                "owner_account_id": channel_owner,
                "running": state.running,
                "error": state.error,
                "operation": state.operation,
                "reason": state.reason,
            }
            channel = self._channels.get((name, channel_owner))
            detail_fn = getattr(channel, "status_detail", None)
            if callable(detail_fn):
                try:
                    row["detail"] = detail_fn()
                except Exception as exc:  # noqa: BLE001 - 状态展示不应阻断请求
                    log.warning("渠道 %s owner=%s status_detail 失败: %s", name, channel_owner, exc)
            rows.append(row)
        return rows
