"""Team / Dynamic Kanban / Wiki 辅助推理的 Provider 解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.core.runctx import current_provider
from crew.state.config import Config, ModelProfile
from crew.wiki.config import WikiConfig


@pytest.mark.asyncio
async def test_team_planning_and_kanban_use_owner_default_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(enable_team=True)
    app.provider = FakeProvider()
    app.team.provider = app.provider
    app.dynamic_kanban.provider = app.provider

    profiles = {
        "owner-a": ModelProfile(id="owner-a", api_key="key-a", model="model-a"),
        "owner-b": ModelProfile(id="owner-b", api_key="key-b", model="model-b"),
    }
    providers = {model_id: FakeProvider() for model_id in profiles}
    monkeypatch.setattr(
        app.config,
        "owner_active_model_profile",
        lambda owner: profiles["owner-a" if owner == "A:uid-a" else "owner-b"],
    )
    monkeypatch.setattr(
        "crew.app.build_provider_for_profile",
        lambda profile, _timeout=None: providers[profile.id],
    )

    team_a = app.team._get_or_create("same-session", owner_account_id="A:uid-a")
    team_b = app.team._get_or_create("same-session", owner_account_id="B:uid-b")
    assert team_a.leader.provider is providers["owner-a"]
    assert team_a.direct_leader.provider is providers["owner-a"]
    assert team_b.leader.provider is providers["owner-b"]

    captured: dict[str, object] = {}

    class EmptyPlanner:
        async def plan_async(self, _team, _goal, *, provider, **_kwargs):
            captured["provider"] = provider
            return SimpleNamespace(nodes=[], edges=[], critical_missing_info=[])

    app.team.graph_planner = EmptyPlanner()
    planned = await app.team._ensure_runtime_plan_async(
        "plan-session",
        team_a,
        "规划一项任务",
        "",
        owner_account_id="A:uid-a",
    )
    assert planned is None
    assert captured["provider"] is providers["owner-a"]

    owner_store = app.dynamic_kanban.store.for_owner("A:uid-a")
    runtime = app.dynamic_kanban._make_runtime(owner_store, owner_account_id="A:uid-a")
    assert runtime.provider is providers["owner-a"]
    assert runtime.orchestrator.provider is providers["owner-a"]
    assert app._wiki_compiler._provider_for_owner("A:uid-a") is providers["owner-a"]
    assert app._wiki_summarizer._provider_for_owner("A:uid-a") is providers["owner-a"]


def test_wiki_uses_current_session_provider_before_owner_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(enable_team=False)
    owner_profile = ModelProfile(id="owner-model", api_key="key-a", model="model-a")
    owner_provider = FakeProvider()
    session_provider = FakeProvider()
    monkeypatch.setattr(app.config, "owner_active_model_profile", lambda _owner: owner_profile)
    monkeypatch.setattr(
        "crew.app.build_provider_for_profile",
        lambda _profile, _timeout=None: owner_provider,
    )

    # 没有 Agent 运行时上下文时，Wiki API/后台入口仍使用 owner 默认模型。
    assert app._wiki_compiler._provider_for_owner("A:uid-a") is owner_provider
    assert app._wiki_summarizer._provider_for_owner("A:uid-a") is owner_provider

    token = current_provider.set(session_provider)
    try:
        assert app._wiki_compiler._provider_for_owner("A:uid-a") is session_provider
        assert app._wiki_summarizer._provider_for_owner("A:uid-a") is session_provider
    finally:
        current_provider.reset(token)

    assert app._wiki_compiler._provider_for_owner("A:uid-a") is owner_provider


def test_explicit_wiki_model_overrides_current_session_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    config = Config()
    config.model_profiles = {
        "default": ModelProfile(id="default", api_key="default-key", model="default-model"),
        "wiki-fast": ModelProfile(id="wiki-fast", api_key="wiki-key", model="wiki-model"),
    }
    config.active_model_id = "default"
    config.default_model_id = "default"
    config.wiki = WikiConfig(model="wiki-fast")
    app_provider = FakeProvider()
    wiki_provider = FakeProvider()
    session_provider = FakeProvider()
    monkeypatch.setattr("crew.app.build_provider", lambda _config: app_provider)
    monkeypatch.setattr(
        "crew.app.build_provider_for_profile",
        lambda profile, _timeout=None: wiki_provider if profile.id == "wiki-fast" else app_provider,
    )
    app = build_app(config=config, enable_team=False)

    token = current_provider.set(session_provider)
    try:
        assert app._wiki_compiler._provider_for_owner("A:uid-a") is wiki_provider
        assert app._wiki_summarizer._provider_for_owner("A:uid-a") is wiki_provider
    finally:
        current_provider.reset(token)


def test_owner_provider_cache_is_evicted_without_dropping_team_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(enable_team=True)
    owner = "A:uid-a"
    profile = ModelProfile(id="owner-model", api_key="key-a", model="model-a")
    provider = FakeProvider()
    monkeypatch.setattr(app.config, "owner_active_model_profile", lambda _owner: profile)
    monkeypatch.setattr("crew.app.build_provider_for_profile", lambda _profile, _timeout=None: provider)

    app.owner_team_provider(owner)
    app.team._teams[(owner, "session-a")] = SimpleNamespace()
    plan = SimpleNamespace()
    app.team._plans[(owner, "session-a")] = plan

    app._invalidate_owner_team_provider(owner)

    assert owner not in app._owner_team_providers
    assert app._stale_owner_team_providers[id(provider)] is provider
    assert (owner, "session-a") not in app.team._teams
    assert app.team._plans[(owner, "session-a")] is plan
