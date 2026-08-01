"""Authenticated RPC broker between the gateway and the Electron browser host.

The Electron main process connects *to* the gateway and registers itself as the
browser host for one authenticated account.  Browser tools never receive the
host socket and the renderer never receives a CDP endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


class ElectronBridgeError(RuntimeError):
    """A browser-host transport or remote execution failure."""

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        browser_stopped: bool = False,
        stop_unconfirmed: bool = False,
        retryable: bool = False,
        request_sent: bool = False,
        connection_generation: int = 0,
        remote_terminal: bool = False,
        code: str = "",
    ) -> None:
        super().__init__(message)
        # Host 的稳定错误码（如 stale_ref_security），供上层区分可恢复失败。
        self.code = code
        self.uncertain = uncertain
        self.browser_stopped = browser_stopped
        self.stop_unconfirmed = stop_unconfirmed
        # True 表示这些标志来自 Host 的确定答复（我们知道那次动作的终局），
        # False 表示我们从未拿到答复（超时/失联，只能自己判定）。
        self.remote_terminal = remote_terminal
        # Transport-only metadata. BrowserManager intentionally sees only the
        # lifecycle flags above; the bridge uses these fields for one bounded,
        # read-only retry after an actual Host epoch change.
        self.retryable = retryable
        self.request_sent = request_sent
        self.connection_generation = connection_generation


class ElectronBridgeCancelled(asyncio.CancelledError):
    """Cancellation that waited for a sent mutation's remote terminal result."""

    def __init__(self, error: ElectronBridgeError) -> None:
        super().__init__(str(error))
        self.uncertain = error.uncertain
        self.browser_stopped = error.browser_stopped
        # Host 给出了确定的终局结果 → 采信它（我们知道那次动作到底怎么了）。
        # 没拿到答复（超时/失联）→ 这次已发出的 mutation 是否执行过永远无法确认，
        # fail-stop。不能继承超时错误的 stop_unconfirmed=False——那是为「纯超时不该
        # 锁 Profile」设的，而这里叠加了「调用方放弃了一个已发出的 mutation」。
        self.stop_unconfirmed = (
            error.stop_unconfirmed if error.remote_terminal else True
        )


@dataclass
class _PendingRequest:
    future: asyncio.Future[dict[str, Any]]
    mutating: bool
    sent: bool = False


@dataclass
class _AttemptState:
    sent: bool = False


@dataclass
class _HostConnection:
    socket: WebSocket
    generation: int
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _PendingRequest] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    initialization_error: ElectronBridgeError | None = None
    connected_at: float = field(default_factory=time.monotonic)


