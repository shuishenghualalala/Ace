"""Owner 默认模型在 Team / Dynamic Kanban 辅助推理中的解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.state.config import ModelProfile


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
