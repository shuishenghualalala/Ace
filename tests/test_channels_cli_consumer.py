"""Channels CLI/Router 消费路径收口测试 (4D-2).

验证点:
- CLI 不再本地 new ChannelManager 兜底,channel_manager 缺失时直接报错。
- CLI 命令经 app.channel_manager 消费。
- router 工厂签名向后兼容(显式传入/动态解析均可)。
- router helper 下沉到 crew.channels.router_helpers 后,CLI 与 router 共享重启逻辑。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from crew.channels.router_helpers import _restart_channel
from crew.cli.app import CliError
from crew.cli.integration import _channel_connect, _channel_disconnect, _channel_manager
from crew.gateway.routers.channels import create_channels_router


class _FakeConfig:
    def persist_channel_config(self, name, payload, *, owner_account_id):
        pass


class _FakeChannelManager:
    def __init__(self) -> None:
        self._channels: dict[tuple[str, str], Any] = {}
        self._status: list[dict[str, Any]] = []
        self._busy: set[tuple[str, str]] = set()

    def get(self, name: str, owner: str) -> Any:
        return self._channels.get((name, owner))

    def status(self, owner: str) -> list[dict[str, Any]]:
        return [row for row in self._status if row.get("owner_account_id", "") == owner]

    def is_busy(self, name: str, owner: str) -> bool:
        return (name, owner) in self._busy

    def lock_for(self, name: str, owner: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _guard():
            yield

        return _guard()

    async def stop_one(self, name: str, owner: str):
        self._channels.pop((name, owner), None)
        return SimpleNamespace(error=None)

    async def restart_one(self, name, channel, handler, *, owner_account_id):
        self._channels[(name, owner_account_id)] = channel
        return SimpleNamespace(running=True, error=None)

    def record_error(self, name: str, error: str, owner: str) -> None:
        self._status.append({"name": name, "owner_account_id": owner, "error": error})


class _FakeCrew:
    def __init__(self, manager=None, router=None) -> None:
        self.config = _FakeConfig()
        self.channel_manager = manager
        self.delivery_router = router
        self.channel_handler = None

    def dispatch(self, envelope):
        return
        yield  # pragma: no cover


def test_channel_manager_missing_raises_instead_of_local_new():
    app = _FakeCrew(manager=None)
    with pytest.raises(CliError, match="渠道服务未就绪"):
        _channel_manager(app)
    assert app.channel_manager is None


def test_channel_manager_returns_existing():
    manager = _FakeChannelManager()
    app = _FakeCrew(manager=manager)
    assert _channel_manager(app) is manager


def test_router_factory_accepts_none_and_resolves_from_crew():
    manager = _FakeChannelManager()
    crew = _FakeCrew(manager=manager, router=None)
    router = create_channels_router(crew, dispatcher=None, channel_manager=None)
    assert router is not None


def test_router_factory_explicit_manager_overrides_crew_attribute():
    crew_manager = _FakeChannelManager()
    explicit_manager = _FakeChannelManager()
    crew = _FakeCrew(manager=crew_manager, router=None)
    router = create_channels_router(crew, dispatcher=None, channel_manager=explicit_manager)
    assert router is not None


@pytest.mark.asyncio
async def test_restart_channel_uses_shared_router_helper(monkeypatch):
    """CLI _restart_channel 应调用 router_helpers._restart_channel 而非本地实现。"""
    from crew.channels.platform_registry import PlatformEntry, platform_registry

    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    platform_registry.register(
        PlatformEntry(
            name="stubplat",
            label="Stub",
            adapter_factory=lambda cfg: object(),
            validate_config=lambda cfg: True,
            is_connected=lambda cfg: True,
        )
    )

    calls = []

    async def fake_restart(app, name, owner, *, channel_manager):
        calls.append((app, name, owner, channel_manager))
        return True, {"name": name, "owner_account_id": owner}

    monkeypatch.setattr("crew.cli.integration._restart_platform", fake_restart)

    manager = _FakeChannelManager()
    app = _FakeCrew(manager=manager)
    ctx = SimpleNamespace(app=app, owner="owner-1")

    try:
        await _channel_connect(SimpleNamespace(platform="stubplat"), ctx)
        assert len(calls) == 1
        assert calls[0][3] is manager
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)


@pytest.mark.asyncio
async def test_restart_channel_helper_routes_through_channel_manager():
    """router_helpers._restart_channel 真实消费 channel_manager 并注册 delivery_router。"""
    from crew.channels.platform_registry import PlatformEntry, platform_registry

    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()

    class _StubChannel:
        name = "stubplat"

        async def start(self, handler):
            pass

        async def stop(self):
            pass

        def bind_app(self, app):
            pass

        async def send_to_target(self, target, message):
            return True

    platform_registry.register(
        PlatformEntry(
            name="stubplat",
            label="Stub Platform",
            adapter_factory=lambda cfg: _StubChannel(),
            validate_config=lambda cfg: True,
            is_connected=lambda cfg: True,
        )
    )

    manager = _FakeChannelManager()
    router = SimpleNamespace(registered=[], unregistered=[])

    def fake_register(name, sender, *, owner_account_id):
        router.registered.append((name, owner_account_id))

    def fake_unregister(name, *, owner_account_id):
        router.unregistered.append((name, owner_account_id))

    router.register = fake_register
    router.unregister = fake_unregister

    crew = _FakeCrew(manager=manager, router=router)

    try:
        ok, _status = await _restart_channel(crew, "stubplat", "owner-1")
        assert ok is True
        assert manager.get("stubplat", "owner-1") is not None
        assert ("stubplat", "owner-1") in router.registered
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)


@pytest.mark.asyncio
async def test_disconnect_uses_channel_manager():
    from crew.channels.platform_registry import PlatformEntry, platform_registry

    old_entries = list(platform_registry.all_entries())
    platform_registry._entries.clear()
    platform_registry.register(
        PlatformEntry(
            name="stubplat",
            label="Stub",
            adapter_factory=lambda cfg: object(),
            validate_config=lambda cfg: True,
            is_connected=lambda cfg: True,
        )
    )

    manager = _FakeChannelManager()
    app = _FakeCrew(manager=manager)
    ctx = SimpleNamespace(app=app, owner="owner-1")
    try:
        await _channel_disconnect(SimpleNamespace(platform="stubplat"), ctx)
        assert manager.get("stubplat", "owner-1") is None
    finally:
        platform_registry._entries.clear()
        for entry in old_entries:
            platform_registry.register(entry)
