"""Ordered, bounded WebSocket delivery for independent Gateway subscribers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class _Batch:
    frames: tuple[dict, ...]
    size: int
    deadline: float
    done: asyncio.Future[bool]


@dataclass
class _Subscriber:
    queue: deque[_Batch] = field(default_factory=deque)
    size: int = 0
    count: int = 0
    task: asyncio.Task | None = None


class SocketDelivery:
    def __init__(
        self, send: Callable[[WebSocket, dict], Awaitable[None]], failed: Callable[[WebSocket], None],
        *, max_bytes: int, max_frames: int, max_age: float,
    ):
        self._send = send
        self._failed = failed
        self.max_bytes = max_bytes
        self._max_frames = max_frames
        self._max_age = max_age
        self._subscribers: dict[WebSocket, _Subscriber] = {}

    def submit(self, ws: WebSocket, frames: tuple[dict, ...], size: int) -> asyncio.Future[bool]:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[bool] = loop.create_future()
        subscriber = self._subscribers.setdefault(ws, _Subscriber())
        if size + subscriber.size > self.max_bytes or len(frames) + subscriber.count > self._max_frames:
            self.retire(ws)
            self._failed(ws)
            done.set_result(False)
            return done
        subscriber.queue.append(_Batch(frames, size, loop.time() + self._max_age, done))
        subscriber.size += size
        subscriber.count += len(frames)
        if subscriber.task is None:
            subscriber.task = loop.create_task(self._drain(ws, subscriber))
        return done

    def retire(self, ws: WebSocket) -> None:
        subscriber = self._subscribers.pop(ws, None)
        if subscriber is None:
            return
        if subscriber.task is not None and subscriber.task is not asyncio.current_task():
            subscriber.task.cancel()
        for batch in subscriber.queue:
            if not batch.done.done():
                batch.done.set_result(False)
        subscriber.queue.clear()

    async def _drain(self, ws: WebSocket, subscriber: _Subscriber) -> None:
        try:
            while subscriber.queue:
                batch = subscriber.queue[0]
                if asyncio.get_running_loop().time() >= batch.deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(batch.deadline):
                    for index, frame in enumerate(batch.frames):
                        if index and index % 64 == 0:
                            await asyncio.sleep(0)
                        await self._send(ws, frame)
                subscriber.queue.popleft()
                subscriber.size -= batch.size
                subscriber.count -= len(batch.frames)
                if not batch.done.done():
                    batch.done.set_result(True)
        except Exception:
            self.retire(ws)
            self._failed(ws)
        finally:
            if self._subscribers.get(ws) is subscriber:
                self.retire(ws)
