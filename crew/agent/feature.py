"""Core Agent Factory Feature.

The Feature publishes the stable factory contract used by other Features.  It
does not own the Agent cache or the concrete Agent implementation; the host
supplies a builder and remains responsible for those resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from crew.core.interfaces import (
    Agent,
    AgentFactory,
    AgentModelResolution,
    ChildAgentRequest,
)
from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    ServiceKey,
    run_async_compat,
)

AGENT_FACTORY_FEATURE_ID = "core.agent-factory"
AGENT_FACTORY_SERVICE_KEY: ServiceKey[AgentFactory] = ServiceKey("agent-factory", version=1)


class ChildAgentBuilder(Protocol):
    """Host-side child construction callback at the typed Feature boundary."""

    def __call__(
        self,
        request: ChildAgentRequest,
    ) -> Agent: ...


class ModelResolver(Protocol):
    """Provider-neutral model resolver used by the factory contract."""

    def __call__(self, request: ChildAgentRequest) -> AgentModelResolution: ...


class AgentFactoryHost(Protocol):
    """Minimum host surface required by the core Agent Feature."""

    agent_factory: AgentFactory | None

    def _build_subagent(self, request: ChildAgentRequest) -> Agent: ...


class InProcessAgentFactory:
    """Adapter that exposes the current in-process child builder as a service."""

    def __init__(
        self,
        builder: ChildAgentBuilder,
        model_resolver: ModelResolver | None = None,
    ) -> None:
        if not callable(builder):
            raise TypeError("child agent builder must be callable")
        self._builder = builder
        self._model_resolver = model_resolver

    def create_child(self, request: ChildAgentRequest) -> Agent:
        if not isinstance(request, ChildAgentRequest):
            raise TypeError("create_child requires ChildAgentRequest")
        return self._builder(request)

    def resolve_model(self, request: ChildAgentRequest) -> AgentModelResolution:
        if not isinstance(request, ChildAgentRequest):
            raise TypeError("resolve_model requires ChildAgentRequest")
        if self._model_resolver is not None:
            return self._model_resolver(request)
        requested = request.model.strip() or "inherit"
        return AgentModelResolution(
            requested_model=requested,
            effective_model=requested,
            inherited=requested == "inherit",
        )

    # Compatibility aliases are intentionally outside the core Protocol.  They
    # let a host migrate old generic factory consumers without weakening the
    # typed child-agent contract above.
    def create(
        self,
        agent_config: dict[str, Any] | None = None,
        *,
        owner_account_id: str,
    ) -> Agent:
        request = ChildAgentRequest.from_mapping(agent_config or {})
        return self.create_child(request)

    def build(
        self,
        agent_config: dict[str, Any] | None = None,
        *,
        owner_account_id: str,
    ) -> Agent:
        """Compatibility spelling for consumers that call a factory ``build``."""

        return self.create(agent_config, owner_account_id=owner_account_id)

    def __call__(
        self,
        agent_config: dict[str, Any] | None = None,
        *,
        owner_account_id: str,
    ) -> Agent:
        return self.create(agent_config, owner_account_id=owner_account_id)


@dataclass(frozen=True, slots=True)
class AgentFactoryFeatureBundle:
    """Definition and candidate service for one core Agent generation."""

    definition: FeatureDefinition
    factory: AgentFactory


def build_agent_factory_feature(
    host: AgentFactoryHost,
    *,
    factory: AgentFactory | None = None,
    child_builder: ChildAgentBuilder | None = None,
    model_resolver: ModelResolver | None = None,
    desired_config_revision: int = 1,
    runtime: FeatureRuntime | None = None,
    activate: bool = False,
) -> AgentFactoryFeatureBundle:
    """Build the ``core.agent-factory`` Feature definition.

    ``factory`` is useful for embedded hosts and tests.  Production callers
    normally use the host's ``_build_subagent`` callback through the default
    in-process adapter.  The Feature only publishes the service; it never
    creates an Agent during activation.
    """

    if factory is not None and (child_builder is not None or model_resolver is not None):
        raise ValueError("pass either factory or child_builder/model_resolver, not both")
    candidate: AgentFactory = factory or InProcessAgentFactory(
        child_builder or host._build_subagent,
        model_resolver,
    )
    predecessor = getattr(host, "agent_factory", None)
    predecessor_marker = getattr(host, "_agent_factory_feature_factory", None)
    pending: list[AgentFactory] = [candidate]

    def next_factory() -> AgentFactory:
        return pending.pop() if pending else (
            factory
            or InProcessAgentFactory(
                child_builder or host._build_subagent,
                model_resolver,
            )
        )

    async def install(context: FeatureInstallContext) -> None:
        candidate_factory = next_factory()
        current = getattr(host, "agent_factory", None)
        is_predecessor = current is not None and (
            current is predecessor and predecessor_marker is current
        )
        explicit_binding = (
            current is not None
            and current is not candidate_factory
            and not is_predecessor
        )
        active = current if explicit_binding else candidate_factory
        host.agent_factory = active
        # This marker distinguishes an embedded host's explicit service from
        # the previous generation when a restart installs a replacement.
        try:
            setattr(host, "_agent_factory_feature_factory", active)
        except Exception:  # pragma: no cover - unusual slots-only host
            pass

        def clear_host_binding() -> None:
            if getattr(host, "agent_factory", None) is active and not explicit_binding:
                host.agent_factory = None
            if getattr(host, "_agent_factory_feature_factory", None) is active:
                try:
                    setattr(host, "_agent_factory_feature_factory", None)
                except Exception:  # pragma: no cover - unusual slots-only host
                    pass

        context.register_disposer(
            clear_host_binding,
            label="binding:agent-factory",
        )
        context.register_service(
            AGENT_FACTORY_SERVICE_KEY,
            active,
            label="service:agent-factory",
        )

    definition = FeatureDefinition(
        AGENT_FACTORY_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            AGENT_FACTORY_FEATURE_ID,
            provides=(AGENT_FACTORY_SERVICE_KEY,),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.REPLACE,
        required_by_product=True,
    )
    bundle = AgentFactoryFeatureBundle(definition=definition, factory=candidate)
    if activate:
        if runtime is None:
            raise ValueError("activate=True requires a FeatureRuntime")
        record = run_async_compat(runtime.activate(definition))
        if record.state is not FeatureState.ACTIVE or record.scope is None:
            raise RuntimeError(
                f"Agent Factory Feature 激活失败: state={record.state.value} error={record.error}"
            )
    return bundle


__all__ = [
    "AGENT_FACTORY_FEATURE_ID",
    "AGENT_FACTORY_SERVICE_KEY",
    "ChildAgentBuilder",
    "AgentFactoryFeatureBundle",
    "AgentFactoryHost",
    "InProcessAgentFactory",
    "ModelResolver",
    "build_agent_factory_feature",
]
