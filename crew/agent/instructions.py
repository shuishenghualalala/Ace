"""Resolve the owner and checkout instruction files for an Ace turn."""

from __future__ import annotations

import codecs
import logging
import os
import stat
from functools import lru_cache
from pathlib import Path

from crew.agent.prompt_budget import render_prompt_source
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


def _render_source(
    path: Path,
    info: os.stat_result,
    heading: str,
    max_tokens: int | None = None,
    notice: str = "",
) -> str:
    try:
        content, truncated = _read_source(path, _file_version(info))
    except (OSError, UnicodeError, FileConflictError) as exc:
        logger.warning(
            "agent_instruction_read_failed request_id=%s file=%s error=%s",
            current_request_id.get(), path.name, type(exc).__name__,
        )
        return ""
    if max_tokens is not None:
        return render_prompt_source(f"## {heading}", content, max_tokens, notice, truncated=truncated)
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


def build_context_files_prompt(
    cwd: str | None = None,
    *,
    workspace_instructions: str = "",
    budgets: dict[str, int] | None = None,
    allow_file_read: bool = True,
) -> str:
    """Load owner defaults and the nearest instruction file in the active checkout."""
    owner_home = get_owner_runtime_home(create=False).resolve()
    directories = _directory_chain(Path(cwd or os.getcwd()).expanduser().resolve(), owner_home)
    project = _project_source(directories)
    owner_path = owner_home / "AGENTS.md"
    blocks = []
    if project is None or project[0] != owner_path:
        if (info := _regular_file(owner_path)) is not None:
            blocks.append(_render_source(
                owner_path, info, "用户级指令（AGENTS.md）",
                budgets["owner_rules"] if budgets is not None else None,
                "用户级指令已截断；必要时请用户提供其余规则。",
            ))
    if workspace_instructions.strip():
        blocks.append(
            f"# 项目提示词\n{workspace_instructions.strip()}" if budgets is None else render_prompt_source(
                "# 项目提示词", workspace_instructions, budgets["workspace_rules"],
                "工作空间设置已截断；必要时请用户核对其余内容。",
            )
        )
    if project is not None:
        path, info = project
        source = path.relative_to(directories[-1]).as_posix()
        notice = (
            f"项目规则已截断；如需其余规则，在授权范围内用 file_read 分段读取 `{path}`。"
            if allow_file_read else "项目规则已截断；必要时请用户提供其余规则。"
        )
        blocks.append(_render_source(
            path, info, f"项目规则（{source}）",
            budgets["project_rules"] if budgets is not None else None, notice,
        ))
    return "\n\n".join(block for block in blocks if block)
