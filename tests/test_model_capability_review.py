"""Regression tests for model capability enforcement and cache invalidation."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew.agent.executor import BuiltinExecutor, ExecutionContext
from crew.app import AgentManager, build_app
from crew.core.mocks import FakeProvider
from crew.core.runctx import current_model_capabilities
from crew.core.types import ChatResponse, Message, ToolCall
from crew.plugins.manager import PluginManager
from crew.state.config import Config, ModelProfile
from crew.tools.registry import Registry


def _profile(
    model_id: str,
    *,
    capabilities: list[str] | None = None,
    builtin: bool = False,
) -> ModelProfile:
    return ModelProfile(
        id=model_id,
        api_key=f"key-{model_id}",
        api_key_env=f"KEY_{model_id.upper()}",
        model=f"model-{model_id}",
        loaded=True,
        builtin=builtin,
        capabilities=capabilities or ["text", "tools"],
    )


def test_removing_non_active_global_model_clears_all_agent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(
        active_model_id="active",
        model_profiles={
            "active": _profile("active"),
            "removed": _profile("removed"),
            "other": _profile("other"),
        },
    )
    app = build_app(config=cfg, enable_team=False)
    app.agents = AgentManager(lambda _config, owner_account_id="": object())
    app.agents.get("bound-to-removed", owner_account_id="owner-a")
    app.agents.get("other-session", owner_account_id="owner-b")
    monkeypatch.setattr(cfg, "persist_model_profiles", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr("crew.app.remove_env_key", lambda *_args, **_kwargs: None)

    app.remove_model("removed")

    assert app.agents.peek("bound-to-removed", owner_account_id="owner-a") is None
    assert app.agents.peek("other-session", owner_account_id="owner-b") is None


def test_removing_owner_model_drops_only_that_owners_agent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _profile("shared", builtin=True)
    private = _profile("private")
    owner_profiles = {"shared": shared, "private": private}
    cfg = Config(active_model_id="shared", model_profiles={"shared": shared})
    app = build_app(config=cfg, enable_team=False)
    app.agents = AgentManager(lambda _config, owner_account_id="": object())
    app.agents.get("owner-session", owner_account_id="owner-a")
    other_agent = app.agents.get("other-session", owner_account_id="owner-b")
    monkeypatch.setattr(
        app,
        "owner_model_profiles",
        lambda owner="": owner_profiles if owner == "owner-a" else {"shared": shared},
    )
    monkeypatch.setattr(cfg, "owner_active_model_id", lambda _owner=None: "shared")
    monkeypatch.setattr(
        cfg,
        "persist_owner_model_profiles",
        lambda *_args, **_kwargs: tmp_path / "owner-config.yaml",
    )
    monkeypatch.setattr("crew.app.remove_env_key", lambda *_args, **_kwargs: None)

    app.remove_model("private", owner_account_id="owner-a")

    assert app.agents.peek("owner-session", owner_account_id="owner-a") is None
    assert app.agents.peek("other-session", owner_account_id="owner-b") is other_agent


def test_inherited_text_only_subagent_cannot_use_browser_vision() -> None:
    cfg = Config(
        active_model_id="text",
        model_profiles={"text": _profile("text", capabilities=["text", "tools"])},
    )
    app = build_app(config=cfg, enable_team=False)
    app.browser_manager.driver.available = lambda: True
    token = current_model_capabilities.set(("text", "tools"))
    try:
        child = app._make_subagent({"model": "inherit"})
    finally:
        current_model_capabilities.reset(token)

    assert "browser_use" in (child.tool_filter or [])
    assert "browser_vision" not in (child.tool_filter or [])
    assert child.model_capabilities == ("text", "tools")


def test_explicit_no_tools_subagent_has_empty_tool_filter() -> None:
    no_tools = _profile("plain", capabilities=["text"])
    cfg = Config(active_model_id="plain", model_profiles={"plain": no_tools})
    app = build_app(config=cfg, enable_team=False)

    child = app._make_subagent({"model": "plain"})

    assert child.tool_filter == []
    assert child.model_capabilities == ("text",)


@pytest.mark.asyncio
async def test_executor_rejects_tool_outside_effective_schema() -> None:
    registry = Registry()
    executions = 0

    async def forbidden_handler(_args):
        nonlocal executions
        executions += 1
        return "should not run"

    registry.register(
        name="forbidden_tool",
        toolset="test",
        schema={"name": "forbidden_tool", "parameters": {"type": "object"}},
        handler=forbidden_handler,
        is_async=True,
    )
    provider = FakeProvider(
        script=[
            ChatResponse(tool_calls=[ToolCall("tc-1", "forbidden_tool", {})]),
            ChatResponse(text="done", finish_reason="stop"),
        ]
    )
    executor = BuiltinExecutor(
        provider,
        registry,
        PluginManager(),
        max_iterations=3,
        backoff_seconds=0,
    )
    ctx = ExecutionContext(
        session_id="session",
        request_id="request",
        system_prompt="system",
        messages=[Message.user("test")],
        query="test",
        tool_schemas=[],
        enforce_tool_scope=True,
    )

    chunks = [chunk async for chunk in executor.execute(ctx)]

    assert executions == 0
    assert any(
        message.role == "tool" and "允许范围" in message.content
        for message in ctx.messages
    )
    assert chunks[-1].kind == "final"
