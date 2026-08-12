"""Crew tool dispatch helpers for safe batch execution.

Design constraints:
- Tool calls are ``crew.core.types.ToolCall`` objects (``name``/``arguments``)
  instead of OpenAI SDK objects (``function.name``/JSON arguments).
- Built-in file tool names are ``file_read`` / ``file_write`` / ``file_delete``.
- MCP parallel safety stays conservative unless a tool name is already in the
  read-only allowlist.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset()

_PARALLEL_SAFE_TOOLS = frozenset(
    {
        "file_read",
        "read_file",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
        "search_files",
        "session_search",
        "skill_view",
        "skills_list",
        "vision_analyze",
        "web_extract",
        "web_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

_PATH_SCOPED_TOOLS = frozenset({"file_read", "file_write", "file_delete", "read_file", "write_file", "patch"})

_DESTRUCTIVE_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        cp\s|install\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.VERBOSE,
)
_REDIRECT_OVERWRITE = re.compile(r"[^>]>[^>]|^>[^>]")
_SHELL_CONTROL_CHARS = re.compile(r"[;&|<>`]")
_READONLY_TERMINAL_COMMANDS = frozenset(
    {
        "cat",
        "date",
        "df",
        "du",
        "file",
        "find",
        "git",
        "grep",
        "head",
        "id",
        "ls",
        "pwd",
        "rg",
        "sed",
        "stat",
        "tail",
        "tree",
        "wc",
        "which",
        "whoami",
    }
)
_READONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "branch",
        "diff",
        "grep",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "status",
    }
)
_READONLY_COMMAND_SUBCOMMANDS = frozenset({"-v", "-V"})
_UNSAFE_FIND_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"})


@dataclass(frozen=True)
class ToolBatchDecision:
    parallel: bool
    reason: str


def _tool_name(tool_call: Any) -> str:
    return str(getattr(tool_call, "name", "") or "")


def _tool_args(tool_call: Any) -> dict[str, Any] | None:
    args = getattr(tool_call, "arguments", None)
    return args if isinstance(args, dict) else None


def _is_destructive_command(cmd: str) -> bool:
    """Crew heuristic: terminal command may modify/delete files."""
    if not cmd:
        return False
    return bool(_DESTRUCTIVE_PATTERNS.search(cmd) or _REDIRECT_OVERWRITE.search(cmd))


def _is_readonly_terminal_command(command: str) -> tuple[bool, str]:
    """Return whether a terminal command is simple enough to parallelize.

    This intentionally accepts only one plain command with no shell control
    syntax. Complex shell expressions stay serial because side effects and
    ordering are hard to reason about from a string.
    """
    command = command.strip()
    if not command:
        return False, "terminal command is empty"
    if "\n" in command:
        return False, "terminal command contains multiple lines"
    if _SHELL_CONTROL_CHARS.search(command) or "$(" in command:
        return False, "terminal command uses shell control syntax"
    if _is_destructive_command(command):
        return False, "terminal command may mutate files or state"

    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "terminal command cannot be parsed safely"
    if not parts:
        return False, "terminal command is empty"

    cmd = Path(parts[0]).name
    if cmd == "command":
        if len(parts) >= 3 and parts[1] in _READONLY_COMMAND_SUBCOMMANDS:
            return True, "terminal command lookup is read-only"
        return False, "terminal command wrapper is not a read-only lookup"
    if cmd not in _READONLY_TERMINAL_COMMANDS:
        return False, f"terminal command {cmd} is not in read-only allowlist"
    if cmd == "git":
        if len(parts) < 2 or parts[1] not in _READONLY_GIT_SUBCOMMANDS:
            return False, "git subcommand is not read-only"
        if parts[1] == "branch" and any(p in {"-D", "-d", "--delete", "-m", "-M"} for p in parts[2:]):
            return False, "git branch arguments may mutate refs"
    if cmd == "sed" and any(p == "-i" or p.startswith("-i.") for p in parts[1:]):
        return False, "sed -i mutates files"
    if cmd == "find" and any(p in _UNSAFE_FIND_FLAGS for p in parts[1:]):
        return False, "find arguments may mutate files or write outputs"
    return True, "terminal command is read-only"


def _terminal_parallel_safety(function_args: dict[str, Any]) -> tuple[bool, str]:
    if bool(function_args.get("background")):
        return False, "terminal background process has shared lifecycle state"
    if bool(function_args.get("force")):
        return False, "terminal force=true may bypass safety checks"
    command = function_args.get("command")
    if not isinstance(command, str):
        return False, "terminal command is not a string"
    return _is_readonly_terminal_command(command)


def _is_mcp_tool_parallel_safe(tool_name: str) -> bool:
    """Conservative MCP parallel-safe opt-in for Crew.

    Crew's MCP registry does not yet expose a per-server parallel-safe bit.
    Treat only names already present in the explicit read-only allowlist as
    parallel safe.
    """
    return tool_name in _PARALLEL_SAFE_TOOLS


def _extract_parallel_scope_path(tool_name: str, function_args: dict[str, Any]) -> Path | None:
    """Return normalized target path for path-scoped Crew/Crew file tools."""
    if tool_name not in _PATH_SCOPED_TOOLS:
        return None
    raw_path = function_args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    expanded = Path(raw_path).expanduser()
    if expanded.is_absolute():
        return Path(os.path.abspath(str(expanded)))
    return Path(os.path.abspath(str(Path.cwd() / expanded)))


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return True when two paths may refer to the same subtree."""
    left_parts = left.parts
    right_parts = right.parts
    if not left_parts or not right_parts:
        return bool(left_parts) == bool(right_parts) and bool(left_parts)
    common_len = min(len(left_parts), len(right_parts))
    return left_parts[:common_len] == right_parts[:common_len]


