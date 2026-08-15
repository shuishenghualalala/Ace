"""Workspace guard for isolated Team delegate tasks."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from crew.tools.terminal_guard import detect_dangerous_command, detect_hardline_command

SEARCH_TOOL_NAMES = {
    "search_files",
    "file_search",
    "mcp_filesystem_search_files",
}

CREW_INTERACTION_MCP_TOOL_NAMES = {
    "mcp_crew_interaction_ask_followup_question",
    "mcp__crew-interaction__ask_followup_question",
    "mcp_crew_interaction_team_mention",
    "mcp__crew-interaction__team_mention",
    "mcp_crew_interaction_team_plan_create",
    "mcp__crew-interaction__team_plan_create",
    "mcp_crew_interaction_team_plan_read",
    "mcp__crew-interaction__team_plan_read",
    "mcp_crew_interaction_team_plan_update",
    "mcp__crew-interaction__team_plan_update",
}

SEARCH_COMMANDS = {"find", "rg", "grep", "ls", "du", "tree"}
READ_PATH_TOOLS = SEARCH_TOOL_NAMES | {"file_read"}
WRITE_PATH_TOOLS = {"file_write", "file_delete", "patch"}
READ_COMMANDS = SEARCH_COMMANDS | {"cat", "head", "tail", "wc", "sed"}
WRITE_COMMANDS = {"touch", "mkdir", "rm", "rmdir", "cp", "mv", "tee"}
TRUSTED_COMMAND_LAUNCHERS = {
    "/bin/bash",
    "/bin/dash",
    "/bin/sh",
    "/bin/zsh",
    "/usr/bin/bash",
    "/usr/bin/env",
    "/usr/bin/sh",
    "/usr/bin/zsh",
}
ACP_TOOL_ALIASES = {
    "apply_patch": "patch",
    "bash": "terminal",
    "execute": "terminal",
    "execute_command": "terminal",
    "edit": "file_write",
    "edit_file": "file_write",
    "delete": "file_delete",
    "delete_file": "file_delete",
    "remove": "file_delete",
    "remove_file": "file_delete",
    "run_command": "terminal",
    "read": "file_read",
    "read_file": "file_read",
    "shell": "terminal",
    "shell_command": "terminal",
    "write": "file_write",
    "write_file": "file_write",
}
# Only match a POSIX absolute path at a token boundary.  The old expression
# also matched the slash inside ``./scripts/foo.py`` as ``/scripts/foo.py`` and
# treated a workspace-local command as a root-level read.
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./:\\-])(?P<path>/[^\s`'\"|;&)]+)")
NETWORK_OR_SIDE_EFFECT_RE = re.compile(
    r"\b(?:curl|wget|ssh|scp|rsync|git\s+(?:push|fetch|pull)|npm\s+(?:install|publish)|"
    r"pnpm\s+(?:install|publish)|yarn\s+(?:install|publish)|pip\s+install|brew\s+install|"
    r"docker\s+(?:push|login)|kubectl\s+(?:apply|delete)|terraform\s+apply)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkspaceGuardDecision:
    allowed: bool
    reason: str = ""


PermissionAction = Literal["allow", "ask", "deny"]
PermissionOperation = Literal["read", "write", "execute", "network", "unknown"]


@dataclass(frozen=True)
class WorkspacePermissionDecision:
    action: PermissionAction
    reason: str
    tool_name: str = ""
    target: str = ""
    operation: PermissionOperation = "unknown"


def normalize_acp_tool_name(value: Any) -> str:
    """Normalize runtime-specific file tool labels into Crew guard names."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("mcp_", "mcp__")):
        return lowered
    key = raw.replace("-", "_").replace(" ", "_").lower()
    return ACP_TOOL_ALIASES.get(key, key)


