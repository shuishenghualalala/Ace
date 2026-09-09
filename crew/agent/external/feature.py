"""Generation owned services for external agent runtimes.

The adapters in this package know how to speak ACP, CLI and Codex protocols.
This module owns the lifecycle boundary around those adapters: a Provider is a
feature service and every submitted Run belongs to that Provider generation.
Consumers therefore keep a generation lease for the whole stream, while
stopping a feature first closes admission and then drains or cancels runs.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from crew.agent.external.catalog import ExternalAgentCatalog
from crew.agent.external.runtime_adapter import (
    ExternalStreamEvent,
    RuntimeAdapter,
    RuntimeExecutionRequest,
    get_runtime_adapter,
)
from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureServiceDependencies,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    ServiceKey,
)

EXTERNAL_AGENT_FEATURE_ID = "product.external-agents"
EXTERNAL_AGENT_CATALOG_SERVICE_KEY: ServiceKey[ExternalAgentCatalog] = ServiceKey(
    "external-agent-catalog"
)
AGENT_RUNTIME_PROVIDER_SERVICE_KEY: ServiceKey["AgentRuntimeProvider"] = ServiceKey(
    "agent-runtime-provider"
)
DELEGATION_SERVICE_KEY: ServiceKey["DelegationService"] = ServiceKey("delegation")


@contextmanager
def acquire_external_service_lease(plugins: Any):
    """Acquire Catalog and Delegation from one visible Feature generation."""
    catalog_binding = plugins.acquire_service_lease(
        EXTERNAL_AGENT_CATALOG_SERVICE_KEY,
        label="external:catalog",
    )
    if catalog_binding is None:
        raise ExternalRunError("external agent catalog service is unavailable")
    catalog, catalog_lease = catalog_binding
    generation = catalog_lease.generation
    delegation_binding = plugins.acquire_service_lease(
        DELEGATION_SERVICE_KEY,
        label="external:delegation",
        generation=generation,
    )
    if delegation_binding is None:
        catalog_lease.release()
        raise ExternalRunError("external delegation service is unavailable")
    delegation, delegation_lease = delegation_binding
    try:
        yield catalog, delegation
    finally:
        delegation_lease.release()
        catalog_lease.release()


class ExternalRunError(RuntimeError):
    """A run cannot be started or has already reached a terminal state."""


@runtime_checkable
class ExternalAgentRun(Protocol):
    """One externally visible invocation owned by a Provider generation."""

    async def stream(self) -> AsyncIterator[ExternalStreamEvent]: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class AgentRuntimeProvider(Protocol):
    """Create external runs without exposing a provider-specific adapter."""

    async def open_run(self, request: RuntimeExecutionRequest) -> ExternalAgentRun: ...

    async def close(self) -> None: ...


class _AdapterRun:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        request: RuntimeExecutionRequest,
        on_done: Callable[[_AdapterRun], None],
    ) -> None:
        self._adapter = adapter
        self._request = request
        self._on_done = on_done
        self._stream_iterator: AsyncIterator[ExternalStreamEvent] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._closed = False
        self._terminal = False
        self._cancelled = False
        self._iterator_close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def _close_iterator(self) -> None:
        iterator = self._stream_iterator
        if iterator is None:
            return
        async with self._iterator_close_lock:
            if iterator is not self._stream_iterator:
                return
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                await aclose()

    async def stream(self) -> AsyncIterator[ExternalStreamEvent]:
        if self._closed or self._terminal:
            raise ExternalRunError("external run is closed")
        if self._stream_iterator is not None:
            raise ExternalRunError("external run stream can only be consumed once")
        self._stream_iterator = self._adapter.stream(self._request)
        self._task = asyncio.current_task()
        try:
            async for event in self._stream_iterator:
                yield event
        except asyncio.CancelledError:
            self._cancelled = True
            raise
        finally:
            await self._close_iterator()
            self._terminal = True
            self._on_done(self)
            self._stream_iterator = None
            self._task = None

    async def cancel(self) -> None:
        self._cancelled = True
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.close()

    async def close(self) -> None:
        async with self._iterator_close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._complete_close(),
                    name="external-adapter-run-close",
                )
            task = self._close_task
        await asyncio.shield(task)

    async def _complete_close(self) -> None:
        active_task = self._task
        current = asyncio.current_task()
        if active_task is not None and active_task is not current and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
        await self._close_iterator()
        self._closed = True
        self._terminal = True
        self._on_done(self)


class AdapterRuntimeProvider:
    """Default Provider that dispatches to the registered protocol adapter."""

    def __init__(
        self,
        adapter_resolver: Callable[[RuntimeExecutionRequest], RuntimeAdapter] | None = None,
    ) -> None:
        self._adapter_resolver = adapter_resolver or (
            lambda request: get_runtime_adapter(request.adapter_id or request.provider)
        )
        self._runs: set[_AdapterRun] = set()
        self._closed = False

    async def open_run(self, request: RuntimeExecutionRequest) -> ExternalAgentRun:
        if self._closed:
            raise ExternalRunError("external runtime provider is closed")
        try:
            adapter = self._adapter_resolver(request)
        except KeyError as exc:
            raise ExternalRunError(str(exc)) from exc
        run = _AdapterRun(adapter, request, self._runs.discard)
        self._runs.add(run)
        return run

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        runs = tuple(self._runs)
        if runs:
            await asyncio.gather(*(run.cancel() for run in runs), return_exceptions=True)
        self._runs.clear()


class DelegationService:
    """Provider-backed consumer surface used by Agent, Team and Gateway."""

    def __init__(self, provider: AgentRuntimeProvider, lease_factory: Callable[[str], Any] | None = None) -> None:
        self.provider = provider
        self._lease_factory = lease_factory
        self._closed = False
        self._runs: set[_LeasedRun] = set()

    async def submit(self, request: RuntimeExecutionRequest) -> ExternalAgentRun:
        if self._closed:
            raise ExternalRunError("delegation service is closed")
        lease = self._lease_factory("external:run") if self._lease_factory else None
        # Run cancellation is coordinated by the watcher below. The submitter
        # may be a long-lived request task that must not be interrupted before
        # the Run has released its generation lease.
        detach_owner = getattr(lease, "detach_owner", None)
        if callable(detach_owner):
            detach_owner()
        try:
            run = await self.provider.open_run(request)
        except BaseException:
            if lease is not None:
                lease.release()
            raise
        leased = _LeasedRun(run, lease, self._runs.discard)
        self._runs.add(leased)
        return leased

    async def stream(self, request: RuntimeExecutionRequest) -> AsyncIterator[ExternalStreamEvent]:
        run = await self.submit(request)
        try:
            async for event in run.stream():
                yield event
        finally:
            await run.close()

    async def cancel(self, run: ExternalAgentRun) -> None:
        await run.cancel()

    async def close(self) -> None:
        self._closed = True
        runs = tuple(self._runs)
        if runs:
            await asyncio.gather(*(run.cancel() for run in runs), return_exceptions=True)
        self._runs.clear()


class _LeasedRun:
    """Release its generation lease only after the run has fully closed."""

    def __init__(
        self,
        run: ExternalAgentRun,
        lease: Any,
        on_done: Callable[[_LeasedRun], None],
    ) -> None:
        self._run = run
        self._lease = lease
        self._on_done = on_done
        self._released = False
        self._cancel_watcher: asyncio.Task[Any] | None = None
        if lease is not None and hasattr(lease, "cancel_event"):
            self._cancel_watcher = asyncio.create_task(
                self._watch_cancellation(lease),
                name="external-run-cancel-watcher",
            )

    async def _watch_cancellation(self, lease: Any) -> None:
        await lease.cancel_event.wait()
        if not self._released:
            await self.close()

    async def stream(self) -> AsyncIterator[ExternalStreamEvent]:
        try:
            async for event in self._run.stream():
                yield event
        finally:
            await self.close()

    async def cancel(self) -> None:
        try:
            await self._run.cancel()
        finally:
            self._release()

    async def close(self) -> None:
        try:
            await self._run.close()
        finally:
            self._release()

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        watcher = self._cancel_watcher
        if watcher is not None and watcher is not asyncio.current_task() and not watcher.done():
            watcher.cancel()
        if self._lease is not None:
            self._lease.release()
        self._on_done(self)


@dataclass(frozen=True, slots=True)
class ExternalAgentFeatureBundle:
    definition: FeatureDefinition
    catalog: ExternalAgentCatalog | None
    provider: AgentRuntimeProvider | None
    delegation: DelegationService | None


class ExternalAgentFeatureHost(Protocol):
    """Small compatibility surface for hosts during the migration."""

    external_agents: ExternalAgentCatalog | None


def build_external_agent_feature(
    host: ExternalAgentFeatureHost | None = None,
    *,
    registry: Any = None,
    interaction_bridge_getter: Callable[[], Any] | None = None,
    catalog: ExternalAgentCatalog | None = None,
    provider: AgentRuntimeProvider | None = None,
    catalog_factory: Callable[[], ExternalAgentCatalog] | None = None,
    provider_factory: Callable[[], AgentRuntimeProvider] | None = None,
    own_catalog: bool = False,
    own_provider: bool = False,
    enabled: bool = True,
    desired_config_revision: int = 1,
) -> ExternalAgentFeatureBundle:
    """Build one external-agent Feature definition.

    Factories are evaluated once for the candidate generation and again only
    when Runtime asks the definition to install a replacement generation. A
    supplied host catalog remains an embedded compatibility dependency and is
    not closed by this feature.
    """

    if own_provider and provider_factory is None:
        raise ValueError("own_provider requires provider_factory for fresh generations")
    host_catalog = getattr(host, "external_agents", None) if host is not None else None
    candidate_catalog = catalog or host_catalog
    candidate_provider = provider

    def make_catalog() -> ExternalAgentCatalog | None:
        return catalog_factory() if catalog_factory else candidate_catalog

    def make_provider() -> AgentRuntimeProvider | None:
        return provider_factory() if provider_factory else candidate_provider

    async def install(context: FeatureInstallContext) -> None:
        if not enabled:
            return
        active_catalog: ExternalAgentCatalog | None = None
        active_provider: AgentRuntimeProvider | None = None
        try:
            active_catalog = make_catalog()
            active_provider = make_provider()
            if active_catalog is None:
                raise ValueError("external agent feature requires an ExternalAgentCatalog")
            if active_provider is None:
                raise ValueError("external agent feature requires an AgentRuntimeProvider")
            delegation = DelegationService(active_provider, context.acquire_lease)
        except BaseException:
            await _close_owned(active_provider if (own_provider or provider_factory) else None)
            await _close_owned(active_catalog if (own_catalog or catalog_factory) else None)
            raise
        # Contribution teardown closes admission before resource teardown.
        context.register_disposer(
            delegation.close,
            label="contribution:external.delegation",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        if own_provider or provider_factory:
            context.register_disposer(
                active_provider.close,
                label="resource:external.provider",
                phase=RegistrationPhase.RESOURCE,
            )
        if own_catalog or catalog_factory:
            close_catalog = getattr(active_catalog, "close", None)
            if callable(close_catalog):
                context.register_disposer(
                    close_catalog,
                    label="resource:external.catalog",
                    phase=RegistrationPhase.RESOURCE,
                )
        context.register_service(
            EXTERNAL_AGENT_CATALOG_SERVICE_KEY,
            active_catalog,
            label="service:external-agent-catalog",
        )
        context.register_service(
            AGENT_RUNTIME_PROVIDER_SERVICE_KEY,
            active_provider,
            label="service:agent-runtime-provider",
        )
        context.register_service(
            DELEGATION_SERVICE_KEY,
            delegation,
            label="service:delegation",
        )
        if registry is not None:
            from crew.agent.external.tools import register_external_agent_tools

            register_external_agent_tools(
                registry,
                active_catalog,
                interaction_bridge_getter=interaction_bridge_getter,
                delegation_service=delegation,
                lease_factory=context.acquire_lease,
            )
            context.register_disposer(
                lambda: registry.unregister("delegate_to_external_agent"),
                label="tool:delegate_to_external_agent",
                phase=RegistrationPhase.CONTRIBUTION,
            )
        if host is not None:
            setattr(host, "external_agents", active_catalog)
            context.register_disposer(
                lambda: _clear_host_catalog(host, active_catalog),
                label="binding:external.catalog",
                phase=RegistrationPhase.CONTRIBUTION,
            )

    definition = FeatureDefinition(
        EXTERNAL_AGENT_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            EXTERNAL_AGENT_FEATURE_ID,
            provides=(
                (
                    EXTERNAL_AGENT_CATALOG_SERVICE_KEY,
                    AGENT_RUNTIME_PROVIDER_SERVICE_KEY,
                    DELEGATION_SERVICE_KEY,
                )
                if enabled
                else ()
            ),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.CANCEL,
        update_strategy=FeatureUpdateStrategy.RESTART,
    )
    return ExternalAgentFeatureBundle(
        definition=definition,
        catalog=candidate_catalog,
        provider=candidate_provider,
        delegation=(DelegationService(candidate_provider) if candidate_provider and enabled else None),
    )


async def _close_owned(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


def _clear_host_catalog(host: ExternalAgentFeatureHost, catalog: ExternalAgentCatalog) -> None:
    if getattr(host, "external_agents", None) is catalog:
        setattr(host, "external_agents", None)


__all__ = [
    "acquire_external_service_lease",
    "AGENT_RUNTIME_PROVIDER_SERVICE_KEY",
    "AdapterRuntimeProvider",
    "AgentRuntimeProvider",
    "DELEGATION_SERVICE_KEY",
    "DelegationService",
    "EXTERNAL_AGENT_CATALOG_SERVICE_KEY",
    "EXTERNAL_AGENT_FEATURE_ID",
    "ExternalAgentFeatureBundle",
    "ExternalAgentFeatureHost",
    "ExternalAgentRun",
    "ExternalRunError",
    "build_external_agent_feature",
]
