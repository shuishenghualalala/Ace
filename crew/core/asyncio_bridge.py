"""同步↔异步桥：纯同步代码路径调用 async LLM 接口时的显式协议。

调用方约束（见 crew/evolution/queue.py、crew/evolution/manager.py）：
- 正常路径：EvolutionManager 的同步方法由 ``asyncio.to_thread`` 调起，所在线程
  没有事件循环，直接 ``asyncio.run`` 即可，开销最小。
- 防御路径：若发现已在事件循环中（本不应发生），不再每任务新建临时线程池跑
  ``asyncio.run`` 并阻塞等待（原 pool.submit(asyncio.run, ...).result() 模式会
  占住调用方循环线程，协程一旦依赖调用方循环的资源即成死锁），改为投递到常驻
  后台事件循环线程执行。注意此时调用方线程仍被阻塞到结果返回，事件循环线程
  不得使用该协议——它只服务「误闯循环内」的防御场景。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine

log = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_loop_thread_id: int | None = None


def _run_bridge_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the bridge loop and retain its thread identity for deadlock checks."""
    global _loop_thread_id
    _loop_thread_id = threading.get_ident()
    try:
        loop.run_forever()
    finally:
        _loop_thread_id = None


def _bridge_loop() -> asyncio.AbstractEventLoop:
    """常驻后台事件循环（首次使用时启动，进程退出随守护线程回收）。"""
    global _loop
    if _loop is not None and _loop.is_running():
        return _loop
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_bridge_loop,
            args=(loop,),
            name="crew-asyncio-bridge",
            daemon=True,
        )
        thread.start()
        _loop = loop
        log.debug("asyncio 桥接循环已启动 thread=%s", thread.name)
        return loop


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """在同步上下文中运行协程并返回结果。

    无线程循环 → ``asyncio.run``；已在循环中 → 投递常驻桥接循环并阻塞等待
    （防御路径，调用方不得是事件循环线程自身）。
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    bridge_loop = _bridge_loop()
    if running_loop is bridge_loop or threading.get_ident() == _loop_thread_id:
        # Do not submit to the same loop and then wait for its result: that
        # permanently blocks the loop before the submitted coroutine can run.
        # Closing the caller-created coroutine also avoids an unawaited warning.
        coro.close()
        raise RuntimeError(
            "run_sync() cannot be called from the asyncio bridge loop thread"
        )

    future = asyncio.run_coroutine_threadsafe(coro, bridge_loop)
    return future.result()
