"""文件工具公共辅助函数。

本文件从 Crew 的 tools/file_tools.py 和 tools/file_operations.py
复制/改造关键独立函数，用于增强 Crew 的 file_read / file_write / patch / glob / grep。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Optional

from crew.core.runctx import current_agent_workdir


# ---------------------------------------------------------------------------
# 路径解析（采用 _resolve_base_dir / _resolve_path_for_task）
# ---------------------------------------------------------------------------

def _resolve_base_dir() -> Path:
    """返回解析相对路径时的绝对基目录。

    优先使用 current_agent_workdir ContextVar；否则使用进程 cwd。
    与 Crew 的区别：Crew 用 task_id + TERMINAL_CWD；Crew 用 ContextVar。
    """
    raw = current_agent_workdir.get()
    base = Path(raw).expanduser() if raw else Path(os.getcwd())
    if not base.is_absolute():
        base = Path(os.getcwd()) / base
    return base.resolve()


def _resolve_path(filepath: str) -> Path:
    """把 filepath 解析为绝对 Path。"""
    p = Path(filepath).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (_resolve_base_dir() / p).resolve()


# ---------------------------------------------------------------------------
# 设备路径黑名单（实现）
# ---------------------------------------------------------------------------

_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.expanduser(path)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/environ", "/cmdline", "/maps")):
        return True
    return False


def _is_blocked_device(filepath: str) -> bool:
    """检查路径是否是会挂起或泄露敏感信息的设备文件。"""
    normalized = os.path.expanduser(filepath)
    if _is_blocked_device_path(normalized):
        return True
    try:
        resolved = os.path.realpath(normalized)
    except (OSError, ValueError):
        return False
    if resolved != normalized and _is_blocked_device_path(resolved):
        return True
    return False


# ---------------------------------------------------------------------------
# 敏感写入路径保护（实现）
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PREFIXES = (
    "/etc/",
    "/boot/",
    "/usr/lib/systemd/",
    "/private/etc/",
    "/private/boot/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}


def _get_crew_config_resolved() -> str | None:
    """返回 Crew 主配置文件的绝对路径（用于防止 agent 修改自身配置）。"""
    try:
        from crew.state.config import _get_user_config_dir, ROOT
        # 用户配置目录优先（冻结态 get_crew_home()，开发态 ROOT/config）
        user_yaml = _get_user_config_dir() / "config.yaml"
        if user_yaml.is_file():
            return str(user_yaml.resolve())
        return str((ROOT / "config" / "config.yaml").resolve())
    except Exception:
        return None


def _check_sensitive_path(filepath: str) -> str | None:
    """Return error message if path targets a sensitive system location."""
    try:
        resolved = str(_resolve_path(filepath))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(os.path.expanduser(filepath))
    raw_posix = os.path.expanduser(filepath).replace("\\", "/")
    resolved_posix = resolved.replace("\\", "/")
    normalized_posix = normalized.replace("\\", "/")
    err = (
        f"Refusing to write to sensitive system path: {filepath}\n"
        "Use the terminal tool with sudo if you need to modify system files."
    )
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if (
            resolved_posix.startswith(prefix)
            or normalized_posix.startswith(prefix)
            or raw_posix.startswith(prefix)
        ):
            return err
    if (
        resolved_posix in _SENSITIVE_EXACT_PATHS
        or normalized_posix in _SENSITIVE_EXACT_PATHS
        or raw_posix in _SENSITIVE_EXACT_PATHS
    ):
        return err
    crew_config = _get_crew_config_resolved()
    if crew_config and (resolved == crew_config or normalized == crew_config):
        return (
            f"Refusing to write to Crew config file: {filepath}\n"
            "Agent cannot modify security-sensitive configuration."
        )
    return None


def _is_expected_write_exception(exc: Exception) -> bool:
    """True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return True
    return False


