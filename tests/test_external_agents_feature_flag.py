from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.envelope import Envelope
from crew.gateway.server import create_app
from crew.state.config import Config, load_config


def test_external_agents_flag_defaults_true_and_parses_false(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    default_path.write_text("{}\n", encoding="utf-8")
    assert load_config(config_path=default_path).external_agents_enabled is True

    disabled_path = tmp_path / "disabled.yaml"
    disabled_path.write_text(
        yaml.safe_dump({"external_agents": {"enabled": False}}),
        encoding="utf-8",
    )
    assert load_config(config_path=disabled_path).external_agents_enabled is False


@pytest.mark.asyncio
async def test_disabled_flag_hides_management_api_and_rejects_new_external_bindings(
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            external_agents_enabled=False,
            gateway_admin_accounts=["A:uid-a"],
        ),
        enable_team=False,
    )
    owner = "A:uid-a"
    crew.session_store.ensure_session("existing-external", owner_account_id=owner)
    crew.session_store.set_agent_config(
        "existing-external",
        {
            "executor": "external",
            "external": {"external_agent_id": "agent-1"},
        },
        owner_account_id=owner,
    )
    crew.session_store.ensure_session("existing-external-team", owner_account_id=owner)
    crew.session_store.set_agent_config(
        "existing-external-team",
        {
            "executor": "team",
            "team": {"external_team_id": "team-1"},
        },
        owner_account_id=owner,
    )
    api = create_app(crew)
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        config_response = await client.get("/api/config")
        assert config_response.status_code == 200
        assert config_response.json()["external_agents"] == {"enabled": False}
        assert config_response.json()["security"] == {
            "enabled": False,
            "default_mode": "full_access",
        }

        external_list = await client.get("/api/external-agents")
        assert external_list.status_code == 403
        assert external_list.json()["code"] == "external_agents_disabled"

        external_binding = await client.put(
            "/api/session/external/agent-config",
            json={
                "executor": "acp",
                "acp": {"external_agent_id": "agent-1"},
            },
        )
        assert external_binding.status_code == 403
        assert external_binding.json()["code"] == "external_agents_disabled"

        team_binding = await client.put(
            "/api/session/external-team/agent-config",
            json={
                "executor": "team",
                "team": {"external_team_id": "team-1"},
            },
        )
        assert team_binding.status_code == 403
        assert team_binding.json()["code"] == "external_agents_disabled"

        external_config = await client.get("/api/session/existing-external/agent-config")
        assert external_config.status_code == 403
        assert external_config.json()["code"] == "external_agents_disabled"

        external_model = await client.get("/api/session/existing-external/model")
        assert external_model.status_code == 403
        assert external_model.json()["code"] == "external_agents_disabled"

        team_model = await client.get("/api/session/existing-external-team/model")
        assert team_model.status_code == 403
        assert team_model.json()["code"] == "external_agents_disabled"

        sessions = await client.get("/api/sessions")
        assert sessions.status_code == 200
        assert any(
            session["session_id"] == "existing-external"
            for session in sessions.json()
        )

        builtin_binding = await client.put(
            "/api/session/builtin/agent-config",
            json={"executor": "builtin"},
        )
        assert builtin_binding.status_code == 200


@pytest.mark.asyncio
async def test_disabled_flag_preserves_but_blocks_existing_external_session(tmp_path: Path) -> None:
    owner = "A:uid-a"
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            external_agents_enabled=False,
        ),
        enable_team=False,
    )
    crew.session_store.ensure_session("old-external", owner_account_id=owner)
    crew.session_store.set_agent_config(
        "old-external",
        {
            "executor": "acp",
            "acp": {"external_agent_id": "agent-1"},
        },
        owner_account_id=owner,
    )

    chunks = [
        chunk
        async for chunk in crew.handle(
            Envelope.of("继续", session_id="old-external", user_id=owner),
        )
    ]

    assert len(chunks) == 1
    assert chunks[0].kind == "error"
    assert chunks[0].body["message"] == "外部智能体功能已在配置中关闭"
    stored = crew.session_store.get_agent_config("old-external", owner_account_id=owner)
    assert stored["acp"]["external_agent_id"] == "agent-1"

    crew.session_store.ensure_session("old-team", owner_account_id=owner)
    crew.session_store.set_agent_config(
        "old-team",
        {
            "executor": "team",
            "team": {"external_team_id": "team-1"},
        },
        owner_account_id=owner,
    )
    team_chunks = [
        chunk
        async for chunk in crew.handle(
            Envelope.of("继续", session_id="old-team", user_id=owner, mode="team"),
        )
    ]
    assert len(team_chunks) == 1
    assert team_chunks[0].kind == "error"
    assert team_chunks[0].body["message"] == "外部智能体功能已在配置中关闭"
    stored_team = crew.session_store.get_agent_config("old-team", owner_account_id=owner)
    assert stored_team["team"]["external_team_id"] == "team-1"
