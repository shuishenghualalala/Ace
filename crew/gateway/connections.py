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
import json
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from fastapi import WebSocket

from crew.core.followup import get_followup_waiter
from crew.gateway.replay_journal import ReplayJournal
from crew.state.logging import get_logger

log = get_logger("gateway.connections")

# 连续推送失败阈值：超过此数暂停推送该 session
_MAX_CONSECUTIVE_FAILURES = 3
# 每个 session 缓存的 WS payload 上限，用于断线重连后回放
_CHUNK_BUFFER_SIZE = 2000
_REPLAY_SESSION_BYTES = 2 * 1024 * 1024
_REPLAY_TOTAL_BYTES = 16 * 1024 * 1024
_REPLAY_SESSIONS = 128
_MAX_PENDING_FOLLOWUPS = 8
_MAX_FOLLOWUP_SESSIONS = 128
_MAX_FOLLOWUP_FRAME_BYTES = 32_768
_MAX_ACTIVE_SNAPSHOTS = 16
_MAX_ACTIVE_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_DELTA_PARTS = 256
_MAX_WIRE_SEQUENCE = 2**53 - 1
_SNAPSHOT_KINDS = {"delta", "thinking", "tool", "file_changes", "todo_updated", "status"}
_LEGACY_CURSOR = object()