# ---------------------------------------------------------------------------
# 行尾符与 BOM 处理（实现 tools/file_operations.py）
# ---------------------------------------------------------------------------

def _detect_line_ending(sample: str) -> Optional[str]:
    """Return the dominant line ending in ``sample`` or None."""
    if not sample:
        return None
    head = sample[:4096]
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    """Convert all line endings in ``text`` to ``target`` (``\\n`` or ``\\r\\n``)."""
    lf_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return lf_normalized
    if target == "\r\n":
        return lf_normalized.replace("\n", "\r\n")
    return text


_UTF8_BOM = "﻿"


def _strip_bom(text: str) -> tuple[str, bool]:
    """Return (text-without-leading-BOM, had_bom)."""
    if text and text.startswith(_UTF8_BOM):
        return text[len(_UTF8_BOM):], True
    return text, False


def _has_bom(text: Optional[str]) -> bool:
    """True if ``text`` begins with a UTF-8 BOM."""
    return bool(text) and text.startswith(_UTF8_BOM)


# ---------------------------------------------------------------------------
# 分页规范化（实现 tools/file_operations.py）
# ---------------------------------------------------------------------------

def _normalize_read_pagination(
    total_lines: int,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Return validated (offset, limit) for read_file pagination.

    Offset is 1-based for model-facing parameters (uses Crew conventions).
    """
    if offset is None:
        offset = 1
    else:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 1
    if limit is None:
        limit = total_lines
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = total_lines
    offset = max(1, min(offset, total_lines + 1))
    limit = max(0, limit)
    return offset, limit


def _normalize_search_pagination(
    total_hits: int,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Return validated (offset, limit) for glob/grep pagination."""
    if offset is None:
        offset = 1
    else:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 1
    if limit is None:
        limit = total_hits
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = total_hits
    offset = max(1, offset)
    limit = max(0, limit)
    return offset, limit


# ---------------------------------------------------------------------------
# 二进制扩展名检测（实现 tools/binary_extensions.py 的子集）
# ---------------------------------------------------------------------------

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svgz",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov", ".mkv",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".pyc", ".pyo", ".o", ".a", ".class", ".jar",
})


def _has_binary_extension(path: Path) -> bool:
    """True if the file extension suggests binary content."""
    return path.suffix.lower() in _BINARY_EXTENSIONS


# ---------------------------------------------------------------------------
# 读取上限（实现）
# ---------------------------------------------------------------------------

_DEFAULT_MAX_READ_CHARS = 100_000


def _get_max_read_chars() -> int:
    """Return configured max characters per file read."""
    try:
        from crew.state.config import load_config
        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("file", {}).get("read_max_chars")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    except Exception:
        pass
    return _DEFAULT_MAX_READ_CHARS


# ---------------------------------------------------------------------------
# 行号/片段辅助
# ---------------------------------------------------------------------------

def _apply_line_pagination(text: str, offset: int, limit: int) -> str:
    """Return the requested slice of lines (1-based offset)."""
    lines = text.splitlines(keepends=True)
    start = offset - 1
    end = start + limit
    return "".join(lines[start:end])


def _format_read_result(
    content: str,
    *,
    total_lines: int,
    file_size: int,
    offset: int = 1,
    limit: int | None = None,
    truncated: bool = False,
    hint: str = "",
) -> str:
    """把读取结果格式化为 JSON，与 Crew 返回结构接近。"""
    import json

    shown_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    payload: dict = {
        "success": True,
        "content": content,
        "total_lines": total_lines,
        "file_size": file_size,
        "offset": offset,
        "limit": limit if limit is not None else total_lines,
        "shown_lines": shown_lines,
    }
    if truncated:
        payload["truncated"] = True
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 通用输出截断（多个工具 handler 共用）
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 12000) -> str:
    """截断超长文本，避免 tool result 超出模型上下文。"""
    return text if len(text) <= limit else text[:limit] + "\n...[内容已截断]"