def should_parallelize_tool_batch(tool_calls: list[Any]) -> ToolBatchDecision:
    """Return Crew parallelization decision for a Crew tool batch."""
    if len(tool_calls) <= 1:
        return ToolBatchDecision(False, "single tool call")

    delegate_calls = [tc for tc in tool_calls if _tool_name(tc) == "delegate_to_teammate"]
    if delegate_calls:
        if len(delegate_calls) != len(tool_calls):
            return ToolBatchDecision(False, "delegate_to_teammate mixed with other tools")
        members: set[str] = set()
        for tool_call in delegate_calls:
            function_args = _tool_args(tool_call)
            if function_args is None:
                return ToolBatchDecision(False, "delegate_to_teammate arguments are not a dict")
            member = str(function_args.get("member") or "").strip()
            if not member:
                return ToolBatchDecision(False, "delegate_to_teammate missing member")
            if member in members:
                return ToolBatchDecision(False, "delegate_to_teammate targets the same member")
            members.add(member)
        return ToolBatchDecision(True, "delegate_to_teammate calls target distinct members")

    reserved_paths: list[Path] = []
    for tool_call in tool_calls:
        tool_name = _tool_name(tool_call)
        if tool_name in _NEVER_PARALLEL_TOOLS:
            return ToolBatchDecision(False, f"{tool_name} is never parallel")
        function_args = _tool_args(tool_call)
        if function_args is None:
            return ToolBatchDecision(False, f"{tool_name} arguments are not a dict")

        if tool_name == "terminal":
            safe, reason = _terminal_parallel_safety(function_args)
            if not safe:
                return ToolBatchDecision(False, reason)
            continue

        if tool_name in _PATH_SCOPED_TOOLS:
            scoped_path = _extract_parallel_scope_path(tool_name, function_args)
            if scoped_path is None:
                return ToolBatchDecision(False, f"{tool_name} has no path scope")
            if any(_paths_overlap(scoped_path, existing) for existing in reserved_paths):
                return ToolBatchDecision(False, f"{tool_name} path overlaps another tool")
            reserved_paths.append(scoped_path)
            if tool_name in {"file_write", "file_delete", "write_file", "patch"}:
                return ToolBatchDecision(False, f"{tool_name} mutates files")
            continue

        if tool_name not in _PARALLEL_SAFE_TOOLS and not _is_mcp_tool_parallel_safe(tool_name):
            return ToolBatchDecision(False, f"{tool_name} is not parallel safe")

    return ToolBatchDecision(True, "all tool calls are independent read-only calls")


