"""Runtime/provider lifecycle contract tests for one-shot subagents."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from crew.agent.subagent.feature import SUBAGENT_FEATURE_ID, build_subagent_feature
from crew.agent.subagent.providers import InProcessSpawnProvider
from crew.agent.subagent.runtime import SubagentRuntime
from crew.agent.subagent.service import (
    DuplicateProviderError,
    ProviderRegistry,
    StartRequest,
    SubagentCapabilities,
    SubagentCapabilityError,
    SubagentPublicationError,
    SubagentResult,
    SubagentRun,
    UnknownProviderError,
    SUBAGENT_SERVICE_KEY,
)
from crew.agent.subagent.tools import ActiveSubagents
from crew.core.envelope import ResponseChunk
from crew.features import FeatureRuntime, FeatureState


class _Provider:
    """Minimal provider used to exercise the runtime seam without an Agent."""

    inherits_parent_context = False

    def __init__(
        self,
        name: str = "fake",
        *,
        capabilities: SubagentCapabilities | None = None,
        fail_labels: set[str] | None = None,
    ) -> None:
        self.name = name
        self.capabilities = capabilities or SubagentCapabilities()
        self.fail_labels = fail_labels or set()
        self.started: list[str] = []

    async def start(self, request: Any) -> SubagentRun:
        self.started.append(request.label)
        if request.label in self.fail_labels:
            raise RuntimeError(f"publication failed for {request.label}")
        return SubagentRun(
            f"run::{request.label}",
            SubagentResult(output=f"done:{request.label}"),
        )


class _ContinuableProvider(_Provider):
    async def prepare_continuable(self, request: Any) -> None:
        return None


class _Child:
    def __init__(self, mode: str, *, cleanup_error: bool = False) -> None:
        self.mode = mode
        self.cleanup_error = cleanup_error
        self.interrupt_messages: list[str | None] = []
        self.close_calls = 0

    async def run(self, _envelope: Any):
        if self.mode == "completed":
            yield ResponseChunk.final("child", "child result")
            return
        if self.mode == "error":
            yield ResponseChunk.error("child", "chunk failure")
            return
        if self.mode == "idle":
            yield ResponseChunk.delta("child", "partial")
            await asyncio.sleep(0.2)
            return
        if self.mode == "absolute":
            while True:
                yield ResponseChunk.delta("child", "progress")
                await asyncio.sleep(0.002)
        while True:
            await asyncio.sleep(1)
            yield ResponseChunk.delta("child", "waiting")

    def interrupt(self, message: str | None = None) -> None:
        self.interrupt_messages.append(message)

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.cleanup_error:
            raise RuntimeError("cleanup failed")


def _child_factory(spec: dict[str, Any]) -> _Child:
    return _Child(str(spec.get("mode", "completed")))


def _request(label: str = "child", **kwargs: Any) -> StartRequest:
    return StartRequest(
        label=label,
        goal_text="do the work",
        parent_session_id="parent-session",
        spec=kwargs.pop("spec", {}),
        **kwargs,
    )


def test_provider_registry_rejects_duplicate_and_unknown_providers():
    registry = ProviderRegistry()
    first = _Provider("duplicate")
    second = _Provider("duplicate")

    registry.register(first)
    with pytest.raises(DuplicateProviderError, match="already registered"):
        registry.register(second)
    with pytest.raises(UnknownProviderError, match="missing"):
        registry.require("missing")


def test_provider_registry_dynamic_family_is_lazy_scoped_and_conflict_safe():
    registry = ProviderRegistry()
    created: list[tuple[str, str | None]] = []

    def resolve(name: str, owner_account_id: str | None):
        if not name.startswith("dynamic:"):
            return None
        created.append((name, owner_account_id))
        return _Provider(name)

    dispose = registry.register_resolver(resolve)

    assert registry.names() == []
    assert registry.get("dynamic:a", owner_account_id="owner-a").name == "dynamic:a"
    assert created == [("dynamic:a", "owner-a")]
    assert registry.get("other:a") is None

    other_dispose = registry.register_resolver(
        lambda name, _owner: _Provider(name) if name.startswith("dynamic:") else None
    )
    with pytest.raises(DuplicateProviderError, match="multiple"):
        registry.get("dynamic:a")

    other_dispose()
    dispose()
    assert registry.get("dynamic:a") is None


@pytest.mark.asyncio
async def test_runtime_passes_request_owner_to_dynamic_provider_resolution():
    registry = ProviderRegistry()
    resolved_owners: list[str | None] = []

    def resolve(name: str, owner_account_id: str | None):
        resolved_owners.append(owner_account_id)
        return _Provider(name) if name == "dynamic:a" else None

    registry.register_resolver(resolve)
    runtime = SubagentRuntime(provider_registry=registry)

    run = await runtime.start(
        _request(provider="dynamic:a", owner_account_id="owner-a")
    )
    await run.result
    await run.dispose()

    assert resolved_owners == ["owner-a"]


@pytest.mark.asyncio
async def test_runtime_unknown_provider_fails_loudly():
    runtime = SubagentRuntime(provider_registry=ProviderRegistry())

    with pytest.raises(UnknownProviderError, match="missing"):
        await runtime.start("missing", _request())


def test_explicit_child_depth_cannot_roll_back_parent_depth():
    request = _request(parent_depth=2, depth=1)

    assert request.child_depth == 3


@pytest.mark.parametrize(
    ("field", "value", "capability"),
    [
        ("output_schema", {"type": "object"}, "output_schema"),
        ("max_depth", 2, "depth_limit"),
        ("tool_filter", ["file_read"], "tool_filter"),
        ("persona", "reviewer", "persona"),
        ("agent_options", {"temperature": 0}, "agent_options"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_rejects_each_unsupported_capability(
    field: str,
    value: Any,
    capability: str,
):
    provider = _Provider("limited")
    runtime = SubagentRuntime(provider_registry=ProviderRegistry({"limited": provider}))
    request = _request(provider="limited", **{field: value})

    with pytest.raises(SubagentCapabilityError) as raised:
        await runtime.start(request)

    assert raised.value.provider == "limited"
    assert raised.value.capability == capability
    assert provider.started == []


@pytest.mark.asyncio
async def test_continuable_lifecycle_is_checked_by_prepare_method_presence():
    one_shot_provider = _Provider("one-shot")
    one_shot_runtime = SubagentRuntime(
        provider_registry=ProviderRegistry({"one-shot": one_shot_provider})
    )

    with pytest.raises(SubagentCapabilityError, match="continuable"):
        await one_shot_runtime.start(
            _request(provider="one-shot", lifecycle="continuable"),
        )

    continuable_provider = _ContinuableProvider("continuable")
    continuable_runtime = SubagentRuntime(
        provider_registry=ProviderRegistry({"continuable": continuable_provider})
    )
    run = await continuable_runtime.start(
        _request(provider="continuable", lifecycle="continuable"),
    )
    try:
        result = await run.result
        assert result.output == "done:child"
    finally:
        await run.dispose()


@pytest.mark.asyncio
async def test_child_creation_failure_has_no_active_residue():
    active = ActiveSubagents()

    def fail_to_create(_spec: dict[str, Any]) -> _Child:
        raise RuntimeError("factory failed")

    runtime = SubagentRuntime(build_child=fail_to_create, active=active)

    with pytest.raises(SubagentPublicationError, match="failed to publish"):
        await runtime.start(_request(spec={"mode": "completed"}))

    assert active.snapshot("parent-session") == []


@pytest.mark.asyncio
async def test_active_publication_failure_closes_child_and_leaves_no_residue():
    child = _Child("completed")

    class BrokenActive(ActiveSubagents):
        def register(self, parent_session_id, child_id, record):
            super().register(parent_session_id, child_id, record)
            raise RuntimeError("active registry failed")

    active = BrokenActive()
    runtime = SubagentRuntime(build_child=lambda _spec: child, active=active)

    with pytest.raises(SubagentPublicationError, match="active registry failed"):
        await runtime.start(_request(spec={"mode": "completed"}))

    assert active.snapshot("parent-session") == []
    assert child.close_calls == 1


@pytest.mark.asyncio
async def test_in_process_provider_applies_supported_request_options():
    captured: list[dict[str, Any]] = []

    def build(spec: dict[str, Any]) -> _Child:
        captured.append(spec)
        return _Child("completed")

    runtime = SubagentRuntime(build_child=build)
    result = await runtime.execute(
        _request(
            tool_filter=["file_read"],
            agent_options={"model": "fast", "max_iterations": 7},
        )
    )

    assert result.stop_reason == "completed"
    assert captured == [{
        "toolsets": None,
        "tools": ["file_read"],
        "model": "fast",
        "max_iterations": 7,
    }]


@pytest.mark.asyncio
async def test_in_process_provider_rejects_unknown_agent_options():
    runtime = SubagentRuntime(build_child=_child_factory)

    with pytest.raises(ValueError, match="unsupported.*temperature"):
        await runtime.start(_request(agent_options={"temperature": 0}))


@pytest.mark.asyncio
async def test_published_chunk_error_is_a_subagent_result():
    active = ActiveSubagents()
    runtime = SubagentRuntime(build_child=_child_factory, active=active)
    run = await runtime.start(_request(spec={"mode": "error"}))

    result = await run.result

    assert isinstance(result, SubagentResult)
    assert result.stop_reason == "error"
    assert result.diagnostic == "chunk failure"
    assert result.status == "error"
    assert active.snapshot("parent-session") == []
    await run.dispose()


@pytest.mark.asyncio
async def test_idle_timeout_preserves_machine_readable_timeout_kind():
    runtime = SubagentRuntime(
        build_child=_child_factory,
        idle_timeout=0.01,
        max_runtime=1.0,
    )
    run = await runtime.start(_request(spec={"mode": "idle"}))

    result = await run.result

    assert result.stop_reason == "timed_out"
    assert result.timeout_kind == "idle"
    assert result.status == "timeout"
    assert result.partial_output == "partial"
    await run.dispose()


@pytest.mark.asyncio
async def test_absolute_timeout_wins_over_continuous_progress():
    runtime = SubagentRuntime(
        build_child=_child_factory,
        idle_timeout=1.0,
        max_runtime=0.03,
    )
    run = await runtime.start(_request(spec={"mode": "absolute"}))

    result = await run.result

    assert result.stop_reason == "timed_out"
    assert result.timeout_kind == "absolute"
    assert result.status == "timeout"
    await run.dispose()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_overwrite_terminal_result():
    child = _Child("completed", cleanup_error=True)
    active = ActiveSubagents()
    runtime = SubagentRuntime(build_child=lambda _spec: child, active=active)
    run = await runtime.start(_request(spec={"mode": "completed"}))

    result = await run.result

    assert result.stop_reason == "completed"
    assert result.output == "child result"
    assert child.close_calls == 1
    assert active.snapshot("parent-session") == []
    await run.dispose()


@pytest.mark.asyncio
async def test_runtime_cleanup_failure_does_not_overwrite_terminal_result():
    async def fail_cleanup() -> None:
        raise RuntimeError("dispose failed")

    provider = _Provider("cleanup")

    async def start_with_bad_dispose(request: Any) -> SubagentRun:
        return SubagentRun(
            f"run::{request.label}",
            SubagentResult(output="kept"),
            dispose=fail_cleanup,
        )

    provider.start = start_with_bad_dispose  # type: ignore[method-assign]
    runtime = SubagentRuntime(provider_registry=ProviderRegistry({"cleanup": provider}))

    result = await runtime.execute(_request(provider="cleanup"))

    assert result == SubagentResult(output="kept")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            SubagentResult(partial_output="delta only"),
            {
                "agent": "worker", "status": "completed", "summary": "",
                "duration_seconds": 0.0, "tool_calls": 0, "last_tool": "",
            },
        ),
        (
            SubagentResult(stop_reason="error", diagnostic="chunk failure"),
            {
                "agent": "worker", "status": "error",
                "summary": "chunk failure\n部分输出：\nchunk failure",
                "duration_seconds": 0.0, "tool_calls": 0, "last_tool": "",
            },
        ),
        (
            SubagentResult(output="truncated", stop_reason="max_tokens"),
            {
                "agent": "worker", "status": "completed", "summary": "truncated",
                "duration_seconds": 0.0, "tool_calls": 0, "last_tool": "",
            },
        ),
        (
            SubagentResult(output="declined", stop_reason="refusal"),
            {
                "agent": "worker", "status": "completed", "summary": "declined",
                "duration_seconds": 0.0, "tool_calls": 0, "last_tool": "",
            },
        ),
    ],
)
def test_legacy_result_edge_shapes_remain_exact(result, expected):
    assert result.to_legacy_dict(agent="worker") == expected


@pytest.mark.asyncio
async def test_subagent_run_concurrent_and_repeated_dispose_is_idempotent():
    dispose_calls = 0

    async def dispose() -> None:
        nonlocal dispose_calls
        await asyncio.sleep(0)
        dispose_calls += 1

    run = SubagentRun(
        "run-id",
        SubagentResult(output="done"),
        dispose=dispose,
    )
    await asyncio.gather(*(run.dispose() for _ in range(5)))
    await run.dispose()

    assert run.disposed is True
    assert dispose_calls == 1
    assert (await run.result).output == "done"


@pytest.mark.asyncio
async def test_runtime_close_disposes_published_runs_and_is_idempotent():
    runtime = SubagentRuntime(build_child=_child_factory)
    run = await runtime.start(_request(spec={"mode": "cancel"}))

    await asyncio.sleep(0)
    await asyncio.gather(runtime.aclose(), runtime.aclose())

    assert run.disposed is True
    assert (await run.result).stop_reason == "aborted"
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.start(_request())


@pytest.mark.parametrize(
    ("mode", "expected_stop_reason"),
    [
        ("completed", "completed"),
        ("error", "error"),
        ("idle", "timed_out"),
        ("absolute", "timed_out"),
        ("cancel", "aborted"),
    ],
)
@pytest.mark.asyncio
async def test_active_is_unregistered_on_every_provider_terminal_path(
    mode: str,
    expected_stop_reason: str,
):
    active = ActiveSubagents()
    runtime = SubagentRuntime(
        build_child=_child_factory,
        active=active,
        idle_timeout=0.01,
        max_runtime=0.03,
    )
    run = await runtime.start(_request(mode, spec={"mode": mode}))
    assert active.snapshot("parent-session")

    if mode == "cancel":
        # Let the provider task enter its child loop before cancelling it; a
        # task cancelled before its first step has no opportunity to publish
        # the normal aborted result.
        await asyncio.sleep(0)
        await run.dispose()
    result = await run.result

    assert result.stop_reason == expected_stop_reason
    assert active.snapshot("parent-session") == []
    await run.dispose()


@pytest.mark.asyncio
async def test_runtime_batch_keeps_siblings_when_publication_one_fails():
    provider = _Provider("batch", fail_labels={"bad"})
    runtime = SubagentRuntime(provider_registry=ProviderRegistry({"batch": provider}))

    outcomes = await runtime.execute_many(
        [
            (_request("good", provider="batch"), None),
            (_request("bad", provider="batch"), None),
        ],
        max_concurrent=2,
    )

    assert outcomes[0] == SubagentResult(output="done:good")
    assert outcomes[1].stop_reason == "error"
    assert outcomes[1].diagnostic == 'failed to publish subagent "bad": publication failed for bad'
    assert provider.started == ["good", "bad"]


def test_legacy_result_shape_remains_exact():
    result = SubagentResult(
        output="answer",
        tool_calls=2,
        last_tool="file_read",
        duration_seconds=1.234,
    )

    assert result.to_legacy_dict(agent="worker") == {
        "agent": "worker",
        "status": "completed",
        "summary": "answer",
        "duration_seconds": 1.23,
        "tool_calls": 2,
        "last_tool": "file_read",
    }


def test_in_process_provider_exposes_expected_one_shot_capability_matrix():
    provider = InProcessSpawnProvider(_child_factory)

    assert provider.capabilities == SubagentCapabilities(
        output_schema=False,
        depth_limit=True,
        tool_filter=True,
        persona=False,
        agent_options=True,
    )
    assert provider.inherits_parent_context is False


@pytest.mark.asyncio
async def test_subagent_feature_publishes_and_withdraws_service_generation():
    service = SubagentRuntime(InProcessSpawnProvider(_child_factory))
    runtime = FeatureRuntime()
    bundle = build_subagent_feature(service)

    record = await runtime.activate(bundle.definition)

    assert record.state is FeatureState.ACTIVE
    assert bundle.definition.feature_id == SUBAGENT_FEATURE_ID
    assert runtime.services.get(SUBAGENT_SERVICE_KEY) is service
    assert record.scope is not None
    await record.scope.stop(bundle.definition.stop_policy)
    assert runtime.services.get(SUBAGENT_SERVICE_KEY) is None
    assert service._closed is False

    await service.aclose()
