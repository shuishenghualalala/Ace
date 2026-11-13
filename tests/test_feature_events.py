"""Feature Event contributor contracts and lifecycle integration."""

from __future__ import annotations

import asyncio

import pytest

from crew.core.envelope import Envelope
from crew.features import (
    EventFailurePolicy,
    FeatureDefinition,
    ExecutionDriverRegistry,
    FeatureEvent,
    FeatureEventBinding,
    FeatureEventContributionFailedError,
    FeatureEventContributor,
    FeatureEventContributorConflictError,
    FeatureEventContributorRegistry,
    FeatureGeneration,
    FeatureRuntime,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RouteRegistry,
    ServiceRegistry,
    ContextContributorRegistry,
)


def _active_scope(feature_id: str, sequence: int = 1) -> FeatureScope:
    scope = FeatureScope(FeatureGeneration(feature_id, sequence))
    scope.activate()
    return scope


def _event(name: str, value: object = 1) -> FeatureEvent:
    return FeatureEvent("test.feature", name, 1, {"value": value})


def test_feature_event_validates_identifiers_version_and_payload_without_business_names():
    original = {"value": 1}
    event = FeatureEvent("acme.feature", "done", 1, original)
    assert event.feature == "acme.feature"
    assert event.as_body() == {
        "feature": "acme.feature",
        "event": "done",
        "version": 1,
        "payload": {"value": 1},
    }
    original["other"] = 2
    body = event.as_body()
    body["payload"]["value"] = 3
    assert event.payload == {"value": 1}
    assert "kind" not in body

    for feature, event_name in (("", "done"), ("has space", "done"), ("Bad", "done"), ("ok", "Bad"), ("ok", "")):
        with pytest.raises(ValueError):
            FeatureEvent(feature, event_name, 1, {})
    for version in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            FeatureEvent("ok", "done", version, {})
    with pytest.raises(TypeError):
        FeatureEvent("ok", "done", 1, [])


async def test_registry_visibility_release_conflict_and_deterministic_order():
    registry = FeatureEventContributorRegistry()
    old = _active_scope("feature", 1)
    old_token = registry.register(old, FeatureEventContributor("same.id", lambda _: _event("old")))
    activating = FeatureScope(FeatureGeneration("feature", 2))
    new_token = registry.register(
        activating,
        FeatureEventContributor("same.id", lambda _: _event("new")),
    )
    assert [binding.generation.key for binding in registry.bindings()] == ["feature@g1"]
    activating.activate()
    assert [binding.generation.key for binding in registry.bindings()] == ["feature@g2"]
    with pytest.raises(FeatureEventContributorConflictError):
        registry.register(_active_scope("other"), FeatureEventContributor("same.id", lambda _: None))
    with pytest.raises(FeatureEventContributorConflictError):
        registry.register(activating, FeatureEventContributor("same.id", lambda _: None))

    ordered = _active_scope("ordered")
    registry.register(ordered, FeatureEventContributor("z.id", lambda _: _event("z"), priority=10))
    registry.register(ordered, FeatureEventContributor("a.id", lambda _: _event("a"), priority=10))
    registry.register(ordered, FeatureEventContributor("first.id", lambda _: _event("first"), priority=1))
    assert [b.contributor.contributor_id for b in registry.bindings()] == [
        "first.id", "a.id", "z.id", "same.id",
    ]

    await new_token.dispose()
    assert [binding.generation.key for binding in registry.bindings() if binding.contributor.contributor_id == "same.id"] == ["feature@g1"]
    await old_token.dispose()
    assert [binding for binding in registry.bindings() if binding.contributor.contributor_id == "same.id"] == []
    await ordered.dispose()
    await activating.dispose()
    await old.dispose()


async def test_registry_supports_sync_async_predicate_none_single_and_iterable_results():
    registry = FeatureEventContributorRegistry()
    scope = _active_scope("forms")
    seen: list[str] = []

    def sync(_):
        seen.append("sync")
        return _event("sync")

    async def async_handler(_):
        seen.append("async")
        return [_event("list-1"), _event("list-2")]

    def skipped(_):
        seen.append("skipped")
        return _event("bad")

    def empty(_):
        seen.append("none")
        return None

    registry.register(scope, FeatureEventContributor("sync", sync, priority=1))
    registry.register(scope, FeatureEventContributor("async", async_handler, priority=2))
    registry.register(scope, FeatureEventContributor("skipped", skipped, priority=3, predicate=lambda _: False))
    registry.register(scope, FeatureEventContributor("empty", empty, priority=4))
    delivered = []

    async def sink(event):
        delivered.append(event)

    report = await registry.dispatch(Envelope.of("q", session_id="s"), sink)
    assert seen == ["sync", "async", "none"]
    assert [item.event for item in report.events] == ["sync", "list-1", "list-2"]
    assert delivered == list(report.events)
    assert report.failures == ()
    await scope.dispose()


