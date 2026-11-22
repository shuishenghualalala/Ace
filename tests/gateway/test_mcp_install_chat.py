from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

import crew.app as app_module
from crew.core.envelope import Envelope
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, ToolCall
from crew.gateway.server import create_app
from crew.state.config import Config
from crew.tools.mcp_client import _ServerWorker
from crew.tools.tool_search import function_name

TOOL = "mcp_install"
URL = "https://echo-mcp.invalid/mcp"
ARGS = {"name": "local-fixture", "url": URL, "transport": "http"}

class ModelProbe(FakeProvider):
    def __init__(self):
        super().__init__()
        self.offered = []
        self.script = {}
        self.tool_results = []

    async def chat(self, messages, tools=None):
        self.offered.append({function_name(schema) for schema in (tools or [])})
        self.tool_results = [m for m in messages if m.role == "tool"]
        marker = next((m.content for m in reversed(messages) if m.role == "user" and m.content in self.script), "")
        call = self.script.pop(marker, None)
        if call is not None:
            return ChatResponse(tool_calls=[call], finish_reason="tool_calls")
        return ChatResponse(text="fixture model observed its tools", finish_reason="stop")

@pytest.fixture
async def gateway(tmp_path, monkeypatch, request):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("mcp_servers: {}\n", "utf-8")
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "ace-home"))
    monkeypatch.setenv("CREW_OFFLINE", "1")
    monkeypatch.delenv("ACE_ALLOW_HOST_MCP_STDIO", raising=False)
    provider = ModelProbe()
    monkeypatch.setattr(app_module, "build_provider", lambda _cfg: provider)
    monkeypatch.setattr(app_module, "build_provider_for_profile", lambda *args, **kwargs: provider)
    io = []

    class EchoSdkSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(
                name="echo", description="return one test value",
                input_schema={"type": "object", "properties": {}},
            )])

        async def send_ping(self):
            return None

    async def fake_connection(worker, _stack):
        assert worker.cfg.get("url") == URL
        assert not worker.cfg.get("command"), "no subprocess may be staged by the fixture"
        io.append(worker.name)
        return EchoSdkSession()

    monkeypatch.setattr(_ServerWorker, "_open", fake_connection)
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        config_path=str(config_file),
        gateway_admin_accounts=["local"],
        gateway_dev_mode=False,
        security_enabled=getattr(request, "param", "") == "managed",
        auth_mode="email" if getattr(request, "param", "") == "email" else "local",
        title_auto=False,
        plugins_enabled=[],
    )
    crew = app_module.build_app(config=config, enable_team=False)
    app = create_app(crew)
    try:
        yield SimpleNamespace(crew=crew, app=app, provider=provider, config_file=config_file, io=io)
    finally:
        await crew.shutdown()

@asynccontextmanager
async def open_ws(app, headers=()):
    inbound = asyncio.Queue()
    outbound = asyncio.Queue()
    scope = {
        "type": "websocket", "scheme": "ws", "path": "/ws", "raw_path": b"/ws",
        "query_string": b"", "root_path": "", "headers": list(headers), "subprotocols": [],
        "http_version": "1.1", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 16000), "state": {},
    }
    task = asyncio.create_task(app(scope, inbound.get, outbound.put))
    await inbound.put({"type": "websocket.connect"})
    first = await asyncio.wait_for(outbound.get(), 5)
    assert first["type"] == "websocket.accept", first
    async def send(message):
        await inbound.put({"type": "websocket.receive", "text": json.dumps(message)})
    async def receive():
        event = await asyncio.wait_for(outbound.get(), 7)
        assert event["type"] == "websocket.send", event
        return json.loads(event.get("text") or event["bytes"])
    try:
        yield send, receive
    finally:
        await inbound.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 7)

async def finish_foreground(app, *, plan: bool = False):
    async with open_ws(app) as (send, receive):
        await send({
            "session_id": "mcp-plan-fixture" if plan else "mcp-foreground-fixture",
            "request_id": "mcp-plan-turn" if plan else "mcp-foreground-turn",
            "query": "Please inspect the available installer for test only.",
            "mode": "agent", "workspace_id": "default", "plan_active": plan,
        })
        frames = []
        for _ in range(100):
            event = await receive()
            frames.append(event)
            if event.get("is_final") is True:
                break
        return frames

