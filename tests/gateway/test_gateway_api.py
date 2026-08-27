"""Gateway 新增 REST 端点测试。"""

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.interfaces import LLMProvider
from crew.core.types import ChatResponse, Message, StreamChunk
from crew.gateway.server import create_app
from crew.state.config import Config, ModelProfile
from crew.team.roles import CREW_BUILTIN_AGENT_ID

OWNER_A = "A:uid-a"


class NearbyTestProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None) -> ChatResponse:
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        return ChatResponse(text=f"已回复：{last_user}", finish_reason="stop")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        response = await self.chat(messages, tools, max_tokens=max_tokens)
        yield StreamChunk(delta_text=response.text)
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class NearbyCaptureProvider(NearbyTestProvider):
    """记录每次流式调用的消息与工具列表，用于断言 Nearby 会话注入的提示内容。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        async for chunk in super().stream_chat(messages, tools=tools, max_tokens=max_tokens):
            yield chunk


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    return create_app(crew)


def test_gateway_wires_interaction_bridge_to_team_manager(tmp_path):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)

    create_app(crew)

    assert crew.interaction_bridge is not None
    assert crew.team is not None
    assert crew.team.interaction_bridge is crew.interaction_bridge


@pytest.mark.asyncio
async def test_companion_gateway_rejects_offline_open_and_send(api, auth_headers):
    transport = ASGITransport(app=api)
    peer = {"peer_id": "peer-offline", "display_name": "离线同伴"}
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        await client.post(
            "/api/companion/link-state",
            json={
                "type": "peer",
                "peer_id": peer["peer_id"],
                "profile": peer,
                "connection_state": "disconnected",
            },
        )
        offline_open = await client.post(
            "/api/companion/conversations/open",
            json={"kind": "nearby_dm", "target_id": peer["peer_id"], "title": "离线同伴"},
        )
        await client.post(
            "/api/companion/link-state",
            json={
                "type": "peer",
                "peer_id": peer["peer_id"],
                "profile": peer,
                "connection_state": "connected",
            },
        )
        opened = await client.post(
            "/api/companion/conversations/open",
            json={"kind": "nearby_dm", "target_id": peer["peer_id"], "title": "离线同伴"},
        )
        await client.post(
            "/api/companion/link-state",
            json={
                "type": "peer",
                "peer_id": peer["peer_id"],
                "profile": peer,
                "connection_state": "disconnected",
            },
        )
        offline_send = await client.post(
            f"/api/companion/conversations/{opened.json()['session_id']}/messages",
            json={"text": "不能发送"},
        )
        outbox = await client.get("/api/companion/outbox")

    assert offline_open.status_code == 400
    assert "暂时离线" in offline_open.json()["error"]
    assert opened.status_code == 200
    assert offline_send.status_code == 400
    assert "暂时离线" in offline_send.json()["error"]
    assert outbox.json()["events"] == []


@pytest.mark.asyncio
async def test_companion_incoming_message_is_idempotent_and_restored_from_main_history(
    api, auth_headers
):
    transport = ASGITransport(app=api)
    incoming = {
        "type": "message",
        "kind": "nearby_dm",
        "target_id": "peer-history",
        "conversation_title": "林墨",
        "message_id": "remote-message-1",
        "sender_id": "peer-history",
        "sender_name": "林墨",
        "sender_kind": "human",
        "text": "双向消息已收到",
    }
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=auth_headers
    ) as client:
        first = await client.post("/api/companion/link-state", json=incoming)
        duplicate = await client.post("/api/companion/link-state", json=incoming)
        sessions = await client.get("/api/sessions", params={"workspace_id": "companion"})
        session_id = first.json()["binding"]["session_id"]
        history = await client.get(f"/api/session/{session_id}")

    assert first.status_code == 200
    assert first.json()["appended"] is True
    assert duplicate.json()["appended"] is False
    assert any(item["session_id"] == session_id for item in sessions.json())
    assert history.json() == [{
        "role": "user",
        "content": "双向消息已收到",
        "name": "林墨",
        "message_id": "remote-message-1",
        "origin": {
            "source": "companion",
            "sender_kind": "human",
            "sender_id": "peer-history",
            "sender_name": "林墨",
            "is_self": False,
            "delivery_state": "delivered",
        },
        "source_session_id": session_id,
    }]


@pytest.mark.asyncio
async def test_api_usage(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_tokens" in data
    assert "session_count" in data


@pytest.mark.asyncio
async def test_api_session_context(api, auth_headers):
    transport = ASGITransport(app=api)
    # create_app fixture builds a real crew under app routes; use the endpoint
    # setup path that creates an owner-scoped session for the default test auth.
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        await client.put(
            "/api/session/test-session/agent-config",
            json={"executor": "builtin", "title": "ctx"},
        )
        resp = await client.get("/api/session/test-session/context")
    assert resp.status_code == 200
    data = resp.json()
    assert "used_tokens" in data
    assert "max_tokens" in data
    assert "ratio" in data


@pytest.mark.asyncio
async def test_nearby_agent_turn_rejects_fake_provider(tmp_path):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "peer_name": "Windows 工作站",
                "request_id": "request-1",
                "query": "你好",
                "room_id": "room-fake-provider",
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "model_not_configured"
    assert "设置 → 模型" in response.json()["error"]


@pytest.mark.asyncio
async def test_nearby_agent_turn_rejects_dm_and_isolates_rooms(tmp_path):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.provider = NearbyTestProvider()
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "peer_name": "Windows 工作站",
                "request_id": "request-1",
                "query": "你好",
                "room_id": "room-project-a",
            },
        )
        second = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "peer_name": "Windows 工作站",
                "request_id": "request-2",
                "query": "还记得上一条消息吗？",
                "room_id": "room-project-a",
            },
        )
        other = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_other",
                "peer_name": "另一台电脑",
                "request_id": "request-3",
                "query": "你好",
                "room_id": "room-project-b",
            },
        )
        visible_sessions = await client.get("/api/sessions")
        rejected_dm = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "request_id": "request-dm",
                "query": "私聊 Agent",
            },
        )

    assert first.status_code == 200
    assert first.json()["text"] == "已回复：你好"
    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]
    assert other.status_code == 200
    assert other.json()["session_id"] != first.json()["session_id"]
    assert rejected_dm.status_code == 403
    assert rejected_dm.json()["code"] == "agent_dm_forbidden"
    visible = visible_sessions.json()
    assert {item["session_id"] for item in visible} == {
        first.json()["session_id"],
        other.json()["session_id"],
    }
    assert all(item["workspace_id"] == "companion" for item in visible)

    config = crew.session_store.get_agent_config(
        first.json()["session_id"], owner_account_id="local"
    )
    assert config["disabled_toolsets"] == ["*"]
    assert config["disabled_skills"] == ["*"]
    assert config["nearby_text_only"] is True
    history = crew.session_store.load(first.json()["session_id"], owner_account_id="local")
    assert [message.content for message in history if message.role == "user"] == [
        "你好",
        "还记得上一条消息吗？",
    ]


@pytest.mark.asyncio
async def test_nearby_agent_turn_room_session_isolated_and_history_truncated(tmp_path):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    provider = NearbyCaptureProvider()
    crew.provider = provider
    app = create_app(crew)
    transport = ASGITransport(app=app)
    history = [
        {"sender": f"成员{i:02d}", "text": f"消息{i:02d}"} for i in range(25)
    ] + [{"sender": "甲", "text": "长" * 600}]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        room = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "peer_name": "Windows 工作站",
                "request_id": "request-2",
                "query": "@小助手 总结一下",
                "room_id": "room-1",
                "room_name": "项目群",
                "history": history,
            },
        )
        other_room = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "ace_windows",
                "peer_name": "Windows 工作站",
                "request_id": "request-3",
                "query": "在吗",
                "room_id": "room-2",
            },
        )

    assert room.status_code == 200
    assert other_room.status_code == 200
    assert room.json()["session_id"].startswith("agent:main:nearby:room:")
    # Agent 只能在群里运行，不同群之间相互隔离。
    assert other_room.json()["session_id"] != room.json()["session_id"]

    # 群聊会话沿用同样的强制安全配置
    config = crew.session_store.get_agent_config(
        room.json()["session_id"], owner_account_id="local"
    )
    assert config["executor"] == "builtin"
    assert config["disabled_toolsets"] == ["*"]
    assert config["disabled_skills"] == ["*"]
    assert config["nearby_text_only"] is True

    hint = next(
        str(message.content)
        for call in provider.calls
        for message in call["messages"]
        if message.role == "user" and "项目群" in str(message.content)
    )
    assert "只需回应 @ 你或直接问你的内容" in hint
    # 最近消息只取最后 20 条：消息06 保留、消息05 被丢弃
    assert "消息06" in hint
    assert "消息05" not in hint
    # 单条消息截断到 500 字符
    assert "长" * 500 in hint
    assert "长" * 501 not in hint

@pytest.mark.asyncio
async def test_nearby_agent_turn_allowed_toolsets(tmp_path, monkeypatch):
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        cron_enabled=False,
        active_model_id="m1",
        default_model_id="m1",
        model_profiles={
            "m1": ModelProfile(id="m1", name="M1", api_key="test-key", model="m1-model"),
        },
    )
    crew = build_app(config=cfg, enable_team=False)
    provider = NearbyCaptureProvider()
    crew.provider = provider
    # 用捕获型 Provider 替换按 profile 创建的客户端，避免真实网络调用；
    # 模型能力仍来自 profile（默认含 tools），保证工具过滤链路真实生效。
    monkeypatch.setattr(
        "crew.app.build_provider_for_profile",
        lambda *_args, **_kwargs: provider,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    all_toolsets = set(crew.registry.toolsets())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "peer_default",
                "request_id": "request-1",
                "query": "你好",
                "room_id": "room-default",
            },
        )
        empty = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "peer_empty",
                "request_id": "request-2",
                "query": "你好",
                "room_id": "room-empty",
                "allowed_toolsets": [],
            },
        )
        allowed = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "peer_web",
                "request_id": "request-3",
                "query": "帮我查一下今天的天气",
                "room_id": "room-web",
                "allowed_toolsets": ["web"],
            },
        )
        mixed = await client.post(
            "/api/nearby/agent-turn",
            json={
                "peer_id": "peer_mixed",
                "request_id": "request-4",
                "query": "帮我查一下今天的天气",
                "room_id": "room-mixed",
                "allowed_toolsets": ["web", "bogus", "*", 42],
            },
        )

    for resp in (default, empty, allowed, mixed):
        assert resp.status_code == 200

    def stored(resp):
        return crew.session_store.get_agent_config(
            resp.json()["session_id"], owner_account_id="local"
        )

    # 缺省与空数组：维持全禁，行为与之前完全一致
    assert stored(default)["disabled_toolsets"] == ["*"]
    assert stored(empty)["disabled_toolsets"] == ["*"]
    # 白名单生效：全集中减去白名单
    expected_disabled = sorted(all_toolsets - {"web"})
    assert stored(allowed)["disabled_toolsets"] == expected_disabled
    # 非法条目（不存在的 toolset、"*" 通配、非字符串）被忽略，不会放大权限
    assert stored(mixed)["disabled_toolsets"] == expected_disabled

    # 运行时工具范围：全禁会话没有任何工具；白名单会话只剩 web 工具集
    default_agent = crew.agents.get(
        default.json()["session_id"], stored(default), owner_account_id="local"
    )
    assert default_agent.tool_filter == []
    allowed_agent = crew.agents.get(
        allowed.json()["session_id"], stored(allowed), owner_account_id="local"
    )
    assert set(allowed_agent.tool_filter) == set(crew.registry.names_for_toolset("web"))


@pytest.mark.asyncio
async def test_nearby_agent_turn_rejects_invalid_room_and_whitelist_payloads(tmp_path):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.provider = NearbyTestProvider()
    app = create_app(crew)
    transport = ASGITransport(app=app)
    base = {
        "peer_id": "ace_windows",
        "request_id": "request-1",
        "query": "你好",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad_room = await client.post(
            "/api/nearby/agent-turn",
            json={**base, "room_id": "room/../../etc"},
        )
        bad_history = await client.post(
            "/api/nearby/agent-turn",
            json={**base, "room_id": "room-1", "history": "not-a-list"},
        )
        bad_history_item = await client.post(
            "/api/nearby/agent-turn",
            json={**base, "room_id": "room-1", "history": ["oops"]},
        )
        bad_allowed = await client.post(
            "/api/nearby/agent-turn",
            json={**base, "room_id": "room-1", "allowed_toolsets": "web"},
        )

    assert bad_room.status_code == 400
    assert bad_history.status_code == 400
    assert bad_history_item.status_code == 400
    assert bad_allowed.status_code == 400


@pytest.mark.asyncio
async def test_external_session_model_switch_requires_idle_and_runtime_catalog(tmp_path, auth_headers, monkeypatch):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    runtime = crew.external_agents.upsert_runtime({
        "id": "codex-model-switch",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "/bin/codex",
        "version": "test",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "gpt-default",
            "runtime_capabilities": {"model_switch": True},
            "models": [
                {"id": "gpt-default", "label": "GPT Default", "default": True},
                {"id": "gpt-alt", "label": "GPT Alt"},
            ],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Codex Agent",
        runtime_id=runtime["id"],
        model="gpt-default",
    )
    crew.session_store.ensure_session("external-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "external-model",
        {
            "executor": "acp",
            "external_agent_id": agent["id"],
            "acp": {"external_agent_id": agent["id"]},
        },
        owner_account_id=OWNER_A,
    )
    crew.session_store.save(
        "external-model",
        [Message.user("hello"), Message.assistant("answer", model="gpt-default")],
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        current = await client.get("/api/session/external-model/model")
        history = await client.get("/api/session/external-model")
        switched = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "gpt-alt"},
        )
        reloaded = await client.get("/api/session/external-model/model")
        invalid = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "unknown"},
        )
        monkeypatch.setattr(
            crew.dispatcher,
            "status",
            lambda *_args, **_kwargs: {"live": "running", "active_request_id": "req-1"},
        )
        busy = await client.put(
            "/api/session/external-model/model",
            json={"model_profile_id": "gpt-default"},
        )

    assert current.status_code == 200
    assert current.json()["source"] == "external"
    assert current.json()["model_profile_id"] == "gpt-default"
    assert current.json()["model_switchable"] is True
    assert [item["id"] for item in current.json()["models"]] == ["gpt-default", "gpt-alt"]
    assert history.json()[-1]["model"] == "gpt-default"
    assert switched.status_code == 200
    assert switched.json()["model_profile_id"] == "gpt-alt"
    assert reloaded.status_code == 200
    assert reloaded.json()["model_profile_id"] == "gpt-alt"
    assert invalid.status_code == 400
    assert busy.status_code == 409
    stored = crew.session_store.get_agent_config("external-model", owner_account_id=OWNER_A)
    assert stored["executor"] == "external"
    assert stored["external"]["model"] == "gpt-alt"

@pytest.mark.asyncio
async def test_external_session_model_resolves_adapter_declared_legacy_id(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    old_runtime = crew.external_agents.upsert_runtime({
        "id": "claude-session-model-migration",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": "/bin/claude",
        "version": "old",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "default",
            "models": [{"id": "default", "label": "CLI 默认模型", "default": True}],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Claude Agent",
        runtime_id=old_runtime["id"],
        model="default",
    )
    crew.external_agents.upsert_runtime({
        **old_runtime,
        "version": "new",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "sonnet",
            "models": [
                {"id": "sonnet", "label": "Claude Sonnet（当前）", "default": True},
                {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
            ],
            "model_migrations": {"default": "sonnet"},
        },
    })
    crew.session_store.ensure_session("claude-legacy-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "claude-legacy-model",
        {
            "executor": "external",
            "external_agent_id": agent["id"],
            "external": {
                "external_agent_id": agent["id"],
                "model": "default",
            },
        },
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        current = await client.get("/api/session/claude-legacy-model/model")

    assert current.status_code == 200
    assert current.json()["model_profile_id"] == "sonnet"
    assert current.json()["model_label"] == "Claude Sonnet（当前）"
    assert crew.external_agents.get_agent(
        agent["id"],
        owner_account_id=OWNER_A,
    )["model"] == "sonnet"


@pytest.mark.asyncio
async def test_external_session_model_switch_requires_explicit_runtime_capability(tmp_path, auth_headers):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    runtime = crew.external_agents.upsert_runtime({
        "id": "protocol-is-not-capability",
        "provider": "custom",
        "name": "Custom CLI",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "model-a",
            "models": [
                {"id": "model-a", "label": "Model A", "default": True},
                {"id": "model-b", "label": "Model B"},
            ],
        },
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Custom Agent",
        runtime_id=runtime["id"],
        model="model-a",
    )
    crew.session_store.ensure_session("explicit-capability", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "explicit-capability",
        {
            "executor": "external",
            "external": {"external_agent_id": agent["id"]},
        },
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        current = await client.get("/api/session/explicit-capability/model")
        switched = await client.put(
            "/api/session/explicit-capability/model",
            json={"model_profile_id": "model-b"},
        )

    assert current.status_code == 200
    assert current.json()["model_switchable"] is False
    assert switched.status_code == 409


@pytest.mark.asyncio
async def test_team_session_model_materializes_and_switches_one_member(tmp_path, auth_headers, monkeypatch):
    db_path = tmp_path / "crew.db"
    crew = build_app(config=Config(db_path=str(db_path), cron_enabled=False), enable_team=True)
    runtime = crew.external_agents.upsert_runtime({
        "id": "team-model-runtime",
        "provider": "custom",
        "name": "Team Model Runtime",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "model-b",
            "runtime_capabilities": {"model_switch": True, "session_resume": True},
            "models": [
                {"id": "model-a", "label": "Model A", "capabilities": ["text", "tools"]},
                {"id": "model-b", "label": "Model B", "default": True, "capabilities": ["text"]},
            ],
        },
    })
    leader = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Team Leader",
        runtime_id=runtime["id"],
        model="model-b",
    )
    member = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Team Member",
        runtime_id=runtime["id"],
        model="model-b",
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Model Team",
        leader_agent_id=leader["id"],
        members=[
            {"agent_id": leader["id"], "role": "Leader"},
            {"agent_id": member["id"], "role": "Member"},
        ],
    )
    crew.session_store.ensure_session("team-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-model",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    crew.session_store.ensure_session("team-model-other", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-model-other",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        initial = await client.get("/api/session/team-model/model")
        forged = await client.put(
            "/api/session/team-model/agent-config",
            json={
                "executor": "team",
                "team": {
                    "external_team_id": team["id"],
                    "member_model_bindings": {
                        leader["id"]: {"model_id": "model-a", "revision": 99},
                    },
                    "model_binding_revision": 99,
                },
            },
        )
        switched = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-a",
                "expected_revision": 1,
            },
        )
        selected = await client.get(
            f"/api/session/team-model/model?member_id={leader['id']}"
        )
        other_session = await client.get(
            f"/api/session/team-model-other/model?member_id={leader['id']}"
        )
        stale = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-b",
                "expected_revision": 1,
            },
        )
        restored = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": leader["id"],
                "model_profile_id": "model-b",
                "expected_revision": 2,
                "restore_default": True,
            },
        )
        # Dispatcher 表示 Team 当前仍有一轮在执行；此时只要目标成员空闲，
        # 仍可更新它下一轮任务的模型。旧的 session-level busy guard 会误拒绝。
        monkeypatch.setattr(
            crew.dispatcher,
            "status",
            lambda *_args, **_kwargs: {"live": "running"},
        )
        member_switched_while_leader_busy = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": member["id"],
                "model_profile_id": "model-a",
                "expected_revision": 3,
            },
        )
        crew.team._mark_child_active({
            "child_id": "task-model::member",
            "parent_session_id": "team-model",
            "owner_account_id": OWNER_A,
            "member": member["name"],
            "agent": object(),
        })
        busy_member = await client.get(
            f"/api/session/team-model/model?member_id={member['id']}"
        )
        blocked_member_switch = await client.put(
            "/api/session/team-model/model",
            json={
                "member_id": member["id"],
                "model_profile_id": "model-b",
                "expected_revision": 4,
            },
        )
        crew.team._mark_child_done("team-model", "task-model::member", OWNER_A)

    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["scope"] == "team"
    assert initial_body["model_binding_revision"] == 1
    assert {item["member_id"] for item in initial_body["members"]} == {leader["id"], member["id"]}
    assert {item["model_profile_id"] for item in initial_body["members"]} == {"model-b"}
    assert all(item["model_switchable"] for item in initial_body["members"])
    assert forged.status_code == 200
    assert forged.json()["team"]["model_binding_revision"] == 1
    assert forged.json()["team"]["member_model_bindings"][leader["id"]]["model_id"] == "model-b"

    assert switched.status_code == 200
    assert switched.json()["scope"] == "team_member"
    assert switched.json()["model_profile_id"] == "model-a"
    assert switched.json()["model_binding_revision"] == 2
    assert selected.json()["model_profile_id"] == "model-a"
    assert other_session.json()["model_profile_id"] == "model-b"
    assert stale.status_code == 409
    assert stale.json()["code"] == "model_binding_stale"
    assert restored.status_code == 200
    assert restored.json()["model_profile_id"] == "model-b"
    assert restored.json()["binding_source"] == "restored_from_agent_default"
    assert restored.json()["model_binding_revision"] == 3
    assert crew.external_agents.get_agent(leader["id"], owner_account_id=OWNER_A)["model"] == "model-b"
    assert member_switched_while_leader_busy.status_code == 200
    assert member_switched_while_leader_busy.json()["model_binding_revision"] == 4
    assert busy_member.status_code == 200
    assert busy_member.json()["status"] == "running"
    assert busy_member.json()["active_task_count"] == 1
    assert blocked_member_switch.status_code == 409
    assert blocked_member_switch.json()["code"] == "member_busy"

    stored = crew.session_store.get_agent_config("team-model", owner_account_id=OWNER_A)
    assert set(stored["team"]["member_model_bindings"]) == {leader["id"], member["id"]}
    assert stored["team"]["member_model_bindings"][member["id"]]["model_id"] == "model-a"
    with sqlite3.connect(db_path) as conn:
        envelope = json.loads(conn.execute(
            "SELECT profile_json FROM external_agent WHERE id = ?",
            (leader["id"],),
        ).fetchone()[0])
    assert set(envelope["model_overlays"]) == {"model-a", "model-b"}

    rebuilt = crew.team._build_team(
        "team-model",
        external_team_id=team["id"],
        owner_account_id=OWNER_A,
    )
    assert rebuilt.leader.executor.config.model == "model-b"


@pytest.mark.asyncio
async def test_team_builtin_member_model_switch_uses_bound_profile(tmp_path, auth_headers, monkeypatch):
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        cron_enabled=False,
        active_model_id="builtin-b",
        default_model_id="builtin-b",
        model_profiles={
            "builtin-a": ModelProfile(
                id="builtin-a",
                name="Builtin A",
                api_key="test-a-key",
                model="builtin-a-model",
                builtin=True,
            ),
            "builtin-b": ModelProfile(
                id="builtin-b",
                name="Builtin B",
                api_key="test-b-key",
                model="builtin-b-model",
                builtin=True,
            ),
        },
    )
    crew = build_app(config=cfg, enable_team=True)
    # Gateway owner 的模型目录来自登录 overlay；测试中直接提供可用 profile，
    # 以验证 Team 内置成员的绑定不会退回全局默认 Provider。
    profiles = dict(cfg.model_profiles)
    monkeypatch.setattr(crew, "owner_model_profiles", lambda _owner: profiles)
    monkeypatch.setattr(crew.config, "owner_default_model_id", lambda _owner: "builtin-b")
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Builtin Model Team",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[{"agent_id": CREW_BUILTIN_AGENT_ID, "role": "Leader"}],
    )
    crew.session_store.ensure_session("team-builtin-model", owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(
        "team-builtin-model",
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        initial = await client.get("/api/session/team-builtin-model/model")
        switched = await client.put(
            "/api/session/team-builtin-model/model",
            json={
                "member_id": CREW_BUILTIN_AGENT_ID,
                "model_profile_id": "builtin-a",
                "expected_revision": 1,
            },
        )

    assert initial.status_code == 200
    initial_member = initial.json()["members"][0]
    assert initial_member["member_id"] == CREW_BUILTIN_AGENT_ID
    assert initial_member["model_profile_id"] == "builtin-b"
    assert initial_member["model_switchable"] is True
    assert switched.status_code == 200
    assert switched.json()["model_profile_id"] == "builtin-a"
    assert switched.json()["runtime_id"] == "builtin"

    rebuilt = crew.team._build_team(
        "team-builtin-model",
        external_team_id=team["id"],
        owner_account_id=OWNER_A,
    )
    assert rebuilt.leader.executor.provider.model == "builtin-a-model"


@pytest.mark.asyncio
async def test_internal_interaction_api_rejects_missing_or_invalid_token(api):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        missing = await client.post(
            "/api/internal/interactions/ask",
            json={"questions": [{"question": "?", "options": ["A"]}]},
        )
        invalid = await client.post(
            "/api/internal/interactions/ask",
            headers={"Authorization": "Bearer invalid"},
            json={"questions": [{"question": "?", "options": ["A"]}]},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_internal_interaction_api_returns_followup_answer(tmp_path, monkeypatch):
    from crew.core.followup import resolve_answer
    from crew.gateway.interaction_bridge import interaction_bridge

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.active_owner.claim(OWNER_A)
    app = create_app(crew)

    async def push(session_id, payload, *, owner_account_id):
        assert owner_account_id == OWNER_A
        resolve_answer(
            session_id,
            payload["body"]["question_id"],
            [{"question_id": "q1", "answers": ["B"]}],
        )

    interaction_bridge.configure(
        push_fn=push,
        gateway_url="http://127.0.0.1:8000",
    )
    binding = interaction_bridge.create_binding(
        owner_account_id=OWNER_A,
        display_session_id="main",
        origin_session_id="main::codex",
        agent_name="Codex",
        ttl_seconds=30,
    )
    assert binding is not None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/internal/interactions/ask",
            headers={"Authorization": f"Bearer {binding.token}"},
            json={
                "title": "选择",
                "questions": [{
                    "id": "q1",
                    "question": "选哪个？",
                    "options": ["A", "B"],
                    "multiSelect": False,
                }],
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["answers"] == [
        {"question_id": "q1", "answers": ["B"]},
    ]


@pytest.mark.asyncio
async def test_feishu_challenge(api, auth_headers):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/feishu/events", json={"challenge": "abc123"})
    # feishu channel 未启用时 503；启用时返回 challenge
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert resp.json().get("challenge") == "abc123"


@pytest.mark.asyncio
async def test_cron_run_now_creates_manual_fire_without_changing_schedule(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=True)
    crew = build_app(config=cfg, enable_team=False)
    job = crew.cron_store.create(
        name="run-now",
        schedule="every 1h",
        query="ping",
        session_id="s1",
        owner_account_id=OWNER_A,
    )
    crew.session_store.save("s1", [Message.user("cron")], owner_account_id=OWNER_A)
    crew.cron_store.set_enabled(job["id"], False, owner_account_id=OWNER_A)
    before = crew.cron_store.get(job["id"], owner_account_id=OWNER_A)
    app = create_app(crew)
    await crew.cron_service.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(f"/api/cron/jobs/{job['id']}/run")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["job"]["enabled"] is False
    assert data["job"]["last_status"] == ""
    for _ in range(50):
        await asyncio.sleep(0.01)
        runs = crew.cron_store.get_job_runs(job["id"])
        if runs and runs[0]["status"] != "running":
            break
    refreshed = crew.cron_store.get(job["id"], owner_account_id=OWNER_A)
    assert refreshed["next_run_at"] == before["next_run_at"]
    assert refreshed["enabled"] == before["enabled"]
    assert len(runs) == 1
    assert runs[0]["fire_kind"] == "manual"
    await crew.shutdown()


@pytest.mark.asyncio
async def test_external_session_binding_is_owner_validated_and_projected(
    tmp_path,
    auth_headers,
):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    runtime = crew.external_agents.upsert_runtime({
        "id": "runtime-session-binding",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "/bin/codex",
        "protocol": "cli",
        "metadata": {"availability_status": "ready"},
    })
    agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="Codex 外援",
        runtime_id=runtime["id"],
        model="gpt-test",
    )
    other_owner_agent = crew.external_agents.create_agent(
        owner_account_id="B:uid-b",
        name="其他账号外援",
        runtime_id=runtime["id"],
        model="gpt-test",
    )
    degraded_runtime = crew.external_agents.upsert_runtime({
        "id": "runtime-session-binding-degraded",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": "/bin/claude",
        "protocol": "cli",
        "metadata": {"availability_status": "degraded"},
    })
    degraded_agent = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="不可用外援",
        runtime_id=degraded_runtime["id"],
        model="claude-test",
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="研发外援团",
        leader_agent_id=agent["id"],
        members=[
            {
                "agent_id": agent["id"],
                "role": "Leader",
                "role_key": "tech_lead",
                "role_label": "技术负责人",
            },
        ],
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        missing = await client.put(
            "/api/session/missing-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": "missing-agent"},
            },
        )
        cross_owner = await client.put(
            "/api/session/cross-owner-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": other_owner_agent["id"]},
            },
        )
        degraded = await client.put(
            "/api/session/degraded-external/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": degraded_agent["id"]},
            },
        )
        agent_bound = await client.put(
            "/api/session/agent-bound/agent-config",
            json={
                "executor": "external",
                "external": {"external_agent_id": agent["id"]},
            },
        )
        team_bound = await client.put(
            "/api/session/team-bound/agent-config",
            json={
                "executor": "team",
                "team": {"external_team_id": team["id"]},
            },
        )
        listed = await client.get("/api/sessions")

    assert missing.status_code == 404
    assert cross_owner.status_code == 404
    assert degraded.status_code == 409
    assert agent_bound.status_code == 200
    assert team_bound.status_code == 200
    rows = {row["session_id"]: row for row in listed.json()}
    assert "missing-external" not in rows
    assert "cross-owner-external" not in rows
    assert "degraded-external" not in rows
    assert rows["agent-bound"]["agent_binding"] == {
        "kind": "external_agent",
        "id": agent["id"],
    }
    assert rows["team-bound"]["agent_binding"] == {
        "kind": "external_team",
        "id": team["id"],
    }


@pytest.mark.asyncio
async def test_cron_retry_creates_linked_fire_without_replaying_source(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=True)
    crew = build_app(config=cfg, enable_team=False)
    job = crew.cron_store.create(
        name="retry",
        schedule="every 1h",
        query="ping",
        session_id="s-retry",
        owner_account_id=OWNER_A,
    )
    crew.session_store.save("s-retry", [Message.user("cron")], owner_account_id=OWNER_A)
    source = crew.cron_store.claim_manual_fire(job["id"], owner_account_id=OWNER_A)
    assert source is not None
    assert crew.cron_store.finish_job_run(source["id"], "failed", "boom") is True
    app = create_app(crew)
    await crew.cron_service.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(f"/api/cron/fires/{source['id']}/retry")

    assert resp.status_code == 202
    assert resp.json()["source_fire_id"] == source["id"]
    for _ in range(50):
        await asyncio.sleep(0.01)
        runs = crew.cron_store.get_job_runs(job["id"])
        if len(runs) == 2 and runs[0]["status"] != "running":
            break
    assert len(runs) == 2
    assert runs[0]["fire_kind"] == "retry"
    assert runs[0]["retry_of_fire_id"] == source["id"]
    assert runs[0]["status"] == "completed"
    assert runs[1]["id"] == source["id"]
    assert runs[1]["status"] == "failed"
    await crew.shutdown()


@pytest.mark.asyncio
async def test_unified_task_api_list_wait_cancel(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=False)
    crew.session_store.save("s1", [Message.user("task")], owner_account_id=OWNER_A)
    task = crew.tasks.create_runtime(
        kind="team",
        session_id="s1",
        title="api task",
        owner_account_id=OWNER_A,
    )
    crew.tasks.mark_running(task["task_id"])
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": "s1"})
        assert listed.status_code == 200
        assert listed.json()[0]["task_id"] == task["task_id"]

        waited = await client.post(
            f"/api/tasks/{task['task_id']}/wait",
            json={"timeout": 0.01},
        )
        assert waited.status_code == 200
        assert waited.json()["retrieval_status"] == "timeout"

        cancelled = await client.post(
            f"/api/tasks/{task['task_id']}/cancel",
            json={"reason": "test"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_team_task_api_ignores_legacy_turn_child_tasks_without_workflow(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False, gateway_dev_mode=False)
    crew = build_app(config=cfg, enable_team=False)
    parent = "web_team_parent"
    child = f"{parent}::turn::req_abc"
    crew.session_store.save(parent, [Message.user("开发一个贪吃蛇游戏")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    parent_task = crew.tasks.create_runtime(
        kind="agent_turn", session_id=parent, title="开发一个贪吃蛇游戏", owner_account_id=OWNER_A
    )
    team_task = crew.tasks.create_runtime(
        kind="team", session_id=child, title="实现：贪吃蛇小游戏", owner_account_id=OWNER_A
    )
    crew.tasks.finish(
        parent_task["task_id"],
        owner_account_id=OWNER_A,
        result="团队工作流完成",
    )
    crew.tasks.finish(
        team_task["task_id"],
        owner_account_id=OWNER_A,
        result="成员完成开发",
    )
    other_task = crew.tasks.create_runtime(
        kind="team", session_id="other", title="不应出现", owner_account_id=OWNER_A
    )
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})
        history = await client.get(f"/api/session/{parent}")

    assert listed.status_code == 200
    titles = [item["title"] for item in listed.json()]
    assert titles == ["开发一个贪吃蛇游戏"]
    ids = [item["task_id"] for item in listed.json()]
    assert parent_task["task_id"] in ids
    assert team_task["task_id"] not in ids
    assert other_task["task_id"] not in ids
    assert history.status_code == 200
    assert any(item["role"] == "assistant" and "团队工作流完成" in item["content"] for item in history.json())
    assert any("成员完成开发" in item["content"] for item in history.json())


@pytest.mark.asyncio
async def test_team_task_api_projects_team_plan_nodes(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False, gateway_dev_mode=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_plan_parent"
    crew.session_store.save(parent, [Message.user("写一个像素游戏")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        parent,
        goal="写一个像素游戏",
        nodes=[
            {"id": "leader_plan", "title": "Leader 承接任务", "assignee": "leader"},
            {
                "id": "dev",
                "title": "开发像素游戏",
                "assignee": "coder",
                "metadata": {
                    "workflow_lane": "build",
                    "display_order": 41,
                    "role_label": "自定义开发主责",
                },
            },
            {"id": "verify", "title": "验证像素游戏", "assignee": "researcher"},
            {"id": "leader_summary", "title": "Leader 汇总结论", "assignee": "leader"},
        ],
        edges=[
            ["leader_plan", "dev"],
            ["dev", "verify"],
            ["verify", "leader_summary"],
        ],
        owner_account_id=OWNER_A,
    )
    delegate_task = crew.tasks.create_runtime(
        kind="team",
        session_id=parent,
        title="开发像素游戏",
        owner_account_id=OWNER_A,
    )
    crew.tasks.touch_activity(delegate_task["task_id"], {"plan_node_id": "dev"})
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    assert [row["title"] for row in rows] == [
        "Leader 承接任务",
        "开发像素游戏",
        "验证像素游戏",
        "Leader 汇总结论",
    ]
    assert delegate_task["task_id"] not in [row["task_id"] for row in rows]
    assert [row["progress"]["source"] for row in rows] == ["team_kanban"] * 4
    by_task_id = {row["task_id"]: row for row in rows}
    by_plan_node_id = {row["progress"]["plan_node_id"]: row for row in rows}
    assert [row["progress"]["workflow_lane"] for row in rows] == ["lead", "build", "verify", "summary"]
    assert [row["progress"]["display_order"] for row in rows] == [10, 41, 50, 80]
    assert by_plan_node_id["dev"]["progress"]["role_label"] == "自定义开发主责"

    def dependency_plan_ids(row, key: str) -> list[str]:
        return [
            by_task_id[task_id]["progress"]["plan_node_id"]
            for task_id in row["progress"][key]
        ]

    assert dependency_plan_ids(by_plan_node_id["leader_plan"], "child_node_ids") == ["dev"]
    assert dependency_plan_ids(by_plan_node_id["dev"], "parent_node_ids") == ["leader_plan"]
    assert dependency_plan_ids(by_plan_node_id["verify"], "parent_node_ids") == ["dev"]
    assert dependency_plan_ids(by_plan_node_id["leader_summary"], "parent_node_ids") == ["verify"]
    assert {row["progress"]["turn_session_id"] for row in rows} == {parent}
    assert {row["progress"]["turn_title"] for row in rows} == {"写一个像素游戏"}


@pytest.mark.asyncio
async def test_team_task_api_keeps_direct_turn_separate_and_hides_shell(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_mixed_parent"
    turn = f"{parent}::turn::req_test"
    crew.session_store.save(parent, [Message.user("你好"), Message.user("测试")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        turn,
        goal="测试",
        nodes=[
            {"id": "leader_plan", "title": "Leader 承接任务", "assignee": "leader"},
            {"id": "test_plan", "title": "测试设计：测试", "assignee": "kk"},
        ],
        edges=[["leader_plan", "test_plan"]],
        owner_account_id=OWNER_A,
    )
    direct = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你好",
        detail="你好",
        owner_account_id=OWNER_A,
    )
    team_wrapper = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="测试",
        detail="测试",
        owner_account_id=OWNER_A,
    )
    delegate = crew.tasks.create_runtime(
        kind="team",
        session_id=turn,
        title="测试设计：测试",
        detail="测试设计：测试",
        owner_account_id=OWNER_A,
    )
    shell = crew.tasks.create_runtime(
        kind="shell",
        session_id=turn,
        title="npm test",
        detail="npm test",
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(direct["task_id"], owner_account_id=OWNER_A, result="你好！")
    crew.tasks.update_status(team_wrapper["task_id"], "cancelled", "已停止当前回复")
    crew.tasks.touch_activity(delegate["task_id"], {"plan_node_id": "test_plan"})
    crew.tasks.finish(shell["task_id"], owner_account_id=OWNER_A, result="shell output")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    ids = {row["task_id"] for row in rows}
    assert direct["task_id"] in ids
    assert team_wrapper["task_id"] not in ids
    assert shell["task_id"] not in ids
    assert {row["progress"]["turn_title"] for row in rows} == {"你好", "测试"}
    assert len({row["progress"]["turn_session_id"] for row in rows}) == 2


@pytest.mark.asyncio
async def test_team_task_api_hides_parent_turn_when_plan_has_no_delegate_tasks(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_leader_only_parent"
    request_id = "req_question"
    turn = f"{parent}::turn::{request_id}"
    prompt = "刚刚项目从头到尾做完用了多长时间"
    crew.session_store.save(parent, [Message.user(prompt)], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.team.create_plan(
        turn,
        goal=prompt,
        nodes=[
            {"id": "leader_plan", "title": f"Leader 拆分任务：{prompt}", "assignee": "leader"},
            {"id": "leader_summary", "title": f"Leader 汇总：{prompt}", "assignee": "leader"},
        ],
        edges=[["leader_plan", "leader_summary"]],
        owner_account_id=OWNER_A,
    )
    parent_task = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        request_id=request_id,
        title=prompt,
        detail=prompt,
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(parent_task["task_id"], owner_account_id=OWNER_A, result="完成")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    rows = listed.json()
    assert parent_task["task_id"] not in {row["task_id"] for row in rows}
    assert [row["title"] for row in rows] == [
        f"Leader 拆分任务：{prompt}",
        f"Leader 汇总：{prompt}",
    ]


@pytest.mark.asyncio
async def test_team_session_history_uses_visible_parent_turns_only(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_visible_history"
    turn = f"{parent}::turn::req_test"
    runtime = crew.external_agents.upsert_runtime({
        "id": "rt-team-history",
        "provider": "test",
        "name": "Test Runtime",
        "executable_path": "/bin/test",
    })
    hh = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="hh",
        runtime_id=runtime["id"],
    )
    kk = crew.external_agents.create_agent(
        owner_account_id=OWNER_A,
        name="kk",
        runtime_id=runtime["id"],
    )
    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="测试团队",
        leader_agent_id=hh["id"],
        members=[
            {"agent_id": hh["id"], "role": "Leader", "role_key": "tech_lead", "role_label": "leader"},
            {"agent_id": kk["id"], "role": "测试", "role_key": "qa_engineer", "role_label": "测试"},
        ],
    )
    crew.session_store.save(parent, [Message.user("内部恢复消息", is_meta=True)], owner_account_id=OWNER_A)
    crew.session_store.save(
        f"{turn}::leader",
        [Message.user("测试设计：你能帮我测试一下我的贪吃蛇游戏么"), Message.assistant("Leader 内部执行日志")],
        owner_account_id=OWNER_A,
    )
    crew.session_store.save(
        f"{turn}::kk",
        [Message.user("测试执行：你能帮我测试一下我的贪吃蛇游戏么"), Message.assistant("kk 内部执行日志")],
        owner_account_id=OWNER_A,
    )
    crew.session_store.set_agent_config(
        parent,
        {"executor": "team", "team": {"external_team_id": team["id"]}},
        owner_account_id=OWNER_A,
    )
    first = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你好",
        detail="你好",
        owner_account_id=OWNER_A,
    )
    second = crew.tasks.create_runtime(
        kind="agent_turn",
        session_id=parent,
        title="你能帮我测试一下我的贪吃蛇游戏么",
        detail="你能帮我测试一下我的贪吃蛇游戏么",
        owner_account_id=OWNER_A,
    )
    crew.tasks.finish(first["task_id"], owner_account_id=OWNER_A, result="你好！")
    crew.tasks.update_status(second["task_id"], "cancelled", "已停止当前回复")
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        history = await client.get(f"/api/session/{parent}")

    assert history.status_code == 200
    rows = history.json()
    user_messages = [row["content"] for row in rows if row["role"] == "user"]
    internal_messages = [row for row in rows if row["role"] == "team_internal"]
    assert user_messages == ["你好", "你能帮我测试一下我的贪吃蛇游戏么"]
    assert all("测试设计：" not in row["content"] for row in rows)
    assert [row["content"] for row in internal_messages] == ["Leader 内部执行日志", "kk 内部执行日志"]
    assert internal_messages[0]["agent_id"] == hh["id"]
    assert internal_messages[0]["agent_name"] == "hh"
    assert internal_messages[0]["agent_role"] == "leader"
    assert internal_messages[0]["agent_tone"] == 0
    assert internal_messages[0]["is_leader"] is True
    assert internal_messages[1]["agent_id"] == kk["id"]
    assert internal_messages[1]["agent_name"] == "kk"
    assert internal_messages[1]["agent_role"] == "测试"
    assert internal_messages[1]["agent_tone"] == 1
    assert internal_messages[1]["is_leader"] is False


@pytest.mark.asyncio
async def test_team_task_api_does_not_synthesize_legacy_delegate_fallback(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    crew = build_app(config=cfg, enable_team=True)
    parent = "web_team_fallback_title"
    turn = f"{parent}::turn::req_test"
    crew.session_store.save(parent, [Message.user("你能帮我测试一下我的贪吃蛇游戏么")], owner_account_id=OWNER_A)
    crew.session_store.set_agent_config(parent, {"executor": "team"}, owner_account_id=OWNER_A)
    crew.tasks.create_runtime(
        kind="team",
        session_id=turn,
        title="测试设计：你能帮我测试一下我的贪吃蛇游戏么",
        detail="测试设计：你能帮我测试一下我的贪吃蛇游戏么\n\n并行准备测试路径",
        owner_account_id=OWNER_A,
    )
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        listed = await client.get("/api/tasks", params={"session_id": parent})

    assert listed.status_code == 200
    assert listed.json() == []