def check_workspace_guard(
    tool_name: str,
    args: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    *,
    cwd: str = "",
) -> WorkspaceGuardDecision:
    if not isinstance(guard, dict) or not guard.get("enabled"):
        return WorkspaceGuardDecision(True)
    tool = normalize_acp_tool_name(tool_name)
    payload = dict(args or {})
    readable_roots = _roots(
        guard.get("readable_roots") or guard.get("allowed_roots") or [guard.get("root")],
        cwd=cwd,
    )
    readable_files = _files(guard.get("readable_files"), cwd=cwd)
    writable_roots = _roots(guard.get("writable_roots") or [guard.get("root")], cwd=cwd)
    if not readable_roots:
        return WorkspaceGuardDecision(True)
    if not writable_roots:
        writable_roots = readable_roots
    if tool in READ_PATH_TOOLS:
        target = _resolve_path(str(payload.get("path") or "."), cwd=cwd)
        if target is not None and not _is_readable(target, readable_roots, readable_files):
            return _blocked(target, readable_roots, access="read")
    if tool in WRITE_PATH_TOOLS:
        target = _resolve_path(str(payload.get("path") or "."), cwd=cwd)
        if target is not None and not _is_under_any(target, writable_roots):
            return _blocked(target, writable_roots, access="write")
    if tool == "terminal":
        command = str(payload.get("command") or payload.get("raw") or "").strip()
        inspected_command = _unwrap_trusted_shell_command(command)
        for target in _terminal_read_paths(inspected_command, cwd=cwd):
            if not _is_readable(target, readable_roots, readable_files):
                return _blocked(target, readable_roots, access="read")
        for target in _terminal_write_paths(inspected_command, cwd=cwd):
            if not _is_under_any(target, writable_roots):
                return _blocked(target, writable_roots, access="write")
    return WorkspaceGuardDecision(True)