async def test_built_registry_offers_install_and_contextless_direct_call_does_not_write(gateway):
    original = gateway.config_file.read_bytes()
    assert {
        TOOL, "mcp_status", "mcp_remove", "mcp_reload", "mcp_update",
    } <= set(gateway.crew.registry.names()), "the built registry must expose MCP management"
    result = await gateway.crew.registry.execute(ToolCall("direct", TOOL, ARGS))
    assert result.is_error
    assert result.code not in {"tool_not_found", "invalid_arguments"}
    assert not gateway.crew.config.mcp_servers
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []

async def test_gateway_local_admin_can_discover_chat_install_in_foreground_tools(gateway):
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 16000))
    async with httpx.AsyncClient(transport=transport, base_url="http://local-gateway") as client:
        config = await client.get("/api/config")
    assert config.status_code == 200
    assert config.json()["is_gateway_admin"] is True
    frames = await finish_foreground(gateway.app)
    assert frames[-1]["kind"] == "final", frames[-1]
    assert gateway.crew.active_owner.current().owner_account_id == "local"
    assert any(TOOL in names for names in gateway.provider.offered), gateway.provider.offered
    assert gateway.io == []


async def begin_install(gateway, send, label, *, arguments=None, session_id="mcp-install-fixture", plan=False):
    query = f"fixture requests chat MCP install {label}"
    gateway.provider.script[query] = ToolCall(f"install-{label}", TOOL, arguments if arguments is not None else ARGS)
    await send({
        "session_id": session_id, "request_id": label, "query": query, "mode": "agent",
        "workspace_id": "default", "plan_active": plan,
    })


def is_permission_card(event):
    body = event.get("body", {})
    return event.get("kind") == "followup_question" and any(
        item.get("id") == "perm" for item in body.get("questions", []) if isinstance(item, dict)
    )


async def read_until(receive, predicate):
    records = []
    for _ in range(128):
        item = await receive()
        records.append(item)
        if predicate(item):
            return item, records
    raise AssertionError(records)


async def reply_permission(send, session_id, request_id, card, answer="allow_once"):
    await send({
        "action": "followup_answer", "session_id": session_id, "request_id": request_id,
        "question_id": card["body"]["question_id"],
        "submission_id": f"answer-{request_id}",
        "answers": [{"id": "perm", "answers": [answer]}],
    })


def config_servers(gateway):
    return yaml.safe_load(gateway.config_file.read_text("utf-8")).get("mcp_servers", {})


def payload_for(gateway, call_id):
    matches = [m for m in gateway.provider.tool_results if m.tool_call_id == call_id]
    assert len(matches) == 1, matches
    return json.loads(matches[0].content)


