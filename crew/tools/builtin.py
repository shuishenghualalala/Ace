"""内置工具：terminal / file_read / file_write。

本文件使用 Hermes 风格工具格式：
  SCHEMA + handler(args) + registry.register(name, toolset, schema, handler)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from functools import partial
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.core.runctx import emit_tool_progress
from crew.tools.file_utils import (
    _DEFAULT_MAX_FILE_BYTES,
    FileIdentity,
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
)
from crew.tools.output_filters import strip_ansi, truncate_output
from crew.tools.redact import redact_sensitive_text, safe_public_error
from crew.tools.registry import Registry
from crew.tools.security_guard import AuthorizedFileTarget, authorize_file_tool
from crew.tools.terminal_guard import detect_dangerous_command, detect_hardline_command

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


def _terminal_max_timeout() -> float:
    """从配置读取最大前台超时，默认 600s。"""
    try:
        from crew.state.config import load_config

        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("terminal", {}).get("max_timeout")
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    except Exception:  # noqa: BLE001, S110 - optional configuration falls back safely
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
    return True, None, None


def _classification_auto_allows(mode: Any, classification: Any) -> bool:
    """Auto-allow only a classifier result with current executable identities."""
    from crew.security.models import ConversationPermissionMode
    from crew.security.runtime_client import (
        ShellClassification,
        ShellVerdict,
        _command_identity,
        _executable_identity,
    )

    if (
        mode is not ConversationPermissionMode.AUTO_REVIEW
        or not isinstance(classification, ShellClassification)
        or classification.verdict is not ShellVerdict.ALLOW_READ_ONLY
        or not classification.executable
        or not hmac.compare_digest(
            classification.executable_digest,
            _executable_identity(classification.executable)[1],
        )
        or len(classification.command_identities) != len(classification.parsed_commands)
        or not classification.command_identities
    ):
        return False
    return all(
        _command_identity(command[0]) == identity
        for command, identity in zip(
            classification.parsed_commands,
            classification.command_identities,
            strict=True,
        )
        if command
    )


TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "执行 shell 命令或显式 argv，返回 stdout/stderr。argv 模式不经过 shell，首项必须是绝对可执行文件。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 256,
                "description": "不经过 shell 的参数数组；argv[0] 必须是绝对可执行文件",
            },
            "timeout": {"type": "number", "description": "超时秒数，默认 30，最大 600"},
            "background": {
                "type": "boolean",
                "description": "是否后台执行（立即返回 session_id，用 process 工具查询）",
            },
            "watch_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "仅后台模式：输出命中其中任一子串时排队通知（限流，连续刷屏会自动降级为完成通知）",
            },
            "notify_on_complete": {
                "type": "boolean",
                "description": "仅后台模式：进程退出时排队一条完成通知（含退出码和输出尾部）",
            },
        },
        "oneOf": [
            {"required": ["command"], "not": {"required": ["argv"]}},
            {"required": ["argv"], "not": {"required": ["command"]}},
        ],
        "additionalProperties": False,
    },
}


def _audit_terminal_boundary_denial(
    security_service: Any,
    context: Any,
    action: Any,
    error_code: str,
) -> None:
    """Persist a post-approval spawn denial through the configured security audit."""
    record = getattr(security_service, "_audit_exec", None)
    if not callable(record):
        return
    record(
        context,
        action,
        "deny",
        f"post_approval_boundary_{error_code}",
        "terminal",
    )


async def handle_terminal(
    args: dict[str, Any],
    *,
    timeout: float = 30.0,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    has_command = args.get("command") is not None
    has_argv = args.get("argv") is not None
    if has_command == has_argv:
        raise ToolError("command 与 argv 必须且只能提供一个")
    direct_argv: tuple[str, ...] | None = None
    if has_argv:
        raw_argv = args.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not 1 <= len(raw_argv) <= 256
            or any(
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or len(token.encode("utf-8")) > 16 * 1024
                for token in raw_argv
            )
        ):
            raise ToolError("argv 必须是 1-256 个非空、无 NUL 的有界字符串")
        executable = Path(raw_argv[0]).expanduser()
        if not executable.is_absolute():
            raise ToolError("argv[0] 必须是绝对可执行文件路径")
        try:
            executable = executable.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError("argv[0] 指向的可执行文件不可用") from exc
        if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
            raise ToolError("argv[0] 不是可执行普通文件")
        direct_argv = (str(executable), *raw_argv[1:])
        command = json.dumps(list(direct_argv), ensure_ascii=False)
    else:
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
    final_argv = None
    granted_additional_permissions = None
    security_wired = security_service is not None and workspace_store is not None
    if not security_wired:
        try:
            from crew.security.launch import (
                current_process_launch,
                shell_argv,
                validate_process_launch,
            )

            explicit_launch = current_process_launch.get()
            if explicit_launch is None or explicit_launch.sandboxed:
                return json.dumps(
                    {
                        "success": False,
                        "error": "terminal 缺少安全授权上下文",
                        "error_code": "security_context_missing",
                    },
                    ensure_ascii=False,
                )
            validate_process_launch(explicit_launch)
            launch = explicit_launch
            final_argv = direct_argv or shell_argv(command)
        except Exception:  # noqa: BLE001 - any boundary failure must deny execution
            return json.dumps(
                {
                    "success": False,
                    "error": "terminal 安全启动边界不可用",
                    "error_code": "security_unavailable",
                },
                ensure_ascii=False,
            )
    else:
        try:
            from crew.security.actions import normalize_exec_action
            from crew.security.approvals import ApprovalDecision
            from crew.security.context import build_security_context
            from crew.security.launch import (
                ProcessLaunch,
                compile_process_launch,
                packaged_runtime_argv,
                shell_argv,
            )
            from crew.security.models import AdditionalPermissionProfile
            from crew.security.policy import merge_additional_permissions
            from crew.security.runtime_client import NativeRuntimeClient
            from crew.security.snapshot import _verified_file_digest

            security_context = build_security_context(workspace_store)
            mode = security_service.mode_for(security_context)
            final_argv = direct_argv or shell_argv(command)
            executable_path = Path(final_argv[0]).expanduser().resolve(strict=True)
            if not executable_path.is_file():
                raise ValueError("terminal executable is not a regular file")
            executable_digest = _verified_file_digest(executable_path)
            shell_kind = (
                "argv" if direct_argv is not None else ("powershell" if os.name == "nt" else "bash")
            )
            classification = (
                None
                if direct_argv is not None
                else await NativeRuntimeClient(packaged_runtime_argv()).classify_shell(
                    shell_kind=shell_kind,
                    executable=str(executable_path),
                    raw_command=command,
                )
            )
            # Classification fields are part of the exact action digest, so the request/UI,
            # grant, persistent rule, and eventual execution all refer to the same command.
            action = normalize_exec_action(
                final_argv,
                cwd,
                raw_command=command,
                shell_kind=shell_kind,
                parsed_commands=(
                    classification.parsed_commands
                    if classification
                    else ((direct_argv,) if direct_argv is not None else ())
                ),
                canonical_digest=classification.canonical_digest if classification else "",
                executable_digest=executable_digest,
                command_identities=(
                    classification.command_identities
                    if classification
                    else ((str(executable_path), executable_digest),)
                ),
            )
            # 只有 auto_review + runtime 成功证明全部命令只读时可自动放行；request_approval
            # 始终询问，classifier 缺失/崩溃/未知语法均 ASK，不回退 Python 正则猜测。
            proven_read_only = _classification_auto_allows(mode, classification)
            authorized, approval = security_service.authorize_exec_action(
                security_context,
                action,
                tool_name="terminal",
                risk_class=(
                    "dangerous_command" if error_code == "approval_required" else "shell_command"
                ),
                auto_allow=proven_read_only,
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
                if outcome is None or outcome.decision is ApprovalDecision.REJECT:
                    return json.dumps(
                        {
                            "success": False,
                            "error": "用户未批准该命令",
                            "error_code": "approval_rejected",
                        },
                        ensure_ascii=False,
                    )
                # 批准：grant/rule 已由 decide() 落地；复用同一 action 消费 once grant，
                # 避免二次 shell_argv/which 在 PATH 变化时生成不同 digest。
                authorized, approval = security_service.authorize_exec_action(
                    security_context,
                    action,
                    tool_name="terminal",
                    risk_class=(
                        "dangerous_command"
                        if error_code == "approval_required"
                        else "shell_command"
                    ),
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
                if isinstance(approval, dict):
                    granted_additional_permissions = approval.get("additional_permissions")
            launch = compile_process_launch(
                security_context,
                mode,
                db_path=security_service.db_path,
                approved_action=action,
                additional_permissions=(
                    merge_additional_permissions(
                        security_service.grants.additional_permissions(security_context),
                        granted_additional_permissions or AdditionalPermissionProfile(),
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - any boundary failure must deny execution
            return json.dumps(
                {
                    "success": False,
                    "error": "terminal 安全启动边界不可用",
                    "error_code": "security_unavailable",
                },
                ensure_ascii=False,
            )
        if launch is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "terminal 缺少已编译的安全启动决策",
                    "error_code": "security_launch_missing",
                },
                ensure_ascii=False,
            )
        if not isinstance(launch, ProcessLaunch):
            return json.dumps(
                {
                    "success": False,
                    "error": "terminal 安全启动决策无效",
                    "error_code": "security_launch_invalid",
                },
                ensure_ascii=False,
            )

    # Every terminal process crosses one explicit ProcessLaunch. The registry alone
    # may translate an explicitly disabled launch into the audited host path.
    launch_options = {"launch": launch, "launch_argv": final_argv}

    from crew.core.runctx import (
        current_owner_account_id,
        current_parent_task_id,
        current_request_id,
        current_session_id,
        current_tool_call_id,
    )
    from crew.security.runtime_client import NativeRuntimeError
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
                getattr(getattr(runtime, "defaults", {}), "get", lambda *_: 600.0)(
                    "shell_inactivity", 600.0
                )
            ),
            backgrounded=background,
        )
        task_id = task["task_id"]
        output_ref = str(output_dir / f"{task_id}.log")
        runtime.update(task_id, owner_account_id=owner, output_ref=output_ref)
        if launch is not None and launch.task_id != task_id:
            from crew.security.launch import bind_process_launch_task

            launch = bind_process_launch_task(launch, task_id)
            launch_options["launch"] = launch

    if background:
        watch_patterns = args.get("watch_patterns") or []
        if not isinstance(watch_patterns, list):
            watch_patterns = [str(watch_patterns)]
        notify_on_complete = bool(args.get("notify_on_complete", False))
        try:
            if runtime is not None and task_id:
                runtime.mark_running(task_id)
            session = process_registry.spawn_security(
                command,
                **launch_options,
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
                    runtime.touch_activity(
                        task_id, {"pid": session.pid, "process_session_id": session.id}
                    )
                    runtime.attach_worker(
                        task_id,
                        None,
                        cancel=lambda _reason, process_id=session.id, process_owner=session.owner_account_id: (
                            process_registry.kill_process(
                                process_id,
                                owner_account_id=process_owner,
                            )
                        ),
                    )
            return json.dumps(
                {
                    "success": True,
                    "background": True,
                    "session_id": session.id,
                    "task_id": task_id or session.id,
                    "pid": session.pid,
                    "command": command,
                    "cwd": cwd,
                    "hint": "后台运行中。用 process(action='poll'|'log'|'wait'|'kill', session_id=...) 查询。",
                },
                ensure_ascii=False,
            )
        except NativeRuntimeError as exc:
            if security_wired:
                _audit_terminal_boundary_denial(
                    security_service,
                    security_context,
                    action,
                    exc.code.value,
                )
            return json.dumps(
                {
                    "success": False,
                    "error": "安全运行时不可用，命令未执行",
                    "error_code": exc.code.value,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            raise ToolError(safe_public_error(exc, "后台启动失败")) from exc

    # Foreground and background share one managed process. A long-running
    # foreground command is reclassified in place; it is never restarted.
    if runtime is not None and task_id:
        runtime.mark_running(task_id)
    try:
        session = process_registry.spawn_security(
            command,
            **launch_options,
            cwd=cwd,
            session_key=current_session_id.get(),
            owner_account_id=current_owner_account_id.get(),
            notify_on_complete=False,
            task_id=task_id,
            output_ref=output_ref,
        )
    except NativeRuntimeError as exc:
        if security_wired:
            _audit_terminal_boundary_denial(
                security_service,
                security_context,
                action,
                exc.code.value,
            )
        return json.dumps(
            {
                "success": False,
                "error": "安全运行时不可用，命令未执行",
                "error_code": exc.code.value,
            },
            ensure_ascii=False,
        )
    if runtime is not None and task_id:
        current = runtime.get(task_id, owner_account_id=owner)
        if current["status"] == "running":
            runtime.touch_activity(task_id, {"pid": session.pid, "process_session_id": session.id})
            runtime.attach_worker(
                task_id,
                None,
                cancel=lambda _reason, process_id=session.id, process_owner=session.owner_account_id: (
                    process_registry.kill_process(
                        process_id,
                        owner_account_id=process_owner,
                    )
                ),
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
            process_registry.kill_process(
                session.id,
                owner_account_id=session.owner_account_id or current_owner_account_id.get(),
            )
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
        return json.dumps(
            {
                "success": True,
                "background": True,
                "auto_backgrounded": True,
                "session_id": session.id,
                "task_id": task_id or session.id,
                "pid": session.pid,
                "command": command,
                "cwd": cwd,
                "output_ref": output_ref,
                "hint": "命令超过前台阻塞预算，已原地转为后台任务；完成后会自动通知。",
            },
            ensure_ascii=False,
        )

    text = session.output_buffer
    # 输出后处理：去 ANSI → 头尾截断 → 脱敏（对齐 Hermes）
    text = strip_ansi(text)
    text, truncated = truncate_output(text)
    # 输出后处理：去 ANSI → 头尾截断 → 脱敏（对齐 Hermes）。force=True：安全边界
    # 输出脱敏不可由 CREW_REDACT_SECRETS=false 关闭（spec §109）。
    text = redact_sensitive_text(text, force=True)

    result = {
        "success": True,
        "cwd": cwd,
        "command": command,
        "exit_code": session.exit_code,
        "output": text,
        "truncated": truncated,
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
            raise ToolError(safe_public_error(exc, "路径校验失败")) from exc
        current = current.parent


async def handle_file_read(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    authorized = await authorize_file_tool(
        args,
        operation="read",
        tool_name="file_read",
        workspace_store=workspace_store,
        security_service=security_service,
        bind_identity=True,
    )
    if not isinstance(authorized, AuthorizedFileTarget):
        raise ToolError("文件授权未绑定目标身份")
    path = authorized.path
    _assert_no_symlink_component(path)
    if _is_blocked_device(str(args.get("path", ""))):
        raise ToolError(f"禁止读取设备/特殊文件: {path}")
    if not path.exists():
        raise ToolError(f"文件不存在: {path}")
    if not path.is_file():
        raise ToolError(f"不是文件: {path}")
    if _has_binary_extension(path):
        return "[二进制文件，跳过文本读取]"
    try:
        # 阻塞 I/O 丢线程池，避免卡住事件循环（拖垮网关心跳）
        raw_bytes = await asyncio.to_thread(
            read_verified_bytes,
            path,
            max_bytes=_DEFAULT_MAX_FILE_BYTES,
            expected_identity=authorized.identity,
        )
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        raise ToolError(safe_public_error(exc, "读取失败")) from exc

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
    elif (
        raw_bytes
        and len(raw_bytes) > 512_000
        and (args.get("limit") is None or int(args.get("limit") or 0) > 200)
    ):
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


def _write_file_sync(
    path: Path,
    content: str,
    append: bool,
    expected_identity: FileIdentity,
) -> dict[str, Any]:
    """同步执行文件写入（含 BOM / 行尾保留）。阻塞 I/O，由调用方丢线程池执行。"""
    version = snapshot_file(path, expected_identity=expected_identity)

    # Preserve existing file's BOM and line endings (Hermes-compatible)
    original_ending = None
    had_bom = False
    if path.exists() and path.is_file() and not append:
        try:
            raw = version.data
            existing = raw.decode("utf-8", errors="replace")
            existing, had_bom = _strip_bom(existing)
            original_ending = _detect_line_ending(existing)
        except (AttributeError, TypeError, ValueError):
            original_ending = None
            had_bom = False

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
    authorized = await authorize_file_tool(
        args,
        operation="write",
        tool_name="file_write",
        workspace_store=workspace_store,
        security_service=security_service,
        bind_identity=True,
    )
    if not isinstance(authorized, AuthorizedFileTarget):
        raise ToolError("文件授权未绑定目标身份")
    path = authorized.path
    _assert_no_symlink_component(path)
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))

    sensitive = _check_sensitive_path(str(args.get("path", "")))
    if sensitive:
        raise ToolError(sensitive)

    try:
        # 阻塞 I/O 丢线程池，避免卡住事件循环（拖垮网关心跳）
        result = await asyncio.to_thread(
            _write_file_sync,
            path,
            content,
            append,
            authorized.identity,
        )
    except Exception as exc:
        raise ToolError(safe_public_error(exc, "写入失败")) from exc
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

    from crew.tools.permission_tools import (
        REQUEST_PERMISSIONS_SCHEMA,
        handle_request_permissions,
    )

    registry.register(
        name="request_permissions",
        toolset="security",
        schema=REQUEST_PERMISSIONS_SCHEMA,
        handler=partial(
            handle_request_permissions,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="申请额外权限",
        ui_label_template="申请额外权限",
        always_load=True,
        search_hint="request permissions filesystem network sandbox escalation",
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
    register_skills_tools(registry)
    register_memory_tools(registry)
    register_web_tools(
        registry,
        workspace_store=workspace_store,
        security_service=security_service,
    )
    register_interaction_tools(registry)