def classify_external_permission(
    tool_call: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    *,
    cwd: str = "",
) -> WorkspacePermissionDecision:
    """Classify a runtime permission request without interpreting business text."""
    call = dict(tool_call or {})
    raw_input = call.get("rawInput") or call.get("raw_input") or {}
    if not isinstance(raw_input, dict):
        raw_input = {}
    nested_args = raw_input.get("arguments")
    args = dict(nested_args) if isinstance(nested_args, dict) else dict(raw_input)
    tool_name = normalize_acp_tool_name(
        raw_input.get("tool")
        or raw_input.get("name")
        or call.get("name")
        or call.get("toolName")
        or call.get("tool_name")
        or call.get("title")
        or ""
    )
    kind = str(call.get("kind") or "").strip().lower()
    title = str(call.get("title") or "").strip()
    if not tool_name and kind == "edit":
        tool_name = "file_write"

    if tool_name in CREW_INTERACTION_MCP_TOOL_NAMES:
        return WorkspacePermissionDecision(
            "allow",
            "Crew Interaction MCP 由服务端 Binding 再校验身份、角色与操作范围。",
            tool_name=tool_name,
        )

    readable_roots, readable_files, writable_roots = _permission_roots(guard, cwd=cwd)
    confirm_write_files = _files(dict(guard or {}).get("confirm_write_files"), cwd=cwd)
    confirm_write_roots = _roots(dict(guard or {}).get("confirm_write_roots"), cwd=cwd)
    if not readable_roots or not writable_roots:
        return WorkspacePermissionDecision(
            "deny",
            "Crew 无法确认当前 Session 的可信工作区边界。",
            tool_name=tool_name,
        )

    if tool_name in READ_PATH_TOOLS:
        target = _resolve_path(str(args.get("path") or "."), cwd=cwd)
        return _classify_read_path(
            target,
            readable_roots,
            readable_files,
            tool_name=tool_name,
        )

    if tool_name in WRITE_PATH_TOOLS or kind == "edit":
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return WorkspacePermissionDecision(
                "ask",
                "Runtime 请求修改文件，但没有提供可验证的目标路径。",
                tool_name=tool_name or "file_write",
                operation="write",
            )
        target = _resolve_path(raw_path, cwd=cwd)
        if target is not None and _requires_write_confirmation(
            target,
            confirm_write_files,
            confirm_write_roots,
        ):
            return WorkspacePermissionDecision(
                "ask",
                "请求修改用户通过 @ 引用的原始文件或目录。",
                tool_name=tool_name or "file_write",
                target=str(target),
                operation="write",
            )
        if tool_name == "file_delete":
            decision = _classify_delete_path(
                target,
                writable_roots,
                tool_name=tool_name,
                confirm_delete_roots=_roots(
                    dict(guard or {}).get("confirm_delete_roots"),
                    cwd=cwd,
                ),
            )
            return decision
        return _classify_write_path(target, writable_roots, tool_name=tool_name or "file_write")

    command = str(args.get("command") or args.get("raw") or "").strip()
    if tool_name == "terminal" or kind == "execute" or command:
        if not command:
            return WorkspacePermissionDecision(
                "deny",
                "Runtime 请求执行命令，但没有提供可检查的命令内容。",
                tool_name=tool_name or "terminal",
                operation="execute",
            )
        inspected_command = _unwrap_trusted_shell_command(command)
        hardline, hardline_reason = detect_hardline_command(inspected_command)
        if hardline:
            return WorkspacePermissionDecision(
                "deny",
                f"命令命中不可放行的系统安全边界：{hardline_reason or 'hardline command'}。",
                tool_name=tool_name or "terminal",
                target=_compact_target(command),
                operation="execute",
            )
        read_paths = _terminal_read_paths(inspected_command, cwd=cwd)
        write_paths = _terminal_write_paths(inspected_command, cwd=cwd)
        for path in write_paths:
            if _requires_write_confirmation(
                path,
                confirm_write_files,
                confirm_write_roots,
            ):
                return WorkspacePermissionDecision(
                    "ask",
                    "命令请求修改用户通过 @ 引用的原始文件或目录。",
                    tool_name=tool_name or "terminal",
                    target=str(path),
                    operation="write",
                )
            decision = _classify_write_path(path, writable_roots, tool_name=tool_name or "terminal")
            if decision.action != "allow":
                return decision
        for path in read_paths:
            decision = _classify_read_path(
                path,
                readable_roots,
                readable_files,
                tool_name=tool_name or "terminal",
            )
            if decision.action != "allow":
                return decision
        if _is_delete_command(inspected_command):
            return _classify_delete_command(
                inspected_command,
                write_paths,
                writable_roots,
                confirm_delete_roots=_roots(
                    dict(guard or {}).get("confirm_delete_roots"),
                    cwd=cwd,
                ),
                tool_name=tool_name or "terminal",
            )
        dangerous, dangerous_reason = detect_dangerous_command(inspected_command)
        if dangerous:
            return WorkspacePermissionDecision(
                "ask",
                f"命令可能产生不可逆副作用：{dangerous_reason or 'dangerous command'}。",
                tool_name=tool_name or "terminal",
                target=_compact_target(command),
                operation="execute",
            )
        if NETWORK_OR_SIDE_EFFECT_RE.search(inspected_command):
            return WorkspacePermissionDecision(
                "ask",
                "命令包含网络访问、依赖安装、发布或外部系统副作用。",
                tool_name=tool_name or "terminal",
                target=_compact_target(command),
                operation="network",
            )
        return WorkspacePermissionDecision(
            "allow",
            "命令只作用于当前可信工作区。",
            tool_name=tool_name or "terminal",
            target=_compact_target(command),
            operation="execute",
        )

    if tool_name or title:
        return WorkspacePermissionDecision(
            "ask",
            "Runtime 请求执行 Crew 尚未分类的真实工具操作。",
            tool_name=tool_name or kind or "external_tool",
            target=_compact_target(title),
        )
    return WorkspacePermissionDecision(
        "deny",
        "权限请求缺少真实工具、路径或命令信息，已安全拒绝。",
    )


