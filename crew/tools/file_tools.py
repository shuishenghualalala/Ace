"""文件操作工具：glob、grep、patch。

glob/grep 参考 OCC（Open-Claude-Code）的 GlobTool/GrepTool 设计：
  - glob：按 glob 模式找文件，结果按修改时间倒序
  - grep：按正则搜内容，支持 content/files_with_matches/count 三种输出模式

底层优先用 ripgrep；rg 解析顺序（managed_tools）：managed rg → 系统 rg → None。
rg 不可用时退回纯 Python 实现（wcmatch 编译 glob、re 搜内容），自动排除
.git/__pycache__/node_modules/.venv 等噪音目录。patch 是文本替换补丁。
"""

from __future__ import annotations

import asyncio
import difflib
import heapq
import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

import wcmatch.glob as wcglob

from crew.agent.file_mutation import (
    EditOp,
    file_mutation,
    guard_file_edit,
)
from crew.core.errors import ToolError
from crew.core.interfaces import ToolResultRetention
from crew.tools.file_utils import (
    _check_sensitive_path,
    _has_binary_extension,
    _truncate,
    FileConflictError,
    read_verified_bytes,
    stat_verified_file,
    MAX_READ_FILE_BYTES,
)
from crew.tools.managed_tools import (
    ChecksumMismatchError,
    ManagedToolUnavailableError,
    ensure_ripgrep,
)
from crew.tools.registry import Registry, tool_result
from crew.tools.security_guard import authorize_file_tool

logger = logging.getLogger(__name__)

# 噪音目录：rg 路径转 --glob '!<dir>'，Python 兜底用作 os.walk 剪枝黑名单
_NOISE_DIRS = (
    ".git", ".svn", ".hg", ".bzr", ".jj", ".sl",
    "__pycache__", "node_modules", ".venv", ".crew",
)

_GLOB_DEADLINE_SECONDS = 10
_GREP_DEADLINE_SECONDS = 15
_DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024  # 兜底跳过 10MB 以上文件
_DEFAULT_HEAD_LIMIT = 250
_RG_DEADLINE_SECONDS = 30
_SEARCH_BUFFER_BYTES = 8 * 1024 * 1024
_SEARCH_PATH_PAGE_CHARS = 40_000
_SEARCH_TEXT_PAGE_CHARS = 12_000
_MAX_GLOB_LIMIT = 1000
_MAX_GLOB_OFFSET = 10_000

_PARTIAL_LABELS = {
    "timeout": "扫描超时",
    "file_size_limit": "部分文件超过大小上限",
    "unsupported_encoding": "部分文件不是受支持的 UTF-8 文本",
    "unreadable_files": "部分文件无法读取或已变更",
    "unreadable_directories": "部分目录无法读取",
    "result_buffer_limit": "达到搜索缓冲上限",
    "ripgrep_error": "部分路径无法被 ripgrep 搜索",
    "file_order_unavailable": "部分文件的排序信息无法读取",
}

_WCGLOB_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR

# rg --type 的兜底扩展名映射（仅 Python 兜底用；rg 路径直接 --type）
_RG_TYPE_MAP: dict[str, list[str]] = {
    "py": ["*.py"], "js": ["*.js", "*.mjs", "*.cjs"], "ts": ["*.ts", "*.tsx"],
    "rust": ["*.rs"], "go": ["*.go"], "java": ["*.java"], "c": ["*.c", "*.h"],
    "cpp": ["*.cpp", "*.hpp", "*.cc", "*.hh"], "rb": ["*.rb"], "php": ["*.php"],
    "sh": ["*.sh", "*.bash"], "md": ["*.md"], "json": ["*.json"],
    "yaml": ["*.yaml", "*.yml"], "html": ["*.html", "*.htm"], "css": ["*.css"],
    "vue": ["*.vue"],
}


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

async def _resolve_rg() -> str | None:
    """三级解析可用 rg 路径：managed → 系统 → None（调用方走 Python 兜底）。"""
    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is not None and launch.managed:
        return None
    try:
        path = await ensure_ripgrep()
    except ChecksumMismatchError:
        logger.error("managed rg 校验失败，疑似供应链异常，改用 Python 兜底", exc_info=True)
        return None
    except ManagedToolUnavailableError:
        logger.debug("managed rg 不可用，改用 Python 兜底")
        return None
    return str(path) if path else None


