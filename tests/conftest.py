"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """Use the historical test owner without sending identity headers."""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


@pytest.fixture(autouse=True)
def _close_live_crew_apps():
    """兜底关闭本用例构建、但未走 ASGI lifespan 的 CrewApp。

    直接 ASGITransport 调 app 的用例不会触发 lifespan，crew.shutdown() 不会
    执行；每个 CrewApp 持有多个 SQLite/WAL 连接，不关闭会跨用例累积 fd 直至
    耗尽（Errno 24）。正常 shutdown 过的实例已自行从注册表移除。
    """
    yield

    from crew.app import _LIVE_APPS

    while _LIVE_APPS:
        app = _LIVE_APPS.pop()
        if app._shutdown_complete:  # noqa: SLF001
            continue
        try:
            asyncio.run(app.shutdown())
        except Exception:  # noqa: BLE001 - 跨 loop 的原语可能拒绝，退化为同步关 Store
            for store in (
                app.session_store,
                app.workspace_store,
                app.memory,
                app.plugin_prefs,
                app.summary_store,
                app.tasks,
            ):
                close = getattr(store, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