def classify_acp_permission(
    tool_call: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    *,
    cwd: str = "",
) -> WorkspacePermissionDecision:
    """Backward-compatible alias for the protocol-neutral permission classifier."""
    return classify_external_permission(tool_call, guard, cwd=cwd)


def _roots(values: Any, *, cwd: str = "") -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, list) else [values]
    for raw in raw_values:
        value = str(raw or "").strip()
        if not value:
            continue
        path = _resolve_path(value, cwd=cwd)
        if path is None:
            continue
        if path.is_file():
            path = path.parent
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _files(values: Any, *, cwd: str = "") -> list[Path]:
    """Resolve exact readable files without widening them to their parent directories."""
    files: list[Path] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, list) else [values]
    for raw in raw_values:
        path = _resolve_path(str(raw or ""), cwd=cwd)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
    return files


def _permission_roots(
    guard: dict[str, Any] | None,
    *,
    cwd: str,
) -> tuple[list[Path], list[Path], list[Path]]:
    config = dict(guard or {})
    if not config.get("enabled"):
        config = {
            "enabled": True,
            "root": cwd,
            "readable_roots": [cwd],
            "writable_roots": [cwd],
        }
    readable = _roots(
        config.get("readable_roots") or config.get("allowed_roots") or [config.get("root")],
        cwd=cwd,
    )
    readable_files = _files(config.get("readable_files"), cwd=cwd)
    writable = _roots(config.get("writable_roots") or [config.get("root")], cwd=cwd)
    return readable, readable_files, writable


def _classify_read_path(
    path: Path | None,
    readable_roots: list[Path],
    readable_files: list[Path],
    *,
    tool_name: str,
) -> WorkspacePermissionDecision:
    if path is not None and _is_readable(path, readable_roots, readable_files):
        return WorkspacePermissionDecision(
            "allow",
            "目标位于当前 Session 的可信工作区或本轮只读附件中。",
            tool_name=tool_name,
            target=str(path),
            operation="read",
        )
    return _classify_path(path, readable_roots, tool_name=tool_name, access="read")


def _classify_path(
    path: Path | None,
    allowed_roots: list[Path],
    *,
    tool_name: str,
    access: str,
) -> WorkspacePermissionDecision:
    if path is None:
        return WorkspacePermissionDecision(
            "deny",
            "目标路径无法安全解析。",
            tool_name=tool_name,
            operation="read" if access == "read" else "write",
        )
    if _is_under_any(path, allowed_roots):
        return WorkspacePermissionDecision(
            "allow",
            "目标位于当前 Session 的可信工作区。",
            tool_name=tool_name,
            target=str(path),
            operation="read" if access == "read" else "write",
        )
    verb = "读取" if access == "read" else "写入"
    return WorkspacePermissionDecision(
        "ask",
        f"请求{verb}当前 Session 工作区外的路径。",
        tool_name=tool_name,
        target=str(path),
        operation="read" if access == "read" else "write",
    )


def _classify_write_path(
    path: Path | None,
    writable_roots: list[Path],
    *,
    tool_name: str,
) -> WorkspacePermissionDecision:
    decision = _classify_path(path, writable_roots, tool_name=tool_name, access="write")
    if decision.action != "allow" or path is None:
        return decision
    if _is_protected_workspace_path(path, writable_roots):
        return WorkspacePermissionDecision(
            "deny",
            "不允许覆盖或删除工作区根目录、.crew 或 .git 控制数据。",
            tool_name=tool_name,
            target=str(path),
            operation="write",
        )
    return WorkspacePermissionDecision(
        "allow",
        "普通写入位于当前 Session 的可信工作区。",
        tool_name=tool_name,
        target=str(path),
        operation="write",
    )


