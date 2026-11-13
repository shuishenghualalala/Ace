"""4B-1 Dynamic Kanban Bundle ownership and generation contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.core.envelope import Envelope, ResponseChunk
from crew.app import build_app
from crew.dynamickanban.routes import create_dynamic_kanban_router
from crew.gateway.auth import AccountContext
from starlette.requests import Request
from crew.state.config import Config
from crew.dynamickanban.feature import (
    DYNAMIC_KANBAN_FEATURE_ID,
    DYNAMIC_KANBAN_SERVICE_KEY,
    build_dynamic_kanban_feature,
)
from crew.features import FeatureLeaseUnavailableError, FeatureRuntime, FeatureState
from crew.tools.registry import Registry


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        team_max_concurrent_children=2,
        dk_max_concurrent=2,
        dynamic_kanban_enabled=True,
    )


def _build(host, db_path, revision=1):
    return build_dynamic_kanban_feature(
        host,
        db_path=str(db_path),
        provider=FakeProvider(),
        base_registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=SimpleNamespace(),
        config=_config(),
        desired_config_revision=revision,
    )


@pytest.mark.asyncio
async def test_dynamic_kanban_activation_publishes_consumer_and_deactivation_clears_host(tmp_path):
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    runtime = FeatureRuntime()
    bundle = _build(host, tmp_path / "kanban.db")

    record = await runtime.activate(bundle.definition)
    assert record.state is FeatureState.ACTIVE
    service = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY)
    consumer = service.consumer
    assert consumer is host.dynamic_kanban_consumer
    workflow = consumer.create_workflow(
        session_id="session-1", title="Persistent", context={}, owner_account_id="owner-1"
    )

    assert await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID) is True
    assert host.dynamic_kanban is None
    assert host.dynamic_kanban_consumer is None
    with pytest.raises(FeatureLeaseUnavailableError):
        consumer.get_workflow(workflow.id, owner_account_id="owner-1")


@pytest.mark.asyncio
async def test_dynamic_kanban_update_allocates_new_manager_store_and_preserves_rows(tmp_path):
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    runtime = FeatureRuntime()
    db_path = tmp_path / "kanban.db"
    first = _build(host, db_path)
    await runtime.activate(first.definition)
    old_manager = host.dynamic_kanban
    old_consumer = host.dynamic_kanban_consumer
    workflow = old_consumer.create_workflow(
        session_id="session-1", title="Keep me", context={"v": 1}, owner_account_id="owner-1"
    )

    replacement = _build(host, db_path, revision=2)
    result = await runtime.update(replacement.definition)
    assert result.updated is True
    assert host.dynamic_kanban is not old_manager
    assert host.dynamic_kanban.store is not old_manager.store
    assert old_manager._closed is True
    new_consumer = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY).consumer
    assert new_consumer is not old_consumer
    assert new_consumer.get_workflow(workflow.id, owner_account_id="owner-1").title == "Keep me"

    await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)


@pytest.mark.asyncio
async def test_dynamic_kanban_driver_closes_inner_stream_on_partial_consume(tmp_path):
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    runtime = FeatureRuntime()
    bundle = _build(host, tmp_path / "kanban.db")
    closed = asyncio.Event()

    async def interact(_envelope):
        try:
            yield ResponseChunk.delta("request-1", "first")
            await asyncio.Event().wait()
        finally:
            closed.set()

    await runtime.activate(bundle.definition)
    host.dynamic_kanban.interact = interact
    envelope = Envelope.of("run", session_id="session-1", request_id="request-1", mode="dynamic_kanban")
    stream = runtime.execution_drivers.dispatch(envelope)
    assert (await stream.__anext__()).body == {"text": "first"}
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)
    record = runtime.get(DYNAMIC_KANBAN_FEATURE_ID)
    assert record is not None and record.scope is not None
    assert record.scope.active_leases == ()
    await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)


@pytest.mark.asyncio
async def test_dynamic_kanban_resume_sse_closes_manager_stream_on_partial_consume():
    closed = asyncio.Event()

    class _Manager:
        def acquire_feature_lease(self, _label):
            class _Lease:
                def release(self):
                    pass
            return _Lease()

        async def resume_stream(self, *_args):
            try:
                yield ResponseChunk.delta("request-1", "first")
                await asyncio.Event().wait()
            finally:
                closed.set()

    manager = _Manager()
    router = create_dynamic_kanban_router(
        SimpleNamespace(config=SimpleNamespace(auth_mode="local")),
        service=SimpleNamespace(manager=manager),
    )
    route = next(r for r in router.routes if r.path.endswith("/resume"))
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/dynamic-kanban/session-1/resume",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
    })
    request.state.account = AccountContext(owner_account_id="owner-1", is_local=True)
    response = await route.endpoint("session-1", request)
    assert response.media_type == "text/event-stream"
    assert "first" in await response.body_iterator.__anext__()
    await response.body_iterator.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_dynamic_kanban_deactivate_is_idempotent_and_new_generation_reopens_store(tmp_path):
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    runtime = FeatureRuntime()
    db_path = tmp_path / "kanban.db"
    first = _build(host, db_path)
    await runtime.activate(first.definition)
    workflow = host.dynamic_kanban_consumer.create_workflow(
        session_id="session-1", title="Retained", context={}, owner_account_id="owner-1"
    )
    assert await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID) is True
    assert await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID) is False

    second = _build(host, db_path, revision=2)
    record = await runtime.activate(second.definition)
    assert record.state is FeatureState.ACTIVE
    assert host.dynamic_kanban_consumer.get_workflow(
        workflow.id, owner_account_id="owner-1"
    ).title == "Retained"
    await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)


@pytest.mark.asyncio
async def test_dynamic_kanban_owned_background_task_is_cancelled_on_stop(tmp_path):
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    config = _config()
    config.tasks_auto_background_after_seconds = 0.001
    bundle = build_dynamic_kanban_feature(
        host,
        db_path=str(tmp_path / "kanban.db"),
        provider=FakeProvider(),
        base_registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=SimpleNamespace(),
        config=config,
    )
    runtime = FeatureRuntime()
    await runtime.activate(bundle.definition)

    async def long_workflow(*_args, **_kwargs):
        try:
            yield ResponseChunk.delta("request-1", "running")
            await asyncio.Event().wait()
        finally:
            closed.set()

    closed = asyncio.Event()
    host.dynamic_kanban._run_workflow_with_persistence = long_workflow
    envelope = Envelope.of(
        "run", session_id="session-1", request_id="request-1", user_id="owner-1",
        mode="dynamic_kanban",
    )
    stream = host.dynamic_kanban.interact(envelope)
    assert (await stream.__anext__()).body == {"text": "running"}
    await stream.aclose()
    await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)
    await asyncio.wait_for(closed.wait(), timeout=1)
    assert runtime.get(DYNAMIC_KANBAN_FEATURE_ID).scope is None


@pytest.mark.asyncio
async def test_dynamic_kanban_manager_construction_failure_closes_store(monkeypatch, tmp_path):
    import crew.dynamickanban.feature as feature_module

    class _Store:
        close_calls = 0
        def close(self):
            type(self).close_calls += 1

    class _BrokenManager:
        def __init__(self, **_kwargs):
            raise RuntimeError("manager construction failed")

    monkeypatch.setattr(feature_module, "SQLiteKanbanStore", lambda *_args, **_kwargs: _Store())
    monkeypatch.setattr(feature_module, "DynamicKanbanManager", _BrokenManager)
    host = SimpleNamespace(dynamic_kanban=None, dynamic_kanban_consumer=None)
    runtime = FeatureRuntime()
    bundle = feature_module.build_dynamic_kanban_feature(
        host,
        db_path=str(tmp_path / "broken.db"),
        provider=FakeProvider(),
        base_registry=Registry(),
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=SimpleNamespace(),
        config=_config(),
    )

    record = await runtime.activate(bundle.definition)

    assert record.state is FeatureState.FAILED
    assert "manager construction failed" in str(record.error)
    assert _Store.close_calls == 1
    assert host.dynamic_kanban is None
    assert host.dynamic_kanban_consumer is None


@pytest.mark.asyncio
async def test_production_build_app_wires_dynamic_kanban_feature_and_team_consumer(tmp_path):
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=True,
    )
    try:
        runtime = app.plugins.feature_runtime
        record = runtime.get(DYNAMIC_KANBAN_FEATURE_ID)
        assert record is not None
        assert record.state is FeatureState.ACTIVE
        assert app.dynamic_kanban is not None
        assert app.dynamic_kanban_consumer is not None
        assert app.team is not None
        assert app.team.kanban_consumer_provider() is app.dynamic_kanban_consumer
        old_consumer = app.dynamic_kanban_consumer
        workflow = old_consumer.create_workflow(
            session_id="production-session",
            title="Production row",
            context={},
            owner_account_id="owner-1",
        )
        await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)
        assert app.team.kanban_consumer_provider() is None
        with pytest.raises(FeatureLeaseUnavailableError):
            old_consumer.get_workflow(
                workflow.id, owner_account_id="owner-1"
            )
    finally:
        await app.shutdown()
