"""Protocol-neutral contracts for external runtime adapters.

Descriptors select an adapter by ``adapter_id``.  Adapters own protocol
probing and execution; callers consume one normalized event stream without
branching on provider names.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal, Protocol, Sequence
from urllib.parse import urlsplit

from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile
from crew.security.models import (
    AdditionalPermissionProfile,
    NetworkAccess,
    NetworkEntry,
)


_PROTECTED_EXTERNAL_ENV_NAMES = frozenset({"JWT"})
_PROTECTED_EXTERNAL_ENV_PREFIXES = ("CREW_", "ACE_SECURITY_", "ACE_BUNDLED_")
_NATIVE_RUNTIME_CONTROLLED_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "PATH",
        "HOME",
        "TMPDIR",
        "PWD",
        "OLDPWD",
    }
)
_MAX_PROJECTED_HOME_FILE_BYTES = 1024 * 1024
_MAX_PROJECTED_HOME_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_PROJECTED_HOME_FILES = 64
_MAX_RUNTIME_CONFIG_ENDPOINTS = 32
_RUNTIME_CONFIG_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _is_protected_external_env(key: str) -> bool:
    normalized = str(key or "").strip().upper()
    return (
        not normalized
        or normalized in _PROTECTED_EXTERNAL_ENV_NAMES
        or normalized.startswith(_PROTECTED_EXTERNAL_ENV_PREFIXES)
    )


def build_external_runtime_env(
    custom_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Inherit the owner env while withholding Crew-owned credentials.

    Model-provider credentials, base URLs and local developer settings remain
    available to known external runtimes. Crew-internal variables, JWT and
    host-only credential material are never inherited or accepted as Runtime overrides.
    """

    env = {
        key: value
        for key, value in os.environ.items()
        if not _is_protected_external_env(key)
    }
    for key, value in dict(custom_env or {}).items():
        if _is_protected_external_env(key):
            continue
        env[str(key)] = str(value)
    return env


