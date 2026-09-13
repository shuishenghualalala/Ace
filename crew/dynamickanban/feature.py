"""Dynamic Kanban feature bundle and its explicit consumer boundary."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from crew.dynamickanban.manager import DynamicKanbanManager
from crew.dynamickanban.store import SQLiteKanbanStore
from crew.features import (
    ExecutionDriver,
    FeatureDefinition,
    FeatureInstallContext,
    FeatureServiceDependencies,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    ServiceKey,
)

DYNAMIC_KANBAN_FEATURE_ID = "product.dynamic-kanban"
DYNAMIC_KANBAN_MODE = "dynamic_kanban"
DYNAMIC_KANBAN_SERVICE_KEY = ServiceKey("dynamic-kanban", version=1)


class DynamicKanbanHost(Protocol):
    dynamic_kanban: DynamicKanbanManager | None
    dynamic_kanban_consumer: DynamicKanbanConsumer | None


class DynamicKanbanConsumer:
    """Small, explicit Team-facing API over one generation's Store.

    Every synchronous operation acquires and releases a generation lease.  The
    consumer intentionally exposes only the Store methods used by TeamPlanStore.
    """

    def __init__(self, store: SQLiteKanbanStore, scope: Any) -> None:
        self._store = store
        self._scope = scope

    def _scoped(self, owner_account_id: str) -> SQLiteKanbanStore:
        return self._store.for_owner(owner_account_id)

    def _call(self, label: str, fn: Callable[[], Any]) -> Any:
        lease = self._scope.acquire_lease(label)
        try:
            return fn()
        finally:
            lease.release()

    def for_owner(self, owner_account_id: str) -> "_OwnerConsumer":
        return _OwnerConsumer(self, owner_account_id)

    def list_workflows_by_session_prefix(self, session_id: str, *, owner_account_id: str) -> Any:
        return self._call("team:kanban:hydrate", lambda: self._scoped(owner_account_id).list_workflows_by_session_prefix(session_id))

    def get_board_state(self, workflow_id: str, *, owner_account_id: str) -> Any:
        return self._call("team:kanban:board", lambda: self._scoped(owner_account_id).get_board_state(workflow_id))

    def create_workflow_graph(self, session_id: str, title: str, *, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:persist", lambda: self._scoped(owner_account_id).create_workflow_graph(session_id, title, **kwargs))

    def create_workflow(self, *, session_id: str, title: str, context: dict[str, Any], owner_account_id: str) -> Any:
        return self._call("team:kanban:persist", lambda: self._scoped(owner_account_id).create_workflow(session_id=session_id, title=title, context=context))

    def add_task(self, workflow_id: str, *, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:persist", lambda: self._scoped(owner_account_id).add_task(workflow_id, **kwargs))

    def add_event(self, workflow_id: str, event_type: str, *, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:persist", lambda: self._scoped(owner_account_id).add_event(workflow_id, event_type, **kwargs))

    def update_task_status(self, task_id: str, status: str, *, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:sync", lambda: self._scoped(owner_account_id).update_task_status(task_id, status, **kwargs))

    def update_workflow_status(self, workflow_id: str, status: str, *, owner_account_id: str) -> Any:
        return self._call("team:kanban:sync", lambda: self._scoped(owner_account_id).update_workflow_status(workflow_id, status))

    def list_runs(self, task_id: str, *, owner_account_id: str) -> Any:
        return self._call("team:kanban:runs", lambda: self._scoped(owner_account_id).list_runs(task_id))
    def get_workflow(self, workflow_id: str, *, owner_account_id: str) -> Any:
        return self._call("team:kanban:workflow", lambda: self._scoped(owner_account_id).get_workflow(workflow_id))
    def apply_task_reassignment_revision(self, *args: Any, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:revision", lambda: self._scoped(owner_account_id).apply_task_reassignment_revision(*args, **kwargs))
    def save_workflow_plan_revision(self, *args: Any, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:revision", lambda: self._scoped(owner_account_id).save_workflow_plan_revision(*args, **kwargs))
    def apply_workflow_graph_revision(self, *args: Any, owner_account_id: str, **kwargs: Any) -> Any:
        return self._call("team:kanban:revision", lambda: self._scoped(owner_account_id).apply_workflow_graph_revision(*args, **kwargs))


class _OwnerConsumer:
    """Owner-bound view used by synchronous Team manager paths."""
    def __init__(self, consumer: DynamicKanbanConsumer, owner: str) -> None:
        self._consumer, self._owner = consumer, owner
    def list_workflows_by_session_prefix(self, session_id: str) -> Any:
        return self._consumer.list_workflows_by_session_prefix(session_id, owner_account_id=self._owner)
    def get_board_state(self, workflow_id: str) -> Any:
        return self._consumer.get_board_state(workflow_id, owner_account_id=self._owner)
    def create_workflow_graph(self, session_id: str, title: str, **kwargs: Any) -> Any:
        return self._consumer.create_workflow_graph(session_id, title, owner_account_id=self._owner, **kwargs)
    def create_workflow(self, **kwargs: Any) -> Any:
        return self._consumer.create_workflow(owner_account_id=self._owner, **kwargs)
    def add_task(self, workflow_id: str, **kwargs: Any) -> Any:
        return self._consumer.add_task(workflow_id, owner_account_id=self._owner, **kwargs)
    def add_event(self, workflow_id: str, event_type: str, **kwargs: Any) -> Any:
        return self._consumer.add_event(workflow_id, event_type, owner_account_id=self._owner, **kwargs)
    def update_task_status(self, task_id: str, status: str, **kwargs: Any) -> Any:
        return self._consumer.update_task_status(task_id, status, owner_account_id=self._owner, **kwargs)
    def update_workflow_status(self, workflow_id: str, status: str) -> Any:
        return self._consumer.update_workflow_status(workflow_id, status, owner_account_id=self._owner)
    def list_runs(self, task_id: str) -> Any:
        return self._consumer.list_runs(task_id, owner_account_id=self._owner)
    def get_workflow(self, workflow_id: str) -> Any:
        return self._consumer.get_workflow(workflow_id, owner_account_id=self._owner)
    def apply_task_reassignment_revision(self, *args: Any, **kwargs: Any) -> Any:
        return self._consumer.apply_task_reassignment_revision(*args, owner_account_id=self._owner, **kwargs)
    def save_workflow_plan_revision(self, *args: Any, **kwargs: Any) -> Any:
        return self._consumer.save_workflow_plan_revision(*args, owner_account_id=self._owner, **kwargs)
    def apply_workflow_graph_revision(self, *args: Any, **kwargs: Any) -> Any:
        return self._consumer.apply_workflow_graph_revision(*args, owner_account_id=self._owner, **kwargs)


@dataclass(frozen=True, slots=True)
class DynamicKanbanService:
    manager: DynamicKanbanManager
    consumer: DynamicKanbanConsumer


@dataclass(frozen=True, slots=True)
class DynamicKanbanFeatureBundle:
    definition: FeatureDefinition
    consumer_provider: Callable[[], DynamicKanbanConsumer | None]


def build_dynamic_kanban_feature(
    host: DynamicKanbanHost,
    *,
    db_path: str,
    provider: Any,
    base_registry: Any,
    session_store: Any,
    memory: Any,
    plugins: Any,
    config: Any,
    agent_factory: Any = None,
    on_runtime_chunk: Any = None,
    provider_for_owner: Any = None,
    wal_enabled: bool = True,
    legacy_db_path: str | None = None,
    desired_config_revision: int = 1,
) -> DynamicKanbanFeatureBundle:
    """Create a restartable Bundle; activation owns fresh runtime resources."""
    def new_manager() -> DynamicKanbanManager:
        # legacy_db_path 触发 store 构造器内的 copy-on-first-activate（ADR-0038）：
        # 每次激活重建 store 时重复传入是安全的——目标库已有行即整体跳过。
        store = SQLiteKanbanStore(
            db_path,
            wal_enabled=wal_enabled,
            legacy_db_path=legacy_db_path,
        )
        try:
            return DynamicKanbanManager(
                store=store,
                provider=provider,
                base_registry=base_registry,
                session_store=session_store,
                memory=memory,
                plugins=plugins,
                config=config,
                agent_factory=agent_factory,
                on_runtime_chunk=on_runtime_chunk,
                provider_for_owner=provider_for_owner,
            )
        except BaseException:
            store.close()
            raise

    async def install(context: FeatureInstallContext) -> None:
        manager = new_manager()
        # Register ownership before constructing any dependent contribution so
        # every activation failure follows Runtime's normal rollback path.
        context.register_disposer(manager.close, label="resource:dynamic-kanban.manager")
        consumer = DynamicKanbanConsumer(manager.store, context.scope)
        service = DynamicKanbanService(manager, consumer)
        manager.bind_feature_scope(context.scope)
        context.register_disposer(
            lambda: (
                setattr(host, "dynamic_kanban", None)
                if getattr(host, "dynamic_kanban", None) is manager
                else None
            ),
            label="binding:dynamic-kanban.manager",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        context.register_disposer(
            lambda: (
                setattr(host, "dynamic_kanban_consumer", None)
                if getattr(host, "dynamic_kanban_consumer", None) is consumer
                else None
            ),
            label="binding:dynamic-kanban.consumer",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        context.register_service(DYNAMIC_KANBAN_SERVICE_KEY, service, label="service:dynamic-kanban")
        from crew.dynamickanban.routes import create_dynamic_kanban_router
        context.register_api_router(
            create_dynamic_kanban_router(host, service=service),
            contribution_id=DYNAMIC_KANBAN_FEATURE_ID,
            label="route:dynamic-kanban",
        )

        async def execute(envelope: Any):
            async with aclosing(manager.interact(envelope)) as stream:
                async for chunk in stream:
                    yield chunk

        context.register_execution_driver(
            ExecutionDriver(
                mode=DYNAMIC_KANBAN_MODE,
                execute=execute,
                capabilities=("dynamic-kanban.execute",),
                description="Dynamic Kanban workflow execution",
            ),
            label="execution-driver:dynamic-kanban",
        )
        host.dynamic_kanban = manager
        host.dynamic_kanban_consumer = consumer
    definition = FeatureDefinition(
        DYNAMIC_KANBAN_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            DYNAMIC_KANBAN_FEATURE_ID,
            provides=(DYNAMIC_KANBAN_SERVICE_KEY,),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.CANCEL,
        update_strategy=FeatureUpdateStrategy.RESTART,
    )
    def consumer_provider() -> DynamicKanbanConsumer | None:
        plugins = getattr(host, "plugins", None)
        resolver = getattr(plugins, "resolve_service", None)
        if callable(resolver):
            service = resolver(DYNAMIC_KANBAN_SERVICE_KEY, default=None)
            return service.consumer if service is not None else None
        return None

    return DynamicKanbanFeatureBundle(
        definition=definition,
        consumer_provider=consumer_provider,
    )


__all__ = [
    "DYNAMIC_KANBAN_FEATURE_ID",
    "DYNAMIC_KANBAN_MODE",
    "DYNAMIC_KANBAN_SERVICE_KEY",
    "DynamicKanbanConsumer",
    "DynamicKanbanService",
    "DynamicKanbanFeatureBundle",
    "build_dynamic_kanban_feature",
]