async def test_gateway_admin_removes_installed_server_after_one_approval(gateway):
    session_id = "mcp-remove-fixture"
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "seed", session_id=session_id)
        card, frames = await read_until(
            receive, lambda row: is_permission_card(row) or row.get("is_final"),
        )
        assert is_permission_card(card), frames
        await reply_permission(send, session_id, "seed", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames
        assert config_servers(gateway)[ARGS["name"]]["url"] == URL
        assert f"{ARGS['name']}__echo" in gateway.crew.registry.names()

        query = "fixture requests chat MCP remove"
        gateway.provider.script[query] = ToolCall("remove-1", "mcp_remove", {"name": ARGS["name"]})
        await send({
            "session_id": session_id, "request_id": "remove", "query": query, "mode": "agent",
            "workspace_id": "default", "plan_active": False,
        })
        card, frames = await read_until(
            receive, lambda row: is_permission_card(row) or row.get("is_final"),
        )
        assert is_permission_card(card), frames
        question = next(item for item in card["body"]["questions"] if item["id"] == "perm")
        assert {item["value"] for item in question["options"]} == {"allow_once", "deny"}
        assert ARGS["name"] in config_servers(gateway)
        await reply_permission(send, session_id, "remove", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames
        removed = payload_for(gateway, "remove-1")
        assert removed["removed"] is True
        assert removed["disconnected"] is True
        assert ARGS["name"] not in config_servers(gateway)
        assert f"{ARGS['name']}__echo" not in gateway.crew.registry.names()


async def test_gateway_admin_confirms_install_once_and_repeat_reuses_config(gateway):
    original = gateway.config_file.read_bytes()
    session_id = "mcp-install-fixture"
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "first", session_id=session_id)
        card, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert is_permission_card(card), frames
        question = next(item for item in card["body"]["questions"] if item["id"] == "perm")
        assert {item["value"] for item in question["options"]} == {"allow_once", "deny"}
        assert gateway.config_file.read_bytes() == original
        assert gateway.io == []
        await reply_permission(send, session_id, "first", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames
        created = payload_for(gateway, "install-first")
        assert created["name"] == ARGS["name"] and created["transport"] == "http"
        assert created["saved"] is True and created["connected"] is True and created["tool_count"] == 1
        assert config_servers(gateway)[ARGS["name"]]["url"] == URL
        assert f"{ARGS['name']}__echo" in gateway.crew.registry.names()
        frozen = gateway.config_file.read_bytes()

        await begin_install(gateway, send, "same-again", session_id=session_id)
        event, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        if is_permission_card(event):
            await reply_permission(send, session_id, "same-again", event)
            event, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert event["kind"] == "final", frames
        repeated = payload_for(gateway, "install-same-again")
        assert repeated["already_configured"] is True
        assert repeated["saved"] is True and repeated["connected"] is True

    assert gateway.config_file.read_bytes() == frozen
    assert gateway.io == [ARGS["name"]]


async def test_gateway_install_can_save_without_auto_connect(gateway):
    session_id = "mcp-install-manual"
    arguments = {**ARGS, "auto_connect": False}
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "manual", arguments=arguments, session_id=session_id)
        card, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert is_permission_card(card), frames
        await reply_permission(send, session_id, "manual", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames
        created = payload_for(gateway, "install-manual")
        assert created["saved"] is True
        assert created["connected"] is False
        assert created["status"] == "saved_not_connected"
        assert config_servers(gateway)[ARGS["name"]]["auto_connect"] is False
        assert gateway.io == []


async def test_gateway_cancel_while_awaiting_approval_leaves_config_unchanged(gateway):
    original = gateway.config_file.read_bytes()
    session_id = "mcp-install-fixture"
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "cancelled", session_id=session_id)
        card, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert is_permission_card(card), frames
        await send({"action": "stop", "session_id": session_id, "request_id": "cancelled"})
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final.get("status") == "stopped" or final.get("body", {}).get("outcome", {}).get("status") == "stopped", frames
        await reply_permission(send, session_id, "cancelled", card)
        receipt, frames = await read_until(
            receive, lambda row: row.get("kind") == "interaction_receipt"
            and row.get("body", {}).get("type") == "followup"
            and row.get("body", {}).get("submission_id") == "answer-cancelled",
        )
        assert receipt["body"]["status"] == "expired", frames
    assert gateway.config_file.read_bytes() == original
    assert not config_servers(gateway)
    assert gateway.io == []


async def test_gateway_admin_updates_installed_server_after_one_approval(gateway):
    session_id = "mcp-update-fixture"
    updated_args = {**ARGS, "headers": {"X-Ace-Fixture": "updated"}}
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "update-seed", session_id=session_id)
        card, frames = await read_until(
            receive, lambda row: is_permission_card(row) or row.get("is_final"),
        )
        assert is_permission_card(card), frames
        await reply_permission(send, session_id, "update-seed", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames

        query = "fixture requests chat MCP update"
        gateway.provider.script[query] = ToolCall("update-1", "mcp_update", updated_args)
        await send({
            "session_id": session_id, "request_id": "update", "query": query, "mode": "agent",
            "workspace_id": "default", "plan_active": False,
        })
        card, frames = await read_until(
            receive, lambda row: is_permission_card(row) or row.get("is_final"),
        )
        assert is_permission_card(card), frames
        assert "headers" not in config_servers(gateway)[ARGS["name"]]
        await reply_permission(send, session_id, "update", card)
        final, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert final["kind"] == "final", frames
        payload = payload_for(gateway, "update-1")
        assert payload["changed"] is True
        assert payload["reloaded"] is True
        assert config_servers(gateway)[ARGS["name"]]["headers"] == {"X-Ace-Fixture": "updated"}
        assert f"{ARGS['name']}__echo" in gateway.crew.registry.names()


async def test_gateway_plan_never_exposes_install_or_writes_on_guessed_call(gateway):
    assert TOOL in gateway.crew.registry.names()
    original = gateway.config_file.read_bytes()
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "plan", plan=True)
        final, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert not is_permission_card(final), frames
        assert final["kind"] == "final", frames
    assert gateway.provider.offered
    management = {TOOL, "mcp_status", "mcp_remove", "mcp_reload", "mcp_update"}
    assert all(not (management & names) for names in gateway.provider.offered)
    assert not config_servers(gateway)
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []


