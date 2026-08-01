"""内置工具：terminal / file_read / file_write。

本文件使用 Crew工具格式：
  SCHEMA + handler(args) + registry.register(name, toolset, schema, handler)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
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
    _resolve_path,
    _strip_bom,
)
from crew.tools.output_filters import strip_ansi, truncate_output
from crew.tools.redact import redact_sensitive_text
from crew.tools.registry import Registry
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
    except Exception:
        pass
    return 600.0


def _check_terminal_command(command: str, force: bool) -> tuple[bool, str | None]:
    """检查命令是否被阻止或需要 force。"""
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        return False, f"BLOCKED (hardline): {hardline_desc}. 该命令无条件禁止通过 agent 执行。"
    is_dangerous, dangerous_desc = detect_dangerous_command(command)
    if is_dangerous and not force:
        return False, (
            f"DANGEROUS: {dangerous_desc}. "
            "若已确认风险，请使用 terminal(force=true) 重试。"
        )
    return True, None


TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "在当前工作目录执行一条 shell 命令，返回 stdout/stderr。支持 timeout、后台运行、危险命令检测。",
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
            "force": {"type": "boolean", "description": "是否跳过危险命令检测（需用户已确认）"},
        },
        "required": ["command"],
    },
}


async def handle_terminal(args: dict[str, Any], *, timeout: float = 30.0) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("command 不能为空")

    timeout_explicit = "timeout" in args and args.get("timeout") is not None
    requested_timeout = float(args.get("timeout", timeout))
    max_timeout = _terminal_max_timeout()
    effective_timeout = min(max(requested_timeout, 1.0), max_timeout)
    background = bool(args.get("background", False))
    force = bool(args.get("force", False))

    allowed, reason = _check_terminal_command(command, force)
    if not allowed:
        return json.dumps({"success": False, "error": reason}, ensure_ascii=False)

    cwd = str(_resolve_base_dir())

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
            session = process_registry.spawn_local(
                command,
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
                "hint": "后台运行中。用 process(action='poll'|'log'|'wait'|'kill', session_id=...) 查询。",
            }, ensure_ascii=False)
        except Exception as exc:
            raise ToolError(f"后台启动失败: {exc}") from exc

    # Foreground and background share one managed process. A long-running
    # foreground command is reclassified in place; it is never restarted.
    if runtime is not None and task_id:
        runtime.mark_running(task_id)
    session = process_registry.spawn_local(
        command,
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
    # Crew Stage 5 onProgress：前台阻塞期按已累计输出增量推给前端，让用户实时看到命令输出。
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
            "output_ref": output_ref,
            "hint": "命令超过前台阻塞预算，已原地转为后台任务；完成后会自动通知。",
        }, ensure_ascii=False)

    text = session.output_buffer
    # 输出后处理：去 ANSI → 头尾截断 → 脱敏（采用）
    text = strip_ansi(text)
    text, truncated = truncate_output(text)
    text = redact_sensitive_text(text)

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


async def handle_file_read(args: dict[str, Any]) -> str:
    path = _resolve_path(str(args.get("path", "")))
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
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"读取失败: {exc}") from exc

    # BOM / line-ending preservation (Crew)
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

    # Preserve existing file's BOM and line endings (Crew)
    original_ending = None
    had_bom = False
    if path.exists() and path.is_file() and not append:
        try:
            raw = path.read_bytes()
            existing = raw.decode("utf-8", errors="replace")
            existing, had_bom = _strip_bom(existing)
            original_ending = _detect_line_ending(existing)
        except Exception:
            pass

    if original_ending is not None:
        content = _normalize_line_endings(content, original_ending)
    if had_bom and not content.startswith("﻿"):
        content = "﻿" + content

    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8", newline="") as f:
        f.write(content)

    return {
        "success": True,
        "path": str(path),
        "bytes_written": len(content.encode("utf-8")),
        "append": append,
    }


async def handle_file_write(args: dict[str, Any]) -> str:
    path = _resolve_path(str(args.get("path", "")))
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


def register_builtin_tools(registry: Registry) -> None:
    """注册所有内置工具。"""
    registry.register(
        name="terminal",
        toolset="terminal",
        schema=TERMINAL_SCHEMA,
        handler=handle_terminal,
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
        handler=handle_file_read,
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
        handler=handle_file_write,
        is_async=True,
        display_name="写入文件",
        ui_label_template="写入 {path}",
        always_load=True,
        search_hint="write file append create save text",
    )

    from crew.tools.file_tools import register_file_tools
    from crew.tools.interaction import register_interaction_tools
    from crew.tools.memory_tools import register_memory_tools
    from crew.tools.process_registry import register_process_tool
    from crew.tools.skills_tools import register_skills_tools
    from crew.tools.web_tools import register_web_tools

    register_process_tool(registry)
    register_file_tools(registry)
    register_skills_tools(registry)
    register_memory_tools(registry)
    register_web_tools(registry)
    register_interaction_tools(registry)
