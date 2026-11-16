"""Final continuable hardening: identity proof and activation lifecycle."""

from __future__ import annotations

import pytest
import json

from crew.agent.subagent.providers import InProcessSpawnProvider
from crew.agent.subagent.runtime import SubagentRuntime
from crew.agent.subagent.service import StartRequest
from crew.core.envelope import ResponseChunk
from crew.agent.subagent.store import SubagentStore


class _Child:
    async def run(self, envelope):
        yield ResponseChunk.final("child", f"ok:{envelope.query}")

    async def aclose(self):
        return None


def _runtime(tmp_path):
    store = SubagentStore(tmp_path / "subagent.db")
    runtime = SubagentRuntime(
        InProcessSpawnProvider(lambda _spec: _Child()),
        store=store,
        continuable_enabled=True,
    )
    return runtime, store


@pytest.mark.asyncio
async def test_cold_resume_rejects_descriptor_identity_mismatch(tmp_path):
    runtime, store = _runtime(tmp_path)
    result = await runtime.execute(
        StartRequest(
            label="private",
            goal_text="first",
            parent_session_id="parent-a",
            owner_account_id="owner-a",
            lifecycle="continuable",
        )
    )
    descriptor = store.read_descriptor(result.subagent_id)
    assert descriptor is not None

    descriptor["parent_session_id"] = "forged-parent"
    store._writer.execute(  # test-only corruption of the persisted proof
        lambda conn: conn.execute(
            "UPDATE subagent_descriptors SET payload = ? WHERE subagent_id = ?",
            (json.dumps(descriptor), result.subagent_id),
        )
    )

    with pytest.raises(PermissionError):
        await runtime.send_message(
            subagent_id=result.subagent_id,
            message="must not resume",
            parent_session_id="parent-a",
            owner_account_id="owner-a",
        )
    await runtime.aclose()
    store.close()


@pytest.mark.asyncio
async def test_activation_events_cover_running_ready_idle(tmp_path):
    runtime, store = _runtime(tmp_path)
    result = await runtime.execute(
        StartRequest(
            label="events",
            goal_text="first",
            parent_session_id="parent-events",
            owner_account_id="owner-events",
            lifecycle="continuable",
        )
    )
    first = runtime.drain_lifecycle_events(
        owner_account_id="owner-events", session_id="parent-events"
    )
    assert [item["event"] for item in first] == [
        "run_started",
        "activation_running",
        "run_settled",
        "activation_idle",
    ]

    queued = await runtime.send_message(
        subagent_id=result.subagent_id,
        message="queue this",
        parent_session_id="parent-events",
        owner_account_id="owner-events",
        trigger_turn=False,
    )
    assert queued["state"] == "ready"
    ready = runtime.drain_lifecycle_events(
        owner_account_id="owner-events", session_id="parent-events"
    )
    assert [item["event"] for item in ready] == ["activation_ready"]

    resumed = await runtime.send_message(
        subagent_id=result.subagent_id,
        message="run this",
        parent_session_id="parent-events",
        owner_account_id="owner-events",
    )
    assert resumed["state"] == "idle"
    resumed_events = runtime.drain_lifecycle_events(
        owner_account_id="owner-events", session_id="parent-events"
    )
    assert [item["event"] for item in resumed_events] == [
        "activation_running",
        "run_started",
        "run_settled",
        "activation_idle",
    ]
    await runtime.aclose()
    store.close()
