"""MCP 2 Client（连真 stdio server）+ MCPServer 工具。"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")  # 未装 mcp 包则跳过

from crew.agent.skills import SkillActivation
from crew.app import CrewApp
from crew.core.mocks import FakeProvider, InMemorySessionStore, InMemoryWorkspaceStore, NullMemory
from crew.core.types import Message, ToolCall
from crew.gateway.interaction_bridge import InteractionBridge
from crew.gateway.mcp_server import build_interaction_mcp_server, build_mcp_server
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.tasks.task_manager import InMemoryTaskManager
from crew.team.team_manager import InProcessTeamManager
from crew.tools.mcp_client import MCPClientManager, probe_remote_server
from crew.tools.registry import Registry, register_builtin_tools

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "echo_mcp_server.py")


def _crew() -> CrewApp:
    reg = Registry()
    register_builtin_tools(reg)
    return CrewApp(
        Config(),
        FakeProvider(),
        reg,
        InMemorySessionStore(),
        InMemoryWorkspaceStore(),
        NullMemory(),
        PluginManager(),
    )


def _crew_with_team() -> CrewApp:
    crew = _crew()
    crew.team = InProcessTeamManager(
        provider=crew.provider,
        registry=crew.registry,
        session_store=crew.session_store,
        memory=crew.memory,
        plugins=crew.plugins,
        tasks=InMemoryTaskManager(),
        config=Config(max_iterations=5),
    )
    return crew


# ---- MCP Client ----

async def test_mcp_client_connects_and_calls(monkeypatch):
    from crew.security.launch import current_process_launch
    from crew.security.models import PermissionProfile, PermissionProfileKind

    monkeypatch.setenv("ACE_ALLOW_HOST_MCP_STDIO", "1")
    launch_token = current_process_launch.set(SimpleNamespace(
        managed=False,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
    ))
    reg = Registry()
    mgr = MCPClientManager({"echo": {"command": sys.executable, "args": [_FIXTURE]}})
    await mgr.start(reg)
    await mgr.await_started()  # start 为 fire-and-forget，需显式等待后台注册完成
    try:
        assert "echo__echo" in reg.names()  # 外部工具已注册
        registered = reg.get("echo__echo")
        assert registered.parameters["required"] == ["text"]
        assert registered.parameters["properties"]["text"]["type"] == "string"
        res = await reg.execute(ToolCall("1", "echo__echo", {"text": "hi"}))
        assert not res.is_error
        assert "echo: hi" in res.content

        structured = await reg.execute(ToolCall("structured-1", "echo__structured", {"text": "测试"}))
        assert not structured.is_error
        assert structured.content.startswith("Structured content:\n")
        assert json.loads(structured.content.removeprefix("Structured content:\n")) == {"value": "测试", "count": 7}

        failed = await reg.execute(ToolCall("2", "echo__fail", {"message": "expected failure"}))
        assert failed.is_error
        assert "fail" in failed.content  # MCP 2 masks unhandled server exception details.
    finally:
        await mgr.aclose()
        current_process_launch.reset(launch_token)


async def test_mcp_client_streamable_http_connects_and_calls(monkeypatch, tmp_path):
    """HTTP 分支使用 MCP 2 的 streamable_http_client、httpx2 和双元素流。"""
    import httpx2
    from mcp.server import MCPServer
    from mcp.types import CallToolResult, ImageContent, TextContent

    server = MCPServer("http-echo", version="2.0-test")

    @server.tool()
    def http_echo(text: str) -> str:
        return f"http echo: {text}"

    duplicate = '{ "status": "ready", "count": 3 }'
    image = ImageContent(type="image", mime_type="image/png", data="iVBORw0KGgo=")
    structured = {"status": "ready", "count": 3}
    results = {
        "mixed": CallToolResult(
            content=[TextContent(type="text", text="Summary"), image], structured_content=structured,
        ),
        "large_mixed": CallToolResult(
            content=[TextContent(type="text", text="Large summary"), image],
            structured_content={"body": '"line"\n' * 10_000, "end": "last field"},
        ),
        "duplicate": CallToolResult(
            content=[TextContent(type="text", text=duplicate)], structured_content=structured,
        ),
        "empty": CallToolResult(content=[TextContent(type="text", text="unchanged")], structured_content={}),
        "error": CallToolResult(
            content=[TextContent(type="text", text="cannot continue"), image],
            structured_content={"state": "incomplete"}, is_error=True,
        ),
    }

    @server.tool()
    def http_result(shape: str) -> CallToolResult:
        return results[shape]

    app = server.streamable_http_app(stateless_http=True, json_response=True)
    original_async_client = httpx2.AsyncClient
    seen: dict[str, object] = {}

    def asgi_client(**kwargs):
        seen.update(kwargs)
        return original_async_client(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            **kwargs,
        )

    monkeypatch.setattr(httpx2, "AsyncClient", asgi_client)
    from crew.security.context import SecurityContext
    from crew.security.launch import current_process_launch

    context = SecurityContext(
        os_user="tester",
        owner_account_id="owner",
        workspace_id="default",
        workspace_root=tmp_path,
        session_id="http-session",
        request_id="http-1",
        task_id="http-task",
        cwd=tmp_path,
    )

    async def authorize_remote_call(*_args, security_context, **_kwargs):
        seen["security_context"] = security_context

    monkeypatch.setattr(
        "crew.tools.security_guard.authorize_configured_mcp_call",
        authorize_remote_call,
    )
    launch_token = current_process_launch.set(SimpleNamespace(
        security_context=context,
        approval_service=object(),
    ))
    reg = Registry()
    mgr = MCPClientManager({
        "remote": {
            "url": "http://127.0.0.1:8000/mcp",
            "transport": "http",
            "headers": {"X-MCP-Test": "mcp2"},
        }
    })

    try:
        async with app.router.lifespan_context(app):
            await mgr.start(reg)
            await mgr.await_started()
            try:
                assert "remote__http_echo" in reg.names()
                assert seen["headers"] == {"X-MCP-Test": "mcp2"}
                assert seen["follow_redirects"] is True
                result = await reg.execute(
                    ToolCall("http-1", "remote__http_echo", {"text": "hi"})
                )
                assert not result.is_error
                assert "http echo: hi" in result.content
                assert seen["security_context"] == context

                async def call(shape):
                    return await reg.execute(ToolCall(shape, "remote__http_result", {"shape": shape}))

                mixed = await call("mixed")
                assert not mixed.is_error
                mixed_content = json.loads(mixed.content)
                summary, details = mixed_content["text"].split("\n\nStructured content:\n")
                assert summary == "Summary"
                assert json.loads(details) == structured
                assert mixed_content["images"][0]["url"] == "data:image/png;base64,iVBORw0KGgo="

                large = await call("large_mixed")
                assert not large.is_error
                assert len(large.content) <= 50_000
                large_content = json.loads(large.content)
                assert large_content["images"] == mixed_content["images"]
                assert large_content["text"].startswith("Large summary\n\nStructured content:\n")
                assert '"end": "last field"' in large_content["text"]
                assert "输出已截断" in large_content["text"]

                assert (await call("duplicate")).content == duplicate
                assert (await call("empty")).content == "unchanged"
                failed = await call("error")
                assert failed.is_error
                assert 'cannot continue\n\nStructured content:\n{"state": "incomplete"}' in failed.content
                assert "iVBORw0KGgo=" not in failed.content

                probe = await probe_remote_server({
                    "url": "http://127.0.0.1:8000/mcp",
                    "transport": "http",
                    "headers": {"X-MCP-Test": "probe"},
                })
                assert probe["ok"] is True, probe
                assert "http_echo" in probe["tools"]
            finally:
                await mgr.aclose()
    finally:
        current_process_launch.reset(launch_token)


async def test_probe_caps_reported_tool_names(monkeypatch):
    import crew.tools.mcp_client as mcp_client

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            self._tools = [SimpleNamespace(name=f"tool-{index}") for index in range(60)]
            self._error = None

        async def start(self):
            return True

        async def stop(self):
            return None

    monkeypatch.setattr(mcp_client, "_ServerWorker", FakeWorker)
    result = await mcp_client.probe_remote_server({"url": "http://127.0.0.1/mcp", "transport": "http"})
    assert result["ok"] is True
    assert result["tool_count"] == 60
    assert len(result["tools"]) == 50
    assert result["tools"][0] == "tool-0"


async def test_probe_stop_is_bounded(monkeypatch):
    import crew.tools.mcp_client as mcp_client

    class HangingStopWorker:
        last = None

        def __init__(self, *_args, **_kwargs):
            self._tools = []
            self._error = None
            self.aborted = False
            HangingStopWorker.last = self

        async def start(self):
            return True

        async def stop(self):
            await asyncio.sleep(10)

        def force_abort(self, _reason):
            self.aborted = True

    monkeypatch.setattr(mcp_client, "_ServerWorker", HangingStopWorker)
    monkeypatch.setattr(mcp_client, "MCP_PROBE_STOP_TIMEOUT_SECONDS", 0.05)
    started = asyncio.get_running_loop().time()
    result = await mcp_client.probe_remote_server({"url": "http://127.0.0.1/mcp", "transport": "http"})
    assert result["ok"] is True
    assert asyncio.get_running_loop().time() - started < 1
    assert HangingStopWorker.last is not None and HangingStopWorker.last.aborted is True


async def test_disabled_reconnect_reports_one_shot_failure(monkeypatch):
    from crew.tools.mcp_client import _ServerWorker

    worker = _ServerWorker(
        "probe-disabled",
        {
            "url": "http://127.0.0.1/mcp",
            "transport": "http",
            "reconnect": {"enabled": False},
            "fail_on_startup_error": True,
        },
        Registry(),
        startup_timeout=1,
    )

    async def fail(_stack):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_open", fail)
    try:
        assert await worker.start() is False
        await asyncio.sleep(0)
        assert "不重试" in str(worker._error)
        assert "预算" not in str(worker._error)
    finally:
        await worker.stop()


async def test_mcp_client_empty_config_noop():
    reg = Registry()
    mgr = MCPClientManager({})
    await mgr.start(reg)  # 不应抛出
    assert reg.names() == []
    await mgr.aclose()


# ---- MCP Server ----

async def test_mcp_server_exposes_tools():
    crew = _crew()
    server = build_mcp_server(crew)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {
        "sessions_list",
        "session_history",
        "send_message",
        "session_status",
        "team_request_delegate",
        "team_plan_create",
        "team_plan_read",
        "team_plan_update",
    } <= names


async def test_mcp_server_team_plan_tools_call_team_manager():
    crew = _crew_with_team()
    server = build_mcp_server(crew)
    created = await server.call_tool("team_plan_create", {
        "session_id": "mcp_plan_s1",
        "owner_account_id": "A:uid-a",
        "goal": "完成小任务",
        "nodes": [{"id": "code", "title": "编码", "detail": "写代码", "assignee": "coder"}],
        "edges": [],
    })
    assert "code" in str(created)
    read = await server.call_tool(
        "team_plan_read",
        {"session_id": "mcp_plan_s1", "owner_account_id": "A:uid-a"},
    )
    assert "编码" in str(read)
    updated = await server.call_tool("team_plan_update", {
        "session_id": "mcp_plan_s1",
        "owner_account_id": "A:uid-a",
        "node_id": "code",
        "status": "completed",
        "result_summary": "done",
    })
    assert "completed" in str(updated)


async def test_mcp_server_sessions_list_reads_store():
    crew = _crew()
    crew.session_store.save("s1", [Message.user("第一个问题"), Message.assistant("答")], owner_account_id="A:uid-a")
    server = build_mcp_server(crew)
    result = await server.call_tool("sessions_list", {"owner_account_id": "A:uid-a"})
    assert "s1" in str(result)


async def test_mcp_server_round_trips_through_mcp2_client():
    """内置 Crew MCPServer 能被 MCP 2 Client 发现并通过协议实际调用。"""
    from mcp import Client

    crew = _crew()
    crew.session_store.save(
        "protocol-s1",
        [Message.user("协议往返")],
        owner_account_id="A:protocol-user",
    )
    server = build_mcp_server(crew)

    async with Client(server, mode="auto") as client:
        tools = await client.list_tools()
        assert "sessions_list" in {tool.name for tool in tools.tools}
        result = await client.call_tool(
            "sessions_list",
            {"owner_account_id": "A:protocol-user"},
        )

    assert result.is_error is False
    assert "protocol-s1" in _extract_protocol_text(result)


def _extract_protocol_text(result) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in result.content
        if getattr(block, "type", "") == "text"
    )


async def test_interaction_mcp_server_only_exposes_followup():
    server = build_interaction_mcp_server("http://127.0.0.1:8000", "test-token")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {"ask_followup_question"}


async def test_interaction_mcp_server_exposes_role_scoped_team_tools():
    leader = build_interaction_mcp_server(
        "http://127.0.0.1:8000",
        "leader-token",
        context_type="team",
        team_role="leader",
    )
    member = build_interaction_mcp_server(
        "http://127.0.0.1:8000",
        "member-token",
        context_type="team",
        team_role="member",
    )

    assert {tool.name for tool in await leader.list_tools()} == {
        "ask_followup_question",
        "team_mention",
        "team_plan_create",
        "team_plan_read",
        "team_plan_update",
    }
    assert {tool.name for tool in await member.list_tools()} == {
        "team_mention",
        "team_plan_read",
        "team_plan_update",
    }


async def test_interaction_bridge_binds_question_to_display_session():
    from crew.core.followup import resolve_answer

    pushed = []

    async def push(sid, payload, *, owner_account_id=""):
        pushed.append((sid, payload, owner_account_id))
        question_id = payload["body"]["question_id"]
        resolve_answer(sid, question_id, [{"question_id": "q1", "answers": ["B"]}], owner_account_id=owner_account_id)

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="main-session",
        control_session_id="main-session::turn::req_1",
        origin_session_id="main-session::codex",
        agent_name="Codex",
        ttl_seconds=30,
    )
    assert binding is not None
    assert binding.control_session_id == "main-session::turn::req_1"

    result = await bridge.ask(
        binding.token,
        title="选择",
        questions=[{
            "id": "q1",
            "question": "选哪个？",
            "options": ["A", "B"],
            "multiSelect": False,
        }],
    )

    assert result["answers"] == [{"question_id": "q1", "answers": ["B"]}]
    assert pushed[0][0] == "main-session"
    assert pushed[0][1]["body"]["origin"]["agent_name"] == "Codex"
    assert pushed[0][2] == "A:uid-a"


async def test_interaction_bridge_permission_uses_control_plane_origin_without_history():
    from crew.core.followup import drain_followup_answer_messages, resolve_answer

    pushed = []

    async def push(sid, payload, *, owner_account_id=""):
        pushed.append((sid, payload, owner_account_id))
        resolve_answer(
            sid,
            payload["body"]["question_id"],
            [{"question_id": "permission", "answers": ["allow_once"]}],
            owner_account_id=owner_account_id,
        )

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="team-session",
        control_session_id="team-session::turn::req_1",
        origin_session_id="team-session::hermes",
        agent_name="Hermes",
        ttl_seconds=30,
    )
    assert binding is not None

    approved = await bridge.ask_permission(
        binding.token,
        title="操作权限确认",
        question="Hermes 请求写入工作区外文件。",
        display_name="像素开发小游戏团队",
        origin_type="team_control",
    )

    assert approved is True
    body = pushed[0][1]["body"]
    assert body["origin"]["type"] == "team_control"
    assert body["origin"]["agent_name"] == "像素开发小游戏团队"
    assert body["questions"][0]["allowFreeText"] is False
    assert drain_followup_answer_messages("team-session") == []


async def test_interaction_bridge_serializes_three_permission_answers():
    from crew.core.followup import resolve_answer

    pushed = []

    async def push(sid, payload, *, owner_account_id=""):
        pushed.append((sid, payload, owner_account_id))
        resolve_answer(
            sid,
            payload["body"]["question_id"],
            [{"question_id": "permission", "answers": ["allow_once"]}],
            owner_account_id=owner_account_id,
        )

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="team-session",
        control_session_id="team-session::turn::req_1",
        origin_session_id="team-session::kimi",
        agent_name="Kimi",
        ttl_seconds=30,
    )
    assert binding is not None

    results = await asyncio.gather(*[
        bridge.ask_permission(
            binding.token,
            title="操作权限确认",
            question=f"即将执行：修改文件\n目标：outside-{index}.js",
            display_name="产品开发团队",
            origin_type="team_control",
        )
        for index in range(3)
    ])

    assert results == [True, True, True]
    assert len(pushed) == 3
    assert len({item[1]["body"]["question_id"] for item in pushed}) == 3


async def test_interaction_bridge_pushes_followup_with_owner_scope():
    from crew.core.followup import resolve_answer

    pushed = []

    async def push(sid, payload, *, owner_account_id=""):
        pushed.append((sid, payload, owner_account_id))
        question_id = payload["body"]["question_id"]
        resolve_answer(sid, question_id, [{"question_id": "q1", "answers": ["B"]}], owner_account_id=owner_account_id)

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    binding = bridge.create_binding(
        owner_account_id="acct:test",
        display_session_id="main-session",
        control_session_id="main-session::turn::req_1",
        origin_session_id="main-session::codex",
        agent_name="Codex",
        ttl_seconds=30,
    )
    assert binding is not None

    await bridge.ask(
        binding.token,
        title="选择",
        questions=[{
            "id": "q1",
            "question": "选哪个？",
            "options": ["A", "B"],
            "multiSelect": False,
        }],
    )

    assert pushed[0][0] == "main-session"
    assert pushed[0][2] == "acct:test"



async def test_mcp_server_requires_explicit_owner():
    crew = _crew()
    server = build_mcp_server(crew)

    with pytest.raises(Exception, match="sessions_list|owner_account_id"):
        await server.call_tool("sessions_list", {"owner_account_id": ""})


async def test_mcp_server_owner_scopes_history_and_status():
    crew = _crew()
    crew.session_store.save("same", [Message.user("A")], owner_account_id="A:uid-a")
    crew.session_store.save("same", [Message.user("B")], owner_account_id="B:uid-b")
    crew.session_store.set_status("same", "completed", owner_account_id="A:uid-a", error="A done")
    crew.session_store.set_status("same", "failed", owner_account_id="B:uid-b", error="B failed")
    server = build_mcp_server(crew)

    history = await server.call_tool("session_history", {"session_id": "same", "owner_account_id": "B:uid-b"})
    status = await server.call_tool("session_status", {"session_id": "same", "owner_account_id": "B:uid-b"})

    assert "B" in str(history)
    assert "A" not in str(history)
    assert "B failed" in str(status)
    assert "A done" not in str(status)


async def test_interaction_team_routes_use_control_session_id():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from crew.gateway.interaction_bridge import create_interaction_router

    seen = {}

    class FakeTeam:
        def create_plan(self, session_id, **kwargs):
            seen["create"] = (session_id, kwargs)
            return {"ok": True, "session_id": session_id}

        def read_plan(self, session_id, **kwargs):
            seen["read"] = (session_id, kwargs)
            return {"ok": True, "session_id": session_id}

        def update_plan_node(self, session_id, **kwargs):
            seen["update"] = (session_id, kwargs)
            return {"ok": True, "session_id": session_id}

        async def external_team_mention(self, session_id, **kwargs):
            seen["mention"] = (session_id, kwargs)
            return {"ok": True, "session_id": session_id}

    class FakeCrew:
        team = FakeTeam()
        logout_coordinator = SimpleNamespace(allows_work=lambda owner: owner == "A:uid-a")

    async def push(_sid, _payload):
        return None

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    active = SkillActivation(
        skill_id="directory-search",
        name="统一搜索",
        instruction="instructions",
        skill_root="/skills/directory-search",
    )
    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="web_s1",
        control_session_id="web_s1::turn::req_1",
        origin_session_id="web_s1::turn::req_1::leader",
        agent_name="Leader",
        ttl_seconds=30,
        context_type="team",
        team_session_id="web_s1::turn::req_1",
        member_id="leader",
        team_role="leader",
        active_skills=(active,),
    )
    assert binding is not None

    app = FastAPI()
    app.include_router(create_interaction_router(bridge, FakeCrew()))
    headers = {"Authorization": f"Bearer {binding.token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        await client.post(
            "/api/internal/team/plan/create",
            headers=headers,
            json={"goal": "g", "nodes": [{"id": "n1", "title": "N1"}], "edges": []},
        )
        await client.post("/api/internal/team/plan/read", headers=headers, json={})
        await client.post(
            "/api/internal/team/plan/update",
            headers=headers,
            json={"node_id": "n1", "status": "completed"},
        )
        await client.post(
            "/api/internal/team/mention",
            headers=headers,
            json={"to": ["coder"], "intent": "assign", "content": "do it"},
        )

    assert seen["create"][0] == "web_s1::turn::req_1"
    assert seen["read"][0] == "web_s1::turn::req_1"
    assert seen["update"][0] == "web_s1::turn::req_1"
    assert seen["mention"][0] == "web_s1::turn::req_1"
    assert seen["mention"][1]["member_id"] == "leader"
    assert seen["mention"][1]["task_payload_meta"]["active_skills"][0]["skill_id"] == "directory-search"


def test_mcp_status_projection_redacts_configuration_secrets():
    from crew.tools.mcp_management import _public_server_status

    public = _public_server_status({
        "name": "remote-docs",
        "transport": "http",
        "connected": False,
        "state": "failed",
        "error": (
            "failed https://user:pass@mcp.example.com/secret-path?token=abc "
            "with sk-live-123 and Bearer header-secret"
        ),
        "tools": ["echo", "lookup"],
        "config": {
            "url": "https://user:pass@mcp.example.com/secret-path?token=abc",
            "transport": "http",
            "enabled": True,
            "env": {"API_KEY": "sk-live-123"},
            "headers": {"Authorization": "Bearer header-secret"},
        },
    })
    blob = json.dumps(public)
    assert public["name"] == "remote-docs"
    assert public["target"] == "https://mcp.example.com"
    assert public["connected"] is False
    assert public["state"] == "failed"
    assert public["enabled"] is True
    assert public["tool_count"] == 2
    assert public["tools"] == ["echo", "lookup"]
    assert "sk-live-123" not in blob
    assert "header-secret" not in blob
    assert "pass@" not in blob
    assert "secret-path" not in blob
    assert "token=abc" not in blob


async def test_mcp_status_tool_requires_management_context_and_lists_safe_status():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import register_mcp_tools

    registry = Registry()
    rows = [{
        "name": "remote-docs",
        "transport": "http",
        "connected": True,
        "state": "connected",
        "error": "",
        "tools": ["echo"],
        "config": {
            "url": "https://mcp.example.com/v1",
            "transport": "http",
            "headers": {"Authorization": "Bearer hidden"},
        },
    }]
    app = SimpleNamespace(
        mcp_manager=SimpleNamespace(status=lambda: rows),
        config=SimpleNamespace(mcp_servers={}),
        registry=registry,
    )
    register_mcp_tools(app)

    denied = await registry.execute(ToolCall("status-1", "mcp_status", {}))
    assert denied.is_error
    assert "管理权限" in denied.content

    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("status-call")
    try:
        result = await registry.execute(ToolCall("status-2", "mcp_status", {"name": "remote-docs"}))
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    assert payload["servers"][0]["name"] == "remote-docs"
    assert payload["servers"][0]["connected"] is True
    assert payload["servers"][0]["state"] == "connected"
    assert payload["servers"][0]["tools"] == ["echo"]
    assert "hidden" not in result.content


async def test_mcp_remove_requires_one_time_approval_and_deletes_config():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import register_mcp_tools

    registry = Registry()
    servers = {"echo": {"command": "echo-server"}}

    class ConfigStub:
        config_path = "config.yaml"
        mcp_servers = servers

        def delete_mcp_server(self, name):
            self.mcp_servers.pop(name)

    class ManagerStub:
        def __init__(self):
            self.removed = []

        async def remove_server(self, name):
            self.removed.append(name)

    manager = ManagerStub()
    app = SimpleNamespace(config=ConfigStub(), mcp_manager=manager, registry=registry)
    register_mcp_tools(app)

    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("remove-call")
    try:
        tool = registry.get("mcp_remove")
        decision = tool.permission_resolver({"name": "echo"})
        assert decision.behavior == "ask"
        assert decision.allow_always is False
        assert decision.approval_token
        assert tool.permission_approver(decision.approval_token, {"name": "echo"}) is True
        result = await registry.execute(ToolCall("remove-1", "mcp_remove", {"name": "echo"}))
        absent = await registry.execute(ToolCall("remove-2", "mcp_remove", {"name": "echo"}))
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload == {
        "name": "echo",
        "removed": True,
        "already_absent": False,
        "disconnected": True,
    }
    assert "echo" not in servers
    assert manager.removed == ["echo"]
    assert json.loads(absent.content)["already_absent"] is True


async def test_mcp_remove_rejects_stale_approval_and_missing_management_context():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import register_mcp_tools

    registry = Registry()
    servers = {"echo": {"command": "echo-server"}}

    class ConfigStub:
        config_path = "config.yaml"
        mcp_servers = servers

        def delete_mcp_server(self, name):
            self.mcp_servers.pop(name)

    app = SimpleNamespace(
        config=ConfigStub(),
        mcp_manager=SimpleNamespace(remove_server=lambda _name: asyncio.sleep(0)),
        registry=registry,
    )
    register_mcp_tools(app)

    denied = await registry.execute(ToolCall("remove-denied", "mcp_remove", {"name": "echo"}))
    assert denied.is_error

    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("remove-stale")
    try:
        tool = registry.get("mcp_remove")
        decision = tool.permission_resolver({"name": "echo"})
        servers["echo"] = {"command": "different-server"}
        assert tool.permission_approver(decision.approval_token, {"name": "echo"}) is False
        stale = await registry.execute(ToolCall("remove-stale", "mcp_remove", {"name": "echo"}))
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)
    assert stale.is_error
    assert "一次性批准" in stale.content
    assert servers["echo"] == {"command": "different-server"}


async def test_mcp_reload_requires_management_context_and_reconnects_configured_server():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import register_mcp_tools

    registry = Registry()
    server_cfg = {"url": "https://mcp.example.com/v1", "transport": "http"}
    servers = {"echo": dict(server_cfg)}

    class ManagerStub:
        def __init__(self):
            self.reloaded = []

        async def reload_one(self, name):
            self.reloaded.append(name)
            return True

        def status(self):
            return [{
                "name": "echo",
                "transport": "http",
                "connected": True,
                "state": "connected",
                "error": "",
                "tools": ["echo"],
                "config": dict(server_cfg),
            }]

    manager = ManagerStub()
    app = SimpleNamespace(
        config=SimpleNamespace(mcp_servers=servers),
        mcp_manager=manager,
        registry=registry,
    )
    register_mcp_tools(app)

    denied = await registry.execute(ToolCall("reload-denied", "mcp_reload", {"name": "echo"}))
    assert denied.is_error

    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("reload-call")
    try:
        result = await registry.execute(ToolCall("reload-1", "mcp_reload", {"name": "echo"}))
        missing = await registry.execute(ToolCall("reload-2", "mcp_reload", {"name": "missing"}))
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["reloaded"] is True
    assert payload["server"]["state"] == "connected"
    assert payload["server"]["tools"] == ["echo"]
    assert manager.reloaded == ["echo"]
    assert missing.is_error
    assert "不存在" in missing.content


async def test_manager_serializes_concurrent_adds(monkeypatch):
    from crew.tools import mcp_client

    class FakeWorker:
        name = "echo"

        def __init__(self, *_args, **_kwargs):
            pass

        def _unregister_tools(self):
            pass

        async def start(self):
            await asyncio.sleep(0.02)
            return True

        async def stop(self):
            pass

    monkeypatch.setattr(mcp_client, "_ServerWorker", FakeWorker)
    manager = mcp_client.MCPClientManager({})
    manager._registry = Registry()
    manager._start_task = asyncio.create_task(asyncio.sleep(0))
    await manager._start_task

    results = await asyncio.gather(
        manager.add_server("echo", {"url": "https://example.invalid/mcp"}),
        manager.add_server("echo", {"url": "https://example.invalid/mcp"}),
    )

    assert sorted(results) == [False, True]
    assert len([worker for worker in manager._workers if worker.name == "echo"]) == 1


async def test_manager_cleans_up_a_worker_adopted_during_shutdown():
    from crew.tools.mcp_client import MCPClientManager

    class FakeWorker:
        name = "echo"

        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    manager = MCPClientManager({})
    manager._closing = True
    worker = FakeWorker()

    assert await manager._adopt_worker(worker) is False
    assert worker.stopped is True
    assert manager._workers == []


async def test_manager_close_wins_over_a_slow_reload(monkeypatch):
    from crew.tools import mcp_client

    started = asyncio.Event()
    release = asyncio.Event()

    class FakeWorker:
        name = "echo"

        def __init__(self, *_args, **_kwargs):
            self.stopped = False

        def _unregister_tools(self):
            pass

        async def stop(self):
            self.stopped = True

        async def start(self):
            started.set()
            await release.wait()
            return True

    monkeypatch.setattr(mcp_client, "_ServerWorker", FakeWorker)
    manager = mcp_client.MCPClientManager({"echo": {"url": "https://example.invalid/mcp"}})
    manager._registry = Registry()
    manager._start_task = asyncio.create_task(asyncio.sleep(0))
    await manager._start_task

    reloading = asyncio.create_task(manager.reload_one("echo"))
    await started.wait()
    await manager.aclose()
    release.set()
    await reloading

    assert manager._workers == []


async def test_manager_serializes_concurrent_server_reloads(monkeypatch):
    from crew.tools import mcp_client

    events: list[tuple[str, str]] = []

    class FakeWorker:
        def __init__(self, name, _cfg, _registry, **_kwargs):
            self.name = name

        def _unregister_tools(self):
            events.append(("unregister", self.name))

        async def stop(self):
            events.append(("stop", self.name))
            await asyncio.sleep(0)

        async def start(self):
            events.append(("start", self.name))
            await asyncio.sleep(0.02)
            return True

    monkeypatch.setattr(mcp_client, "_ServerWorker", FakeWorker)
    manager = mcp_client.MCPClientManager({"echo": {"url": "https://example.invalid/mcp"}})
    manager._registry = Registry()
    manager._start_task = asyncio.create_task(asyncio.sleep(0))
    await manager._start_task

    results = await asyncio.gather(
        manager.reload_one("echo"),
        manager.reload_one("echo"),
    )

    assert results == [True, True]
    assert len([worker for worker in manager._workers if worker.name == "echo"]) == 1
    assert events.count(("start", "echo")) == 2
    assert events.count(("stop", "echo")) == 1


def test_mcp_status_invalid_port_does_not_break_projection():
    from crew.tools.mcp_management import _public_server_status

    public = _public_server_status({
        "name": "bad-port",
        "transport": "http",
        "connected": False,
        "tools": [],
        "config": {"url": "https://mcp.example.com:notaport/v1"},
    })
    assert public["target"] == ""
    assert public["name"] == "bad-port"


async def test_mcp_update_applies_approved_config_and_reloads():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import (
        _approved_update,
        _OneShotApproval,
        _proposal,
        _update_digest,
        register_mcp_tools,
    )

    registry = Registry()
    previous = {"transport": "http", "url": "https://old.example.com/v1"}

    class ConfigStub:
        config_path = "/tmp/mcp-config.yaml"

        def __init__(self):
            self.mcp_servers = {"echo": dict(previous)}

        def update_mcp_server(self, name, config):
            self.mcp_servers[name] = dict(config)

    class ManagerStub:
        def __init__(self):
            self.reloads = []
            self.server_config = dict(previous)

        async def reload_one(self, name, cfg=None):
            self.reloads.append((name, dict(cfg or {})))
            self.server_config = dict(cfg or {})
            return True

        def status(self):
            return [{
                "name": "echo",
                "transport": "http",
                "connected": True,
                "state": "connected",
                "tools": ["echo"],
                "config": dict(self.server_config),
            }]

    manager = ManagerStub()
    app = SimpleNamespace(config=ConfigStub(), mcp_manager=manager, registry=registry)
    register_mcp_tools(app)
    new_args = {
        "name": "echo",
        "transport": "http",
        "url": "https://new.example.com/v1?token=secret-value",
    }
    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("update-call")
    try:
        _, new_config = _proposal(new_args, "更新")
        approval = _approved_update.set(_OneShotApproval(_update_digest(
            app.config.config_path, "echo", previous, new_config,
        )))
        try:
            result = await registry.execute(ToolCall("update-1", "mcp_update", new_args))
        finally:
            _approved_update.reset(approval)
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["changed"] is True
    assert payload["reloaded"] is True
    assert payload["server"]["state"] == "connected"
    assert payload["server"]["target"] == "https://new.example.com"
    assert "secret-value" not in result.content
    assert app.config.mcp_servers["echo"] == new_config
    assert manager.reloads == [("echo", new_config)]


async def test_mcp_update_rejects_a_stale_approval_and_unknown_server():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import (
        _approved_update,
        _OneShotApproval,
        _proposal,
        _update_digest,
        register_mcp_tools,
    )

    registry = Registry()
    previous = {"transport": "http", "url": "https://old.example.com/v1"}

    class ConfigStub:
        config_path = "/tmp/mcp-config.yaml"

        def __init__(self):
            self.mcp_servers = {"echo": dict(previous)}
            self.updates = []

        def update_mcp_server(self, name, config):
            self.updates.append((name, dict(config)))
            self.mcp_servers[name] = dict(config)

    app = SimpleNamespace(config=ConfigStub(), mcp_manager=None, registry=registry)
    register_mcp_tools(app)
    new_args = {"name": "echo", "transport": "http", "url": "https://new.example.com/v1"}
    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("update-call-2")
    try:
        _, new_config = _proposal(new_args, "更新")
        approval = _approved_update.set(_OneShotApproval(_update_digest(
            app.config.config_path, "echo", previous, new_config,
        )))
        try:
            app.config.mcp_servers["echo"] = {
                "transport": "http", "url": "https://other.example.com/v1",
            }
            stale = await registry.execute(ToolCall("update-2", "mcp_update", new_args))
            missing = await registry.execute(ToolCall("update-3", "mcp_update", {
                "name": "missing", "transport": "http", "url": "https://missing.example.com/v1",
            }))
        finally:
            _approved_update.reset(approval)
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)

    assert stale.is_error
    assert "批准" in stale.content
    assert missing.is_error
    assert "不存在" in missing.content
    assert app.config.updates == []


def test_mcp_worker_state_distinguishes_starting_from_reconnecting():
    from crew.tools.mcp_client import _ServerWorker

    worker = _ServerWorker("echo", {"url": "https://example.invalid"}, Registry())

    class _PendingTask:
        def done(self) -> bool:
            return False

    assert worker.state == "starting"
    worker._task = _PendingTask()
    assert worker.state == "starting"
    worker._ready.set()
    worker._error = RuntimeError("connect failed")
    assert worker.state == "reconnecting"
    worker._permanent_error = RuntimeError("reconnect budget exhausted")
    assert worker.state == "failed"
    worker._permanent_error = None
    worker._connected = True
    assert worker.state == "connected"


def test_mcp_remove_denies_when_configuration_cannot_be_written():
    from crew.core.runctx import current_mcp_install_allowed, current_tool_call_id
    from crew.tools.mcp_management import register_mcp_tools

    registry = Registry()
    app = SimpleNamespace(
        config=SimpleNamespace(
            config_path="",
            mcp_servers={"echo": {"command": "echo-server"}},
        ),
        mcp_manager=None,
        registry=registry,
    )
    register_mcp_tools(app)
    authority = current_mcp_install_allowed.set(True)
    call_id = current_tool_call_id.set("remove-no-config")
    try:
        decision = registry.get("mcp_remove").permission_resolver({"name": "echo"})
    finally:
        current_tool_call_id.reset(call_id)
        current_mcp_install_allowed.reset(authority)
    assert decision.behavior == "deny"
    assert "配置文件" in decision.reason
