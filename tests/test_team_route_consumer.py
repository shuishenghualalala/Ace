"""4C-2 Team Route/CLI 消费路径生命周期化回归测试。"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.interaction_bridge import InteractionBridge
from crew.gateway.server import create_app
from crew.state.config import Config


def _api(tmp_path, monkeypatch, *, enable_team: bool = False):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            gateway_dev_mode=False,
            gateway_admin_accounts=["A:uid-a"],
        ),
        enable_team=enable_team,
    )
    return create_app(crew)


@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """复用 tests/conftest.py 的测试 owner，避免真实身份校验。"""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


@pytest.mark.asyncio
async def test_recover_team_node_returns_unavailable_when_team_disabled(
    tmp_path,
    monkeypatch,
    auth_headers,
) -> None:
    """recover_team_node 在 crew.team = None 时返回 409，不抛 AttributeError。"""

    api = _api(tmp_path, monkeypatch, enable_team=False)
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        session_id = "s-test-recover"
        # 先创建会话，避免 404
        resp = await client.put(
            f"/api/session/{session_id}/agent-config",
            json={"config": {"executor": "builtin"}},
        )
        assert resp.status_code == 200

        resp = await client.post(
            f"/api/session/{session_id}/team/recover",
            json={"node_id": "n1", "action": "retry"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "Team Runtime 不可用"


@pytest.mark.asyncio
async def test_set_session_agent_config_safe_when_team_disabled(
    tmp_path,
    monkeypatch,
    auth_headers,
) -> None:
    """set_session_agent_config 在 crew.team = None 时安全跳过 drop_session_team。"""

    api = _api(tmp_path, monkeypatch, enable_team=False)
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        session_id = "s-test-config"
        resp = await client.put(
            f"/api/session/{session_id}/agent-config",
            json={"config": {"executor": "builtin"}},
        )
        assert resp.status_code == 200
        assert resp.json()["executor"] == "builtin"


@pytest.mark.asyncio
async def test_interaction_bridge_team_tool_disabled_when_team_missing() -> None:
    """Team binding 的 invoke_tool 在 crew.team = None 时抛 ValueError 并降级。"""

    bridge = InteractionBridge()
    bridge.configure(push_fn=lambda *args, **kwargs: None, gateway_url="http://127.0.0.1:8080")
    crew = SimpleNamespace(team=None)
    bridge.bind_crew(crew)

    binding = bridge.create_binding(
        owner_account_id="A:uid-a",
        display_session_id="s1",
        origin_session_id="s1",
        agent_name="agent",
        ttl_seconds=3600.0,
        context_type="team",
        team_session_id="s1",
        member_id="m1",
        team_role="leader",
    )
    assert binding is not None

    with pytest.raises(ValueError, match="Team 模式未启用"):
        await bridge.invoke_tool(
            binding.token,
            tool_name="team_mention",
            payload={
                "to": ["all"],
                "intent": "broadcast",
                "content": "hello",
            },
        )


@pytest.mark.asyncio
async def test_dynamic_tool_specs_loads_team_status_constants() -> None:
    """TEAM_RESULT_STATUSES 惰性加载，dynamic_tool_specs 在 team 禁用时仍能渲染 schema。"""

    binding = SimpleNamespace(context_type="team", team_role="leader")
    specs = InteractionBridge.dynamic_tool_specs(binding)
    team_mention = next(spec for spec in specs if spec["name"] == "team_mention")
    assert "result_status" in team_mention["inputSchema"]["properties"]


def test_cli_integration_does_not_import_runtimes_router() -> None:
    """CLI 对 runtime payload 的消费改经 crew.team.payloads，不再反向依赖 gateway router。"""

    module = importlib.import_module("crew.cli.integration")
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.ImportFrom, ast.Import)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "crew.gateway.routers.runtimes" not in node.module, (
                "crew.cli.integration 仍通过 from crew.gateway.routers.runtimes 引用 router"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "crew.gateway.routers.runtimes" not in alias.name, (
                    "crew.cli.integration 仍通过 import crew.gateway.routers.runtimes 引用 router"
                )


def test_payloads_module_exports_expected_functions() -> None:
    """payloads.py 提供 CLI 与 Router 共用的四个投影函数。"""

    from crew.team import payloads

    assert callable(payloads._runtime_availability)
    assert callable(payloads._external_agent_payloads)
    assert callable(payloads._external_team_payloads)
    assert callable(payloads._managed_temporary_agent_ids)
