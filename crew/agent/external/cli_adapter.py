"""Generic CLI adapters for external agents.

The goal is deliberately small: run a local agent CLI once, collect its text
output, and normalize common JSON-line stream formats into plain assistant text.
Provider-specific protocol depth can be added here later without changing the
Agent executor contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from crew.agent.external.process_lifecycle import (
    finish_process_after_terminal,
    isolated_process_kwargs,
    terminate_process_tree,
)
from crew.agent.external.runtime_adapter import (
    ExternalPermissionRequest,
    ExternalStreamEvent,
    ExternalToolEvent,
    RuntimeAdapterProbe,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    build_external_runtime_home_files,
    build_external_runtime_network_permissions,
    build_external_runtime_env,
    build_managed_external_runtime_env,
    register_runtime_adapter,
)
from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
from crew.state.logging import get_logger


log = get_logger("agent.cli")


class ExternalCliError(RuntimeError):
    pass


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CLI_ERROR_MARKERS = (
    " error",
    "error:",
    "failed",
    "failure",
    "fatal",
    "timeout",
    "timed out",
    "permission denied",
    "operation not permitted",
    "unauthorized",
    "forbidden",
    "not found",
    "rate limit",
    "quota",
    "超时",
    "未授权",
    "拒绝",
)


def _compact_cli_error(
    stderr: str,
    stdout: str,
    *,
    prompt: str,
    returncode: int | None,
) -> str:
    """Extract one actionable CLI failure without echoing the user prompt."""

    prompt_text = " ".join(prompt.split())
    candidates: list[str] = []
    for raw_line in f"{stderr}\n{stdout}".splitlines():
        line = " ".join(_ANSI_RE.sub("", raw_line).strip().split())
        if not line:
            continue
        if prompt_text and line in prompt_text:
            continue
        lower = f" {line.lower()}"
        if any(marker in lower for marker in _CLI_ERROR_MARKERS):
            candidates.append(line)
    if candidates:
        return candidates[-1][:500]
    return f"进程退出码 {returncode if returncode is not None else 'unknown'}，详细原因已写入服务日志"


def _diagnostic_tail(value: str, *, max_chars: int = 16000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"...<truncated>\n{text[-max_chars:]}"


@dataclass
class ExternalCliConfig:
    provider: str
    executable_path: str
    prompt: str
    model: str = ""
    cwd: str = "."
    system_prompt: str = ""
    custom_args: list[str] = field(default_factory=list)
    custom_env: dict[str, str] = field(default_factory=dict)
    credential_home_paths: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    timeout: float = 120.0


@dataclass(frozen=True)
class CliRuntimeProbeResult:
    models: list[RuntimeModelProfile]
    default_model_id: str
    capabilities: RuntimeCapabilities


class ClaudeResumeRejected(RuntimeResumeRejected, ExternalCliError):
    """Claude rejected a resume pointer before starting the requested turn."""


_CLAUDE_MANAGED_VALUE_ARGS = {
    "--output-format",
    "--input-format",
    "--model",
    "--append-system-prompt",
    "--resume",
    "--mcp-config",
    "--permission-mode",
}
_CLAUDE_MANAGED_FLAGS = {
    "-p",
    "--include-partial-messages",
    "--verbose",
    "--dangerously-skip-permissions",
}


def _filtered_claude_custom_args(values: list[str]) -> list[str]:
    """Keep user argv additive without allowing protocol/MCP overrides."""

    result: list[str] = []
    skip_value = False
    for raw in values:
        value = str(raw)
        if skip_value:
            skip_value = False
            continue
        key = value.split("=", 1)[0]
        if key in _CLAUDE_MANAGED_VALUE_ARGS:
            skip_value = "=" not in value
            continue
        if key in _CLAUDE_MANAGED_FLAGS:
            continue
        result.append(value)
    return result


def _write_claude_mcp_config(request: RuntimeExecutionRequest) -> str:
    if not request.mcp_servers:
        return ""
    payload = {
        "mcpServers": {
            server.name: {
                key: value
                for key, value in server.stdio_config().items()
                if key != "name"
            }
            for server in request.mcp_servers
        }
    }
    fd, path = tempfile.mkstemp(prefix="crew-claude-mcp-", suffix=".json")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def default_cli_args(provider: str, prompt: str, model: str = "") -> tuple[list[str], str | None]:
    """Return default argv and optional stdin for a one-shot provider call."""
    provider = provider.strip().lower()
    if provider == "codex":
        args = ["exec", "--skip-git-repo-check"]
        if model:
            args.extend(["--model", model])
        args.append(prompt)
        return args, None
    if provider == "claude":
        args = ["-p", prompt, "--output-format", "stream-json"]
        if model:
            args.extend(["--model", model])
        return args, None
    args = []
    if model:
        args.extend(["--model", model])
    return args, prompt


def _parse_codex_models(raw: str) -> tuple[list[RuntimeModelProfile], str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], ""
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return [], ""
    models: list[RuntimeModelProfile] = []
    default_model_id = str(payload.get("default_model") or payload.get("defaultModel") or "").strip()
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("slug") or entry.get("id") or entry.get("model") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        levels = entry.get("supported_reasoning_levels") or entry.get("supportedReasoningLevels") or []
        thinking_levels = tuple(
            str(level.get("effort") if isinstance(level, dict) else level).strip()
            for level in levels
            if str(level.get("effort") if isinstance(level, dict) else level).strip()
        ) if isinstance(levels, list) else ()
        is_default = bool(entry.get("default") or entry.get("is_default")) or model_id == default_model_id
        if is_default and not default_model_id:
            default_model_id = model_id
        models.append(RuntimeModelProfile(
            id=model_id,
            label=str(entry.get("display_name") or entry.get("displayName") or entry.get("name") or model_id).strip(),
            provider="openai",
            default=is_default,
            capabilities=("text", "tools"),
            thinking_levels=thinking_levels,
        ))
    if not default_model_id and models:
        default_model_id = models[0].id
        first = models[0]
        models[0] = RuntimeModelProfile(
            id=first.id,
            label=first.label,
            provider=first.provider,
            default=True,
            capabilities=first.capabilities,
            thinking_levels=first.thinking_levels,
        )
    return models, default_model_id


def _parse_kimi_models(raw: str, summary: str = "") -> tuple[list[RuntimeModelProfile], str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], ""
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return [], ""
    default_model_id = str(payload.get("default_model") or payload.get("defaultModel") or "").strip()
    if not default_model_id:
        for line in summary.splitlines():
            if line.strip().lower().startswith("default model:"):
                default_model_id = line.split(":", 1)[1].strip()
                break
    models: list[RuntimeModelProfile] = []
    for model_id, raw_model in entries.items():
        if not isinstance(raw_model, dict) or not str(model_id).strip():
            continue
        model_key = str(model_id).strip()
        capabilities = raw_model.get("capabilities") or []
        efforts = raw_model.get("supportEfforts") or raw_model.get("support_efforts") or []
        models.append(RuntimeModelProfile(
            id=model_key,
            label=str(raw_model.get("displayName") or raw_model.get("display_name") or model_key).strip(),
            provider=model_key.split("/", 1)[0] if "/" in model_key else "kimi",
            default=model_key == default_model_id,
            capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
            if isinstance(capabilities, list) else (),
            thinking_levels=tuple(str(item).strip() for item in efforts if str(item).strip())
            if isinstance(efforts, list) else (),
        ))
    if models and default_model_id not in {model.id for model in models}:
        default_model_id = models[0].id
        first = models[0]
        models[0] = RuntimeModelProfile(
            id=first.id,
            label=first.label,
            provider=first.provider,
            default=True,
            capabilities=first.capabilities,
            thinking_levels=first.thinking_levels,
        )
    return models, default_model_id


async def _run_probe_command(
    executable_path: str,
    args: list[str],
    *,
    provider: str,
    env: dict[str, str],
    timeout: float,
) -> str:
    proc = await asyncio.create_subprocess_exec(
        executable_path,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **isolated_process_kwargs(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await terminate_process_tree(proc)
        raise
    except asyncio.TimeoutError as exc:
        await terminate_process_tree(proc)
        raise ExternalCliError(f"{provider} 模型探测超时") from exc
    if proc.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        detail = " ".join(detail.split())[:240]
        raise ExternalCliError(f"{provider} 模型探测失败: {detail or f'exit={proc.returncode}'}")
    return stdout.decode("utf-8", errors="replace")


async def probe_kimi_model_catalog(
    executable_path: str,
    *,
    custom_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[list[RuntimeModelProfile], str]:
    """Read Kimi's local model catalog when ACP omits session model state."""

    env = build_external_runtime_env(custom_env)
    raw, summary = await asyncio.gather(
        _run_probe_command(
            executable_path,
            ["provider", "list", "--json"],
            provider="kimi",
            env=env,
            timeout=timeout,
        ),
        _run_probe_command(
            executable_path,
            ["provider", "list"],
            provider="kimi",
            env=env,
            timeout=timeout,
        ),
    )
    return _parse_kimi_models(raw, summary)


