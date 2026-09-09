"""4D-1 Channels Feature ownership and generation contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crew.channels import (
    CHANNELS_FEATURE_ID,
    CHANNELS_SERVICE_KEY,
    ChannelsService,
    build_channels_feature,
)
from crew.channels.platform_registry import PlatformEntry, platform_registry
from crew.core.mocks import InMemorySessionStore
from crew.features import FeatureRuntime, FeatureState
from crew.tools.registry import Registry


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        channel_config=lambda _name, owner_account_id: {},
        owner_env_map=lambda _owner: {},
    )


def _crew(config=None, channel_handler=None) -> SimpleNamespace:
    return SimpleNamespace(
        config=config or _config(),
        channel_bindings=None,
        channel_handler=channel_handler or (lambda envelope: _empty_stream()),
        session_store=InMemorySessionStore(),
    )


async def _empty_stream():
    return
    yield  # pragma: no cover


@pytest.fixture
def clean_registry():
    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    yield
    platform_registry._entries.clear()
    for entry in old_entries:
        platform_registry.register(entry)


def _build(crew, registry, revision=1):
    return build_channels_feature(
        crew,
        registry=registry,
        session_store=crew.session_store,
        desired_config_revision=revision,
    )


@pytest.mark.asyncio
async def test_channels_activation_publishes_service_and_deactivation_stops_all(clean_registry):
    crew = _crew()
    runtime = FeatureRuntime()
    registry = Registry()
    bundle = _build(crew, registry)

    record = await runtime.activate(bundle.definition)
    assert record.state is FeatureState.ACTIVE
    service = runtime.services.resolve(CHANNELS_SERVICE_KEY)
    assert isinstance(service, ChannelsService)
    assert crew.channel_manager is service.channel_manager
    assert crew.delivery_router is service.delivery_router

    # Register a synthetic channel and start it.
    started = asyncio.Event()
    stopped = asyncio.Event()

    class _Channel:
        name = "test"

        async def start(self, handler):
            started.set()

        async def stop(self):
            stopped.set()

    service.channel_manager.register(_Channel(), owner_account_id="owner-1")
    await service.channel_manager.start_all(crew.channel_handler, owner_account_id="owner-1")
    assert started.is_set()

    assert await runtime.deactivate(CHANNELS_FEATURE_ID) is True
    assert stopped.is_set()
    assert crew.channel_manager is None
    assert crew.delivery_router is None


@pytest.mark.asyncio
async def test_channels_update_allocates_new_manager_and_stops_old(clean_registry):
    crew = _crew()
    runtime = FeatureRuntime()
    registry = Registry()
    bundle = _build(crew, registry)

    await runtime.activate(bundle.definition)
    old_service = runtime.services.resolve(CHANNELS_SERVICE_KEY)
    old_manager = old_service.channel_manager

    replacement = _build(crew, registry, revision=2)
    result = await runtime.update(replacement.definition)
    assert result.updated is True

    new_service = runtime.services.resolve(CHANNELS_SERVICE_KEY)
    assert new_service.channel_manager is not old_manager
    assert new_service.delivery_router is not old_service.delivery_router

    await runtime.deactivate(CHANNELS_FEATURE_ID)


@pytest.mark.asyncio
async def test_channels_consumer_fail_closed_after_deactivation(clean_registry):
    crew = _crew()
    runtime = FeatureRuntime()
    registry = Registry()
    bundle = _build(crew, registry)

    await runtime.activate(bundle.definition)
    await runtime.deactivate(CHANNELS_FEATURE_ID)

    assert runtime.services.get(CHANNELS_SERVICE_KEY) is None
    assert crew.channel_manager is None
    assert crew.delivery_router is None

    # Service resolution must fail-closed after deactivation.
    from crew.features import ServiceNotFoundError
    with pytest.raises(ServiceNotFoundError):
        runtime.services.resolve(CHANNELS_SERVICE_KEY)


@pytest.mark.asyncio
async def test_channels_feature_registers_new_conversation_tool(clean_registry):
    crew = _crew()
    runtime = FeatureRuntime()
    registry = Registry()
    bundle = _build(crew, registry)

    await runtime.activate(bundle.definition)
    assert "new_conversation" in registry.names()

    await runtime.deactivate(CHANNELS_FEATURE_ID)
    assert "new_conversation" not in registry.names()


@pytest.mark.asyncio
async def test_channels_feature_registers_platform_channels(clean_registry):
    crew = _crew()
    runtime = FeatureRuntime()
    registry = Registry()

    class _Adapter:
        name = "testplat"

        async def start(self, handler):
            pass

        async def stop(self):
            pass

        def bind_app(self, app):
            pass

    platform_registry.register(
        PlatformEntry(
            name="testplat",
            label="Test Platform",
            adapter_factory=lambda cfg: _Adapter(),
            check_fn=lambda: True,
            is_connected=lambda cfg: True,
        )
    )
    bundle = _build(crew, registry)
    await runtime.activate(bundle.definition)
    service = runtime.services.resolve(CHANNELS_SERVICE_KEY)
    assert service.channel_manager.get("testplat", "local") is not None

    await runtime.deactivate(CHANNELS_FEATURE_ID)


@pytest.mark.asyncio
async def test_production_build_app_wires_channels_feature(tmp_path):
    from crew.app import build_app
    from crew.state.config import Config

    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )
    try:
        runtime = app.plugins.feature_runtime
        record = runtime.get(CHANNELS_FEATURE_ID)
        assert record is not None
        assert record.state is FeatureState.DISCOVERED
        assert app.channel_manager is not None
        assert app.delivery_router is not None
    finally:
        await app.shutdown()
