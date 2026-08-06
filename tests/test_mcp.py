"""MCP 2 Client（连真 stdio server）+ MCPServer 工具。"""

import asyncio
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
from crew.tools.mcp_client import MCPClientManager
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

async def test_mcp_client_connects_and_calls():
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

        failed = await reg.execute(ToolCall("2", "echo__fail", {"message": "expected failure"}))
        assert failed.is_error
        assert "expected failure" in failed.content
    finally:
        await mgr.aclose()


async def test_mcp_client_streamable_http_connects_and_calls(monkeypatch):
    """HTTP 分支使用 MCP 2 的 streamable_http_client、httpx2 和双元素流。"""
    import httpx2
    from mcp.server import MCPServer

    server = MCPServer("http-echo", version="2.0-test")

    @server.tool()
    def http_echo(text: str) -> str:
        return f"http echo: {text}"

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
    reg = Registry()
    mgr = MCPClientManager({
        "remote": {
            "url": "http://127.0.0.1:8000/mcp",
            "transport": "http",
            "headers": {"X-MCP-Test": "mcp2"},
        }
    })

    async with app.router.lifespan_context(app):
        await mgr.start(reg)
        await mgr.await_started()
        try:
            assert "remote__http_echo" in reg.names()
            assert seen["headers"] == {"X-MCP-Test": "mcp2"}
            assert seen["follow_redirects"] is True
            result = await reg.execute(ToolCall("http-1", "remote__http_echo", {"text": "hi"}))
            assert not result.is_error
            assert "http echo: hi" in result.content
        finally:
            await mgr.aclose()


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
        resolve_answer(sid, question_id, [{"question_id": "q1", "answers": ["B"]}])

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
        resolve_answer(sid, question_id, [{"question_id": "q1", "answers": ["B"]}])

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

    with pytest.raises(Exception, match="owner_account_id"):
        await server.call_tool("sessions_list", {"owner_account_id": ""})


async def test_mcp_server_owner_scopes_history_and_status():
    crew = _crew()
    crew.session_store.save("same", [Message.user("A")], owner_account_id="A:uid-a")
    crew.session_store.save("same", [Message.user("B")], owner_account_id="B:uid-b")
    crew.session_store.set_status("same", "completed", "A done", owner_account_id="A:uid-a")
    crew.session_store.set_status("same", "failed", "B failed", owner_account_id="B:uid-b")
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
