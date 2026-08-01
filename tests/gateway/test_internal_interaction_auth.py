"""External Runtime/MCP 内部回调的短期 binding 认证边界。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crew.gateway.auth_policy import requires_gateway_auth
from crew.gateway.interaction_bridge import InteractionBridge, create_interaction_router
from crew.core.runctx import current_owner_account_id


INTERNAL_BINDING_PATHS = (
    "/api/internal/interactions/ask",
    "/api/internal/team/mention",
    "/api/internal/team/plan/create",
    "/api/internal/team/plan/read",
    "/api/internal/team/plan/update",
)


def test_only_known_binding_routes_bypass_desktop_gateway_auth():
    """短期 binding 路由自认证；其它 internal 路径仍走 Desktop 身份认证。"""
    assert all(not requires_gateway_auth(path) for path in INTERNAL_BINDING_PATHS)
    assert requires_gateway_auth("/api/internal/unknown") is True


def test_interaction_binding_requires_owner():
    """没有 Owner 的 token 不能成为可调用的跨进程凭据。"""
    bridge = InteractionBridge()
    bridge.configure(push_fn=lambda *_: None, gateway_url="http://127.0.0.1:8000")

    assert bridge.create_binding(
        display_session_id="main",
        origin_session_id="main::agent",
        agent_name="Agent",
        ttl_seconds=30,
    ) is None


def test_interaction_binding_inherits_runtime_owner_context():
    """实际 ACP 执行器可从当前 turn 的 Owner ContextVar 建立绑定。"""
    bridge = InteractionBridge()
    bridge.configure(push_fn=lambda *_: None, gateway_url="http://127.0.0.1:8000")
    token = current_owner_account_id.set("A:uid-a")
    try:
        binding = bridge.create_binding(
            display_session_id="main",
            origin_session_id="main::agent",
            agent_name="Agent",
            ttl_seconds=30,
        )
    finally:
        current_owner_account_id.reset(token)

    assert binding is not None
    assert binding.owner_account_id == "A:uid-a"


def test_remove_owner_revokes_only_that_owners_bindings():
    """Logout 撤销 A 时不能误删 B 的独立短期 binding。"""
    bridge = InteractionBridge()
    bridge.configure(push_fn=lambda *_: None, gateway_url="http://127.0.0.1:8000")
    binding_a = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="a",
        origin_session_id="a::agent",
        agent_name="Agent A",
        ttl_seconds=30,
    )
    binding_b = bridge.create_binding(
        owner_account_id="B:uid-b",
        display_session_id="b",
        origin_session_id="b::agent",
        agent_name="Agent B",
        ttl_seconds=30,
    )
    assert binding_a is not None and binding_b is not None

    assert bridge.remove_owner("A:uid-a") == 1
    assert bridge.resolve_binding(binding_a.token) is None
    assert bridge.resolve_binding(binding_b.token) is binding_b


@pytest.mark.asyncio
async def test_binding_route_requires_same_owner_to_remain_active():
    """退出或账号切换后，未到期的旧 binding 也必须立即失效。"""
    active = True

    async def push(_session_id, _payload):
        return None

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="main",
        origin_session_id="main::agent",
        agent_name="Agent",
        ttl_seconds=30,
    )
    assert binding is not None

    crew = SimpleNamespace(
        team=None,
        logout_coordinator=SimpleNamespace(allows_work=lambda owner: active and owner == "A:uid-a"),
    )
    app = FastAPI()
    app.include_router(create_interaction_router(bridge, crew))
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {binding.token}"}
    payload = {"questions": [{"id": "q1", "question": "?", "options": ["A"]}]}

    active = False
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/internal/interactions/ask",
            headers=headers,
            json=payload,
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ACTIVE_OWNER_REQUIRED"


@pytest.mark.asyncio
async def test_team_binding_enforces_role_identity_and_own_plan_node():
    """外部 Runtime 不能自报身份；Member 不能直问用户、建计划或改他人节点。"""
    seen: dict[str, object] = {}

    class FakeTeam:
        async def external_team_mention(self, session_id, **kwargs):
            seen["mention"] = (session_id, kwargs)
            return {"ok": True}

        def create_plan(self, session_id, **kwargs):
            seen["create"] = (session_id, kwargs)
            return {"ok": True}

        def read_plan(self, session_id, **_kwargs):
            return {
                "plan": {
                    "nodes": [
                        {"id": "mine", "assignee": "coder"},
                        {"id": "other", "assignee": "reviewer"},
                    ]
                }
            }

        def update_plan_node(self, session_id, **kwargs):
            seen["update"] = (session_id, kwargs)
            return {"ok": True}

    async def push(_session_id, _payload):
        return None

    bridge = InteractionBridge()
    bridge.configure(push_fn=push, gateway_url="http://127.0.0.1:8000")
    member = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="team-display",
        control_session_id="team-control",
        origin_session_id="team-control::coder",
        agent_name="Coder",
        ttl_seconds=30,
        context_type="team",
        team_session_id="team-control",
        member_id="coder",
        team_role="member",
    )
    assert member is not None

    crew = SimpleNamespace(
        team=FakeTeam(),
        logout_coordinator=SimpleNamespace(allows_work=lambda owner: owner == "A:uid-a"),
    )
    app = FastAPI()
    app.include_router(create_interaction_router(bridge, crew))
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {member.token}"}
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        ask = await client.post(
            "/api/internal/interactions/ask",
            headers=headers,
            json={"questions": [{"id": "q1", "question": "?", "options": ["A"]}]},
        )
        create = await client.post(
            "/api/internal/team/plan/create",
            headers=headers,
            json={"goal": "g", "nodes": [], "edges": []},
        )
        spoof = await client.post(
            "/api/internal/team/mention",
            headers=headers,
            json={
                "member_id": "leader",
                "to": ["leader"],
                "intent": "submit",
                "content": "done",
            },
        )
        own_update = await client.post(
            "/api/internal/team/plan/update",
            headers=headers,
            json={"node_id": "mine", "status": "completed"},
        )
        other_update = await client.post(
            "/api/internal/team/plan/update",
            headers=headers,
            json={"node_id": "other", "status": "completed"},
        )

    assert ask.status_code == 403
    assert create.status_code == 403
    assert spoof.status_code == 200
    assert seen["mention"][1]["member_id"] == "coder"
    assert own_update.status_code == 200
    assert seen["update"][1]["node_id"] == "mine"
    assert other_update.status_code == 403
