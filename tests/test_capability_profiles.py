from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from crew.agent.capabilities import (
    CapabilityProfile,
    CapabilityProfileRegistry,
    canonicalize_capability_config,
)
from crew.app import build_app
from crew.gateway.helpers import session_agent_label
from crew.gateway.server import create_app
from crew.sites.capabilities import register_site_capability_profiles
from crew.state.access_control import AccessControlConfig
from crew.state.config import Config


OWNER = "A:uid-a"
SITE_SKILLS = {
    "webapp-building",
    "blueprint",
    "automation",
    "widget",
    "widgetdesign",
    "binding",
    "canvas",
}


def _app(tmp_path, *, access_control: AccessControlConfig | None = None):
    return build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            access_control=access_control or AccessControlConfig(),
        ),
        enable_team=False,
    )


def test_capability_registry_resolves_composition_and_detects_cycles() -> None:
    registry = CapabilityProfileRegistry()
    register_site_capability_profiles(registry)

    resolved = registry.resolve(["sites.authoring"])

    assert resolved.profile_ids == ("blueprint.authoring", "sites.authoring")
    assert resolved.toolsets == ("blueprint", "sites")
    assert set(resolved.skills) == SITE_SKILLS
    assert resolved.features == ("blueprint", "sites")
    assert registry.display_for(["sites.authoring"]).name == "灵感"

    cyclic = CapabilityProfileRegistry()
    cyclic.register(CapabilityProfile(id="a", feature="a", includes=("b",)))
    cyclic.register(CapabilityProfile(id="b", feature="b", includes=("a",)))
    with pytest.raises(ValueError, match="循环依赖"):
        cyclic.resolve(["a"])


def test_profile_unregister_keeps_feature_ownership_fail_closed() -> None:
    class Catalog:
        @staticmethod
        def toolset_for(name: str) -> str:
            return {"publish_site": "sites", "file_read": "file"}[name]

    registry = CapabilityProfileRegistry()
    register_site_capability_profiles(registry)
    assert registry.unregister("sites.authoring") is True

    resolved = registry.resolve(["sites.authoring"], strict=False)

    assert resolved.profile_ids == ()
    assert registry.filter_authorized_tools(
        Catalog(), ["publish_site", "file_read"], resolved
    ) == ["file_read"]
    assert "webapp-building" in (registry.disabled_skills_for(None, resolved) or [])


def test_capability_config_canonicalizes_legacy_site_flags() -> None:
    registry = CapabilityProfileRegistry()
    register_site_capability_profiles(registry)

    config = canonicalize_capability_config(
        {"executor": "builtin", "inspiration_creation": True},
        registry,
    )

    assert config == {
        "executor": "builtin",
        "capability_profiles": ["sites.authoring"],
    }


def test_site_profile_scopes_main_agent_tools_skills_and_prompt(tmp_path) -> None:
    app = _app(tmp_path)

    main = app._make_agent({}, owner_account_id=OWNER)
    blueprint = app._make_agent(
        {"executor": "builtin", "capability_profiles": ["blueprint.authoring"]},
        owner_account_id=OWNER,
    )
    site = app._make_agent(
        {"executor": "builtin", "capability_profiles": ["sites.authoring"]},
        owner_account_id=OWNER,
    )
    legacy_site = app._make_agent(
        {"executor": "builtin", "site_creation": True},
        owner_account_id=OWNER,
    )

    assert "publish_site" not in main.tool_filter
    assert not {"Canvas", "Widget", "Automation", "Binding"} & set(main.tool_filter)
    assert SITE_SKILLS <= set(main.disabled_skills or [])

    assert "publish_site" not in blueprint.tool_filter
    assert {"Canvas", "Widget", "Automation", "Binding"} <= set(blueprint.tool_filter)
    assert "webapp-building" in (blueprint.disabled_skills or [])
    assert not ({"blueprint", "widget", "canvas"} & set(blueprint.disabled_skills or []))

    assert "publish_site" in site.tool_filter
    assert {"Canvas", "Widget", "Automation", "Binding"} <= set(site.tool_filter)
    assert not (SITE_SKILLS & set(site.disabled_skills or []))
    assert "Ace 灵感 App" in site.system_prompt
    assert legacy_site.tool_filter == site.tool_filter
    assert legacy_site.disabled_skills == site.disabled_skills


def test_site_profile_supplies_session_display_metadata(tmp_path) -> None:
    app = _app(tmp_path)
    app.session_store.set_agent_config(
        "site-session",
        {"executor": "builtin", "capability_profiles": ["sites.authoring"]},
        owner_account_id=OWNER,
    )

    assert session_agent_label(app, "site-session", owner_account_id=OWNER) == {
        "name": "灵感",
        "provider": "sites",
        "display_badge": "◇",
    }


def test_site_profile_cannot_bypass_access_control(tmp_path) -> None:
    app = _app(
        tmp_path,
        access_control=AccessControlConfig(
            internal={
                "disabled_toolsets": ["sites", "blueprint"],
                "disabled_skills": ["webapp-building", "blueprint"],
            }
        ),
    )

    site = app._make_agent(
        {"executor": "builtin", "capability_profiles": ["sites.authoring"]},
        owner_account_id=OWNER,
    )

    assert "publish_site" not in site.tool_filter
    assert not {"Canvas", "Widget", "Automation", "Binding"} & set(site.tool_filter)
    assert {"webapp-building", "blueprint"} <= set(site.disabled_skills or [])


def test_site_profile_preserves_deny_all_skill_policy(tmp_path) -> None:
    app = _app(
        tmp_path,
        access_control=AccessControlConfig(internal={"disabled_skills": ["*"]}),
    )

    site = app._make_agent(
        {"executor": "builtin", "capability_profiles": ["sites.authoring"]},
        owner_account_id=OWNER,
    )

    assert site.disabled_skills == ["*"]


@pytest.mark.asyncio
async def test_session_agent_config_validates_and_canonicalizes_profiles(
    tmp_path,
    auth_headers,
) -> None:
    app = _app(tmp_path)
    api = create_app(app)
    transport = ASGITransport(app=api)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        legacy = await client.put(
            "/api/session/legacy-site/agent-config",
            json={"executor": "builtin", "inspiration_creation": True},
        )
        unknown = await client.put(
            "/api/session/unknown-profile/agent-config",
            json={"executor": "builtin", "capability_profiles": ["missing.profile"]},
        )
        malformed = await client.put(
            "/api/session/malformed-profile/agent-config",
            json={"executor": "builtin", "capability_profiles": "sites.authoring"},
        )

    assert legacy.status_code == 200
    assert legacy.json()["capability_profiles"] == ["sites.authoring"]
    assert "inspiration_creation" not in legacy.json()
    assert unknown.status_code == 400
    assert "未知 Capability Profile" in unknown.json()["error"]
    assert malformed.status_code == 400
    assert "字符串数组" in malformed.json()["error"]
