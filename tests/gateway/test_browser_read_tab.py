"""POST /api/browser/{session_id}/read-tab 端点与 BrowserManager.read_tab_content 的契约测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crew.browser.driver import BrowserDriverError
from crew.gateway.auth import AccountContext
from crew.gateway.routers.browser import create_browser_router
from crew.tools.registry import Registry
from tests.gateway.conftest import make_browser_manager


@pytest.fixture(autouse=True)
def _instance_access_token(monkeypatch):
    monkeypatch.setattr(
        "crew.gateway.routers.browser.verify_gateway_instance_access_token",
        lambda token: token == "expected-token",
    )


def _registry() -> Registry:
    registry = Registry()
    registry.register(
        name="browser_use",
        toolset="browser",
        schema={"name": "browser_use", "parameters": {"type": "object", "properties": {}}},
        handler=lambda _args: "ok",
    )
    return registry


class _AccessControl:
    user_type = "internal"

    def __init__(self, enabled_tools: list[str]) -> None:
        self.enabled_tools = enabled_tools

    def resolve_for(self, _user_type: str) -> dict:
        return {
            "enabled_toolsets": ["browser"],
            "enabled_tools": list(self.enabled_tools),
        }


class _SessionStore:
    def session_belongs_to(self, session_id: str, owner: str) -> bool:
        return session_id == "session" and owner == "dev:dev"

    def get_agent_config(self, _session_id: str, *, owner_account_id: str) -> dict:
        assert owner_account_id == "dev:dev"
        return {"user_type": "internal"}


async def _read_should_not_be_called(*_args):
    raise AssertionError("校验失败前不应触达 manager.read_tab_content")


def _endpoint(enabled_tools: list[str], read=_read_should_not_be_called):
    crew = SimpleNamespace(
        browser_manager=SimpleNamespace(read_tab_content=read),
        config=SimpleNamespace(access_control=_AccessControl(enabled_tools)),
        registry=_registry(),
        session_store=_SessionStore(),
    )
    router = create_browser_router(crew)
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/api/browser/{session_id}/read-tab"
    )
    request = SimpleNamespace(
        state=SimpleNamespace(account=AccountContext("dev:dev")),
        headers={"authorization": "Bearer expected-token"},
    )
    return endpoint, request


def _body(response) -> dict:
    return json.loads(response.body)


async def test_read_tab_returns_content():
    async def fake_read(owner, session_id, tab_id):
        assert (owner, session_id, tab_id) == ("dev:dev", "session", "s0123-1")
        return {"title": "标题", "url": "https://example.com/p", "text": "正文"}

    endpoint, request = _endpoint(["browser_use"], fake_read)

    response = await endpoint(request, "session", {"tab_id": "s0123-1"})

    assert response.status_code == 200
    assert _body(response) == {
        "ok": True,
        "title": "标题",
        "url": "https://example.com/p",
        "text": "正文",
    }


async def test_read_tab_host_offline_is_explicit_error_not_500():
    async def fake_read(*_args):
        raise BrowserDriverError("桌面内置浏览器尚未连接；请打开 Crew 桌面应用并保持登录")

    endpoint, request = _endpoint(["browser_use"], fake_read)

    response = await endpoint(request, "session", {"tab_id": "s0123-1"})

    assert response.status_code == 409
    body = _body(response)
    assert body["ok"] is False
    assert "尚未连接" in body["error"]


async def test_read_tab_missing_tab_is_explicit_error():
    async def fake_read(*_args):
        raise BrowserDriverError("标签页不存在或已关闭")

    endpoint, request = _endpoint(["browser_use"], fake_read)

    response = await endpoint(request, "session", {"tab_id": "gone"})

    assert response.status_code == 409
    assert _body(response)["ok"] is False


async def test_read_tab_requires_tab_id():
    endpoint, request = _endpoint(["browser_use"])

    response = await endpoint(request, "session", {})

    assert response.status_code == 400
    assert _body(response)["ok"] is False


async def test_read_tab_requires_browser_use_capability():
    called = False

    async def fake_read(*_args):
        nonlocal called
        called = True
        return {"title": "", "url": "", "text": ""}

    endpoint, request = _endpoint([], fake_read)

    response = await endpoint(request, "session", {"tab_id": "s0123-1"})

    assert response.status_code == 403
    assert called is False


async def test_read_tab_requires_instance_token():
    endpoint, _request = _endpoint(["browser_use"])
    request = SimpleNamespace(
        state=SimpleNamespace(account=AccountContext("dev:dev")),
        headers={"authorization": "Bearer wrong-token"},
    )

    response = await endpoint(request, "session", {"tab_id": "s0123-1"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# BrowserManager.read_tab_content（真 manager + 假 driver，白盒播种内部图）
# ---------------------------------------------------------------------------


async def test_read_tab_content_reads_targeted_tab():
    manager = make_browser_manager({"title": "T", "url": "https://example.com/p", "text": "正文"})

    result = await manager.read_tab_content("dev:dev", "session", "s0123-1")

    assert result == {"title": "T", "url": "https://example.com/p", "text": "正文"}
    call = manager.driver.calls[0]
    assert call["command"] == "eval"
    assert call["target_id"] == "target-s0123-1"
    assert call["mutating"] is False


async def test_read_tab_content_truncates_text():
    manager = make_browser_manager({"title": "", "url": "", "text": "x" * 9000})

    default = await manager.read_tab_content("dev:dev", "session", "s0123-1")
    assert len(default["text"]) == 8000

    short = await manager.read_tab_content("dev:dev", "session", "s0123-1", max_chars=100)
    assert len(short["text"]) == 100


async def test_read_tab_content_falls_back_to_serialized():
    manager = make_browser_manager({"title": "T", "url": "u", "text": "正文"})

    async def broken_value(*args, **kwargs):
        return {"success": True, "data": {"value": None, "serialized": '{"title": "T", "url": "u", "text": "正文"}'}}

    manager.driver.execute_targeted = broken_value
    result = await manager.read_tab_content("dev:dev", "session", "s0123-1")
    assert result["text"] == "正文"


async def test_read_tab_content_rejects_unknown_tab():
    manager = make_browser_manager({}, tabs=())

    with pytest.raises(BrowserDriverError, match="标签页不存在或已关闭"):
        await manager.read_tab_content("dev:dev", "session", "s0123-1")


async def test_read_tab_content_rejects_without_browser_session():
    manager = make_browser_manager({})
    manager._owners.clear()  # noqa: SLF001 - 白盒：模拟该账号无浏览器会话

    with pytest.raises(BrowserDriverError, match="没有浏览器会话"):
        await manager.read_tab_content("dev:dev", "session", "s0123-1")


async def test_read_tab_content_rejects_human_mode():
    manager = make_browser_manager({}, mode="human")

    with pytest.raises(BrowserDriverError, match="人工接管"):
        await manager.read_tab_content("dev:dev", "session", "s0123-1")
    assert manager.driver.calls == []


async def test_read_tab_content_propagates_driver_error():
    manager = make_browser_manager(
        None,
        driver_error=BrowserDriverError("桌面内置浏览器尚未连接；请打开 Crew 桌面应用并保持登录"),
    )

    with pytest.raises(BrowserDriverError, match="尚未连接"):
        await manager.read_tab_content("dev:dev", "session", "s0123-1")
