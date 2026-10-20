"""Stage C: Subagent Feature lifecycle, route and event ownership."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crew.agent.subagent.service import (
    StartRequest,
    SubagentCapabilities,
    SubagentResult,
    SubagentRun,
)
from crew.agent.subagent.tools import ActiveSubagents
from crew.app import build_app
from crew.core.envelope import Envelope
from crew.features import FeatureState
from crew.features.routes import RouteUnavailableError
from crew.gateway.app import create_app


class _ImmediateProvider:
    name = "stage-c-immediate"
    inherits_parent_context = False
    capabilities = SubagentCapabilities()

    async def start(self, _request):
        return SubagentRun(
            "stage-c-run",
            SubagentResult(output="stage-c complete"),
        )


@pytest.mark.asyncio
async def test_subagent_feature_events_are_versioned_and_drained_once():
    app = build_app()
    runtime = app.subagent_runtime
    runtime.register_provider(_ImmediateProvider())

    result = await runtime.execute(
        StartRequest(
            label="stage-c",
            goal_text="exercise lifecycle event",
            parent_session_id="parent-stage-c",
            owner_account_id="local",
            provider="stage-c-immediate",
        )
    )
    assert result.stop_reason == "completed"
    runs = runtime.list_runs(
        parent_session_id="parent-stage-c",
        owner_account_id="local",
    )
    subagent_id = runs[0]["subagent_id"]

    envelope = Envelope.of(
        "continue",
        session_id="parent-stage-c",
        user_id="local",
    )
    report = await app.event_contributors.dispatch(envelope, lambda _event: _noop())
    assert [event.as_body() for event in report.events] == [
        {
            "feature": "subagent",
            "event": "run_started",
            "version": 1,
            "payload": {
                "subagent_id": subagent_id,
                "label": "stage-c",
                "provider": "stage-c-immediate",
                "depth": 1,
                "lifecycle": "one_shot",
            },
        },
        {
            "feature": "subagent",
            "event": "run_settled",
            "version": 1,
            "payload": {
                "subagent_id": subagent_id,
                "stop_reason": "completed",
                "status": "settled",
            },
        },
    ]
    second = await app.event_contributors.dispatch(envelope, lambda _event: _noop())
    assert second.events == ()


async def _noop() -> None:
    return None


def test_active_subagent_snapshot_is_owner_scoped():
    active = ActiveSubagents()
    active.register(
        "shared-session",
        "child-a",
        {"child_id": "child-a", "owner_account_id": "A:uid-a"},
    )
    active.register(
        "shared-session",
        "child-b",
        {"child_id": "child-b", "owner_account_id": "B:uid-b"},
    )

    assert [item["child_id"] for item in active.snapshot(
        "shared-session",
        owner_account_id="A:uid-a",
    )] == ["child-a"]
    assert list(active.snapshot(owner_account_id="B:uid-b")) == ["shared-session"]
    assert active.snapshot("shared-session", owner_account_id="local") == []


@pytest.mark.asyncio
async def test_subagent_feature_stop_removes_route_and_compatibility_bindings():
    app = build_app()
    feature_runtime = app.plugins.feature_runtime

    runtime = app.subagent_runtime
    store = app.subagent_store
    assert runtime is not None
    assert store is not None
    assert app._subagent_notifications is not None
    assert feature_runtime.routes.resolve("product.subagent")

    assert await feature_runtime.deactivate(
        "product.subagent",
        allow_required_by_product=True,
    )

    record = feature_runtime.get("product.subagent")
    assert record is not None
    assert record.state is FeatureState.DISCOVERED
    assert app.subagent_runtime is None
    assert app.subagent_active is None
    assert app.subagent_store is None
    assert runtime._closed is True
    assert store._closed is True
    with pytest.raises(RouteUnavailableError):
        feature_runtime.routes.resolve("product.subagent")


def test_subagent_feature_route_is_mounted_through_gateway_registry():
    crew = build_app()
    gateway = create_app(crew)

    response = TestClient(gateway).get("/api/subagents")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["items"] == {}
    assert response.json()["runs"] == []
