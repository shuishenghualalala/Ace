"""Subagent capability seam.

This module contains the small, transport-neutral contract shared by the
subagent tools and providers.  The contract deliberately does not import the
tool registry or the application composition root: a provider can therefore
be replaced without changing the model-facing layer.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from crew.core.errors import CrewError
from crew.features.services import ServiceKey


StopReason: TypeAlias = Literal[
    "completed",
    "aborted",
    "error",
    "max_tokens",
    "refusal",
    "timed_out",
]
TimeoutKind: TypeAlias = Literal["idle", "absolute"]
LifecycleKind: TypeAlias = Literal["one_shot", "continuable"]
SessionPersistenceKind: TypeAlias = Literal["none", "hidden"]


def normalize_stop_reason(value: object) -> StopReason:
    """Normalize provider vocabulary at the seam boundary.

    Providers are allowed to use the wire spelling ``max-tokens`` and the
    legacy ``timeout`` spelling internally.  Consumers only see the stable
    Ace vocabulary.
    """

    normalized = str(value or "error").strip().lower().replace("-", "_")
    if normalized == "timeout":
        normalized = "timed_out"
    if normalized not in {
        "completed",
        "aborted",
        "error",
        "max_tokens",
        "refusal",
        "timed_out",
    }:
        return "error"
    return normalized  # type: ignore[return-value]


class SubagentError(CrewError):
    """Base error for failures before a one-shot run is published."""

    default_code = "SUBAGENT_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class SubagentValidationError(SubagentError, ValueError):
    """A caller supplied an invalid start request."""

    default_code = "INVALID_REQUEST"


class SubagentProviderError(SubagentError):
    """A provider cannot be resolved or registered."""


class UnknownProviderError(SubagentProviderError):
    default_code = "NO_PROVIDER"


class DuplicateProviderError(SubagentProviderError):
    default_code = "DUPLICATE_PROVIDER"


class SubagentCapabilityError(SubagentError):
    """The selected provider cannot satisfy a requested capability."""

    default_code = "UNSUPPORTED_CAPABILITY"

    def __init__(self, provider: str, capability: str) -> None:
        self.provider = provider
        self.capability = capability
        super().__init__(
            f'subagent provider "{provider}" does not support the '
            f'"{capability}" capability',
        )


class SubagentDepthError(SubagentError):
    """The requested child would exceed the runtime depth limit."""

    default_code = "DEPTH_LIMIT"

    def __init__(self, depth: int, maximum: int) -> None:
        self.depth = depth
        self.maximum = maximum
        super().__init__(f"subagent depth {depth} exceeds maximum {maximum}")


class SubagentPublicationError(SubagentError):
    """A provider failed while crossing the publication boundary."""

    default_code = "PUBLICATION_FAILED"


@dataclass(frozen=True, slots=True)
class SubagentCapabilities:
    """Features a provider can honor for a one-shot request.

    These five fields are the capability matrix used by the service.  Provider
    context inheritance is descriptive provider metadata, not a capability;
    continuable support is expressed by the provider's optional
    ``prepare_continuable`` method.
    """

    output_schema: bool = False
    depth_limit: bool = False
    tool_filter: bool = False
    persona: bool = False
    agent_options: bool = False


@dataclass(frozen=True, slots=True)
class SubagentDescriptor:
    """Immutable publication record for one subagent identity."""

    version: int = 1
    lifecycle: LifecycleKind = "one_shot"
    provider: str = "inprocess-spawn"
    parent_session_id: str = ""
    label: str | None = None
    model: str | None = None
    persona: str | None = None
    tool_filter: tuple[str, ...] = ()
    delegation_depth: int = 0
    subagent_id: str = ""
    session_persistence: SessionPersistenceKind = "none"
    spec: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def backend(self) -> str:
        """Compatibility alias used by the earlier backend terminology."""

        return self.provider

    @property
    def depth(self) -> int:
        return self.delegation_depth


@dataclass(frozen=True, slots=True)
class StartRequest:
    """Caller-facing one-shot request.

    ``goal_text`` is the Ace-native name.  ``prompt`` is accepted as a
    parallel field so adapters can pass a transport-neutral request without
    first manufacturing an ``Envelope``.  The provider uses
    ``effective_prompt`` and never needs to know which spelling was used.
    """

    label: str = ""
    goal_text: str = ""
    prompt: str = ""
    parent_session_id: str = ""
    spec: Mapping[str, Any] = field(default_factory=dict)
    parent_depth: int = 0
    depth: int | None = None
    max_depth: int | None = None
    parent: Any | None = field(default=None, repr=False, compare=False)
    owner_account_id: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    subagent_id: str | None = None
    provider: str = ""
    lifecycle: LifecycleKind = "one_shot"
    session_persistence: SessionPersistenceKind = "none"
    output_schema: Mapping[str, Any] | None = None
    tool_filter: Any | None = None
    persona: str | None = None
    agent_options: Mapping[str, Any] | None = None

    @property
    def effective_prompt(self) -> str:
        return (self.goal_text or self.prompt or "").strip()

    @property
    def goal(self) -> str:
        """Short alias for integrations that call the prompt a goal."""

        return self.effective_prompt

    @property
    def child_depth(self) -> int:
        """The depth to record for the child, never below the parent depth."""

        minimum = max(0, int(self.parent_depth)) + 1
        if self.depth is not None:
            return max(minimum, int(self.depth))
        return minimum


@dataclass(frozen=True, slots=True)
class ResolvedStartRequest(StartRequest):
    """Provider-facing request after runtime validation and publication."""

    descriptor: SubagentDescriptor = field(default_factory=SubagentDescriptor)
    # Provider-specific execution details are attached only after the stable
    # request has crossed runtime validation.  They are intentionally absent
    # from ``StartRequest`` so the public seam cannot grow application-owned
    # handles or in-process lifecycle callbacks.
    runtime_options: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class SubagentResult:
    """Terminal one-shot outcome; child-level failures are data."""

    output: str = ""
    stop_reason: StopReason = "completed"
    diagnostic: str = ""
    partial_output: str = ""
    timeout_kind: TimeoutKind | None = None
    structured: dict[str, Any] | None = None
    tool_calls: int = 0
    last_tool: str = ""
    duration_seconds: float = 0.0
    subagent_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_reason", normalize_stop_reason(self.stop_reason))
        if self.stop_reason != "timed_out":
            object.__setattr__(self, "timeout_kind", None)

    @property
    def status(self) -> str:
        """Map the stable outcome to the legacy tool status vocabulary."""

        if self.stop_reason in {"completed", "max_tokens", "refusal"}:
            return "completed"
        if self.stop_reason == "aborted" and self.output:
            return "completed"
        if self.stop_reason == "timed_out":
            return "timeout"
        if self.stop_reason == "aborted":
            return "cancelled"
        return "error"

    @property
    def error(self) -> str | None:
        return self.diagnostic or None

    @property
    def summary(self) -> str:
        """Human-readable result text used by the old tool adapter."""

        if self.stop_reason in {"completed", "max_tokens", "refusal"}:
            return self.output
        if self.stop_reason == "aborted" and self.output:
            return self.output
        diagnostic = self.diagnostic or {
            "timed_out": "子智能体执行超时",
            "aborted": "子智能体已取消",
            "refusal": "子智能体拒绝执行",
            "max_tokens": "子智能体达到输出上限",
            "error": "子智能体执行出错",
        }.get(self.stop_reason, "子智能体执行出错")
        # The pre-seam adapter used the error message as both diagnosis and
        # partial output.  Keep that odd but stable rendering at this boundary
        # while the semantic fields remain separated internally.
        tail = (
            diagnostic
            if self.stop_reason == "error"
            else (self.output or self.partial_output).strip()
        )
        if tail:
            return f"{diagnostic}\n部分输出：\n{tail[-800:]}"
        return diagnostic

    def to_legacy_dict(self, *, agent: str = "") -> dict[str, Any]:
        """Return the stable JSON shape consumed by existing subagent tools.

        ``stop_reason``/``timeout_kind`` 是阶段 B 新增字段（只增不改）：旧
        ``status`` 语义保持不变，消费方按需取用六态结局词汇。
        """

        result: dict[str, Any] = {
            "agent": agent,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "summary": self.summary,
            "duration_seconds": round(self.duration_seconds, 2),
            "tool_calls": self.tool_calls,
            "last_tool": self.last_tool,
        }
        if self.timeout_kind:
            result["timeout_kind"] = self.timeout_kind
        if self.subagent_id:
            result["subagent_id"] = self.subagent_id
        return result


class SubagentRun:
    """Published one-shot handle with an idempotent disposal boundary."""

    def __init__(
        self,
        run_id: str,
        result: Awaitable[SubagentResult] | SubagentResult,
        *,
        dispose: Callable[[], Awaitable[None] | None] | None = None,
        local_agent: Any | None = None,
        descriptor: SubagentDescriptor | None = None,
    ) -> None:
        self.id = str(run_id)
        self.local_agent = local_agent
        self.descriptor = descriptor
        self._result = result if inspect.isawaitable(result) else _immediate_result(result)
        self._dispose_callback = dispose
        self._disposed = False
        self._dispose_lock: Any | None = None

    @property
    def result(self) -> Awaitable[SubagentResult]:
        return self._result

    @property
    def disposed(self) -> bool:
        return self._disposed

    async def dispose(self) -> None:
        """Release owned resources exactly once, including concurrent callers."""

        if self._dispose_lock is None:
            # Creating the lock lazily keeps constructing a run safe outside an
            # event loop; providers normally construct it while running.
            import asyncio

            self._dispose_lock = asyncio.Lock()
        async with self._dispose_lock:
            if self._disposed:
                return
            self._disposed = True
            callback = self._dispose_callback
            if callback is None:
                return
            returned = callback()
            if inspect.isawaitable(returned):
                await returned


async def _immediate_result(result: SubagentResult) -> SubagentResult:
    return result


@runtime_checkable
class SubagentProvider(Protocol):
    """A transport implementation for one-shot subagent execution."""

    name: str
    capabilities: SubagentCapabilities
    inherits_parent_context: bool

    async def start(self, request: ResolvedStartRequest) -> SubagentRun:
        ...


@runtime_checkable
class SubagentService(Protocol):
    """Stable consumer-facing service implemented by ``SubagentRuntime``."""

    def register_provider(
        self,
        provider: SubagentProvider,
    ) -> Callable[[], None]:
        ...

    def register_provider_resolver(
        self,
        resolver: Callable[[str, str | None], SubagentProvider | None],
    ) -> Callable[[], None]:
        ...

    def unregister_provider(
        self,
        name: str,
        *,
        expected: SubagentProvider | None = None,
    ) -> bool:
        ...

    def get_provider(
        self,
        name: str,
        *,
        owner_account_id: str | None = None,
    ) -> SubagentProvider | None:
        ...

    def list_providers(self) -> list[str]:
        ...

    async def start(
        self,
        provider: str | StartRequest,
        request: StartRequest | None = None,
    ) -> SubagentRun:
        ...

    async def execute(
        self,
        provider: str | StartRequest | Mapping[str, Any] | None = None,
        request: StartRequest | Mapping[str, Any] | None = None,
        *,
        provider_name: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> SubagentResult:
        ...

    async def execute_many(
        self,
        items: Iterable[
            tuple[StartRequest | Mapping[str, Any], Mapping[str, Any] | None]
        ],
        *,
        max_concurrent: int,
    ) -> list[SubagentResult]:
        ...

    def list_agents(
        self,
        *,
        parent_session_id: str,
        owner_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    async def send_message(
        self,
        *,
        subagent_id: str,
        message: str,
        parent_session_id: str,
        owner_account_id: str | None = None,
        trigger_turn: bool = True,
    ) -> dict[str, Any]:
        ...

    def interrupt_agent(
        self,
        *,
        subagent_id: str,
        parent_session_id: str,
        owner_account_id: str | None = None,
        message: str | None = None,
    ) -> bool:
        ...


class ProviderRegistry:
    """Named provider registry with fail-loud duplicate handling."""

    def __init__(self, providers: Mapping[str, SubagentProvider] | None = None) -> None:
        self._providers: dict[str, SubagentProvider] = {}
        self._resolvers: list[
            Callable[[str, str | None], SubagentProvider | None]
        ] = []
        for provider in (providers or {}).values():
            self.register(provider)

    def register(self, provider: SubagentProvider) -> Callable[[], None]:
        name = str(getattr(provider, "name", "") or "").strip()
        if not name:
            raise SubagentValidationError("subagent provider name must not be empty")
        if name in self._providers:
            raise DuplicateProviderError(
                f'subagent provider "{name}" is already registered',
            )
        self._providers[name] = provider
        removed = False

        def dispose() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            if self._providers.get(name) is provider:
                self._providers.pop(name, None)

        return dispose

    def register_resolver(
        self,
        resolver: Callable[[str, str | None], SubagentProvider | None],
    ) -> Callable[[], None]:
        """Register a lazy provider family owned by an external Feature scope."""

        if not callable(resolver):
            raise TypeError("subagent provider resolver must be callable")
        self._resolvers.append(resolver)
        removed = False

        def dispose() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                self._resolvers.remove(resolver)
            except ValueError:
                pass

        return dispose

    def unregister(
        self,
        name: str,
        *,
        expected: SubagentProvider | None = None,
    ) -> bool:
        provider = self._providers.get(str(name).strip())
        if provider is None or (expected is not None and provider is not expected):
            return False
        self._providers.pop(str(name).strip(), None)
        return True

    def get(
        self,
        name: str,
        *,
        owner_account_id: str | None = None,
    ) -> SubagentProvider | None:
        normalized = str(name).strip()
        direct = self._providers.get(normalized)
        if direct is not None:
            return direct
        matches: list[SubagentProvider] = []
        for resolver in tuple(self._resolvers):
            candidate = resolver(normalized, owner_account_id)
            if candidate is None:
                continue
            candidate_name = str(getattr(candidate, "name", "") or "").strip()
            if candidate_name != normalized:
                raise SubagentValidationError(
                    f'provider resolver returned {candidate_name!r} for {normalized!r}'
                )
            matches.append(candidate)
        if len(matches) > 1:
            raise DuplicateProviderError(
                f'multiple subagent provider resolvers matched "{normalized}"'
            )
        return matches[0] if matches else None

    def require(
        self,
        name: str,
        *,
        owner_account_id: str | None = None,
    ) -> SubagentProvider:
        provider = self.get(name, owner_account_id=owner_account_id)
        if provider is None:
            raise UnknownProviderError(f'no subagent provider registered for "{name}"')
        return provider

    def names(self) -> list[str]:
        return list(self._providers)

    def list(self) -> list[SubagentProvider]:
        return list(self._providers.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self):
        return iter(self._providers.values())


# Avoid constructing through the subscripted typing alias at runtime: the
# frozen/slots-backed ServiceKey has no room for ``__orig_class__``.
SUBAGENT_SERVICE_KEY = ServiceKey("subagent")
ServiceKeySubagent = SUBAGENT_SERVICE_KEY
SUBAGENT_SERVICE = SUBAGENT_SERVICE_KEY

# Naming aliases keep the seam easy to consume from adapters that use the
# fully qualified DSH-style names while the Ace-facing contract stays short.
SubagentStartRequest = StartRequest
SubagentOutcome = SubagentResult
SubagentBackend = SubagentProvider
SubagentProviderRegistry = ProviderRegistry


def coerce_start_request(value: StartRequest | Mapping[str, Any]) -> StartRequest:
    """Convert a mapping from a tool adapter into a validated request object."""

    if isinstance(value, StartRequest):
        return value
    if not isinstance(value, Mapping):
        raise SubagentValidationError("subagent start request must be a mapping")
    raw = dict(value)
    goal = raw.pop("goal_text", raw.pop("goal", raw.pop("prompt", "")))
    prompt = raw.pop("prompt", "")
    allowed = {
        field_name
        for field_name in StartRequest.__dataclass_fields__
    }
    payload = {key: raw_value for key, raw_value in raw.items() if key in allowed}
    payload["goal_text"] = str(goal or "")
    if prompt:
        payload["prompt"] = str(prompt)
    spec = payload.get("spec")
    payload["spec"] = dict(spec or {}) if isinstance(spec, Mapping) else {}
    return StartRequest(**payload)


__all__ = [
    "DuplicateProviderError",
    "LifecycleKind",
    "SessionPersistenceKind",
    "ProviderRegistry",
    "ResolvedStartRequest",
    "SUBAGENT_SERVICE_KEY",
    "SUBAGENT_SERVICE",
    "ServiceKeySubagent",
    "StartRequest",
    "StopReason",
    "SubagentCapabilities",
    "SubagentCapabilityError",
    "SubagentDepthError",
    "SubagentDescriptor",
    "SubagentError",
    "SubagentProvider",
    "SubagentBackend",
    "SubagentOutcome",
    "SubagentProviderError",
    "SubagentProviderRegistry",
    "SubagentPublicationError",
    "SubagentResult",
    "SubagentRun",
    "SubagentService",
    "SubagentValidationError",
    "SubagentStartRequest",
    "TimeoutKind",
    "UnknownProviderError",
    "coerce_start_request",
    "normalize_stop_reason",
]
