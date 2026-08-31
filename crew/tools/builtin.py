"""内置工具：terminal / file_read / file_write / file_delete。

本文件使用 Hermes 风格工具格式：
  SCHEMA + handler(args) + registry.register(name, toolset, schema, handler)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

from crew.core.errors import ToolError
from crew.core.runctx import emit_tool_progress
from crew.tools.file_utils import (
    _apply_line_pagination,
    _check_sensitive_path,
    _detect_line_ending,
    _format_read_result,
    _get_max_read_chars,
    _has_binary_extension,
    _is_blocked_device,
    _normalize_line_endings,
    _normalize_read_pagination,
    _resolve_base_dir,
    _strip_bom,
    atomic_replace_bytes,
    read_verified_bytes,
    snapshot_file,
    MAX_READ_FILE_BYTES,
)
from crew.tools.output_filters import strip_ansi, truncate_output
from crew.tools.redact import redact_sensitive_text
from crew.tools.registry import Registry
from crew.tools.security_guard import authorize_file_tool
from crew.tools.terminal_guard import classify_command, detect_dangerous_command, detect_hardline_command

FILE_READ_SCHEMA = {
    "name": "file_read",
    "description": "读取一个文本文件的内容。支持 offset/limit 分页、保留原始行尾符、自动处理 UTF-8 BOM。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对或绝对）"},
            "offset": {"type": "integer", "description": "起始行号（从1开始），可选"},
            "limit": {"type": "integer", "description": "最多返回行数，可选"},
        },
        "required": ["path"],
    },
}

FILE_WRITE_SCHEMA = {
    "name": "file_write",
    "description": "把内容写入文件（覆盖）。会自动创建父目录；保留原文件行尾符和 UTF-8 BOM；写入敏感系统路径会被拒绝。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
            "append": {"type": "boolean", "description": "是否追加写入，默认 false"},
        },
        "required": ["path", "content"],
    },
}

FILE_DELETE_SCHEMA = {
    "name": "file_delete",
    "description": "删除一个明确的文件。只接受文件，不递归删除目录；工作区外路径会按安全模式请求精确授权。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要删除的文件路径（宿主绝对路径或工作区相对路径）"},
        },
        "required": ["path"],
    },
}




def _terminal_max_timeout() -> float:
    """从配置读取最大前台超时，默认 600s。"""
    try:
        from crew.state.config import load_config
        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("terminal", {}).get("max_timeout")
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    except Exception:
        pass
    return 600.0


def _check_terminal_command(command: str) -> tuple[bool, str | None, str | None]:
    """Apply immutable best-effort hardlines; managed commands are always authorized later."""
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        return (
            False,
            f"BLOCKED (hardline): {hardline_desc}. 该命令无条件禁止通过 agent 执行。",
            "policy_denied",
        )
    is_dangerous, dangerous_desc = detect_dangerous_command(command)
    if is_dangerous:
        return (
            False,
            f"DANGEROUS: {dangerous_desc}. 需要宿主运行时向用户申请批准。",
            "approval_required",
        )
    verdict, reason = classify_command(command)
    if verdict == "ask":
        return (
            False,
            f"SECURITY CHECK: {reason}. 需要宿主运行时向用户申请批准。",
            "approval_required",
        )
    return True, None, None


TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "执行 shell 命令。受管模式下 HOME 指向宿主用户目录，但沙箱仍单独限制读写；工作区外写入应使用宿主绝对路径申请精确权限，删除单个文件优先使用 file_delete。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "超时秒数，默认 30，最大 600"},
            "background": {"type": "boolean", "description": "是否后台执行（立即返回 session_id，用 process 工具查询）"},
            "watch_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅后台模式：输出命中其中任一子串时排队通知（限流，连续刷屏会自动降级为完成通知）",
            },
            "notify_on_complete": {
                "type": "boolean",
                "description": "仅后台模式：进程退出时排队一条完成通知（含退出码和输出尾部）",
            },
            "additional_permissions": {
                "type": "object",
                "description": "命令确实需要越过基础沙箱时申请的精确权限；非空时必须由用户批准。",
                "properties": {
                    "filesystem": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "root": {"type": "string", "description": "已存在的宿主绝对文件或目录；相对路径以当前工作目录解析"},
                                "access": {"type": "string", "enum": ["read", "read_write"]},
                            },
                            "required": ["root", "access"],
                        },
                    },
                    "network": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},
                                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                                "protocol": {
                                    "type": "string",
                                    "enum": ["http", "https", "tcp", "udp", "socks5_tcp", "socks5_udp"],
                                },
                                "allow_private": {"type": "boolean"},
                            },
                            "required": ["host", "port", "protocol"],
                        },
                    },
                    "allow_local_binding": {"type": "boolean"},
                },
            },
            "permission_reason": {
                "type": "string",
                "description": "向用户说明为何需要额外权限，不参与授权匹配；保留用于兼容旧调用。",
            },
            "justification": {
                "type": "string",
                "description": "向用户说明为何需要额外权限；与 permission_reason 等价，优先使用此字段。",
            },
            "sandbox_permissions": {
                "type": "string",
                "enum": ["use_default", "with_additional_permissions", "require_escalated"],
                "description": "use_default 使用当前沙箱；with_additional_permissions 在沙箱内扩权；require_escalated 经批准后仅本命令使用宿主用户权限，包括宿主用户可访问的 Ace 文件。",
            },
            "prefix_rule": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "description": "仅 require_escalated 可用的始终允许前缀建议；必须是当前唯一静态命令的安全前缀。",
            },
        },
        "required": ["command"],
    },
}


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_destructive_terminal_targets(
    parsed_commands: tuple[tuple[str, ...], ...],
    *,
    shell_kind: str,
    raw_command: str = "",
    workspace_root: Path,
    requested_permissions: Any,
) -> None:
    """Require literal absolute targets for statically parsed delete commands.

    This binds an external write grant to the path the command will actually
    delete. Dynamic shell expressions remain outside this helper and are still
    handled by the native classifier and ordinary approval policy.
    """
    from crew.security.models import FilesystemAccess

    if not parsed_commands and re.search(
        r"(?i)(?:^|[;&|]\s*)(?:rm|rmdir|unlink|remove-item|del|erase|ri)(?:\.exe)?(?:\s|$)",
        raw_command,
    ):
        raise ToolError(
            "无法把删除命令静态绑定到实际路径；请改用 file_delete，或使用不含 ~、环境变量、"
            "通配符和命令替换的宿主绝对路径"
        )

    for command in parsed_commands:
        if not command:
            continue
        executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable in {"sudo", "env", "command", "nohup"}:
            delete_index = next(
                (
                    index
                    for index, token in enumerate(command[1:], start=1)
                    if token.replace("\\", "/").rsplit("/", 1)[-1].lower()
                    in {"rm", "rmdir", "unlink", "remove-item", "del", "erase", "ri"}
                ),
                None,
            )
            if delete_index is not None:
                command = command[delete_index:]
                executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        operands: list[str] = []
        if shell_kind == "powershell" and executable in {
            "remove-item",
            "del",
            "erase",
            "ri",
            "rm",
            "rmdir",
        }:
            skip_next = False
            for token in command[1:]:
                if skip_next:
                    operands.append(token)
                    skip_next = False
                elif token.lower() in {"-path", "-literalpath"}:
                    skip_next = True
                elif not token.startswith("-"):
                    operands.append(token)
        elif shell_kind != "powershell" and executable in {"rm", "rmdir", "unlink"}:
            after_options = False
            for token in command[1:]:
                if token == "--":
                    after_options = True
                elif after_options or not token.startswith("-"):
                    operands.append(token)
        else:
            continue

        if not operands:
            raise ToolError("删除命令必须包含明确的目标路径；删除单个文件请优先使用 file_delete")
        for raw_target in operands:
            absolute = (
                PureWindowsPath(raw_target).is_absolute()
                if shell_kind == "powershell"
                else Path(raw_target).is_absolute()
            )
            if not absolute:
                raise ToolError(
                    "删除命令必须使用宿主绝对路径，不能用相对路径、~ 或环境变量；"
                    "删除单个文件请使用 file_delete"
                )
            target = Path(raw_target).resolve(strict=False)
            if _path_is_within(target, workspace_root):
                continue
            covered = any(
                entry.access is FilesystemAccess.READ_WRITE
                and _path_is_within(target, entry.root)
                for entry in requested_permissions.filesystem
            )
            if (
                requested_permissions.sandbox_permissions.value != "require_escalated"
                and not covered
            ):
                raise ToolError(
                    f"删除工作区外文件必须为实际目标申请 read_write 权限：{target}；"
                    "无需脱离沙箱"
                )


def _parse_additional_permissions(
    raw: object,
    *,
    cwd: Path,
    security_context: Any,
    mode: Any,
    db_path: Path,
    sandbox_permissions: object = None,
):
    """Validate model-requested authority before it can appear in an approval."""
    from crew.security.file_policy import (
        _is_filesystem_root,
        _protected_entries,
        approvable_file_permission_root,
    )
    from crew.security.models import (
        EMPTY_ADDITIONAL_PERMISSIONS,
        AdditionalPermissionProfile,
        FilesystemAccess,
        FilesystemEntry,
        FilesystemOperation,
        NetworkEntry,
        SandboxPermissions,
    )
    from crew.security.policy import filesystem_operation_allowed, settings_for_mode

    try:
        sandbox_override = SandboxPermissions(
            str(sandbox_permissions or SandboxPermissions.USE_DEFAULT.value)
        )
    except ValueError as exc:
        raise ToolError("sandbox_permissions 值无效") from exc
    if raw is None:
        return AdditionalPermissionProfile(sandbox_permissions=sandbox_override)
    if not isinstance(raw, dict):
        raise ToolError("additional_permissions 必须是对象")
    unknown = set(raw) - {"filesystem", "network", "allow_local_binding"}
    if unknown:
        raise ToolError(
            f"additional_permissions 包含未知字段: {', '.join(sorted(map(str, unknown)))}"
        )
    filesystem_raw = raw.get("filesystem", [])
    network_raw = raw.get("network", [])
    if not isinstance(filesystem_raw, list) or not isinstance(network_raw, list):
        raise ToolError("filesystem 和 network 必须是数组")
    if len(filesystem_raw) > 32 or len(network_raw) > 32:
        raise ToolError("单次最多申请 32 条文件权限和 32 条网络权限")

    filesystem_entries = []
    for item in filesystem_raw:
        if not isinstance(item, dict) or set(item) - {"root", "access"}:
            raise ToolError("文件权限条目只能包含 root 和 access")
        root_value = item.get("root")
        if (
            not isinstance(root_value, str)
            or not root_value.strip()
            or "\x00" in root_value
            or len(root_value) > 4096
        ):
            raise ToolError("文件权限 root 必须是长度不超过 4096 的非空路径")
        try:
            root = Path(root_value).expanduser()
            if not root.is_absolute():
                root = cwd / root
            root = root.resolve(strict=False)
            exists = root.exists()
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"无法解析额外文件权限路径: {exc}") from exc
        if not exists:
            raise ToolError("额外文件权限只能授予已存在路径；创建文件时请申请已存在的父目录")
        try:
            access = FilesystemAccess(str(item.get("access", "")))
        except ValueError as exc:
            raise ToolError("文件权限 access 仅支持 read 或 read_write") from exc
        if access not in {FilesystemAccess.READ, FilesystemAccess.READ_WRITE}:
            raise ToolError("文件权限不能申请 deny")
        if access is FilesystemAccess.READ_WRITE and _is_filesystem_root(root):
            raise ToolError("不能申请文件系统根目录的写权限")
        permission_root = (
            approvable_file_permission_root(
                security_context,
                root,
                db_path=db_path,
            )
            if access is FilesystemAccess.READ_WRITE
            else root
        )
        filesystem_entries.append(FilesystemEntry(permission_root, access))
    if sum(len(str(entry.root)) for entry in filesystem_entries) > 32_768:
        raise ToolError("额外文件权限路径总长度不能超过 32768")

    network_entries = []
    for item in network_raw:
        if not isinstance(item, dict) or set(item) - {"host", "port", "protocol", "allow_private"}:
            raise ToolError("网络权限条目字段无效")
        allow_private = item.get("allow_private", False)
        if not isinstance(allow_private, bool):
            raise ToolError("allow_private 必须是布尔值")
        host = item.get("host", "")
        if not isinstance(host, str) or not 1 <= len(host) <= 253:
            raise ToolError("网络权限 host 必须是长度不超过 253 的非空字符串")
        try:
            network_entries.append(
                NetworkEntry(
                    host=host,
                    port=item.get("port"),
                    protocol=item.get("protocol", ""),
                    allow_private=allow_private,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ToolError(f"网络权限无效: {exc}") from exc
    allow_local_binding = raw.get("allow_local_binding", False)
    if not isinstance(allow_local_binding, bool):
        raise ToolError("allow_local_binding 必须是布尔值")

    profile = AdditionalPermissionProfile(
        filesystem=tuple(dict.fromkeys(filesystem_entries)),
        network=tuple(dict.fromkeys(network_entries)),
        allow_local_binding=allow_local_binding,
        sandbox_permissions=sandbox_override,
    )
    base = settings_for_mode(
        mode,
        security_context.workspace_root,
        deny_entries=_protected_entries(security_context, db_path),
    ).profile
    effective_filesystem = []
    for entry in profile.filesystem:
        operation = (
            FilesystemOperation.READ
            if entry.access is FilesystemAccess.READ
            else FilesystemOperation.WRITE
        )
        if filesystem_operation_allowed(
            base,
            EMPTY_ADDITIONAL_PERMISSIONS,
            entry.root,
            operation,
        ):
            continue
        if not filesystem_operation_allowed(base, profile, entry.root, operation):
            raise ToolError("请求的额外权限与不可升级的受保护路径冲突")
        effective_filesystem.append(entry)
    return AdditionalPermissionProfile(
        filesystem=tuple(effective_filesystem),
        network=profile.network,
        allow_local_binding=profile.allow_local_binding,
        sandbox_permissions=profile.sandbox_permissions,
    )


async def handle_terminal(
    args: dict[str, Any],
    *,
    timeout: float = 30.0,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("command 不能为空")

    timeout_explicit = "timeout" in args and args.get("timeout") is not None
    requested_timeout = float(args.get("timeout", timeout))
    max_timeout = _terminal_max_timeout()
    effective_timeout = min(max(requested_timeout, 1.0), max_timeout)
    background = bool(args.get("background", False))
    allowed, reason, error_code = _check_terminal_command(command)
    if not allowed and error_code == "policy_denied":
        return json.dumps(
            {"success": False, "error": reason, "error_code": error_code},
            ensure_ascii=False,
        )

    cwd = str(_resolve_base_dir())
    launch = None
    approval_decision = ""
    terminal_metadata: dict[str, Any] = {
        "execution_boundary": "host",
        "effective_home": str(Path.home().expanduser().resolve(strict=False)),
        "applied_permissions": {
            "filesystem": [],
            "network": [],
            "allow_local_binding": False,
            "sandbox_permissions": "use_default",
        },
    }
    if security_service is not None and workspace_store is not None:
        from crew.security.actions import normalize_exec_action
        from crew.security.approvals import ApprovalDecision
        from crew.security.context import build_security_context
        from crew.security.launch import (
            compile_process_launch,
            current_process_launch,
            packaged_runtime_argv,
            shell_argv,
        )
        from crew.security.models import (
            EMPTY_ADDITIONAL_PERMISSIONS,
            ConversationPermissionMode,
            SandboxPermissions,
            serialize_additional_permissions,
        )
        from crew.security.runtime_client import NativeRuntimeClient

        security_context = build_security_context(workspace_store)
        mode = security_service.mode_for(security_context)
        explicit_sandbox_permissions = args.get("sandbox_permissions")
        requested_permissions = (
            EMPTY_ADDITIONAL_PERMISSIONS
            if mode is ConversationPermissionMode.FULL_ACCESS
            else _parse_additional_permissions(
                args.get("additional_permissions"),
                cwd=Path(cwd),
                security_context=security_context,
                mode=mode,
                db_path=security_service.db_path,
                sandbox_permissions=explicit_sandbox_permissions,
            )
        )
        has_permission_entries = bool(
            requested_permissions.filesystem
            or requested_permissions.network
            or requested_permissions.allow_local_binding
        )
        if mode is not ConversationPermissionMode.FULL_ACCESS:
            if explicit_sandbox_permissions is None and has_permission_entries:
                requested_permissions = replace(
                    requested_permissions,
                    sandbox_permissions=SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS,
                )
            elif (
                requested_permissions.sandbox_permissions
                is SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS
                and not has_permission_entries
            ):
                raise ToolError("with_additional_permissions 必须声明非空 additional_permissions")
            elif (
                requested_permissions.sandbox_permissions is SandboxPermissions.USE_DEFAULT
                and has_permission_entries
            ):
                raise ToolError("use_default 不能与非空 additional_permissions 同时使用")
            elif (
                requested_permissions.sandbox_permissions
                is SandboxPermissions.REQUIRE_ESCALATED
                and has_permission_entries
            ):
                raise ToolError("require_escalated 不能与 additional_permissions 同时使用")
        permission_reason = args.get("justification", args.get("permission_reason", ""))
        if not isinstance(permission_reason, str) or len(permission_reason) > 1000:
            raise ToolError("justification 必须是长度不超过 1000 的字符串")
        if (
            requested_permissions.sandbox_permissions
            is SandboxPermissions.REQUIRE_ESCALATED
            and not permission_reason.strip()
        ):
            raise ToolError("require_escalated 必须提供 justification")
        prefix_rule = args.get("prefix_rule")
        if prefix_rule is not None and (
            not isinstance(prefix_rule, list)
            or len(prefix_rule) < 2
            or any(not isinstance(token, str) or not token for token in prefix_rule)
        ):
            raise ToolError("prefix_rule 必须是至少包含两个非空字符串的数组")
        if (
            prefix_rule is not None
            and requested_permissions.sandbox_permissions
            is not SandboxPermissions.REQUIRE_ESCALATED
        ):
            raise ToolError("prefix_rule 只能与 require_escalated 一起使用")
        final_argv = shell_argv(command)
        shell_kind = "powershell" if os.name == "nt" else "bash"
        classification = None
        if mode is not ConversationPermissionMode.FULL_ACCESS:
            classification = await NativeRuntimeClient(packaged_runtime_argv()).classify_shell(
                shell_kind=shell_kind,
                executable=final_argv[0],
                raw_command=command,
            )
            _validate_destructive_terminal_targets(
                classification.parsed_commands,
                shell_kind=shell_kind,
                raw_command=command,
                workspace_root=security_context.workspace_root,
                requested_permissions=requested_permissions,
            )
        # Classification fields are part of the exact action digest, so the request/UI,
        # grant, persistent rule, and eventual execution all refer to the same command.
        action = normalize_exec_action(
            final_argv,
            cwd,
            raw_command=command,
            shell_kind=shell_kind,
            parsed_commands=classification.parsed_commands if classification else (),
            canonical_digest=classification.canonical_digest if classification else "",
        )
        # The native sandbox is the authority for ordinary commands. Dangerous
        # classifications and every requested permission expansion still require
        # explicit approval in the two managed auto-run modes.
        authorization = security_service.authorize_exec_action(
            security_context,
            action,
            tool_name="terminal",
            risk_class=(
                "dangerous_command" if error_code == "approval_required" else "shell_command"
            ),
            requires_approval=(
                error_code == "approval_required" or not requested_permissions.empty
            ),
            additional_permissions=requested_permissions,
            preview=permission_reason,
            proposed_argv_prefix=prefix_rule,
        )
        authorized, approval = authorization
        granted_permissions = getattr(
            authorization,
            "additional_permissions",
            requested_permissions if authorized else None,
        )
        if not authorized:
            if approval is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": "该命令被安全规则拒绝",
                        "error_code": "policy_denied",
                    },
                    ensure_ascii=False,
                )
            # 阻塞等待 owner 决策：抛/回灌审批请求会让模型复述进正文、且 turn 结束后
            # grant 无人消费（"对话停了"）。批准则继续，拒绝则回干净错误让模型自适应。
            outcome = await security_service.await_decision(approval["request_id"])
            if outcome is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": "命令审批已过期或会话已变更",
                        "error_code": "approval_expired",
                    },
                    ensure_ascii=False,
                )
            if outcome.decision is ApprovalDecision.REJECT:
                return json.dumps(
                    {
                        "success": False,
                        "error": "用户拒绝了该命令",
                        "error_code": "approval_rejected",
                    },
                    ensure_ascii=False,
                )
            approval_decision = outcome.decision.value
            # 批准：grant/rule 已由 decide() 落地；复用同一 action 消费 once grant，
            # 避免二次 shell_argv/which 在 PATH 变化时生成不同 digest。
            authorization = security_service.authorize_exec_action(
                security_context,
                action,
                tool_name="terminal",
                risk_class=("dangerous_command" if error_code == "approval_required" else "shell_command"),
                requires_approval=True,
                additional_permissions=requested_permissions,
                preview=permission_reason,
            )
            authorized, approval = authorization
            granted_permissions = getattr(
                authorization,
                "additional_permissions",
                requested_permissions if authorized else None,
            )
            if not authorized:
                return json.dumps(
                    {
                        "success": False,
                        "error": "批准后授权校验失败，请重试",
                        "error_code": "approval_lost",
                    },
                    ensure_ascii=False,
                )
        inherited_launch = current_process_launch.get()
        launch = compile_process_launch(
            security_context,
            security_service.mode_for(security_context),
            db_path=security_service.db_path,
            audit=security_service.audit,
            additional_permissions=granted_permissions,
            trusted_readable_roots=(
                inherited_launch.trusted_readable_roots if inherited_launch is not None else ()
            ),
        )
        applied_permissions = launch.additional_permissions
        terminal_metadata = {
            "execution_boundary": "sandbox" if launch.managed else "host",
            "effective_home": str(Path.home().expanduser().resolve(strict=False)),
            "applied_permissions": serialize_additional_permissions(applied_permissions),
        }
        if approval_decision:
            terminal_metadata["approval_decision"] = approval_decision
    elif not allowed:
        return json.dumps(
            {"success": False, "error": reason, "error_code": error_code},
            ensure_ascii=False,
        )

    from crew.core.runctx import (
        current_owner_account_id,
        current_parent_task_id,
        current_request_id,
        current_session_id,
        current_tool_call_id,
    )
    from crew.state.home import get_owner_runtime_home
    from crew.tools.process_registry import process_registry

    runtime = getattr(process_registry, "_task_runtime", None)
    task_id = ""
    output_ref = ""
    if runtime is not None:
        owner = current_owner_account_id.get()
        output_dir = get_owner_runtime_home(owner) / "tasks"
        task = runtime.create_runtime(
            kind="shell",
            session_id=current_session_id.get() or "shell",
            owner_account_id=owner,
            request_id=current_request_id.get(),
            tool_call_id=current_tool_call_id.get(),
            parent_task_id=current_parent_task_id.get(),
            title=command[:120],
            detail=command,
            output_ref=str(output_dir / "pending.log"),
            execution_timeout=(
                effective_timeout
                if timeout_explicit
                else float(runtime.defaults.get("shell_execution", 0.0))
            ),
            inactivity_timeout=float(
                getattr(getattr(runtime, "defaults", {}), "get", lambda *_: 600.0)("shell_inactivity", 600.0)
            ),
            backgrounded=background,
        )
        task_id = task["task_id"]
        output_ref = str(output_dir / f"{task_id}.log")
        runtime.update(task_id, owner_account_id=owner, output_ref=output_ref)

    if background:
        watch_patterns = args.get("watch_patterns") or []
        if not isinstance(watch_patterns, list):
            watch_patterns = [str(watch_patterns)]
        notify_on_complete = bool(args.get("notify_on_complete", False))
        try:
            if runtime is not None and task_id:
                runtime.mark_running(task_id)
            spawn = process_registry.spawn_security if launch is not None else process_registry.spawn_local
            session = spawn(
                command,
                **({"launch": launch} if launch is not None else {}),
                cwd=cwd,
                session_key=current_session_id.get(),
                owner_account_id=current_owner_account_id.get(),
                watch_patterns=[str(p) for p in watch_patterns],
                notify_on_complete=notify_on_complete,
                task_id=task_id,
                output_ref=output_ref,
            )
            if runtime is not None and task_id:
                current = runtime.get(task_id, owner_account_id=owner)
                if current["status"] == "running":
                    runtime.touch_activity(task_id, {"pid": session.pid, "process_session_id": session.id})
                    runtime.attach_worker(
                        task_id,
                        None,
                        cancel=lambda reason, pid=session.pid: runtime.kill_process_group(int(pid or 0), reason),
                    )
            return json.dumps({
                "success": True,
                "background": True,
                "session_id": session.id,
                "task_id": task_id or session.id,
                "pid": session.pid,
                "command": command,
                "cwd": cwd,
                **terminal_metadata,
                "hint": "后台运行中。用 process(action='poll'|'log'|'wait'|'kill', session_id=...) 查询。",
            }, ensure_ascii=False)
        except Exception as exc:
            raise ToolError(f"后台启动失败: {exc}") from exc

    # Foreground and background share one managed process. A long-running
    # foreground command is reclassified in place; it is never restarted.
    if runtime is not None and task_id:
        runtime.mark_running(task_id)
    spawn = process_registry.spawn_security if launch is not None else process_registry.spawn_local
    session = spawn(
        command,
        **({"launch": launch} if launch is not None else {}),
        cwd=cwd,
        session_key=current_session_id.get(),
        owner_account_id=current_owner_account_id.get(),
        notify_on_complete=False,
        task_id=task_id,
        output_ref=output_ref,
    )
    if runtime is not None and task_id:
        current = runtime.get(task_id, owner_account_id=owner)
        if current["status"] == "running":
            runtime.touch_activity(task_id, {"pid": session.pid, "process_session_id": session.id})
            runtime.attach_worker(
                task_id,
                None,
                cancel=lambda reason, pid=session.pid: runtime.kill_process_group(int(pid or 0), reason),
            )

    auto_after = 15.0
    if runtime is not None:
        auto_after = float(getattr(runtime, "auto_background_after", auto_after))
    wait_budget = min(effective_timeout, auto_after) if auto_after > 0 else effective_timeout
    started = time.monotonic()
    # OCC Stage 5 onProgress：前台阻塞期按已累计输出增量推给前端，让用户实时看到命令输出。
    emitted_len = 0
    last_emit = 0.0
    while not session.exited and time.monotonic() - started < wait_budget:
        await asyncio.sleep(0.1)
        buf = session.output_buffer
        if len(buf) > emitted_len and (time.monotonic() - last_emit) >= 0.5:
            await emit_tool_progress(buf[emitted_len:])
            emitted_len = len(buf)
            last_emit = time.monotonic()

    if not session.exited:
        elapsed = time.monotonic() - started
        if timeout_explicit and effective_timeout <= auto_after and elapsed >= effective_timeout:
            process_registry.kill_process(session.id)
            if runtime is not None and task_id:
                runtime.finish(
                    task_id,
                    owner_account_id=owner,
                    status="timed_out",
                    error=f"命令超时（>{effective_timeout}s）",
                )
            raise ToolError(f"命令超时（>{effective_timeout}s）")
        if runtime is not None and task_id:
            runtime.set_backgrounded(task_id, automatic=True)
        return json.dumps({
            "success": True,
            "background": True,
            "auto_backgrounded": True,
            "session_id": session.id,
            "task_id": task_id or session.id,
            "pid": session.pid,
            "command": command,
            "cwd": cwd,
            **terminal_metadata,
            "output_ref": output_ref,
            "hint": "命令超过前台阻塞预算，已原地转为后台任务；完成后会自动通知。",
        }, ensure_ascii=False)

    text = session.output_buffer
    # 输出后处理：去 ANSI → 头尾截断 → 脱敏（对齐 Hermes）
    text = strip_ansi(text)
    text, truncated = truncate_output(text)
    # 输出后处理：去 ANSI → 头尾截断 → 脱敏（对齐 Hermes）。force=True：安全边界
    # 输出脱敏不可由 CREW_REDACT_SECRETS=false 关闭（spec §109）。
    text = redact_sensitive_text(text, force=True)

    if session.stable_error_code:
        runtime_messages = {
            "runtime_crashed": "安全运行时异常退出，命令未执行",
            "runtime_protocol_mismatch": "安全运行时协议不匹配，命令未执行",
            "sandbox_unavailable": "当前平台的安全沙箱不可用，命令未执行",
            "sandbox_denied": "安全沙箱拒绝执行该命令",
            "policy_denied": "安全策略拒绝执行该命令",
            "network_unavailable": "安全运行时无法建立获批的网络边界",
            "timeout": "安全运行时执行超时",
            "output_truncated": "安全运行时输出超过限制",
        }
        return json.dumps(
            {
                "success": False,
                "error": runtime_messages.get(
                    session.stable_error_code,
                    "安全运行时失败，命令未执行",
                ),
                "error_code": session.stable_error_code,
                "cwd": cwd,
                "command": command,
                "output": text,
                "retryable": session.stable_error_code == "sandbox_denied",
                "retry_hint": (
                    "请根据错误输出申请精确的 additional_permissions 后重试；"
                    "若仍需宿主用户权限，使用 require_escalated 并明确说明原因。"
                    if session.stable_error_code == "sandbox_denied"
                    else ""
                ),
                **terminal_metadata,
            },
            ensure_ascii=False,
        )

    result = {
        "success": True,
        "cwd": cwd,
        "command": command,
        "exit_code": session.exit_code,
        "output": text,
        "truncated": truncated,
        **terminal_metadata,
    }
    if task_id:
        result["task_id"] = task_id
    return json.dumps(result, ensure_ascii=False)


def _assert_no_symlink_component(path: Path) -> None:
    """授权后、I/O 前复检：canonical path 的任一组件不能是符号链接（H-5 轻量加固）。

    防 TOCTOU：授权时父目录是普通目录，授权后到 open 之间被换为指向控制面（crew.db /
    identity / 凭据）的 symlink/junction，随后的 ``read_bytes``/``snapshot`` 会跟随新链接
    逃逸出授权范围。逐级 ``lstat``，任一组件为符号链接即拒绝。完整修复需 POSIX
    ``openat``+``O_NOFOLLOW``+``fstat`` 或 Windows reparse-safe handle；此为缩小窗口的
    轻量加固，仍存在 lstat 后到 open 前的残余窗口。
    """
    current = path
    while current != current.parent:
        try:
            if current.is_symlink():
                raise ToolError(f"路径组件 {current} 是符号链接，拒绝（TOCTOU 防护）")
        except OSError as exc:
            raise ToolError(f"路径 {current} 校验失败：{exc}") from exc
        current = current.parent


async def handle_file_read(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    path = await authorize_file_tool(
        args,
        operation="read",
        tool_name="file_read",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    _assert_no_symlink_component(path)
    if _is_blocked_device(str(args.get("path", ""))):
        raise ToolError(f"禁止读取设备/特殊文件: {path}")
    if not path.exists():
        raise ToolError(f"文件不存在: {path}")
    if not path.is_file():
        raise ToolError(f"不是文件: {path}")
    if _has_binary_extension(path):
        return f"[二进制文件，跳过文本读取]: {path}"
    try:
        # 阻塞 I/O 丢线程池，避免卡住事件循环（拖垮网关心跳）
        raw_bytes = await asyncio.to_thread(read_verified_bytes, path, max_bytes=MAX_READ_FILE_BYTES)
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"读取失败: {exc}") from exc

    # BOM / line-ending preservation (Hermes-compatible)
    text, had_bom = _strip_bom(text.replace("\r\r\n", "\r\n"))
    if not had_bom:
        # On Windows, test fixtures and user-created text files may be written
        # with text-mode newline translation. Present ordinary reads as LF for
        # stable model-facing output; BOM-tagged files keep their original CRLF.
        text = _normalize_line_endings(text, "\n")

    total_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    offset, limit = _normalize_read_pagination(
        total_lines,
        args.get("offset"),
        args.get("limit"),
    )
    sliced = _apply_line_pagination(text, offset, limit)
    truncated = len(sliced) < len(text)

    max_chars = _get_max_read_chars()
    hint = ""
    if len(sliced) > max_chars:
        sliced = sliced[:max_chars]
        truncated = True
        hint = f"单次读取上限 {max_chars} 字符，请用 offset/limit 分段读取。"
    elif raw_bytes and len(raw_bytes) > 512_000 and (args.get("limit") is None or int(args.get("limit") or 0) > 200):
        hint = "文件较大，建议使用 offset/limit 读取目标片段。"

    if had_bom:
        hint = (hint + "\n" if hint else "") + "文件包含 UTF-8 BOM，已自动剥离显示。"

    return _format_read_result(
        sliced,
        total_lines=total_lines,
        file_size=len(raw_bytes),
        offset=offset,
        limit=limit,
        truncated=truncated,
        hint=hint.strip(),
    )


def _write_file_sync(path: Path, content: str, append: bool) -> dict[str, Any]:
    """同步执行文件写入（含 BOM / 行尾保留）。阻塞 I/O，由调用方丢线程池执行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    version = snapshot_file(path, max_bytes=MAX_READ_FILE_BYTES)

    # Preserve existing file's BOM and line endings (Hermes-compatible)
    original_ending = None
    had_bom = False
    if path.exists() and path.is_file() and not append:
        try:
            raw = version.data
            existing = raw.decode("utf-8", errors="replace")
            existing, had_bom = _strip_bom(existing)
            original_ending = _detect_line_ending(existing)
        except Exception:
            pass

    if original_ending is not None:
        content = _normalize_line_endings(content, original_ending)
    if had_bom and not content.startswith("﻿"):
        content = "﻿" + content

    encoded = content.encode("utf-8")
    if append and version.exists:
        encoded = version.data + encoded
    atomic_replace_bytes(path, encoded, version)

    return {
        "success": True,
        "path": str(path),
        "bytes_written": len(content.encode("utf-8")),
        "append": append,
    }


