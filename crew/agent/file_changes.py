"""Protocol-neutral file-change capture for Agent turns.

The existing Builtin Agent remains the reference behaviour.  This module owns
the pure snapshot/diff/merge operations so Runtime-backed agents and Team
members can emit the same ``file_changes`` contract without copying UI- or
executor-specific code.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.state.logging import get_logger
from crew.team.workspace_guard import normalize_acp_tool_name
from crew.tools.file_utils import _has_binary_extension
from crew.tools.redact import redact_sensitive_text

log = get_logger("agent.file_changes")

WORKSPACE_SNAPSHOT_MAX_FILES = 20_000
FILE_CHANGE_MAX_BYTES = 128 * 1024
SENSITIVE_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SENSITIVE_FILE_NAMES = frozenset({
    ".env",
    "credentials.json",
    "secrets.json",
    "service-account.json",
})
WORKSPACE_SNAPSHOT_SKIP_DIRS = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
})

FileMetadataSnapshot = dict[str, tuple[int, int]]


@dataclass(frozen=True)
class FileState:
    """Small before/after state used for exact path-level diffs."""

    exists: bool
    binary: bool = False
    text: str | None = None


def resolve_file_path(raw: str | Path, *, cwd: str | Path = "") -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(cwd).expanduser() if cwd else Path.cwd()
        path = base / path
    return path.resolve()


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SENSITIVE_FILE_NAMES
        or name.startswith(".env.")
        or name.endswith(SENSITIVE_FILE_SUFFIXES)
    )


def _read_text_with_limit(path: Path) -> tuple[str | None, bool]:
    try:
        with path.open("rb") as stream:
            data = stream.read(FILE_CHANGE_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None, False
    if len(data) > FILE_CHANGE_MAX_BYTES:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def read_file_state(path: str | Path) -> FileState:
    target = Path(path)
    try:
        if not target.is_file():
            return FileState(False)
    except OSError:
        return FileState(False)
    if _is_sensitive_file(target):
        return FileState(True, binary=True)
    binary = _has_binary_extension(target)
    if binary:
        return FileState(True, binary=True)
    text, oversized = _read_text_with_limit(target)
    if oversized:
        return FileState(True, binary=True)
    if text is None:
        return FileState(True)
    return FileState(True, text=text)


def _text_within_limit(text: str) -> bool:
    if len(text) > FILE_CHANGE_MAX_BYTES:
        return False
    return len(text.encode("utf-8", errors="replace")) <= FILE_CHANGE_MAX_BYTES


def workspace_snapshot(
    root: str | Path,
    *,
    max_files: int = WORKSPACE_SNAPSHOT_MAX_FILES,
) -> FileMetadataSnapshot | None:
    """Return deterministic workspace metadata, or ``None`` when too large."""

    base = Path(root).expanduser()
    snapshot: FileMetadataSnapshot = {}
    if not base.is_dir():
        return snapshot
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            name for name in dirnames if name not in WORKSPACE_SNAPSHOT_SKIP_DIRS
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
            if len(snapshot) >= max_files:
                log.warning("文件快照达到上限 root=%s limit=%s", base, max_files)
                return None
    return snapshot


def _diff_rows(before: str, after: str) -> tuple[list[dict[str, Any]], int, int]:
    if not _text_within_limit(before) or not _text_within_limit(after):
        return [], 0, 0
    rows: list[dict[str, Any]] = []
    added = 0
    removed = 0
    for line in difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        lineterm="",
    ):
        if line.startswith(("@@", "---", "+++")):
            rows.append({"line": 0, "kind": "meta", "text": redact_sensitive_text(line, force=True)})
        elif line.startswith("+"):
            added += 1
            rows.append({"line": 0, "kind": "add", "text": redact_sensitive_text(line[1:], force=True)})
        elif line.startswith("-"):
            removed += 1
            rows.append({"line": 0, "kind": "del", "text": redact_sensitive_text(line[1:], force=True)})
        else:
            rows.append({"line": 0, "kind": "ctx", "text": redact_sensitive_text(line[1:], force=True)})
    return rows[:200], added, removed


def file_change_from_states(path: str | Path, before: FileState, after: FileState) -> dict[str, Any] | None:
    """Build one standard file-change item from exact turn boundary states."""

    target = Path(path)
    if not before.exists and not after.exists:
        return None
    status = "added" if not before.exists else "deleted" if not after.exists else "modified"
    binary = before.binary or after.binary or any(
        text is not None and not _text_within_limit(text)
        for text in (before.text, after.text)
    )
    diff: list[dict[str, Any]] = []
    added = 0
    removed = 0
    if not binary and (before.text is not None or after.text is not None):
        diff, added, removed = _diff_rows(before.text or "", after.text or "")
    change: dict[str, Any] = {
        "path": str(target),
        "name": target.name or str(target),
        "added": added,
        "removed": removed,
        "status": status,
        "diff": diff,
    }
    if binary:
        change["binary"] = True
    return change


def metadata_change(path_text: str, status: str) -> dict[str, Any]:
    """Build the metadata-only shape used by terminal/full-workspace snapshots."""

    path = Path(path_text)
    binary = _has_binary_extension(path) or _is_sensitive_file(path)
    added = 0
    diff_rows: list[dict[str, Any]] = []
    if not binary and status != "deleted":
        try:
            binary = path.stat().st_size > FILE_CHANGE_MAX_BYTES
        except OSError:
            pass
    if status == "added" and not binary:
        text, oversized = _read_text_with_limit(path)
        if oversized:
            binary = True
        elif text is not None:
            lines = text.splitlines()
            added = len(lines)
            diff_rows = [
                {"line": 0, "kind": "add", "text": redact_sensitive_text(line, force=True)}
                for line in lines[:200]
            ]
    change: dict[str, Any] = {
        "path": str(path),
        "name": path.name or str(path),
        "added": added,
        "removed": 0,
        "status": status,
        "diff": diff_rows,
    }
    if binary:
        change["binary"] = True
    return change


def changes_between_snapshots(
    before: FileMetadataSnapshot,
    after: FileMetadataSnapshot,
) -> list[dict[str, Any]]:
    changed = sorted(
        path for path, metadata in after.items()
        if path not in before or before[path] != metadata
    )
    deleted = sorted(path for path in before if path not in after)
    changes: list[dict[str, Any]] = []
    for path in changed:
        change = metadata_change(path, "added" if path not in before else "modified")
        change["revision"] = f"{after[path][0]}:{after[path][1]}"
        changes.append(change)
    for path in deleted:
        change = metadata_change(path, "deleted")
        change["revision"] = f"deleted:{before[path][0]}:{before[path][1]}"
        changes.append(change)
    return changes


def merge_changes(existing: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge by absolute path while preserving deterministic insertion order."""

    merged: dict[str, dict[str, Any]] = {}
    for item in [*existing, *incoming]:
        path = str(item.get("path") or "").strip()
        if path:
            merged[path] = dict(item)
    return list(merged.values())


