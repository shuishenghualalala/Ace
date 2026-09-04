"""Scoped services, dependency resolution, and lifecycle ownership."""

from __future__ import annotations

import asyncio

import pytest

from crew.features import (
    FeatureDependencyGraph,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
    FeatureServiceDependencies,
    ServiceConflictError,
    ServiceKey,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceScopeKind,
    ServiceScopePath,
)


def _active_scope(feature_id: str, sequence: int = 1) -> FeatureScope:
    scope = FeatureScope(FeatureGeneration(feature_id, sequence))
    scope.activate()
    return scope


def test_service_scope_path_validates_nested_identity():
    assert ServiceScopePath.session(" workspace ", " user ", " session ") == ServiceScopePath(
        workspace_id="workspace",
        user_id="user",
        session_id="session",
    )
    with pytest.raises(ValueError, match="workspace_id"):
        ServiceScopePath(user_id="user")
    with pytest.raises(ValueError, match="user_id"):
        ServiceScopePath(workspace_id="workspace", session_id="session")


def test_registry_resolves_nearest_service_scope():
    registry = ServiceRegistry()
    knowledge = ServiceKey[str]("knowledge")
    target = ServiceScopePath.session("workspace-a", "user-a", "session-a")

    registry.register(_active_scope("global-provider"), knowledge, "global")
    registry.register(
        _active_scope("workspace-provider"),
        knowledge,
        "workspace",
        scope_kind=ServiceScopeKind.WORKSPACE,
        scope_path=ServiceScopePath.workspace("workspace-a"),
    )
    registry.register(
        _active_scope("user-provider"),
        knowledge,
        "user",
        scope_kind=ServiceScopeKind.USER,
        scope_path=ServiceScopePath.user("workspace-a", "user-a"),
    )
    registry.register(
        _active_scope("session-provider"),
        knowledge,
        "session",
        scope_kind=ServiceScopeKind.SESSION,
        scope_path=target,
    )

    assert registry.resolve(knowledge, target) == "session"
    assert registry.resolve(
        knowledge, ServiceScopePath.user("workspace-a", "user-a")
    ) == "user"
    assert registry.resolve(
        knowledge, ServiceScopePath.workspace("workspace-a")
    ) == "workspace"
    assert registry.resolve(
        knowledge, ServiceScopePath.workspace("workspace-b")
    ) == "global"


async def test_activating_service_is_staged_and_rollback_removes_it():
    registry = ServiceRegistry()
    storage = ServiceKey[object]("storage")
    scope = FeatureScope(FeatureGeneration("storage-provider", 1))

    registry.register(scope, storage, object())

    assert not registry.contains(storage)
    assert len(registry.bindings()) == 1
    assert not registry.bindings()[0].visible

    await scope.rollback()

    assert registry.bindings() == ()
    assert not registry.contains(storage)


async def test_service_becomes_unavailable_before_owned_async_resource_finishes():
    registry = ServiceRegistry()
    browser = ServiceKey[object]("browser")
    scope = FeatureScope(FeatureGeneration("browser-provider", 1))
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def dispose_process() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    scope.register(dispose_process, label="process:browser")
    registry.register(scope, browser, object())
    scope.activate()
    assert registry.contains(browser)

    disposal = asyncio.create_task(scope.dispose())
    await cleanup_started.wait()

    assert not registry.contains(browser)
    assert not disposal.done()

    cleanup_release.set()
    await disposal
    assert registry.bindings() == ()


async def test_registration_token_removes_only_its_owned_service():
    registry = ServiceRegistry()
    key = ServiceKey[str]("provider")
    first_scope = _active_scope("first", 1)
    first_token = registry.register(first_scope, key, "first")

    with pytest.raises(ServiceConflictError, match="first@g1"):
        registry.register(_active_scope("second", 1), key, "second")

    same_feature_active = _active_scope("first", 2)
    with pytest.raises(ServiceConflictError, match="first@g1"):
        registry.register(same_feature_active, key, "unexpected")

    await first_token.dispose()
    second_scope = _active_scope("second", 1)
    registry.register(second_scope, key, "second")

    await first_token.dispose()
    assert registry.resolve(key) == "second"


async def test_registry_lease_tracks_generation_and_drains_before_release():
    registry = ServiceRegistry()
    key = ServiceKey[str]("knowledge")
    closed: list[str] = []

    old = _active_scope("product.wiki", 1)
    old.register(lambda: closed.append("old"), label="resource:old")
    registry.register(old, key, "old")

    value, lease = registry.acquire_lease(key, label="gateway:wiki")
    assert value == "old"
    assert old.state is FeatureState.ACTIVE
    assert [item.label for item in old.active_leases] == ["gateway:wiki"]

    stopping = asyncio.create_task(old.stop(FeatureStopPolicy.DRAIN, timeout_seconds=None))
    async def wait_for_draining() -> None:
        while old.state is FeatureState.ACTIVE:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_draining(), timeout=1)
    assert old.state is FeatureState.DRAINING
    assert registry.acquire_lease(key) is None
    assert not stopping.done()
    assert closed == []

    lease.release()
    lease.release()
    await stopping
    assert old.state is FeatureState.DISPOSED
    assert closed == ["old"]
    assert registry.acquire_lease(key) is None


def test_registry_lease_returns_none_for_missing_or_inactive_service():
    registry = ServiceRegistry()
    key = ServiceKey[str]("knowledge")
    assert registry.acquire_lease(key) is None

    activating = FeatureScope(FeatureGeneration("product.wiki", 1))
    registry.register(activating, key, "staged")
    assert registry.acquire_lease(key) is None


