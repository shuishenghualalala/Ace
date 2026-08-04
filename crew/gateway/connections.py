"""WebSocket 连接管理器（含推送限流）。

维护 (owner_account_id, session_id) → Set[WebSocket] 映射，供 WS 对话、cron / 未来其它后台任务
把 ResponseChunk 或已格式化 payload 推送给前端。

推送限流（用于 GatewayStreamConsumer 的 edit_interval 与 flood control）：
  - 每个 session 的推送间隔不低于 min_interval（默认 50ms），避免客户端过载。
  - 连续推送失败时追踪 _consecutive_failures，超过阈值（3 次）暂时停止推送，
    降级为只落库（下次成功推送后重置计数）。

生命周期：
  - WS handler 每次处理一条 session 消息或订阅恢复时调 register(session_id, socket, owner_account_id=...)
  - WS 建立后调 register_owner(owner_account_id, socket)，用于账号级轻量事件
  - WS 断开时调 unregister_all(socket, registered_sessions, owner_account_id=...)
  - CronService runner 执行完每个 chunk 后调 push(session_id, chunk)
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import WebSocket

from crew.state.logging import get_logger

log = get_logger("gateway.connections")

# 连续推送失败阈值：超过此数暂停推送该 session
_MAX_CONSECUTIVE_FAILURES = 3
# 每个 session 缓存的 WS payload 上限，用于断线重连后回放
_CHUNK_BUFFER_SIZE = 2000


class ConnectionManager:
    """账号 + session_id → 活跃 WebSocket 连接集合。线程不安全，仅在 asyncio 单线程内使用。"""

    def __init__(
        self,
        *,
        min_interval: float = 0.05,
    ) -> None:
        self._conns: dict[tuple[str, str], set[WebSocket]] = defaultdict(set)
        self._owner_conns: dict[str, set[WebSocket]] = defaultdict(set)
        self._min_interval = min_interval  # 推送最小间隔（秒），0=不限流
        self._last_push_ts: dict[tuple[str, str], float] = {}  # key -> 上次推送时间戳
        self._consecutive_failures: dict[tuple[str, str], int] = {}  # key -> 连续失败次数
        self._pending_payloads: dict[tuple[str, str], list[dict]] = defaultdict(list)  # 限流期间缓存的帧
        self._flush_tasks: dict[tuple[str, str], asyncio.Task] = {}  # key -> 延迟推送 task
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}  # 同一 socket 的 send_json 必须串行
        self._ws_to_sessions: dict[WebSocket, set[tuple[str, str]]] = defaultdict(set)
        self._ws_to_owners: dict[WebSocket, set[str]] = defaultdict(set)
        # 断线回放：每个 session 的有界 payload 缓存 + 单调 gateway_sequence + 回放锁
        self._chunk_buffers: dict[tuple[str, str], deque[dict]] = defaultdict(
            lambda: deque(maxlen=_CHUNK_BUFFER_SIZE)
        )
        self._gateway_seq: dict[tuple[str, str], int] = defaultdict(int)
        self._replay_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    def _key(self, session_id: str, owner_account_id: str | None = None) -> tuple[str, str]:
        """Build the connection key from explicit owner + session."""
        return owner_account_id or "", session_id

    @staticmethod
    def _label(key: tuple[str, str]) -> str:
        """Human-readable key for logs."""
        owner, session_id = key
        return f"{owner}:{session_id}" if owner else session_id

    def register(self, session_id: str, ws: WebSocket, *, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        self._conns[key].add(ws)
        self._ws_to_sessions[ws].add(key)
        # Do not reset failures until a send actually succeeds. A sequence of
        # newly registered but already-dead sockets must still trip degradation.

    def register_owner(self, owner_account_id: str, ws: WebSocket) -> None:
        """Register an owner-level socket for lightweight account events."""
        owner = str(owner_account_id or "")
        if not owner:
            return
        self._owner_conns[owner].add(ws)
        self._ws_to_owners[ws].add(owner)

    def _unregister_owner(self, owner_account_id: str, ws: WebSocket) -> None:
        owner = str(owner_account_id or "")
        if not owner:
            return
        conns = self._owner_conns.get(owner)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                self._owner_conns.pop(owner, None)
        owners = self._ws_to_owners.get(ws)
        if owners is not None:
            owners.discard(owner)
            if not owners:
                self._ws_to_owners.pop(ws, None)
        if not self._ws_to_sessions.get(ws) and not self._ws_to_owners.get(ws):
            self._send_locks.pop(ws, None)

    def _unregister_socket_owners(self, ws: WebSocket, owner_account_id: str = "") -> None:
        owners = {str(owner_account_id or "")} if owner_account_id else set(self._ws_to_owners.get(ws, set()))
        for owner in owners:
            self._unregister_owner(owner, ws)

    def _unregister_socket_sessions(self, ws: WebSocket) -> None:
        for owner, session_id in list(self._ws_to_sessions.get(ws, set())):
            self.unregister(session_id, ws, owner_account_id=owner)

    def unregister(self, session_id: str, ws: WebSocket, *, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        self._conns[key].discard(ws)
        sessions = self._ws_to_sessions.get(ws)
        if sessions is not None:
            sessions.discard(key)
            if not sessions:
                self._ws_to_sessions.pop(ws, None)
        if not self._conns[key]:
            del self._conns[key]
            self._cleanup_session_state(key)
        if not self._ws_to_sessions.get(ws) and not self._ws_to_owners.get(ws):
            self._send_locks.pop(ws, None)

    def unregister_all(
        self,
        ws: WebSocket,
        session_ids: set[str],
        *,
        owner_account_id: str = "",
    ) -> None:
        """WS 断开时，从该连接注册的所有 session 里移除。"""
        for sid in session_ids:
            self.unregister(sid, ws, owner_account_id=owner_account_id)
        self._unregister_socket_owners(ws, owner_account_id)

    def _cleanup_session_state(
        self,
        key: tuple[str, str],
        *,
        preserve_failures: bool = False,
    ) -> None:
        """清理 session 的辅助推送状态（限流时间戳、失败计数、缓存帧、延迟 task）。

        在该 session 不再有活跃连接时调用，防止 dict 无限增长。
        _pending_payloads / _flush_tasks 正常情况下由 _flush_pending 自清理，
        此处兜底防止 flush task 被取消后残留。
        """
        self._last_push_ts.pop(key, None)
        if not preserve_failures:
            self._consecutive_failures.pop(key, None)
        flush_task = self._flush_tasks.pop(key, None)
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
        self._pending_payloads.pop(key, None)

    def has_connection(self, session_id: str, *, owner_account_id: str = "") -> bool:
        return bool(self._conns.get(self._key(session_id, owner_account_id)))

    async def send_socket(self, ws: WebSocket, payload: dict) -> None:
        """向单个 socket 串行发送 payload，供心跳等 per-connection 帧使用。"""
        lock = self._send_locks.setdefault(ws, asyncio.Lock())
        async with lock:
            await ws.send_json(payload)

    async def notify_owner(self, owner_account_id: str, payload: dict) -> None:
        """向某账号下所有已注册 WS 连接广播轻量事件（如渠道会话更新）。"""
        owner = str(owner_account_id or "")
        if not owner:
            return
        sockets: set[WebSocket] = set()
        sockets.update(self._owner_conns.get(owner, set()))
        for (own, _sid), conns in self._conns.items():
            if own == owner:
                sockets.update(conns)
        for ws in sockets:
            try:
                await self.send_socket(ws, payload)
            except Exception:  # noqa: BLE001
                self._unregister_socket_sessions(ws)
                self._unregister_socket_owners(ws, owner)
                log.debug("notify_owner 推送失败 owner=%s", owner)

    async def close_owner(
        self,
        owner_account_id: str,
        *,
        code: int = 4401,
        reason: str = "Login required",
    ) -> int:
        """Detach and close every socket and replay buffer owned by an account.

        Local mappings are removed before the network close so concurrent
        producers cannot publish logout terminal frames to the old account.
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            return 0
        sockets = set(self._owner_conns.get(owner, set()))
        for (owned_by, _session_id), conns in list(self._conns.items()):
            if owned_by == owner:
                sockets.update(conns)

        for ws in sockets:
            self._unregister_socket_sessions(ws)
            self._unregister_socket_owners(ws, owner)

        owner_keys = {
            key
            for mapping in (
                self._last_push_ts,
                self._consecutive_failures,
                self._pending_payloads,
                self._flush_tasks,
                self._chunk_buffers,
                self._gateway_seq,
                self._replay_locks,
            )
            for key in mapping
            if key[0] == owner
        }
        for key in owner_keys:
            self._cleanup_session_state(key)
            self._chunk_buffers.pop(key, None)
            self._gateway_seq.pop(key, None)
            self._replay_locks.pop(key, None)

        async def _close(ws: WebSocket) -> None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001 - mappings are already detached fail closed
                log.debug("Logout 关闭 WebSocket 失败 owner=%s", owner)

        await asyncio.gather(*(_close(ws) for ws in sockets))
        return len(sockets)

    def _build_payload(self, chunk: Any, session_id: str) -> dict:
        return {
            "kind": chunk.kind,
            "body": chunk.body,
            "is_final": chunk.is_final,
            "sequence": chunk.sequence,
            "request_id": chunk.request_id,
            "session_id": session_id,
        }

    async def _do_push(self, key: tuple[str, str], payload: dict) -> bool:
        """实际推送。返回 True 表示至少一个连接推送成功。

        调用方只在存在活跃连接时进入这里；因此 False 表示尝试发送但全部失败。
        同一 WebSocket 可能同时收到多个 session 的后台推送，按 socket 加锁避免并发
        send_json 交错破坏 ASGI WebSocket 状态机。
        """
        sockets = list(self._conns.get(key, set()))
        dead: list[WebSocket] = []
        any_ok = False
        for ws in sockets:
            try:
                lock = self._send_locks.setdefault(ws, asyncio.Lock())
                async with lock:
                    await ws.send_json(payload)
                any_ok = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._conns[key].discard(ws)
            sessions = self._ws_to_sessions.get(ws)
            if sessions is not None:
                sessions.discard(key)
            if sessions is not None and not sessions:
                self._ws_to_sessions.pop(ws, None)
            self._unregister_socket_owners(ws)
            if not self._ws_to_sessions.get(ws) and not self._ws_to_owners.get(ws):
                self._send_locks.pop(ws, None)
        if not self._conns[key]:
            self._conns.pop(key, None)
            # 发送失败导致的死连接清理需要保留失败计数，才能维持连续失败降级语义；
            # 主动 unregister 仍会清掉计数，让正常断线重连不继承旧状态。
            self._cleanup_session_state(key, preserve_failures=True)
        return any_ok

    async def _flush_pending(self, key: tuple[str, str]) -> None:
        """延迟推送：等 min_interval 后把缓存的帧一次性推送。"""
        async with self._replay_locks[key]:
            await self._flush_pending_locked(key)

    async def _flush_pending_locked(self, key: tuple[str, str]) -> None:
        """在 replay lock 保护下刷新限流缓存帧。"""
        payloads = self._pending_payloads.pop(key, [])
        self._flush_tasks.pop(key, None)
        if not payloads or not self._conns.get(key):
            return
        for payload in self._merge_pending_payloads(payloads):
            await self._do_push(key, payload)
        self._last_push_ts[key] = time.monotonic()

    def _merge_pending_payloads(self, payloads: list[dict]) -> list[dict]:
        """合并限流缓存帧。

        delta 是增量协议，不能只保留最后一帧；这里把连续 delta 文本拼成一帧，
        既减少刷屏，也保证前端追加文本时不丢 token。其它事件保持原序发送。
        """
        merged: list[dict] = []
        delta_buffer: dict | None = None
        delta_request_id: str | None = None
        delta_text = ""
        delta_start: int | None = None
        delta_end: int | None = None
        thinking_buffer: dict | None = None
        thinking_request_id: str | None = None

        def flush_delta() -> None:
            nonlocal delta_buffer, delta_request_id, delta_text, delta_start, delta_end
            if delta_buffer is None:
                return
            payload = dict(delta_buffer)
            body = dict(payload.get("body") or {})
            body["text"] = delta_text
            if delta_start is not None and delta_end is not None:
                body["delta_start"] = delta_start
                body["delta_end"] = delta_end
            payload["body"] = body
            merged.append(payload)
            delta_buffer = None
            delta_request_id = None
            delta_text = ""
            delta_start = None
            delta_end = None

        def flush_thinking() -> None:
            nonlocal thinking_buffer, thinking_request_id
            if thinking_buffer is None:
                return
            merged.append(thinking_buffer)
            thinking_buffer = None
            thinking_request_id = None

        for payload in payloads:
            if payload.get("kind") == "delta" and not payload.get("is_final", False):
                body = payload.get("body") or {}
                text = body.get("text")
                if isinstance(text, str):
                    flush_thinking()
                    request_id = payload.get("request_id")
                    request_id = request_id if isinstance(request_id, str) else None
                    if delta_buffer is not None and delta_request_id != request_id:
                        flush_delta()
                    start_raw = body.get("delta_start", payload.get("sequence"))
                    end_raw = body.get("delta_end", payload.get("sequence"))
                    try:
                        start = int(start_raw)
                        end = int(end_raw)
                    except (TypeError, ValueError):
                        start = 0
                        end = 0
                    delta_buffer = payload
                    delta_request_id = request_id
                    delta_text += text
                    if start > 0 and end > 0:
                        if delta_start is None or start < delta_start:
                            delta_start = start
                        if delta_end is None or end > delta_end:
                            delta_end = end
                    continue
            if payload.get("kind") == "thinking" and not payload.get("is_final", False):
                body = payload.get("body") or {}
                text = body.get("text")
                request_id = payload.get("request_id")
                request_id = request_id if isinstance(request_id, str) and request_id else None
                if isinstance(text, str) and request_id is not None:
                    flush_delta()
                    if thinking_buffer is not None and thinking_request_id != request_id:
                        flush_thinking()
                    # thinking 帧承载截至当前的累计全文；同一 request 的连续快照只保留
                    # 最新一帧即可，sequence/gateway_sequence 也随最新状态一起保留。
                    thinking_buffer = payload
                    thinking_request_id = request_id
                    continue
            flush_delta()
            flush_thinking()
            merged.append(payload)
        flush_delta()
        flush_thinking()
        return merged

    @staticmethod
    def _is_priority_payload(payload: dict) -> bool:
        """需要越过推送限流的用户可感知控制帧。"""
        kind = payload.get("kind")
        if kind == "tool":
            body = payload.get("body") or {}
            return body.get("phase") in {"generating", "start"}
        return kind in {"followup_question", "plan_review"}

    async def push(self, session_id: str, chunk: Any, *, owner_account_id: str = "") -> None:
        """把一个 ResponseChunk 推送给该 session 的所有活跃 WS 连接。

        推送失败（连接已关闭）时静默移除该连接，不抛异常。
        含限流：距上次推送 < min_interval 时缓存帧、延迟推送。
        """
        await self.push_payload(
            session_id,
            self._build_payload(chunk, session_id),
            owner_account_id=owner_account_id,
        )

    async def push_payload(
        self,
        session_id: str,
        payload: dict,
        *,
        owner_account_id: str = "",
    ) -> None:
        """把已格式化的 WS payload 推送给该 session 的所有活跃连接。

        WS 对话路径已经在 server.py 中完成过滤与格式化，因此这里复用限流、
        失败追踪和 socket 清理逻辑，不再反向构造 ResponseChunk。

        同时把 payload 按 session 有界缓存，并写入单调递增的 gateway_sequence，
        供断线重连后的 subscribe/resume 回放。
        """
        key = self._key(session_id, owner_account_id)

        # 分配 gateway_sequence 并入回放缓存（无论当前是否有连接）
        self._gateway_seq[key] += 1
        payload["gateway_sequence"] = self._gateway_seq[key]
        self._chunk_buffers[key].append(dict(payload))

        # 连续失败过多，降级为静默（只落库，不推送）
        if self._consecutive_failures.get(key, 0) >= _MAX_CONSECUTIVE_FAILURES:
            log.debug("推送降级（连续失败过多）session=%s", self._label(key))
            return

        # 无活跃连接不是发送失败：断线期间后台任务继续跑，但不应累计失败导致
        # 新连接注册后仍被静默降级。
        if not self._conns.get(key):
            return

        # 持 replay lock 推送，避免与 replay() 交错导致乱序
        async with self._replay_locks[key]:
            await self._push_under_lock(key, payload)

    async def _push_under_lock(self, key: tuple[str, str], payload: dict) -> None:
        """在 replay lock 保护下执行限流判断与推送。"""
        now = time.monotonic()
        last_ts = self._last_push_ts.get(key, 0)
        elapsed = now - last_ts

        # final / 用户可感知控制帧必须立即推送，不限流。若已有 pending，
        # 先按原序刷新 pending，避免 tool/start 跑到前面的 delta 之前。
        is_final = bool(payload.get("is_final", False))
        is_priority = self._is_priority_payload(payload)
        if (is_final or is_priority) and self._pending_payloads.get(key):
            flush_task = self._flush_tasks.pop(key, None)
            if flush_task is not None and not flush_task.done():
                flush_task.cancel()
            await self._flush_pending_locked(key)

        if is_priority and not is_final:
            ok = await self._do_push(key, payload)
            self._last_push_ts[key] = time.monotonic()
            if ok:
                self._consecutive_failures[key] = 0
            else:
                self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
                if self._consecutive_failures[key] >= _MAX_CONSECUTIVE_FAILURES:
                    log.warning("推送连续失败 %d 次，降级为静默 session=%s",
                                self._consecutive_failures[key], self._label(key))
            return

        if not is_final and self._pending_payloads.get(key):
            self._pending_payloads[key].append(payload)
            if self._min_interval <= 0 or elapsed >= self._min_interval:
                flush_task = self._flush_tasks.pop(key, None)
                if flush_task is not None and not flush_task.done():
                    flush_task.cancel()
                await self._flush_pending_locked(key)
            elif key not in self._flush_tasks:
                delay = self._min_interval - elapsed
                self._flush_tasks[key] = asyncio.create_task(
                    self._delayed_flush(key, delay)
                )
            return

        if not is_final and self._min_interval > 0 and elapsed < self._min_interval:
            # 限流：缓存帧，安排延迟推送
            self._pending_payloads[key].append(payload)
            if key not in self._flush_tasks:
                delay = self._min_interval - elapsed
                self._flush_tasks[key] = asyncio.create_task(
                    self._delayed_flush(key, delay)
                )
            return

        # 立即推送
        ok = await self._do_push(key, payload)
        self._last_push_ts[key] = time.monotonic()

        if ok:
            self._consecutive_failures[key] = 0
        else:
            self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
            if self._consecutive_failures[key] >= _MAX_CONSECUTIVE_FAILURES:
                log.warning("推送连续失败 %d 次，降级为静默 session=%s",
                            self._consecutive_failures[key], self._label(key))

    async def replay(
        self,
        session_id: str,
        ws: WebSocket,
        *,
        after_gateway_sequence: int = 0,
        filter_fn: Callable[[dict], bool] | None = None,
        owner_account_id: str = "",
    ) -> None:
        """向指定 socket 回放该 session 缓存中 gateway_sequence > after 的帧。

        回放与实时推送共享 replay lock，避免乱序。发送失败则停止回放。
        filter_fn 为可选过滤函数，返回 False 时跳过该帧（用于跳过已失效的
        临时交互帧，如已被回答的 followup_question）。
        """
        key = self._key(session_id, owner_account_id)
        async with self._replay_locks[key]:
            buffer = self._chunk_buffers.get(key)
            if not buffer:
                return
            # 先拍快照再逐帧发送：send_json 会让出事件循环，而 push_payload 在
            # replay lock 外向同一个 deque append，直接迭代会抛
            # "deque mutated during iteration" 并拆掉整条 WS 连接。
            # 回放期间新入缓存的帧不在快照里，但 push_payload 会等回放结束
            # 拿到锁后实时补发，顺序与完整性都不受影响。
            for payload in list(buffer):
                if payload.get("gateway_sequence", 0) <= after_gateway_sequence:
                    continue
                if filter_fn is not None and not filter_fn(payload):
                    continue
                try:
                    lock = self._send_locks.setdefault(ws, asyncio.Lock())
                    async with lock:
                        await ws.send_json(payload)
                except Exception:  # noqa: BLE001
                    # socket 已死，停止回放；外层 WS handler 会清理死连接
                    break

    def clear_buffer(self, session_id: str, *, owner_account_id: str = "") -> None:
        """新一轮请求开始时清空回放缓存，但保持 gateway_sequence 会话级单调递增。

        客户端用 gateway_sequence 做断线回放水位与实时去重。若每轮从 1 重新开始，
        plan 审批后自动启动的新执行轮会被前端误判为上一轮重复帧，导致早期 delta /
        todo_updated / plan 状态帧丢失。
        """
        key = self._key(session_id, owner_account_id)
        self._chunk_buffers.pop(key, None)

    async def _delayed_flush(self, key: tuple[str, str], delay: float) -> None:
        """等待 delay 秒后刷新缓存的帧。"""
        try:
            await asyncio.sleep(delay)
            await self._flush_pending(key)
        except asyncio.CancelledError:
            pass  # 连接断开时 task 被取消，忽略
        except Exception:  # noqa: BLE001 — 后台 flush 任务的顶层兜底，send 错误已由 _do_push 内层吞掉
            log.exception("延迟推送异常 session=%s", self._label(key))
