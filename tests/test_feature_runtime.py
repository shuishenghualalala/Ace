"""Feature Runtime ownership, rollback, and async cleanup behavior."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from crew.features import (
    FeatureActivationError,
    FeatureCleanupError,
    FeatureConfigRevisions,
    FeatureDrainTimeoutError,
    FeatureGeneration,
    FeatureLeaseUnavailableError,
    FeatureRestartRequiredError,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
    FeatureTransaction,
    RegistrationState,
    StaleFeatureGenerationError,
)


def test_generation_has_stable_diagnostic_key_and_validates_identity():
    generation = FeatureGeneration("  wiki  ", 2)

    assert generation.feature_id == "wiki"
    assert generation.key == "wiki@g2"
    with pytest.raises(ValueError, match="feature_id"):
        FeatureGeneration(" ", 1)
    with pytest.raises(ValueError, match="sequence"):
        FeatureGeneration("wiki", 0)
    with pytest.raises(ValueError, match="config revision"):
        FeatureGeneration("wiki", 1, desired_config_revision=0)


def test_config_revisions_only_publish_the_latest_desired_generation():
    revisions = FeatureConfigRevisions("wiki")
    first = revisions.new_generation(sequence=1)

    revisions.mark_effective(first)
    assert revisions.desired_config_revision == 1
    assert revisions.effective_config_revision == 1

    revisions.request(2)
    second = revisions.new_generation(sequence=2)
    revisions.request(3)

    with pytest.raises(StaleFeatureGenerationError):
        revisions.mark_effective(second)
    assert revisions.effective_config_revision == 1

    third = revisions.new_generation(sequence=3)
    revisions.mark_effective(third)
    assert revisions.desired_config_revision == 3
    assert revisions.effective_config_revision == 3

    revisions.request(4)
    restored = revisions.generation_for(sequence=4, config_revision=3)
    revisions.mark_restored(restored)
    assert revisions.desired_config_revision == 4
    assert revisions.effective_config_revision == 3


async def test_scope_awaits_async_cleanup_and_disposes_in_lifo_order():
    scope = FeatureScope(FeatureGeneration("browser", 1))
    events: list[str] = []
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    def dispose_first() -> None:
        events.append("first")

    async def dispose_second() -> None:
        events.append("second:start")
        cleanup_started.set()
        await cleanup_release.wait()
        events.append("second:end")

    scope.register(dispose_first, label="process:browser")
    scope.register(dispose_second, label="connection:browser")
    scope.activate()

    disposal = asyncio.create_task(scope.dispose())
    await cleanup_started.wait()

    assert scope.state is FeatureState.STOPPING
    assert not disposal.done()
    assert events == ["second:start"]

    cleanup_release.set()
    await disposal

    assert events == ["second:start", "second:end", "first"]
    assert scope.state is FeatureState.DISPOSED
    assert [token.state for token in scope.registrations] == [
        RegistrationState.DISPOSED,
        RegistrationState.DISPOSED,
    ]


async def test_scope_aggregates_cleanup_errors_without_skipping_remaining_tokens():
    scope = FeatureScope(FeatureGeneration("wiki", 3))
    events: list[str] = []

    def dispose_first() -> None:
        events.append("first")

    def dispose_broken() -> None:
        events.append("broken")
        raise RuntimeError("database close failed")

    def dispose_last() -> None:
        events.append("last")

    scope.register(dispose_first, label="service:knowledge")
    broken = scope.register(dispose_broken, label="store:wiki")
    scope.register(dispose_last, label="tool:wiki_query")
    scope.activate()

    with pytest.raises(FeatureCleanupError) as captured:
        await scope.dispose()

    assert events == ["last", "broken", "first"]
    assert scope.state is FeatureState.FAILED
    assert broken.state is RegistrationState.FAILED
    assert [issue.label for issue in captured.value.issues] == ["store:wiki"]
    assert str(captured.value.issues[0].error) == "database close failed"


async def test_registration_token_is_single_shot_for_concurrent_and_repeated_callers():
    scope = FeatureScope(FeatureGeneration("cron", 1))
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    calls = 0

    async def dispose_job() -> None:
        nonlocal calls
        calls += 1
        cleanup_started.set()
        await cleanup_release.wait()

    token = scope.register(dispose_job, label="task:scheduler")
    first = asyncio.create_task(token.dispose())
    second = asyncio.create_task(token.dispose())
    await cleanup_started.wait()

    assert calls == 1
    assert token.state is RegistrationState.DISPOSING

    cleanup_release.set()
    await asyncio.gather(first, second)
    await token.dispose()

    assert calls == 1
    assert token.state is RegistrationState.DISPOSED


async def test_scope_rejects_registration_after_shutdown_begins():
    scope = FeatureScope(FeatureGeneration("sites", 1))
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def dispose_server() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    scope.register(dispose_server, label="server:preview")
    scope.activate()
    disposal = asyncio.create_task(scope.dispose())
    await cleanup_started.wait()

    with pytest.raises(RuntimeError, match="cannot register while stopping"):
        scope.register(lambda: None, label="route:too-late")

    cleanup_release.set()
    await disposal


async def test_transaction_rolls_back_partial_activation_and_preserves_failure_context():
    generation = FeatureGeneration("external-agent", 4)
    transaction = FeatureTransaction(generation)
    events: list[str] = []

    with pytest.raises(FeatureActivationError) as captured:
        async with transaction:
            transaction.register(lambda: events.append("first"), label="service:catalog")
            transaction.register(lambda: events.append("second"), label="process:runtime")
            raise ValueError("install failed")

    assert events == ["second", "first"]
    assert captured.value.generation == generation
    assert isinstance(captured.value.cause, ValueError)
    assert captured.value.cleanup_error is None
    assert transaction.scope.state is FeatureState.DISPOSED


async def test_transaction_without_commit_rolls_back_on_clean_context_exit():
    transaction = FeatureTransaction(FeatureGeneration("optional-feature", 1))
    disposed = False

    def dispose_resource() -> None:
        nonlocal disposed
        disposed = True

    async with transaction:
        transaction.register(dispose_resource, label="resource:optional")

    assert not transaction.committed
    assert disposed
    assert transaction.scope.state is FeatureState.DISPOSED


async def test_committed_transaction_stays_active_until_scope_is_disposed():
    transaction = FeatureTransaction(FeatureGeneration("agent-loop", 1))
    disposed = False

    def dispose_loop() -> None:
        nonlocal disposed
        disposed = True

    async with transaction:
        transaction.register(dispose_loop, label="runtime:agent-loop")
        scope = transaction.commit()

    assert transaction.committed
    assert scope.state is FeatureState.ACTIVE
    assert not disposed

    await scope.dispose()

    assert disposed
    assert scope.state is FeatureState.DISPOSED


async def test_activation_error_includes_rollback_failures():
    transaction = FeatureTransaction(FeatureGeneration("team", 1))

    def broken_cleanup() -> None:
        raise RuntimeError("worker did not stop")

    with pytest.raises(FeatureActivationError) as captured:
        async with transaction:
            transaction.register(broken_cleanup, label="worker:team")
            raise ValueError("install failed")

    error = captured.value
    assert isinstance(error.cause, ValueError)
    assert error.cleanup_error is not None
    assert [issue.label for issue in error.cleanup_error.issues] == ["worker:team"]
    assert transaction.scope.state is FeatureState.FAILED


async def test_drain_rejects_new_requests_and_waits_for_active_lease():
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    disposed = False

    def cleanup() -> None:
        nonlocal disposed
        disposed = True

    scope.register(cleanup, label="resource:wiki")
    scope.activate()
    lease = scope.acquire_lease("query:42")

    stopping = asyncio.create_task(
        scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scope.state is FeatureState.DRAINING
    assert not stopping.done()
    assert not disposed
    with pytest.raises(FeatureLeaseUnavailableError, match="draining"):
        scope.acquire_lease("query:late")

    lease.release()
    await stopping

    assert disposed
    assert scope.state is FeatureState.DISPOSED


async def test_cancel_interrupts_the_task_holding_a_lease_before_cleanup():
    scope = FeatureScope(FeatureGeneration("browser", 1))
    scope.register(lambda: None, label="resource:browser")
    scope.activate()
    acquired = asyncio.Event()
    leases = []

    async def run_request() -> None:
        async with scope.acquire_lease("action:navigate") as lease:
            leases.append(lease)
            acquired.set()
            await asyncio.Event().wait()

    request = asyncio.create_task(run_request())
    await acquired.wait()

    await scope.stop(FeatureStopPolicy.CANCEL, timeout_seconds=1)

    assert request.cancelled()
    assert leases[0].cancel_requested
    assert scope.stop_diagnostic is not None
    assert scope.stop_diagnostic.cancel_signalled
    assert scope.state is FeatureState.DISPOSED


async def test_drain_timeout_keeps_resources_and_can_be_retried():
    scope = FeatureScope(FeatureGeneration("provider", 1))
    disposed = False

    def cleanup() -> None:
        nonlocal disposed
        disposed = True

    scope.register(cleanup, label="resource:provider")
    scope.activate()
    lease = scope.acquire_lease("completion:slow")

    with pytest.raises(FeatureDrainTimeoutError) as captured:
        await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=0.01)

    assert captured.value.leases[0].label == "completion:slow"
    assert scope.state is FeatureState.DRAINING
    assert not disposed
    assert scope.stop_diagnostic is not None and scope.stop_diagnostic.timed_out

    lease.release()
    await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    assert disposed


async def test_immediate_stop_records_forced_cleanup_with_active_leases():
    scope = FeatureScope(FeatureGeneration("stateless", 1))
    scope.register(lambda: None, label="contribution:stateless")
    scope.activate()
    lease = scope.acquire_lease("request:existing")

    await scope.stop(FeatureStopPolicy.IMMEDIATE)

    assert scope.state is FeatureState.DISPOSED
    assert scope.stop_diagnostic is not None
    assert scope.stop_diagnostic.forced_leases == ("request:existing",)
    lease.release()


async def test_restart_required_leaves_generation_active_for_host_boundary():
    scope = FeatureScope(FeatureGeneration("gateway-routes", 1))
    scope.register(lambda: None, label="route:gateway")
    scope.activate()

    with pytest.raises(FeatureRestartRequiredError):
        await scope.stop(FeatureStopPolicy.RESTART_REQUIRED)

    assert scope.state is FeatureState.ACTIVE
    assert scope.stop_diagnostic is not None
    assert scope.stop_diagnostic.restart_required


async def test_cancelled_stop_caller_does_not_start_parallel_teardown():
    scope = FeatureScope(FeatureGeneration("provider", 1))
    cleanup_calls = 0

    def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    scope.register(cleanup, label="resource:provider")
    scope.activate()
    lease = scope.acquire_lease("request:held")
    first = asyncio.create_task(
        scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(
        scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    )
    lease.release()
    await second

    assert cleanup_calls == 1


async def test_scope_task_factory_registers_one_owner_and_cancels_and_joins_tasks():
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    scope.activate()
    cancelled = asyncio.Event()
    scheduled_with_owner = False

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original_create_task = asyncio.create_task

    def create_task_with_assertion(awaitable, **kwargs):
        nonlocal scheduled_with_owner
        scheduled_with_owner = any(
            token.label == "tasks:feature-scope" for token in scope.registrations
        )
        return original_create_task(awaitable, **kwargs)

    # The runtime calls asyncio.create_task only after registering the owner.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("crew.features.runtime.asyncio.create_task", create_task_with_assertion)
    try:
        first = scope.create_task(worker(), name="wiki-worker-1")
        second = scope.create_task(worker(), name="wiki-worker-2")
        assert scheduled_with_owner
        assert [
            token.label
            for token in scope.registrations
            if token.label == "tasks:feature-scope"
        ] == ["tasks:feature-scope"]

        await scope.dispose()
        assert first.cancelled()
        assert second.cancelled()
        assert cancelled.is_set()
        assert scope.state is FeatureState.DISPOSED
    finally:
        monkeypatch.undo()


async def test_scope_finished_task_does_not_keep_strong_reference():
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    scope.activate()

    async def finished() -> str:
        return "done"

    task = scope.create_task(finished(), name="wiki-finished")
    reference = weakref.ref(task)
    await task
    await asyncio.sleep(0)
    del task
    gc.collect()

    assert reference() is None
    await scope.dispose()


async def test_scope_task_admission_closes_on_drain_and_resume_reopens_after_timeout():
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    scope.activate()
    lease = scope.acquire_lease("request:held")

    with pytest.raises(FeatureDrainTimeoutError):
        await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=0.001)
    assert scope.state is FeatureState.DRAINING

    async def rejected() -> None:
        raise AssertionError("rejected coroutine must never run")

    coroutine = rejected()
    with pytest.raises(FeatureLeaseUnavailableError):
        scope.create_task(coroutine, name="wiki-rejected")
    assert coroutine.cr_frame is None

    scope.resume_active()
    accepted = scope.create_task(asyncio.sleep(0), name="wiki-resumed")
    await accepted
    lease.release()
    await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    assert scope.state is FeatureState.DISPOSED


async def test_scope_stop_and_rollback_are_idempotent():
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    calls = 0

    def cleanup() -> None:
        nonlocal calls
        calls += 1

    scope.register(cleanup, label="resource:wiki")
    scope.activate()
    await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    await scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    await scope.rollback()
    await scope.rollback()
    assert calls == 1
    assert scope.state is FeatureState.DISPOSED