def _classify_delete_path(
    path: Path | None,
    writable_roots: list[Path],
    *,
    tool_name: str,
    confirm_delete_roots: list[Path],
) -> WorkspacePermissionDecision:
    decision = _classify_write_path(path, writable_roots, tool_name=tool_name)
    if decision.action != "allow" or path is None:
        return decision
    if path.is_dir() or _is_under_any(path, confirm_delete_roots):
        return WorkspacePermissionDecision(
            "ask",
            "请求删除目录、批量数据或用户绑定项目中的原有文件。",
            tool_name=tool_name,
            target=str(path),
            operation="write",
        )
    return WorkspacePermissionDecision(
        "allow",
        "删除目标是 Session 工作目录内的单个普通文件。",
        tool_name=tool_name,
        target=str(path),
        operation="write",
    )


def _classify_delete_command(
    command: str,
    paths: list[Path],
    writable_roots: list[Path],
    *,
    confirm_delete_roots: list[Path],
    tool_name: str,
) -> WorkspacePermissionDecision:
    if not paths:
        return WorkspacePermissionDecision(
            "deny",
            "删除命令缺少可验证的目标路径。",
            tool_name=tool_name,
            target=_compact_target(command),
            operation="write",
        )
    for path in paths:
        decision = _classify_write_path(path, writable_roots, tool_name=tool_name)
        if decision.action != "allow":
            return decision
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    recursive = any(
        token == "--recursive"
        or (
            token.startswith("-")
            and not token.startswith("--")
            and "r" in token[1:].lower()
        )
        for token in tokens
    )
    directory_target = any(path.is_dir() for path in paths)
    project_target = any(_is_under_any(path, confirm_delete_roots) for path in paths)
    if recursive or directory_target or len(paths) > 1 or project_target:
        return WorkspacePermissionDecision(
            "ask",
            "请求删除目录、批量数据或用户绑定项目中的原有文件。",
            tool_name=tool_name,
            target=_compact_target(command),
            operation="write",
        )
    return WorkspacePermissionDecision(
        "allow",
        "删除目标是 Session 工作目录内的单个普通文件。",
        tool_name=tool_name,
        target=str(paths[0]),
        operation="write",
    )


def _is_protected_workspace_path(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] in {".crew", ".git"}:
            return True
    return False


def _is_delete_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(Path(token).name in {"rm", "rmdir"} for token in tokens)


def _compact_target(value: str, *, max_chars: int = 280) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else f"{text[:max_chars].rstrip()}..."


def _resolve_path(raw: str, *, cwd: str = "") -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            base = Path(cwd).expanduser() if cwd else Path.cwd()
            path = base / path
        return path.resolve()
    except Exception:  # noqa: BLE001
        return None


