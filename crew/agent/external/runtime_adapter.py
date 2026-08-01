"""Protocol-neutral contracts for external runtime adapters.

Descriptors select an adapter by ``adapter_id``.  Adapters own protocol
probing and execution; callers consume one normalized event stream without
branching on provider names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile


_PROTECTED_EXTERNAL_ENV_NAMES = frozenset({"JWT"})
_PROTECTED_EXTERNAL_ENV_PREFIXES = ("CREW_",)


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
    mcp_servers: list[RuntimeMcpServer] = field(default_factory=list)
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