async def test_registry_reports_degrade_and_fail_timeout_and_propagates_cancelled_error():
    registry = FeatureEventContributorRegistry()
    degraded = _active_scope("degraded")

    def broken(_):
        raise RuntimeError("broken source")

    async def slow(_):
        await asyncio.sleep(10)

    def invalid(_):
        return {"not": "an event"}

    registry.register(degraded, FeatureEventContributor("broken", broken))
    registry.register(degraded, FeatureEventContributor("invalid", invalid))
    registry.register(degraded, FeatureEventContributor("slow", slow, timeout_seconds=0.001))
    async def sink(_event):
        return None

    report = await registry.dispatch(Envelope.of("q", session_id="s"), sink)
    assert [failure.contributor_id for failure in report.failures] == ["broken", "invalid", "slow"]
    assert report.failures[2].timed_out is True
    assert degraded.active_leases == ()

    fail_scope = _active_scope("fail")
    registry.register(fail_scope, FeatureEventContributor("fatal", broken, failure_policy=EventFailurePolicy.FAIL))
    with pytest.raises(FeatureEventContributionFailedError) as captured:
        await registry.dispatch(Envelope.of("q", session_id="s"), sink)
    assert captured.value.contributor_id == "fatal"
    assert fail_scope.active_leases == ()

    cancelled_scope = _active_scope("cancelled")
    started = asyncio.Event()

    async def cancelled(_):
        started.set()
        await asyncio.Event().wait()

    registry.register(cancelled_scope, FeatureEventContributor("cancelled", cancelled))
    task = asyncio.create_task(registry.dispatch(Envelope.of("q", session_id="s"), sink))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_scope.active_leases == ()
    await degraded.dispose()
    await fail_scope.dispose()
    await cancelled_scope.dispose()


async def test_dispatch_sink_failure_propagates_unchanged_and_stops_later_contributors():
    registry = FeatureEventContributorRegistry()
    scope = _active_scope("sink-failure")
    registry.register(
        scope,
        FeatureEventContributor("first", lambda _: _event("first"), priority=1),
    )
    registry.register(
        scope,
        FeatureEventContributor("second", lambda _: _event("second"), priority=2),
    )
    sent = []
    cause = RuntimeError("sink unavailable")

    async def sink(event):
        sent.append(event.event)
        raise cause

    with pytest.raises(RuntimeError) as captured:
        await registry.dispatch(Envelope.of("q", session_id="s"), sink)
    assert captured.value is cause
    assert sent == ["first"]
    assert scope.active_leases == ()
    await scope.dispose()


async def test_generation_replace_hides_activating_new_generation_and_rolls_back_failed_candidate():
    runtime = FeatureRuntime()

    def definition(value: str, revision: int, *, broken: bool = False) -> FeatureDefinition:
        def install(context):
            context.register_event_contributor(
                FeatureEventContributor("versioned.source", lambda _: _event("value", value))
            )
            if broken:
                raise RuntimeError("candidate failed")

        return FeatureDefinition(
            "versioned",
            install,
            desired_config_revision=revision,
            update_strategy=FeatureUpdateStrategy.REPLACE,
        )

    record = await runtime.activate(definition("old", 1))
    async def sink(_event):
        return None

    assert (await runtime.event_contributors.dispatch(Envelope.of("q", session_id="s"), sink)).events[0].payload["value"] == "old"
    assert record.scope is not None
    old_scope = record.scope
    old_lease = old_scope.acquire_lease("old-request")
    updating = asyncio.create_task(runtime.update(definition("new", 2)))
    while old_scope.state is not FeatureState.DRAINING:
        await asyncio.sleep(0)
    assert (await runtime.event_contributors.dispatch(Envelope.of("q", session_id="s"), sink)).events[0].payload["value"] == "new"
    old_lease.release()
    result = await updating
    assert result.updated
    assert old_scope.state is FeatureState.DISPOSED
    assert runtime.event_contributors.bindings()[0].generation.key == "versioned@g2"

    failed = await runtime.update(definition("broken", 3, broken=True))
    assert not failed.updated
    assert [b.generation.key for b in runtime.event_contributors.bindings()] == ["versioned@g2"]
    assert runtime.get("versioned").state is FeatureState.ACTIVE
    await runtime.deactivate("versioned")
    assert runtime.event_contributors.bindings() == ()


