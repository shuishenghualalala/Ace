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
import platform
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from crew.agent.external.process_lifecycle import (
    ExternalProcessBoundaryError,
    external_runtime_environment,
    finish_process_after_terminal,
    run_trusted_external_probe,
    spawn_authorized_external_process,
    terminate_process_tree,
    validate_external_env_overrides,
)
from crew.agent.external.runtime_adapter import (
    ExternalPermissionRequest,
    ExternalStreamEvent,
    ExternalToolEvent,
    RuntimeAdapterProbe,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    NativeInteractiveLineTransport,
    build_external_runtime_home_files,
    build_external_runtime_network_permissions,
    build_external_runtime_env,
    open_managed_external_interactive,
    register_runtime_adapter,
)
from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
from crew.state.logging import get_logger

log = get_logger("agent.cli")
MAX_EXTERNAL_STREAM_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_EXTERNAL_INPUT_BYTES = 1024 * 1024


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

_PROVIDER_NETWORK_HOSTS = {
    "codex": (
        "api.openai.com",
        "chatgpt.com",
        "chat.openai.com",
        "auth.openai.com",
        "auth.api.openai.org",
    ),
    "claude": ("api.anthropic.com", "claude.ai", "statsig.anthropic.com"),
}

_RUNTIME_CONTROLLED_ENV = frozenset(
    {
        "ALL_PROXY",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_COMMON_MANAGED_ENV_NAMES = frozenset(
    {
        "CURL_CA_BUNDLE",
        "LANG",
        "LANGUAGE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
_PROVIDER_ENV_PREFIXES = {
    "codex": ("AZURE_OPENAI_", "CODEX_", "OPENAI_"),
    "claude": (
        "ANTHROPIC_",
        "AWS_",
        "AZURE_",
        "CLAUDE_",
        "GOOGLE_",
        "VERTEX_",
    ),
}


def _provider_key(provider: str) -> str:
    value = provider.strip().lower()
    if value in {"codex", "openai"}:
        return "codex"
    if value in {"claude", "claude-code", "anthropic"}:
        return "claude"
    return value


def _managed_runtime_env(
    provider: str,
    env: dict[str, str],
    explicit_env: dict[str, str],
) -> dict[str, str]:
    """Pass provider-scoped settings while preserving Native Runtime's OS boundary."""
    explicit_names = {str(name).upper() for name in explicit_env}
    prefixes = _PROVIDER_ENV_PREFIXES.get(_provider_key(provider), ())
    result: dict[str, str] = {}
    for name, value in env.items():
        normalized = str(name).upper()
        if normalized in _RUNTIME_CONTROLLED_ENV:
            continue
        if (
            normalized in explicit_names
            or normalized in _COMMON_MANAGED_ENV_NAMES
            or normalized.startswith("LC_")
            or any(normalized.startswith(prefix) for prefix in prefixes)
        ):
            result[str(name)] = str(value)
    return result


def _codex_native_executable(executable_path: str) -> Path:
    """Resolve the npm launcher to its packaged native binary on every supported OS."""
    source = Path(executable_path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError:
        return source.resolve(strict=False)
    if resolved.suffix.lower() not in {".js", ".cmd", ".bat"}:
        return resolved

    package_roots: list[Path] = []
    if resolved.name.lower() == "codex.js" and resolved.parent.name == "bin":
        package_roots.append(resolved.parent.parent)
    package_roots.extend(
        (
            source.parent / "node_modules" / "@openai" / "codex",
            resolved.parent / "node_modules" / "@openai" / "codex",
        )
    )
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    elif machine in {"amd64", "x64", "x86_64"}:
        architecture = "x64"
    else:
        return resolved
    if system == "darwin":
        package_name = f"codex-darwin-{architecture}"
        target = f"{'aarch64' if architecture == 'arm64' else 'x86_64'}-apple-darwin"
        executable_name = "codex"
    elif system == "linux":
        package_name = f"codex-linux-{architecture}"
        target = f"{'aarch64' if architecture == 'arm64' else 'x86_64'}-unknown-linux-musl"
        executable_name = "codex"
    elif system == "windows":
        package_name = f"codex-win32-{architecture}"
        target = f"{'aarch64' if architecture == 'arm64' else 'x86_64'}-pc-windows-msvc"
        executable_name = "codex.exe"
    else:
        return resolved
    for package_root in dict.fromkeys(package_roots):
        candidates = (
            package_root
            / "node_modules"
            / "@openai"
            / package_name
            / "vendor"
            / target
            / "bin"
            / executable_name,
            package_root / "vendor" / target / "bin" / executable_name,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve(strict=True)
    return resolved


def _claude_native_executable(executable_path: str) -> Path:
    """Resolve Windows npm command shims without weakening the executable boundary."""
    source = Path(executable_path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError:
        return source.resolve(strict=False)
    if resolved.suffix.lower() not in {".bat", ".cmd", ".ps1"}:
        return resolved
    for root in dict.fromkeys((source.parent, resolved.parent)):
        candidate = (
            root
            / "node_modules"
            / "@anthropic-ai"
            / "claude-code"
            / "bin"
            / "claude.exe"
        )
        if candidate.is_file():
            return candidate.resolve(strict=True)
    return resolved


def _managed_external_executable(provider: str, executable_path: str) -> Path:
    provider = _provider_key(provider)
    if provider == "codex":
        return _codex_native_executable(executable_path)
    if provider == "claude":
        return _claude_native_executable(executable_path)
    return Path(executable_path).expanduser().resolve(strict=False)


def _managed_default_args(provider: str, args: list[str]) -> list[str]:
    """Delegate prompts to the outer sandbox and disable nested interactive policy."""
    provider = _provider_key(provider)
    if provider == "codex" and args[:1] == ["exec"]:
        return [
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
            *args[1:],
        ]
    if provider == "claude":
        result = list(args)
        try:
            output_index = result.index("--output-format") + 1
            result[output_index] = "json"
        except (ValueError, IndexError):
            result.extend(("--output-format", "json"))
        result.extend(
            (
                "--permission-mode",
                "bypassPermissions",
                "--safe-mode",
                "--no-session-persistence",
            )
        )
        return result
    return args


async def _authorized_external_launch(
    *,
    provider: str,
    executable_path: str,
    cwd: Path,
    custom_env: dict[str, str],
):
    """Return a launch with an approved, provider-scoped public network overlay."""
    from crew.security.actions import normalize_exec_action
    from crew.security.approvals import ApprovalDecision
    from crew.security.launch import current_process_launch
    from crew.security.models import (
        AdditionalPermissionProfile,
        NetworkEntry,
        SandboxPermissions,
    )
    from crew.security.outbound import parse_public_http_target

    launch = current_process_launch.get()
    if launch is None or not launch.managed:
        return launch
    provider = _provider_key(provider)
    targets = [
        NetworkEntry(host, 443, "https")
        for host in _PROVIDER_NETWORK_HOSTS.get(provider, ())
    ]
    base_url_names = {
        "codex": ("OPENAI_BASE_URL",),
        "claude": ("ANTHROPIC_BASE_URL",),
    }.get(provider, ())
    for name in base_url_names:
        value = str(custom_env.get(name) or "").strip()
        if value:
            target = parse_public_http_target(value)
            targets.append(NetworkEntry(target.host, target.port, target.protocol))
    if not targets:
        return launch
    if launch.security_context is None or launch.approval_service is None:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "external agent network approval context is missing",
        )
    network = tuple(
        dict.fromkeys(
            (
                *launch.additional_permissions.network,
                *targets,
            )
        )
    )
    additional = AdditionalPermissionProfile(
        filesystem=launch.additional_permissions.filesystem,
        network=network,
        allow_local_binding=launch.additional_permissions.allow_local_binding,
        sandbox_permissions=SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS,
    )
    action = normalize_exec_action(
        (str(Path(executable_path).expanduser().resolve(strict=False)), "external-agent", provider),
        cwd,
    )
    service = launch.approval_service
    authorization = service.authorize_exec_action(
        launch.security_context,
        action,
        tool_name="external_agent",
        risk_class="external_agent_network",
        requires_approval=True,
        additional_permissions=additional,
    )
    if not authorization.allowed:
        if authorization.request is None:
            raise ExternalCliError("外部智能体联网已被安全策略拒绝")
        outcome = await service.await_decision(authorization.request["request_id"])
        if outcome is None or outcome.decision is ApprovalDecision.REJECT:
            raise ExternalCliError("用户未批准外部智能体联网")
        authorization = service.authorize_exec_action(
            launch.security_context,
            action,
            tool_name="external_agent",
            risk_class="external_agent_network",
            requires_approval=True,
            additional_permissions=additional,
        )
        if not authorization.allowed:
            raise ExternalCliError("批准后外部智能体联网授权校验失败，请重试")
    return replace(launch, additional_permissions=additional)


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
    return (
        f"进程退出码 {returncode if returncode is not None else 'unknown'}，详细原因已写入服务日志"
    )


def _diagnostic_tail(value: str, *, max_chars: int = 16000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"...<truncated>\n{text[-max_chars:]}"


def _bounded_json_line(payload: dict[str, Any]) -> bytes:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_EXTERNAL_INPUT_BYTES:
        raise ExternalCliError("Claude Code 协议输入超过大小上限")
    return data


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
                key: value for key, value in server.stdio_config().items() if key != "name"
            }
            for server in request.mcp_servers
        }
    }
    fd, path = tempfile.mkstemp(
        prefix=".crew-claude-mcp-",
        suffix=".json",
        dir=str(Path(request.cwd or ".").expanduser().resolve(strict=True)),
    )
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


class _SubprocessClaudeLineTransport:
    """Line transport for the legacy direct path used when external security is off."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.process = proc

    async def read_line(self) -> bytes:
        if self.process.stdout is None:
            return b""
        return await self.process.stdout.readline()

    async def write(self, data: bytes) -> None:
        if self.process.stdin is None:
            raise ExternalCliError("Claude CLI stdin 不可用")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()

    async def abort(self) -> None:
        await terminate_process_tree(self.process)


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
    default_model_id = str(
        payload.get("default_model") or payload.get("defaultModel") or ""
    ).strip()
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("slug") or entry.get("id") or entry.get("model") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        levels = (
            entry.get("supported_reasoning_levels") or entry.get("supportedReasoningLevels") or []
        )
        thinking_levels = (
            tuple(
                str(level.get("effort") if isinstance(level, dict) else level).strip()
                for level in levels
                if str(level.get("effort") if isinstance(level, dict) else level).strip()
            )
            if isinstance(levels, list)
            else ()
        )
        is_default = (
            bool(entry.get("default") or entry.get("is_default")) or model_id == default_model_id
        )
        if is_default and not default_model_id:
            default_model_id = model_id
        models.append(
            RuntimeModelProfile(
                id=model_id,
                label=str(
                    entry.get("display_name")
                    or entry.get("displayName")
                    or entry.get("name")
                    or model_id
                ).strip(),
                provider="openai",
                default=is_default,
                capabilities=("text", "tools"),
                thinking_levels=thinking_levels,
            )
        )
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
    default_model_id = str(
        payload.get("default_model") or payload.get("defaultModel") or ""
    ).strip()
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
        models.append(
            RuntimeModelProfile(
                id=model_key,
                label=str(
                    raw_model.get("displayName") or raw_model.get("display_name") or model_key
                ).strip(),
                provider=model_key.split("/", 1)[0] if "/" in model_key else "kimi",
                default=model_key == default_model_id,
                capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
                if isinstance(capabilities, list)
                else (),
                thinking_levels=tuple(str(item).strip() for item in efforts if str(item).strip())
                if isinstance(efforts, list)
                else (),
            )
        )
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
    custom_env: dict[str, str] | None,
    timeout: float,
) -> str:
    result = await run_trusted_external_probe(
        executable_path,
        *args,
        custom_env=custom_env,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        detail = " ".join(detail.split())[:240]
        raise ExternalCliError(f"{provider} 模型探测失败: {detail or f'exit={result.returncode}'}")
    return result.stdout.decode("utf-8", errors="replace")


async def probe_kimi_model_catalog(
    executable_path: str,
    *,
    custom_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[list[RuntimeModelProfile], str]:
    """Read Kimi's local model catalog when ACP omits session model state."""

    raw, summary = await asyncio.gather(
        _run_probe_command(
            executable_path,
            ["provider", "list", "--json"],
            provider="kimi",
            custom_env=custom_env,
            timeout=timeout,
        ),
        _run_probe_command(
            executable_path,
            ["provider", "list"],
            provider="kimi",
            custom_env=custom_env,
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
    raw = await _run_probe_command(
        executable_path,
        ["debug", "models", "--bundled"],
        provider=provider_key,
        custom_env=custom_env,
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
    is_user = (
        str(message.get("role") or "").lower() == "user" if isinstance(message, dict) else False
    )
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
            events.append(
                ExternalStreamEvent(
                    kind="tool",
                    tool=ExternalToolEvent(
                        name=name,
                        phase="start",
                        tool_call_id=tool_id,
                        args=args,
                    ),
                )
            )
        elif block_type == "tool_result" or (is_user and block.get("tool_use_id")):
            tool_id = str(block.get("tool_use_id") or block.get("toolUseId") or "").strip()
            detail = _extract_text(block.get("content"))
            is_error = bool(block.get("is_error") or block.get("isError"))
            events.append(
                ExternalStreamEvent(
                    kind="tool",
                    tool=ExternalToolEvent(
                        name=tool_names.get(tool_id, "tool"),
                        phase="error" if is_error else "result",
                        detail=detail,
                        tool_call_id=tool_id,
                    ),
                )
            )
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
    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is None:
        raise ExternalCliError("Claude Code 缺少安全启动上下文")
    cwd = str(Path(request.cwd or ".").expanduser().resolve())
    try:
        custom_env = validate_external_env_overrides(request.custom_env)
    except ExternalProcessBoundaryError as exc:
        raise ExternalCliError(f"Claude Code 环境配置已拒绝: {exc}") from exc
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
    command = (request.executable_path, *args)
    native_session = None
    if launch.external_managed:
        try:
            native_session = await open_managed_external_interactive(request, command)
        except Exception as exc:  # noqa: BLE001 - preserve adapter-level diagnostics
            raise ExternalCliError(f"Claude Code managed runtime 启动失败: {exc}") from exc
        if native_session is None:
            raise ExternalCliError("Claude Code managed runtime 未建立 interactive session")
        proc = native_session.process
        transport = NativeInteractiveLineTransport(
            native_session,
            max_line_bytes=64 * 1024 * 1024,
        )
    else:
        try:
            proc = await spawn_authorized_external_process(
                request.executable_path,
                *args,
                cwd=cwd,
                custom_env=custom_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=64 * 1024 * 1024,
            )
        except (OSError, ExternalProcessBoundaryError) as exc:
            if mcp_config_path:
                try:
                    os.remove(mcp_config_path)
                except OSError:
                    pass
            if isinstance(exc, FileNotFoundError):
                raise ExternalCliError(f"找不到可执行文件: {request.executable_path}") from exc
            raise ExternalCliError(f"Claude Code 启动失败: {exc}") from exc
        transport = _SubprocessClaudeLineTransport(proc)

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
                stderr_parts[:] = [b"".join(stderr_parts)[-64 * 1024 :]]

    stderr_task = None if native_session is not None else asyncio.create_task(_drain_stderr())
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
    async def _write_initial_prompt() -> None:
        await transport.write(_bounded_json_line(initial))

    # Multica-compatible ordering: start the writer as a task and immediately
    # drain stdout. Some Claude builds emit startup JSON before reading stdin.
    writer_task = asyncio.create_task(_write_initial_prompt())
    loop = asyncio.get_running_loop()
    hard_deadline = loop.time() + max(request.timeout * 4, request.timeout + 900.0)
    stdout_bytes = 0

    try:
        while True:
            hard_remaining = hard_deadline - loop.time()
            if hard_remaining <= 0:
                raise ExternalCliError("Claude Code 调用总时长超时")
            try:
                line = await asyncio.wait_for(
                    transport.read_line(),
                    timeout=min(request.timeout, hard_remaining),
                )
            except asyncio.TimeoutError as exc:
                message = (
                    "Claude Code 调用总时长超时"
                    if loop.time() >= hard_deadline
                    else "Claude Code 模型响应空闲超时"
                )
                raise ExternalCliError(message) from exc
            if not line:
                break
            stdout_bytes += len(line)
            if stdout_bytes > MAX_EXTERNAL_STREAM_OUTPUT_BYTES:
                raise ExternalCliError("Claude Code 输出超过大小上限")
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
                request_id = str(
                    payload.get("request_id") or raw_request.get("request_id") or ""
                ).strip()
                tool_name = str(
                    raw_request.get("tool_name") or raw_request.get("toolName") or "tool"
                )
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
                await transport.write(_bounded_json_line(response))
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
        else:
            remaining = hard_deadline - loop.time()
            if remaining <= 0:
                raise ExternalCliError("Claude Code 调用总时长超时")
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=min(request.timeout, remaining),
                )
            except asyncio.TimeoutError as exc:
                raise ExternalCliError("Claude Code 进程退出超时") from exc
        native_stderr = getattr(transport, "stderr_lines", ())
        stderr = (
            "\n".join(str(line) for line in native_stderr)
            if native_stderr
            else b"".join(stderr_parts).decode("utf-8", errors="replace")
        ).strip()
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
        await transport.abort()
        raise
    finally:
        if terminal_received:
            await transport.close()
            await finish_process_after_terminal(proc)
        else:
            await transport.abort()
        tasks: list[asyncio.Task[Any]] = [writer_task]
        if stderr_task is not None:
            tasks.append(stderr_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        if mcp_config_path:
            try:
                os.remove(mcp_config_path)
            except OSError:
                pass


async def stream_claude_events(
    request: RuntimeExecutionRequest,
) -> AsyncIterator[ExternalStreamEvent]:
    """Stream one Claude Code turn as protocol-neutral external events."""

    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is not None and launch.managed:
        output = await run_external_cli(
            ExternalCliConfig(
                provider="claude",
                executable_path=request.executable_path,
                prompt=request.prompt,
                model=request.model if request.model != "default" else "",
                cwd=request.cwd,
                system_prompt=request.system_prompt,
                custom_env=request.custom_env,
                timeout=request.timeout,
            )
        )
        if output:
            yield ExternalStreamEvent(kind="text", text=output)
        return

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
    using_default_args = not args
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
    # 宿主路径快照 fail-closed 拒绝未声明的凭据环境：基础 env 必须走
    # 白名单净化（external_runtime_environment），owner 的 provider 凭据只
    # 允许经 managed native runtime 通道（env_overrides/runtime_env）下发。
    env = external_runtime_environment(config.custom_env)
    try:
        from crew.security.launch import (
            current_process_launch,
            execute_captured,
            use_process_launch,
        )

        projected_home_files = build_external_runtime_home_files(config.credential_home_paths)
        additional_permissions = build_external_runtime_network_permissions(
            projected_home_files,
            config.network_endpoints,
        )

        launch = current_process_launch.get()
        managed = launch is not None and launch.managed
        executable_path = (
            _managed_external_executable(config.provider, config.executable_path)
            if managed
            else Path(config.executable_path)
        )
        if managed and using_default_args:
            args = _managed_default_args(config.provider, args)
        effective_launch = await _authorized_external_launch(
            provider=config.provider,
            executable_path=str(executable_path),
            cwd=Path(cwd),
            custom_env=env,
        )
        runtime_env = (
            _managed_runtime_env(config.provider, env, config.custom_env)
            if managed
            else config.custom_env
        )
        argv_size = sum(len(str(part).encode("utf-8")) for part in (str(executable_path), *args))
        stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        if argv_size > MAX_EXTERNAL_INPUT_BYTES or (
            stdin_bytes is not None and len(stdin_bytes) > MAX_EXTERNAL_INPUT_BYTES
        ):
            raise ExternalCliError(f"{config.provider} CLI 输入超过大小上限")

        with use_process_launch(effective_launch):
            result = await execute_captured(
                (str(executable_path), *args),
                cwd=Path(cwd),
                env=env,
                stdin=stdin_text.encode("utf-8") if stdin_text is not None else None,
                home_files=projected_home_files,
                additional_permissions=additional_permissions,
                env_overrides=runtime_env,
                timeout=config.timeout,
                on_started=_mark_started,
                on_output=_mark_first_io,
                tool_name="external_agent_cli",
                external=not managed,
            )
    except FileNotFoundError as exc:
        raise ExternalCliError(f"找不到可执行文件: {config.executable_path}") from exc
    except ExternalProcessBoundaryError as exc:
        raise ExternalCliError(f"{config.provider} CLI 环境配置已拒绝: {exc}") from exc
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
