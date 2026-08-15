"""文件操作工具：glob、grep、patch。

glob/grep 参考 OCC（Open-Claude-Code）的 GlobTool/GrepTool 设计：
  - glob：按 glob 模式找文件，结果按修改时间倒序
  - grep：按正则搜内容，支持 content/files_with_matches/count 三种输出模式

底层优先用 ripgrep；默认只接受固定版本、固定摘要的 managed rg。
系统 PATH 上的 rg 仅在操作员显式选择 system installer 时使用。
rg 不可用时退回纯 Python 实现（wcmatch 编译 glob、re 搜内容），自动排除
.git/__pycache__/node_modules/.venv 等噪音目录。patch 是文本替换补丁。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import wcmatch.glob as wcglob

from crew.core.errors import ToolError
from crew.tools.file_utils import (
    _DEFAULT_MAX_FILE_BYTES,
    FileConflictError,
    _check_sensitive_path,
    _detect_line_ending,
    _has_binary_extension,
    _normalize_line_endings,
    _strip_bom,
    _truncate,
    atomic_replace_bytes,
    read_verified_bytes,
    snapshot_file,
    stat_verified_file,
)
from crew.tools.managed_tools import (
    ChecksumMismatchError,
    ManagedToolUnavailableError,
    ensure_ripgrep,
)
from crew.tools.registry import Registry, tool_result
from crew.tools.redact import safe_public_error
from crew.tools.security_guard import AuthorizedFileTarget, authorize_file_tool

logger = logging.getLogger(__name__)

# 噪音目录：rg 路径转 --glob '!<dir>'，Python 兜底用作 os.walk 剪枝黑名单
_NOISE_DIRS = (
    ".git", ".svn", ".hg", ".bzr", ".jj", ".sl",
    "__pycache__", "node_modules", ".venv", ".crew",
)

_GLOB_DEADLINE_SECONDS = 10
_GREP_DEADLINE_SECONDS = 15
_DEFAULT_HEAD_LIMIT = 250
_MAX_SEARCH_DEPTH = 64
_MAX_SEARCH_FILES = 50_000
_MAX_SEARCH_RESULTS = 5_000
_MAX_SEARCH_CONTEXT_LINES = 100
_MAX_SEARCH_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_SEARCH_OUTPUT_CHARS = 12_000
_MAX_RG_STDOUT_BYTES = 2 * 1024 * 1024
_MAX_RG_STDERR_BYTES = 64 * 1024

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
    """Resolve verified rg, or use the in-process bounded fallback."""
    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is not None and launch.managed:
        return None
    try:
        path = await ensure_ripgrep()
    except ChecksumMismatchError:
        logger.exception("managed rg 校验失败，疑似供应链异常，改用 Python 兜底")
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


def _run_rg(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    """跑 rg 子进程。exit 1 = 无匹配，视为正常；其他非零退出抛错。"""
    from crew.security.launch import minimal_inherited_environment

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=minimal_inherited_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(safe_public_error(exc, "ripgrep 不可用")) from exc

    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain(stream, output: bytearray, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = limit - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    proc.kill()
                    return
        finally:
            stream.close()

    assert proc.stdout is not None and proc.stderr is not None
    readers = [
        threading.Thread(
            target=drain,
            args=(proc.stdout, stdout, _MAX_RG_STDOUT_BYTES),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(proc.stderr, stderr, _MAX_RG_STDERR_BYTES),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        return_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise ToolError(f"ripgrep 搜索超时（>{timeout}s）") from exc
    finally:
        for reader in readers:
            reader.join(timeout=2)
    if overflow.is_set():
        raise ToolError("ripgrep 搜索输出超过安全上限")
    return (
        return_code,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


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


def _sort_files_by_mtime(base: Path, rel_paths: list[str]) -> list[str]:
    """按 mtime 倒序排（最新在前，对齐 OCC GrepTool），mtime 相同按路径升序 tiebreaker。stat 失败 mtime=0。"""
    def mtime(rel: str) -> float:
        try:
            return stat_verified_file(base / rel).st_mtime
        except (FileConflictError, OSError):
            return 0.0
    return sorted(rel_paths, key=lambda r: (-mtime(r), r))


def _prune_linked_directories(dirpath: str, dirnames: list[str]) -> None:
    """Prevent os.walk from traversing symlinks and Windows reparse directories."""
    kept: list[str] = []
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for name in dirnames:
        try:
            metadata = os.lstat(Path(dirpath) / name)
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            continue
        kept.append(name)
    dirnames[:] = kept


def _directory_identity(path: Path) -> tuple[int, int, int]:
    """Capture the directory object used by a bounded search walk."""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ToolError("搜索根目录不可用") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or (reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ToolError("搜索根目录不是安全的普通目录")
    return (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_ctime_ns))


def _assert_directory_identity(path: Path, expected: tuple[int, int, int]) -> None:
    if _directory_identity(path) != expected:
        raise ToolError("搜索目录在操作期间发生变化")


def _enforce_walk_budget(
    root: Path,
    dirpath: str,
    dirnames: list[str],
    filenames: list[str],
    files_seen: int,
) -> int:
    try:
        depth = len(Path(dirpath).relative_to(root).parts)
    except ValueError as exc:
        raise ToolError("搜索遍历离开授权根") from exc
    if depth >= _MAX_SEARCH_DEPTH and dirnames:
        raise ToolError(f"搜索目录深度超过上限 {_MAX_SEARCH_DEPTH}")
    files_seen += len(filenames)
    if files_seen > _MAX_SEARCH_FILES:
        raise ToolError(f"搜索文件数量超过上限 {_MAX_SEARCH_FILES}")
    return files_seen


def _bounded_head_limit(requested: int) -> int:
    if requested <= 0:
        return _MAX_SEARCH_RESULTS
    return min(requested, _MAX_SEARCH_RESULTS)


def _preflight_search_tree(root: Path, *, timeout: int) -> None:
    """Bound metadata traversal before delegating content search to ripgrep."""

    root_identity = _directory_identity(root)
    deadline = time.monotonic() + timeout
    files_seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        _assert_directory_identity(root, root_identity)
        dirnames[:] = [name for name in dirnames if name not in _NOISE_DIRS]
        _prune_linked_directories(dirpath, dirnames)
        files_seen = _enforce_walk_budget(
            root,
            dirpath,
            dirnames,
            filenames,
            files_seen,
        )
        if time.monotonic() > deadline:
            raise ToolError(f"搜索目录预检超时（>{timeout}s）")
    _assert_directory_identity(root, root_identity)


def _apply_pagination(items: list[str], head_limit: int, offset: int) -> tuple[list[str], bool]:
    """OCC applyHeadLimit 语义：head_limit=0 不限；否则 slice(offset, offset+limit)。"""
    if head_limit == 0:
        return items[offset:], False
    page = items[offset:offset + head_limit]
    truncated = len(items) > offset + head_limit
    return page, truncated


def _bound_string_items(items: list[str]) -> tuple[list[str], bool]:
    bounded: list[str] = []
    chars = 0
    for item in items:
        added = len(item) + (1 if bounded else 0)
        if chars + added > _MAX_SEARCH_OUTPUT_CHARS:
            return bounded, True
        bounded.append(item)
        chars += added
    return bounded, False


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
        return int(args.get(key) or default)
    except (TypeError, ValueError):
        return default


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
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            "limit": {"type": "integer", "description": "最多返回多少条，默认 100"},
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
    limit = min(_MAX_SEARCH_RESULTS, max(1, _int_arg(args, "limit", 100)))

    # The authorized path is only a pathname.  A native rg process cannot keep
    # that directory object pinned between approval, traversal, and open.  Keep
    # the helper for explicit/native callers and use the verified walker at the
    # model-facing boundary until a dirfd/snapshot implementation exists.
    files = await asyncio.to_thread(
        _glob_via_python,
        pattern,
        root,
        max_results=limit,
    )

    page, output_truncated = _bound_string_items(files[:limit])
    truncated = len(files) > limit or output_truncated
    return tool_result(
        success=True,
        pattern=pattern,
        path=str(root),
        files=page,
        count=len(page),
        truncated=truncated,
    )


def _glob_via_rg(rg: str, pattern: str, root: Path, limit: int) -> list[str]:
    """rg --files --glob <pattern> --sort=modified --hidden + 噪音排除。返回相对 root 路径。"""
    _preflight_search_tree(root, timeout=_GLOB_DEADLINE_SECONDS)
    collect = limit + 1  # 多取一个用于判断 truncated
    cmd = [
        rg,
        "--files",
        "--no-follow",
        "--max-depth",
        str(_MAX_SEARCH_DEPTH),
        "--sort=modified",
        "--hidden",
        "--glob",
        pattern,
    ]
    cmd.extend(_noise_glob_args())
    cmd.extend(["--", "."])
    rc, stdout, _stderr = _run_rg(cmd, root)
    _assert_directory_identity(root, _directory_identity(root))
    if rc not in (0, 1):
        raise ToolError(f"ripgrep glob 失败 (exit {rc})")
    files: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            files.append(line.removeprefix("./"))
            if len(files) >= collect:
                break
    return files


def _glob_via_python(
    pattern: str,
    root: Path,
    *,
    max_results: int = _MAX_SEARCH_RESULTS,
) -> list[str]:
    """纯 Python glob：os.walk + wcmatch 匹配 + 噪音剪枝 + mtime 倒序。"""
    matcher = _compile_glob_pattern(pattern)
    root_identity = _directory_identity(root)
    deadline = time.monotonic() + _GLOB_DEADLINE_SECONDS
    found: list[tuple[float, str]] = []
    files_seen = 0
    collect = min(max(1, max_results), _MAX_SEARCH_RESULTS) + 1
    for dirpath, dirnames, filenames in os.walk(root):
        _assert_directory_identity(root, root_identity)
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
        _prune_linked_directories(dirpath, dirnames)
        files_seen = _enforce_walk_budget(
            root,
            dirpath,
            dirnames,
            filenames,
            files_seen,
        )
        if time.monotonic() > deadline:
            raise ToolError(f"glob 搜索超时（>{_GLOB_DEADLINE_SECONDS}s）")
        for fn in filenames:
            _assert_directory_identity(root, root_identity)
            if time.monotonic() > deadline:
                raise ToolError(f"glob 搜索超时（>{_GLOB_DEADLINE_SECONDS}s）")
            full = Path(dirpath) / fn
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            if not matcher(rel):
                continue
            try:
                mtime = stat_verified_file(full).st_mtime
            except (FileConflictError, OSError):
                continue
            if len(found) < collect:
                found.append((mtime, rel))
    _assert_directory_identity(root, root_identity)
    # mtime 升序（最旧在前，对齐 OCC GlobTool 的 --sort=modified），path 升序 tiebreaker
    found.sort(key=lambda x: (x[0], x[1]))
    return [rel for _, rel in found]


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
            "head_limit": {"type": "integer", "description": "限制输出条数，默认 250；0 表示不限"},
            "offset": {"type": "integer", "description": "跳过前 N 条，默认 0"},
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
    if not target.exists():
        raise ToolError(f"搜索路径不存在: {target}")
    output_mode = str(args.get("output_mode") or "files_with_matches")
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ToolError(f"output_mode 必须是 content/files_with_matches/count，得到 {output_mode!r}")

    # See handle_glob: an unpinned native process cannot safely consume the
    # model-authorized pathname.  The Python walker checks the root identity
    # and each opened regular file instead.
    return await asyncio.to_thread(_grep_via_python, args, target, output_mode)


def _grep_context_params(args: dict[str, Any]) -> tuple[int, int]:
    """解析 context：context > -C > (-B, -A)。返回 (before, after)。"""
    ctx = args.get("context")
    if ctx is None:
        ctx = args.get("-C")
    if ctx is not None:
        n = min(_MAX_SEARCH_CONTEXT_LINES, max(0, int(ctx)))
        return n, n
    before = min(_MAX_SEARCH_CONTEXT_LINES, max(0, _int_arg(args, "-B", 0)))
    after = min(_MAX_SEARCH_CONTEXT_LINES, max(0, _int_arg(args, "-A", 0)))
    return before, after


def _grep_via_rg(rg: str, args: dict[str, Any], target: Path, output_mode: str) -> str:
    """对齐 OCC GrepTool 的 rg 参数组装与输出格式化。"""
    pattern = str(args.get("pattern", ""))
    case_insensitive = _bool_arg(args, "-i", False)
    show_line_numbers = _bool_arg(args, "-n", True)
    glob_filter = str(args.get("glob") or "").strip()
    file_type = str(args.get("type") or "").strip()
    multiline = _bool_arg(args, "multiline", False)
    head_limit = _bounded_head_limit(_int_arg(args, "head_limit", _DEFAULT_HEAD_LIMIT))
    offset = min(_MAX_SEARCH_RESULTS, max(0, _int_arg(args, "offset", 0)))
    before, after = _grep_context_params(args)

    target_is_dir = target.is_dir()
    root = target if target_is_dir else target.parent
    root_identity = _directory_identity(root)
    if target_is_dir:
        _preflight_search_tree(target, timeout=_GREP_DEADLINE_SECONDS)
    else:
        try:
            target_info = stat_verified_file(target)
        except (FileConflictError, OSError) as exc:
            raise ToolError("grep 搜索目标不是安全的普通文件") from exc
        if target_info.st_size > _DEFAULT_MAX_FILE_BYTES:
            raise ToolError("grep 搜索文件超过读取上限")

    cmd: list[str] = [
        rg,
        "--hidden",
        "--no-follow",
        "--max-depth",
        str(_MAX_SEARCH_DEPTH),
        "--max-filesize",
        str(_DEFAULT_MAX_FILE_BYTES),
        "--max-columns",
        "500",
    ]
    cmd.extend(_noise_glob_args())
    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])
    if case_insensitive:
        cmd.append("-i")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:  # content
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

    if target.is_dir():
        cwd = target
        cmd.extend(["--", "."])
    else:
        cwd = target.parent
        cmd.extend(["--", target.name])

    rc, stdout, stderr = _run_rg(cmd, cwd)
    _assert_directory_identity(root, root_identity)
    if rc not in (0, 1):
        raise ToolError(safe_public_error(f"ripgrep 失败 (exit {rc}): {stderr[:300]}", "ripgrep 失败"))

    lines = [line.removeprefix("./") for line in stdout.splitlines() if line]
    if output_mode == "files_with_matches":
        page, truncated = _apply_pagination(lines, head_limit, offset)
        if not page:
            return tool_result(success=True, output_mode=output_mode, files=[], num_files=0,
                               content="No files found")
        page = _sort_files_by_mtime(target, page)
        page, output_truncated = _bound_string_items(page)
        truncated = truncated or output_truncated
        payload: dict[str, Any] = {"success": True, "output_mode": output_mode,
                                   "files": page, "num_files": len(page)}
        if truncated:
            payload["applied_limit"] = head_limit
        if offset:
            payload["applied_offset"] = offset
        return tool_result(**payload)
    if output_mode == "count":
        page, truncated = _apply_pagination(lines, head_limit, offset)
        if not page:
            return tool_result(success=True, output_mode=output_mode, content="No matches found",
                               num_matches=0, num_files=0)
        total = 0
        for line in page:
            idx = line.rfind(":")
            try:
                total += int(line[idx + 1:] if idx > 0 else line)
            except ValueError:
                pass
        payload = {"success": True, "output_mode": output_mode,
                   "content": _truncate("\n".join(page)), "num_matches": total, "num_files": len(page)}
        if truncated:
            payload["applied_limit"] = head_limit
        if offset:
            payload["applied_offset"] = offset
        return tool_result(**payload)
    # content
    page, truncated = _apply_pagination(lines, head_limit, offset)
    if not page:
        return tool_result(success=True, output_mode=output_mode, content="No matches found", num_lines=0)
    payload = {"success": True, "output_mode": output_mode,
               "content": _truncate("\n".join(page)), "num_lines": len(page)}
    if truncated:
        payload["applied_limit"] = head_limit
    if offset:
        payload["applied_offset"] = offset
    return tool_result(**payload)


def _grep_via_python(args: dict[str, Any], target: Path, output_mode: str) -> str:
    """纯 Python grep 兜底：os.walk + wcmatch 过滤 + re.search。"""
    pattern = str(args.get("pattern", ""))
    case_insensitive = _bool_arg(args, "-i", False)
    show_line_numbers = _bool_arg(args, "-n", True)
    glob_filter = str(args.get("glob") or "").strip()
    file_type = str(args.get("type") or "").strip()
    multiline = _bool_arg(args, "multiline", False)
    head_limit = _bounded_head_limit(_int_arg(args, "head_limit", _DEFAULT_HEAD_LIMIT))
    offset = min(_MAX_SEARCH_RESULTS, max(0, _int_arg(args, "offset", 0)))
    before, after = _grep_context_params(args)

    flags = re.MULTILINE
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(safe_public_error(exc, "正则编译失败")) from exc

    include_matchers: list[Callable[[str], bool]] = []
    if glob_filter:
        include_matchers.extend(_compile_grep_include(g) for g in _split_glob_filter(glob_filter))
    if file_type:
        for g in _RG_TYPE_MAP.get(file_type, []):
            include_matchers.append(_compile_grep_include(g))

    target_is_dir = target.is_dir()
    root = target if target_is_dir else target.parent
    root_identity = _directory_identity(root)
    deadline = time.monotonic() + _GREP_DEADLINE_SECONDS
    file_matches: dict[str, list[tuple[int, str]]] = {}  # rel -> [(line_num, line)]
    file_lines: dict[str, list[str]] = {}  # 只有有匹配的文件存全部行（content 上下文用）
    file_counts: dict[str, int] = {}
    collection_limit = head_limit + offset + 1
    collected_matches = 0
    collection_saturated = False
    files_seen = 0
    total_bytes_read = 0

    def search_file(full: Path, *, follow_symlink: bool = False) -> None:
        nonlocal collected_matches, collection_saturated, total_bytes_read
        _assert_directory_identity(root, root_identity)
        try:
            rel = full.relative_to(root).as_posix()
        except ValueError:
            return
        if include_matchers and not any(m(rel) for m in include_matchers):
            return
        if _has_binary_extension(full):
            return
        # 拒绝遍历到的符号链接：工作区内的文件 symlink 可能指向授权根外，跟随它读取
        # 会绕过文件读取审批与 native sandbox（H-4）。os.walk 已 followlinks=False
        # 不递归目录 symlink；这里在 open 前按 lstat 拒绝文件 symlink。单文件 target
        # 由上层授权层 canonicalize 后授权，显式 follow_symlink=True 放行。
        if not follow_symlink:
            try:
                if full.is_symlink():
                    return
            except OSError:
                return
        try:
            content = read_verified_bytes(full, max_bytes=_DEFAULT_MAX_FILE_BYTES)
            total_bytes_read += len(content)
            if total_bytes_read > _MAX_SEARCH_TOTAL_BYTES:
                raise ToolError(
                    f"grep 读取总量超过上限 {_MAX_SEARCH_TOTAL_BYTES} 字节"
                )
            lines_list = content.decode("utf-8", errors="strict").splitlines(keepends=True)
        except ToolError:
            raise
        except (FileConflictError, UnicodeDecodeError, OSError, ValueError):
            return
        if multiline:
            # 整文件 search：DOTALL 让 . 匹配换行，定位匹配起始行号（兜底不完美，但跨行能命中）
            whole = "".join(lines_list)
            for m in regex.finditer(whole):
                start_line = whole.count("\n", 0, m.start()) + 1
                first_line = m.group(0).splitlines()[0] if m.group(0) else ""
                file_counts[rel] = file_counts.get(rel, 0) + 1
                if output_mode == "files_with_matches":
                    file_matches.setdefault(rel, []).append((start_line, first_line))
                    return
                if output_mode == "content":
                    if collected_matches < collection_limit:
                        file_matches.setdefault(rel, []).append((start_line, first_line))
                        file_lines.setdefault(rel, lines_list)
                        collected_matches += 1
                    else:
                        collection_saturated = True
        else:
            for i, line in enumerate(lines_list, 1):
                if regex.search(line):
                    file_counts[rel] = file_counts.get(rel, 0) + 1
                    if output_mode == "files_with_matches":
                        file_matches.setdefault(rel, []).append((i, line.rstrip("\n")))
                        return
                    if output_mode == "content":
                        if collected_matches < collection_limit:
                            file_matches.setdefault(rel, []).append((i, line.rstrip("\n")))
                            file_lines.setdefault(rel, lines_list)
                            collected_matches += 1
                        else:
                            collection_saturated = True
        if output_mode == "count" and rel in file_counts:
            file_matches.setdefault(rel, [])

    if not target_is_dir:
        search_file(target, follow_symlink=True)
    else:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            _assert_directory_identity(root, root_identity)
            dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
            _prune_linked_directories(dirpath, dirnames)
            files_seen = _enforce_walk_budget(
                root,
                dirpath,
                dirnames,
                filenames,
                files_seen,
            )
            if time.monotonic() > deadline:
                raise ToolError(f"grep 搜索超时（>{_GREP_DEADLINE_SECONDS}s）")
            for fn in filenames:
                _assert_directory_identity(root, root_identity)
                if time.monotonic() > deadline:
                    raise ToolError(f"grep 搜索超时（>{_GREP_DEADLINE_SECONDS}s）")
                search_file(Path(dirpath) / fn)

    _assert_directory_identity(root, root_identity)

    if output_mode == "files_with_matches":
        files = _sort_files_by_mtime(root, list(file_matches.keys()))
        page, truncated = _apply_pagination(files, head_limit, offset)
        truncated = truncated or len(files) > _MAX_SEARCH_RESULTS
        page, output_truncated = _bound_string_items(page)
        truncated = truncated or output_truncated
        if not page:
            return tool_result(success=True, output_mode=output_mode, files=[], num_files=0,
                               content="No files found")
        payload: dict[str, Any] = {"success": True, "output_mode": output_mode,
                                   "files": page, "num_files": len(page)}
        if truncated:
            payload["applied_limit"] = head_limit
        if offset:
            payload["applied_offset"] = offset
        return tool_result(**payload)

    if output_mode == "count":
        items = [f"{rel}:{file_counts[rel]}" for rel in sorted(file_counts.keys())]
        page, truncated = _apply_pagination(items, head_limit, offset)
        if not page:
            return tool_result(success=True, output_mode=output_mode, content="No matches found",
                               num_matches=0, num_files=0)
        # num_matches 只算 page 内（对齐 OCC/rg 路径：applyHeadLimit 先分页再累加）
        total = 0
        for line in page:
            idx = line.rfind(":")
            try:
                total += int(line[idx + 1:] if idx > 0 else line)
            except ValueError:
                pass
        payload = {"success": True, "output_mode": output_mode,
                   "content": _truncate("\n".join(page)),
                   "num_matches": total, "num_files": len(page)}
        if truncated:
            payload["applied_limit"] = head_limit
        if offset:
            payload["applied_offset"] = offset
        return tool_result(**payload)

    # content
    out_lines: list[str] = []
    output_chars = 0
    output_saturated = False
    output_line_limit = head_limit + offset + 1
    for rel in sorted(file_matches.keys()):
        if output_saturated:
            break
        full_lines = file_lines.get(rel, [])
        for line_num, _text in file_matches[rel]:
            if output_saturated:
                break
            start = max(0, line_num - 1 - before)
            end = min(len(full_lines), line_num + after)
            for j in range(start, end):
                ln = j + 1
                text = full_lines[j].rstrip("\n")
                rendered = f"{rel}:{ln}:{text}" if show_line_numbers else f"{rel}:{text}"
                if len(out_lines) >= output_line_limit:
                    output_saturated = True
                    break
                remaining = _MAX_SEARCH_OUTPUT_CHARS - output_chars
                if remaining <= 0:
                    output_saturated = True
                    break
                separator_size = 1 if out_lines else 0
                available = remaining - separator_size
                if available <= 0:
                    output_saturated = True
                    break
                if len(rendered) > available:
                    out_lines.append(rendered[:available])
                    output_chars = _MAX_SEARCH_OUTPUT_CHARS
                    output_saturated = True
                    break
                out_lines.append(rendered)
                output_chars += separator_size + len(rendered)
    page, truncated = _apply_pagination(out_lines, head_limit, offset)
    truncated = truncated or collection_saturated or output_saturated
    if not page:
        return tool_result(success=True, output_mode=output_mode, content="No matches found", num_lines=0)
    payload = {"success": True, "output_mode": output_mode,
               "content": _truncate("\n".join(page), limit=_MAX_SEARCH_OUTPUT_CHARS),
               "num_lines": len(page)}
    if truncated:
        payload["applied_limit"] = head_limit
    if offset:
        payload["applied_offset"] = offset
    return tool_result(**payload)


# ---------------------------------------------------------------------------
# patch 工具（保留，未改动）
# ---------------------------------------------------------------------------

PATCH_SCHEMA = {
    "name": "patch",
    "description": "对文本文件执行替换补丁：把 old 替换为 new。保留原文件行尾符和 UTF-8 BOM；拒绝写入敏感系统路径。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文本文件"},
            "old": {"type": "string", "description": "要被替换的原文本"},
            "new": {"type": "string", "description": "替换后的新文本"},
            "count": {"type": "integer", "description": "最多替换次数，默认 1；0 表示全部"},
        },
        "required": ["path", "old", "new"],
    },
}


async def handle_patch(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    authorized = await authorize_file_tool(
        args,
        operation="patch",
        tool_name="patch",
        workspace_store=workspace_store,
        security_service=security_service,
        bind_identity=True,
    )
    if not isinstance(authorized, AuthorizedFileTarget):
        raise ToolError("文件授权未绑定目标身份")
    path = authorized.path
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    count = int(args.get("count", 1))

    sensitive = _check_sensitive_path(str(args.get("path", "")))
    if sensitive:
        raise ToolError(sensitive)

    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")
    if not old:
        raise ToolError("old 不能为空")

    try:
        version = snapshot_file(
            path,
            expected_identity=authorized.identity,
        )
    except (FileConflictError, OSError, ValueError) as exc:
        raise ToolError(safe_public_error(exc, "补丁目标身份校验失败")) from exc
    text_bytes = version.data
    text = text_bytes.decode("utf-8", errors="replace")
    text, had_bom = _strip_bom(text)
    original_ending = _detect_line_ending(text)

    if old not in text:
        raise ToolError("未找到 old 文本，未修改文件")

    replace_count = text.count(old) if count == 0 else min(text.count(old), count)
    updated = text.replace(old, new, count if count > 0 else -1)

    if original_ending is not None:
        updated = _normalize_line_endings(updated, original_ending)
    if had_bom and not updated.startswith("﻿"):
        updated = "﻿" + updated

    try:
        atomic_replace_bytes(path, updated.encode("utf-8"), version)
    except (FileConflictError, OSError, ValueError) as exc:
        raise ToolError(safe_public_error(exc, "补丁写入失败")) from exc

    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return tool_result(
        success=True,
        path=str(path),
        replacements=replace_count,
        diff=_truncate(diff, 4000),
        bom_preserved=had_bom,
        line_ending=repr(original_ending)[1:-1] if original_ending else "\\n",
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
    )