async def test_dispatch_holds_generation_lease_until_async_source_finishes_and_drain_waits():
    registry = FeatureEventContributorRegistry()
    scope = _active_scope("blocking")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_):
        started.set()
        await release.wait()
        return _event("done")

    registry.register(scope, FeatureEventContributor("blocking.source", blocking))
    async def sink(_event):
        return None

    dispatching = asyncio.create_task(registry.dispatch(Envelope.of("q", session_id="s"), sink))
    await started.wait()
    stopping = asyncio.create_task(scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1))
    while scope.state is not FeatureState.DRAINING:
        await asyncio.sleep(0)
    assert registry.bindings() == ()
    assert not stopping.done()
    assert scope.active_leases
    release.set()
    assert [event.event for event in (await dispatching).events] == ["done"]
    await stopping
    assert scope.active_leases == ()


async def test_binding_snapshot_race_during_acquire_is_safely_skipped(monkeypatch):
    registry = FeatureEventContributorRegistry()
    scope = _active_scope("race")
    registry.register(scope, FeatureEventContributor("race.source", lambda _: _event("never")))
    original = FeatureEventBinding.acquire_lease

    def drain_before_acquire(current, label):
        scope.begin_draining()
        return original(current, label)

    monkeypatch.setattr(FeatureEventBinding, "acquire_lease", drain_before_acquire)
    async def sink(_event):
        return None

    report = await registry.dispatch(Envelope.of("q", session_id="s"), sink)
    assert report.events == ()
    assert report.failures == ()
    await scope.dispose()


async def test_runtime_event_context_wiring_and_registration_diagnostics():
    runtime = FeatureRuntime()

    async def install(context):
        context.register_event_contributor(
            FeatureEventContributor("diagnostic.source", lambda _: None),
            label="event:diagnostic",
        )

    record = await runtime.activate(FeatureDefinition("diagnostic", install))
    assert record.state is FeatureState.ACTIVE
    assert [binding.contributor.contributor_id for binding in runtime.event_contributors.bindings()] == ["diagnostic.source"]
    diagnostic = runtime.startup_audit().as_dict()["features"][0]
    assert {item["label"] for item in diagnostic["registrations"]} == {"event:diagnostic"}
    await runtime.deactivate("diagnostic")


async def test_plugin_context_registers_event_under_generation_and_unload_revokes(tmp_path):
    from crew.plugins.manager import PluginManager
    from crew.tools.registry import Registry

    plugin_dir = tmp_path / "event_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: event-plugin\nkind: standalone\n", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        """
from crew.features import FeatureEvent

def register(ctx):
    ctx.register_event_contributor(
        "plugin.events",
        lambda envelope: FeatureEvent("plugin", "ready", 1, {"session": envelope.session_id}),
    )
""".lstrip(),
        encoding="utf-8",
    )
    plugins = PluginManager(registry=Registry())
    await plugins.discover_and_load_async([tmp_path], enabled=["event-plugin"])
    loaded = plugins.get_plugin("event-plugin")
    assert loaded is not None and loaded.feature_record is not None
    assert loaded.feature_record.state is FeatureState.ACTIVE
    generation = loaded.feature_record.generation
    assert generation is not None
    binding = plugins.event_contributors.bindings()[0]
    assert binding.generation == generation
    async def sink(_event):
        return None

    report = await plugins.event_contributors.dispatch(Envelope.of("q", session_id="s"), sink)
    assert report.events[0].as_body()["payload"] == {"session": "s"}
    assert await plugins.unload_plugin_async("event-plugin") is True
    assert plugins.event_contributors.bindings() == ()


def test_runtime_preserves_existing_positional_constructor_order():
    services = ServiceRegistry()
    drivers = ExecutionDriverRegistry()
    contexts = ContextContributorRegistry()
    routes = RouteRegistry()
    runtime = FeatureRuntime(services, drivers, contexts, routes)
    assert runtime.routes is routes
