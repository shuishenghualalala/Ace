"""Sites Product Feature 的声明式装配与 Generation 所有权。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from crew.agent.capabilities import CapabilityProfileRegistry
from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
)
from crew.sites.capabilities import register_site_capability_profiles
from crew.sites.manager import SiteManager
from crew.sites.store import SQLiteSiteStore
from crew.tools.blueprint_tools import build_blueprint_tools
from crew.tools.registry import FunctionTool, Registry
from crew.tools.site_tools import build_site_tools

SITES_FEATURE_ID = "product.sites"


class SitesFeatureHost(Protocol):
    """Sites Bundle 使用的最小宿主能力。"""

    sites: SiteManager | None
    capability_profiles: CapabilityProfileRegistry


@dataclass(frozen=True, slots=True)
class SitesFeatureBundle:
    """Sites 定义和 build_app 后仍可检查的候选 Manager。"""

    definition: FeatureDefinition
    manager: SiteManager | None


def _manager_factory(
    db_path: str,
    wal_enabled: bool,
) -> Callable[[], SiteManager]:
    def create() -> SiteManager:
        return SiteManager(SQLiteSiteStore(db_path, wal_enabled=wal_enabled))

    return create


def _bind_tool_lease(tool: FunctionTool, context: FeatureInstallContext) -> None:
    handler = tool.handler

    async def run(args: dict[str, Any]) -> Any:
        async with context.acquire_lease(f"tool:{tool.name}"):
            result = handler(args)
            return await result if inspect.isawaitable(result) else result

    tool.handler = run
    tool.is_async = True


def _register_tools(
    registry: Registry,
    manager: SiteManager,
    context: FeatureInstallContext,
    *,
    workspace_store: Any = None,
    security_service: Any = None,
) -> None:
    """Register both Sites tool families and attach generation leases."""

    tools = (
        *build_site_tools(
            manager,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        *build_blueprint_tools(
            manager,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
    )
    for tool in tools:
        _bind_tool_lease(tool, context)
        registry.register(tool)
        context.register_disposer(
            lambda tool=tool: registry.unregister(tool.name, expected=tool),
            label=f"tool:{tool.name}",
            phase=RegistrationPhase.CONTRIBUTION,
        )


def build_sites_feature(
    host: SitesFeatureHost,
    registry: Registry,
    store: SQLiteSiteStore | None = None,
    *,
    db_path: str | None = None,
    wal_enabled: bool = True,
    enabled: bool = True,
    desired_config_revision: int = 1,
    manager_factory: Callable[[], SiteManager] | None = None,
    workspace_store: Any = None,
    security_service: Any = None,
) -> SitesFeatureBundle:
    """Build one reusable definition; each install receives a fresh Manager.

    The first candidate is exposed as a compatibility view by ``build_app``.  A
    failed activation consumes and closes that candidate; retries create a new
    Manager and therefore never reuse closed SQLite connections.  The host
    binding captured during construction is only a predecessor marker; it is
    not used as the candidate and is never implicitly closed as an external
    dependency.
    """

    if manager_factory is None:
        if store is not None:
            db_path = str(store.db_path)
            wal_enabled = store.wal_enabled

            def manager_factory() -> SiteManager:
                return SiteManager(SQLiteSiteStore(db_path, wal_enabled=wal_enabled))

        elif not db_path:
            raise ValueError("build_sites_feature requires db_path or store")
        else:
            manager_factory = _manager_factory(db_path, wal_enabled)
    factory = manager_factory
    predecessor = getattr(host, "sites", None)
    predecessor_marker = getattr(host, "_sites_feature_manager", None)
    candidate = SiteManager(store) if store is not None else factory()
    pending = [candidate]
    owned_managers: set[int] = {id(candidate)}

    def next_manager() -> SiteManager:
        manager = pending.pop() if pending else factory()
        owned_managers.add(id(manager))
        return manager

    async def install(context: FeatureInstallContext) -> None:
        candidate_manager = next_manager()
        current = getattr(host, "sites", None)
        current_is_owned = current is not None and id(current) in owned_managers
        is_predecessor = current is not None and (
            current is predecessor
            and predecessor_marker is current
        )
        explicit_binding = (
            current is not None
            and current is not candidate_manager
            and not current_is_owned
            and not is_predecessor
        )
        manager = current if explicit_binding else candidate_manager
        owns_manager = not explicit_binding and id(manager) in owned_managers
        start_failed = False

        host.sites = manager
        host._sites_feature_manager = manager

        def clear_host_binding() -> None:
            # Identity check prevents an old Generation's disposer from clearing
            # a newer restart Generation's Manager.
            if host.sites is manager and not (explicit_binding and start_failed):
                host.sites = None
            if getattr(host, "_sites_feature_manager", None) is manager:
                host._sites_feature_manager = None

        context.register_disposer(
            clear_host_binding,
            label="binding:sites.manager",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        if explicit_binding:
            # The independently-created candidate is still owned by this
            # Generation even when the host supplied another Manager.
            context.register_disposer(
                candidate_manager.close,
                label="resource:sites.unused-candidate",
            )

        if not enabled:
            if owns_manager:
                context.register_disposer(
                    manager.close,
                    label="resource:sites.manager",
                )
            elif explicit_binding:
                context.register_disposer(
                    manager.stop,
                    label="resource:sites.scheduler",
                )
            return

        profiles = register_site_capability_profiles(host.capability_profiles)
        for profile in profiles:
            context.register_disposer(
                lambda profile=profile: host.capability_profiles.unregister(
                    profile.id,
                    expected=profile,
                ),
                label=f"capability:{profile.id}",
                phase=RegistrationPhase.CONTRIBUTION,
            )

        _register_tools(
            registry,
            manager,
            context,
            workspace_store=workspace_store,
            security_service=security_service,
        )

        if owns_manager:
            context.register_disposer(
                manager.close,
                label="resource:sites.manager",
            )
        else:
            context.register_disposer(
                manager.stop,
                label="resource:sites.scheduler",
            )
        try:
            pending = manager.start()
            if inspect.isawaitable(pending):
                await pending
        except Exception:
            start_failed = True
            raise

    definition = FeatureDefinition(
        SITES_FEATURE_ID,
        install,
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.CANCEL,
        update_strategy=FeatureUpdateStrategy.RESTART,
        required_by_product=False,
    )
    return SitesFeatureBundle(definition=definition, manager=candidate)


__all__ = [
    "SITES_FEATURE_ID",
    "SitesFeatureBundle",
    "SitesFeatureHost",
    "build_sites_feature",
]
