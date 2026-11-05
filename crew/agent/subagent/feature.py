"""Lifecycle-owned Feature Bundle for the subagent runtime.

The Bundle owns the runtime, persistence, notifications and model-facing
contributions for one Generation. The application only supplies the core
AgentFactory and keeps compatibility views for older callers.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request

from crew.agent.feature import AGENT_FACTORY_SERVICE_KEY
from crew.agent.subagent.context import (
    SubagentNotificationQueue,
    SubagentNotificationRouter,
    build_subagent_notification_handler,
)
from crew.agent.subagent.definition import to_preset_contribution
from crew.agent.subagent.registry import SubagentRegistry
from crew.agent.subagent.providers import InProcessForkProvider, InProcessSpawnProvider
from crew.agent.subagent.runtime import BatchSchedulePolicy, SubagentRuntime
from crew.agent.subagent.service import SUBAGENT_SERVICE_KEY, SubagentService
from crew.agent.subagent.store import SubagentStore
from crew.agent.subagent.tools import (
    ActiveSubagents,
    build_structured_output_schema,
    build_subagent_tools,
    handle_structured_output,
)
from crew.core.runctx import normalize_owner_account_id
from crew.features import (
    ContextContributor,
    ContextPhase,
    FeatureEvent,
    FeatureEventContributor,
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
from crew.features.runtime import FeatureLeaseUnavailableError
from crew.tools.registry import Registry

SUBAGENT_FEATURE_ID = "product.subagent"


class SubagentFeatureHost(Protocol):
    """Small host surface required by the Subagent Bundle."""

    config: Any
    plugins: Any
    registry: Registry
    tasks: Any
    agent_factory: Any
    subagent_registry: SubagentRegistry | None
    subagent_runtime: SubagentService | None
    subagent_active: ActiveSubagents | None
    subagent_store: SubagentStore | None
    subagent_tasks: Any
    _subagent_bg_tasks: set[asyncio.Task[Any]]
    session_store: Any

    def _build_subagent(self, request: Any) -> Any: ...

    def _on_subagent_background_done(self, session_id: str, result: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class SubagentFeatureBundle:
    """Definition and compatibility views for one Subagent Generation."""

    definition: FeatureDefinition
    service: SubagentService
    active: ActiveSubagents | None = None
    store: SubagentStore | None = None
    notifications: SubagentNotificationQueue | None = None


def _bind_tool_lease(tool: Any, context: FeatureInstallContext) -> None:
    """Make every model-facing subagent call hold its Feature Generation lease."""

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


def _host_owner(request: Request) -> str:
    account = getattr(getattr(request, "state", None), "account", None)
    return normalize_owner_account_id(getattr(account, "owner_account_id", ""))


def _create_subagent_router(host: SubagentFeatureHost) -> APIRouter:
    """Create the owner-scoped projection and continuable control endpoints."""

    router = APIRouter(prefix="/api/subagents", tags=["subagents"])

    @asynccontextmanager
    async def service_context(request: Request):
        plugins = getattr(host, "plugins", None)
        acquire = getattr(plugins, "acquire_service_lease", None)
        binding = getattr(getattr(request, "state", None), "route_binding", None)
        generation = getattr(binding, "generation", None)
        if binding is not None and generation is None:
            yield None
            return
        if callable(acquire):
            try:
                acquired = acquire(
                    SUBAGENT_SERVICE_KEY,
                    label="gateway:subagent",
                    generation=generation,
                )
            except (FeatureLeaseUnavailableError, TypeError):
                acquired = None
            if acquired is None:
                if generation is not None:
                    yield None
                    return
                fallback = getattr(host, "subagent_runtime", None)
                if fallback is None:
                    yield None
                    return
                yield fallback
                return
            service, lease = acquired
            async with lease:
                yield service
            return
        if generation is not None:
            yield None
            return
        yield getattr(host, "subagent_runtime", None)

    @router.get("")
    async def list_subagents(
        request: Request,
        session_id: str | None = None,
        limit: int = 100,
    ) -> Any:
        async with service_context(request) as service:
            if service is None:
                return {"ok": False, "error": "Subagent 未启用"}
            owner_account_id = _host_owner(request)
            bounded_limit = max(1, min(int(limit), 500))
            parent_session_id = str(session_id or "").strip() or None
            active = getattr(service, "active_snapshot", None)
            runs = getattr(service, "list_runs", None)
            return {
                "ok": True,
                "items": (
                    active(
                        parent_session_id,
                        owner_account_id=owner_account_id,
                    )
                    if callable(active)
                    else ([] if parent_session_id else {})
                ),
                "runs": (
                    runs(
                        parent_session_id=parent_session_id,
                        owner_account_id=owner_account_id,
                        limit=bounded_limit,
                    )
                    if callable(runs)
                    else []
                ),
            }

    async def _control_payload(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed input is a client error
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        return payload

    def _control_session(session_id: str | None) -> str:
        value = str(session_id or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail="session_id is required")
        return value

    @router.post("/{subagent_id}/message")
    async def send_subagent_message(
        request: Request,
        subagent_id: str,
        session_id: str | None = None,
    ) -> Any:
        parent_session_id = _control_session(session_id)
        payload = await _control_payload(request)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        async with service_context(request) as service:
            if service is None:
                raise HTTPException(status_code=503, detail="Subagent 未启用")
            try:
                result = await service.send_message(
                    subagent_id=str(subagent_id or "").strip(),
                    message=message,
                    parent_session_id=parent_session_id,
                    owner_account_id=_host_owner(request),
                    trigger_turn=bool(payload.get("trigger_turn", True)),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Subagent 不存在") from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail="Subagent 不属于当前会话") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"ok": True, **result}

    @router.post("/{subagent_id}/interrupt")
    async def interrupt_subagent(
        request: Request,
        subagent_id: str,
        session_id: str | None = None,
    ) -> Any:
        parent_session_id = _control_session(session_id)
        payload = await _control_payload(request)
        async with service_context(request) as service:
            if service is None:
                raise HTTPException(status_code=503, detail="Subagent 未启用")
            accepted = service.interrupt_agent(
                subagent_id=str(subagent_id or "").strip(),
                parent_session_id=parent_session_id,
                owner_account_id=_host_owner(request),
                message=str(payload.get("message") or "").strip() or None,
            )
            return {
                "ok": True,
                "status": "interrupted" if accepted else "not_running",
                "subagent_id": subagent_id,
            }

    return router


def _create_candidate(
    host: SubagentFeatureHost,
    *,
    db_path: str | Path,
    wal_enabled: bool,
    legacy_db_path: str | Path | None,
    retain_days: int,
    default_provider: str,
    max_depth: int,
    continuable_enabled: bool,
    fork_enabled: bool,
    idle_timeout: float,
    max_runtime: float,
) -> tuple[SubagentRuntime, ActiveSubagents, SubagentStore]:
    active = ActiveSubagents()
    store = SubagentStore(
        db_path,
        wal_enabled=wal_enabled,
        legacy_db_path=legacy_db_path,
        retain_days=retain_days,
    )
    child_factory = getattr(host, "agent_factory", None) or host._build_subagent
    providers: list[Any] = [
        InProcessSpawnProvider(
            child_factory,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
        )
    ]
    session_store = getattr(host, "session_store", None)
    if fork_enabled and session_store is not None:
        providers.append(
            InProcessForkProvider(
                child_factory,
                active=active,
                idle_timeout=idle_timeout,
                max_runtime=max_runtime,
                session_store=session_store,
            )
        )
    try:
        service = SubagentRuntime(
            providers=providers,
            active=active,
            default_provider=default_provider,
            max_depth=max_depth,
            continuable_enabled=continuable_enabled,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            store=store,
        )
    except BaseException:
        store.close()
        raise
    return service, active, store


def build_subagent_feature(
    service: SubagentService | None = None,
    *,
    host: SubagentFeatureHost | None = None,
    sub_registry: SubagentRegistry | None = None,
    db_path: str | Path | None = None,
    wal_enabled: bool = True,
    legacy_db_path: str | Path | None = None,
    retain_days: int = 30,
    default_provider: str = "inprocess-spawn",
    max_depth: int = 3,
    continuable_enabled: bool = False,
    fork_enabled: bool = False,
    idle_timeout: float = 120.0,
    max_runtime: float = 1800.0,
    desired_config_revision: int = 1,
    runtime: FeatureRuntime | None = None,
    activate: bool = False,
) -> SubagentFeatureBundle:
    """Build a restartable Subagent Feature.

    Passing only ``service`` keeps the service-only contract used by embedded
    consumers and tests. The production host path passes ``host``; in that
    mode each Generation constructs and owns its store/runtime, tools, presets,
    notifications, route and lifecycle event source.
    """

    owns_resources = service is None
    active: ActiveSubagents | None = None
    store: SubagentStore | None = None
    notifications: SubagentNotificationQueue | None = None

    if service is None:
        if host is None:
            raise ValueError("build_subagent_feature requires host when service is omitted")
        if db_path is None:
            config = getattr(host, "config", None)
            db_path = (
                getattr(config, "subagent_db_path", None)
                or getattr(config, "db_path", None)
            )
        if db_path is None:
            raise ValueError("build_subagent_feature requires db_path")
        service, active, store = _create_candidate(
            host,
            db_path=db_path,
            wal_enabled=wal_enabled,
            legacy_db_path=legacy_db_path,
            retain_days=retain_days,
            default_provider=default_provider,
            max_depth=max_depth,
            continuable_enabled=continuable_enabled,
            fork_enabled=fork_enabled,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
        )
    if host is not None:
        notifications = SubagentNotificationQueue()

    candidate = service
    pending = [candidate]

    def next_service() -> SubagentService:
        return pending.pop() if pending else candidate

    async def close_owned_resources() -> None:
        close = getattr(candidate, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        if store is not None:
            store.close()

    async def install(context: FeatureInstallContext) -> None:
        active_service = next_service()
        if owns_resources:
            context.register_disposer(
                close_owned_resources,
                label="resource:subagent.runtime",
                phase=RegistrationPhase.RESOURCE,
            )

        if host is None:
            context.register_service(
                SUBAGENT_SERVICE_KEY,
                active_service,
                label="service:subagent",
            )
            return

        current_service = getattr(host, "subagent_runtime", None)
        owned_marker = getattr(host, "_subagent_feature_service", None)
        explicit_binding = current_service is not None and (
            current_service is not active_service and current_service is not owned_marker
        )
        service_view = current_service if explicit_binding else active_service
        active_view = (
            getattr(service_view, "active", None)
            or active
            or getattr(host, "subagent_active", None)
        )
        store_view = getattr(service_view, "store", None) or store
        queue = notifications or getattr(host, "_subagent_notifications", None)
        if queue is None:
            queue = SubagentNotificationQueue()
        router = SubagentNotificationRouter(queue)

        host.subagent_runtime = service_view
        host.subagent_active = active_view
        host.subagent_store = store_view
        host._subagent_notifications = queue
        host._subagent_router = router
        host._subagent_feature_service = service_view

        def clear_bindings() -> None:
            if explicit_binding:
                return
            if getattr(host, "subagent_runtime", None) is service_view:
                host.subagent_runtime = None
            if getattr(host, "subagent_active", None) is active_view:
                host.subagent_active = None
            if getattr(host, "subagent_store", None) is store_view:
                host.subagent_store = None
            if getattr(host, "_subagent_feature_service", None) is service_view:
                host._subagent_feature_service = None

        context.register_disposer(
            clear_bindings,
            label="binding:subagent.host",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        context.register_service(
            SUBAGENT_SERVICE_KEY,
            service_view,
            label="service:subagent",
        )

        context.register_context_contributor(
            ContextContributor(
                contributor_id="subagent.background.reminder",
                handler=build_subagent_notification_handler(queue),
                phase=ContextPhase.PROMPT,
                priority=10,
                model_visible=True,
                persistent=False,
                description="Background subagent completion notifications",
            )
        )

        async def lifecycle_events(envelope: Any):
            drain = getattr(service_view, "drain_lifecycle_events", None)
            if not callable(drain):
                return None
            records = drain(
                owner_account_id=envelope.user_id,
                session_id=envelope.session_id,
            )
            return tuple(
                FeatureEvent(
                    "subagent",
                    str(record["event"]),
                    1,
                    record.get("payload") or {},
                )
                for record in records
            ) or None

        context.register_event_contributor(
            FeatureEventContributor(
                contributor_id="subagent.lifecycle",
                handler=lifecycle_events,
                priority=100,
                description="Subagent lifecycle events for the current parent session",
            )
        )

        if sub_registry is not None:
            for definition in tuple(sub_registry.list()):
                context.register_agent_preset(
                    to_preset_contribution(definition),
                    label=f"agent-preset:{definition.name}",
                )

        tasks = getattr(host, "subagent_tasks", None)
        if tasks is None:
            from crew.tasks.task_manager import LegacyTaskManagerAdapter

            tasks = LegacyTaskManagerAdapter(host.tasks)
            host.subagent_tasks = tasks

        background_counter = 0

        def launch_background(coro: Any) -> None:
            nonlocal background_counter
            background_counter += 1
            task = context.create_task(
                coro,
                name=f"subagent-background:{background_counter}",
            )
            compatibility_tasks = getattr(host, "_subagent_bg_tasks", None)
            if isinstance(compatibility_tasks, set):
                compatibility_tasks.add(task)
                task.add_done_callback(compatibility_tasks.discard)

        def register_owned_tool(**kwargs: Any) -> None:
            host.registry.register(**kwargs)
            tool = host.registry.get(str(kwargs["name"]))
            _bind_tool_lease(tool, context)
            context.register_disposer(
                lambda tool=tool: host.registry.unregister(
                    tool.name,
                    expected=tool,
                ),
                label=f"tool:{tool.name}",
                phase=RegistrationPhase.CONTRIBUTION,
            )

        register_owned_tool(
            name="structured_output",
            toolset="subagent.output",
            schema=build_structured_output_schema(),
            handler=handle_structured_output,
            display_name="提交结构化结果",
            should_defer=False,
            search_hint="subagent structured output schema submit",
        )

        build_subagent_tools(
            host.registry,
            sub_registry or SubagentRegistry(),
            service_view,
            max_concurrent=int(getattr(host.config, "subagent_max_concurrent", 3)),
            max_tasks=int(getattr(host.config, "subagent_max_tasks", 8)),
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            active=active_view,
            tasks=tasks,
            launch_background=launch_background,
            on_background_done=host._on_subagent_background_done,
            background_capacity=lambda: len(getattr(host, "_subagent_bg_tasks", ()))
            < int(getattr(host.config, "subagent_max_concurrent", 3)),
            on_collected=queue.remove,
            preset_registry=context.agent_presets,
            batch_policy=BatchSchedulePolicy.from_config(host.config),
            max_batch_items=int(getattr(host.config, "subagent_max_batch_items", 128)),
            summary_defaults={
                "min_chars": int(getattr(host.config, "subagent_summary_min_chars", 200)),
                "retries": int(getattr(host.config, "subagent_summary_retries", 1)),
            },
            register_tool=register_owned_tool,
        )
        context.register_api_router(
            _create_subagent_router(host),
            contribution_id=SUBAGENT_FEATURE_ID,
            label="route:subagents",
        )

    definition = FeatureDefinition(
        SUBAGENT_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            SUBAGENT_FEATURE_ID,
            requires=(AGENT_FACTORY_SERVICE_KEY,) if host is not None else (),
            provides=(SUBAGENT_SERVICE_KEY,),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.RESTART,
        required_by_product=False,
    )
    bundle = SubagentFeatureBundle(
        definition=definition,
        service=candidate,
        active=active,
        store=store,
        notifications=notifications,
    )
    if activate:
        if runtime is None:
            raise ValueError("activate=True requires a FeatureRuntime")
        record = run_async_compat(runtime.activate(definition))
        if record.state is not FeatureState.ACTIVE or record.scope is None:
            raise RuntimeError(
                f"Subagent Feature 激活失败: state={record.state.value} error={record.error}"
            )
    return bundle


__all__ = [
    "SUBAGENT_FEATURE_ID",
    "SubagentFeatureBundle",
    "SubagentFeatureHost",
    "build_subagent_feature",
]
