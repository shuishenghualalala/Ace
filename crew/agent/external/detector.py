"""Detect locally installed external agent runtimes.

Descriptors come from the built-in compatibility catalog. Detection never
installs or downloads a runtime: every candidate command must already resolve
on the local host.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crew.agent.external import codex_adapter as _codex_adapter  # noqa: F401
from crew.agent.external.acp_adapter import AcpAdapterError
from crew.agent.external.cli_adapter import ExternalCliError
from crew.agent.external.runtime_adapter import get_runtime_adapter
from crew.agent.external.runtime_profile import (
    ProbeResult,
    RuntimeCapabilities,
    RuntimeProfile,
)
from crew.agent.external.runtime_registry import (
    BUILTIN_RUNTIME_DESCRIPTORS,
    RuntimeDescriptor,
    builtin_descriptor,
    runtime_descriptors,
)


@dataclass
class RuntimeCandidate:
    id: str
    provider: str
    name: str
    executable_path: str
    version: str
    protocol: str = "acp"
    adapter_id: str = ""
    launch_args: tuple[str, ...] = ()
    probe_env: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "executable_path": self.executable_path,
            "version": self.version,
            "protocol": self.protocol,
            "metadata": {
                **(self.metadata or {}),
                "launch_args": list(self.launch_args),
                "adapter_id": self.adapter_id,
                "credential_home_paths": list(
                    (self.metadata or {}).get("credential_home_paths", ())
                ),
            },
        }


# Backward-compatible public name for code/tests that imported the old tuple.
RUNTIME_PROBES = BUILTIN_RUNTIME_DESCRIPTORS


def _runtime_id(provider: str, path: str) -> str:
    digest = hashlib.sha1(f"{provider}:{path}".encode("utf-8")).hexdigest()[:16]
    return f"{provider}_{digest}"


def codex_desktop_app_bundle_paths() -> list[str]:
    bundle_names = ("ChatGPT.app", "Codex.app")
    paths = [
        f"/Applications/{bundle}/Contents/Resources/codex"
        for bundle in bundle_names
    ]
    home = Path.home()
    if home:
        paths.extend(
            str(home / "Applications" / bundle / "Contents" / "Resources" / "codex")
            for bundle in bundle_names
        )
    return paths


def _platform_search_dirs(*, platform_name: str | None = None) -> tuple[str, ...]:
    home = Path.home()
    if (platform_name or os.name) == "nt":
        candidates = (
            os.getenv("APPDATA", ""),
            os.getenv("PNPM_HOME", ""),
            str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs") if os.getenv("LOCALAPPDATA") else "",
            str(Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps")
            if os.getenv("LOCALAPPDATA") else "",
            str(home / ".local" / "bin"),
            str(home / ".cargo" / "bin"),
            str(home / ".bun" / "bin"),
            str(home / ".deno" / "bin"),
            str(home / "scoop" / "shims"),
        )
    else:
        candidates = (
            os.getenv("PNPM_HOME", ""),
            str(home / ".local" / "bin"),
            str(home / ".local" / "share" / "pnpm"),
            str(home / "bin"),
            str(home / ".cargo" / "bin"),
            str(home / ".bun" / "bin"),
            str(home / ".deno" / "bin"),
            str(home / ".npm-global" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
        )
    return tuple(dict.fromkeys(path for path in candidates if path))


_SHELL_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SUPPORTED_LOGIN_SHELLS = {"bash", "zsh", "sh", "dash", "ksh"}


def _usable_executable(
    path: str | None,
    *,
    platform_name: str | None = None,
) -> str | None:
    if not path:
        return None
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        return None
    if (platform_name or os.name) != "nt" and not os.access(resolved, os.X_OK):
        return None
    return resolved


def _login_shell_executables(commands: set[str]) -> dict[str, str]:
    """Resolve missing command names once through a trusted POSIX login shell."""

    if os.name == "nt":
        return {}
    safe_commands = sorted({
        command for command in commands
        if _SHELL_COMMAND.fullmatch(command)
    })[:256]
    if not safe_commands:
        return {}
    configured_shell = os.getenv("SHELL", "").strip()
    if Path(configured_shell).name not in _SUPPORTED_LOGIN_SHELLS:
        return {}
    shell = _usable_executable(configured_shell)
    if shell is None:
        return {}
    script = (
        'for name do '
        'unalias "$name" 2>/dev/null; unset -f "$name" 2>/dev/null; '
        'resolved=$(command -v "$name" 2>/dev/null) || continue; '
        'case "$resolved" in /*) ;; *) continue ;; esac; '
        'dir=$(dirname "$resolved") && file=$(basename "$resolved") && '
        'canonical=$(cd "$dir" 2>/dev/null && pwd -P) || continue; '
        'printf "%s\\t%s/%s\\n" "$name" "$canonical" "$file"; '
        'done'
    )
    try:
        proc = subprocess.run(
            [shell, "-ilc", script, "crew-runtime-scan", *safe_commands],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        command, separator, path = line.partition("\t")
        usable = _usable_executable(path) if os.path.isabs(path) else None
        if separator and command in safe_commands and usable:
            result[command] = usable
    return result


def _resolve_executable(
    env_var: str,
    commands: tuple[str, ...],
    *,
    login_shell_paths: dict[str, str] | None = None,
) -> str | None:
    path, _source = _resolve_executable_with_source(
        env_var,
        commands,
        login_shell_paths=login_shell_paths,
    )
    return path


def _resolve_executable_with_source(
    env_var: str,
    commands: tuple[str, ...],
    *,
    login_shell_paths: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    configured = os.getenv(env_var, "").strip()
    if configured:
        path = _usable_executable(configured) or _usable_executable(shutil.which(configured))
        return (path, "environment") if path else (None, "environment_invalid")

    for command in commands:
        if os.path.isabs(command):
            path = _usable_executable(command)
            if path:
                return path, "descriptor_path"
            continue
        path = _usable_executable(shutil.which(command))
        if path:
            return path, "path"

    search_path = os.pathsep.join(_platform_search_dirs())
    for command in commands:
        if os.path.isabs(command):
            continue
        path = _usable_executable(shutil.which(command, path=search_path))
        if path:
            return path, "platform_user_dir"

    if login_shell_paths:
        for command in commands:
            path = _usable_executable(login_shell_paths.get(command))
            if path:
                return path, "login_shell"

    if "codex" in commands:
        for bundle_path in codex_desktop_app_bundle_paths():
            path = _usable_executable(bundle_path)
            if path:
                return path, "app_bundle"
    return None, "not_found"


def _scan_descriptors(descriptors: tuple[RuntimeDescriptor, ...]) -> list[RuntimeCandidate]:
    unresolved_commands: set[str] = set()
    initially_resolved: dict[int, tuple[str, str]] = {}
    for index, descriptor in enumerate(descriptors):
        path, source = _resolve_executable_with_source(descriptor.env_var, descriptor.commands)
        if path:
            initially_resolved[index] = (path, source)
            continue
        if descriptor.env_var and os.getenv(descriptor.env_var, "").strip():
            continue
        unresolved_commands.update(
            command for command in descriptor.commands
            if not os.path.isabs(command)
        )
    login_paths = _login_shell_executables(unresolved_commands)

    candidates: list[RuntimeCandidate] = []
    seen_paths: set[tuple[str, str]] = set()
    for index, descriptor in enumerate(descriptors):
        resolved = initially_resolved.get(index)
        if resolved is None:
            resolved = _resolve_executable_with_source(
                descriptor.env_var,
                descriptor.commands,
                login_shell_paths=login_paths,
            )
        path, source = resolved
        if not path:
            continue
        normalized = os.path.normcase(os.path.realpath(path))
        dedupe_key = (descriptor.protocol, normalized)
        if dedupe_key in seen_paths:
            continue
        seen_paths.add(dedupe_key)
        candidates.append(_candidate_from_path(descriptor, path, resolution_source=source))
    return candidates


def _detect_version(path: str) -> str:
    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is not None and launch.managed:
        return "managed-probe-unavailable"
    for args in ([path, "--version"], [path, "-v"]):
        try:
            proc = subprocess.run(
                args,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
        except Exception:
            continue
        text = (proc.stdout or "").strip()
        if proc.returncode == 0 and text:
            for line in text.splitlines():
                clean = line.strip()
                if clean and not clean.lower().startswith(("warning:", "error:")):
                    return clean
            return text.splitlines()[0].strip()
    return "unknown"


def runtime_capability_profile(provider: str, protocol: str) -> dict[str, Any]:
    """Return normalized capability hints discovered during runtime scanning.

    This is intentionally cheap and side-effect free. It captures the ACP/MCP
    shapes Crew knows how to normalize before a real conversation starts; a
    later live probe can refresh this metadata with runtime-reported schemas.
    """
    provider_key = str(provider or "").strip().lower()
    protocol_key = str(protocol or "").strip().lower()
    if protocol_key != "acp":
        return {
            "capability_probe": {
                "version": 1,
                "source": "static",
                "supports_mcp_servers": False,
            }
        }
    followup = {
        "tool_name": "ask_followup_question",
        "tool_name_candidates": [
            "mcp__crew-interaction__ask_followup_question",
            "mcp_crew_interaction_ask_followup_question",
            "ask_followup_question",
        ],
        "question_fields": ["id", "question", "options", "multiSelect"],
        "option_label_fields": ["label", "text", "name", "title", "key"],
        "option_value_fields": ["value"],
        "option_description_fields": [
            "description",
            "desc",
            "detail",
            "details",
            "content",
            "body",
            "explanation",
            "summary",
        ],
    }
    if provider_key == "hermes":
        followup["notes"] = [
            "Hermes may emit short option labels such as A/B/C/D; preserve description-like fields for UI display.",
        ]
    return {
        "capability_probe": {
            "version": 1,
            "source": "static",
            "supports_mcp_servers": True,
            "session_new_mcp_fields": ["mcpServers", "mcp_servers"],
            "session_resume_mcp_fields": ["mcpServers", "mcp_servers"],
            "followup": followup,
        }
    }


def scan_provider_runtime(probe: RuntimeDescriptor) -> RuntimeCandidate | None:
    """Return one detected runtime candidate for the configured provider."""
    path, source = _resolve_executable_with_source(probe.env_var, probe.commands)
    if not path:
        return None
    return _candidate_from_path(probe, path, resolution_source=source)


def _candidate_from_path(
    probe: RuntimeDescriptor,
    path: str,
    *,
    resolution_source: str = "resolved",
) -> RuntimeCandidate:
    version = _detect_version(path)
    return RuntimeCandidate(
        id=_runtime_id(probe.provider, path),
        provider=probe.provider,
        name=probe.name,
        executable_path=path,
        version=version,
        protocol=probe.protocol,
        adapter_id=probe.adapter_id or probe.protocol,
        launch_args=probe.launch_args,
        probe_env=dict(probe.probe_env),
        metadata={
            **runtime_capability_profile(probe.provider, probe.protocol),
            "runtime_descriptor_source": probe.source,
            "descriptor_id": probe.descriptor_id,
            "display_badge": probe.display_badge,
            "adapter_id": probe.adapter_id or probe.protocol,
            "credential_home_paths": list(probe.credential_home_paths),
            "resolution_source": resolution_source,
        },
    )


def scan_kimi_runtime() -> RuntimeCandidate | None:
    """Return the detected Kimi runtime, if the CLI is available."""
    return scan_provider_runtime(builtin_descriptor("kimi"))


def scan_codex_runtime() -> RuntimeCandidate | None:
    """Return the detected Codex runtime, if the CLI is available."""
    return scan_provider_runtime(builtin_descriptor("codex"))


def scan_claude_runtime() -> RuntimeCandidate | None:
    """Return the detected Claude runtime, if the CLI is available."""
    return scan_provider_runtime(builtin_descriptor("claude"))


def scan_hermes_runtime() -> RuntimeCandidate | None:
    """Return the detected Hermes ACP runtime, if the CLI is available."""
    return scan_provider_runtime(builtin_descriptor("hermes"))


def scan_runtimes() -> list[dict[str, Any]]:
    return [candidate.as_dict() for candidate in _scan_descriptors(runtime_descriptors())]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def probe_runtime(candidate: RuntimeCandidate) -> RuntimeProfile:
    """Probe one candidate and normalize failures into a degraded profile."""

    checked_at = _now()
    started_at = time.perf_counter()
    try:
        adapter = get_runtime_adapter(candidate.adapter_id)
        result = await adapter.probe(
            candidate.executable_path,
            provider=candidate.provider,
            launch_args=candidate.launch_args,
            custom_env=candidate.probe_env,
        )
        source = result.source
        models = result.models
        default_model_id = result.default_model_id
        ready = bool(models)
        return RuntimeProfile(
            id=candidate.id,
            provider=candidate.provider,
            name=candidate.name,
            protocol=candidate.protocol,
            executable_path=candidate.executable_path,
            version=candidate.version,
            launch_args=candidate.launch_args,
            availability_status="ready" if ready else "degraded",
            models=models,
            default_model_id=default_model_id,
            capabilities=result.capabilities,
            probe=ProbeResult(
                source=source,
                checked_at=checked_at,
                last_success_at=checked_at if ready else "",
                error_code="" if ready else "models_empty",
                message="" if ready else "运行时未返回可选模型",
            ),
            metadata={
                **(candidate.metadata or {}),
                "probe_stage": source,
                "probe_latency_ms": round((time.perf_counter() - started_at) * 1000),
                **(
                    {"model_migrations": dict(result.model_migrations)}
                    if result.model_migrations
                    else {}
                ),
            },
        )
    except (AcpAdapterError, ExternalCliError, OSError, RuntimeError, KeyError) as exc:
        message = " ".join(str(exc).split())[:240]
        return RuntimeProfile(
            id=candidate.id,
            provider=candidate.provider,
            name=candidate.name,
            protocol=candidate.protocol,
            executable_path=candidate.executable_path,
            version=candidate.version,
            launch_args=candidate.launch_args,
            availability_status="degraded",
            capabilities=RuntimeCapabilities(mcp_servers=candidate.protocol == "acp"),
            probe=ProbeResult(
                source=candidate.adapter_id or candidate.protocol,
                checked_at=checked_at,
                error_code="probe_failed",
                message=message or "运行时探测失败",
            ),
            metadata={
                **(candidate.metadata or {}),
                "probe_stage": candidate.adapter_id or candidate.protocol,
                "probe_latency_ms": round((time.perf_counter() - started_at) * 1000),
            },
        )


async def discover_local_runtimes() -> list[dict[str, Any]]:
    """Discover known local agents and probe all candidates concurrently."""

    detected = _scan_descriptors(runtime_descriptors())
    semaphore = asyncio.Semaphore(4)

    async def bounded_probe(candidate: RuntimeCandidate) -> RuntimeProfile:
        async with semaphore:
            return await probe_runtime(candidate)

    profiles = await asyncio.gather(*(bounded_probe(candidate) for candidate in detected))
    return [profile.to_runtime_dict() for profile in profiles]
