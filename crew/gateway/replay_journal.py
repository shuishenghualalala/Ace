"""Bounded, contiguous replay windows for Gateway WebSocket sessions."""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field

SessionKey = tuple[str, str]


@dataclass
class _Window:
    frames: deque[bytes] = field(default_factory=deque)
    size: int = 0


class ReplayJournal:
    def __init__(self, max_frames: int, session_bytes: int, total_bytes: int, max_sessions: int):
        self._frames = max_frames
        self._session_bytes = min(session_bytes, total_bytes)
        self._total_bytes = total_bytes
        self._sessions = max_sessions
        self._windows: OrderedDict[SessionKey, _Window] = OrderedDict()
        self.size = 0

    def clear(self, key: SessionKey) -> None:
        window = self._windows.pop(key, None)
        if window is not None:
            self.size -= window.size

    def clear_owner(self, owner: str) -> None:
        for key in tuple(self._windows):
            if key[0] == owner:
                self.clear(key)

    def append(self, key: SessionKey, frame: bytes | None) -> None:
        if frame is None or len(frame) > self._session_bytes:
            # A gap invalidates the entire prefix. Future frames start a new suffix.
            self.clear(key)
            return
        window = self._windows.get(key)
        if window is None:
            window = self._windows[key] = _Window()
        self._windows.move_to_end(key)
        window.frames.append(frame)
        window.size += len(frame)
        self.size += len(frame)
        while len(window.frames) > self._frames or window.size > self._session_bytes:
            removed = len(window.frames.popleft())
            window.size -= removed
            self.size -= removed
        while self.size > self._total_bytes or len(self._windows) > self._sessions:
            self.clear(next(iter(self._windows)))

    def count(self, key: SessionKey) -> int:
        window = self._windows.get(key)
        return len(window.frames) if window is not None else 0

    def after(self, key: SessionKey, head: int, cursor: int) -> Iterator[dict]:
        window = self._windows.get(key)
        if window is not None:
            first = head - len(window.frames) + 1
            for seq, data in enumerate(window.frames, start=first):
                if seq > cursor:
                    yield json.loads(data)
