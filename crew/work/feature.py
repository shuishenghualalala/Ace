"""Work Product Feature Bundle and Generation-owned resource assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    run_async_compat,
)
from crew.wiki.service import KNOWLEDGE_SERVICE_KEY as WORK_KNOWLEDGE_SERVICE_KEY

from .briefs import WorkBriefStore
from .items import WorkItemStore
from .knowledge import KnowledgeServiceAcquirer, WorkKnowledgeStore
from .preferences import WorkPreferenceStore
from .references import WorkReferenceStore
from .service import (
    WORK_SERVICE_KEY,
    WorkService,
)
from .settings import WorkSettingsStore
from .sources import WorkSourceAdapter, WorkSourceStore
from .templates import WorkTemplateStore

WORK_FEATURE_ID = "product.work"


class WorkFeatureHost(Protocol):
    """Small host surface required by the Work Bundle."""

    work_service: WorkService | None
    session_store: Any
    workspace_store: Any


@dataclass(frozen=True, slots=True)
class WorkFeatureBundle:
    """Feature definition and the first candidate Service for compatibility."""

    definition: FeatureDefinition
    service: WorkService

    @property
    def stores(self) -> tuple[Any, ...]:
        """Return all Work-owned stores in deterministic close order."""
        service = self.service
        return (
            service.briefs,
            service.sources,
            service.preferences,
            service.references,
            service.items,
            service.knowledge,
            service.settings,
            service.templates,
        )


def _work_router(host: WorkFeatureHost) -> Any:
    """Load the Work-owned route contribution lazily during host assembly."""
    from .routes import create_work_router

    return create_work_router(host)


def _default_knowledge_acquirer(host: WorkFeatureHost) -> KnowledgeServiceAcquirer:
    """Resolve and lease the current optional KnowledgeService per call."""

    def acquire() -> Any:
        plugins = getattr(host, "plugins", None)
        service_key = WORK_KNOWLEDGE_SERVICE_KEY
        acquire_lease = getattr(plugins, "acquire_service_lease", None)
        if callable(acquire_lease):
            return acquire_lease(service_key, label="work:knowledge")
        resolver = getattr(plugins, "resolve_service", None)
        if callable(resolver):
            service = resolver(service_key, default=None)
            return service
        service = getattr(host, "knowledge_service", None)
        if service is not None:
            return service
        return None

    return acquire


def _store_factory(
    host: WorkFeatureHost,
    *,
    db_path: str | Path,
    wal_enabled: bool,
    knowledge_service_acquirer: KnowledgeServiceAcquirer,
    organization_provider: Callable[[], Any] | None,
    preference_extractor: Any,
    preference_notifier: Any,
    approved_source_keys: set[str],
    source_adapters: Mapping[str, WorkSourceAdapter],
    hook_registry: Any,
) -> WorkService:
    """Create one complete Work generation without publishing side effects."""

    session_store = getattr(host, "session_store", None)
    workspace_store = getattr(host, "workspace_store", None)
    if session_store is None or workspace_store is None:
        raise ValueError("Work Feature requires session_store and workspace_store")
    return WorkService(
        references=WorkReferenceStore(
            db_path,
            session_store=session_store,
            wal_enabled=wal_enabled,
        ),
        preferences=WorkPreferenceStore(db_path, wal_enabled=wal_enabled),
        items=WorkItemStore(db_path, wal_enabled=wal_enabled),
        sources=WorkSourceStore(
            db_path,
            approved_source_keys=approved_source_keys,
            adapters=source_adapters,
            wal_enabled=wal_enabled,
        ),
        briefs=WorkBriefStore(db_path, wal_enabled=wal_enabled),
        settings=WorkSettingsStore(
            db_path,
            workspace_store=workspace_store,
            wal_enabled=wal_enabled,
        ),
        templates=WorkTemplateStore(db_path, wal_enabled=wal_enabled),
        knowledge=WorkKnowledgeStore(
            db_path,
            knowledge_service_acquirer=knowledge_service_acquirer,
            organization_provider=organization_provider,
            wal_enabled=wal_enabled,
        ),
        session_store=session_store,
        workspace_store=workspace_store,
        preference_extractor=preference_extractor,
        preference_notifier=preference_notifier,
        hook_registry=hook_registry,
    )


def build_work_feature(
    host: WorkFeatureHost,
    *,
    db_path: str | Path | None = None,
    wal_enabled: bool = True,
    enabled: bool = True,
    desired_config_revision: int = 1,
    knowledge_service_acquirer: KnowledgeServiceAcquirer | None = None,
    organization_provider: Callable[[], Any] | None = None,
    preference_extractor: Any = None,
    preference_notifier: Any = None,
    approved_source_keys: set[str] | None = None,
    source_adapters: Mapping[str, WorkSourceAdapter] | None = None,
    hook_registry: Any = None,
    service_factory: Callable[[], WorkService] | None = None,
    runtime: FeatureRuntime | None = None,
    activate: bool = False,
) -> WorkFeatureBundle:
    """Build a Work Feature definition; activation owns one fresh generation.

    ``service_factory`` is primarily useful for contract tests and embedded
    deployments.  The normal path creates all eight SQLite stores from the
    host database path and gives the generation sole responsibility for their
    stop/close lifecycle.  Existing business data is never removed on disable.
    """

    if db_path is None:
        config = getattr(host, "config", None)
        db_path = getattr(config, "db_path", None)
    if db_path is None and service_factory is None:
        raise ValueError("build_work_feature requires db_path")
    if knowledge_service_acquirer is None:
        knowledge_service_acquirer = _default_knowledge_acquirer(host)
    if preference_notifier is None:
        notify = getattr(host, "_notify_owner_fn", None)
        if callable(notify):
            async def preference_notifier(owner: str, payload: dict[str, Any]) -> None:
                await notify(owner, payload)
    approved = set(approved_source_keys or ())
    adapters = dict(source_adapters or {})
    factory = service_factory
    if factory is None:
        assert db_path is not None

        def factory() -> WorkService:
            return _store_factory(
                host,
                db_path=db_path,
                wal_enabled=wal_enabled,
                knowledge_service_acquirer=knowledge_service_acquirer,
                organization_provider=organization_provider,
                preference_extractor=preference_extractor,
                preference_notifier=preference_notifier,
                approved_source_keys=approved,
                source_adapters=adapters,
                hook_registry=hook_registry,
            )

    candidate = factory()
    pending = [candidate]

    def next_service() -> WorkService:
        return pending.pop() if pending else factory()

    route = _work_router(host)

    async def install(context: FeatureInstallContext) -> None:
        candidate_service = next_service()
        # Register cleanup before publishing any service/route or starting the
        # agent:end hook. CONTRIBUTION phase guarantees stop before close.
        context.register_disposer(
            candidate_service.close,
            label="resource:work.stores",
            phase=RegistrationPhase.RESOURCE,
        )
        current_service = getattr(host, "work_service", None)
        owned_marker = getattr(host, "_work_feature_service", None)
        explicit_binding = current_service is not None and (
            current_service is not candidate_service and current_service is not owned_marker
        )
        service = current_service if explicit_binding else candidate_service

        if not explicit_binding and enabled:
            context.register_disposer(
                lambda: service.stop(),
                label="contribution:work.agent-end-hook",
                phase=RegistrationPhase.CONTRIBUTION,
            )

        if explicit_binding:
            # Embedded hosts may inject a service owned by another lifecycle.
            # Publish it through the registry without changing its compatibility
            # binding or taking responsibility for stop/close.
            if not enabled:
                return
        else:
            host.work_service = service if enabled else None
            host._work_feature_service = service if enabled else None

        def clear_binding() -> None:
            if explicit_binding:
                return
            if getattr(host, "work_service", None) is service:
                host.work_service = None
            if getattr(host, "_work_feature_service", None) is service:
                host._work_feature_service = None

        context.register_disposer(
            clear_binding,
            label="binding:work.service",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        if not enabled:
            return

        context.register_service(
            WORK_SERVICE_KEY,
            service,
            label="service:work",
        )
        context.register_api_router(
            route,
            contribution_id=WORK_FEATURE_ID,
            label="route:work",
        )
        if not explicit_binding:
            await service.start()

    definition = FeatureDefinition(
        WORK_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            WORK_FEATURE_ID,
            optional=(WORK_KNOWLEDGE_SERVICE_KEY,),
            provides=(WORK_SERVICE_KEY,) if enabled else (),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.RESTART,
        required_by_product=False,
    )
    bundle = WorkFeatureBundle(definition=definition, service=candidate)
    if activate:
        if runtime is None:
            raise ValueError("activate=True requires a FeatureRuntime")
        record = run_async_compat(runtime.activate(definition))
        if record.state is not FeatureState.ACTIVE or record.scope is None:
            raise RuntimeError(
                f"Work Feature 激活失败: state={record.state.value} error={record.error}"
            )
    return bundle


__all__ = [
    "WORK_FEATURE_ID",
    "WORK_SERVICE_KEY",
    "WorkFeatureBundle",
    "WorkFeatureHost",
    "build_work_feature",
]
