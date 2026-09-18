"""Stage E contracts: fork snapshots, structured output and preset policy."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from crew.agent.subagent.definition import parse_definition
from crew.agent.subagent.policy import apply_preset_tool_policy
from crew.agent.subagent.providers import (
    INPROCESS_FORK_PROVIDER,
    InProcessForkProvider,
    InProcessSpawnProvider,
    fork_completed_prefix,
)
from crew.agent.subagent.tools import (
    ActiveSubagents,
    _available_presets,
    build_structured_output_schema,
    handle_structured_output,
)
from crew.app import CrewApp
from crew.agent.subagent.runtime import SubagentRuntime
from crew.agent.subagent.service import StartRequest
from crew.core.envelope import ResponseChunk
from crew.core.types import Message
from crew.state.session_store import SQLiteSessionStore


class _StructuredChild:
    def __init__(self, *, payload: Any, raw: str = "") -> None:
        self.payload = payload
        self.raw = raw

    async def run(self, _envelope: Any):
        yield ResponseChunk.final("child", self.raw, structured=self.payload)

    async def aclose(self) -> None:
        return None


class _StructuredToolChild:
    async def run(self, _envelope: Any):
        yield ResponseChunk.tool_event("child", "structured_output", "start")
        handle_structured_output({"value": {"answer": "from-tool"}})
        yield ResponseChunk.tool_event("child", "structured_output", "result")
        raise AssertionError("provider should close the child after a successful submission")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_inprocess_structured_output_is_validated_and_published():
    runtime = SubagentRuntime(
        InProcessSpawnProvider(
            lambda _spec: _StructuredChild(payload={"answer": "ok"})
        )
    )

    result = await runtime.execute(
        StartRequest(
            label="structured",
            goal_text="return data",
            output_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    )

    assert result.structured == {"answer": "ok"}
    assert result.stop_reason == "completed"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_inprocess_structured_output_rejects_invalid_payload():
    runtime = SubagentRuntime(
        InProcessSpawnProvider(
            lambda _spec: _StructuredChild(payload={"answer": 7})
        )
    )

    result = await runtime.execute(
        StartRequest(
            label="structured-invalid",
            goal_text="return data",
            output_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
        )
    )

    assert result.stop_reason == "error"
    assert "结构化输出校验失败" in result.diagnostic
    assert result.structured is None
    await runtime.aclose()


def test_structured_output_tool_is_child_scoped_and_schema_shaped():
    schema = build_structured_output_schema()
    assert schema["name"] == "structured_output"
    assert "value" in schema["parameters"]["properties"]
    assert "only available" in handle_structured_output({"value": {}})


@pytest.mark.asyncio
async def test_structured_output_tool_submits_once_and_ends_the_child_run():
    runtime = SubagentRuntime(InProcessSpawnProvider(lambda _spec: _StructuredToolChild()))

    result = await runtime.execute(
        StartRequest(
            label="structured-tool",
            goal_text="submit data",
            output_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
        )
    )

    assert result.stop_reason == "completed"
    assert result.structured == {"answer": "from-tool"}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_structured_output_can_be_recovered_from_json_text():
    runtime = SubagentRuntime(
        InProcessSpawnProvider(
            lambda _spec: _StructuredChild(
                payload=None,
                raw=json.dumps({"answer": "from-text"}),
            )
        )
    )

    result = await runtime.execute(
        StartRequest(
            label="structured-text",
            goal_text="return data",
            output_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
        )
    )

    assert result.structured == {"answer": "from-text"}
    await runtime.aclose()


def test_fork_completed_prefix_filters_meta_and_incomplete_tail():
    messages = [
        Message.user("question"),
        Message.system_reminder("internal"),
        Message.assistant("answer"),
        Message.user("unfinished follow-up"),
    ]

    prefix = fork_completed_prefix(messages)

    assert [message.content for message in prefix] == ["question", "answer"]
    assert all(not message.is_meta for message in prefix)
    assert messages[1].source_kind == "system_reminder"


def test_gateway_active_children_view_is_owner_scoped():
    active = ActiveSubagents()
    active.register(
        "shared-session",
        "child-a",
        {"child_id": "child-a", "owner_account_id": "owner-a"},
    )
    active.register(
        "shared-session",
        "child-b",
        {"child_id": "child-b", "owner_account_id": "owner-b"},
    )

    app = object.__new__(CrewApp)
    app.team = SimpleNamespace(
        active_children=lambda _session_id, *, owner_account_id: [],
    )
    app.subagent_active = active

    assert app._active_children_snapshot(
        "shared-session",
        "owner-a",
    ) == [{"child_id": "child-a", "owner_account_id": "owner-a"}]


@pytest.mark.asyncio
async def test_fork_provider_seeds_child_with_completed_parent_prefix(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "sessions.db"))
    owner = "owner-e"
    parent = "parent-e"
    store.save(
        parent,
        [
            Message.user("question"),
            Message.system_reminder("hidden"),
            Message.assistant("answer"),
            Message.user("unfinished"),
        ],
        owner_account_id=owner,
    )
    observed: list[list[Message]] = []

    class Child:
        async def run(self, envelope: Any):
            observed.append(
                await store.load_async(envelope.session_id, owner_account_id=owner)
            )
            yield ResponseChunk.final("child", "forked")

        async def aclose(self) -> None:
            return None

    runtime = SubagentRuntime(
        InProcessForkProvider(lambda _spec: Child(), session_store=store)
    )
    result = await runtime.execute(
        StartRequest(
            label="forked",
            goal_text="continue from the snapshot",
            provider=INPROCESS_FORK_PROVIDER,
            parent_session_id=parent,
            owner_account_id=owner,
            workspace_id="workspace-e",
        )
    )

    assert result.output == "forked"
    assert [message.content for message in observed[0]] == ["question", "answer"]
    await runtime.aclose()
    store.close()


def test_read_only_preset_policy_removes_write_capabilities():
    class Catalog:
        def names_for_toolset(self, toolset: str) -> list[str]:
            return {"terminal": ["terminal"], "file": ["file_read", "file_write"]}.get(
                toolset, []
            )

        def toolset_for(self, name: str) -> str | None:
            return "terminal" if name == "terminal" else "file"

    names = apply_preset_tool_policy(
        Catalog(),
        ["file_read", "file_write", "terminal"],
        {"read_only": True},
    )

    assert names == ["file_read"]


def test_preset_frontmatter_exposes_read_only_and_delegation_contract(tmp_path):
    path = tmp_path / "review.md"
    path.write_text(
        "---\n"
        "name: review\n"
        "description: review\n"
        "extends: Explore\n"
        "override: true\n"
        "read_only: true\n"
        "subagents: [verification]\n"
        "---\n"
        "Review the work.\n",
        encoding="utf-8",
    )

    definition = parse_definition(path, source="user")

    assert definition is not None
    assert definition.extends == "Explore"
    assert definition.override is True
    assert definition.read_only is True
    assert definition.delegatable_presets == ["verification"]


def test_user_preset_requires_explicit_override_for_builtin(monkeypatch, tmp_path):
    path = tmp_path / "Explore.md"
    path.write_text(
        "---\nname: Explore\ndescription: user version\n---\nUser prompt.\n",
        encoding="utf-8",
    )
    from crew.agent.subagent import registry as registry_module

    monkeypatch.setattr(registry_module, "get_user_agents_dir", lambda: tmp_path)
    registry = registry_module.SubagentRegistry()
    assert registry.get("Explore").description != "user version"

    path.write_text(
        "---\nname: Explore\ndescription: user version\noverride: true\n---\nUser prompt.\n",
        encoding="utf-8",
    )
    registry.reload()
    assert registry.get("Explore").description == "user version"


def test_preset_subagent_whitelist_narrows_visible_roster():
    from crew.core.runctx import current_agent_id

    class Service:
        def get_provider(self, _name: str, *, owner_account_id: str | None = None):
            return SimpleNamespace(capabilities=SimpleNamespace(agent_options=True))

    owner = SimpleNamespace(
        name="planner",
        agent_id="subagent:planner",
        delegatable_presets=("verification",),
        provider="inprocess-spawn",
    )
    verification = SimpleNamespace(name="verification", provider="inprocess-spawn")
    explore = SimpleNamespace(name="Explore", provider="inprocess-spawn")
    token = current_agent_id.set("subagent:planner")
    try:
        visible = _available_presets(Service(), [owner, verification, explore])
    finally:
        current_agent_id.reset(token)

    assert [item.name for item in visible] == ["verification"]
