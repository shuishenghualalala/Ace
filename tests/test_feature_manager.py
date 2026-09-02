"""Neutral FeatureRuntime activation and dependency behavior."""

from __future__ import annotations

import asyncio

from crew.features import (
    FeatureDefinition,
    FeatureDrainTimeoutError,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    MissingProvidedServicesError,
    ServiceKey,
)


async def test_runtime_waits_for_required_service_without_installing():
    runtime = FeatureRuntime()
    storage = ServiceKey[object]("storage")
    installs = 0

    def install(_context) -> None:
        nonlocal installs
        installs += 1

    record = await runtime.activate(
        FeatureDefinition(
            "wiki",
            install,
            dependencies=FeatureServiceDependencies("wiki", requires=(storage,)),
        )
    )

    assert record.state is FeatureState.WAITING
    assert installs == 0
    assert record.dependency_resolution is not None
    assert record.dependency_resolution.missing_required == (storage,)


async def test_runtime_commits_installation_and_awaits_deactivation():
    runtime = FeatureRuntime()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    def install(context) -> None:
        context.register_disposer(cleanup, label="resource:test")

    record = await runtime.activate(FeatureDefinition("test", install))
    assert record.state is FeatureState.ACTIVE
    assert record.generation is not None
    assert record.generation.key == "test@g1"
    assert record.effective_config_revision == 1

    stopping = asyncio.create_task(runtime.deactivate("test"))
    await cleanup_started.wait()
    assert runtime.get("test").state is FeatureState.STOPPING
    assert not stopping.done()

    cleanup_release.set()
    assert await stopping is True
    assert runtime.get("test").state is FeatureState.DISCOVERED


async def test_runtime_rolls_back_failed_installation():
    runtime = FeatureRuntime()
    events: list[str] = []

    def install(context) -> None:
        context.register_disposer(lambda: events.append("first"), label="first")
        context.register_disposer(lambda: events.append("second"), label="second")
        raise RuntimeError("install failed")

    record = await runtime.activate(FeatureDefinition("broken", install))

    assert record.state is FeatureState.FAILED
    assert events == ["second", "first"]
    assert record.error is not None
    assert "install failed" in str(record.error)


async def test_runtime_does_not_retry_after_incomplete_rollback():
    runtime = FeatureRuntime()
    install_calls = 0

    def broken_install(context) -> None:
        nonlocal install_calls
        install_calls += 1

        def broken_cleanup() -> None:
            raise RuntimeError("cleanup failed")

        context.register_disposer(broken_cleanup, label="resource:broken")
        raise RuntimeError("install failed")

    first = await runtime.activate(FeatureDefinition("broken", broken_install))
    second = await runtime.activate(FeatureDefinition("broken", broken_install))

    assert first is second
    assert second.state is FeatureState.FAILED
    assert second.scope is not None and second.scope.state is FeatureState.FAILED
    assert install_calls == 1


async def test_runtime_activates_provider_before_alphabetically_first_consumer():
    runtime = FeatureRuntime()
    catalog = ServiceKey[dict]("catalog")
    events: list[str] = []

    def install_consumer(context) -> None:
        events.append(f"consumer:{context.resolve_service(catalog)['ready']}")

    def install_provider(context) -> None:
        events.append("provider")
        context.register_service(catalog, {"ready": True})

    records = await runtime.activate_many(
        [
            FeatureDefinition(
                "a-consumer",
                install_consumer,
                dependencies=FeatureServiceDependencies(
                    "a-consumer",
                    requires=(catalog,),
                ),
            ),
            FeatureDefinition(
                "z-provider",
                install_provider,
                dependencies=FeatureServiceDependencies(
                    "z-provider",
                    provides=(catalog,),
                ),
            ),
        ]
    )

    assert [record.definition.feature_id for record in records] == [
        "z-provider",
        "a-consumer",
    ]
    assert events == ["provider", "consumer:True"]
    assert all(record.state is FeatureState.ACTIVE for record in records)


