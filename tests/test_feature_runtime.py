"""Feature Runtime ownership, rollback, and async cleanup behavior."""

from __future__ import annotations

import asyncio

import pytest

from crew.features import (
    FeatureActivationError,
    FeatureCleanupError,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureTransaction,
    RegistrationState,
)


def test_generation_has_stable_diagnostic_key_and_validates_identity():
    generation = FeatureGeneration("  wiki  ", 2)

    assert generation.feature_id == "wiki"
    assert generation.key == "wiki@2"
    with pytest.raises(ValueError, match="feature_id"):
        FeatureGeneration(" ", 1)
    with pytest.raises(ValueError, match="sequence"):
        FeatureGeneration("wiki", 0)


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
