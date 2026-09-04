"""SkillWatcher：watchdog 监听各 skill roots，变更时触发索引失效。

watchdog 不可用、observer 启动失败、或没有任何可监听目录时 ``start()``
返回 False，调用方（SkillIndex）回退到 stat TTL 失效检测。

监听目标是各逻辑 root 的最近现存祖先（root 可能尚未创建）；事件过滤
只放行 root 内的 SKILL.md / PACKAGE.md 文件事件与目录增删移动，祖先目录
里与 skills 无关的变更不会引发重扫。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("crew.agent.skills")

_WATCHED_FILENAMES = frozenset({"SKILL.md", "PACKAGE.md"})


def _nearest_existing_dir(path: Path) -> Path | None:
    """path 本身或最近的现存祖先目录；都不存在返回 None。"""
    current = path
    while True:
        try:
            if current.is_dir():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


class SkillWatcher:
    """对一组逻辑 skill roots 的文件监听；roots 动态取值以跟随插件装卸。"""

    def __init__(
        self,
        roots: Callable[[], list[Path]],
        on_change: Callable[[], None],
    ) -> None:
        self._roots = roots
        self._on_change = on_change
        self._observer = None
        self._handler = None
        self._watched: set[str] = set()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._observer is not None

    def start(self) -> bool:
        """懒启动 observer；任何失败都返回 False（调用方回退 stat）。"""
        with self._lock:
            if self._observer is not None:
                return True
            try:
                from watchdog.events import FileSystemEventHandler
                from watchdog.observers import Observer
            except Exception:
                logger.debug("watchdog 不可用，skill 索引回退 stat 失效检测")
                return False

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):  # noqa: ANN001, ANN202
                    watcher._handle_event(event)

            handler = _Handler()
            try:
                observer = Observer()
                scheduled = False
                for target in self._watch_targets():
                    observer.schedule(handler, str(target), recursive=True)
                    self._watched.add(self._target_key(target))
                    scheduled = True
                if not scheduled:
                    return False
                observer.daemon = True
                observer.start()
            except Exception:
                logger.debug("skill watcher 启动失败，回退 stat 失效检测", exc_info=True)
                self._watched = set()
                return False
            self._handler = handler
            self._observer = observer
            return True

    def sync(self) -> None:
        """补齐新出现的 roots（监听目标变化时增量 schedule）。"""
        with self._lock:
            if self._observer is None or self._handler is None:
                return
            try:
                for target in self._watch_targets():
                    key = self._target_key(target)
                    if key in self._watched:
                        continue
                    self._observer.schedule(self._handler, str(target), recursive=True)
                    self._watched.add(key)
            except Exception:
                logger.debug("skill watcher 增量监听失败", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            observer = self._observer
            self._observer = None
            self._handler = None
            self._watched = set()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:
                logger.debug("skill watcher 停止失败", exc_info=True)

    # ── 内部 ──────────────────────────────────────────────────────────

    @staticmethod
    def _target_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _watch_targets(self) -> list[Path]:
        """各逻辑 root 的最近现存祖先目录（去重）。"""
        targets: list[Path] = []
        seen: set[str] = set()
        for root in self._roots():
            target = _nearest_existing_dir(Path(root))
            if target is None:
                continue
            key = self._target_key(target)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
        return targets

    def _handle_event(self, event) -> None:  # noqa: ANN001
        try:
            if self._is_relevant(event):
                self._on_change()
        except Exception:
            logger.debug("skill watcher 事件处理失败", exc_info=True)

    def _is_relevant(self, event) -> bool:  # noqa: ANN001
        roots = [Path(root) for root in self._roots()]
        paths = [Path(event.src_path)]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(Path(dest))
        for path in paths:
            if self._path_relevant(path, event.is_directory, roots):
                return True
        return False

    @staticmethod
    def _path_relevant(path: Path, is_directory: bool, roots: list[Path]) -> bool:
        for root in roots:
            if path == root or root in path.parents:
                # root 内：目录增删移动、SKILL.md/PACKAGE.md 文件事件
                if is_directory or path.name in _WATCHED_FILENAMES:
                    return True
            if is_directory and path in root.parents:
                # root 的祖先目录变化可能导致 root 本身出现/消失
                return True
        return False