async def probe_cli_runtime(
    executable_path: str,
    *,
    provider: str,
    custom_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> CliRuntimeProbeResult:
    """Discover a CLI runtime model catalog using its registered strategy."""

    provider_key = str(provider or "").strip().lower()
    if provider_key != "codex":
        raise ExternalCliError(f"{provider or 'unknown'} CLI 尚未配置模型探测策略")
    env = build_external_runtime_env(custom_env)
    raw = await _run_probe_command(
        executable_path,
        ["debug", "models", "--bundled"],
        provider=provider_key,
        env=env,
        timeout=timeout,
    )
    models, current = _parse_codex_models(raw)
    return CliRuntimeProbeResult(
        models=models,
        default_model_id=current,
        capabilities=RuntimeCapabilities(
            session_resume=False,
            model_switch=bool(models),
            mcp_servers=False,
            images=True,
            tool_events=True,
        ),
    )


def _claude_usage(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        raw = payload.get("modelUsage")
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "cache_read_input_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
        "cache_creation_input_tokens": (
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
        ),
    }
    usage: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[target] = int(value)
                break
    if usage:
        usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return usage


def _claude_message_events(
    payload: dict[str, Any],
    *,
    tool_names: dict[str, str],
    allow_text: bool,
) -> list[ExternalStreamEvent]:
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[ExternalStreamEvent] = []
    is_user = str(message.get("role") or "").lower() == "user" if isinstance(message, dict) else False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        if block_type == "text" and allow_text:
            text = str(block.get("text") or "")
            if text:
                events.append(ExternalStreamEvent(kind="text", text=text))
        elif block_type == "thinking":
            text = str(block.get("thinking") or block.get("text") or "")
            if text:
                events.append(ExternalStreamEvent(kind="thinking", text=text))
        elif block_type == "tool_use":
            tool_id = str(block.get("id") or "").strip()
            name = str(block.get("name") or "tool").strip() or "tool"
            if tool_id:
                tool_names[tool_id] = name
            raw_input = block.get("input")
            args = json.dumps(raw_input, ensure_ascii=False) if raw_input is not None else ""
            events.append(ExternalStreamEvent(
                kind="tool",
                tool=ExternalToolEvent(
                    name=name,
                    phase="start",
                    tool_call_id=tool_id,
                    args=args,
                ),
            ))
        elif block_type == "tool_result" or (is_user and block.get("tool_use_id")):
            tool_id = str(block.get("tool_use_id") or block.get("toolUseId") or "").strip()
            detail = _extract_text(block.get("content"))
            is_error = bool(block.get("is_error") or block.get("isError"))
            events.append(ExternalStreamEvent(
                kind="tool",
                tool=ExternalToolEvent(
                    name=tool_names.get(tool_id, "tool"),
                    phase="error" if is_error else "result",
                    detail=detail,
                    tool_call_id=tool_id,
                ),
            ))
    return events


def _claude_stream_event(payload: dict[str, Any]) -> ExternalStreamEvent | None:
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    delta_type = str(delta.get("type") or "").lower()
    if delta_type == "text_delta":
        text = str(delta.get("text") or "")
        return ExternalStreamEvent(kind="text", text=text) if text else None
    if delta_type in {"thinking_delta", "signature_delta"}:
        text = str(delta.get("thinking") or delta.get("text") or "")
        return ExternalStreamEvent(kind="thinking", text=text) if text else None
    return None


def _claude_control_response(request_id: str, allow: bool, tool_input: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "behavior": "allow" if allow else "deny",
    }
    if allow and isinstance(tool_input, dict):
        response["updatedInput"] = tool_input
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": response,
        },
    }