def _noise_glob_args() -> list[str]:
    """生成 rg 排除噪音目录的 --glob 参数。"""
    args: list[str] = []
    for d in _NOISE_DIRS:
        args.extend(["--glob", f"!{d}"])
    return args


class _AsyncRipgrepUnavailable(Exception):
    pass


async def _run_rg(
    args: list[str], cwd: Path, scan: _SearchScan, *, delimiter: bytes = b"\n",
) -> tuple[int, str, str]:
    """Read bounded output and retire the owned child on timeout or cancellation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise ToolError(f"ripgrep 不可用: {exc}") from exc
    except NotImplementedError as exc:
        raise _AsyncRipgrepUnavailable from exc

    assert proc.stdout is not None and proc.stderr is not None
    output, diagnostics = bytearray(), bytearray()
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr, diagnostics, 4096))
    try:
        async with asyncio.timeout(max(0, scan.deadline - time.monotonic())):
            while chunk := await proc.stdout.read(64 * 1024):
                room = _SEARCH_BUFFER_BYTES - len(output)
                output.extend(chunk[:room])
                if len(chunk) > room:
                    scan.partial_reasons.add("result_buffer_limit")
                    break
            else:
                await proc.wait()
    except TimeoutError:
        scan.partial_reasons.add("timeout")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        cleanup = asyncio.gather(_drain_stream(proc.stdout), stderr_task, proc.wait())
        _, _, returncode = await asyncio.shield(cleanup)

    if scan.partial_reasons and not output.endswith(delimiter):
        # A killed process may leave an incomplete last record.
        del output[output.rfind(delimiter) + 1:]
    return returncode, output.decode("utf-8", "replace"), diagnostics.decode("utf-8", "replace")


async def _drain_stream(
    stream: asyncio.StreamReader, buffer: bytearray | None = None, limit: int = 0,
) -> None:
    while chunk := await stream.read(64 * 1024):
        if buffer is not None and len(buffer) < limit:
            buffer.extend(chunk[:limit - len(buffer)])


def _split_glob_filter(glob_str: str) -> list[str]:
    """按空格/逗号拆分 glob 过滤，保留 {...} 花括号整体不拆（对齐 OCC GrepTool）。"""
    result: list[str] = []
    for part in glob_str.split():
        if "{" in part or "}" in part:
            result.append(part)
        else:
            result.extend([p for p in part.split(",") if p])
    return [p for p in result if p]


def _compile_glob_pattern(pattern: str) -> Callable[[str], bool]:
    """编译 glob 工具的递归匹配模式：前缀 **/ + DOTMATCH（对齐 deepagents compile_recursive_glob）。"""
    compiled = wcglob.compile("**/" + pattern.lstrip("/"), flags=_WCGLOB_FLAGS | wcglob.DOTMATCH)

    def matcher(rel_path: str) -> bool:
        return bool(compiled.match(rel_path))

    return matcher


def _compile_grep_include(pattern: str) -> Callable[[str], bool]:
    """编译 grep 的 glob 过滤：无 / 匹配 basename 任意深度，有 / 匹配相对路径（ripgrep 语义）。"""
    compiled = wcglob.compile(pattern.lstrip("/"), flags=_WCGLOB_FLAGS)
    anchored = "/" in pattern

    if anchored:
        def matcher(rel_path: str) -> bool:
            return bool(compiled.match(rel_path))
    else:
        def matcher(rel_path: str) -> bool:
            return bool(compiled.match(Path(rel_path).name))

    return matcher


def _sort_files_by_mtime(rel_paths: list[str], newest_first: bool = True, *, scan: _SearchScan) -> list[str]:
    direction = -1 if newest_first else 1
    known: list[tuple[float, str]] = []
    for index, rel in enumerate(rel_paths):
        if scan.checkpoint():
            # Already-discovered paths remain useful; a partial scan has no continuation offset.
            return [path for _, path in sorted(known)] + sorted(rel_paths[index:])
        try:
            modified = (scan.root / rel).stat(follow_symlinks=False).st_mtime
        except OSError:
            scan.partial_reasons.add("file_order_unavailable")
            modified = 0.0
        known.append((direction * modified, rel))
    scan.checkpoint()
    return [path for _, path in sorted(known)]


def _prune_linked_directories(dirpath: str, dirnames: list[str]) -> int:
    """Prevent os.walk from traversing symlinks and Windows reparse directories."""
    kept: list[str] = []
    unreadable = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for name in dirnames:
        try:
            metadata = os.lstat(Path(dirpath) / name)
        except OSError:
            unreadable += 1
            continue
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            continue
        kept.append(name)
    dirnames[:] = kept
    return unreadable


@dataclass
class _SearchScan:
    root: Path
    timeout: float
    native: bool = False
    started: float = field(default_factory=lambda: time.monotonic())
    cancelled: threading.Event = field(default_factory=threading.Event)
    partial_reasons: set[str] = field(default_factory=set)
    scanned_files: int = 0
    skipped_files: int = 0
    skipped_directories: int = 0
    line_preview_clipped: bool = False

    @property
    def deadline(self) -> float:
        return self.started + self.timeout

    def checkpoint(self) -> bool:
        if self.cancelled.is_set() or "timeout" in self.partial_reasons:
            return True
        if time.monotonic() >= self.deadline:
            self.partial_reasons.add("timeout")
            return True
        return False

    def skip_file(self, reason: str) -> None:
        self.skipped_files += 1
        self.partial_reasons.add(reason)

    def skip_directories(self, count: int) -> None:
        if count:
            self.skipped_directories += count
            self.partial_reasons.add("unreadable_directories")


async def _run_python_scan(scan: _SearchScan, search: Callable[..., Any], *args: Any) -> Any:
    try:
        return await asyncio.to_thread(search, *args, scan=scan)
    except asyncio.CancelledError:
        scan.cancelled.set()
        raise


def _search_files(scan: _SearchScan) -> Iterator[Path]:
    if scan.checkpoint():
        return
    for dirpath, dirnames, filenames in os.walk(
        scan.root, onerror=lambda _exc: scan.skip_directories(1), followlinks=False,
    ):
        if scan.checkpoint():
            return
        dirnames[:] = sorted(d for d in dirnames if d not in _NOISE_DIRS)
        scan.skip_directories(_prune_linked_directories(dirpath, dirnames) or 0)
        for name in sorted(filenames):
            if scan.checkpoint():
                return
            yield Path(dirpath) / name


def _search_page(
    items: list[str], scan: _SearchScan, *, offset: int, limit: int,
    paths: bool = False, max_offset: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    page: list[str] = []
    remaining = _SEARCH_PATH_PAGE_CHARS if paths else _SEARCH_TEXT_PAGE_CHARS
    text_clipped = False
    end = min(len(items), offset + limit) if limit else len(items)
    for index in range(offset, end):
        row = items[index]
        size = len(json.dumps(row, ensure_ascii=False)) + 2 if paths else len(row) + 1
        if size > remaining:
            if not page and not paths:
                page.append(_truncate(row, max(1, remaining - 32)))
                text_clipped = True
            break
        page.append(row)
        remaining -= size

    complete = not scan.partial_reasons
    next_index = offset + len(page)
    more = len(items) > next_index
    details: dict[str, Any] = {
        "scan_complete": complete,
        "partial_reasons": sorted(scan.partial_reasons),
        "truncated": more or text_clipped or scan.line_preview_clipped,
        "duration_ms": round((time.monotonic() - scan.started) * 1000),
    }
    if not scan.native:
        details.update(scanned_files=scan.scanned_files, skipped_files=scan.skipped_files,
                       skipped_directories=scan.skipped_directories)
    if offset:
        details["applied_offset"] = offset

    if not complete:
        reasons = "、".join(_PARTIAL_LABELS[reason] for reason in details["partial_reasons"])
        details["search_notice"] = f"搜索未完成（{reasons}）。请缩小 path 或筛选范围后重新搜索。"
    elif more and page and (max_offset is None or next_index <= max_offset):
        details["next_offset"] = next_index
        details["search_notice"] = f"结果还有下一页。使用相同搜索条件并设置 offset={next_index}。"
    elif more:
        details["search_notice"] = "达到结果分页上限。请缩小 path 或筛选范围。"
    elif offset and not page:
        details["search_notice"] = "所请求的偏移量之后没有更多结果。"
    if text_clipped or scan.line_preview_clipped:
        details["search_notice"] = (str(details.get("search_notice", "")) + "部分行内容过长，可用 file_read 查看原文。")
    return page, details


def _bool_arg(args: dict[str, Any], key: str, default: bool) -> bool:
    """容错解析布尔参数（模型可能传字符串 'false'/'true'）。"""
    val = args.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(val)


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    try:
        value = args.get(key)
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _rg_lines(returncode: int, output: str, diagnostics: str, scan: _SearchScan, *, paths: bool = False) -> list[str]:
    if returncode not in (0, 1) and not scan.partial_reasons and not output:
        raise ToolError(f"ripgrep 搜索失败 (exit {returncode}): {diagnostics[:300]}")
    if not scan.partial_reasons and (returncode not in (0, 1) or diagnostics.strip()):
        scan.partial_reasons.add("ripgrep_error")
    if paths:
        rows = output.split("\0")
    else:
        rows = [line.removesuffix("\r") for line in output.split("\n")]
    return [line.removeprefix("./").removeprefix(".\\") for line in rows if line]


# ---------------------------------------------------------------------------
# glob 工具
# ---------------------------------------------------------------------------

GLOB_SCHEMA = {
    "name": "glob",
    "description": (
        "按 glob 模式查找文件（如 **/*.py、*.md、src/**/*.{ts,tsx}）。"
        "始终用 glob 做按文件名的查找，不要用 terminal 去跑 `find` 或 `ls`——"
        "glob 已针对权限与访问做过优化，体验更好、更易审查。\n"
        "结果按修改时间排序（最旧在前，对齐 OCC glob），"
        "自动排除 .git/__pycache__/node_modules/.venv 等噪音目录。"
        "底层优先用 ripgrep，不可用时退回 Python 实现。"
        "scan_complete=false 表示扫描未完成，请缩小 path 或模式再搜。结果含 next_offset 时可用同一条件续查。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_GLOB_LIMIT, "description": "最多返回多少条，默认 100"},
            "offset": {"type": "integer", "minimum": 0, "maximum": _MAX_GLOB_OFFSET, "description": "使用上次结果给出的 next_offset，默认 0"},
        },
        "required": ["pattern"],
    },
}


async def handle_glob(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolError("pattern 不能为空")
    root = await authorize_file_tool(
        args,
        operation="read",
        tool_name="glob",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    if not root.exists() or not root.is_dir():
        raise ToolError(f"搜索目录不存在或不是目录: {root}")
    limit = min(_MAX_GLOB_LIMIT, max(1, _int_arg(args, "limit", 100)))
    offset = max(0, _int_arg(args, "offset", 0))
    if offset > _MAX_GLOB_OFFSET:
        raise ToolError(f"glob offset 超过 {_MAX_GLOB_OFFSET}，请缩小搜索路径或模式。")

    rg = await _resolve_rg()
    window = offset + limit + 1
    if rg:
        scan = _SearchScan(root, _RG_DEADLINE_SECONDS, native=True)
        try:
            files = await _glob_via_rg(rg, pattern, scan)
        except _AsyncRipgrepUnavailable:
            rg = None
    if not rg:
        scan = _SearchScan(root, _GLOB_DEADLINE_SECONDS)
        files = await _run_python_scan(scan, _glob_via_python, pattern, root, window)

    page, details = _search_page(files, scan, offset=offset, limit=limit, paths=True, max_offset=_MAX_GLOB_OFFSET)
    return tool_result(
        success=True,
        **details,
        pattern=pattern,
        path=str(root),
        files=page,
        count=len(page),
    )


async def _glob_via_rg(rg: str, pattern: str, scan: _SearchScan) -> list[str]:
    cmd = [rg, "--no-config", "--files", "--null", "--hidden", "--path-separator", "/", "--glob", pattern]
    cmd.extend(_noise_glob_args())
    cmd.extend(["--", "."])
    rc, stdout, stderr = await _run_rg(cmd, scan.root, scan, delimiter=b"\0")
    lines = _rg_lines(rc, stdout, stderr, scan, paths=True)
    return await _run_python_scan(scan, _sort_files_by_mtime, lines, False)


def _glob_via_python(
    pattern: str, root: Path, window: int = _MAX_GLOB_LIMIT + 1, *, scan: _SearchScan | None = None,
) -> list[str]:
    """Keep only the oldest files needed for this page and its lookahead."""
    scan = scan if scan is not None else _SearchScan(root, _GLOB_DEADLINE_SECONDS)
    matcher = _compile_glob_pattern(pattern)

    def matches() -> Iterator[tuple[float, str]]:
        for full in _search_files(scan):
            rel = full.relative_to(root).as_posix()
            if not matcher(rel):
                scan.scanned_files += 1
                continue
            try:
                if full.is_symlink():
                    continue
                mtime = stat_verified_file(full).st_mtime
            except (FileConflictError, OSError):
                scan.skip_file("unreadable_files")
                continue
            if scan.checkpoint():
                return
            scan.scanned_files += 1
            yield mtime, rel

    return [rel for _, rel in heapq.nsmallest(window, matches())]


# ---------------------------------------------------------------------------
# grep 工具
# ---------------------------------------------------------------------------

GREP_SCHEMA = {
    "name": "grep",
    "description": (
        "在文件内容里按正则搜索（ripgrep 正则语法）。"
        "始终用 grep 做内容搜索，绝不要用 terminal 去跑 `grep` 或 `rg`——"
        "本工具已针对权限与访问做过优化。\n"
        "output_mode: files_with_matches（默认，只列文件名，按修改时间倒序）/ content（带行号和内容）/ count（每文件匹配数）。"
        "自动排除 .git/__pycache__/node_modules/.venv 等噪音目录。"
        "底层优先用 ripgrep，不可用时退回 Python（re）。"
        "scan_complete=false 时请缩小 path 或文件筛选后重试。结果含 next_offset 时可用同一条件续查。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（ripgrep 语法）"},
            "path": {"type": "string", "description": "搜索的文件或目录，默认当前目录"},
            "glob": {"type": "string", "description": "文件过滤 glob，如 *.py 或 *.{ts,tsx}（可空格/逗号分隔多个）"},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "输出模式，默认 files_with_matches",
            },
            "-i": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
            "-n": {"type": "boolean", "description": "content 模式是否显示行号，默认 true"},
            "-A": {"type": "integer", "description": "匹配行后显示的上下文行数（仅 content 模式）"},
            "-B": {"type": "integer", "description": "匹配行前显示的上下文行数（仅 content 模式）"},
            "-C": {"type": "integer", "description": "匹配行前后各显示的上下文行数（仅 content 模式），优先级高于 -A/-B"},
            "context": {"type": "integer", "description": "-C 的别名"},
            "type": {"type": "string", "description": "ripgrep 文件类型，如 py/js/rust（兜底用扩展名映射）"},
            "multiline": {"type": "boolean", "description": "多行模式（. 匹配换行）"},
            "head_limit": {"type": "integer", "minimum": 0, "description": "限制输出条数，默认 250；0 只受输出体积限制"},
            "offset": {"type": "integer", "minimum": 0, "description": "使用上次结果给出的 next_offset，默认 0"},
        },
        "required": ["pattern"],
    },
}


async def handle_grep(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolError("pattern 不能为空")
    target = await authorize_file_tool(
        args,
        operation="read",
        tool_name="grep",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    if not target.is_dir() and not target.is_file():
        raise ToolError(f"搜索路径必须是文件或目录: {target}")
    output_mode = str(args.get("output_mode") or "files_with_matches")
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ToolError(f"output_mode 必须是 content/files_with_matches/count，得到 {output_mode!r}")

    rg = await _resolve_rg()
    root = target if target.is_dir() else target.parent
    if rg:
        scan = _SearchScan(root, _RG_DEADLINE_SECONDS, native=True)
        try:
            return await _grep_via_rg(rg, args, target, output_mode, scan)
        except _AsyncRipgrepUnavailable:
            pass
    scan = _SearchScan(root, _GREP_DEADLINE_SECONDS)
    return await _run_python_scan(scan, _grep_via_python, args, target, output_mode)


def _grep_context_params(args: dict[str, Any]) -> tuple[int, int]:
    """解析 context：context > -C > (-B, -A)。返回 (before, after)。"""
    key = "context" if args.get("context") is not None else "-C"
    if args.get(key) is not None:
        n = max(0, _int_arg(args, key, 0))
        return n, n
    before = max(0, _int_arg(args, "-B", 0))
    after = max(0, _int_arg(args, "-A", 0))
    return before, after


async def _grep_via_rg(rg: str, args: dict[str, Any], target: Path, output_mode: str, scan: _SearchScan) -> str:
    """对齐 OCC GrepTool 的 rg 参数组装与输出格式化。"""
    pattern = str(args.get("pattern", ""))
    case_insensitive = _bool_arg(args, "-i", False)
    show_line_numbers = _bool_arg(args, "-n", True)
    glob_filter = str(args.get("glob") or "").strip()
    file_type = str(args.get("type") or "").strip()
    multiline = _bool_arg(args, "multiline", False)
    before, after = _grep_context_params(args)

    cmd: list[str] = [rg, "--no-config", "--hidden", "--path-separator", "/"]
    cmd.extend(_noise_glob_args())
    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])
    if case_insensitive:
        cmd.append("-i")
    if output_mode == "files_with_matches":
        cmd.extend(["-l", "--null"])
    elif output_mode == "count":
        cmd.extend(["-H", "-c", "--sort", "path"])
    else:  # content
        cmd.extend(["-H", "--sort", "path", "--max-columns", "500", "--max-columns-preview"])
        if show_line_numbers:
            cmd.append("-n")
        if before or after:
            cmd.extend(["-B", str(before), "-A", str(after)])
    if pattern.startswith("-"):
        cmd.extend(["-e", pattern])
    else:
        cmd.append(pattern)
    if file_type:
        cmd.extend(["--type", file_type])
    if glob_filter:
        for g in _split_glob_filter(glob_filter):
            cmd.extend(["--glob", g])

    cmd.extend(["--", "." if target == scan.root else target.name])
    paths = output_mode == "files_with_matches"
    rc, stdout, stderr = await _run_rg(cmd, scan.root, scan, delimiter=b"\0" if paths else b"\n")
    lines = _rg_lines(rc, stdout, stderr, scan, paths=paths)
    if paths:
        lines = await _run_python_scan(scan, _sort_files_by_mtime, lines)
    elif output_mode == "content":
        scan.line_preview_clipped = any(line.endswith(" [... omitted end of long line]") for line in lines)
        if before or after:
            lines = [line for line in lines if line != "--"]
    return _render_grep(args, output_mode, lines, scan)


def _grep_via_python(
    args: dict[str, Any], target: Path, output_mode: str, *, scan: _SearchScan | None = None,
) -> str:
    """纯 Python grep 兜底：os.walk + wcmatch 过滤 + re.search。"""
    pattern = str(args.get("pattern", ""))
    case_insensitive = _bool_arg(args, "-i", False)
    show_line_numbers = _bool_arg(args, "-n", True)
    glob_filter = str(args.get("glob") or "").strip()
    file_type = str(args.get("type") or "").strip()
    multiline = _bool_arg(args, "multiline", False)
    before, after = _grep_context_params(args)

    flags = re.MULTILINE
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(f"正则编译失败: {exc}") from exc

    if file_type and file_type not in _RG_TYPE_MAP:
        raise ToolError(f"托管搜索不支持文件类型 {file_type!r}，请改用 glob 过滤。")
    filters = _split_glob_filter(glob_filter) + _RG_TYPE_MAP.get(file_type, [])
    include_matchers = [_compile_grep_include(value) for value in filters]

    single_file = target.is_file()
    scan = scan if scan is not None else _SearchScan(target.parent if single_file else target, _GREP_DEADLINE_SECONDS)
    retained: dict[str, list[str]] = {}
    remaining = _SEARCH_BUFFER_BYTES
    for full in (target,) if single_file else _search_files(scan):
        if scan.checkpoint():
            break
        rel = full.relative_to(scan.root).as_posix()
        if include_matchers and not any(m(rel) for m in include_matchers):
            continue
        if _has_binary_extension(full):
            continue
        text = _read_grep_file(full, scan)
        if text is None:
            continue
        lines = text.splitlines(keepends=True)
        indices = _grep_line_indices(regex, text, lines, multiline=multiline, scan=scan)
        rows: list[str] = []
        for row in _grep_file_rows(rel, lines, indices, output_mode, before, after, show_line_numbers, scan):
            size = len(row.encode("utf-8")) + 1
            if size > remaining:
                scan.partial_reasons.add("result_buffer_limit")
                break
            rows.append(row)
            remaining -= size
        if rows:
            retained[rel] = rows
        if scan.checkpoint() or "result_buffer_limit" in scan.partial_reasons:
            break
        scan.scanned_files += 1

    if output_mode == "files_with_matches":
        items = _sort_files_by_mtime(list(retained), scan=scan)
    else:
        items = [row for path in sorted(retained) for row in retained[path]]
    return _render_grep(args, output_mode, items, scan)


def _read_grep_file(path: Path, scan: _SearchScan) -> str | None:
    try:
        if path.is_symlink():
            return None
        return read_verified_bytes(path, max_bytes=_DEFAULT_MAX_FILE_BYTES).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        scan.skip_file("unsupported_encoding")
    except ValueError:
        scan.skip_file("file_size_limit")
    except (FileConflictError, OSError):
        scan.skip_file("unreadable_files")
    return None


def _grep_line_indices(
    regex: re.Pattern[str], text: str, lines: list[str], *, multiline: bool, scan: _SearchScan,
) -> Iterator[int]:
    if multiline:
        previous = index = 0
        for match in regex.finditer(text):
            if scan.checkpoint():
                return
            index += text.count("\n", previous, match.start())
            previous = match.start()
            yield index
        return
    for index, line in enumerate(lines):
        if scan.checkpoint():
            return
        if regex.search(line):
            yield index


def _grep_file_rows(
    rel: str, lines: list[str], indices: Iterator[int], mode: str,
    before: int, after: int, numbers: bool, scan: _SearchScan,
) -> Iterator[str]:
    if mode == "files_with_matches":
        if next(indices, None) is not None:
            yield rel
        return
    if mode == "count":
        count = sum(1 for _ in indices)
        if count and not scan.checkpoint():
            yield f"{rel}:{count}"
        return

    end = 0
    for index in indices:
        start = max(end, index - before)
        end = min(len(lines), index + after + 1)
        for pos in range(start, end):
            if scan.checkpoint():
                return
            text = lines[pos].rstrip("\r\n")
            yield f"{rel}:{pos + 1}:{text}" if numbers else f"{rel}:{text}"


def _render_grep(args: dict[str, Any], output_mode: str, items: list[str], scan: _SearchScan) -> str:
    limit = max(0, _int_arg(args, "head_limit", _DEFAULT_HEAD_LIMIT))
    offset = max(0, _int_arg(args, "offset", 0))
    paths = output_mode == "files_with_matches"
    page, details = _search_page(items, scan, offset=offset, limit=limit, paths=paths)
    payload: dict[str, Any] = {"success": True, **details, "output_mode": output_mode}
    if limit and len(page) == limit and offset + len(page) < len(items):
        payload["applied_limit"] = limit
    if paths:
        payload.update(files=page, num_files=len(page))
    else:
        payload["content"] = "\n".join(page)
    if output_mode == "count":
        counts = (row.rpartition(":")[2] for row in page)
        payload.update(num_matches=sum(int(count) for count in counts if count.isdecimal()), num_files=len(page))
    elif output_mode == "content":
        payload["num_lines"] = len(page)
    if not page and details["scan_complete"] and not offset:
        payload["content"] = "No files found" if paths else "No matches found"
    return tool_result(**payload)


# ---------------------------------------------------------------------------
# patch 工具（匹配/写入载体下沉到 crew.agent.file_mutation，这里只做参数与呈现）
# ---------------------------------------------------------------------------

PATCH_SCHEMA = {
    "name": "patch",
    "description": (
        "对文本文件执行替换补丁：把 old 替换为 new。匹配在 LF 归一化域进行"
        "（CRLF 文件写回时按原行尾还原），保留原文件 UTF-8 BOM；"
        "拒绝写入敏感系统路径。old 默认必须唯一匹配；精确失败时自动做模糊兜底"
        "（行尾空白/智能引号/Unicode 破折号归一化），命中方式与得分随结果上报；"
        "匹配失败与多处歧义都会给出含行号的诊断。可用 edits 一次事务化应用多处编辑。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文本文件"},
            "old": {"type": "string", "description": "要被替换的原文本（与 new 二选一；使用 edits 时不必填）"},
            "new": {"type": "string", "description": "替换后的新文本（与 old 二选一；使用 edits 时不必填）"},
            "count": {"type": "integer", "description": "最多替换次数，默认 1（要求唯一）；0 表示全部"},
            "edits": {
                "type": "array",
                "description": "一次调用按序应用多处编辑（事务化：全部匹配成功才统一写入，"
                "任一处失败文件保持原样）。每处 old 必须唯一匹配且与其它编辑互不重叠。",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "description": "要被替换的原文本"},
                        "new": {"type": "string", "description": "替换后的新文本"},
                    },
                    "required": ["old", "new"],
                },
            },
        },
        "required": ["path"],
    },
}


def _parse_patch_ops(args: dict[str, Any]) -> list[EditOp]:
    """把 patch 参数规整为编辑操作列表：edits[] 优先，否则 old/new/count。"""
    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        ops: list[EditOp] = []
        for index, item in enumerate(edits):
            if not isinstance(item, dict):
                raise ToolError(f"edits[{index}] 必须是对象")
            old = item.get("old")
            new = item.get("new")
            if not isinstance(old, str) or not old:
                raise ToolError(f"edits[{index}].old 必须是非空字符串")
            if not isinstance(new, str):
                raise ToolError(f"edits[{index}].new 必须是字符串")
            if item.get("count") not in (None, 1):
                raise ToolError("edits[] 模式要求每处编辑唯一匹配，不支持 count，请拆分调用")
            ops.append(EditOp(old=old, new=new))
        return ops
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not old:
        raise ToolError("必须提供非空的 old/new，或非空的 edits 列表")
    if not isinstance(new, str):
        raise ToolError("new 必须是字符串")
    try:
        count = int(args.get("count", 1))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"count 必须是整数，得到 {args.get('count')!r}") from exc
    return [EditOp(old=old, new=new, count=count)]


async def handle_patch(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    path = await authorize_file_tool(
        args,
        operation="patch",
        tool_name="patch",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    sensitive = _check_sensitive_path(str(args.get("path", "")))
    if sensitive:
        raise ToolError(sensitive)
    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")

    ops = _parse_patch_ops(args)
    outcome = await file_mutation.edit_text(
        path,
        ops,
        expected=guard_file_edit(path),
        max_bytes=MAX_READ_FILE_BYTES,
    )

    diff = "".join(
        difflib.unified_diff(
            outcome.before.splitlines(keepends=True),
            outcome.after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return tool_result(
        success=True,
        path=str(path),
        replacements=len(outcome.plan.matches),
        matches=[match.__dict__ for match in outcome.plan.matches],
        diff=_truncate(diff, 4000),
        bom_preserved=outcome.had_bom,
        line_ending=repr(outcome.line_ending)[1:-1] if outcome.line_ending else "\\n",
    )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register_file_tools(
    registry: Registry,
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> None:
    registry.register(
        name="glob",
        toolset="file",
        schema=GLOB_SCHEMA,
        handler=partial(
            handle_glob,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="查找文件",
ui_label_template="查找 {pattern}",
        always_load=True,
        search_hint="search files grep ripgrep find filename content glob",
        result_retention=ToolResultRetention.TEMPORARY,
    )
    registry.register(
        name="grep",
        toolset="file",
        schema=GREP_SCHEMA,
        handler=partial(
            handle_grep,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="搜索内容",
        ui_label_template="搜索 {pattern}",
        result_retention=ToolResultRetention.TEMPORARY,
    )
    registry.register(
        name="patch",
        toolset="file",
        schema=PATCH_SCHEMA,
        handler=partial(
            handle_patch,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="修改文件",
        ui_label_template="修改 {path}",
        always_load=True,
        search_hint="patch edit replace modify file text",
        result_retention=ToolResultRetention.TEMPORARY,
    )