async def test_registry_lease_resolves_new_generation_while_old_request_is_in_flight():
    registry = ServiceRegistry()
    key = ServiceKey[str]("knowledge")
    old = _active_scope("product.wiki", 1)
    old_closed = asyncio.Event()
    old.register(lambda: old_closed.set(), label="resource:old")
    registry.register(old, key, "old")
    new = FeatureScope(FeatureGeneration("product.wiki", 2))
    registry.register(new, key, "new")

    old_value, old_lease = registry.acquire_lease(key, label="in-flight")
    assert old_value == "old"
    new.activate()
    new_value, new_lease = registry.acquire_lease(key, label="new-request")
    assert new_value == "new"

    stopping = asyncio.create_task(old.stop(timeout_seconds=None))
    await asyncio.sleep(0)
    assert not stopping.done()
    assert not old_closed.is_set()
    assert registry.resolve(key) == "new"

    old_lease.release()
    await stopping
    assert old_closed.is_set()
    new_lease.release()
    await new.dispose()


async def test_new_generation_is_staged_then_atomically_replaces_old_service():
    registry = ServiceRegistry()
    key = ServiceKey[str]("provider")
    old_scope = _active_scope("provider-feature", 1)
    registry.register(old_scope, key, "old")
    new_scope = FeatureScope(FeatureGeneration("provider-feature", 2))
    registry.register(new_scope, key, "new")

    assert registry.resolve(key) == "old"
    assert [binding.visible for binding in registry.bindings()] == [True, False]

    new_scope.activate()
    assert registry.resolve(key) == "new"

    await old_scope.dispose()
    assert registry.resolve(key) == "new"
    assert [binding.generation.key for binding in registry.bindings()] == [
        "provider-feature@g2"
    ]


def test_dependency_graph_distinguishes_required_and_optional_services():
    registry = ServiceRegistry()
    graph = FeatureDependencyGraph()
    storage = ServiceKey[object]("storage")
    knowledge = ServiceKey[object]("knowledge")
    owner_model = ServiceKey[object]("owner-model")
    graph.add(
        FeatureServiceDependencies(
            "wiki",
            requires=(storage,),
            optional=(owner_model,),
            provides=(knowledge,),
        )
    )
    graph.add(FeatureServiceDependencies("storage-provider", provides=(storage,)))

    waiting = graph.resolve("wiki", registry)
    assert not waiting.ready
    assert waiting.missing_required == (storage,)
    assert waiting.missing_optional == (owner_model,)
    assert graph.providers(storage) == ("storage-provider",)

    registry.register(_active_scope("storage-provider"), storage, object())
    ready = graph.resolve("wiki", registry)

    assert ready.ready
    assert [binding.key for binding in ready.required] == [storage]
    assert ready.optional == ()
    assert ready.missing_optional == (owner_model,)


def test_dependency_graph_audits_services_without_any_declared_provider():
    graph = FeatureDependencyGraph()
    security = ServiceKey[object]("security")
    storage = ServiceKey[object]("storage")
    graph.add(
        FeatureServiceDependencies("wiki", requires=(security, storage))
    )
    graph.add(FeatureServiceDependencies("storage-provider", provides=(storage,)))

    assert graph.undeclared_required_services(host_services=(security,)) == {}
    assert graph.undeclared_required_services() == {"wiki": (security,)}


def test_dependency_graph_plans_batches_and_reports_cycles():
    storage = ServiceKey[object]("storage")
    knowledge = ServiceKey[object]("knowledge")
    graph = FeatureDependencyGraph()
    graph.add(FeatureServiceDependencies("storage", provides=(storage,)))
    graph.add(
        FeatureServiceDependencies("wiki", requires=(storage,), provides=(knowledge,))
    )

    plan = graph.plan_activation()
    assert plan.complete
    assert plan.batches == (("storage",), ("wiki",))

    left = ServiceKey[object]("left")
    right = ServiceKey[object]("right")
    cyclic = FeatureDependencyGraph()
    cyclic.add(FeatureServiceDependencies("left", requires=(right,), provides=(left,)))
    cyclic.add(FeatureServiceDependencies("right", requires=(left,), provides=(right,)))

    blocked = cyclic.plan_activation()
    assert not blocked.complete
    assert blocked.batches == ()
    assert [item.feature_id for item in blocked.blocked] == ["left", "right"]
    assert [item.unavailable_services for item in blocked.blocked] == [(right,), (left,)]


def test_dependency_declaration_rejects_ambiguous_or_conflicting_entries():
    storage = ServiceKey[object]("storage")
    with pytest.raises(ValueError, match="both required and optional"):
        FeatureServiceDependencies("wiki", requires=(storage,), optional=(storage,))
    with pytest.raises(ValueError, match="service it provides"):
        FeatureServiceDependencies("wiki", requires=(storage,), provides=(storage,))


def test_registry_raises_for_missing_service_and_supports_default_lookup():
    registry = ServiceRegistry()
    key = ServiceKey[str]("missing")

    with pytest.raises(ServiceNotFoundError, match="missing"):
        registry.resolve(key)
    assert registry.get(key, default="fallback") == "fallback"


def test_registry_lists_visible_keys_and_exact_scope_ownership():
    registry = ServiceRegistry()
    storage = ServiceKey[object]("storage")
    knowledge = ServiceKey[object]("knowledge")
    provider = _active_scope("provider")
    activating = FeatureScope(FeatureGeneration("activating", 1))

    registry.register(provider, storage, object())
    registry.register(activating, knowledge, object())

    assert registry.available_keys() == (storage,)
    owned = registry.bindings_owned_by(activating)
    assert [binding.key for binding in owned] == [knowledge]
    assert not owned[0].visible
