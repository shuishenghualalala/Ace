"""Gateway contract tests for the host feature capability snapshot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.helpers import config_body
from crew.gateway.server import create_app
from crew.state.config import Config


@pytest_asyncio.fixture
async def capability_api(tmp_path):
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )
    try:
        yield create_app(crew)
    finally:
        await crew.shutdown()


@pytest.mark.asyncio
async def test_config_api_exposes_only_minimal_capability_fields(capability_api, auth_headers):
    transport = ASGITransport(app=capability_api)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=auth_headers
    ) as client:
        response = await client.get("/api/config")

    assert response.status_code == 200, response.text
    capabilities = response.json()["feature_capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities
    assert "product.team" not in capabilities
    for capability in capabilities.values():
        assert set(capability) == {"state", "available", "generation"}
        assert isinstance(capability["state"], str)
        assert isinstance(capability["available"], bool)
        assert capability["generation"] is None or isinstance(capability["generation"], str)


def test_config_body_legacy_runtime_substitute_returns_empty_map():
    class LegacyRuntime:
        pass

    crew = SimpleNamespace(
        plugins=SimpleNamespace(feature_runtime=LegacyRuntime()),
        config=SimpleNamespace(
            wiki_enabled=False,
            external_agents_enabled=True,
            security_enabled=False,
        ),
        owner_visible_model_profiles=lambda *_args, **_kwargs: (),
        owner_default_model_profile=lambda *_args: SimpleNamespace(
            model="test", has_key=False, base_url="", id="default"
        ),
        owner_public_model_options=lambda *_args: [],
    )

    assert config_body(crew, owner_account_id="owner")["feature_capabilities"] == {}


@pytest.mark.asyncio
async def test_real_team_and_kanban_states_are_independent_in_config_response(
    auth_headers, tmp_path,
):
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        cron_enabled=False,
        api_key="",
    )
    crew = build_app(config=config, enable_team=True)
    try:
        await crew._activate_managed_features()
        app = create_app(crew)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=auth_headers
        ) as client:
            response = await client.get("/api/config")
            assert response.status_code == 200, response.text
            capabilities = response.json()["feature_capabilities"]
            assert capabilities["product.team"]["available"] is True
            assert capabilities["product.dynamic-kanban"]["available"] is True

            await crew.plugins.feature_runtime.deactivate("product.team")
            response = await client.get("/api/config")

        assert response.status_code == 200, response.text
        capabilities = response.json()["feature_capabilities"]
        assert capabilities["product.team"]["available"] is False
        assert capabilities["product.dynamic-kanban"]["available"] is True
        assert capabilities["product.team"]["generation"] is None
        assert capabilities["product.dynamic-kanban"]["generation"].startswith(
            "product.dynamic-kanban@g"
        )
    finally:
        await crew.shutdown()
