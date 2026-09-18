"""Feature-owned Agent preset contracts and Wiki migration boundaries."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from crew.agent.subagent.tools import register_subagent_tools
from crew.agent.subagent.registry import SubagentRegistry
from crew.agent.subagent import registry as sub_registry_module
from crew.core.envelope import ResponseChunk
from crew.core.types import ToolCall
from crew.features import (
    AgentPresetConflictError,
    AgentPresetContribution,
    AgentPresetRegistry,
    AgentPresetUnavailableError,
    FeatureGeneration,
    FeatureLeaseUnavailableError,
    FeatureRuntime,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
)
from crew.tools.registry import Registry
from crew.wiki import build_wiki_feature
from crew.core.mocks import FakeProvider


def _preset(name: str = "Wiki", **overrides: object) -> AgentPresetContribution:
    values: dict[str, object] = {
        "name": name,
        "description": "feature-owned preset",
        "system_prompt": "You are a focused feature agent.",
    }
    values.update(overrides)
    return AgentPresetContribution(**values)


def test_agent_preset_contribution_normalizes_and_validates_policy_fields():
    contribution = AgentPresetContribution(
        name="  Wiki  ",
        description=None,  # type: ignore[arg-type]
        system_prompt="  prompt  ",
        agent_id="  wiki-agent  ",
        fixed_skills=(" crew-wiki-curator ", "crew-wiki-curator"),
        toolsets=["wiki.read", " wiki.read ", ""],  # type: ignore[arg-type]
        toolset_additions=("wiki.manage", "wiki.manage"),
        context_tags=["wiki", " wiki"],  # type: ignore[arg-type]
        reserved_toolsets="wiki.read",  # type: ignore[arg-type]
        reserved_skills=["crew-wiki-curator"],  # type: ignore[arg-type]
    )

    assert contribution.name == "Wiki"
    assert contribution.description == ""
    assert contribution.system_prompt == "prompt"
    assert contribution.agent_id == "wiki-agent"
    assert contribution.fixed_skills == ("crew-wiki-curator",)
    assert contribution.toolsets == ("wiki.read",)
    assert contribution.toolset_additions == ("wiki.manage",)
    assert contribution.context_tags == ("wiki",)
    assert contribution.reserved_toolsets == ("wiki.read",)
    assert contribution.as_spec()["preset_skills"] == ["crew-wiki-curator"]

    with pytest.raises(ValueError, match="name"):
        _preset(name=" ")
    with pytest.raises(ValueError, match="system prompt"):
        _preset(system_prompt=" ")
    with pytest.raises(ValueError, match="disclosure mode"):
        _preset(disclosure_mode="hidden")


def test_feature_runtime_keeps_legacy_four_positional_registry_arguments():
    from crew.features import (
        ContextContributorRegistry,
        ExecutionDriverRegistry,
        RouteRegistry,
        ServiceRegistry,
    )

    services = ServiceRegistry()
    drivers = ExecutionDriverRegistry()
    contexts = ContextContributorRegistry()
    routes = RouteRegistry()
    runtime = FeatureRuntime(services, drivers, contexts, routes)

    assert runtime.services is services
    assert runtime.execution_drivers is drivers
    assert runtime.context_contributors is contexts
    assert runtime.routes is routes
    assert runtime.agent_presets.names() == []


@pytest.mark.asyncio
async def test_preset_registry_latest_generation_and_exact_token_ownership():
    registry = AgentPresetRegistry()
    old_scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    new_scope = FeatureScope(FeatureGeneration("product.wiki", 2))
    old_token = registry.register(old_scope, _preset(), label="preset:old")
    new_token = registry.register(new_scope, _preset(), label="preset:new")
    old_scope.activate()
    new_scope.activate()

    assert registry.resolve("Wiki").generation.key == "product.wiki@g2"
    assert registry.names() == ["Wiki"]
    with pytest.raises(AgentPresetConflictError):
        registry.register(old_scope, _preset(), label="preset:duplicate")
    unrelated = FeatureScope(FeatureGeneration("product.other", 1))
    with pytest.raises(AgentPresetConflictError):
        registry.register(unrelated, _preset(), label="preset:conflict")

    # Disposing the old registration cannot remove the newer same-name entry.
    await old_token.dispose()
    assert registry.resolve("Wiki").generation.key == "product.wiki@g2"
    assert old_token.generation.key == "product.wiki@g1"

    await new_scope.stop(FeatureStopPolicy.DRAIN)
    with pytest.raises(AgentPresetUnavailableError):
        registry.resolve("Wiki")
    assert new_token.state.value == "disposed"


@pytest.mark.asyncio
async def test_preset_lease_rejects_new_work_and_waits_for_inflight_work():
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    scope.activate()
    lease = scope.acquire_lease("inflight")
    scope.begin_draining()
    stopping = asyncio.create_task(scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=None))
    await asyncio.sleep(0)

    with pytest.raises(FeatureLeaseUnavailableError, match="does not accept new requests"):
        scope.acquire_lease("late")
    assert not stopping.done()
    lease.release()
    await stopping
    assert scope.state is FeatureState.DISPOSED


@pytest.mark.asyncio
async def test_preset_cancel_stop_signals_owner_task_and_releases_lease():
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    scope.activate()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def holder() -> None:
        lease = scope.acquire_lease("cancellable")
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            lease.release()

    task = asyncio.create_task(holder())
    await started.wait()
    await scope.stop(FeatureStopPolicy.CANCEL, timeout_seconds=None)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert scope.state is FeatureState.DISPOSED


class _WikiHost:
    session_store = object()
    workspace_store = object()
    security_service = object()
    _auxiliary_providers: list[object] = []
    _UNSET_KNOWLEDGE_SERVICE = object()

    def __init__(self) -> None:
        self._knowledge_service_override = self._UNSET_KNOWLEDGE_SERVICE


def _wiki_host() -> _WikiHost:
    host = _WikiHost()
    host._auxiliary_providers = []
    return host


@pytest.mark.asyncio
async def test_wiki_bundle_owns_preset_assets_and_generation_switch(tmp_path):
    runtime = FeatureRuntime()
    registry = Registry()
    host = _wiki_host()
    first = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=1,
    )
    second = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=2,
    )

    await runtime.activate(first.definition)
    binding = runtime.agent_presets.resolve("Wiki")
    assert binding.generation.key == "product.wiki@g1"
    assert binding.contribution.toolset_additions == ("wiki.read", "wiki.manage")
    assert binding.contribution.context_tags == ("wiki",)
    assert binding.contribution.disclosure_mode == "direct"
    assert binding.contribution.reserved_toolsets == ("wiki.read", "wiki.manage")
    assert binding.contribution.reserved_skills == ("crew-wiki-curator",)
    assert "Wiki" not in registry.names()

    # A live Agent/child lease keeps the old generation alive across restart.
    old_lease = binding.acquire_lease("agent-preset:Wiki")
    updating = asyncio.create_task(runtime.update(second.definition))
    await asyncio.sleep(0)
    assert not updating.done()
    old_lease.release()
    result = await updating
    assert result.updated
    assert runtime.agent_presets.resolve("Wiki").generation.key == "product.wiki@g2"

    await runtime.deactivate("product.wiki")
    with pytest.raises(AgentPresetUnavailableError):
        runtime.agent_presets.resolve("Wiki")
    assert not set(("wiki_search", "wiki_read", "wiki_apply_ingest")) & set(registry.names())


@pytest.mark.asyncio
async def test_run_agent_schema_and_execution_follow_active_preset_generations():
    runtime = FeatureRuntime()
    registry = Registry()
    presets = runtime.agent_presets
    generic_scope = FeatureScope(FeatureGeneration("core.agent-presets", 1))
    generic_token = presets.register(generic_scope, _preset("Explore"), label="preset:Explore")
    generic_scope.activate()
    wiki_scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    wiki_token = presets.register(wiki_scope, _preset(), label="preset:Wiki")
    wiki_scope.activate()

    class Child:
        async def run(self, envelope):
            yield ResponseChunk.final(envelope.request_id, "ok")

        async def aclose(self):
            return None

    register_subagent_tools(
        registry,
        SimpleNamespace(names=lambda: [], get=lambda _name: None),
        lambda _spec: Child(),
        preset_registry=presets,
    )
    tool = registry.get("run_agent")
    assert set(tool.current_parameters()["properties"]["agent_type"]["enum"]) == {
        "Explore",
        "Wiki",
    }

    result = await registry.execute(
        ToolCall("preset", "run_agent", {"agent_type": "Wiki", "goal": "query"})
    )
    assert not result.is_error
    assert json.loads(result.content)["results"][0]["status"] == "completed"

    await wiki_scope.stop()
    assert tool.current_parameters()["properties"]["agent_type"]["enum"] == ["Explore"]
    rejected = await registry.execute(
        ToolCall("disabled", "run_agent", {"agent_type": "Wiki", "goal": "query"})
    )
    assert rejected.is_error
    assert "Wiki" in rejected.content
    await generic_scope.stop()
    assert generic_token.state.value == "disposed"
    assert wiki_token.state.value == "disposed"


@pytest.mark.asyncio
async def test_run_agent_foreground_lease_blocks_generation_stop_until_child_finishes():
    runtime = FeatureRuntime()
    registry = Registry()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    runtime.agent_presets.register(scope, _preset(), label="preset:Wiki")
    scope.activate()
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingChild:
        async def run(self, envelope):
            started.set()
            await release.wait()
            yield ResponseChunk.final(envelope.request_id, "done")

        async def aclose(self):
            return None

    register_subagent_tools(
        registry,
        SimpleNamespace(names=lambda: [], get=lambda _name: None),
        lambda _spec: BlockingChild(),
        preset_registry=runtime.agent_presets,
    )
    running = asyncio.create_task(
        registry.execute(
            ToolCall("foreground", "run_agent", {"agent_type": "Wiki", "goal": "query"})
        )
    )
    await started.wait()
    stopping = asyncio.create_task(scope.stop(timeout_seconds=None))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    result = await running
    await stopping
    assert not result.is_error
    assert scope.state is FeatureState.DISPOSED


@pytest.mark.asyncio
async def test_run_agent_background_lease_blocks_generation_stop_until_child_finishes():
    runtime = FeatureRuntime()
    registry = Registry()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    runtime.agent_presets.register(scope, _preset(), label="preset:Wiki")
    scope.activate()
    started = asyncio.Event()
    release = asyncio.Event()
    launched: list[object] = []

    class BlockingChild:
        async def run(self, envelope):
            started.set()
            await release.wait()
            yield ResponseChunk.final(envelope.request_id, "done")

        async def aclose(self):
            return None

    class Tasks:
        def create_runtime(self, **kwargs):
            return {"task_id": "bg-1", "id": "bg-1", **kwargs}

        async def create_runtime_async(self, **kwargs):
            return self.create_runtime(**kwargs)

        def mark_running(self, _task_id):
            return None

        async def mark_running_async(self, _task_id):
            return None

        def update_status(self, *_args):
            return None

        async def update_status_async(self, *_args):
            return None

        def touch_activity(self, *_args):
            return None

        async def touch_activity_async(self, *_args):
            return None

    register_subagent_tools(
        registry,
        SimpleNamespace(names=lambda: [], get=lambda _name: None),
        lambda _spec: BlockingChild(),
        tasks=Tasks(),
        launch_background=launched.append,
        preset_registry=runtime.agent_presets,
    )
    launched_result = await registry.execute(
        ToolCall(
            "background",
            "run_agent",
            {"agent_type": "Wiki", "goal": "query", "run_in_background": True},
        )
    )
    assert json.loads(launched_result.content)["status"] == "launched"
    child_task = asyncio.create_task(launched.pop())
    await started.wait()
    stopping = asyncio.create_task(scope.stop(timeout_seconds=None))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await child_task
    await stopping
    assert scope.state is FeatureState.DISPOSED


def test_wiki_user_override_is_claimed_idempotently_and_keeps_fixed_security_policy(
    tmp_path,
):
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    builtin_dir.mkdir()
    user_dir.mkdir()
    body = "用户自定义 Wiki prompt"
    (builtin_dir / "wiki.md").write_text(
        "---\nname: Wiki\ndescription: builtin\n---\nbuiltin\n",
        encoding="utf-8",
    )
    (user_dir / "wiki.md").write_text(
        f"---\nname: Wiki\ndescription: user\nskills: [custom-skill]\n---\n{body}\n",
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sub_registry_module, "_PRESETS_DIR", builtin_dir)
    monkeypatch.setattr(sub_registry_module, "get_user_agents_dir", lambda: user_dir)
    try:
        source = SubagentRegistry()
        claimed = source.claim("Wiki")
        assert claimed is not None and claimed.source == "user"
        assert source.claim("Wiki") is claimed

        host = _wiki_host()
        runtime = FeatureRuntime()
        tools = Registry()
        bundle = build_wiki_feature(
            host,
            tools,
            provider=FakeProvider(),
            storage_root=tmp_path / "wiki",
            preset_source=source,
        )

        async def exercise():
            await runtime.activate(bundle.definition)
            contribution = runtime.agent_presets.resolve("Wiki").contribution
            assert contribution.system_prompt == body
            assert contribution.fixed_skills == ("custom-skill",)
            assert contribution.toolset_additions == ("wiki.read", "wiki.manage")
            assert contribution.reserved_toolsets == ("wiki.read", "wiki.manage")
            assert contribution.reserved_skills == ("crew-wiki-curator",)
            await runtime.deactivate("product.wiki")

        asyncio.run(exercise())
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_main_agent_driver_holds_preset_lease_for_entire_turn():
    from contextlib import asynccontextmanager

    from crew.app import CrewApp
    from crew.core.envelope import Envelope

    runtime = FeatureRuntime()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    runtime.agent_presets.register(scope, _preset(), label="preset:Wiki")
    scope.activate()
    started = asyncio.Event()
    release = asyncio.Event()

    class Agent:
        async def run(self, envelope):
            started.set()
            await release.wait()
            yield ResponseChunk.final(envelope.request_id, "done")

    class Agents:
        @asynccontextmanager
        async def lease(self, _session_id, config, *, owner_account_id):
            assert owner_account_id == "owner"
            assert config["_preset_generation"] == "product.wiki@g1"
            yield Agent()

    app = object.__new__(CrewApp)
    app.agent_presets = runtime.agent_presets
    app.agents = Agents()
    app._session_agent_config = lambda _session_id, owner_account_id: {
        "preset_agent_type": "Wiki"
    }
    envelope = Envelope.of("query", session_id="session", user_id="owner")

    async def collect():
        return [chunk async for chunk in app._run_default_execution_driver(envelope)]

    running = asyncio.create_task(collect())
    await started.wait()
    stopping = asyncio.create_task(scope.stop(timeout_seconds=None))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    chunks = await running
    await stopping
    assert chunks[-1].body["text"] == "done"


@pytest.mark.asyncio
async def test_context_preview_holds_preset_lease_for_entire_preview(monkeypatch):
    from contextlib import asynccontextmanager

    import crew.app as app_module
    from crew.app import CrewApp

    runtime = FeatureRuntime()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    runtime.agent_presets.register(scope, _preset(), label="preset:Wiki")
    scope.activate()
    started = asyncio.Event()
    release = asyncio.Event()

    class PreviewAgent:
        async def preview_context(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return {"used_tokens": 1}

    class SessionStore:
        def get_agent_config(self, _session_id, *, owner_account_id):
            assert owner_account_id == "owner"
            return {"preset_agent_type": "Wiki"}

        def get_workspace_id(self, _session_id, _owner):
            return "default"

    class WorkspaceStore:
        def get(self, _workspace_id, *, owner_account_id):
            assert owner_account_id == "owner"
            return {"instructions": "", "root_path": ""}

    class Agents:
        @asynccontextmanager
        async def lease(self, _session_id, config, *, owner_account_id):
            assert owner_account_id == "owner"
            assert config["_preset_generation"] == "product.wiki@g1"
            yield PreviewAgent()

    class Config:
        pass

    app = object.__new__(CrewApp)
    app.agent_presets = runtime.agent_presets
    app.session_store = SessionStore()
    app.workspace_store = WorkspaceStore()
    app.config = Config()
    app.agents = Agents()
    monkeypatch.setattr(app_module, "SingleAgent", PreviewAgent)

    running = asyncio.create_task(app.preview_session_context("session", "owner"))
    await started.wait()
    stopping = asyncio.create_task(scope.stop(timeout_seconds=None))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    assert await running == {"used_tokens": 1}
    await stopping


def test_matching_preset_child_keeps_parent_cap_and_excludes_nested_capabilities():
    from crew.app import build_app
    from crew.core.runctx import current_authorized_tool_names
    from crew.state.config import Config

    app = build_app(config=Config(max_iterations=5), enable_team=False)
    token = current_authorized_tool_names.set(
        ("file_read", "wiki_search", "delegate_task", "run_agent", "cron_create", "external_agent")
    )
    try:
        child = app._make_subagent(app.agent_presets.resolve("Wiki").contribution.as_spec())
    finally:
        current_authorized_tool_names.reset(token)

    assert "file_read" in child.tool_filter
    assert "wiki_search" in child.tool_filter
    assert not {
        "delegate_task",
        "run_agent",
        "external_agent",
        "cron_create",
    }.intersection(child.tool_filter)


@pytest.mark.asyncio
async def test_agent_manager_retires_only_stale_session_agent_after_generation_change():
    from crew.app import AgentManager

    created: list[object] = []
    closed: list[object] = []

    class Agent:
        async def aclose(self):
            closed.append(self)

    def factory(_config, *, owner_account_id):
        assert owner_account_id == "owner"
        agent = Agent()
        created.append(agent)
        return agent

    manager = AgentManager(factory)
    old_config = {
        "preset_agent_type": "Wiki",
        "_preset_generation": "product.wiki@g1",
    }
    new_config = {
        "preset_agent_type": "Wiki",
        "_preset_generation": "product.wiki@g2",
    }
    unrelated = manager.get("other-session", old_config, owner_account_id="owner")

    async with manager.lease("session", old_config, owner_account_id="owner") as old:
        replacement = manager.get("session", new_config, owner_account_id="owner")
        assert replacement is not old
        assert manager.peek("other-session", "owner") is unrelated
        assert closed == []

    await manager.wait_closed()
    assert closed == [old]
    await manager.aclose()
    assert set(closed) == set(created)


def test_crew_app_agent_assembly_has_no_wiki_name_special_case():
    from crew.app import CrewApp

    source = inspect.getsource(CrewApp._make_agent)
    assert "Wiki" not in source
    assert "crew.wiki" not in source
    assert "_preset_execution_lease" in inspect.getsource(CrewApp._run_default_execution_driver)
    assert "_preset_execution_lease" in inspect.getsource(CrewApp.preview_session_context)
