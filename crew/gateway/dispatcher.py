"""会话调度器：对同一 session 串行化执行 + 暴露运行状态与排队深度。

用于 gateway 的 per-session FIFO（gateway/run.py 的 _queued_events / _queue_depth /
_running guard）与运行态状态字段（gateway/session.py 的 SessionEntry）。Crew 那套深度
耦合其多平台 adapter 架构，这里只复用「per-session 串行 + 队列深度 + 状态词汇」的设计，
用 Crew 原生 asyncio 重写。

忙时策略（用于 _busy_input_mode）：
  - queue（默认）：新消息排队，等当前运行结束后串行执行。
  - interrupt：新消息取消当前运行的 task，立即执行新消息；被中断者收到 error 帧。
  - steer：向运行中的 agent 注入补充指令（需 inner 支持 steer() 方法，否则降级为 queue）。

生命周期钩子触发点：
  - session:start: 新会话创建时（首次消息）
  - agent:start: Agent 开始处理消息
  - agent:end: Agent 完成处理（成功或失败）
"""

from __future__ import annotations

import asyncio
import enum
import json
from pathlib import Path
from typing import AsyncIterator, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ProviderError, ToolError
from crew.core.runctx import current_owner_account_id
from crew.core.interfaces import MessageHandler, SessionStore
from crew.core.types import Message
from crew.gateway.hooks import hook_registry
from crew.gateway.outbound import enrich_error_chunk
from crew.state.logging import get_logger

log = get_logger("gateway.dispatcher")
SessionKey = tuple[str, str]


class BusyMode(enum.Enum):
    """忙时策略：当同一 session 正在运行时，新到达消息的处理方式。"""
    QUEUE = "queue"        # 排队等当前运行结束（默认）
    INTERRUPT = "interrupt"  # 取消当前运行，立即执行新消息
    STEER = "steer"        # 注入到运行中的 agent（需 inner 支持 steer）


