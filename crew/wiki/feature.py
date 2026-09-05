"""Wiki Knowledge Feature Bundle and Local Provider assembly."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from crew.core.interfaces import LLMProvider
from crew.features import (
    ContextContributor,
    ContextPhase,
    FeatureEvent,
    FeatureEventContributor,
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    run_async_compat,
)
from crew.tools.registry import FunctionTool, Registry

from .attachments import build_wiki_agent_context_contributor
from .compiler import WikiCompiler
from .config import WikiConfig
from .manager import WikiSessionManager
from .query import WikiQuerier
from .service import (
    KnowledgeService,
    KNOWLEDGE_SERVICE_KEY,
    LocalWikiProvider,
    WikiProviderComponents,
)
from .store import FileSystemWikiStore, WikiStore
from .summary import WikiSummarizer
from .tools import build_wiki_tools

KNOWLEDGE_SERVICE = KNOWLEDGE_SERVICE_KEY
WIKI_FEATURE_ID = "product.wiki"


class WikiFeatureHost(Protocol):
    """Minimum host surface required by the Wiki bundle."""

    knowledge_service: KnowledgeService | None
    session_store: Any
    workspace_store: Any
    security_service: Any
    _auxiliary_providers: list[LLMProvider]


@dataclass(frozen=True, slots=True)
class WikiFeatureBundle:
    """Definition and candidate compatibility view for one Wiki generation."""

    definition: FeatureDefinition
    provider: LocalWikiProvider

    @property
    def store(self) -> WikiStore:
        return self.provider.store

    @property
    def compiler(self) -> WikiCompiler:
        return self.provider.compiler

    @property
    def querier(self) -> WikiQuerier:
        return self.provider.querier

    @property
    def summarizer(self) -> WikiSummarizer:
        return self.provider.summarizer

    @property
    def manager(self) -> WikiSessionManager:
        return self.provider.manager


def _bind_tool_lease(tool: FunctionTool, context: FeatureInstallContext) -> None:
    handler = tool.handler
    handler_is_async = tool.is_async

    async def run(args: dict[str, Any]) -> Any:
        async with context.acquire_lease(f"tool:{tool.name}"):
            result = (
                handler(args)
                if handler_is_async
                else await asyncio.to_thread(handler, args)
            )
            return await result if inspect.isawaitable(result) else result

    tool.handler = run
    tool.is_async = True


def _provider_for_profile(
    provider_factory: Callable[[Any], LLMProvider] | None,
    profile: Any,
) -> LLMProvider:
    if provider_factory is None:
        raise RuntimeError("Wiki model profile requires a provider factory")
    return provider_factory(profile)


def build_wiki_feature(
    host: WikiFeatureHost,
    registry: Registry,
    *,
    provider: LLMProvider,
    config: WikiConfig | None = None,
    storage_root: Any = None,
    session_store: Any = None,
    workspace_store: Any = None,
    security_service: Any = None,
    provider_factory: Callable[[Any], LLMProvider] | None = None,
    runtime: FeatureRuntime | None = None,
    activate: bool = False,
    desired_config_revision: int = 1,
) -> WikiFeatureBundle:
    """Build Wiki's stable service boundary and reversible feature definition.

    ``activate=True`` is the compatibility mode used by synchronous hosts that
    expose Wiki tools before ASGI lifespan startup.  Normal callers can return
    the definition and activate it in the host's startup phase.
    """
    cfg = config or WikiConfig()
    effective_session_store = session_store or host.session_store
    effective_workspace_store = workspace_store or host.workspace_store
    effective_security_service = security_service or host.security_service
    wiki_provider = provider
    explicit_model = str(cfg.model or "").strip()
    if explicit_model:
        profile = getattr(getattr(host, "config", None), "model_profiles", {}).get(explicit_model)
        if profile is not None and getattr(profile, "api_key", ""):
            wiki_provider = _provider_for_profile(provider_factory, profile)
            host._auxiliary_providers.append(wiki_provider)

    provider_for_owner: Callable[[str], LLMProvider] | None = None
    if not explicit_model or wiki_provider is provider:
        from crew.core.runctx import current_provider

        def resolve_owner(owner_account_id: str) -> LLMProvider:
            current = current_provider.get()
            if current is not None:
                return current
            resolver = getattr(host, "owner_team_provider", None)
            if callable(resolver):
                return resolver(owner_account_id)
            return provider

        provider_for_owner = resolve_owner

    def make_provider() -> LocalWikiProvider:
        store = FileSystemWikiStore(storage_root=storage_root)
        manager = WikiSessionManager(store=store)
        summarizer = WikiSummarizer(
            store,
            wiki_provider,
            provider_for_owner=provider_for_owner,
        )
        compiler = WikiCompiler(
            store,
            wiki_provider,
            summarizer=summarizer,
            provider_for_owner=provider_for_owner,
        )
        return LocalWikiProvider(
            WikiProviderComponents(
                store=store,
                compiler=compiler,
                querier=WikiQuerier(store),
                summarizer=summarizer,
                manager=manager,
            ),
            config=cfg,
            security_service=effective_security_service,
        )

    candidate = make_provider()
    pending = [candidate]

    def next_provider() -> LocalWikiProvider:
        return pending.pop() if pending else make_provider()

    async def install(context: FeatureInstallContext) -> None:
        candidate_provider = next_provider()
        # The candidate is always created by this Bundle. Register its close
        # before any service/context/tool side effect, whether it is selected
        # for this generation or discarded in favor of a host override.
        candidate_token = context.register_disposer(
            candidate_provider.close,
            label="resource:wiki.candidate-provider",
        )
        sentinel = getattr(host, "_UNSET_KNOWLEDGE_SERVICE", None)
        override = getattr(host, "_knowledge_service_override", sentinel)
        if sentinel is not None and override is not sentinel:
            current = override
            explicit_binding = current is not None
        else:
            current = getattr(host, "knowledge_service", None)
            # The normal host resolves to None before activation and to the old
            # service only while it is still active. Any non-None value from a
            # host without the sentinel is an explicitly owned binding.
            explicit_binding = current is not None
        active_service = current if explicit_binding else candidate_provider
        components = getattr(active_service, "components", None)
        if not isinstance(components, WikiProviderComponents):
            raise TypeError("Wiki host KnowledgeService must expose migration components")
        context.register_service(
            KNOWLEDGE_SERVICE,
            active_service,
            label="service:knowledge",
        )
        if explicit_binding:
            # Do not retain an unused local Store for the lifetime of an
            # external binding. The already-registered token remains a safe
            # no-op during rollback/teardown.
            await candidate_token.dispose()
        compiler = components.compiler
        previous_task_factory = compiler.task_factory

        def task_factory(awaitable):
            return context.create_task(awaitable, name="wiki.home-intro")

        context.register_disposer(
            lambda: compiler.restore_task_factory(task_factory, previous_task_factory),
            label="binding:wiki.task-factory",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        compiler.bind_task_factory(task_factory)
        context.register_context_contributor(
            ContextContributor(
                contributor_id="wiki.agent.context",
                handler=build_wiki_agent_context_contributor(
                    components.manager,
                    effective_session_store,
                ),
                phase=ContextPhase.PROMPT,
                priority=100,
                predicate=lambda envelope: "wiki"
                in tuple(envelope.params.get("_context_tags") or ()),
                model_visible=True,
                persistent=True,
                description="Active knowledge base context for Wiki agents",
            )
        )

        def take_pending_cards(envelope):
            manager = components.manager
            cards = manager.take_pending_cards(
                envelope.session_id,
                owner_account_id=envelope.user_id,
            )
            return FeatureEvent("wiki", "cards", 1, {"pages": cards}) if cards else None

        def take_pending_changes(envelope):
            manager = components.manager
            changes = manager.take_pending_changes(
                envelope.session_id,
                owner_account_id=envelope.user_id,
            )
            return FeatureEvent("wiki", "changed", 1, {"changes": changes}) if changes else None

        context.register_event_contributor(
            FeatureEventContributor(
                contributor_id="wiki.session.cards",
                handler=take_pending_cards,
                priority=100,
                description="Pending Wiki cards for the current session",
            )
        )
        context.register_event_contributor(
            FeatureEventContributor(
                contributor_id="wiki.session.changes",
                handler=take_pending_changes,
                priority=200,
                description="Pending Wiki changes for the current session",
            )
        )
        tools = build_wiki_tools(
            components.store,
            components.compiler,
            components.querier,
            components.manager,
            config=cfg,
            session_store=effective_session_store,
            workspace_store=effective_workspace_store,
            security_service=effective_security_service,
        )
        for tool in tools:
            _bind_tool_lease(tool, context)
            # The disposer compares object identity, preventing an old
            # Generation from removing a replacement with the same tool name.
            context.register_disposer(
                lambda tool=tool: registry.unregister(tool.name, expected=tool),
                label=f"tool:{tool.name}",
                phase=RegistrationPhase.CONTRIBUTION,
            )
            registry.register(tool)

    definition = FeatureDefinition(
        WIKI_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            WIKI_FEATURE_ID,
            provides=(KNOWLEDGE_SERVICE,),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        # Registry registrations are name-addressed and cannot stage two
        # generations with the same tool names.  Restart drains/removes the
        # old generation before installing the next one and preserves recovery.
        update_strategy=FeatureUpdateStrategy.RESTART,
        required_by_product=False,
    )
    bundle = WikiFeatureBundle(definition=definition, provider=candidate)
    if activate:
        if runtime is None:
            raise ValueError("activate=True requires a FeatureRuntime")
        record = run_async_compat(runtime.activate(definition))
        if record.state.value != "active" or record.scope is None:
            raise RuntimeError(
                f"Wiki Feature 激活失败: state={record.state.value} error={record.error}"
            )
    return bundle


__all__ = [
    "KNOWLEDGE_SERVICE",
    "KNOWLEDGE_SERVICE_KEY",
    "WIKI_FEATURE_ID",
    "KnowledgeService",
    "LocalWikiProvider",
    "WikiFeatureBundle",
    "WikiFeatureHost",
    "build_wiki_feature",
]