async def test_lightweight_child_cannot_install_even_if_model_guesses_tool(gateway):
    assert TOOL in gateway.crew.registry.names()
    original = gateway.config_file.read_bytes()
    query = "fixture child was asked to install"
    gateway.provider.script[query] = ToolCall("child-tool", TOOL, ARGS)
    effective = gateway.crew._resolve_subagent_tool_scope(
        None, [TOOL], user_type="internal", depth=1,
    )
    child = gateway.crew._build_single_agent(
        provider=gateway.provider, executor_kind="builtin", executor_config={},
        max_iterations=2, tool_filter=effective, lightweight=True, owner_account_id="local",
    )
    try:
        envelope = Envelope.of(query, session_id="fixture-child", user_id="local", channel="web")
        envelope.params["_allow_mcp_install"] = True
        frames = [row async for row in child.run(envelope)]
    finally:
        await child.aclose()
    assert frames
    assert not config_servers(gateway)
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []


async def test_model_cannot_forge_approval_in_tool_arguments(gateway):
    original = gateway.config_file.read_bytes()
    session_id = "mcp-install-fixture"
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "forged", session_id=session_id,
                            arguments={**ARGS, "approved": True, "allow_once": True})
        event, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert gateway.config_file.read_bytes() == original
        assert gateway.io == []
        if is_permission_card(event):
            await reply_permission(send, session_id, "forged", event, answer="deny")
            event, frames = await read_until(receive, lambda row: row.get("is_final") is True)
        assert event["kind"] in {"final", "error"}, frames
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []


@pytest.mark.parametrize("gateway", ["managed"], indirect=True)
async def test_managed_gateway_rejects_host_stdio_before_persist_or_spawn(gateway):
    original = gateway.config_file.read_bytes()
    async with open_ws(gateway.app) as (send, receive):
        await begin_install(gateway, send, "managed-stdio",
                            arguments={"name": "never-spawn", "transport": "stdio",
                                       "command": "fixture-must-not-execute"})
        final, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert not is_permission_card(final), frames
        assert final["kind"] in {"final", "error"}, frames
    assert gateway.provider.tool_results, "no tool outcome returned to the authenticated admin"
    assert gateway.config_file.read_bytes() == original
    assert not config_servers(gateway)
    assert gateway.io == []


async def test_direct_registry_with_admin_context_still_requires_one_time_approval(gateway):
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id

    original = gateway.config_file.read_bytes()
    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("direct-with-authority")
    try:
        result = await gateway.crew.registry.execute(ToolCall("direct-with-authority", TOOL, ARGS))
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)
    assert result.is_error and result.code == "approval_required", result
    assert not config_servers(gateway)
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []


@pytest.mark.parametrize("gateway", ["email"], indirect=True)
async def test_authenticated_non_admin_cannot_ask_for_mcp_install(gateway):
    original = gateway.config_file.read_bytes()
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 16000))
    async with httpx.AsyncClient(transport=transport, base_url="http://local-gateway") as client:
        login = await client.post("/api/auth/login", json={"email": "regular-fixture@example.invalid"})
        assert login.status_code == 200
        config = await client.get("/api/config")
        assert config.status_code == 200 and config.json()["is_gateway_admin"] is False
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items()).encode("latin1")
        assert cookie
    async with open_ws(gateway.app, headers=[(b"cookie", cookie)]) as (send, receive):
        await begin_install(gateway, send, "nonadmin")
        event, frames = await read_until(receive, lambda row: is_permission_card(row) or row.get("is_final"))
        assert not is_permission_card(event), frames
        assert event["kind"] == "final", frames
    assert gateway.provider.offered and all(TOOL not in modes for modes in gateway.provider.offered)
    assert not config_servers(gateway)
    assert gateway.config_file.read_bytes() == original
    assert gateway.io == []