def persist_file_changes(
    plan_manager: Any,
    session_id: str,
    changes: Iterable[dict[str, Any]],
    *,
    owner_account_id: str = "",
) -> list[dict[str, Any]]:
    """Merge changes into Crew's existing cumulative and per-turn stores."""

    items = [dict(item) for item in changes if str(item.get("path") or "").strip()]
    if plan_manager is None:
        return items
    store = plan_manager.file_change_store(
        session_id,
        owner_account_id=owner_account_id,
    )
    for change in items:
        path = str(change["path"])
        # ``created_in_session`` is intentionally not inherited from a previous
        # turn.  A file created last turn and deleted now must be shown as a
        # current-turn deletion; only add-then-delete inside this tracker is
        # filtered before persistence.
        store[:] = [item for item in store if item.get("path") != path]
        store.append(change)
        plan_manager.record_turn_file_change(
            session_id,
            change,
            owner_account_id=owner_account_id,
        )
    return list(store)


def external_tool_write_paths(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    cwd: str | Path,
) -> list[Path]:
    """Extract explicit file targets owned by one external tool event.

    Arbitrary shell commands are deliberately left to the workspace snapshot;
    guessing paths from free-form commands would reintroduce cross-agent false
    attribution.
    """

    name = normalize_acp_tool_name(tool_name)
    if name not in {"file_write", "file_delete", "patch"}:
        return []
    payload = dict(arguments or {})
    raw_paths: list[Any] = []
    for key in ("path", "file_path", "target"):
        if payload.get(key):
            raw_paths.append(payload[key])
    changes = payload.get("changes")
    if isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict):
                raw = item.get("path") or item.get("file_path")
                if raw:
                    raw_paths.append(raw)
    paths: list[Path] = []
    seen: set[str] = set()
    try:
        root = resolve_file_path(cwd)
    except (OSError, RuntimeError, ValueError) as exc:
        log.debug("忽略无效外部工具工作区 cwd=%s error=%s", cwd, exc)
        return []
    for raw in raw_paths:
        try:
            path = resolve_file_path(str(raw), cwd=cwd)
        except (OSError, RuntimeError, ValueError) as exc:
            log.debug("忽略无效外部工具文件路径 raw=%r cwd=%s error=%s", raw, cwd, exc)
            continue
        try:
            path.relative_to(root)
        except ValueError:
            log.debug("忽略工作区外部工具文件路径 raw=%r path=%s root=%s", raw, path, root)
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