class SessionDispatcher:
    """包裹内核入口（CrewApp.handle），对同一会话串行化并跟踪状态。"""

    def __init__(
        self,
        inner: MessageHandler,
        store: SessionStore,
        *,
        busy_mode: BusyMode = BusyMode.QUEUE,
        controller: object | None = None,
        max_active_runs: int = 0,
        max_queue_depth_per_session: int = 0,
        active_children_fn: Callable[..., object] | None = None,
        task_runtime: object | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._busy_mode = busy_mode
        # 可控性目标（CrewApp）：暴露 steer(session_id, text) / interrupt(session_id, msg)。
        # 有它时，steer 走运行中 Agent 的 TurnControl 实时注入；interrupt 走协作式优雅中断
        # （保留历史完整）。无它时退回旧行为：steer 缓存到下一轮、interrupt 硬取消 task。
        self._controller = controller
        self._max_active_runs = max(0, int(max_active_runs or 0))
        self._max_queue_depth_per_session = max(0, int(max_queue_depth_per_session or 0))
        self._global_semaphore = asyncio.Semaphore(self._max_active_runs) if self._max_active_runs > 0 else None
        self._global_waiting: dict[str, int] = {}
        self._global_running: set[str] = set()
        self._active_children_fn = active_children_fn
        self._task_runtime = task_runtime
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._waiting: dict[SessionKey, int] = {}
        self._running: set[SessionKey] = set()
        self._running_counts: dict[SessionKey, int] = {}
        self._running_task: dict[SessionKey, asyncio.Task] = {}
        self._running_request_ids: dict[SessionKey, str] = {}
        self._waiting_request_ids: dict[SessionKey, list[str]] = {}
        self._run_task_ids: dict[SessionKey, str] = {}
        self._active_exec_session_ids: dict[SessionKey, str] = {}
        self._tasks: dict[SessionKey, set[asyncio.Task]] = {}
        self._stop_reasons: dict[SessionKey, str] = {}
        self._steer_texts: dict[SessionKey, str] = {}
        # Logout is an owner-wide generation fence.  Old turns keep the epoch
        # captured at admission, so even if cancellation cleanup is slow they
        # can never emit another frame after the owner logs in again.
        self._owner_epochs: dict[str, int] = {}
        self._blocked_owners: set[str] = set()
        self._closed = False
        # 已触发过 session:start 的会话（每个会话生命周期内只发一次「新会话开始」）。
        self._sessions_started: set[SessionKey] = set()
        # session:end 时回收本会话的 start 标记与 task 集合，避免：
        #   1) _sessions_started / _tasks 无界增长（_cleanup_if_idle 不清理这两项）；
        #   2) 同一 session_id 被删除后复用时 session:start 不再触发（标记残留）。
        # hook_registry 是 gateway 单例，与 routers/sessions.py 的 session:end 发射点同源。
        self._on_session_end = self._handle_session_end
        hook_registry.register("session:end", self._on_session_end)

    def _handle_session_end(self, event_type: str, context: dict) -> None:
        """session:end 钩子：清掉本会话状态并回收 transient security authority。"""
        sid = str((context or {}).get("session_id") or "")
        if not sid:
            return
        owner = str((context or {}).get("owner_account_id") or "")
        keys = [self._key(sid, owner)] if owner else [key for key in self._sessions_started if key[1] == sid]
        for key in keys:
            self._sessions_started.discard(key)
            self._tasks.pop(key, None)
            self._active_exec_session_ids.pop(key, None)
        security_service = getattr(self._controller, "security_service", None)
        end_session = getattr(security_service, "end_session", None)
        if owner and callable(end_session):
            end_session(owner, sid)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(session_id: str, owner_account_id: str = "") -> SessionKey:
        return owner_account_id, session_id

    @staticmethod
    def _label(key: SessionKey) -> str:
        return f"{key[0]}:{key[1]}" if key[0] else key[1]

    def _resolve_key(self, session_id: str, owner_account_id: str = "") -> SessionKey:
        """Return the explicit owner-scoped dispatcher key."""

        return self._key(session_id, owner_account_id)

    def _lock_for(self, key: SessionKey) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _cleanup_if_idle(self, key: SessionKey) -> None:
        """无等待且未运行时回收锁与计数，防止 dict 无限增长。"""
        if self._waiting.get(key, 0) <= 0 and key not in self._running:
            self._waiting.pop(key, None)
            self._global_waiting.pop(self._label(key), None)
            self._locks.pop(key, None)
            self._stop_reasons.pop(key, None)
            self._steer_texts.pop(key, None)
            self._running_task.pop(key, None)
            self._running_request_ids.pop(key, None)
            self._waiting_request_ids.pop(key, None)
            self._active_exec_session_ids.pop(key, None)

    def _control_session_id(self, key: SessionKey, session_id: str) -> str:
        """Return the currently running inner session id for steer/interrupt.

        Agent turns may execute inside a sidechain transcript
        (``{session_id}::turn::{request_id}``) while the gateway and UI keep
        addressing the stable outer session id. Control actions must target
        the inner id so CrewApp can find the cached running Agent.
        """

        return self._active_exec_session_ids.get(key, session_id)

    def _convergence_title_fallback(self) -> str | None:
        """Keep title placeholders intact while asynchronous title generation is enabled."""
        cfg = getattr(self._controller, "config", None)
        if cfg is not None and getattr(cfg, "title_auto", False):
            return ""
        return None

    async def _acquire_global_slot(self, key: SessionKey) -> bool:
        """Acquire the global run slot, returning whether a semaphore was used."""
        if self._global_semaphore is None:
            return False
        label = self._label(key)
        self._global_waiting[label] = self._global_waiting.get(label, 0) + 1
        try:
            await self._global_semaphore.acquire()
        finally:
            self._global_waiting[label] = max(0, self._global_waiting.get(label, 1) - 1)
        self._global_running.add(label)
        return True

    def _release_global_slot(self, key: SessionKey, acquired: bool) -> None:
        if not acquired or self._global_semaphore is None:
            return
        self._global_running.discard(self._label(key))
        self._global_semaphore.release()

    def _queued_depth(self, key: SessionKey) -> int:
        return self._waiting.get(key, 0) + self._global_waiting.get(self._label(key), 0)

    def activate_owner(self, owner_account_id: str) -> None:
        """Allow newly authenticated work for an owner after logout cleanup."""
        owner = str(owner_account_id or "").strip()
        if owner:
            self._blocked_owners.discard(owner)

    def owner_is_blocked(self, owner_account_id: str) -> bool:
        """Return whether owner-wide logout cleanup currently blocks admission."""
        return str(owner_account_id or "").strip() in self._blocked_owners

    async def stop_owner(
        self,
        owner_account_id: str,
        *,
        reason: str = "已停止：账号退出登录",
        timeout: float = 5.0,
    ) -> int:
        """Fence an owner generation and hard-cancel all running or queued turns.

        The owner remains blocked until ``activate_owner`` is called by a later
        authenticated login.  Terminal frames from the invalidated generation
        are suppressed, including after a same-owner re-login.
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            return 0
        self._blocked_owners.add(owner)
        self._owner_epochs[owner] = self._owner_epochs.get(owner, 0) + 1
        tasks: set[asyncio.Task] = set()
        for key, owned_tasks in list(self._tasks.items()):
            if key[0] != owner:
                continue
            self._stop_reasons[key] = reason
            ctrl_fn = getattr(self._controller, "interrupt", None)
            if callable(ctrl_fn):
                try:
                    target_session_id = self._control_session_id(key, key[1])
                    ctrl_fn(target_session_id, reason, owner_account_id=owner)
                except Exception:  # noqa: BLE001 - logout must continue hard cancellation
                    log.exception("Logout 级联中断失败 session=%s", key[1])
            tasks.update(task for task in owned_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.1, float(timeout)),
                )
            except asyncio.TimeoutError:
                log.warning("Logout 等待 Owner 任务退出超时 owner=%s count=%d", owner, len(tasks))
        return len(tasks)

    def active_tasks_snapshot(self) -> set[asyncio.Task]:
        """Return a stable snapshot of admitted running or queued turn tasks."""
        return {
            task
            for tasks in self._tasks.values()
            for task in tasks
            if not task.done()
        }

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        """Stop new admissions, cancel every admitted turn, and wait for cleanup."""
        self._closed = True
        tasks = self.active_tasks_snapshot()
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(0.1, float(timeout)),
            )
        except asyncio.TimeoutError:
            log.warning("Dispatcher shutdown 等待任务退出超时 count=%d", len(tasks))

    def _active_children_snapshot(
        self,
        session_id: str | None = None,
        owner_account_id: str = "",
    ) -> object:
        if callable(self._active_children_fn):
            try:
                return self._active_children_fn(
                    session_id,
                    owner_account_id=owner_account_id,
                )
            except Exception:  # noqa: BLE001 — injected active_children_fn 失败面未知，状态查询须容错降级
                log.exception("active children 状态获取失败 session=%s", session_id)
                return [] if session_id else {}
        team = getattr(self._controller, "team", None)
        fn = getattr(team, "active_children", None)
        if callable(fn):
            try:
                return fn(session_id, owner_account_id=owner_account_id)
            except Exception:  # noqa: BLE001 — team.active_children 内部失败面未知，状态查询须容错降级
                log.exception("team active children 状态获取失败 session=%s", session_id)
        return [] if session_id else {}

    def _has_active_children(self, session_id: str, owner_account_id: str = "") -> bool:
        snap = self._active_children_snapshot(session_id, owner_account_id=owner_account_id)
        return bool(snap)

    def stop(self, session_id: str, reason: str = "已停止当前回复", owner_account_id: str = "") -> bool:
        """停止某会话当前运行/等待的请求——取消所有 task。"""
        key = self._resolve_key(session_id, owner_account_id)
        prefix = f"{session_id}::turn::"
        target_session_ids = {session_id}
        target_keys = {
            item_key
            for item_key in (
                set(self._tasks)
                | set(self._running)
                | set(self._locks)
                | set(self._run_task_ids)
            )
            if item_key == key
            or (item_key[0] == key[0] and item_key[1].startswith(prefix))
        }
        target_session_ids.update(item_key[1] for item_key in target_keys)
        children = self._active_children_snapshot(None, owner_account_id=key[0])
        if isinstance(children, dict):
            target_session_ids.update(
                str(child_session_id)
                for child_session_id, records in children.items()
                if (
                    str(child_session_id) == session_id
                    or str(child_session_id).startswith(prefix)
                )
                and records
            )
        tasks = {
            task
            for item_key in target_keys
            for task in self._tasks.get(item_key, set())
            if not task.done()
        }
        for item_key in target_keys:
            self._stop_reasons[item_key] = reason
        ctrl_fn = getattr(self._controller, "interrupt", None)
        did_interrupt = False
        if callable(ctrl_fn):
            for target_sid in list(dict.fromkeys(sorted(target_session_ids))):
                target_session_id = self._control_session_id(key, target_sid)
                try:
                    did_interrupt = (
                        bool(ctrl_fn(target_session_id, reason, owner_account_id=key[0]))
                        or did_interrupt
                    )
                except Exception:  # noqa: BLE001 — controller.interrupt 内部失败面未知，stop 级联中断失败仅记录
                    log.exception("显式 stop 级联中断失败 session=%s", target_session_id)
        for task in tasks:
            task.cancel()
        did_cancel_runtime = self._cancel_runtime_tasks_for_session_prefix(
            session_id,
            owner_account_id=key[0],
            reason=reason,
        )
        return bool(tasks or did_interrupt or did_cancel_runtime)

    def _cancel_runtime_tasks_for_session_prefix(
        self,
        session_id: str,
        *,
        owner_account_id: str = "",
        reason: str,
    ) -> bool:
        if self._task_runtime is None:
            return False
        prefix = f"{session_id}::turn::"
        try:
            tasks = self._task_runtime.list_tasks(
                limit=1000,
                owner_account_id=owner_account_id,
            )
        except Exception:  # noqa: BLE001 — runtime task 查询失败不能影响内存 task 取消
            log.exception("查询运行任务失败 session=%s", session_id)
            return False
        did_cancel = False
        for task in tasks:
            sid = str(task.get("session_id") or "")
            if sid != session_id and not sid.startswith(prefix):
                continue
            if str(task.get("status") or "") in {"completed", "failed", "cancelled", "timed_out"}:
                continue
            try:
                self._task_runtime.update(
                    str(task.get("task_id") or task.get("id") or ""),
                    owner_account_id=owner_account_id,
                    cancel_requested=True,
                )
                if str(task.get("kind") or "") == "shell":
                    pid = int((task.get("progress") or {}).get("pid") or 0)
                    self._task_runtime.kill_process_group(pid, reason)
                self._task_runtime.finish(
                    str(task.get("task_id") or task.get("id") or ""),
                    owner_account_id=owner_account_id,
                    status="cancelled",
                    error=reason,
                )
                did_cancel = True
            except Exception:  # noqa: BLE001 — 单个任务取消失败不影响其它任务
                log.exception("取消运行任务失败 task=%s", task.get("task_id") or task.get("id"))
        return did_cancel

    def interrupt(self, session_id: str, reason: str = "被新消息中断", owner_account_id: str = "") -> bool:
        """中断当前运行的请求。

        有 controller 时走协作式优雅中断（loop 在安全点停止，历史完整保留）；
        否则退回硬取消持锁 task。排队者不受影响。
        """
        key = self._resolve_key(session_id, owner_account_id)
        if key not in self._running:
            return False
        ctrl_fn = getattr(self._controller, "interrupt", None)
        if callable(ctrl_fn):
            try:
                target_session_id = self._control_session_id(key, session_id)
                try:
                    result = ctrl_fn(target_session_id, reason, owner_account_id=key[0])
                except TypeError:
                    result = ctrl_fn(target_session_id, reason)
                if result:
                    log.info("协作式中断已请求 session=%s", session_id)
                    return True
            except Exception:
                log.exception("协作式中断失败，回退硬取消 session=%s", session_id)
        running = self._running_task.get(key)
        if running is None or running.done():
            return False
        self._stop_reasons[key] = reason
        running.cancel()
        return True

    def steer(self, session_id: str, text: str, owner_account_id: str = "") -> bool:
        """向运行中的 agent 注入补充指令。"""
        if not text.strip():
            return False
        key = self._resolve_key(session_id, owner_account_id)
        if key not in self._running:
            return False
        steer_fn = getattr(self._controller, "steer", None)
        if steer_fn is not None and callable(steer_fn):
            try:
                target_session_id = self._control_session_id(key, session_id)
                try:
                    result = steer_fn(target_session_id, text, owner_account_id=key[0])
                except TypeError:
                    result = steer_fn(target_session_id, text)
                if result:
                    log.info(
                        "steer 注入成功 session=%s target_session=%s",
                        session_id,
                        target_session_id,
                    )
                    return True
            except Exception:
                log.exception("steer 注入失败 session=%s", session_id)
        # controller 不支持/无运行中 Agent，缓存文本到下一轮
        self._steer_texts[key] = text
        log.info("steer 实时注入未生效，已缓存补充指令 session=%s", session_id)
        return True

    def background(self, session_id: str, owner_account_id: str = "") -> str | None:
        """把当前 Agent turn 标记为后台任务；执行协程继续运行。"""
        key = self._resolve_key(session_id, owner_account_id)
        task_id = self._run_task_ids.get(key)
        if not task_id or self._task_runtime is None:
            return None
        try:
            self._task_runtime.set_backgrounded(task_id)
            # The current run keeps its old lock, while future foreground
            # messages use a fresh lock and a fresh sidechain Agent.
            current_lock = self._locks.get(key)
            if current_lock is not None and current_lock.locked():
                self._locks[key] = asyncio.Lock()
            return task_id
        except Exception:
            log.exception("后台化当前轮失败 session=%s", session_id)
            return None

    # ------------------------------------------------------------------ #
    async def run(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """Run one turn under the causally verified Owner logging context."""
        token = current_owner_account_id.set(str(envelope.user_id or "").strip())
        try:
            async for chunk in self._run_owned(envelope):
                yield chunk
        finally:
            current_owner_account_id.reset(token)

    async def _run_owned(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """串行执行一轮对话；忙时根据策略处理。透传内核产出的 ResponseChunk。"""
        if self._closed:
            log.info("Dispatcher 已关闭，拒绝新调度 session=%s", envelope.session_id)
            return
        sid = envelope.session_id
        rid = envelope.request_id
        owner = envelope.user_id
        if owner in self._blocked_owners:
            log.info("Owner 正在退出，拒绝新调度 owner=%s session=%s", owner, sid)
            return
        owner_epoch = self._owner_epochs.get(owner, 0)
        key = self._key(sid, owner)
        lock = self._lock_for(key)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._tasks.setdefault(key, set()).add(current_task)

        # 工作区隔离：已有会话的 workspace 与入站不一致时记录警告（C2）
        stored_ws = self._store.get_workspace_id(sid, owner_account_id=owner)
        if stored_ws is not None and stored_ws != envelope.workspace_id:
            log.warning(
                "workspace 不匹配 session=%s stored=%s incoming=%s",
                sid,
                stored_ws,
                envelope.workspace_id,
            )
            envelope.workspace_id = stored_ws

        # ---- 忙时策略判定（在获取锁之前） ----
        busy = lock.locked()
        effective_mode = self._busy_mode
        demoted_for_children = False
        if busy and effective_mode == BusyMode.STEER:
            steered = self.steer(sid, envelope.query, owner_account_id=owner)
            if steered:
                yield ResponseChunk.status_event(rid, "补充指令已注入当前回复")
                if current_task is not None:
                    tasks = self._tasks.get(key)
                    if tasks is not None:
                        tasks.discard(current_task)
                        if not tasks:
                            self._tasks.pop(key, None)
                self._cleanup_if_idle(key)
                return
            # steer 失败，降级为 queue
            effective_mode = BusyMode.QUEUE

        if busy and effective_mode == BusyMode.INTERRUPT and self._has_active_children(sid, owner_account_id=owner):
            effective_mode = BusyMode.QUEUE
            demoted_for_children = True
            log.info("session=%s 有活跃子 agent，busy interrupt 降级为 queue", sid)

        if busy and effective_mode == BusyMode.INTERRUPT:
            self.interrupt(sid, "被新消息中断", owner_account_id=owner)
            yield ResponseChunk.status_event(rid, "已请求中断当前回复，新消息将在当前回复停止后开始执行")

        # ---- 排队计数 ----
        if busy:
            prior = self._queued_depth(key)
            if self._max_queue_depth_per_session and prior >= self._max_queue_depth_per_session:
                msg = f"队列已满（最多 {self._max_queue_depth_per_session} 条），请稍后再试"
                yield ResponseChunk.error(rid, msg)
                try:
                    self._store.set_status(sid, "failed", msg, owner_account_id=owner)
                except Exception:  # noqa: BLE001 — 抽象 SessionStore 写状态失败面未声明，不得覆盖已 yield 的队列满错误
                    log.exception("写入队列满状态失败 session=%s", sid)
                return
            self._waiting[key] = self._waiting.get(key, 0) + 1
            self._waiting_request_ids.setdefault(key, []).append(rid)
            if effective_mode == BusyMode.QUEUE:
                if demoted_for_children:
                    yield ResponseChunk.status_event(rid, f"子 agent 正在运行，已排队（前面 {prior + 1} 条）")
                else:
                    yield ResponseChunk.status_event(rid, f"排队中（前面 {prior + 1} 条）")

        dequeued = False

        def _dequeue_once() -> None:
            nonlocal dequeued
            if not dequeued:
                dequeued = True
                self._waiting[key] = max(0, self._waiting.get(key, 1) - 1)
                queued_ids = self._waiting_request_ids.get(key)
                if queued_ids:
                    queued_ids.pop(0)
                    if not queued_ids:
                        self._waiting_request_ids.pop(key, None)

        deferred_terminal: ResponseChunk | None = None
        rt_id_token = None
        rt_token = None

        try:
            try:
                async with lock:
                    _dequeue_once()
                    global_slot = False
                    try:
                        global_slot = await self._acquire_global_slot(key)
                    except asyncio.CancelledError:
                        raise
                    self._running.add(key)
                    self._running_counts[key] = self._running_counts.get(key, 0) + 1
                    self._running_request_ids[key] = rid
                    if current_task is not None:
                        self._running_task[key] = current_task
                    runtime_task_id = ""
                    if self._task_runtime is not None:
                        try:
                            cfg = getattr(self._controller, "config", None)
                            task = self._task_runtime.create_runtime(
                                kind="agent_turn",
                                session_id=sid,
                                request_id=rid,
                                title=envelope.query[:120] or "Agent turn",
                                detail=envelope.query,
                                execution_timeout=getattr(
                                    cfg, "tasks_agent_turn_execution_timeout_seconds", 3600.0
                                ),
                                inactivity_timeout=getattr(
                                    cfg, "tasks_agent_turn_inactivity_timeout_seconds", 600.0
                                ),
                                owner_account_id=owner,
                            )
                            runtime_task_id = task["task_id"]
                            from crew.state.home import get_owner_runtime_home

                            output_ref = str(
                                get_owner_runtime_home(owner) / "tasks" / f"{runtime_task_id}.json"
                            )
                            self._task_runtime.update(runtime_task_id, owner_account_id=owner, output_ref=output_ref)
                            self._run_task_ids[key] = runtime_task_id
                            self._task_runtime.mark_running(runtime_task_id)
                            if current_task is not None:
                                self._task_runtime.attach_worker(
                                    runtime_task_id,
                                    current_task,
                                    cancel=(
                                        lambda _reason, owned=current_task: owned.cancel()
                                        if not owned.done()
                                        else None
                                    ),
                                )
                            # 注入 task runtime 上下文，供长耗时工具内部保活
                            from crew.core.runctx import (
                                current_task_runtime,
                                current_task_runtime_id,
                            )

                            rt_id_token = current_task_runtime_id.set(runtime_task_id)
                            rt_token = current_task_runtime.set(self._task_runtime)
                        except Exception:
                            log.exception("注册 agent_turn 任务失败 session=%s", sid)

                    # 标记会话进入 running，同时刷新 updated_at 防止被后台过期清理误删
                    try:
                        self._store.set_status(sid, "running", "", owner_account_id=owner)
                    except Exception:
                        log.exception("写入 running 状态失败 session=%s", sid)

                    steer_text = self._steer_texts.pop(key, "")
                    if steer_text:
                        envelope.params["steer_text"] = steer_text

                    # session:start：每个会话生命周期内只发一次（新会话首次运行）。
                    if key not in self._sessions_started:
                        self._sessions_started.add(key)
                        await hook_registry.emit("session:start", {
                            "session_id": sid,
                            "channel": envelope.channel,
                        })

                    # 触发 agent:start hook
                    await hook_registry.emit("agent:start", {
                        "session_id": sid,
                        "message": envelope.query[:500],
                        "channel": envelope.channel,
                        "owner_account_id": owner,
                    })

                    failed = False
                    err = ""
                    final_text = ""
                    # Dynamic Kanban 自己管理原始 session 的历史（user/status/final 直接落库），
                    # 如果走 sidechain 隔离，收敛时会把 sidechain 上的旧 base history 覆盖回 sid，
                    # 从而把 Dynamic Kanban 刚写入的消息抹掉。因此该模式不创建 sidechain。
                    sidechain_id = (
                        f"{sid}::turn::{rid}"
                        if self._task_runtime is not None and envelope.mode != "dynamic_kanban"
                        else ""
                    )
                    exec_session_id = sidechain_id or sid
                    self._active_exec_session_ids[key] = exec_session_id
                    sidechain_output_ref = ""
                    if runtime_task_id and self._task_runtime is not None:
                            sidechain_output_ref = str(
                                self._task_runtime.get(runtime_task_id, owner_account_id=owner).get("output_ref") or ""
                            )
                    # Run every turn in an isolated transcript. This makes a
                    # mid-turn background transition safe: new foreground
                    # messages never share the same Agent/history object.
                    exec_envelope = envelope
                    if sidechain_id:
                        base_history = self._store.load(sid, owner_account_id=owner)
                        self._store.save(
                            sidechain_id,
                            list(base_history),
                            workspace_id=self._store.get_workspace_id(sid, owner_account_id=owner) or envelope.workspace_id,
                            owner_account_id=owner,
                            title_fallback=self._convergence_title_fallback(),
                        )
                        parent_history = list(base_history)
                        parent_history.append(
                            Message.user(
                                envelope.query,
                                is_meta=bool(envelope.params.get("internal_task_resume")),
                            )
                        )
                        self._store.save(
                            sid,
                            parent_history,
                            workspace_id=(
                                self._store.get_workspace_id(sid, owner_account_id=owner)
                                or envelope.workspace_id
                            ),
                            owner_account_id=owner,
                            title_fallback=self._convergence_title_fallback(),
                        )
                        exec_envelope = Envelope(
                            session_id=sidechain_id,
                            params={
                                **envelope.params,
                                "task_session_id": sid,
                                "sidechain_task_id": runtime_task_id,
                            },
                            request_id=envelope.request_id,
                            channel=envelope.channel,
                            user_id=envelope.user_id,
                            user_type=envelope.user_type,
                            workspace_id=envelope.workspace_id,
                            mode=envelope.mode,
                            is_stream=envelope.is_stream,
                            attachments=list(envelope.attachments),
                        )
                    try:
                        async for chunk in self._inner(exec_envelope):
                            if runtime_task_id and self._task_runtime is not None:
                                progress: dict[str, object] = {"last_chunk": chunk.kind}
                                if chunk.kind in {"delta", "thinking"}:
                                    progress["text_tail"] = str(chunk.body.get("text", ""))[-500:]
                                elif chunk.kind == "tool":
                                    progress["last_tool"] = chunk.body.get("name", "")
                                    progress["tool_phase"] = chunk.body.get("phase", "")
                                self._task_runtime.touch_activity(runtime_task_id, progress)
                            is_terminal = chunk.is_final or chunk.kind in {"final", "error"}
                            if chunk.kind == "error" or chunk.status == "failed":
                                failed = True
                                err = chunk.body.get("message", "") or err
                            elif chunk.kind == "final":
                                final_text = chunk.body.get("text", "")
                            if is_terminal:
                                deferred_terminal = chunk
                            elif self._owner_epochs.get(owner, 0) == owner_epoch:
                                yield chunk
                    except asyncio.CancelledError:
                        failed = True
                        err = self._stop_reasons.get(key, "已停止当前回复")
                        log.info("会话已停止 session=%s", sid)
                        deferred_terminal = enrich_error_chunk(ResponseChunk.error(rid, err))
                    except ProviderError as exc:
                        failed, err = True, str(exc)
                        log.exception("Provider 异常 session=%s", sid)
                        chunk = ResponseChunk.error(rid, str(exc))
                        chunk.body["category"] = exc.category
                        deferred_terminal = chunk
                    except ToolError as exc:
                        failed, err = True, str(exc)
                        log.exception("工具异常 session=%s", sid)
                        chunk = ResponseChunk.error(rid, str(exc))
                        chunk.body["category"] = "tool"
                        deferred_terminal = chunk
                    except Exception as exc:  # noqa: BLE001 — inner 执行委托 provider/tool/skill/plan 多条未知路径，请求最外层兜底须吞住并回报错帧
                        failed, err = True, str(exc)
                        log.exception("会话执行异常 session=%s", sid)
                        deferred_terminal = enrich_error_chunk(ResponseChunk.error(rid, str(exc)), exc)
                    finally:
                        if sidechain_id:
                            try:
                                sidechain_history = self._store.load(sidechain_id, owner_account_id=owner)
                                if sidechain_output_ref:
                                    path = Path(sidechain_output_ref)
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    path.write_text(
                                        json.dumps(
                                            [message.to_openai() for message in sidechain_history],
                                            ensure_ascii=False,
                                            indent=2,
                                        ),
                                        encoding="utf-8",
                                    )
                                backgrounded = False
                                if runtime_task_id and self._task_runtime is not None:
                                    backgrounded = bool(
                                        self._task_runtime.get(runtime_task_id, owner_account_id=owner).get("backgrounded")
                                    )
                                if not backgrounded:
                                    self._store.save(
                                        sid,
                                        sidechain_history,
                                        workspace_id=envelope.workspace_id,
                                        owner_account_id=owner,
                                        title_fallback=self._convergence_title_fallback(),
                                    )
                                    # Agent writes the generated title directly to the parent
                                    # via task_session_id. Sidechain sessions are intentionally
                                    # excluded from list_sessions, so copying their title here is
                                    # both ineffective and risks racing a manual parent rename.
                                self._store.clear(sidechain_id, owner_account_id=owner)
                            except Exception:
                                log.exception(
                                    "sidechain 收敛失败 session=%s sidechain=%s",
                                    sid,
                                    sidechain_id,
                                )
                        # agent:end 负载需要回合刚结束时的队列深度，先算好；
                        # emit 本身挪到下方 running 计数与落库状态清理之后，
                        # 否则监听者（如桌面端渠道通知）收到通知立即拉 status 仍看到 running。
                        remaining = max(0, self._running_counts.get(key, 1) - 1)
                        queue_depth = self._waiting.get(key, 0)
                        if remaining:
                            self._running_counts[key] = remaining
                        else:
                            self._running_counts.pop(key, None)
                            self._running.discard(key)
                            self._running_request_ids.pop(key, None)
                            if self._active_exec_session_ids.get(key) == exec_session_id:
                                self._active_exec_session_ids.pop(key, None)
                        if self._running_task.get(key) is current_task:
                            self._running_task.pop(key, None)
                        if self._run_task_ids.get(key) == runtime_task_id:
                            self._run_task_ids.pop(key, None)
                        if runtime_task_id and self._task_runtime is not None:
                            try:
                                current = self._task_runtime.get(runtime_task_id, owner_account_id=owner)
                                if current["status"] not in {
                                    "completed", "failed", "cancelled", "timed_out"
                                }:
                                    self._task_runtime.finish(
                                        runtime_task_id,
                                        owner_account_id=owner,
                                        status="failed" if failed else "completed",
                                        result=final_text,
                                        error=err,
                                    )
                            except Exception:
                                log.exception("完成 agent_turn 任务失败 task=%s", runtime_task_id)
                        self._release_global_slot(key, global_slot)
                        try:
                            status = "stopped" if err.startswith("已停止") or err.startswith("被新消息中断") else ("failed" if failed else "completed")
                            self._store.set_status(sid, status, err, owner_account_id=owner)
                        except Exception:  # noqa: BLE001 — 抽象 SessionStore 写状态失败面未声明，finally 中不得掩盖主流程结果
                            log.exception("写入会话状态失败 session=%s", sid)
                        # 触发 agent:end hook：必须在 running 计数与落库状态清理之后，
                        # 监听者收到通知后拉到的才是终态。
                        await hook_registry.emit("agent:end", {
                            "session_id": sid,
                            "message": envelope.query[:500],
                            "response": final_text[:500],
                            "channel": envelope.channel,
                            "failed": failed,
                            "error": err,
                            "owner_account_id": owner,
                            "queue_depth": queue_depth,
                            "running_depth": remaining,
                        })
            except asyncio.CancelledError:
                err = self._stop_reasons.get(key, "已停止当前回复")
                log.info("排队中的会话请求已停止 session=%s", sid)
                try:
                    self._store.set_status(sid, "stopped", err, owner_account_id=owner)
                except Exception:  # noqa: BLE001 — 抽象 SessionStore 写状态失败面未声明，取消分支中不得掩盖已 yield 的停止错误
                    log.exception("写入会话状态失败 session=%s", sid)
                deferred_terminal = ResponseChunk.error(rid, err)
        finally:
            if rt_id_token is not None or rt_token is not None:
                from crew.core.runctx import (
                    current_task_runtime,
                    current_task_runtime_id,
                )

                if rt_id_token is not None:
                    try:
                        current_task_runtime_id.reset(rt_id_token)
                    except Exception:
                        pass
                if rt_token is not None:
                    try:
                        current_task_runtime.reset(rt_token)
                    except Exception:
                        pass
            _dequeue_once()
            if current_task is not None:
                tasks = self._tasks.get(key)
                if tasks is not None:
                    tasks.discard(current_task)
                    if not tasks:
                        self._tasks.pop(key, None)
            self._cleanup_if_idle(key)
        if (
            deferred_terminal is not None
            and owner not in self._blocked_owners
            and self._owner_epochs.get(owner, 0) == owner_epoch
        ):
            yield deferred_terminal

    # ------------------------------------------------------------------ #
    def status(self, session_id: str, owner_account_id: str = "") -> dict:
        """返回会话当前运行态（内存）+ 上一轮 terminal 结果（落库）。"""
        key = self._resolve_key(session_id, owner_account_id)
        last_status, last_error = self._store.get_status(session_id, owner_account_id=key[0])
        if key in self._running:
            live = "running"
        elif self._queued_depth(key) > 0:
            live = "queued"
        else:
            live = "idle"
        active_request_id = self._running_request_ids.get(key)
        if active_request_id is None:
            queued_ids = self._waiting_request_ids.get(key) or []
            active_request_id = queued_ids[0] if queued_ids else None
        return {
            "session_id": session_id,
            "owner_account_id": owner_account_id,
            "live": live,
            "active_request_id": active_request_id,
            "queue_depth": self._queued_depth(key),
            "waiting_for_global_slot": self._global_waiting.get(self._label(key), 0),
            "global_active": len(self._global_running),
            "global_queued": sum(self._global_waiting.values()),
            "max_active_runs": self._max_active_runs,
            "queue_limit": self._max_queue_depth_per_session,
            "last_status": last_status,
            "last_error": last_error,
        }

    def runtime_status(self) -> dict:
        """Return a process-local concurrency snapshot for debug/UI panels."""
        keys = sorted(
            set(self._locks)
            | set(self._waiting)
            | set(self._running)
        )
        return {
            "max_active_runs": self._max_active_runs,
            "global_active": len(self._global_running),
            "global_queued": sum(self._global_waiting.values()),
            "sessions": {self._label(key): self.status(key[1], owner_account_id=key[0]) for key in keys},
            "active_children": self._active_children_snapshot(None),
        }