def build_managed_external_runtime_env(
    custom_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build external env overrides without native-runtime control variables.

    Provider credentials and endpoint settings remain available. The native
    runtime owns sandbox paths, proxy routing and its internal ACE variables;
    those values are never forwarded as child overrides. MCP server-specific
    declarations are separate protocol data and are intentionally unaffected.
    """

    env = build_external_runtime_env(custom_env)
    return {
        key: value
        for key, value in env.items()
        if key.upper() not in _NATIVE_RUNTIME_CONTROLLED_ENV_NAMES
    }


def build_external_runtime_home_files(
    declared_paths: tuple[str, ...] | list[str] | None,
) -> dict[str, bytes]:
    """Read only descriptor-declared host-home files for managed projection.

    The descriptor is trusted application metadata, not model input. Paths are
    still validated and must remain relative to the host user's HOME. Missing
    files are skipped so an agent can report its normal authentication/setup
    guidance; no arbitrary HOME traversal or directory copy is permitted.
    """

    if not declared_paths:
        return {}
    host_home = Path.home().expanduser().resolve(strict=True)
    projected: dict[str, bytes] = {}
    total = 0
    for raw_path in tuple(dict.fromkeys(str(value).strip() for value in declared_paths)):
        if not raw_path or len(projected) >= _MAX_PROJECTED_HOME_FILES:
            break
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in raw_path
            or ":" in raw_path
        ):
            continue
        source = host_home.joinpath(*relative.parts)
        try:
            resolved = source.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved != source or host_home not in resolved.parents or not resolved.is_file():
            continue
        try:
            size = resolved.stat().st_size
            if size > _MAX_PROJECTED_HOME_FILE_BYTES or total + size > _MAX_PROJECTED_HOME_TOTAL_BYTES:
                continue
            content = resolved.read_bytes()
        except OSError:
            continue
        projected[raw_path] = content
        total += len(content)
    return projected


def _is_explicit_private_endpoint(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def build_external_runtime_network_permissions(
    projected_home_files: dict[str, bytes],
    declared_endpoints: tuple[str, ...] | list[str] | None = None,
) -> AdditionalPermissionProfile:
    """Build exact API permissions from descriptor and projected config declarations.

    Both sources are host-owned runtime metadata: model input cannot add files or
    network targets. Descriptor endpoints cover service URLs a CLI keeps internally
    (for example an OAuth issuer), while projected config supports user-selected API
    bases. Remote plaintext HTTP endpoints are ignored so provider credentials are
    not sent over an insecure transport. Exact loopback/private HTTP endpoints remain
    available for configured local runtimes. Wildcards and malformed targets are
    rejected by NetworkEntry.
    """

    entries: list[NetworkEntry] = []
    seen: set[tuple[str, int, str]] = set()
    candidates = [str(value).strip() for value in (declared_endpoints or ())]
    for content in projected_home_files.values():
        text = content.decode("utf-8", errors="ignore")
        candidates.extend(
            match.group(0).rstrip(".,;:)]}")
            for match in _RUNTIME_CONFIG_URL.finditer(text)
        )
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
            host = str(parsed.hostname or "").rstrip(".").lower()
            scheme = parsed.scheme.lower()
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError:
            continue
        if not host or scheme not in {"http", "https"}:
            continue
        allow_private = _is_explicit_private_endpoint(host)
        if scheme == "http" and not allow_private:
            continue
        key = (host, port, scheme)
        if key in seen:
            continue
        try:
            entry = NetworkEntry(
                host=host,
                port=port,
                protocol=scheme,
                access=NetworkAccess.ALLOW,
                allow_private=allow_private,
                escalatable=False,
            )
        except ValueError:
            continue
        entries.append(entry)
        seen.add(key)
        if len(entries) >= _MAX_RUNTIME_CONFIG_ENDPOINTS:
            break
    return AdditionalPermissionProfile(network=tuple(entries))


def merge_additional_permission_profiles(
    *profiles: AdditionalPermissionProfile,
) -> AdditionalPermissionProfile:
    """Combine independent host-owned grants without broadening their scope."""

    return AdditionalPermissionProfile(
        filesystem=tuple(dict.fromkeys(
            entry for profile in profiles for entry in profile.filesystem
        )),
        network=tuple(dict.fromkeys(
            entry for profile in profiles for entry in profile.network
        )),
        allow_local_binding=any(profile.allow_local_binding for profile in profiles),
    )


class RuntimeResumeRejected(RuntimeError):
    """Adapter rejected a native session/thread before current-turn work began."""


@dataclass(frozen=True)
class RuntimeMcpServer:
    """Protocol-neutral, argv-only MCP server declaration."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RuntimeMcpServer":
        name = str(raw.get("name") or raw.get("id") or "").strip()
        command = str(raw.get("command") or "").strip()
        if not name or not command:
            raise ValueError("Runtime MCP server 必须包含 name 和 command")
        raw_args = raw.get("args")
        args = tuple(str(item) for item in raw_args) if isinstance(raw_args, list) else ()
        raw_env = raw.get("env")
        env: list[tuple[str, str]] = []
        if isinstance(raw_env, dict):
            env.extend((str(key), str(value)) for key, value in raw_env.items())
        elif isinstance(raw_env, list):
            for item in raw_env:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("name") or "").strip()
                if key:
                    env.append((key, str(item.get("value") or "")))
        return cls(name=name, command=command, args=args, env=tuple(env))

    def stdio_config(self, *, env_as_list: bool = False) -> dict[str, Any]:
        config: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
        }
        if self.env:
            config["env"] = (
                [{"name": key, "value": value} for key, value in self.env]
                if env_as_list
                else {key: value for key, value in self.env}
            )
        return config


@dataclass(frozen=True)
class RuntimeAdapterProbe:
    models: list[RuntimeModelProfile]
    default_model_id: str
    capabilities: RuntimeCapabilities
    source: str
    model_migrations: dict[str, str] = field(default_factory=dict)


@dataclass
class ExternalToolEvent:
    name: str
    phase: str
    detail: str = ""
    tool_call_id: str = ""
    args: str = ""


@dataclass
class ExternalStreamEvent:
    kind: str
    text: str = ""
    tool: ExternalToolEvent | None = None
    session_id: str = ""
    session_resumed: bool = False
    session_reset: bool = False
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalPermissionRequest:
    tool_call: dict[str, Any]
    request_id: str = ""
    session_id: str = ""
    raw_params: dict[str, Any] = field(default_factory=dict)


PermissionDecision = Literal["allow", "deny"]