def _is_under_any(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_readable(path: Path, roots: list[Path], files: list[Path]) -> bool:
    return path in files or _is_under_any(path, roots)


def _requires_write_confirmation(
    path: Path,
    files: list[Path],
    roots: list[Path],
) -> bool:
    return path in files or _is_under_any(path, roots)


def _is_search_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(Path(token).name in SEARCH_COMMANDS for token in tokens)


def _terminal_read_paths(command: str, *, cwd: str = "") -> list[Path]:
    paths: list[Path] = []
    for match in ABS_PATH_RE.finditer(command):
        path = _resolve_path(match.group("path"), cwd=cwd)
        if path is not None and str(path) not in TRUSTED_COMMAND_LAUNCHERS:
            paths.append(path)
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    tokens = [token.strip("\"'") for token in tokens]
    for index, token in enumerate(tokens):
        name = Path(token).name
        if name not in READ_COMMANDS:
            continue
        for candidate in _read_command_positional_paths(name, tokens[index + 1 :], cwd=cwd):
            paths.append(candidate)
    return _dedupe_paths(paths)


def _unwrap_trusted_shell_command(command: str) -> str:
    """Inspect the payload of Runtime-generated ``/bin/*sh -c/-lc`` wrappers."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) < 3 or tokens[0] not in TRUSTED_COMMAND_LAUNCHERS:
        return command
    for index, token in enumerate(tokens[1:-1], start=1):
        if token in {"-c", "-lc"}:
            return tokens[index + 1]
    return command


def _terminal_write_paths(command: str, *, cwd: str = "") -> list[Path]:
    paths: list[Path] = []
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    tokens = [token.strip("\"'") for token in tokens]
    for index, token in enumerate(tokens):
        if token in {">", ">>"} and index + 1 < len(tokens):
            path = _write_token_path(tokens[index + 1], cwd=cwd)
            if path is not None:
                paths.append(path)
            continue
        if token.startswith(">") and token not in {">", ">>"}:
            path = _write_token_path(token.lstrip(">"), cwd=cwd)
            if path is not None:
                paths.append(path)
            continue
        name = Path(token).name
        if name not in WRITE_COMMANDS:
            continue
        for candidate in _write_command_positional_paths(name, tokens[index + 1 :], cwd=cwd):
            paths.append(candidate)
    return _dedupe_paths(paths)


def _read_command_positional_paths(command: str, tokens: list[str], *, cwd: str = "") -> list[Path]:
    if command == "find":
        return _leading_paths(tokens, cwd=cwd)
    if command in {"ls", "du", "tree"}:
        return [_path for _path in (_token_path(token, cwd=cwd) for token in tokens) if _path is not None]
    if command == "grep":
        if "-R" not in tokens and "-r" not in tokens and not any(token.startswith("-R") or token.startswith("-r") for token in tokens):
            return []
        return [_path for _path in (_token_path(token, cwd=cwd) for token in tokens[1:]) if _path is not None]
    if command == "rg":
        return [_path for _path in (_token_path(token, cwd=cwd) for token in tokens[1:]) if _path is not None]
    if command in {"cat", "head", "tail", "wc", "sed"}:
        return [_path for _path in (_token_path(token, cwd=cwd) for token in tokens) if _path is not None]
    return []


def _write_command_positional_paths(command: str, tokens: list[str], *, cwd: str = "") -> list[Path]:
    positional = [token for token in tokens if token and not token.startswith("-")]
    if command in {"touch", "mkdir", "rm", "rmdir"}:
        return [_path for _path in (_write_token_path(token, cwd=cwd) for token in positional) if _path is not None]
    if command in {"cp", "mv"} and positional:
        tail = positional[-1]
        path = _write_token_path(tail, cwd=cwd)
        return [path] if path is not None else []
    if command == "tee":
        return [_path for _path in (_write_token_path(token, cwd=cwd) for token in positional) if _path is not None]
    return []


def _leading_paths(tokens: list[str], *, cwd: str = "") -> list[Path]:
    paths: list[Path] = []
    for token in tokens:
        if token.startswith("-") or token in {"(", ")", "!", "-o", "-a"}:
            break
        path = _token_path(token, cwd=cwd)
        if path is None:
            break
        paths.append(path)
    return paths


def _token_path(token: str, *, cwd: str = "") -> Path | None:
    value = str(token or "").strip()
    if not value or value.startswith("-"):
        return None
    if (
        value in {".", ".."}
        or value.startswith(("./", "../", "/", "\\\\"))
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        return _resolve_path(value, cwd=cwd)
    return None


def _write_token_path(token: str, *, cwd: str = "") -> Path | None:
    value = str(token or "").strip()
    if not value or value.startswith("-"):
        return None
    return _resolve_path(value, cwd=cwd)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _blocked(path: Path, allowed_roots: list[Path], *, access: str) -> WorkspaceGuardDecision:
    del path, allowed_roots
    verb = "写入" if access == "write" else "读取或搜索"
    return WorkspaceGuardDecision(
        False,
        (
            f"外部智能体请求{verb}当前 Session 授权范围外的路径。"
            "请使用当前工作目录内的相对路径，或使用用户已明确提供的附件、引用、Skill 和团队上游产物。"
        ),
    )