RegistrationCallback = Callable[[], Awaitable[None] | None]
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class ElectronBrowserBridge:
    """Route one account's browser RPC calls to its connected Electron host."""

    _MAX_EVENT_RECORD_BYTES = 8 * 1024

    def __init__(self) -> None:
        self._connections: dict[str, _HostConnection] = {}
        self._generations: dict[str, int] = {}

    @staticmethod
    def _runtime_key(value: str) -> str:
        key = str(value or "").strip()
        if re.fullmatch(r"crew_[0-9a-f]{12}", key) is None:
            raise ElectronBridgeError("无效的桌面浏览器账号标识")
        return key

    def connected(self, runtime_key: str | None = None) -> bool:
        if runtime_key is None:
            return any(
                connection.ready.is_set() and connection.initialization_error is None
                for connection in self._connections.values()
            )
        try:
            key = self._runtime_key(runtime_key)
        except ElectronBridgeError:
            return False
        connection = self._connections.get(key)
        return bool(
            connection is not None
            and connection.ready.is_set()
            and connection.initialization_error is None
        )

    @property
    def connected_count(self) -> int:
        return sum(
            connection.ready.is_set() and connection.initialization_error is None
            for connection in self._connections.values()
        )

    async def serve(
        self,
        socket: WebSocket,
        runtime_key: str,
        *,
        on_registered: RegistrationCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        """Attach one authenticated main-process WebSocket until disconnect.

        A re-registered Host is not made available to ordinary tool calls until
        ``on_registered`` has completed the account epoch reset.  The callback
        may issue requests on this socket with ``_allow_unready=True``.
        """

        key = self._runtime_key(runtime_key)
        await socket.accept()
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        connection = _HostConnection(socket, generation)
        previous = self._connections.get(key)
        self._connections[key] = connection
        if previous is not None:
            self._fail_pending(previous, "桌面浏览器宿主已重新连接")
            try:
                await previous.socket.close(code=1012, reason="browser-host-replaced")
            except Exception:
                pass

        registration_task: asyncio.Task[None] | None = None

        async def initialize() -> None:
            try:
                if on_registered is not None:
                    result = on_registered()
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connection.initialization_error = ElectronBridgeError(
                    f"桌面浏览器宿主初始化失败：{str(exc)[:500]}",
                    retryable=True,
                    connection_generation=generation,
                )
            finally:
                connection.ready.set()
            if connection.initialization_error is not None:
                try:
                    await socket.close(code=1011, reason="browser-host-initialization-failed")
                except Exception:
                    pass

        if on_registered is None:
            connection.ready.set()
        else:
            # The receive loop must already be running while initialization
            # sends the idempotent close_owner RPC on this same socket.
            registration_task = asyncio.create_task(initialize())

        try:
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "event":
                    event = self._bounded_debug_event(message.get("event"))
                    if event is not None and on_event is not None:
                        try:
                            result = on_event(event)
                            if inspect.isawaitable(result):
                                await result
                        except Exception:
                            # Host events are observational. A malformed event
                            # consumer must never tear down the control channel.
                            pass
                    continue
                if message.get("type") != "response":
                    continue
                request_id = str(message.get("id") or "")
                pending = connection.pending.pop(request_id, None)
                if pending is None or pending.future.done():
                    continue
                pending.future.set_result(message)
        except WebSocketDisconnect:
            pass
        finally:
            if self._connections.get(key) is connection:
                self._connections.pop(key, None)
            self._fail_pending(connection, "桌面浏览器宿主已断开")
            if registration_task is not None and not registration_task.done():
                registration_task.cancel()
                await asyncio.gather(registration_task, return_exceptions=True)

    @classmethod
    def _bounded_debug_event(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("type") != "debug":
            return None
        channel = value.get("channel")
        target_id = value.get("targetId")
        record = value.get("record")
        if (
            channel not in {"console", "network"}
            or not isinstance(target_id, str)
            or not target_id
            or len(target_id) > 256
            or not isinstance(record, dict)
        ):
            return None
        try:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError):
            return None
        if len(encoded) > cls._MAX_EVENT_RECORD_BYTES:
            record = {"truncated": True}
        return {
            "type": "debug",
            "channel": channel,
            "targetId": target_id,
            "record": record,
        }

    @staticmethod
    def _fail_pending(connection: _HostConnection, message: str) -> None:
        for pending in connection.pending.values():
            if pending.future.done():
                continue
            if pending.mutating and pending.sent:
                error = ElectronBridgeError(
                    message,
                    uncertain=True,
                    stop_unconfirmed=True,
                    request_sent=True,
                    connection_generation=connection.generation,
                )
            else:
                error = ElectronBridgeError(
                    message,
                    retryable=True,
                    request_sent=pending.sent,
                    connection_generation=connection.generation,
                )
            pending.future.set_exception(error)
        connection.pending.clear()

    async def _wait_until_ready(
        self,
        key: str,
        connection: _HostConnection,
        *,
        timeout: float,
        allow_unready: bool,
    ) -> None:
        if allow_unready:
            return
        try:
            await asyncio.wait_for(connection.ready.wait(), timeout=max(0.01, timeout))
        except asyncio.TimeoutError as exc:
            raise ElectronBridgeError(
                "桌面浏览器宿主仍在初始化，请稍后重试",
                retryable=True,
                connection_generation=connection.generation,
            ) from exc
        if self._connections.get(key) is not connection:
            raise ElectronBridgeError(
                "桌面浏览器宿主连接已切换",
                retryable=True,
                connection_generation=connection.generation,
            )
        if connection.initialization_error is not None:
            raise connection.initialization_error

    async def _attempt(
        self,
        key: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        mutating: bool,
        allow_unready: bool,
        state: _AttemptState,
    ) -> Any:
        connection = self._connections.get(key)
        if connection is None:
            raise ElectronBridgeError(
                "桌面内置浏览器尚未连接；请打开 Crew 桌面应用并保持登录",
                retryable=True,
                connection_generation=self._generations.get(key, 0),
            )
        deadline = time.monotonic() + max(0.01, timeout)

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise asyncio.TimeoutError
            return value

        await self._wait_until_ready(
            key,
            connection,
            timeout=remaining(),
            allow_unready=allow_unready,
        )
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingRequest(future=future, mutating=mutating)
        connection.pending[request_id] = pending
        try:
            async def send() -> None:
                async with connection.send_lock:
                    if self._connections.get(key) is not connection:
                        raise ElectronBridgeError(
                            "桌面浏览器宿主连接已切换",
                            retryable=True,
                            connection_generation=connection.generation,
                        )
                    # Mark before the await. If send_json raises or times out,
                    # the transport cannot prove that a mutation was not received.
                    pending.sent = True
                    state.sent = True
                    await connection.socket.send_json(
                        {
                            "type": "request",
                            "id": request_id,
                            "runtime_key": key,
                            "method": str(method or "")[:80],
                            "params": params,
                        }
                    )

            # One absolute deadline covers queueing for the socket, the actual
            # WebSocket write and the response. Otherwise a blocked send_json
            # would make a cancelled sent mutation wait forever in request().
            await asyncio.wait_for(send(), timeout=remaining())
            response = await asyncio.wait_for(future, timeout=remaining())
        except asyncio.TimeoutError as exc:
            raise ElectronBridgeError(
                "桌面浏览器操作超时；若动作可能已发送，请重新观察页面",
                uncertain=bool(mutating and pending.sent),
                # 超时 ≠ 关停未确认。stop_unconfirmed 的语义是「无法确认浏览器已关闭」，
                # 它会锁住 Profile 并要求重启应用；而这里 socket 仍然活着，只是响应慢
                # （大页面、慢网络都会）。把超时也标成 stop_unconfirmed，会让一次点击
                # 超时就 fence 整个账号、清空所有会话的标签页。动作结果的不确定性由
                # uncertain 承载即可，manager 会据此只作废本会话的观察。
                # 连接断开与用户取消仍然置位 stop_unconfirmed——那两种是真的失联。
                request_sent=pending.sent,
                connection_generation=connection.generation,
            ) from exc
        except ElectronBridgeError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if mutating and pending.sent:
                raise ElectronBridgeError(
                    "桌面浏览器通信失败；变更操作结果未知",
                    uncertain=True,
                    stop_unconfirmed=True,
                    request_sent=True,
                    connection_generation=connection.generation,
                ) from exc
            raise ElectronBridgeError(
                "桌面浏览器通信失败",
                retryable=True,
                request_sent=pending.sent,
                connection_generation=connection.generation,
            ) from exc
        finally:
            connection.pending.pop(request_id, None)

        if response.get("ok") is not True:
            error = str(response.get("error") or "桌面浏览器操作失败")[:1000]
            raise ElectronBridgeError(
                error,
                uncertain=bool(response.get("uncertain")),
                browser_stopped=bool(response.get("browser_stopped")),
                stop_unconfirmed=bool(response.get("stop_unconfirmed")),
                request_sent=True,
                connection_generation=connection.generation,
                remote_terminal=True,
                code=str(response.get("code") or "")[:64],
            )
        return response.get("result")

    async def request(
        self,
        runtime_key: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        mutating: bool = False,
        retry_readonly: bool = False,
        _allow_unready: bool = False,
    ) -> Any:
        """Send one Host RPC with lifecycle-aware cancellation and retry rules."""

        key = self._runtime_key(runtime_key)
        total_timeout = max(0.1, float(timeout))
        state = _AttemptState()

        async def run() -> Any:
            # Every WebSocket registration is a new Host epoch. Manager state
            # is reset before that connection becomes ready, so replaying an
            # old read across epochs would both deadlock on owner.lock and read
            # a page that no longer exists. ``retry_readonly`` only documents
            # that the caller may issue a *new* explicit observation afterward.
            return await self._attempt(
                key,
                method,
                params,
                timeout=total_timeout,
                mutating=mutating,
                allow_unready=_allow_unready,
                state=state,
            )

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task) if mutating else await task
        except asyncio.CancelledError:
            if not mutating or not state.sent:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            # A sent mutation owns BrowserManager's account lock until the Host
            # confirms it or the bounded timeout produces fail-stop metadata.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Repeated caller cancellation still cannot abandon a
                    # possibly executed mutation.
                    continue
                except ElectronBridgeError:
                    break
            try:
                task.result()
            except ElectronBridgeError as exc:
                # Keep the remote lifecycle outcome without consuming the
                # user's cancellation and starting another LLM iteration.
                raise ElectronBridgeCancelled(exc) from None
            raise


electron_browser_bridge = ElectronBrowserBridge()