@dataclass
class RuntimeExecutionRequest:
    executable_path: str
    provider: str
    prompt: str
    model: str = ""
    cwd: str = "."
    system_prompt: str = ""
    launch_args: list[str] = field(default_factory=list)
    custom_args: list[str] = field(default_factory=list)
    custom_env: dict[str, str] = field(default_factory=dict)
    credential_home_paths: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    mcp_servers: list[RuntimeMcpServer] = field(default_factory=list)
    additional_permissions: AdditionalPermissionProfile = field(
        default_factory=AdditionalPermissionProfile
    )
    dynamic_tools: list[dict[str, Any]] = field(default_factory=list)
    dynamic_tool_handler: Any = None
    resume_session_id: str = ""
    timeout: float = 120.0
    permission_handler: Any = None

    def __post_init__(self) -> None:
        self.mcp_servers = [
            item if isinstance(item, RuntimeMcpServer) else RuntimeMcpServer.from_mapping(item)
            for item in self.mcp_servers
        ]


class NativeInteractiveLineTransport:
    """Expose a Native Runtime interactive session as a bounded JSONL reader."""

    def __init__(self, session: Any, *, max_line_bytes: int) -> None:
        self.session = session
        self.process = session.process
        self.stderr_lines = session.stderr_lines
        self._max_line_bytes = max(1024, int(max_line_bytes))
        self._buffer = bytearray()

    async def read_line(self) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            if len(self._buffer) > self._max_line_bytes:
                raise ValueError("native external JSONL line exceeds the size limit")
            chunk = await self.session.read_chunk()
            if chunk is None:
                if not self._buffer:
                    return b""
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer.extend(chunk)

    async def write(self, data: bytes) -> None:
        await self.session.write(data)

    async def close(self) -> None:
        await self.session.close()

    async def abort(self) -> None:
        await self.session.abort()


async def open_managed_external_interactive(
    request: RuntimeExecutionRequest,
    command: Sequence[str],
) -> Any | None:
    """Open a managed external child with descriptor-scoped credentials/network."""

    from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
    from crew.security.launch import current_process_launch
    from crew.security.runtime_client import NativeRuntimeClient

    launch = current_process_launch.get()
    if launch is None or not launch.external_managed:
        return None
    if not launch.helper_argv:
        raise RuntimeError("external managed runtime helper is unavailable")

    cwd = Path(request.cwd or ".").expanduser().resolve(strict=True)
    projected_home_files = build_external_runtime_home_files(request.credential_home_paths)
    projected_network_permissions = build_external_runtime_network_permissions(
        projected_home_files,
        request.network_endpoints,
    )
    additional_permissions = merge_additional_permission_profiles(
        request.additional_permissions,
        projected_network_permissions,
    )
    return await SecurityExecutionBroker(
        NativeRuntimeClient(launch.helper_argv)
    ).open_interactive(
        ExecutionRequest(
            command=tuple(str(part) for part in command),
            cwd=cwd,
            permission_profile=launch.profile,
            additional_permissions=additional_permissions,
            trusted_readable_roots=launch.trusted_readable_roots,
            home_files=projected_home_files,
            env_overrides=build_managed_external_runtime_env(request.custom_env),
            timeout_seconds=request.timeout,
            max_output_bytes=64 * 1024 * 1024,
        )
    )


class RuntimeAdapter(Protocol):
    adapter_id: str

    async def probe(
        self,
        executable_path: str,
        *,
        provider: str,
        launch_args: tuple[str, ...] = (),
        custom_env: dict[str, str] | None = None,
    ) -> RuntimeAdapterProbe: ...

    def stream(self, request: RuntimeExecutionRequest) -> AsyncIterator[ExternalStreamEvent]: ...


_ADAPTERS: dict[str, RuntimeAdapter] = {}


def register_runtime_adapter(adapter: RuntimeAdapter) -> RuntimeAdapter:
    adapter_id = str(adapter.adapter_id or "").strip()
    if not adapter_id:
        raise ValueError("RuntimeAdapter.adapter_id 不能为空")
    existing = _ADAPTERS.get(adapter_id)
    if existing is not None and type(existing) is not type(adapter):
        raise ValueError(f"RuntimeAdapter 已注册: {adapter_id}")
    _ADAPTERS[adapter_id] = adapter
    return adapter


def get_runtime_adapter(adapter_id: str) -> RuntimeAdapter:
    key = str(adapter_id or "").strip()
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise KeyError(f"未注册 RuntimeAdapter: {key or '<empty>'}") from exc


def runtime_adapter_ids() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
