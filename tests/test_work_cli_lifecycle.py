"""Work CLI 的动态 Service 与 generation lease 契约测试。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from crew.app import build_app
from crew.cli.app import CliContext, CliError, CliResult
from crew.cli.content import _active_work_service, _lease_work_handler, _work_service
from crew.cli.main import build_parser
from crew.features import (
    FeatureDefinition,
    FeatureGeneration,
    FeatureRuntime,
    FeatureScope,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    ServiceRegistry,
)
from crew.work.service import WORK_SERVICE_KEY
from crew.state.config import Config
from crew.wiki.config import WikiConfig, WikiStorageConfig


OWNER = "work-cli-owner"


class _Lease:
    def __init__(self) -> None:
        self.release_calls = 0
        self.entered = 0
        self.exited = 0

    def release(self) -> None:
        self.release_calls += 1

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1
        self.release()
        return False


class _WorkService:
    def __init__(self, marker: str = "current") -> None:
        self.marker = marker

    def create_session(self, *, owner_account_id: str, workspace_id: str, title: str):
        return {
            "session_id": f"{self.marker}-session",
            "owner_account_id": owner_account_id,
            "workspace_id": workspace_id,
            "title": title,
        }

    def history(self, owner: str, *, include_archived: bool = False):
        return [{"marker": self.marker, "owner": owner, "archived": include_archived}]


class _Plugins:
    def __init__(self, registry: ServiceRegistry, *, record=None):
        self.registry = registry
        self.feature_runtime = SimpleNamespace(get=lambda _feature_id: record)

    def acquire_service_lease(self, key, *, label="service-request"):
        assert key is WORK_SERVICE_KEY
        return self.registry.acquire_lease(key, label=label)


def _ctx(plugins: _Plugins, external=None) -> CliContext:
    return CliContext(
        owner=OWNER,
        _app=SimpleNamespace(plugins=plugins, work_service=external),
    )


def _invoke(argv: list[str], ctx: CliContext):
    args = build_parser().parse_args(argv)
    result = args.handler(args, ctx)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _work_leaf_handlers(parser):
    for action in parser._actions:
        for child in (getattr(action, "choices", {}) or {}).values():
            yield from _work_leaf_handlers(child)
    handler = parser.get_default("handler")
    if handler is not None and handler.__name__.startswith("_work_"):
        yield handler


def test_every_work_leaf_is_wrapped_by_the_generation_gate():
    parser = build_parser()
    work_parser = next(
        child
        for action in parser._actions
        for child in (getattr(action, "choices", {}) or {}).values()
        if child.prog.endswith(" work")
    )
    handlers = list(_work_leaf_handlers(work_parser))
    assert len(handlers) == 38
    plugins = _Plugins(ServiceRegistry(), record=SimpleNamespace(state=FeatureState.DRAINING))
    ctx = _ctx(plugins, external=_WorkService("stale"))
    for handler in handlers:
        with pytest.raises(CliError, match="Work service 未初始化"):
            handler(SimpleNamespace(), ctx)


def test_sync_work_command_uses_registry_service_and_releases_lease():
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.work", 1))
    service = _WorkService("generation-1")
    registry.register(scope, WORK_SERVICE_KEY, service)
    scope.activate()
    try:
        result = _invoke(
            ["work", "sessions", "create", "--workspace-id", "ws", "--title", "Title"],
            _ctx(_Plugins(registry, record=SimpleNamespace(state=FeatureState.ACTIVE))),
        )
        assert result.data["session_id"] == "generation-1-session"
        assert not scope.active_leases
    finally:
        asyncio.run(scope.dispose())


def test_draining_generation_rejects_new_work_command():
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.work", 1))
    registry.register(scope, WORK_SERVICE_KEY, _WorkService())
    scope.activate()
    scope.begin_draining()
    try:
        with pytest.raises(CliError, match="Work service 未初始化"):
            _invoke(["work", "history"], _ctx(_Plugins(registry, record=SimpleNamespace(state=FeatureState.DRAINING))))
    finally:
        asyncio.run(scope.dispose())


def test_legacy_external_service_is_used_without_work_feature_record():
    external = _WorkService("legacy")
    registry = ServiceRegistry()
    result = _invoke(
        ["work", "history"],
        _ctx(_Plugins(registry, record=None), external=external),
    )
    assert result.data["entries"][0]["marker"] == "legacy"


def test_build_app_plugin_manager_argparse_handler_uses_registry_and_fails_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        cron_enabled=False,
        plugins_enabled=[],
        wiki=WikiConfig(
            enabled=False,
            storage=WikiStorageConfig(root=str(tmp_path / "wiki")),
        ),
    )
    app = build_app(config=config, enable_team=False)
    ctx = CliContext(owner=OWNER, _app=app)
    app.work_service = _WorkService("stale-host-field")
    try:
        args = build_parser().parse_args(["work", "history"])
        result = args.handler(args, ctx)
        assert result.data == {"entries": [], "count": 0}
        assert not inspect.isawaitable(result)
        record = app.plugins.feature_runtime.get("product.work")
        assert record is not None and record.scope is not None
        assert not record.scope.active_leases

        asyncio.run(app.plugins.feature_runtime.deactivate("product.work"))
        with pytest.raises(CliError, match="Work service 未初始化"):
            args.handler(args, ctx)
    finally:
        asyncio.run(app.shutdown())


@pytest.mark.asyncio
async def test_async_wrapper_releases_lease_when_handler_raises():
    lease = _Lease()
    service = object()

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            assert key is WORK_SERVICE_KEY
            return service, lease

    async def handler(_args, ctx):
        assert ctx.app is app
        raise RuntimeError("work failure")

    app = SimpleNamespace(plugins=Plugins())
    ctx = CliContext(owner=OWNER, _app=app)
    wrapped = _lease_work_handler(handler, "async-test")
    with pytest.raises(RuntimeError, match="work failure"):
        await wrapped(SimpleNamespace(), ctx)
    assert lease.entered == 1
    assert lease.exited == 1
    assert lease.release_calls == 1


def _definition(service: _WorkService, revision: int) -> FeatureDefinition:
    def install(context):
        context.register_service(WORK_SERVICE_KEY, service, label=f"service:{service.marker}")

    return FeatureDefinition(
        "product.work",
        install,
        dependencies=FeatureServiceDependencies("product.work", provides=(WORK_SERVICE_KEY,)),
        desired_config_revision=revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.RESTART,
    )


async def _wait_for_state(scope: FeatureScope, state: FeatureState) -> None:
    while scope.state is not state:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_real_feature_runtime_update_drains_cli_and_publishes_new_generation():
    runtime = FeatureRuntime()
    old = _WorkService("old")
    new = _WorkService("new")
    await runtime.activate(_definition(old, 1))
    app = SimpleNamespace(
        plugins=SimpleNamespace(
            acquire_service_lease=lambda key, *, label="service-request": runtime.services.acquire_lease(key, label=label),
            feature_runtime=runtime,
        ),
        work_service=old,
    )
    ctx = CliContext(owner=OWNER, _app=app)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_args, _ctx):
        started.set()
        await release.wait()
        return CliResult(data={"marker": _work_service(_ctx.app).marker})

    task = asyncio.create_task(_lease_work_handler(blocking, "blocking")(SimpleNamespace(), ctx))
    await started.wait()
    record = runtime.get("product.work")
    assert record is not None and record.scope is not None
    old_scope = record.scope
    updating = asyncio.create_task(runtime.update(_definition(new, 2)))
    await asyncio.wait_for(_wait_for_state(old_scope, FeatureState.DRAINING), timeout=1)
    assert not updating.done()
    release.set()
    in_flight = await task
    assert in_flight.data["marker"] == "old"
    result = await updating
    assert result.updated
    assert result.current_generation == "product.work@g2"
    assert _invoke(["work", "history"], _ctx(_Plugins(runtime.services, record=runtime.get("product.work")), external=old)).data["entries"][0]["marker"] == "new"
    assert old_scope.state is FeatureState.DISPOSED
    await runtime.deactivate("product.work")


@pytest.mark.asyncio
async def test_real_feature_runtime_deactivate_drains_in_flight_cli_and_rejects_new():
    runtime = FeatureRuntime()
    service = _WorkService("deactivate")
    await runtime.activate(_definition(service, 1))
    plugins = _Plugins(runtime.services, record=runtime.get("product.work"))
    ctx = _ctx(plugins, external=service)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_args, _ctx):
        started.set()
        await release.wait()
        return _work_service(_ctx.app).marker

    task = asyncio.create_task(_lease_work_handler(blocking, "deactivate")(SimpleNamespace(), ctx))
    await started.wait()
    stopping = asyncio.create_task(runtime.deactivate("product.work"))
    record = runtime.get("product.work")
    assert record is not None and record.scope is not None
    await asyncio.wait_for(_wait_for_state(record.scope, FeatureState.DRAINING), timeout=1)
    with pytest.raises(CliError, match="Work service 未初始化"):
        _invoke(["work", "history"], ctx)
    assert not stopping.done()
    release.set()
    assert await task == "deactivate"
    assert await stopping


def test_sync_exception_releases_lease_and_resets_service_context():
    lease = _Lease()
    service = _WorkService("sync")

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))

    def handler(_args, inner_ctx):
        assert _work_service(inner_ctx.app) is service
        raise RuntimeError("sync failure")

    with pytest.raises(RuntimeError, match="sync failure"):
        _lease_work_handler(handler, "sync-test")(SimpleNamespace(), ctx)
    assert lease.release_calls == 1
    assert _active_work_service.get() is None


def test_sync_callable_awaitable_close_before_start_releases_lease_once():
    lease = _Lease()
    service = _WorkService("awaitable-close")
    underlying = None

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            assert key is WORK_SERVICE_KEY
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))

    def handler(_args, _ctx):
        async def pending():
            await asyncio.Event().wait()

        nonlocal underlying
        underlying = pending()
        return underlying

    result = _lease_work_handler(handler, "awaitable-close-test")(
        SimpleNamespace(), ctx
    )
    assert inspect.getcoroutinestate(underlying) == inspect.CORO_CREATED

    result.close()
    result.close()

    assert lease.release_calls == 1
    assert inspect.getcoroutinestate(underlying) == inspect.CORO_CLOSED
    assert _active_work_service.get() is None


@pytest.mark.asyncio
async def test_sync_callable_awaitable_cancel_before_first_step_releases_lease_once():
    lease = _Lease()
    service = _WorkService("awaitable-cancel-before-start")
    underlying = None

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            assert key is WORK_SERVICE_KEY
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))

    def handler(_args, _ctx):
        async def pending():
            await asyncio.Event().wait()

        nonlocal underlying
        underlying = pending()
        return underlying

    result = _lease_work_handler(handler, "awaitable-cancel-before-start-test")(
        SimpleNamespace(), ctx
    )
    task = asyncio.create_task(result)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.release_calls == 1
    assert inspect.getcoroutinestate(underlying) == inspect.CORO_CLOSED
    assert _active_work_service.get() is None


@pytest.mark.asyncio
async def test_sync_callable_returning_awaitable_holds_lease_until_completion():
    lease = _Lease()
    service = _WorkService("awaitable")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            assert key is WORK_SERVICE_KEY
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))

    def handler(_args, inner_ctx):
        assert _work_service(inner_ctx.app) is service

        async def complete():
            assert _work_service(inner_ctx.app) is service
            entered.set()
            await release.wait()
            assert _work_service(inner_ctx.app) is service
            return service.marker

        return complete()

    result = _lease_work_handler(handler, "awaitable-test")(SimpleNamespace(), ctx)
    assert inspect.isawaitable(result)
    assert lease.release_calls == 0
    assert _active_work_service.get() is None
    task = asyncio.create_task(result)
    await entered.wait()
    assert lease.release_calls == 0
    release.set()
    assert await task == "awaitable"
    assert lease.release_calls == 1
    assert _active_work_service.get() is None


@pytest.mark.asyncio
async def test_sync_callable_returning_cancelled_awaitable_releases_lease():
    lease = _Lease()
    service = _WorkService("awaitable-cancel")
    entered = asyncio.Event()

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            assert key is WORK_SERVICE_KEY
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))

    def handler(_args, inner_ctx):
        async def block():
            assert _work_service(inner_ctx.app) is service
            entered.set()
            await asyncio.Event().wait()

        return block()

    task = asyncio.create_task(
        _lease_work_handler(handler, "awaitable-cancel-test")(SimpleNamespace(), ctx)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.release_calls == 1
    assert _active_work_service.get() is None


@pytest.mark.asyncio
async def test_async_cancellation_releases_lease_and_resets_service_context():
    lease = _Lease()
    service = _WorkService("cancel")

    class Plugins:
        def acquire_service_lease(self, key, *, label="service-request"):
            return service, lease

    ctx = CliContext(owner=OWNER, _app=SimpleNamespace(plugins=Plugins()))
    entered = asyncio.Event()

    async def handler(_args, inner_ctx):
        assert _work_service(inner_ctx.app) is service
        entered.set()
        await asyncio.Event().wait()

    async def run_and_observe():
        try:
            await _lease_work_handler(handler, "cancel-test")(SimpleNamespace(), ctx)
        finally:
            assert _active_work_service.get() is None

    task = asyncio.create_task(run_and_observe())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.exited == 1
    assert lease.release_calls == 1
