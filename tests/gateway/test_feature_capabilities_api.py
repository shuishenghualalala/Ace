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


WIKI_FEATURE_ID = "product.wiki"
EXTERNAL_AGENTS_FEATURE_ID = "product.external-agents"


def _config_body_crew(*, snapshot: dict, wiki_enabled: bool, external_agents_enabled: bool) -> SimpleNamespace:
    """Build a config_body host whose Runtime snapshot and config flags are both controllable."""

    class FakeRuntime:
        def capability_snapshot(self) -> dict:
            return dict(snapshot)

    class FakePlugins:
        feature_runtime = FakeRuntime()

    return SimpleNamespace(
        plugins=FakePlugins(),
        config=SimpleNamespace(
            wiki_enabled=wiki_enabled,
            external_agents_enabled=external_agents_enabled,
            security_enabled=False,
        ),
        owner_visible_model_profiles=lambda *_args, **_kwargs: (),
        owner_default_model_profile=lambda *_args: SimpleNamespace(
            model="test", has_key=False, base_url="", id="default"
        ),
        owner_public_model_options=lambda *_args: [],
    )


@pytest.mark.parametrize(
    "feature_id, flag_name",
    [
        (WIKI_FEATURE_ID, "wiki_enabled"),
        (EXTERNAL_AGENTS_FEATURE_ID, "external_agents_enabled"),
    ],
)
@pytest.mark.parametrize("config_enabled", [False, True])
@pytest.mark.parametrize("runtime_available", [False, True])
def test_config_body_capability_available_is_config_enabled_and_runtime_active(
    feature_id, flag_name, config_enabled, runtime_available,
):
    crew = _config_body_crew(
        snapshot={
            feature_id: {
                "state": "active",
                "available": runtime_available,
                "generation": f"{feature_id}@g1",
            }
        },
        wiki_enabled=config_enabled if flag_name == "wiki_enabled" else True,
        external_agents_enabled=config_enabled if flag_name == "external_agents_enabled" else True,
    )

    capability = config_body(crew, owner_account_id="owner")["feature_capabilities"][feature_id]

    assert capability["available"] is (config_enabled and runtime_available)
    assert capability["state"] == "active"
    assert capability["generation"] == f"{feature_id}@g1"


def test_config_body_capability_override_preserves_entries_and_leaves_absent_untouched():
    snapshot = {
        WIKI_FEATURE_ID: {"state": "active", "available": True, "generation": f"{WIKI_FEATURE_ID}@g2"},
        EXTERNAL_AGENTS_FEATURE_ID: {
            "state": "active", "available": True, "generation": f"{EXTERNAL_AGENTS_FEATURE_ID}@g1",
        },
        "product.other": {"state": "active", "available": True, "generation": "product.other@g1"},
    }
    crew = _config_body_crew(
        snapshot=snapshot,
        wiki_enabled=False,
        external_agents_enabled=False,
    )

    capabilities = config_body(crew, owner_account_id="owner")["feature_capabilities"]

    assert capabilities[WIKI_FEATURE_ID]["available"] is False
    assert capabilities[EXTERNAL_AGENTS_FEATURE_ID]["available"] is False
    assert capabilities[WIKI_FEATURE_ID]["state"] == "active"
    assert capabilities[WIKI_FEATURE_ID]["generation"] == f"{WIKI_FEATURE_ID}@g2"
    assert capabilities[EXTERNAL_AGENTS_FEATURE_ID]["generation"] == f"{EXTERNAL_AGENTS_FEATURE_ID}@g1"
    assert capabilities["product.other"] == snapshot["product.other"]


def test_config_body_capability_override_never_adds_undeclared_features():
    crew = _config_body_crew(
        snapshot={
            "product.other": {"state": "active", "available": True, "generation": "product.other@g1"},
        },
        wiki_enabled=False,
        external_agents_enabled=False,
    )

    capabilities = config_body(crew, owner_account_id="owner")["feature_capabilities"]

    assert WIKI_FEATURE_ID not in capabilities
    assert EXTERNAL_AGENTS_FEATURE_ID not in capabilities
    assert capabilities["product.other"]["available"] is True
