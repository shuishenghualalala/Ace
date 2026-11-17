"""Resolve the owner and checkout instruction files for an Ace turn."""

from __future__ import annotations

import codecs
import logging
import os
import stat
from functools import lru_cache
from pathlib import Path

from crew.core.runctx import current_request_id
from crew.state.home import get_owner_runtime_home
from crew.tools.file_utils import FileConflictError, open_verified_read

logger = logging.getLogger(__name__)

_PROJECT_NAMES = ("AGENTS.md", ".crew.md", "CREW.md")
_MAX_SOURCE_BYTES = 100 * 1024


def _directory_chain(cwd: Path, owner_home: Path) -> tuple[Path, ...]:
    """Return cwd through its nearest checkout root, including linked worktrees."""
    directories = (cwd, *cwd.parents)
    for index, directory in enumerate(directories):
        if directory == owner_home:
            # A development install may keep customer workspaces inside the Ace checkout.
            break
        marker = directory / ".git"
        try:
            mode = marker.stat().st_mode
        except FileNotFoundError:
            pass
        except OSError:
            return (cwd,)
        else:
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                return directories[:index + 1]
    return (cwd,)


def _regular_file(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "agent_instruction_stat_failed request_id=%s file=%s error=%s",
            current_request_id.get(), path.name, type(exc).__name__,
        )
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _file_version(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@lru_cache(maxsize=128)
def _read_source(path: Path, version: tuple[int, int, int, int]) -> tuple[str, bool]:
    with open_verified_read(path) as (opened, handle):
        if _file_version(opened) != version:
            raise FileConflictError("指令文件在打开前已变化")
        data = handle.read(_MAX_SOURCE_BYTES + 1)
        if _file_version(os.fstat(handle.fileno())) != version:
            raise FileConflictError("指令文件在读取时已变化")
    truncated = len(data) > _MAX_SOURCE_BYTES
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    content = decoder.decode(data[:_MAX_SOURCE_BYTES], final=not truncated).strip()
    if truncated:
        logger.info(
            "agent_instruction_truncated request_id=%s file=%s limit_bytes=%d",
            current_request_id.get(), path.name, _MAX_SOURCE_BYTES,
        )
    return content, truncated


def _render_source(path: Path, info: os.stat_result, heading: str) -> str:
    try:
        content, truncated = _read_source(path, _file_version(info))
    except (OSError, UnicodeError, FileConflictError) as exc:
        logger.warning(
            "agent_instruction_read_failed request_id=%s file=%s error=%s",
            current_request_id.get(), path.name, type(exc).__name__,
        )
        return ""
    if truncated:
        content += f"\n\n（此指令文件只载入前 {_MAX_SOURCE_BYTES // 1024} KiB。）"
    return f"## {heading}\n\n{content}" if content else ""


def _project_source(directories: tuple[Path, ...]) -> tuple[Path, os.stat_result] | None:
    for directory in directories:
        for name in _PROJECT_NAMES:
            path = directory / name
            if (info := _regular_file(path)) is not None:
                return path, info
    return None


def build_context_files_prompt(cwd: str | None = None, *, workspace_instructions: str = "") -> str:
    """Load owner defaults and the nearest instruction file in the active checkout."""
    owner_home = get_owner_runtime_home(create=False).resolve()
    directories = _directory_chain(Path(cwd or os.getcwd()).expanduser().resolve(), owner_home)
    project = _project_source(directories)
    owner_path = owner_home / "AGENTS.md"
    blocks = []
    if project is None or project[0] != owner_path:
        if (info := _regular_file(owner_path)) is not None:
            blocks.append(_render_source(owner_path, info, "用户级指令（AGENTS.md）"))
    if workspace_instructions.strip():
        blocks.append(f"# 项目提示词\n{workspace_instructions.strip()}")
    if project is not None:
        path, info = project
        source = path.relative_to(directories[-1]).as_posix()
        blocks.append(_render_source(path, info, f"项目规则（{source}）"))
    return "\n\n".join(block for block in blocks if block)
