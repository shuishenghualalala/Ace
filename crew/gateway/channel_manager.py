"""渠道管理器：注册并启动多个接入渠道。

当前内置 WebSocket 渠道（在 ws.py）。新增渠道（飞书/钉钉等）= 实现 core.Channel
并在此注册，统一通过同一个 MessageHandler 进内核 —— 对照 Jiuwen ChannelManager。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from crew.core.interfaces import Channel, MessageHandler
from crew.gateway.hooks import hook_registry
from crew.state.logging import get_logger

log = get_logger("gateway")


@dataclass
class ChannelState:
    running: bool = False
    error: str = ""
    operation: str = ""
    reason: str = ""


class ChannelManager:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._owners: dict[str, str] = {}
        self._states: dict[str, ChannelState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def channels(self) -> MappingProxyType[str, Channel]:
        """已注册渠道的只读视图（投递接线/事件分发按名查找用，禁止外部改字典）。"""
        return MappingProxyType(self._channels)

    def register(self, channel: Channel, *, owner_account_id: str = "") -> None:
        """Register a channel and its optional owning Gateway account."""
        self._channels[channel.name] = channel
        self._owners[channel.name] = str(owner_account_id or "").strip()
        self._states.setdefault(channel.name, ChannelState())
        log.info("注册渠道: %s", channel.name)

    def unregister(self, name: str) -> None:
        """移除一个渠道及其运行态，供禁用/热替换失败回滚使用。"""
        self._channels.pop(name, None)
        self._owners.pop(name, None)
        self._states.pop(name, None)

    def record_error(self, name: str, error: str) -> None:
        self._states[name] = ChannelState(running=False, error=error, reason="error")
        try:
            asyncio.get_running_loop().create_task(
                self._emit_state_change(name, running=False, error=error)
            )
        except RuntimeError:
            pass

    def is_busy(self, name: str) -> bool:
        """返回渠道是否正在执行 connect/reconnect/disconnect/delete 等互斥操作。"""
        lock = self._locks.get(name)
        return bool(lock and lock.locked())

    def lock_for(self, name: str) -> asyncio.Lock:
        """返回指定渠道的互斥锁，供配置写入与生命周期操作共享同一串行边界。"""
        return self._locks.setdefault(name, asyncio.Lock())

    async def _emit_state_change(self, name: str, *, running: bool, error: str = "") -> None:
        await hook_registry.emit("channel:state_change", {
            "name": name,
            "running": running,
            "error": error,
        })

    async def start_all(self, handler: MessageHandler, *, owner_account_id: str = "") -> None:
        """Start global channels plus channels owned by the Active Owner."""
        active_owner = str(owner_account_id or "").strip()
        for ch in self._channels.values():
            channel_owner = self._owners.get(ch.name, "")
            if channel_owner and channel_owner != active_owner:
                continue
            state = self._states.setdefault(ch.name, ChannelState())
            state.operation = "connecting"
            state.reason = ""
            try:
                await ch.start(handler)
                state.running = True
                state.error = ""
                log.info("渠道已启动: %s", ch.name)
                await self._emit_state_change(ch.name, running=True, error="")
            except Exception as exc:  # noqa: BLE001 — 渠道 start 为平台网络连接，失败面未知；启动循环按渠道隔离
                state.running = False
                state.error = str(exc)
                state.reason = "error"
                log.exception("渠道启动失败: %s", ch.name)
                await self._emit_state_change(ch.name, running=False, error=str(exc))
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
        """停止并替换单个渠道，然后启动新实例。

        该方法只影响指定渠道，不触碰 CrewApp、dispatcher 或其它渠道。旧渠道先停止再
        替换，避免同一外部身份在新旧连接间短暂双占用。
        """
        lock = self.lock_for(name)
        async with lock:
            state = self._states.setdefault(name, ChannelState())
            state.operation = "reconnecting"
            old = self._channels.get(name)
            old_owner = self._owners.get(name, "")
            old_state = self._states.get(name, ChannelState())
            if old is not None:
                stop = getattr(old, "stop", None)
                try:
                    if callable(stop):
                        await stop()
                except Exception as exc:  # noqa: BLE001 — 旧渠道停止失败仍继续尝试新配置，避免配置无法修复坏连接
                    log.warning("渠道热重连时停止旧实例失败: %s: %s", name, exc)

            self._channels[name] = channel
            self._owners[name] = str(owner_account_id or "").strip()
            try:
                await channel.start(handler)
                state.running = True
                state.error = ""
                state.operation = ""
                state.reason = ""
                log.info("渠道已热重连: %s", name)
                await self._emit_state_change(name, running=True, error="")
            except Exception as exc:  # noqa: BLE001 — 新渠道启动失败需保留错误状态并回滚旧实例引用
                self._channels.pop(name, None)
                self._owners.pop(name, None)
                if old is not None:
                    self._channels[name] = old
                    self._owners[name] = old_owner
                    old_state.running = False
                    old_state.error = str(exc)
                    old_state.operation = ""
                    old_state.reason = "error"
                    self._states[name] = old_state
                else:
                    state.running = False
                    state.error = str(exc)
                    state.operation = ""
                    state.reason = "error"
                log.exception("渠道热重连失败: %s", name)
                await self._emit_state_change(name, running=False, error=str(exc))
                return self._states[name]
            return state

    async def stop_one_locked(self, name: str, *, operation: str = "disconnecting") -> ChannelState:
        """停止并移除单个渠道；调用方必须已经持有 ``lock_for(name)``。"""
        state = self._states.setdefault(name, ChannelState())
        state.operation = operation
        channel = self._channels.pop(name, None)
        self._owners.pop(name, None)
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
            log.exception("渠道停止失败: %s", name)
        finally:
            state.operation = ""
        await self._emit_state_change(name, running=False, error=state.error)
        return state

    async def stop_one(self, name: str) -> ChannelState:
        """停止并移除单个渠道，主要用于运行时禁用平台。"""
        lock = self.lock_for(name)
        async with lock:
            return await self.stop_one_locked(name)

    async def stop_all(self, *, reason: str = "disconnected") -> list[str]:
        """Stop every registered channel and return names that failed to stop."""
        failed: list[str] = []
        for ch in reversed(list(self._channels.values())):
            state = self._states.setdefault(ch.name, ChannelState())
            # core.Channel 只约定 start()；stop() 是渠道可选实现，鸭子类型按需调用，
            # 避免为生命周期停止去改动已冻结的 core 接口层。
            stop = getattr(ch, "stop", None)
            try:
                if callable(stop):
                    await stop()
                state.error = ""
            except Exception as exc:  # noqa: BLE001 — 渠道 stop 为可选平台生命周期方法，失败面未知；停止循环按渠道隔离
                state.error = str(exc)
                state.reason = "error"
                failed.append(ch.name)
                log.exception("渠道停止失败: %s", ch.name)
            finally:
                state.running = False
                if not state.error:
                    state.reason = reason
                await self._emit_state_change(ch.name, running=False, error=state.error)
        return failed

    def status(self) -> list[dict[str, Any]]:
        names = list(dict.fromkeys([*self._channels.keys(), *self._states.keys()]))
        rows: list[dict[str, Any]] = []
        for name in names:
            state = self._states.get(name, ChannelState())
            row: dict[str, Any] = {
                "name": name,
                "running": state.running,
                "error": state.error,
                "operation": state.operation,
                "reason": state.reason,
            }
            # 渠道可选提供 status_detail() 连接快照，鸭子类型取用，不改 core.Channel。
            channel = self._channels.get(name)
            detail_fn = getattr(channel, "status_detail", None)
            if callable(detail_fn):
                try:
                    row["detail"] = detail_fn()
                except Exception as exc:  # noqa: BLE001 — status_detail 为渠道鸭子类型可选方法，失败面未知；状态展示不应阻断
                    log.warning("渠道 %s status_detail 失败: %s", name, exc)
            rows.append(row)
        return rows