def should_parallelize(tool_calls: list[Any]) -> bool:
    return should_parallelize_tool_batch(tool_calls).parallel


def is_tool_parallel_safe(tool_call: Any) -> bool:
    """单工具并发安全判定（per-tool，对应 Crew 的 isConcurrencySafe）。

    只读、无共享副作用的工具才安全：可在流式期间提前派发、可与其它 safe 工具并发。
    写文件 / terminal / 未知工具一律视为不安全（独占执行）。
    与整批判定 ``should_parallelize_tool_batch`` 复用同一组白名单，避免分类漂移。
    """
    name = _tool_name(tool_call)
    if name == "terminal":
        return False
    if name in _NEVER_PARALLEL_TOOLS:
        return False
    if name in {"file_write", "file_delete", "write_file", "patch"}:
        return False
    args = _tool_args(tool_call)
    if args is None:
        return False
    return name in _PARALLEL_SAFE_TOOLS or _is_mcp_tool_parallel_safe(name)


def segment_consecutive_safe(tool_calls: list[Any]) -> list[tuple[bool, list[Any]]]:
    """把工具序列切成「连续 safe 并发段」与「unsafe 独占段」，保持原始顺序。

    例：``[read, read, write, read]`` → ``[(True,[read,read]), (False,[write]), (True,[read])]``
    safe 段内可并发执行；unsafe 段逐个独占执行。顺序安全由分段保证：
    任何写/命令都自成一段，不会与读乱序。对应 Crew OpenAI 后端的 batching 策略。
    """
    segments: list[tuple[bool, list[Any]]] = []
    for tc in tool_calls:
        safe = is_tool_parallel_safe(tc)
        if segments and segments[-1][0] and safe:
            segments[-1][1].append(tc)
        else:
            segments.append((safe, [tc]))
    return segments


def deduplicate_tool_calls(tool_calls: list[Any]) -> list[Any]:
    """Remove duplicate (tool_name, arguments) pairs in one turn."""
    seen: set[tuple[str, str]] = set()
    unique: list[Any] = []
    for tc in tool_calls:
        key = (_tool_name(tc), repr(_tool_args(tc)))
        if key in seen:
            log.warning("Removed duplicate tool call: %s", _tool_name(tc))
            continue
        seen.add(key)
        unique.append(tc)
    return unique if len(unique) < len(tool_calls) else tool_calls


def cap_delegate_tool_calls(tool_calls: list[Any], max_children: int) -> list[Any]:
    """Cap delegate tool calls while preserving non-delegate calls."""
    max_children = max(1, int(max_children or 1))
    delegate_names = {"delegate_task", "delegate_to_teammate"}
    delegate_count = sum(1 for tc in tool_calls if _tool_name(tc) in delegate_names)
    if delegate_count <= max_children:
        return tool_calls

    kept_delegates = 0
    capped: list[Any] = []
    for tc in tool_calls:
        if _tool_name(tc) in delegate_names:
            if kept_delegates < max_children:
                capped.append(tc)
                kept_delegates += 1
            continue
        capped.append(tc)
    log.warning(
        "Truncated %d excess delegate tool call(s) to enforce max_concurrent_children=%d",
        delegate_count - max_children,
        max_children,
    )
    return capped


def plan_tool_calls(tool_calls: list[Any], *, max_delegate_calls: int) -> list[Any]:
    """Apply Crew pre-execution tool call planning."""
    return deduplicate_tool_calls(cap_delegate_tool_calls(tool_calls, max_delegate_calls))