async def _stream_claude_once(
    request: RuntimeExecutionRequest,
) -> AsyncIterator[ExternalStreamEvent]:
    from crew.security.launch import host_stream_launch_block_reason

    blocked = host_stream_launch_block_reason()
    if blocked:
        raise ExternalCliError(f"严格安全约束已拒绝 Claude Code 宿主流式启动：{blocked}")
    cwd = str(Path(request.cwd or ".").expanduser().resolve())
    env = build_external_runtime_env(request.custom_env)
    args = [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if request.model and request.model != "default":
        args.extend(["--model", request.model])
    if request.system_prompt:
        args.extend(["--append-system-prompt", request.system_prompt])
    if request.resume_session_id:
        args.extend(["--resume", request.resume_session_id])
    mcp_config_path = _write_claude_mcp_config(request)
    if mcp_config_path:
        args.extend(["--mcp-config", mcp_config_path])
    args.extend(_filtered_claude_custom_args(request.custom_args))
    try:
        proc = await asyncio.create_subprocess_exec(
            request.executable_path,
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            **isolated_process_kwargs(),
        )
    except OSError as exc:
        if mcp_config_path:
            try:
                os.remove(mcp_config_path)
            except OSError:
                pass
        if isinstance(exc, FileNotFoundError):
            raise ExternalCliError(f"找不到可执行文件: {request.executable_path}") from exc
        raise ExternalCliError(f"Claude Code 启动失败: {exc}") from exc

    stderr_parts: list[bytes] = []

    async def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(64 * 1024)
            if not chunk:
                return
            stderr_parts.append(chunk)
            if sum(map(len, stderr_parts)) > 64 * 1024:
                stderr_parts[:] = [b"".join(stderr_parts)[-64 * 1024:]]

    stderr_task = asyncio.create_task(_drain_stderr())
    emitted_output = False
    saw_partial_text = False
    session_emitted = False
    pending_reset_session_id = ""
    terminal_received = False
    tool_names: dict[str, str] = {}
    initial = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": request.prompt}],
        },
    }
    if proc.stdin is None or proc.stdout is None:
        await terminate_process_tree(proc)
        raise ExternalCliError("Claude CLI 未建立 stdin/stdout 管道")

    async def _write_initial_prompt() -> None:
        proc.stdin.write((json.dumps(initial, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    # Multica-compatible ordering: start the writer as a task and immediately
    # drain stdout. Some Claude builds emit startup JSON before reading stdin.
    writer_task = asyncio.create_task(_write_initial_prompt())

    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=request.timeout)
            except asyncio.TimeoutError as exc:
                raise ExternalCliError("Claude Code 模型响应空闲超时") from exc
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("type") or "").lower()
            session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
            if session_id and not session_emitted:
                if request.resume_session_id and session_id != request.resume_session_id:
                    pending_reset_session_id = session_id
                else:
                    session_emitted = True
                    yield ExternalStreamEvent(
                        kind="session",
                        session_id=session_id,
                        session_resumed=bool(request.resume_session_id),
                    )
            if event_type == "stream_event":
                event = _claude_stream_event(payload)
                if event is not None:
                    if event.kind == "text":
                        saw_partial_text = True
                    emitted_output = True
                    yield event
            elif event_type in {"assistant", "user"}:
                for event in _claude_message_events(
                    payload,
                    tool_names=tool_names,
                    allow_text=not saw_partial_text,
                ):
                    emitted_output = True
                    yield event
            elif event_type == "control_request":
                raw_request = payload.get("request")
                raw_request = raw_request if isinstance(raw_request, dict) else {}
                request_id = str(payload.get("request_id") or raw_request.get("request_id") or "").strip()
                tool_name = str(raw_request.get("tool_name") or raw_request.get("toolName") or "tool")
                tool_input = raw_request.get("input")
                permission = ExternalPermissionRequest(
                    request_id=request_id,
                    session_id=session_id,
                    tool_call={
                        "toolCallId": request_id,
                        "title": tool_name,
                        "rawInput": {
                            "name": tool_name,
                            "arguments": tool_input if isinstance(tool_input, dict) else {},
                        },
                    },
                    raw_params=payload,
                )
                decision = (
                    await request.permission_handler(permission)
                    if request.permission_handler is not None
                    else "deny"
                )
                response = _claude_control_response(request_id, decision == "allow", tool_input)
                await writer_task
                proc.stdin.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            elif event_type == "result":
                terminal_received = True
                usage = _claude_usage(payload)
                if usage:
                    yield ExternalStreamEvent(kind="usage", usage=usage)
                if bool(payload.get("is_error")):
                    detail = str(
                        payload.get("error")
                        or payload.get("result")
                        or payload.get("subtype")
                        or "Claude Code 执行失败"
                    )
                    raise ExternalCliError(detail)
                break
        if terminal_received:
            await writer_task
            await finish_process_after_terminal(proc, stdin=proc.stdin)
        else:
            await proc.wait()
        stderr = b"".join(stderr_parts).decode("utf-8", errors="replace").strip()
        if proc.returncode and not terminal_received:
            compact = _compact_cli_error(
                stderr,
                "",
                prompt=request.prompt,
                returncode=proc.returncode,
            )
            resume_rejected = request.resume_session_id and any(
                marker in stderr.lower()
                for marker in ("no conversation found", "session not found", "invalid session")
            )
            if resume_rejected and not emitted_output:
                raise ClaudeResumeRejected(compact)
            raise ExternalCliError(f"Claude Code 调用失败: {compact}")
        if pending_reset_session_id and not session_emitted:
            yield ExternalStreamEvent(
                kind="session",
                session_id=pending_reset_session_id,
                session_reset=True,
            )
        await writer_task
    except asyncio.CancelledError:
        await terminate_process_tree(proc)
        raise
    finally:
        if terminal_received:
            await finish_process_after_terminal(proc, stdin=proc.stdin)
        else:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            await terminate_process_tree(proc)
        await asyncio.gather(stderr_task, writer_task, return_exceptions=True)
        if mcp_config_path:
            try:
                os.remove(mcp_config_path)
            except OSError:
                pass


async def stream_claude_events(
    request: RuntimeExecutionRequest,
) -> AsyncIterator[ExternalStreamEvent]:
    """Stream one Claude Code turn as protocol-neutral external events."""

    async for event in _stream_claude_once(request):
        yield event


class ClaudeStreamJsonAdapter:
    """Drive Claude Code through its native stdin/stdout stream-json protocol."""

    adapter_id = "claude-stream-json"

    async def probe(
        self,
        executable_path: str,
        *,
        provider: str,
        launch_args: tuple[str, ...] = (),
        custom_env: dict[str, str] | None = None,
    ) -> RuntimeAdapterProbe:
        """Return the stable Claude Code model catalog and supported capabilities."""

        del executable_path, provider, launch_args, custom_env
        # Claude Code has no documented ``models list`` command.  Keep the
        # short, versioned catalog here (the same strategy used by Multica)
        # so RuntimeProfile still exposes concrete, selectable model ids.
        # The ``sonnet``/``opus`` ids are official Claude Code aliases and
        # remain useful when an installed CLI/account lags a pinned release.
        models = [
            RuntimeModelProfile(
                id=model_id,
                label=label,
                provider="anthropic",
                default=is_default,
                capabilities=("text", "tools"),
            )
            for model_id, label, is_default in (
                ("sonnet", "Claude Sonnet（当前）", True),
                ("opus", "Claude Opus（当前）", False),
                ("claude-sonnet-5", "Claude Sonnet 5", False),
                ("claude-opus-5", "Claude Opus 5", False),
                ("claude-opus-4-8", "Claude Opus 4.8", False),
                ("claude-sonnet-4-6", "Claude Sonnet 4.6", False),
                ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", False),
                ("claude-sonnet-4-5", "Claude Sonnet 4.5", False),
            )
        ]
        return RuntimeAdapterProbe(
            models=models,
            default_model_id="sonnet",
            capabilities=RuntimeCapabilities(
                session_resume=True,
                model_switch=True,
                mcp_servers=True,
                images=True,
                tool_events=True,
                streaming=True,
                usage=True,
                approval=True,
            ),
            source="claude_static_catalog",
            model_migrations={"default": "sonnet"},
        )

    def stream(self, request: RuntimeExecutionRequest) -> AsyncIterator[ExternalStreamEvent]:
        """Start streaming a Claude Code execution request."""

        return stream_claude_events(request)


register_runtime_adapter(ClaudeStreamJsonAdapter())




async def run_external_cli(config: ExternalCliConfig) -> str:
    """Run one CLI turn through the security execution boundary."""

    started_at = time.perf_counter()
    prompt = config.prompt
    if config.system_prompt:
        prompt = f"{config.system_prompt}\n\n---\n\n{config.prompt}"

    args = list(config.custom_args)
    stdin_text: str | None = None
    if not args:
        args, stdin_text = default_cli_args(config.provider, prompt, config.model)

    cwd = str(Path(config.cwd or ".").expanduser().resolve())
    process_id: int | None = None
    first_io_at: dict[str, float] = {}

    def _mark_started(pid: int | None) -> None:
        nonlocal process_id
        process_id = pid
        log.info(
            "[PERF] cli_spawn provider=%s elapsed=%.3fs model=%s pid=%s",
            config.provider,
            time.perf_counter() - started_at,
            config.model or "default",
            pid,
        )

    def _mark_first_io(stream_name: str) -> None:
        if stream_name in first_io_at:
            return
        first_io_at[stream_name] = time.perf_counter()
        log.info(
            "[PERF] cli_first_%s provider=%s elapsed=%.3fs model=%s pid=%s",
            stream_name,
            config.provider,
            first_io_at[stream_name] - started_at,
            config.model or "default",
            process_id,
        )

    env = build_external_runtime_env(config.custom_env)

    try:
        from crew.security.launch import execute_captured

        projected_home_files = build_external_runtime_home_files(config.credential_home_paths)
        result = await execute_captured(
            (config.executable_path, *args),
            cwd=Path(cwd),
            env=env,
            stdin=stdin_text.encode("utf-8") if stdin_text is not None else None,
            home_files=projected_home_files,
            additional_permissions=build_external_runtime_network_permissions(
                projected_home_files,
                config.network_endpoints,
            ),
            env_overrides=build_managed_external_runtime_env(config.custom_env),
            timeout=config.timeout,
            on_started=_mark_started,
            on_output=_mark_first_io,
        )
    except FileNotFoundError as exc:
        raise ExternalCliError(f"找不到可执行文件: {config.executable_path}") from exc
    except (asyncio.TimeoutError, NativeRuntimeError) as exc:
        if isinstance(exc, NativeRuntimeError) and exc.code is not RuntimeErrorCode.TIMEOUT:
            raise
        log.error(
            "[PERF] cli_total provider=%s elapsed=%.3fs model=%s status=timeout",
            config.provider,
            time.perf_counter() - started_at,
            config.model or "default",
        )
        raise ExternalCliError(f"{config.provider} CLI 调用超时") from exc

    out_text = result.stdout
    err_text = result.stderr.strip()
    if result.returncode != 0:
        log.error(
            "[CLI] provider=%s exit=%s stderr=%s stdout=%s",
            config.provider,
            result.returncode,
            _diagnostic_tail(err_text),
            _diagnostic_tail(out_text),
        )
        log.info(
            "[PERF] cli_total provider=%s elapsed=%.3fs model=%s status=failed",
            config.provider,
            time.perf_counter() - started_at,
            config.model or "default",
        )
        detail = _compact_cli_error(
            err_text,
            out_text,
            prompt=prompt,
            returncode=result.returncode,
        )
        raise ExternalCliError(f"{config.provider} CLI 调用失败: {detail}")

    log.info(
        "[PERF] cli_total provider=%s elapsed=%.3fs model=%s status=completed",
        config.provider,
        time.perf_counter() - started_at,
        config.model or "default",
    )

    text = normalize_cli_output(out_text)
    if text:
        return text
    if err_text:
        return err_text
    return f"{config.provider} 已完成，但没有返回文本输出。"


def normalize_cli_output(raw: str) -> str:
    """Extract assistant text from plain output or JSON/JSONL streams."""
    raw = raw.strip()
    if not raw:
        return ""

    parts: list[str] = []
    all_json = True
    for line in raw.splitlines():
        clean = line.strip()
        if not clean:
            continue
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            all_json = False
            break
        text = _extract_text(payload)
        if text:
            parts.append(text)

    if all_json:
        return "".join(parts).strip()
    return raw


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    event_type = str(value.get("type") or value.get("event") or "").lower()
    if event_type in {"system", "tool_use", "tool_result"}:
        return ""

    for key in ("delta", "text", "content", "message", "result", "output"):
        text = _extract_text(value.get(key))
        if text:
            return text

    choices = value.get("choices")
    if isinstance(choices, list):
        return "".join(_extract_text(choice) for choice in choices)
    return ""
