"""Stage D: continuable identity, activation serialization and control tools."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from crew.agent.subagent.providers import InProcessSpawnProvider
from crew.agent.subagent.context import format_subagent_notifications
from crew.agent.subagent.runtime import SubagentRuntime
from crew.agent.subagent.service import StartRequest
from crew.core.envelope import ResponseChunk
from crew.agent.subagent.store import SubagentStore
from crew.app import build_app
from crew.state.config import Config


class _ContinuableChild:
    def __init__(self, prompts: list[str], prompt_log: list[str]) -> None:
        self._prompts = prompts
        self._prompt_log = prompt_log

    async def run(self, envelope: Any):
        self._prompt_log.append(str(envelope.query))
        self._prompts.append(str(envelope.query))
        yield ResponseChunk.final("child", f"received:{envelope.query}")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_continuable_identity_can_resume_and_recover_from_store(tmp_path):
    prompts: list[str] = []
    created: list[_ContinuableChild] = []

    def build_child(_spec: dict[str, Any]) -> _ContinuableChild:
        child = _ContinuableChild([], prompts)
        created.append(child)
        return child

    store = SubagentStore(tmp_path / "subagent.db")
    runtime = SubagentRuntime(
        InProcessSpawnProvider(build_child),
        store=store,
        continuable_enabled=True,
    )
    first = await runtime.execute(
        StartRequest(
            label="reviewer",
            goal_text="first turn",
            parent_session_id="parent-1",
            owner_account_id="owner-1",
            workspace_id="workspace-1",
            lifecycle="continuable",
            spec={"system_prompt": "review"},
        )
    )

    assert first.subagent_id
    subagent_id = first.subagent_id
    assert prompts == ["first turn"]
    assert runtime.list_agents(
        parent_session_id="parent-1", owner_account_id="owner-1"
    )[0]["state"] == "idle"

    queued = await runtime.send_message(
        subagent_id=subagent_id,
        message="queued context",
        parent_session_id="parent-1",
        owner_account_id="owner-1",
        trigger_turn=False,
    )
    assert queued == {
        "status": "queued",
        "subagent_id": subagent_id,
        "state": "ready",
    }

    resumed = await runtime.send_message(
        subagent_id=subagent_id,
        message="second turn",
        parent_session_id="parent-1",
        owner_account_id="owner-1",
    )
    assert resumed["status"] == "completed"
    assert resumed["subagent_id"] == subagent_id
    assert prompts[-1] == "queued context\n\nsecond turn"
    assert len(created) == 2

    await runtime.aclose()
    store.close()

    recovered_store = SubagentStore(tmp_path / "subagent.db")
    recovered_prompts: list[str] = []
    recovered_runtime = SubagentRuntime(
        InProcessSpawnProvider(
            lambda _spec: _ContinuableChild([], recovered_prompts)
        ),
        store=recovered_store,
        continuable_enabled=True,
    )
    recovered = await recovered_runtime.send_message(
        subagent_id=subagent_id,
        message="after restart",
        parent_session_id="parent-1",
        owner_account_id="owner-1",
    )
    assert recovered["subagent_id"] == subagent_id
    assert recovered_prompts == ["after restart"]
    await recovered_runtime.aclose()
    recovered_store.close()


@pytest.mark.asyncio
async def test_continuable_uses_hidden_canonical_session_id(tmp_path):
    observed: list[tuple[str, str, str]] = []

    class SessionAwareChild:
        async def run(self, envelope: Any):
            observed.append(
                (
                    envelope.session_id,
                    str(envelope.params.get("task_session_id") or ""),
                    str(envelope.params.get("subagent_id") or ""),
                )
            )
            yield ResponseChunk.final("child", "done")

        async def aclose(self) -> None:
            return None

    store = SubagentStore(tmp_path / "subagent.db")
    runtime = SubagentRuntime(
        InProcessSpawnProvider(lambda _spec: SessionAwareChild()),
        store=store,
        continuable_enabled=True,
    )
    result = await runtime.execute(
        StartRequest(
            label="reviewer",
            goal_text="hidden session",
            parent_session_id="parent-1",
            owner_account_id="owner-1",
            lifecycle="continuable",
        )
    )

    expected = f"parent-1::subagent::{result.subagent_id}"
    assert observed == [(expected, expected, result.subagent_id)]
    descriptor = store.read_descriptor(result.subagent_id)
    assert descriptor["session_persistence"] == "hidden"
    await runtime.aclose()
    store.close()


@pytest.mark.asyncio
async def test_continuable_control_plane_enforces_parent_and_owner(tmp_path):
    store = SubagentStore(tmp_path / "subagent.db")
    runtime = SubagentRuntime(
        InProcessSpawnProvider(lambda _spec: _ContinuableChild([], [])),
        store=store,
        continuable_enabled=True,
    )
    result = await runtime.execute(
        StartRequest(
            label="private",
            goal_text="private turn",
            parent_session_id="parent-a",
            owner_account_id="owner-a",
            lifecycle="continuable",
        )
    )

    with pytest.raises(PermissionError):
        await runtime.send_message(
            subagent_id=result.subagent_id,
            message="cross parent",
            parent_session_id="parent-b",
            owner_account_id="owner-a",
        )
    with pytest.raises(KeyError):
        await runtime.send_message(
            subagent_id=result.subagent_id,
            message="cross owner",
            parent_session_id="parent-a",
            owner_account_id="owner-b",
        )
    assert runtime.interrupt_agent(
        subagent_id=result.subagent_id,
        parent_session_id="parent-b",
        owner_account_id="owner-a",
    ) is False
    await runtime.aclose()
    store.close()


@pytest.mark.asyncio
async def test_continuable_activation_serializes_messages(tmp_path):
    store = SubagentStore(tmp_path / "subagent.db")
    active = 0
    peak = 0

    class SlowChild(_ContinuableChild):
        async def run(self, envelope: Any):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                yield ResponseChunk.final("child", envelope.query)
            finally:
                active -= 1

    runtime = SubagentRuntime(
        InProcessSpawnProvider(lambda _spec: SlowChild([], [])),
        store=store,
        continuable_enabled=True,
    )
    first = await runtime.execute(
        StartRequest(
            label="serialized",
            goal_text="first",
            parent_session_id="parent",
            owner_account_id="owner",
            lifecycle="continuable",
        )
    )
    await asyncio.gather(
        runtime.send_message(
            subagent_id=first.subagent_id,
            message="a",
            parent_session_id="parent",
            owner_account_id="owner",
        ),
        runtime.send_message(
            subagent_id=first.subagent_id,
            message="b",
            parent_session_id="parent",
            owner_account_id="owner",
        ),
    )
    assert peak == 1
    await runtime.aclose()
    store.close()


def test_continuable_feature_switch_controls_model_tools():
    disabled = build_app(config=Config(subagent_continuable=False))
    assert "send_message" not in disabled.registry.names()
    assert "list_agents" not in disabled.registry.names()
    assert "interrupt_agent" not in disabled.registry.names()

    enabled = build_app(config=Config(subagent_continuable=True))
    assert {"send_message", "list_agents", "interrupt_agent"}.issubset(
        enabled.registry.names()
    )


def test_settlement_notification_separates_agent_message_from_runtime_ledger():
    block = format_subagent_notifications([
        {
            "agent": "reviewer",
            "status": "completed",
            "duration_seconds": 1.2,
            "source": "subagent-settled",
            "agent_message": "已检查 3 个文件并修复测试。",
            "settlement": {
                "status": "completed",
                "stop_reason": "completed",
            },
        }
    ])

    assert "已检查 3 个文件并修复测试。" in block
    assert "stop_reason=completed" in block
