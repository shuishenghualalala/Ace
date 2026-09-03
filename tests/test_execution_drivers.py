"""Execution Driver Registry ownership and CrewApp routing behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crew.app import build_app
from crew.core.envelope import Envelope, ResponseChunk
from crew.features import (
    ExecutionDriver,
    ExecutionDriverConflictError,
    ExecutionDriverRegistry,
    ExecutionDriverUnavailableError,
    FeatureDefinition,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
)
from crew.state.config import Config


def _active_scope(feature_id: str, sequence: int = 1) -> FeatureScope:
    scope = FeatureScope(FeatureGeneration(feature_id, sequence))
    scope.activate()
    return scope


async def _unused_driver(envelope: Envelope):
    yield ResponseChunk.final(envelope.request_id, "unused")


def test_driver_definition_requires_stable_open_mode_identifier() -> None:
    driver = ExecutionDriver(
        " feature.echo ",
        _unused_driver,
        capabilities=("echo", "echo", ""),
    )

    assert driver.mode == "feature.echo"
    assert driver.capabilities == ("echo",)
    with pytest.raises(ValueError, match="lowercase"):
        ExecutionDriver("Feature Echo", _unused_driver)


def test_registry_rejects_unrelated_mode_owner() -> None:
    registry = ExecutionDriverRegistry()
    registry.register(_active_scope("first"), ExecutionDriver("echo", _unused_driver))

    with pytest.raises(ExecutionDriverConflictError, match="first@g1"):
        registry.register(
            _active_scope("second"),
            ExecutionDriver("echo", _unused_driver),
        )


async def test_new_generation_stages_then_replaces_driver_atomically() -> None:
    registry = ExecutionDriverRegistry()
    old_scope = _active_scope("echo-feature", 1)

    async def old(envelope: Envelope):
        yield ResponseChunk.final(envelope.request_id, "old")

    async def new(envelope: Envelope):
        yield ResponseChunk.final(envelope.request_id, "new")

    registry.register(old_scope, ExecutionDriver("echo", old))
    new_scope = FeatureScope(FeatureGeneration("echo-feature", 2))
    registry.register(new_scope, ExecutionDriver("echo", new))

    assert registry.resolve("echo").generation.key == "echo-feature@g1"
    new_scope.activate()
    assert registry.resolve("echo").generation.key == "echo-feature@g2"

    await old_scope.dispose()
    assert registry.resolve("echo").generation.key == "echo-feature@g2"


async def test_dispatch_lease_keeps_driver_generation_alive_during_drain() -> None:
    registry = ExecutionDriverRegistry()
    scope = _active_scope("stream-feature")
    started = asyncio.Event()
    release = asyncio.Event()

    async def stream(envelope: Envelope):
        started.set()
        await release.wait()
        yield ResponseChunk.final(envelope.request_id, "done")

    registry.register(scope, ExecutionDriver("stream", stream))
    envelope = Envelope.of("go", session_id="s1", mode="stream")
    consuming = asyncio.create_task(_collect(registry.dispatch(envelope)))
    await started.wait()

    stopping = asyncio.create_task(
        scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    )
    while scope.state is not FeatureState.DRAINING:
        await asyncio.sleep(0)

    assert not stopping.done()
    with pytest.raises(ExecutionDriverUnavailableError):
        registry.resolve("stream")

    release.set()
    chunks = await consuming
    await stopping
    assert chunks[-1].body["text"] == "done"


async def _collect(stream) -> list[ResponseChunk]:
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_app_routes_registered_custom_mode_without_handle_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )
    assert {"agent", "agent.default", "dynamic_kanban"} <= set(
        app.execution_drivers.modes()
    )

    async def echo(envelope: Envelope):
        yield ResponseChunk.final(envelope.request_id, envelope.query.upper())

    feature_id = "feature.echo"
    try:
        record = await app.plugins.feature_runtime.activate(
            FeatureDefinition(
                feature_id,
                lambda context: context.register_execution_driver(
                    ExecutionDriver(
                        "feature.echo",
                        echo,
                        capabilities=("demo.echo",),
                    )
                ),
            )
        )
        assert record.state is FeatureState.ACTIVE

        chunks = [
            chunk
            async for chunk in app.handle(
                Envelope.of("hello", session_id="s1", mode="feature.echo")
            )
        ]
        assert chunks[-1].body["text"] == "HELLO"

        assert await app.plugins.feature_runtime.deactivate(feature_id)
        unavailable = [
            chunk
            async for chunk in app.handle(
                Envelope.of("hello", session_id="s2", mode="feature.echo")
            )
        ]
        assert len(unavailable) == 1
        assert unavailable[0].kind == "error"
        assert unavailable[0].status == "failed"
        assert unavailable[0].body == {
            "message": "执行能力不可用：feature.echo",
            "code": "capability_unavailable",
        }
    finally:
        await app.plugins.feature_runtime.deactivate(feature_id)
        await app.shutdown()


@pytest.mark.asyncio
async def test_team_adapter_registers_mode_and_preserves_session_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )

    class RecordingTeam:
        def __init__(self) -> None:
            self.envelopes: list[Envelope] = []

        async def interact(self, envelope: Envelope):
            self.envelopes.append(envelope)
            yield ResponseChunk.final(envelope.request_id, "team")

    team = RecordingTeam()
    monkeypatch.setattr(
        app,
        "_session_agent_config",
        lambda *_args, **_kwargs: {
            "team": {"external_team_id": "configured-team"}
        },
    )
    try:
        assert "team" not in app.execution_drivers.modes()
        unavailable = [
            chunk
            async for chunk in app.handle(
                Envelope.of("hello", session_id="disabled", mode="team")
            )
        ]
        assert unavailable[-1].body["code"] == "capability_unavailable"

        app.set_team_manager(team)
        assert "team" in app.execution_drivers.modes()

        envelope = Envelope.of("hello", session_id="s1", mode="team")
        chunks = [chunk async for chunk in app.handle(envelope)]

        assert chunks[-1].body["text"] == "team"
        assert team.envelopes == [envelope]
        assert envelope.params["external_team_id"] == "configured-team"
    finally:
        await app.shutdown()

    assert "team" not in app.execution_drivers.modes()
    assert "dynamic_kanban" not in app.execution_drivers.modes()
