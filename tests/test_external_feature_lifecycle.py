"""4A-2 external Provider/Run generation lifecycle contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crew.agent.external.feature import (
    AGENT_RUNTIME_PROVIDER_SERVICE_KEY,
    AdapterRuntimeProvider,
    DELEGATION_SERVICE_KEY,
    ExternalRunError,
    build_external_agent_feature,
)
from crew.agent.external.runtime_adapter import ExternalStreamEvent, RuntimeExecutionRequest
from crew.agent.subagent.feature import build_subagent_feature
from crew.agent.subagent.runtime import SubagentRuntime
from crew.agent.subagent.service import StartRequest, UnknownProviderError
from crew.core.runctx import current_owner_account_id
from crew.features import (
    FeatureRuntime,
    FeatureState,
)


def _request() -> RuntimeExecutionRequest:
    return RuntimeExecutionRequest(executable_path="fake", provider="fake", prompt="hello")


class _Adapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    def stream(self, _request):
        async def events():
            try:
                self.started.set()
                yield ExternalStreamEvent(kind="text", text="first")
                await self.release.wait()
                yield ExternalStreamEvent(kind="text", text="last")
            finally:
                self.closed.set()

        return events()


class _Catalog:
    def __init__(self, agents: tuple[dict[str, object], ...] = ()) -> None:
        self.close_calls = 0
        self.agents = agents
        self.list_calls: list[tuple[str, bool]] = []
        self.resolve_calls: list[tuple[str, str]] = []

    def list_agents(
        self,
        *,
        owner_account_id: str,
        include_managed: bool = True,
    ) -> list[dict[str, object]]:
        self.list_calls.append((owner_account_id, include_managed))
        return [dict(agent) for agent in self.agents]

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        self.resolve_calls.append((agent_id, owner_account_id))
        agent = next(
            (
                item
                for item in self.agents
                if item.get("id") == agent_id
                and item.get("owner_account_id", owner_account_id)
                == owner_account_id
            ),
            None,
        )
        if agent is None:
            raise KeyError(agent_id)
        return dict(agent), {"id": "runtime-a"}

    async def close(self) -> None:
        self.close_calls += 1


class _Provider:
    def __init__(self, adapter: _Adapter | None = None) -> None:
        self.adapter = adapter or _Adapter()
        self.close_calls = 0
        self.open_calls = 0

    async def open_run(self, _request):
        self.open_calls += 1
        return await AdapterRuntimeProvider(lambda _request: self.adapter).open_run(_request)

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_adapter_run_closes_iterator_and_cannot_be_reentered():
    adapter = _Adapter()
    adapter.release.set()
    run = await AdapterRuntimeProvider(lambda _request: adapter).open_run(_request())
    assert [event.text async for event in run.stream()] == ["first", "last"]
    assert adapter.closed.is_set()
    with pytest.raises(ExternalRunError, match="closed"):
        async for _event in run.stream():
            pass
    await run.close()


@pytest.mark.asyncio
async def test_provider_close_cancels_in_flight_run_and_releases_adapter_iterator():
    adapter = _Adapter()
    provider = AdapterRuntimeProvider(lambda _request: adapter)
    run = await provider.open_run(_request())
    stream = asyncio.create_task(run.stream().__anext__())
    await adapter.started.wait()
    assert (await stream).text == "first"
    await provider.close()
    await asyncio.wait_for(adapter.closed.wait(), timeout=1)
    with pytest.raises(ExternalRunError):
        await run.stream().__anext__()


@pytest.mark.asyncio
async def test_concurrent_provider_and_run_close_waits_for_one_iterator_cleanup():
    adapter = _Adapter()
    provider = AdapterRuntimeProvider(lambda _request: adapter)
    run = await provider.open_run(_request())
    reader = asyncio.create_task(run.stream().__anext__())
    await adapter.started.wait()
    assert (await reader).text == "first"

    await asyncio.gather(provider.close(), run.close())
    await asyncio.wait_for(adapter.closed.wait(), timeout=1)
    assert reader.done()


@pytest.mark.asyncio
async def test_feature_deactivation_closes_unconsumed_run():
    provider = _Provider()
    catalog = _Catalog()
    host = SimpleNamespace(external_agents=None)
    runtime = FeatureRuntime()
    bundle = build_external_agent_feature(
        host,
        catalog_factory=lambda: catalog,
        provider_factory=lambda: provider,
        own_catalog=True,
        own_provider=True,
    )
    await runtime.activate(bundle.definition)
    delegation = runtime.services.get(DELEGATION_SERVICE_KEY)
    run = await delegation.submit(_request())
    assert await runtime.deactivate(bundle.definition.feature_id) is True
    with pytest.raises(ExternalRunError):
        await run.stream().__anext__()
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_external_feature_update_uses_fresh_provider_and_catalog_and_deactivates_admission():
    providers: list[_Provider] = []
    catalogs: list[_Catalog] = []

    def provider_factory():
        provider = _Provider()
        providers.append(provider)
        return provider

    def catalog_factory():
        catalog = _Catalog()
        catalogs.append(catalog)
        return catalog

    host = SimpleNamespace(external_agents=None)
    runtime = FeatureRuntime()
    first = build_external_agent_feature(
        host,
        catalog_factory=catalog_factory,
        provider_factory=provider_factory,
        own_catalog=True,
        own_provider=True,
    )
    record = await runtime.activate(first.definition)
    assert record.state is FeatureState.ACTIVE
    first_provider = runtime.services.get(AGENT_RUNTIME_PROVIDER_SERVICE_KEY)

    second = build_external_agent_feature(
        host,
        catalog_factory=catalog_factory,
        provider_factory=provider_factory,
        own_catalog=True,
        own_provider=True,
        desired_config_revision=2,
    )
    result = await runtime.update(second.definition)
    assert result.updated is True
    assert runtime.services.get(AGENT_RUNTIME_PROVIDER_SERVICE_KEY) is not first_provider
    assert providers[0].close_calls == 1
    assert catalogs[0].close_calls == 1

    delegation = runtime.services.get(DELEGATION_SERVICE_KEY)
    await runtime.deactivate(first.definition.feature_id)
    with pytest.raises(ExternalRunError, match="closed"):
        await delegation.submit(_request())
    assert providers[1].close_calls == 1
    assert catalogs[1].close_calls == 1


@pytest.mark.asyncio
async def test_external_feature_failed_activation_closes_candidate_resources():
    provider = _Provider()
    catalog = _Catalog()
    host = SimpleNamespace(external_agents=None)
    runtime = FeatureRuntime()
    bundle = build_external_agent_feature(
        host,
        catalog_factory=lambda: catalog,
        provider_factory=lambda: provider,
        own_catalog=True,
        own_provider=True,
    )

    # The installer must fail after staging resources, while the candidate
    # generation still owns both resources and therefore must release them.
    from crew.agent.external.feature import DELEGATION_SERVICE_KEY
    definition = bundle.definition
    original = definition.install

    async def broken_install(context):
        await original(context)
        raise RuntimeError("activation failed")

    definition = type(definition)(
        definition.feature_id,
        broken_install,
        dependencies=definition.dependencies,
        desired_config_revision=definition.desired_config_revision,
        stop_policy=definition.stop_policy,
        update_strategy=definition.update_strategy,
    )
    record = await runtime.activate(definition)
    assert record.state is FeatureState.FAILED
    assert runtime.services.get(DELEGATION_SERVICE_KEY) is None
    assert provider.close_calls == 1
    assert catalog.close_calls == 1


@pytest.mark.asyncio
async def test_external_feature_optionally_registers_owner_aware_provider_family():
    service = SubagentRuntime()
    catalog = _Catalog((
        {"id": "agent-a", "owner_account_id": "owner-a"},
        {"id": "agent-b", "owner_account_id": "owner-b"},
    ))
    runtime = FeatureRuntime()
    await runtime.activate(build_subagent_feature(service).definition)
    bundle = build_external_agent_feature(
        SimpleNamespace(external_agents=None),
        catalog=catalog,
        provider=_Provider(),
    )

    record = await runtime.activate(bundle.definition)

    assert record.state is FeatureState.ACTIVE
    assert catalog.list_calls == []
    assert service.list_providers() == []
    owner_token = current_owner_account_id.set("owner-a")
    try:
        assert service.get_provider("external:agent-a") is not None
        assert service.get_provider("external:agent-b") is None
        catalog.agents += ({
            "id": "agent-created-later",
            "owner_account_id": "owner-a",
        },)
        assert service.get_provider("external:agent-created-later") is not None
        assert service.get_provider("external:missing") is None
        assert service.get_provider("other:agent-a") is None
    finally:
        current_owner_account_id.reset(owner_token)

    await runtime.deactivate(bundle.definition.feature_id)
    assert service.get_provider("external:agent-a") is None


@pytest.mark.asyncio
async def test_external_feature_stop_removes_providers_and_new_subagent_start_fails_closed():
    service = SubagentRuntime()
    catalog = _Catalog(({"id": "agent-a"},))
    runtime = FeatureRuntime()
    await runtime.activate(build_subagent_feature(service).definition)
    bundle = build_external_agent_feature(
        SimpleNamespace(external_agents=None),
        catalog=catalog,
        provider=_Provider(),
    )
    await runtime.activate(bundle.definition)
    assert service.get_provider("external:agent-a") is not None

    assert await runtime.deactivate(bundle.definition.feature_id) is True
    assert service.get_provider("external:agent-a") is None
    with pytest.raises(UnknownProviderError, match="external:agent-a"):
        await service.start(
            "external:agent-a",
            StartRequest(goal_text="this must not run"),
        )
