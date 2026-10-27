"""Contract tests for the host feature capability snapshot."""

from __future__ import annotations

import asyncio

import pytest

from crew.features import (
    FeatureDefinition,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    ServiceKey,
)


def _definition(feature_id: str, *, revision: int = 1, replace: bool = False, fail: bool = False):
    async def install(_context):
        if fail:
            raise RuntimeError("install failed")

    return FeatureDefinition(
        feature_id,
        install,
        desired_config_revision=revision,
        update_strategy=(FeatureUpdateStrategy.REPLACE if replace else FeatureUpdateStrategy.RESTART),
    )


@pytest.mark.asyncio
async def test_capability_snapshot_tracks_discovery_activation_and_deactivation():
    runtime = FeatureRuntime()
    runtime.discover(_definition("product.team"))
    runtime.discover(_definition("product.dynamic-kanban"))

    assert runtime.capability_snapshot() == {
        "product.dynamic-kanban": {
            "state": "discovered", "available": False, "generation": None,
        },
        "product.team": {
            "state": "discovered", "available": False, "generation": None,
        },
    }

    await runtime.activate(_definition("product.team"))
    snapshot = runtime.capability_snapshot()
    assert snapshot["product.team"]["state"] == "active"
    assert snapshot["product.team"]["available"] is True
    assert snapshot["product.team"]["generation"] == "product.team@g1"
    assert snapshot["product.dynamic-kanban"]["available"] is False

    assert await runtime.deactivate("product.team") is True
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "discovered", "available": False, "generation": None,
    }


@pytest.mark.asyncio
async def test_capability_snapshot_is_fail_closed_for_activation_failure():
    runtime = FeatureRuntime()
    record = await runtime.activate(_definition("product.team", fail=True))

    assert record.state is FeatureState.FAILED
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "failed", "available": False, "generation": None,
    }


@pytest.mark.asyncio
async def test_replace_keeps_current_generation_as_capability_source():
    runtime = FeatureRuntime()
    await runtime.activate(_definition("product.team", replace=True))
    before = runtime.capability_snapshot()["product.team"]

    result = await runtime.update(_definition("product.team", revision=2, replace=True))

    assert result.updated is True
    after = runtime.capability_snapshot()["product.team"]
    assert before["generation"] == "product.team@g1"
    assert after == {
        "state": "active", "available": True, "generation": "product.team@g2",
    }


@pytest.mark.asyncio
async def test_snapshot_marks_waiting_feature_unavailable_until_required_service_exists():
    runtime = FeatureRuntime()
    dependency = ServiceKey[object]("dependency")
    definition = _definition("product.team")
    definition = FeatureDefinition(
        definition.feature_id,
        definition.install,
        dependencies=FeatureServiceDependencies(
            definition.feature_id,
            requires=(dependency,),
        ),
    )

    record = await runtime.activate(definition)

    assert record.state is FeatureState.WAITING
    assert runtime.capability_snapshot()[definition.feature_id] == {
        "state": "waiting", "available": False, "generation": None,
    }


@pytest.mark.asyncio
async def test_snapshot_becomes_unavailable_immediately_when_draining_holds_a_lease():
    runtime = FeatureRuntime()
    record = await runtime.activate(
        FeatureDefinition(
            "product.team",
            lambda _context: None,
            stop_policy=FeatureStopPolicy.DRAIN,
        )
    )
    lease = record.scope.acquire_lease("test:active")

    record.scope.begin_draining()

    snapshot = runtime.capability_snapshot()["product.team"]
    assert snapshot["state"] == "draining"
    assert snapshot["available"] is False
    assert snapshot["generation"] == "product.team@g1"

    stop_task = asyncio.create_task(runtime.deactivate("product.team"))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(stop_task), timeout=0.05)
    finally:
        lease.release()
    assert await asyncio.wait_for(stop_task, timeout=1) is True


@pytest.mark.asyncio
async def test_replace_publishes_candidate_before_old_generation_drains():
    runtime = FeatureRuntime()
    candidate_started = asyncio.Event()
    candidate_release = asyncio.Event()

    async def install(context):
        if context.generation.sequence == 2:
            candidate_started.set()
            await candidate_release.wait()

    old = FeatureDefinition(
        "product.team", install, update_strategy=FeatureUpdateStrategy.REPLACE
    )
    record = await runtime.activate(old)
    old_lease = record.scope.acquire_lease("test:old")

    update_task = asyncio.create_task(
        runtime.update(
            FeatureDefinition(
                "product.team",
                install,
                desired_config_revision=2,
                update_strategy=FeatureUpdateStrategy.REPLACE,
            )
        )
    )
    await asyncio.wait_for(candidate_started.wait(), timeout=1)
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "active", "available": True, "generation": "product.team@g1",
    }

    candidate_release.set()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(update_task), timeout=0.05)
        assert runtime.capability_snapshot()["product.team"] == {
            "state": "active", "available": True, "generation": "product.team@g2",
        }
    finally:
        old_lease.release()
    result = await asyncio.wait_for(update_task, timeout=1)
    assert result.updated is True
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "active", "available": True, "generation": "product.team@g2",
    }


@pytest.mark.asyncio
async def test_failed_replace_keeps_old_generation_and_snapshot():
    runtime = FeatureRuntime()
    old = _definition("product.team", replace=True)
    await runtime.activate(old)

    result = await runtime.update(
        _definition("product.team", revision=2, replace=True, fail=True)
    )

    assert result.updated is False
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "active", "available": True, "generation": "product.team@g1",
    }


@pytest.mark.asyncio
async def test_restart_failure_restores_old_capability_as_new_generation():
    runtime = FeatureRuntime()
    installs = 0

    async def install(context):
        nonlocal installs
        installs += 1
        if installs == 2:
            raise RuntimeError("candidate failed")

    old = FeatureDefinition("product.team", install)
    await runtime.activate(old)
    result = await runtime.update(
        FeatureDefinition("product.team", install, desired_config_revision=2)
    )

    assert result.updated is False
    assert result.restored is True
    assert runtime.capability_snapshot()["product.team"] == {
        "state": "active", "available": True, "generation": "product.team@g3",
    }


@pytest.mark.asyncio
async def test_snapshot_is_read_only_and_does_not_acquire_a_lease_or_change_state():
    runtime = FeatureRuntime()
    await runtime.activate(_definition("product.team"))
    before = runtime.startup_audit().features[0]

    snapshot = runtime.capability_snapshot()
    after = runtime.startup_audit().features[0]

    assert snapshot["product.team"]["available"] is True
    assert before.active_leases == after.active_leases == ()
    assert before.state == after.state == "active"