def _encode_snapshot_part(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("ascii")


def _utf16_length(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass
class _ActiveTurnSnapshot:
    request_id: str
    started_at: float
    history: bytes
    remaining: int
    frames: list[bytes] = field(default_factory=list)


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
        self._journal = ReplayJournal(_CHUNK_BUFFER_SIZE, _REPLAY_SESSION_BYTES, _REPLAY_TOTAL_BYTES, _REPLAY_SESSIONS)
        self._gateway_seq: dict[tuple[str, str], int] = defaultdict(int)
        self._replay_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._gateway_epoch = uuid4().hex
        self._owner_replay_epochs: dict[str, str] = {}
        self._pending_followups: OrderedDict[tuple[str, str], dict[str, dict]] = OrderedDict()
        self._active_snapshots: OrderedDict[tuple[str, str], _ActiveTurnSnapshot] = OrderedDict()

    def _key(self, session_id: str, owner_account_id: str | None = None) -> tuple[str, str]:
        """Build the connection key from explicit owner + session."""
        return owner_account_id or "", session_id

    @staticmethod
    def _label(key: tuple[str, str]) -> str:
        """Human-readable key for logs."""
        owner, session_id = key
        return f"{owner}:{session_id}" if owner else session_id

    def register(self, session_id: str, ws: WebSocket, *, owner_account_id: str) -> None:
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

    def _unregister_socket_owners(self, ws: WebSocket, owner_account_id: str) -> None:
        owners = {str(owner_account_id or "")} if owner_account_id else set(self._ws_to_owners.get(ws, set()))
        for owner in owners:
            self._unregister_owner(owner, ws)

    def _unregister_socket_sessions(self, ws: WebSocket) -> None:
        for owner, session_id in list(self._ws_to_sessions.get(ws, set())):
            self.unregister(session_id, ws, owner_account_id=owner)

    def unregister(self, session_id: str, ws: WebSocket, *, owner_account_id: str) -> None:
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
        owner_account_id: str,
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

    def has_connection(self, session_id: str, *, owner_account_id: str) -> bool:
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
                self._gateway_seq,
                self._replay_locks,
                self._pending_followups,
                self._active_snapshots,
            )
            for key in mapping
            if key[0] == owner
        }
        self._journal.clear_owner(owner)
        for key in owner_keys:
            self._cleanup_session_state(key)
            self._gateway_seq.pop(key, None)
            self._replay_locks.pop(key, None)
            self._pending_followups.pop(key, None)
            self._active_snapshots.pop(key, None)
        self._owner_replay_epochs[owner] = uuid4().hex

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

    async def _do_push(
        self, key: tuple[str, str], payload: dict, *, skip_socket: WebSocket | None = None
    ) -> bool:
        """实际推送。返回 True 表示至少一个连接推送成功。

        同一 WebSocket 可能同时收到多个 session 的后台推送，按 socket 加锁避免并发
        send_json 交错破坏 ASGI WebSocket 状态机。
        """
        replay_lock = self._replay_locks[key]
        sockets = list(self._conns.get(key, set()))
        dead: list[WebSocket] = []
        any_ok = False
        for ws in sockets:
            if ws is skip_socket:
                continue
            try:
                await self.send_socket(ws, payload)
                any_ok = True
            except Exception:
                dead.append(ws)
            if self._replay_locks.get(key) is not replay_lock:
                return False
        for ws in dead:
            self._conns[key].discard(ws)
            sessions = self._ws_to_sessions.get(ws)
            if sessions is not None:
                sessions.discard(key)
            if sessions is not None and not sessions:
                self._ws_to_sessions.pop(ws, None)
            self._unregister_socket_owners(ws, owner_account_id="local")
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
        lock = self._replay_locks[key]
        async with lock:
            if self._replay_locks.get(key) is lock:
                await self._flush_pending_locked(key)

    async def _flush_pending_locked(
        self, key: tuple[str, str], *, skip_socket: WebSocket | None = None
    ) -> None:
        """在 replay lock 保护下刷新限流缓存帧。"""
        payloads = self._pending_payloads.pop(key, [])
        self._flush_tasks.pop(key, None)
        if not payloads or not self._conns.get(key):
            return
        lock = self._replay_locks[key]
        for payload in self._merge_pending_payloads(payloads):
            await self._do_push(key, payload, skip_socket=skip_socket)
            if self._replay_locks.get(key) is not lock:
                return
        self._last_push_ts[key] = time.monotonic()

    def _merge_pending_payloads(self, payloads: list[dict]) -> list[dict]:
        """合并限流缓存帧。

        delta 是增量协议，不能只保留最后一帧；这里把连续 delta 文本拼成一帧，
        既减少刷屏，也保证前端追加文本时不丢 token。其它事件保持原序发送。
        """
        merged: list[dict] = []
        delta_buffer: dict | None = None
        delta_request_id: str | None = None
        delta_text: list[str] = []
        delta_start: int | None = None
        delta_end: int | None = None
        delta_parts: list[list[int]] | None = []
        thinking_buffer: dict | None = None
        thinking_request_id: str | None = None

        def flush_delta() -> None:
            nonlocal delta_buffer, delta_request_id, delta_start, delta_end, delta_parts
            if delta_buffer is None:
                return
            payload = dict(delta_buffer)
            body = dict(payload.get("body") or {})
            body["text"] = "".join(delta_text)
            if delta_start is not None and delta_end is not None:
                body["delta_start"] = delta_start
                body["delta_end"] = delta_end
            if delta_parts is not None and len(delta_parts) > 1:
                body["delta_parts"] = delta_parts
            payload["body"] = body
            merged.append(payload)
            delta_buffer = None
            delta_request_id = None
            delta_text.clear()
            delta_start = None
            delta_end = None
            delta_parts = []

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
                if isinstance(text, str) and "delta_parts" not in body:
                    flush_thinking()
                    request_id = payload.get("request_id")
                    request_id = request_id if isinstance(request_id, str) else None
                    if delta_buffer is not None and (
                        delta_request_id != request_id
                        or (delta_parts is not None and len(delta_parts) >= _MAX_DELTA_PARTS)
                    ):
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
                    delta_text.append(text)
                    if delta_parts is not None:
                        seq = payload.get("gateway_sequence")
                        if type(seq) is int and 0 < seq <= _MAX_WIRE_SEQUENCE and (
                            not delta_parts or seq > delta_parts[-1][0]
                        ):
                            delta_parts.append([seq, _utf16_length(text)])
                        else:
                            delta_parts = None
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

    async def push(self, session_id: str, chunk: Any, *, owner_account_id: str) -> None:
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
        owner_account_id: str,
    ) -> None:
        """把已格式化的 WS payload 推送给该 session 的所有活跃连接。

        出口只认 envelope，不做任何 kind 转换：feature_event 帧（含插件
        贡献的未登记事件）原样透传给客户端，回放缓存与线上帧同形态。
        """
        await self._push_payload_frame(session_id, payload, owner_account_id=owner_account_id)

    async def _push_payload_frame(
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
        lock = self._replay_locks[key]
        async with lock:
            # Logout can retire this lock while a producer is waiting for it.
            if self._replay_locks.get(key) is not lock:
                return

            self._gateway_seq[key] += 1
            payload["gateway_sequence"] = self._gateway_seq[key]
            payload["gateway_epoch"] = self._owner_replay_epochs.get(key[0], self._gateway_epoch)
            try:
                encoded = _encode_snapshot_part(payload)
            except (TypeError, ValueError):
                encoded = None
            self._journal.append(key, encoded)
            self._record_snapshot_frame(key, payload, encoded)
            if payload.get("kind") == "followup_question":
                self._remember_followup(key, payload)

            if self._consecutive_failures.get(key, 0) >= _MAX_CONSECUTIVE_FAILURES:
                log.debug("推送降级（连续失败过多）session=%s", self._label(key))
                return
            # Keep journaling while disconnected without recording a push failure.
            if self._conns.get(key):
                await self._push_under_lock(key, payload)

    async def _push_under_lock(self, key: tuple[str, str], payload: dict) -> None:
        """在 replay lock 保护下执行限流判断与推送。"""
        lock = self._replay_locks[key]
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
            if self._replay_locks.get(key) is not lock:
                return

        if is_priority and not is_final:
            ok = await self._do_push(key, payload)
            if self._replay_locks.get(key) is not lock:
                return
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
        if self._replay_locks.get(key) is not lock:
            return
        self._last_push_ts[key] = time.monotonic()

        if ok:
            self._consecutive_failures[key] = 0
        else:
            self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
            if self._consecutive_failures[key] >= _MAX_CONSECUTIVE_FAILURES:
                log.warning("推送连续失败 %d 次，降级为静默 session=%s",
                            self._consecutive_failures[key], self._label(key))

    async def capture_turn_history(
        self, owner: str, session_id: str, request_id: str, started_at: float,
        is_current: Callable[[], bool], read_history: Callable[[], dict | None],
    ) -> None:
        """Freeze the committed prefix before this request can emit generation frames."""
        key = self._key(session_id, owner)
        lock = self._replay_locks[key]

        def encode_history() -> bytes | None:
            history = read_history()
            return _encode_snapshot_part(history) if history else None

        try:
            history_bytes = await asyncio.to_thread(encode_history)
        except Exception:  # noqa: BLE001 - an unavailable snapshot must not interrupt generation
            log.debug("Could not capture turn history session=%s", self._label(key), exc_info=True)
            return
        async with lock:
            if self._replay_locks.get(key) is not lock or not is_current():
                return
            self._active_snapshots.pop(key, None)
            if history_bytes is None or len(history_bytes) >= _MAX_ACTIVE_SNAPSHOT_BYTES:
                return
            if len(self._active_snapshots) >= _MAX_ACTIVE_SNAPSHOTS:
                self._active_snapshots.popitem(last=False)
            self._active_snapshots[key] = _ActiveTurnSnapshot(
                request_id, started_at, history_bytes, _MAX_ACTIVE_SNAPSHOT_BYTES - len(history_bytes),
            )

    def _record_snapshot_frame(self, key: tuple[str, str], payload: dict, encoded: bytes | None) -> None:
        snapshot = self._active_snapshots.get(key)
        if snapshot is None or payload.get("request_id") != snapshot.request_id:
            return
        kind = payload.get("kind")
        if kind == "followup_question" and (payload.get("body") or {}).get("answer_history_id"):
            # Recorded user input changes the transcript boundary. The frozen
            # prefix cannot represent the new input and its later live output.
            self._active_snapshots.pop(key, None)
            return
        if payload.get("is_final") or kind in ("final", "error"):
            self._active_snapshots.pop(key, None)
            return
        if kind not in _SNAPSHOT_KINDS or (kind == "status" and (payload.get("body") or {}).get("control")):
            return
        if encoded is None or len(encoded) > snapshot.remaining:
            self._active_snapshots.pop(key, None)
            return
        snapshot.remaining -= len(encoded)
        snapshot.frames.append(encoded)

    def _remember_followup(self, key: tuple[str, str], payload: dict) -> None:
        """Keep still-waiting prompts available even if their replay frames expire."""
        body = payload.get("body") or {}
        if not isinstance(body, dict):
            return
        question_id = body.get("question_id")
        if payload.get("session_id") != key[1] or not isinstance(question_id, str) or not question_id:
            return
        waiting = get_followup_waiter().is_waiting
        prompts = {
            qid: frame for qid, frame in self._pending_followups.get(key, {}).items()
            if waiting(key[1], qid, owner_account_id=key[0])
        }
        prompts.pop(question_id, None)
        if (
            body.get("questions")
            and body.get("status") in (None, "pending")
            and waiting(key[1], question_id, owner_account_id=key[0])
            and len(json.dumps(payload)) <= _MAX_FOLLOWUP_FRAME_BYTES
        ):
            if len(prompts) >= _MAX_PENDING_FOLLOWUPS:
                prompts.pop(next(iter(prompts)))
            prompts[question_id] = dict(payload)
        if prompts:
            self._pending_followups[key] = prompts
            self._pending_followups.move_to_end(key)
            if len(self._pending_followups) > _MAX_FOLLOWUP_SESSIONS:
                self._pending_followups.popitem(last=False)
        else:
            self._pending_followups.pop(key, None)

    @staticmethod
    def _check_resume_cursor(cursor: object, epoch: str, floor: int, head: int) -> tuple[str | None, int]:
        if not isinstance(cursor, dict):
            return "invalid_cursor", 0
        cursor_epoch = cursor.get("epoch")
        sequence = cursor.get("sequence")
        if not isinstance(cursor_epoch, str) or not cursor_epoch or type(sequence) is not int or sequence < 0:
            return "invalid_cursor", 0
        if cursor_epoch != epoch:
            return "epoch_changed", 0
        if sequence < floor:
            return "cursor_expired", 0
        if sequence > head:
            return "cursor_ahead", 0
        return None, sequence

    def _resync_frame(
        self, key: tuple[str, str], epoch: str, floor: int, head: int, reason: str,
        active_request_id: str | None = None,
        *, repair_id: str | None = None,
    ) -> dict:
        waiting = get_followup_waiter().is_waiting
        prompts = self._pending_followups.get(key, {})
        for qid in tuple(prompts):
            if not waiting(key[1], qid, owner_account_id=key[0]):
                del prompts[qid]
        if not prompts:
            self._pending_followups.pop(key, None)
        body: dict[str, Any] = {"reason": reason, "floor": floor}
        if repair_id is not None:
            body["repair_id"] = repair_id
        if prompts:
            body["pending_followups"] = list(prompts.values())
        snapshot = self._active_snapshots.get(key)
        if snapshot is not None and snapshot.request_id == active_request_id:
            body["turn_snapshot"] = {
                "request_id": snapshot.request_id,
                "started_at": snapshot.started_at,
                "history": json.loads(snapshot.history),
                "frames": self._merge_pending_payloads([json.loads(frame) for frame in snapshot.frames]),
            }
        return {
            "kind": "gateway_resync",
            "session_id": key[1],
            "gateway_epoch": epoch,
            "gateway_sequence": head,
            "sequence": 0,
            "is_final": False,
            "body": body,
        }

    async def replay(
        self,
        session_id: str,
        ws: WebSocket,
        *,
        after_gateway_sequence: int = 0,
        filter_fn: Callable[[dict], bool] | None = None,
        owner_account_id: str,
        register_for_live: bool = False,
        resume_cursor: object = _LEGACY_CURSOR,
        snapshot_request_id: Callable[[], str | None] | None = None,
        repair_id: str | None = None,
    ) -> bool:
        """向指定 socket 回放该 session 缓存中 gateway_sequence > after 的帧。

        register_for_live 在同一个锁内接续实时订阅，避免新帧越过回放。
        resume_cursor 失效或客户端主动修复时，以当前水位发 resync，跳过残缺后缀。
        仅在所选帧发送成功且账号仍连接时返回 True。
        filter_fn 为可选过滤函数，返回 False 时跳过该帧（用于跳过已失效的
        临时交互帧，如已被回答的 followup_question）。
        """
        key = self._key(session_id, owner_account_id)
        lock = self._replay_locks[key]
        async with lock:
            if self._replay_locks.get(key) is not lock:
                return False
            head = self._gateway_seq.get(key, 0)
            floor = head - self._journal.count(key)
            epoch = self._owner_replay_epochs.get(key[0], self._gateway_epoch)
            reason = None
            if repair_id is not None:
                reason = "client_stream_gap"
            elif resume_cursor is not _LEGACY_CURSOR:
                reason, after_gateway_sequence = self._check_resume_cursor(resume_cursor, epoch, floor, head)
            elif (
                snapshot_request_id is not None
                and after_gateway_sequence < floor
                and snapshot_request_id() is not None
            ):
                # A snapshot-capable observer without a cursor also needs the
                # full prefix when the log starts partway through this turn.
                reason = "cursor_expired"
            frames = {} if reason else {
                payload["gateway_sequence"]: payload for payload in self._journal.after(key, head, after_gateway_sequence)
            }
            pending = self._pending_payloads.get(key)
            live_sequences: set[int] = set()
            if register_for_live and pending:
                # The pending batch is already journaled. A re-subscribing socket
                # still needs pending live frames that fell out of the journal.
                if reason is None and ws in self._conns.get(key, ()):
                    for payload in pending:
                        seq = payload["gateway_sequence"]
                        frames[seq] = payload
                        live_sequences.add(seq)
                flush_task = self._flush_tasks.pop(key, None)
                if flush_task is not None:
                    flush_task.cancel()
                await self._flush_pending_locked(key, skip_socket=ws)

            sent = False
            if reason:
                if self._replay_locks.get(key) is not lock:
                    return False
                try:
                    active_request_id = snapshot_request_id() if snapshot_request_id is not None else None
                    await self.send_socket(ws, self._resync_frame(
                        key, epoch, floor, head, reason, active_request_id, repair_id=repair_id,
                    ))
                except Exception:  # noqa: BLE001
                    return False
                sent = True
            for seq, payload in sorted(frames.items()):
                if self._replay_locks.get(key) is not lock:
                    return False
                if seq not in live_sequences and filter_fn is not None and not filter_fn(payload):
                    continue
                try:
                    await self.send_socket(ws, payload)
                except Exception:  # noqa: BLE001
                    return False
                sent = True

            if self._replay_locks.get(key) is not lock:
                return False
            if register_for_live and sent:
                self._consecutive_failures.pop(key, None)
            if register_for_live:
                self.register(session_id, ws, owner_account_id=owner_account_id)
            return True

    def clear_buffer(self, session_id: str, *, owner_account_id: str) -> None:
        """新一轮请求开始时清空回放缓存，但保持 gateway_sequence 会话级单调递增。

        客户端用 gateway_sequence 做断线回放水位与实时去重。若每轮从 1 重新开始，
        plan 审批后自动启动的新执行轮会被前端误判为上一轮重复帧，导致早期 delta /
        todo_updated / plan 状态帧丢失。
        """
        key = self._key(session_id, owner_account_id)
        self._journal.clear(key)

    async def _delayed_flush(self, key: tuple[str, str], delay: float) -> None:
        """等待 delay 秒后刷新缓存的帧。"""
        try:
            await asyncio.sleep(delay)
            await self._flush_pending(key)
        except asyncio.CancelledError:
            pass  # 连接断开时 task 被取消，忽略
        except Exception:  # noqa: BLE001 — 后台 flush 任务的顶层兜底，send 错误已由 _do_push 内层吞掉
            log.exception("延迟推送异常 session=%s", self._label(key))