async def handle_file_write(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    path = await authorize_file_tool(
        args,
        operation="write",
        tool_name="file_write",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    _assert_no_symlink_component(path)
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))

    sensitive = _check_sensitive_path(str(args.get("path", "")))
    if sensitive:
        raise ToolError(sensitive)

    try:
        # 阻塞 I/O 丢线程池，避免卡住事件循环（拖垮网关心跳）
        result = await asyncio.to_thread(_write_file_sync, path, content, append)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"写入失败: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


def _delete_file_sync(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ToolError(f"文件不存在: {path}") from exc
    if path.is_symlink():
        raise ToolError("file_delete 不删除符号链接；请提供实际文件的绝对路径")
    if not path.is_file():
        raise ToolError(f"file_delete 只支持普通文件，不递归删除目录: {path}")
    path.unlink()
    return {
        "success": True,
        "path": str(path),
        "deleted": True,
        "bytes_deleted": int(metadata.st_size),
    }


async def handle_file_delete(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    requested_path = Path(str(args.get("path") or ".")).expanduser()
    if not requested_path.is_absolute():
        requested_path = _resolve_base_dir() / requested_path
    requested_path = requested_path.parent.resolve(strict=False) / requested_path.name
    path = await authorize_file_tool(
        args,
        operation="delete",
        tool_name="file_delete",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    if requested_path.is_symlink():
        raise ToolError("file_delete 不删除符号链接；请提供实际文件的绝对路径")
    _assert_no_symlink_component(path)
    if _is_blocked_device(str(args.get("path", ""))):
        raise ToolError(f"禁止删除设备/特殊文件: {path}")
    sensitive = _check_sensitive_path(str(args.get("path", "")))
    if sensitive:
        raise ToolError(sensitive)
    try:
        result = await asyncio.to_thread(_delete_file_sync, path)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"删除失败: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


def register_builtin_tools(
    registry: Registry,
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> None:
    """注册所有内置工具。"""
    registry.register(
        name="terminal",
        toolset="terminal",
        schema=TERMINAL_SCHEMA,
        handler=partial(
            handle_terminal,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="运行命令",
        ui_label_template="运行 {command}",
        always_load=True,
        search_hint="shell command terminal bash powershell execute background process",
    )
    registry.register(
        name="file_read",
        toolset="file",
        schema=FILE_READ_SCHEMA,
        handler=partial(
            handle_file_read,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="读取文件",
        ui_label_template="读取 {path}",
        always_load=True,
        search_hint="read file view contents inspect text",
    )
    registry.register(
        name="file_write",
        toolset="file",
        schema=FILE_WRITE_SCHEMA,
        handler=partial(
            handle_file_write,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="写入文件",
        ui_label_template="写入 {path}",
        always_load=True,
        search_hint="write file append create save text",
    )
    registry.register(
        name="file_delete",
        toolset="file",
        schema=FILE_DELETE_SCHEMA,
        handler=partial(
            handle_file_delete,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="删除文件",
        ui_label_template="删除 {path}",
        always_load=True,
        search_hint="delete remove file unlink exact path",
    )

    from crew.tools.file_tools import register_file_tools
    from crew.tools.interaction import register_interaction_tools
    from crew.tools.memory_tools import register_memory_tools
    from crew.tools.process_registry import register_process_tool
    from crew.tools.skills_tools import register_skills_tools
    from crew.tools.web_tools import register_web_tools

    register_process_tool(registry)
    register_file_tools(
        registry,
        workspace_store=workspace_store,
        security_service=security_service,
    )
    register_skills_tools(
        registry,
        workspace_store=workspace_store,
        security_service=security_service,
    )
    register_memory_tools(registry)
    register_web_tools(
        registry,
        workspace_store=workspace_store,
        security_service=security_service,
    )
    register_interaction_tools(registry)
