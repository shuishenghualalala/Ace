"""Core Agent Factory contract and lifecycle tests."""

from __future__ import annotations

import pytest

from crew.agent.feature import (
    AGENT_FACTORY_FEATURE_ID,
    AGENT_FACTORY_SERVICE_KEY,
    InProcessAgentFactory,
    build_agent_factory_feature,
)
from crew.core.interfaces import (
    Agent,
    AgentModelResolution,
    ChildAgentRequest,
)
from crew.features import FeatureRuntime, FeatureState


class _Agent(Agent):
    async def run(self, envelope):
        if False:
            yield envelope


class _Host:
    agent_factory = None

    def __init__(self):
        self.requests: list[ChildAgentRequest] = []

    def _build_subagent(self, request: ChildAgentRequest) -> Agent:
        self.requests.append(request)
        return _Agent()


def test_child_agent_request_is_typed_and_legacy_conversion_is_defensive():
    request = ChildAgentRequest.from_mapping(
        {
            "system_prompt": "child",
            "toolsets": "file",
            "tools": ["file_read"],
            "model": "inherit",
            "skills": ["python"],
            "_preset_spec": {"system_prompt": "untrusted"},
        }
    )
    assert request.toolsets == ("file",)
    assert request.tools == ("file_read",)
    payload = request.to_mapping()
    payload["toolsets"].append("terminal")
    assert request.toolsets == ("file",)
    assert request.preset_spec is None
    trusted = ChildAgentRequest.from_mapping(
        {"preset_name": "reviewer"},
        trusted_preset_spec={"system_prompt": "trusted"},
    )
    assert trusted.preset_spec == {"system_prompt": "trusted"}


def test_in_process_factory_resolves_and_builds_typed_child_request():
    seen: list[ChildAgentRequest] = []

    def build(request: ChildAgentRequest) -> Agent:
        seen.append(request)
        return _Agent()

    factory = InProcessAgentFactory(
        build,
        lambda request: AgentModelResolution(
            requested_model=request.model,
            effective_model="resolved",
            inherited=request.model == "inherit",
        ),
    )
    request = ChildAgentRequest(model="inherit")
    assert isinstance(factory.create_child(request), Agent)
    assert seen == [request]
    assert factory.resolve_model(request).effective_model == "resolved"
    with pytest.raises(TypeError, match="ChildAgentRequest"):
        factory.create_child({})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_factory_feature_publishes_and_removes_generation_service():
    host = _Host()
    runtime = FeatureRuntime()
    bundle = build_agent_factory_feature(host)
    record = await runtime.activate(bundle.definition)

    assert record.state is FeatureState.ACTIVE
    service = runtime.services.get(AGENT_FACTORY_SERVICE_KEY)
    assert service is host.agent_factory
    request = ChildAgentRequest(system_prompt="hello")
    assert service.create_child(request) is not None
    assert host.requests == [request]

    assert await runtime.deactivate(AGENT_FACTORY_FEATURE_ID, allow_required_by_product=True)
    assert runtime.services.get(AGENT_FACTORY_SERVICE_KEY) is None
    assert host.agent_factory is None


def test_build_app_exposes_core_agent_factory_service():
    from crew.app import build_app

    app = build_app()
    try:
        records = {
            record.definition.feature_id: record
            for record in app.plugins.feature_runtime.records
        }
        record = records[AGENT_FACTORY_FEATURE_ID]
        assert record.state is FeatureState.ACTIVE
        assert app.plugins.feature_runtime.services.get(AGENT_FACTORY_SERVICE_KEY) is app.agent_factory
    finally:
        app._close_persistent_stores()