class TurnFileChangeTracker:
    """Capture all file changes attributable to one Agent prompt."""

    def __init__(self, root: str | Path) -> None:
        self.root = resolve_file_path(root)
        self._before = workspace_snapshot(self.root)
        self._explicit_before: dict[str, FileState] = {}
        self._explicit_changes: dict[str, dict[str, Any]] = {}
        self._finalized: list[dict[str, Any]] | None = None

    def capture_tool_start(self, tool_name: str, arguments: dict[str, Any] | None) -> None:
        for path in external_tool_write_paths(tool_name, arguments, cwd=self.root):
            self._explicit_before.setdefault(str(path), read_file_state(path))

    def capture_tool_end(self, tool_name: str, arguments: dict[str, Any] | None) -> None:
        paths = external_tool_write_paths(tool_name, arguments, cwd=self.root)
        for path in paths:
            key = str(path)
            before = self._explicit_before.setdefault(key, read_file_state(path))
            change = file_change_from_states(path, before, read_file_state(path))
            if change is None:
                self._explicit_changes.pop(key, None)
            else:
                self._explicit_changes[key] = change

    def finalize(self) -> list[dict[str, Any]]:
        if self._finalized is not None:
            return list(self._finalized)
        for key, before in list(self._explicit_before.items()):
            change = file_change_from_states(key, before, read_file_state(key))
            if change is None:
                self._explicit_changes.pop(key, None)
            else:
                self._explicit_changes[key] = change
        snapshot_changes: list[dict[str, Any]] = []
        if self._before is not None:
            after = workspace_snapshot(self.root)
            if after is not None:
                snapshot_changes = changes_between_snapshots(self._before, after)
        # Exact tool-path diffs win over metadata-only snapshot entries.
        self._finalized = merge_changes(snapshot_changes, self._explicit_changes.values())
        return list(self._finalized)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


class WorkspaceChangeCoordinator:
    """Cooperative read/write leases for Crew-managed Agent workspaces."""

    def __init__(self) -> None:
        self._states: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            tuple[asyncio.Condition, dict[object, tuple[tuple[Path, ...], bool]]],
        ] = weakref.WeakKeyDictionary()

    def _state(self) -> tuple[
        asyncio.Condition,
        dict[object, tuple[tuple[Path, ...], bool]],
    ]:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = (asyncio.Condition(), {})
            self._states[loop] = state
        return state

    async def acquire(self, paths: Iterable[str | Path], *, read_only: bool = False) -> object:
        scopes = tuple(sorted({resolve_file_path(path) for path in paths}, key=str))
        token = object()
        condition, active = self._state()
        async with condition:
            await condition.wait_for(
                lambda: all(
                    read_only and active_read_only
                    or not any(_paths_overlap(scope, active) for scope in scopes for active in active_scopes)
                    for active_scopes, active_read_only in active.values()
                )
            )
            active[token] = (scopes, read_only)
        return token

    async def release(self, token: object) -> None:
        condition, active = self._state()
        async with condition:
            active.pop(token, None)
            condition.notify_all()

    @asynccontextmanager
    async def lease(
        self,
        paths: Iterable[str | Path],
        *,
        read_only: bool = False,
    ) -> AsyncIterator[None]:
        token = await self.acquire(paths, read_only=read_only)
        try:
            yield
        finally:
            await self.release(token)


workspace_change_coordinator = WorkspaceChangeCoordinator()
