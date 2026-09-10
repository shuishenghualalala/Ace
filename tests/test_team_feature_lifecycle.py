"""4C-1 Team Feature Foundation 生命周期契约。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.app import build_app
from crew.core.envelope import Envelope, ResponseChunk
from crew.features import (
    ExecutionDriverUnavailableError,
    FeatureRuntime,
    FeatureState,
)
from crew.state.config import Config
from crew.team.feature import TEAM_FEATURE_ID, TEAM_SERVICE_KEY, build_team_feature


def _config() -> Config:
    return Config(
        db_path=":memory:",
        memory_db_path=":memory:",
        cron_enabled=False,
        api_key="",
    )


def _build_feature(host, revision=1, enabled=True):
    return build_team_feature(
        host,
        registry=SimpleNamespace(),
        session_store=SimpleNamespace(),
        memory=SimpleNamespace(),
        plugins=SimpleNamespace(),
        tasks=SimpleNamespace(),
        config=_config(),
        external_store_provider=lambda: None,
        external_services_acquirer=lambda: None,
        interaction_bridge=None,
        kanban_consumer_provider=lambda: None,
        context_contributors=SimpleNamespace(),
        provider=SimpleNamespace(),
        enabled=enabled,
        desired_config_revision=revision,
    )


@pytest.mark.asyncio
async def test_team_activation_publishes_driver_and_service():
    host = SimpleNamespace(team=None)
    runtime = FeatureRuntime()
    bundle = _build_feature(host)

    record = await runtime.activate(bundle.definition)
    assert record.state is FeatureState.ACTIVE
    assert "team" in runtime.execution_drivers.modes()
    service = runtime.services.get(TEAM_SERVICE_KEY)
    assert service is bundle.manager

    assert await runtime.deactivate(TEAM_FEATURE_ID) is True
    assert "team" not in runtime.execution_drivers.modes()
    assert runtime.services.get(TEAM_SERVICE_KEY) is None


@pytest.mark.asyncio
async def test_team_driver_fills_external_team_id_from_session_config():
    host = SimpleNamespace(team=None)
    runtime = FeatureRuntime()
    bundle = _build_feature(host)
    await runtime.activate(bundle.definition)

    received: list[Envelope] = []

    async def interact(envelope):
        received.append(envelope)
        yield ResponseChunk.final(envelope.request_id, "ok")

    manager = runtime.services.get(TEAM_SERVICE_KEY)
    manager.interact = interact

    host._session_agent_config = lambda *_args, **_kwargs: {
        "team": {"external_team_id": "configured-team"}
    }

    envelope = Envelope.of("run", session_id="s1", request_id="r1", mode="team")
    chunks = [chunk async for chunk in runtime.execution_drivers.dispatch(envelope)]
    assert chunks[-1].body["text"] == "ok"
    assert len(received) == 1
    assert received[0].params.get("external_team_id") == "configured-team"

    await runtime.deactivate(TEAM_FEATURE_ID)


@pytest.mark.asyncio
async def test_team_update_allocates_fresh_manager_and_shuts_down_old():
    host = SimpleNamespace(team=None)
    runtime = FeatureRuntime()
    first = _build_feature(host)
    await runtime.activate(first.definition)
    old_manager = runtime.services.get(TEAM_SERVICE_KEY)
    assert old_manager is not None

    second = _build_feature(host, revision=2)
    result = await runtime.update(second.definition)
    assert result.updated is True
    new_manager = runtime.services.get(TEAM_SERVICE_KEY)
    assert new_manager is not old_manager
    assert old_manager._closed is True

    await runtime.deactivate(TEAM_FEATURE_ID)


@pytest.mark.asyncio
async def test_team_deactivate_rejects_new_run_and_closes_manager():
    host = SimpleNamespace(team=None)
    runtime = FeatureRuntime()
    bundle = _build_feature(host)
    await runtime.activate(bundle.definition)
    manager = runtime.services.get(TEAM_SERVICE_KEY)

    await runtime.deactivate(TEAM_FEATURE_ID)
    assert manager._closed is True

    with pytest.raises(ExecutionDriverUnavailableError):
        await _collect(
            runtime.execution_drivers.dispatch(
                Envelope.of("run", session_id="s2", request_id="r2", mode="team")
            )
        )


@pytest.mark.asyncio
async def test_team_deactivate_consumer_fail_closed_via_app_property(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
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
        assert app.team is not None
        runtime = app.plugins.feature_runtime
        await runtime.deactivate(TEAM_FEATURE_ID)
        assert app.team is None
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_production_build_app_wires_team_feature_and_driver_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
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
        record = runtime.get(TEAM_FEATURE_ID)
        assert record is not None
        assert record.state is FeatureState.ACTIVE
        assert app.team is not None
        assert "team" in app.execution_drivers.modes()

        manager = app.team
        received: list[Envelope] = []

        async def interact(envelope):
            received.append(envelope)
            yield ResponseChunk.final(envelope.request_id, "team-ok")

        manager.interact = interact
        chunks = [
            chunk
            async for chunk in app.handle(
                Envelope.of("run", session_id="prod", request_id="r1", mode="team")
            )
        ]
        assert chunks[-1].body["text"] == "team-ok"
        assert len(received) == 1
    finally:
        await app.shutdown()


def test_team_feature_disabled_no_contributions(tmp_path, monkeypatch):
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
    assert app.team is None
    assert "team" not in app.execution_drivers.modes()


async def _collect(stream) -> list[ResponseChunk]:
    return [chunk async for chunk in stream]
