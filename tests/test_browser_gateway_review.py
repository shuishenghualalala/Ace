"""Regression coverage for the Electron BrowserHost gateway boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import WebSocketDisconnect

from crew.browser.types import BrowserConfig
from crew.core.types import ToolCall, ToolResult, tool_arguments_for_history, tool_arguments_for_ui
from crew.gateway.auth import AccountContext
from crew.gateway.routers.browser import (
    _browser_access_allowed,
    _browser_instance_token_matches,
    _browser_tool_allowed,
    create_browser_router,
)
from crew.tools.registry import Registry
from crew.plugins.builtin import LoggingPlugin


@pytest.fixture(autouse=True)
def _instance_access_token(monkeypatch):
    monkeypatch.setattr(
        "crew.gateway.routers.browser.verify_gateway_instance_access_token",
        lambda token: token == "expected-token",
    )


def _registry_with_browser_tool() -> Registry:
    registry = Registry()
    for name in (
        "browser_use",
        "browser_snapshot",
        "browser_navigate",
        "browser_back",
        "browser_tabs",
        "browser_takeover",
        "browser_console",
    ):
        registry.register(
            name=name,
            toolset="browser",
            schema={"name": name, "parameters": {"type": "object", "properties": {}}},
            handler=lambda _args: "ok",
        )
    registry.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "parameters": {"type": "object", "properties": {}}},
        handler=lambda _args: "ok",
    )
    return registry


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"enabled_toolsets": ["browser"]}, True),
        ({"enabled_toolsets": []}, False),
        ({"enabled_toolsets": ["browser"], "disabled_toolsets": ["browser"]}, False),
        ({"enabled_toolsets": ["browser"], "enabled_tools": ["file_read"]}, False),
        ({"enabled_toolsets": ["browser"], "enabled_tools": ["browser_snapshot"]}, True),
        ({"enabled_toolsets": ["browser"], "disabled_tools": ["browser_snapshot"]}, True),
        (
            {
                "enabled_toolsets": ["browser"],
                "disabled_tools": [
                    "browser_use",
                    "browser_snapshot",
                    "browser_navigate",
                    "browser_back",
                    "browser_tabs",
                    "browser_takeover",
                    "browser_console",
                ],
            },
            False,
        ),
        ({"enabled_toolsets": ["*"], "disabled_tools": ["*"]}, False),
    ],
)
def test_browser_gateway_uses_registry_access_filter(policy, expected):
    assert _browser_access_allowed(_registry_with_browser_tool(), policy) is expected


def test_browser_gateway_checks_the_exact_control_capability():
    registry = _registry_with_browser_tool()
    policy = {
        "enabled_toolsets": ["browser"],
        "enabled_tools": ["browser_snapshot", "browser_takeover"],
    }

    assert _browser_tool_allowed(registry, policy, "browser_takeover") is True
    assert _browser_tool_allowed(registry, policy, "browser_snapshot") is True
    assert _browser_tool_allowed(registry, policy, "browser_use") is False
    assert _browser_tool_allowed(registry, policy, "browser_navigate") is False
    assert _browser_tool_allowed(registry, policy, "browser_back") is False


class _ManualAccessControl:
    user_type = "internal"

    def __init__(self, enabled_tools: list[str]) -> None:
        self.enabled_tools = enabled_tools

    def resolve_for(self, _user_type: str) -> dict:
        return {
            "enabled_toolsets": ["browser"],
            "enabled_tools": list(self.enabled_tools),
        }


class _ManualSessionStore:
    def session_belongs_to(self, session_id: str, owner: str) -> bool:
        return session_id == "session" and owner == "dev:dev"

    def get_agent_config(self, _session_id: str, *, owner_account_id: str) -> dict:
        assert owner_account_id == "dev:dev"
        return {"user_type": "internal"}

    def get_workspace_id(self, _session_id: str, *, owner_account_id: str) -> str:
        assert owner_account_id == "dev:dev"
        return "workspace"


class _ManualManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def open_for_user(self, *args, **kwargs) -> dict:
        self.calls.append(("open_for_user", args, kwargs))
        return {"mode": "human"}

    async def user_control(self, *args, **kwargs) -> str:
        self.calls.append(("user_control", args, kwargs))
        return "controlled"

    async def human_command(self, *args, **kwargs) -> dict:
        self.calls.append(("human_command", args, kwargs))
        return {"mode": "human"}

    def state(self, *_args) -> dict:
        return {"mode": "human"}


def _manual_browser_endpoint(enabled_tools: list[str], manager: _ManualManager, path: str):
    crew = SimpleNamespace(
        browser_manager=manager,
        config=SimpleNamespace(
            gateway_dev_mode=True,
            gateway_dev_account="dev:dev",
            access_control=_ManualAccessControl(enabled_tools),
        ),
        registry=_registry_with_browser_tool(),
        session_store=_ManualSessionStore(),
    )
    router = create_browser_router(crew)
    endpoint = next(route.endpoint for route in router.routes if route.path == path)
    request = SimpleNamespace(
        state=SimpleNamespace(account=AccountContext("dev:dev")),
        headers={"authorization": "Bearer expected-token"},
    )
    return endpoint, request, crew


@pytest.mark.parametrize(
    "enabled_tools",
    [
        [],
        ["browser_tabs"],
        ["browser_tabs", "browser_takeover", "browser_navigate"],
    ],
)
async def test_manual_open_requires_the_browser_use_capability(enabled_tools):
    # 面板动作统一由单一 browser_use 承载；只放行部分旧逻辑工具名不再够用。
    manager = _ManualManager()
    endpoint, request, _crew = _manual_browser_endpoint(
        enabled_tools,
        manager,
        "/api/browser/{session_id}/control",
    )

    response = await endpoint(request, "session", {"action": "open", "value": ""})

    assert response.status_code == 403
    assert manager.calls == []


@pytest.mark.parametrize(
    ("action", "value"),
    [
        ("open", ""),
        ("new_tab", "https://example.com"),
    ],
)
async def test_manual_open_accepts_with_the_browser_use_capability(action, value):
    manager = _ManualManager()
    endpoint, request, _crew = _manual_browser_endpoint(
        ["browser_use"],
        manager,
        "/api/browser/{session_id}/control",
    )

    response = await endpoint(request, "session", {"action": action, "value": value})

    assert response.status_code == 200
    assert manager.calls == [
        (
            "open_for_user",
            ("dev:dev", "session"),
            {"url": value, "new_tab": action == "new_tab"},
        )
    ]


async def test_gateway_control_returns_to_ai_only_through_trusted_user_entrypoint():
    manager = _ManualManager()
    endpoint, request, _crew = _manual_browser_endpoint(
        ["browser_use"],
        manager,
        "/api/browser/{session_id}/control",
    )

    response = await endpoint(request, "session", {"action": "return"})

    assert response.status_code == 200
    assert manager.calls == [("user_control", ("dev:dev", "session", "return"), {})]


async def test_artifact_preview_requires_the_browser_use_capability(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<title>Preview</title>", encoding="utf-8")

    manager = _ManualManager()
    endpoint, request, crew = _manual_browser_endpoint(
        ["browser_tabs", "browser_takeover", "browser_navigate"],
        manager,
        "/api/browser/{session_id}/artifact",
    )
    crew.workspace_store = SimpleNamespace(
        get=lambda *_args, **_kwargs: {"root_path": str(tmp_path)}
    )
    response = await endpoint(request, "session", {"path": page.name})
    assert response.status_code == 403
    assert manager.calls == []

    manager = _ManualManager()
    endpoint, request, crew = _manual_browser_endpoint(
        ["browser_use"],
        manager,
        "/api/browser/{session_id}/artifact",
    )
    crew.workspace_store = SimpleNamespace(
        get=lambda *_args, **_kwargs: {"root_path": str(tmp_path)}
    )
    response = await endpoint(request, "session", {"path": page.name, "new_tab": True})

    assert response.status_code == 200
    assert manager.calls == [
        (
            "open_for_user",
            ("dev:dev", "session"),
            {
                "artifact_path": str(page.resolve()),
                "artifact_root": str(tmp_path.resolve()),
                "new_tab": True,
            },
        )
    ]


def test_browser_urls_are_redacted_before_ui_or_history_display():
    raw = (
        "https://alice:password@example.com/search?"
        "keywords=keyboards&access_token=top-secret&note=sk-ant-abcdefghijklmnop#token=fragment"
    )

    navigate = tool_arguments_for_ui("browser_navigate", {"url": raw})
    tabs = tool_arguments_for_ui("browser_tabs", {"action": "new", "url": raw})
    history = tool_arguments_for_history("browser_navigate", {"url": raw})
    use_navigate = tool_arguments_for_ui("browser_use", {"action": "navigate", "url": raw})
    use_history = tool_arguments_for_history("browser_use", {"action": "tab_new", "url": raw})

    for value in (navigate["url"], tabs["url"], history["url"], use_navigate["url"], use_history["url"]):
        parsed = urlsplit(value)
        query = parse_qs(parsed.query)
        assert parsed.netloc == "example.com"
        assert parsed.fragment == ""
        assert query["keywords"] == ["keyboards"]
        assert query["access_token"] != ["top-secret"]
        assert "top-secret" not in value
        assert "sk-ant-abcdefghijklmnop" not in value

    typed = tool_arguments_for_history(
        "browser_type",
        {"ref": "p1:e2", "text": "password-value", "password": "also-secret"},
    )
    dialog = tool_arguments_for_history(
        "browser_dialog",
        {"action": "accept", "text": "one-time-code", "value": "secret"},
    )
    use_typed = tool_arguments_for_history(
        "browser_use",
        {"action": "type", "ref": "p1:e2", "text": "password-value"},
    )
    use_dialog = tool_arguments_for_history(
        "browser_use",
        {"action": "dialog_accept", "text": "one-time-code"},
    )
    assert typed == {"ref": "p1:e2"}
    assert dialog == {"action": "accept"}
    assert use_typed == {"action": "type", "ref": "p1:e2"}
    assert use_dialog == {"action": "dialog_accept"}


def test_fill_form_and_select_values_never_enter_ui_or_history_arguments():
    fields = [
        {"type": "textbox", "ref": "p7:e1", "value": "employee-secret"},
        {
            "type": "combobox",
            "ref": "p7:e2",
            "value": "Secret option label",
            "select_by": "label",
        },
        {"type": "checkbox", "ref": "p7:e3", "value": True},
        {"type": "slider", "ref": "p7:e4", "value": "73"},
    ]
    calls = (
        tool_arguments_for_ui(
            "browser_use",
            {"action": "fill_form", "fields": fields},
        ),
        tool_arguments_for_history(
            "browser_use",
            {"action": "fill_form", "fields": fields},
        ),
        tool_arguments_for_ui("browser_fill_form", {"fields": fields}),
        tool_arguments_for_history("browser_fill_form", {"fields": fields}),
    )
    for projected in calls:
        encoded = json.dumps(projected, ensure_ascii=False)
        assert projected["field_count"] == 4
        assert projected["field_types"] == {
            "textbox": 1,
            "combobox": 1,
            "checkbox": 1,
            "slider": 1,
        }
        assert projected["fields"][1] == {
            "index": 1,
            "type": "combobox",
            "ref": "p7:e2",
            "select_by": "label",
        }
        assert "employee-secret" not in encoded
        assert "Secret option label" not in encoded
        assert '"73"' not in encoded
        assert '"value"' not in encoded

    select_args = {"ref": "p7:e2", "values": ["private-a", "private-b"]}
    selections = (
        tool_arguments_for_ui(
            "browser_use",
            {"action": "select", **select_args},
        ),
        tool_arguments_for_history(
            "browser_use",
            {"action": "select", **select_args},
        ),
        tool_arguments_for_ui("browser_select", select_args),
        tool_arguments_for_history("browser_select", select_args),
    )
    for projected in selections:
        assert projected["ref"] == "p7:e2"
        assert projected["value_count"] == 2
        assert "private-" not in json.dumps(projected)


async def test_logging_plugin_never_logs_browser_arguments_or_results(monkeypatch):
    records: list[str] = []

    def capture(message: str, *args) -> None:
        records.append(message % args)

    monkeypatch.setattr("crew.plugins.builtin.log.info", capture)
    plugin = LoggingPlugin()
    call = ToolCall(
        "call-1",
        "browser_type",
        {"ref": "p1:e1", "text": "employee-password-123"},
    )
    result = ToolResult(
        "call-1",
        "browser_type",
        "page contains employee-password-123",
    )

    await plugin.pre_tool_call(call)
    await plugin.post_tool_call(call, result)

    output = "\n".join(records)
    assert "employee-password-123" not in output
    assert "browser_args_redacted" in output
    assert "browser_result_redacted" in output


async def test_browser_websocket_serializes_sends_and_survives_command_error():
    registry = _registry_with_browser_tool()

    class FakeAccessControl:
        user_type = "internal"

        def resolve_for(self, _user_type: str) -> dict:
            return {"enabled_toolsets": ["browser"]}

    config = SimpleNamespace(
        gateway_dev_mode=True,
        gateway_dev_account="dev:dev",
        access_control=FakeAccessControl(),
    )

    class FakeSessionStore:
        def session_belongs_to(self, session_id: str, owner: str) -> bool:
            return session_id == "session" and owner == "dev:dev"

        def get_agent_config(self, _session_id: str, *, owner_account_id: str) -> dict:
            assert owner_account_id == "dev:dev"
            return {"user_type": "internal"}

    never = asyncio.Event()

    class FakeManager:
        async def subscribe(self, _owner: str, _session_id: str):
            yield {"type": "state"}
            yield {"type": "debug", "channel": "console", "record": {"text": "visible"}}
            await never.wait()

    manager = FakeManager()
    crew = SimpleNamespace(
        browser_manager=manager,
        config=config,
        registry=registry,
        session_store=FakeSessionStore(),
    )
    router = create_browser_router(crew)
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser/{session_id}")

    class FakeSocket:
        def __init__(self) -> None:
            self.headers = {"authorization": "Bearer expected-token"}
            self.client = SimpleNamespace(host="testclient")
            self.messages = [
                {"type": "input", "event": {"kind": "mouse"}},
                ["malformed-message"],
                {"type": "ping"},
            ]
            self.sent: list[dict] = []
            self._active_sends = 0
            self.max_active_sends = 0

        async def accept(self) -> None:
            return None

        async def close(self, code: int) -> None:
            raise AssertionError(f"unexpected websocket close {code}")

        async def receive_json(self):
            if self.messages:
                return self.messages.pop(0)
            raise WebSocketDisconnect()

        async def send_json(self, event: dict) -> None:
            self._active_sends += 1
            self.max_active_sends = max(self.max_active_sends, self._active_sends)
            try:
                await asyncio.sleep(0.001)
                self.sent.append(event)
            finally:
                self._active_sends -= 1

    socket = FakeSocket()
    await endpoint(socket, "session")

    assert socket.max_active_sends == 1
    assert sum(event.get("type") == "command_error" for event in socket.sent) == 2
    assert any(event.get("type") == "pong" for event in socket.sent)
    assert any(event.get("type") == "state" for event in socket.sent)
    assert any(event.get("type") == "debug" for event in socket.sent)


async def test_browser_websocket_drops_debug_when_browser_use_is_disabled():
    registry = _registry_with_browser_tool()

    class FakeAccessControl:
        user_type = "internal"

        def resolve_for(self, _user_type: str) -> dict:
            return {
                "enabled_toolsets": ["browser"],
                "disabled_tools": ["browser_use"],
            }

    config = SimpleNamespace(
        gateway_dev_mode=True,
        gateway_dev_account="dev:dev",
        access_control=FakeAccessControl(),
    )

    class FakeSessionStore:
        def session_belongs_to(self, session_id: str, owner: str) -> bool:
            return session_id == "session" and owner == "dev:dev"

        def get_agent_config(self, _session_id: str, *, owner_account_id: str) -> dict:
            return {"user_type": "internal"}

    never = asyncio.Event()

    class FakeManager:
        async def subscribe(self, _owner: str, _session_id: str):
            yield {"type": "debug", "channel": "console", "record": {"text": "secret"}}
            yield {"type": "frame", "data": "retired-pixels"}
            yield {"type": "state", "state": {"mode": "ai"}}
            await never.wait()

    crew = SimpleNamespace(
        browser_manager=FakeManager(),
        config=config,
        registry=registry,
        session_store=FakeSessionStore(),
    )
    router = create_browser_router(crew)
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser/{session_id}")

    class FakeSocket:
        headers = {"authorization": "Bearer expected-token"}
        client = SimpleNamespace(host="testclient")

        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.received = False

        async def accept(self) -> None:
            return None

        async def close(self, code: int) -> None:
            raise AssertionError(f"unexpected websocket close {code}")

        async def receive_json(self):
            if not self.received:
                self.received = True
                await asyncio.sleep(0.02)
            raise WebSocketDisconnect()

        async def send_json(self, event: dict) -> None:
            self.sent.append(event)

    socket = FakeSocket()
    await endpoint(socket, "session")

    assert any(event.get("type") == "state" for event in socket.sent)
    assert not any(event.get("type") == "frame" for event in socket.sent)
    assert not any(event.get("type") == "debug" for event in socket.sent)


class _HostAccessControl:
    user_type = "internal"

    def __init__(self, *, browser_use: bool = True) -> None:
        self.browser_use = browser_use

    def resolve_for(self, _user_type: str) -> dict:
        policy: dict[str, object] = {"enabled_toolsets": ["browser"]}
        if not self.browser_use:
            policy["disabled_tools"] = ["browser_use"]
        return policy


class _HostSessionStore:
    def session_belongs_to(self, _session_id: str, _owner: str) -> bool:
        return True

    def get_agent_config(self, _session_id: str, *, owner_account_id: str) -> dict:
        assert owner_account_id in {"local", "dev:dev"}
        return {"user_type": "internal"}


class _HostSocket:
    client = SimpleNamespace(host="testclient")

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.closed: list[int] = []

    async def close(self, code: int) -> None:
        self.closed.append(code)


def _host_crew(manager, *, dev: bool = False, browser_use: bool = True):
    return SimpleNamespace(
        browser_manager=manager,
        config=SimpleNamespace(
            gateway_dev_mode=dev,
            gateway_dev_account="dev:dev",
            access_control=_HostAccessControl(browser_use=browser_use),
            browser=BrowserConfig(command_timeout_seconds=3),
        ),
        registry=_registry_with_browser_tool(),
        session_store=_HostSessionStore(),
    )


async def test_browser_host_rejects_wrong_local_owner_token(monkeypatch):
    served = False

    async def serve(*_args, **_kwargs) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(SimpleNamespace()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    socket = _HostSocket(
        {
            "authorization": "Bearer wrong-token",
        }
    )

    await endpoint(socket)

    assert socket.closed == [4401]
    assert served is False


async def test_browser_state_socket_rejects_spoofed_loopback_identity_without_token(monkeypatch):
    subscribed = False

    class Manager:
        async def subscribe(self, *_args):
            nonlocal subscribed
            subscribed = True
            if False:
                yield {}

    router = create_browser_router(_host_crew(Manager()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser/{session_id}")
    socket = _HostSocket(
        {
            "authorization": "Bearer wrong-token",
        }
    )

    await endpoint(socket, "session")

    assert socket.closed == [4401]
    assert subscribed is False


async def test_browser_host_rejects_non_loopback_even_with_valid_token(monkeypatch):
    served = False

    async def serve(*_args, **_kwargs) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(SimpleNamespace()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    socket = _HostSocket(
        {
            "authorization": "Bearer expected-token",
        }
    )
    socket.client = SimpleNamespace(host="203.0.113.10")

    await endpoint(socket)

    assert socket.closed == [4403]
    assert served is False


async def test_browser_host_registration_resets_epoch_and_routes_exact_debug_event(monkeypatch):
    actions: list[tuple[str, object]] = []

    class Manager:
        async def reset_host_registration(self, owner: str) -> None:
            actions.append(("reset", owner))

        def session_for_target(self, owner: str, target: str) -> str | None:
            return "session" if (owner, target) == ("local", "target-1") else None

        async def publish_host_debug(self, *args) -> bool:
            actions.append(("debug", args))
            return True

    async def request(runtime_key: str, method: str, params: dict, **kwargs):
        actions.append(("rpc", (runtime_key, method, params, kwargs)))
        return {"closed": True}

    async def serve(_socket, runtime_key: str, **callbacks) -> None:
        actions.append(("serve", runtime_key))
        await callbacks["on_registered"]()
        await callbacks["on_event"](
            {
                "type": "debug",
                "channel": "console",
                "targetId": "target-1",
                "record": {"text": "visible"},
            }
        )

    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(Manager()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    socket = _HostSocket(
        {
            "authorization": "Bearer expected-token",
        }
    )

    await endpoint(socket)

    assert socket.closed == []
    assert [action[0] for action in actions] == ["serve", "rpc", "reset", "debug"]
    rpc = actions[1][1]
    assert rpc[1] == "close_owner" and rpc[3]["_allow_unready"] is True


async def test_browser_host_routes_popup_download_by_logical_session_hash(monkeypatch):
    published: list[tuple[object, ...]] = []

    class Manager:
        async def reset_host_registration(self, _owner: str) -> None:
            return None

        def session_for_hash(self, owner: str, value: str) -> str | None:
            return "session" if (owner, value) == ("staff:42", "a" * 32) else None

        def session_for_target(self, _owner: str, _target: str) -> None:
            # A newly opened popup legitimately has not reached Manager's tab
            # cache when its first attachment starts.
            return None

        async def publish_host_download(self, *args) -> bool:
            published.append(args)
            return True

    async def request(*_args, **_kwargs):
        return {"closed": True}

    event = {
        "type": "download",
        "downloadId": "download-1",
        "targetId": "target-popup",
        "sessionHash": "a" * 32,
        "path": "/tmp/downloads/browser/report.csv",
        "name": "report.csv",
        "suggestedFilename": "report.csv",
        "url": "https://example.com/report.csv",
        "state": "completed",
        "receivedBytes": 12,
        "totalBytes": 12,
        "createdAt": 1,
        "completedAt": 2,
        "error": "",
    }

    async def serve(_socket, _runtime_key: str, **callbacks) -> None:
        await callbacks["on_registered"]()
        await callbacks["on_event"](event)

    monkeypatch.setattr(
        "crew.gateway.routers.browser.read_session_access_token",
        lambda _owner: "expected-token",
    )
    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(Manager()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    socket = _HostSocket(
        {
            "x-MobileWork-staff-code": "staff",
            "x-MobileWork-staff-uid": "42",
            "authorization": "Bearer expected-token",
        }
    )

    await endpoint(socket)

    assert published == [("staff:42", "session", event)]


async def test_browser_host_debug_event_rechecks_browser_use_policy(monkeypatch):
    published = False

    class Manager:
        async def reset_host_registration(self, _owner: str) -> None:
            return None

        def session_for_target(self, _owner: str, _target: str) -> str:
            return "session"

        async def publish_host_debug(self, *_args) -> bool:
            nonlocal published
            published = True
            return True

    async def request(*_args, **_kwargs):
        return {"closed": True}

    async def serve(_socket, _runtime_key: str, **callbacks) -> None:
        await callbacks["on_registered"]()
        await callbacks["on_event"](
            {
                "type": "debug",
                "channel": "network",
                "targetId": "target-1",
                "record": {"url": "https://example.com"},
            }
        )

    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(Manager(), browser_use=False))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    await endpoint(
        _HostSocket(
            {
                "authorization": "Bearer expected-token",
            }
        )
    )

    assert published is False


async def test_browser_host_requires_instance_token_in_dev_mode(monkeypatch):
    served = False

    async def serve(*_args, **_kwargs) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(
        "crew.gateway.routers.browser.electron_browser_bridge.serve",
        serve,
    )
    router = create_browser_router(_host_crew(SimpleNamespace(), dev=True))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/browser-host")
    socket = _HostSocket({})

    await endpoint(socket)

    assert served is False
    assert socket.closed == [4401]


async def test_browser_doctor_uses_authenticated_account_runtime_key(monkeypatch):
    seen: list[str] = []

    def doctor(_config, runtime_key: str) -> dict:
        seen.append(runtime_key)
        return {"ok": True, "runtime_key": runtime_key}

    monkeypatch.setattr("crew.gateway.routers.browser.runtime_doctor", doctor)
    router = create_browser_router(_host_crew(SimpleNamespace()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/browser/doctor")
    request = SimpleNamespace(
        state=SimpleNamespace(account=AccountContext("owner:42")),
        headers={"authorization": "Bearer expected-token"},
    )

    response = await endpoint(request)
    payload = json.loads(response.body)

    assert seen == [f"crew_{hashlib.sha256(b'owner:42').hexdigest()[:12]}"]
    assert payload["ok"] is True


async def test_browser_http_endpoints_reject_identity_headers_without_matching_token(monkeypatch):
    called = False

    def doctor(*_args) -> dict:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr("crew.gateway.routers.browser.runtime_doctor", doctor)
    router = create_browser_router(_host_crew(SimpleNamespace()))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/browser/doctor")
    request = SimpleNamespace(
        state=SimpleNamespace(account=AccountContext("owner:42")),
        headers={"authorization": "Bearer attacker-token"},
    )

    response = await endpoint(request)

    assert response.status_code == 401
    assert called is False


def test_browser_instance_token_helper_requires_the_derived_token():
    assert _browser_instance_token_matches(
        {"authorization": "Bearer expected-token"}
    )
    assert not _browser_instance_token_matches({})