async def test_optional_service_does_not_block_activation_and_is_audited():
    runtime = FeatureRuntime()
    enhancer = ServiceKey[object]("enhancer")
    installed = False

    def install(_context) -> None:
        nonlocal installed
        installed = True

    await runtime.activate_many(
        [
            FeatureDefinition(
                "optional-consumer",
                install,
                dependencies=FeatureServiceDependencies(
                    "optional-consumer",
                    optional=(enhancer,),
                ),
            )
        ]
    )

    report = runtime.startup_audit()
    assert report.healthy
    assert installed
    assert report.features[0].missing_optional == ("enhancer",)


async def test_promised_service_must_be_owned_by_installing_generation():
    runtime = FeatureRuntime()
    catalog = ServiceKey[object]("catalog")

    record = await runtime.activate(
        FeatureDefinition(
            "empty-provider",
            lambda _context: None,
            dependencies=FeatureServiceDependencies(
                "empty-provider",
                provides=(catalog,),
            ),
        )
    )

    assert record.state is FeatureState.FAILED
    assert record.scope is not None
    assert record.scope.state is FeatureState.DISPOSED
    assert isinstance(record.error.cause, MissingProvidedServicesError)


async def test_startup_audit_names_waiting_feature_and_owned_registrations():
    runtime = FeatureRuntime()
    missing = ServiceKey[object]("missing")

    await runtime.activate(
        FeatureDefinition(
            "waiting",
            lambda _context: None,
            dependencies=FeatureServiceDependencies("waiting", requires=(missing,)),
        )
    )
    active = await runtime.activate(
        FeatureDefinition(
            "active",
            lambda context: context.register_disposer(
                lambda: None,
                label="resource:active",
            ),
        )
    )

    report = runtime.startup_audit()
    payload = report.as_dict()
    assert not report.healthy
    assert payload["issues"] == ["waiting"]
    waiting = next(item for item in payload["features"] if item["id"] == "waiting")
    active_payload = next(item for item in payload["features"] if item["id"] == "active")
    assert waiting["services"]["missing_required"] == ["missing"]
    assert active_payload["registrations"] == [
        {"label": "resource:active", "phase": "resource", "state": "active"}
    ]
    assert active.state is FeatureState.ACTIVE


async def test_runtime_reports_drain_timeout_without_marking_feature_disabled():
    runtime = FeatureRuntime()
    record = await runtime.activate(
        FeatureDefinition("provider", lambda _context: None, drain_timeout_seconds=0.01)
    )
    assert record.scope is not None
    lease = record.scope.acquire_lease("completion:1")

    assert await runtime.deactivate("provider") is False

    assert record.state is FeatureState.DRAINING
    assert isinstance(record.error, FeatureDrainTimeoutError)
    diagnostic = runtime.startup_audit().as_dict()["features"][0]
    assert diagnostic["lifecycle"]["active_leases"] == ["completion:1"]
    assert diagnostic["lifecycle"]["last_stop"]["timed_out"] is True

    unchanged = await runtime.activate(record.definition)
    assert unchanged is record
    assert unchanged.generation is not None
    assert unchanged.generation.sequence == 1

    lease.release()
    assert await runtime.deactivate("provider", timeout_seconds=1) is True


async def test_runtime_records_restart_requirement_without_disposing_generation():
    runtime = FeatureRuntime()
    record = await runtime.activate(
        FeatureDefinition(
            "gateway-routes",
            lambda context: context.register_disposer(
                lambda: None,
                label="route:gateway",
            ),
            stop_policy=FeatureStopPolicy.RESTART_REQUIRED,
        )
    )

    assert await runtime.deactivate("gateway-routes") is False

    assert record.state is FeatureState.ACTIVE
    assert record.restart_required
    assert record.scope is not None
    diagnostic = runtime.startup_audit().as_dict()["features"][0]
    assert diagnostic["lifecycle"]["restart_required"] is True
