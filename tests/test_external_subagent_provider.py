"""Contract tests for the external-agent subagent Provider seam."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from crew.agent.external import ExternalDelegationProvider
from crew.agent.subagent.service import (
    ResolvedStartRequest,
    StartRequest,
    SubagentCapabilities,
    SubagentCapabilityError,
    SubagentDescriptor,
    SubagentProvider,
)
from crew.agent.subagent.runtime import SubagentRuntime
from crew.core.interfaces import ExternalStreamEvent, ExternalToolEvent


class _Catalog:
    def __init__(self, *, legacy_runtime: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.legacy_runtime = legacy_runtime

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append((agent_id, owner_account_id))
        if agent_id != "agent-a":
            raise KeyError(agent_id)
        return (
            {
                "id": agent_id,
                "name": "Review Agent",
                "provider": "kimi",
                "runtime_id": "runtime-a",
                "model": "kimi-model",
                "system_prompt": "unused catalog prompt",
                "custom_args": ["--safe"],
                "custom_env": {"AGENT_MODE": "review"},
            },
            {
                "id": "runtime-a",
                "provider": "kimi",
                "protocol": "acp",
                "executable_path": "/usr/local/bin/kimi",
                "metadata": (
                    {}
                    if self.legacy_runtime
                    else {
                        "adapter_id": "acp-stdio",
                        "launch_args": ["acp"],
                        "default_model_id": "catalog-default",
                        "credential_home_paths": [".kimi-code/config.toml"],
                        "network_endpoints": ["https://api.example.test"],
                    }
                ),
            },
        )


class _ExternalRun:
    def __init__(self, events: list[ExternalStreamEvent], *, wait: bool = False) -> None:
        self.events = events
        self.wait = wait
        self.release = asyncio.Event()
        self.cancel_calls = 0
        self.close_calls = 0

    async def stream(self):
        for event in self.events:
            yield event
        if self.wait:
            await self.release.wait()

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.release.set()

    async def close(self) -> None:
        self.close_calls += 1


class _Delegation:
    def __init__(self, run: _ExternalRun) -> None:
        self.run = run
        self.requests: list[Any] = []

    async def submit(self, request: Any) -> _ExternalRun:
        self.requests.append(request)
        return self.run


def _request(**kwargs: Any) -> ResolvedStartRequest:
    runtime_options = kwargs.pop("runtime_options", {"cwd": "/workspace/project"})
    base = StartRequest(
        label="reviewer",
        goal_text="Review the changed files",
        parent_session_id="parent-session",
        owner_account_id="owner-a",
        spec={"system_prompt": "You are a concise reviewer."},
        **kwargs,
    )
    return ResolvedStartRequest(
        **{
            field: getattr(base, field)
            for field in StartRequest.__dataclass_fields__
        },
        descriptor=SubagentDescriptor(
            provider="external:agent-a",
            parent_session_id="parent-session",
            subagent_id="sub::reviewer::1",
        ),
        runtime_options=runtime_options,
    )


def test_external_provider_has_explicit_name_and_fail_closed_capabilities():
    provider = ExternalDelegationProvider("agent-a", _Catalog(), _Delegation(_ExternalRun([])))

    assert provider.name == "external:agent-a"
    assert provider.inherits_parent_context is False
    assert provider.capabilities == SubagentCapabilities()
    assert isinstance(provider, SubagentProvider)


@pytest.mark.parametrize(
    ("field", "value", "capability"),
    [
        ("agent_options", {"model": "override"}, "agent_options"),
        ("tool_filter", ("file_read",), "tool_filter"),
        ("output_schema", {"type": "object"}, "output_schema"),
    ],
)
@pytest.mark.asyncio
async def test_external_provider_rejects_unsupported_capabilities_before_submit(
    field: str,
    value: Any,
    capability: str,
):
    delegation = _Delegation(_ExternalRun([]))
    provider = ExternalDelegationProvider("agent-a", _Catalog(), delegation)
    runtime = SubagentRuntime(provider)

    with pytest.raises(SubagentCapabilityError) as raised:
        await runtime.start(
            StartRequest(
                goal_text="review",
                provider=provider.name,
                **{field: value},
            )
        )

    assert raised.value.provider == provider.name
    assert raised.value.capability == capability
    assert delegation.requests == []


@pytest.mark.asyncio
async def test_external_provider_builds_runtime_request_and_folds_events():
    external_run = _ExternalRun([
        ExternalStreamEvent(kind="session", session_id="native-session"),
        ExternalStreamEvent(kind="text", text="first "),
        ExternalStreamEvent(
            kind="tool",
            tool=ExternalToolEvent(
                name="file_read",
                phase="start",
                tool_call_id="tool-1",
            ),
        ),
        ExternalStreamEvent(kind="usage", usage={"input_tokens": 3}),
        ExternalStreamEvent(kind="usage", usage={"input_tokens": 2, "output_tokens": 4}),
        ExternalStreamEvent(kind="text", text="answer"),
        ExternalStreamEvent(kind="session", session_id="native-session", session_reset=True),
    ])
    catalog = _Catalog()
    delegation = _Delegation(external_run)
    provider = ExternalDelegationProvider("agent-a", catalog, delegation)

    run = await provider.start(_request())
    result = await run.result

    assert catalog.calls == [("agent-a", "owner-a")]
    assert len(delegation.requests) == 1
    execution = delegation.requests[0]
    assert execution.executable_path == "/usr/local/bin/kimi"
    assert execution.provider == "kimi"
    assert execution.adapter_id == "acp-stdio"
    assert execution.prompt == "Review the changed files"
    assert execution.system_prompt == "You are a concise reviewer."
    assert execution.cwd == "/workspace/project"
    assert execution.model == "kimi-model"
    assert execution.launch_args == ["acp"]
    assert execution.custom_args == ["--safe"]
    assert execution.custom_env == {"AGENT_MODE": "review"}
    assert execution.credential_home_paths == (".kimi-code/config.toml",)
    assert execution.network_endpoints == ("https://api.example.test",)
    assert await execution.permission_handler(object()) == "deny"
    assert result.output == "first answer"
    assert result.partial_output == ""
    assert result.tool_calls == 1
    assert result.last_tool == "file_read"
    assert result.structured == {
        "usage": {"input_tokens": 2, "output_tokens": 4},
        "session_id": "native-session",
        "session_reset": True,
    }
    assert external_run.close_calls == 1
    await run.dispose()
    assert (await run.result).stop_reason == "completed"
    assert external_run.cancel_calls == 0
    assert external_run.close_calls == 1


@pytest.mark.asyncio
async def test_external_provider_folds_error_events_into_failed_results():
    external_run = _ExternalRun([
        ExternalStreamEvent(kind="text", text="partial"),
        ExternalStreamEvent(kind="error", text="runtime failed"),
        ExternalStreamEvent(kind="text", text="must not be consumed"),
    ])
    provider = ExternalDelegationProvider("agent-a", _Catalog(), _Delegation(external_run))

    result = await (await provider.start(_request())).result

    assert result.stop_reason == "error"
    assert result.diagnostic == "runtime failed"
    assert result.output == ""
    assert result.partial_output == "partial"
    assert external_run.close_calls == 1


@pytest.mark.asyncio
async def test_external_provider_uses_runtime_registry_defaults():
    external_run = _ExternalRun([])
    delegation = _Delegation(external_run)
    provider = ExternalDelegationProvider(
        "agent-a",
        _Catalog(legacy_runtime=True),
        delegation,
    )

    run = await provider.start(_request())
    await run.result
    execution = delegation.requests[0]

    assert execution.adapter_id == "acp-stdio"
    assert execution.launch_args == ["acp"]
    assert execution.credential_home_paths == (
        ".kimi-code/config.toml",
        ".kimi-code/oauth/kimi-code",
        ".kimi-code/credentials/kimi-code.json",
    )
    assert execution.network_endpoints == ("https://auth.kimi.com",)


@pytest.mark.asyncio
async def test_external_provider_maps_subagent_runtime_limit_to_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "crew.agent.external.subagent_provider.time.monotonic",
        lambda: 100.0,
    )
    delegation = _Delegation(_ExternalRun([]))
    provider = ExternalDelegationProvider("agent-a", _Catalog(), delegation)

    run = await provider.start(
        _request(runtime_options={"idle_timeout": 30.0, "max_runtime": 60.0})
    )
    await run.result
    execution = delegation.requests[0]

    assert execution.timeout == 30.0
    assert execution.hard_timeout_enabled is True
    assert execution.hard_deadline == 160.0


@pytest.mark.asyncio
async def test_external_provider_cancellation_and_close_are_idempotent():
    external_run = _ExternalRun(
        [ExternalStreamEvent(kind="text", text="partial")],
        wait=True,
    )
    delegation = _Delegation(external_run)
    provider = ExternalDelegationProvider("agent-a", _Catalog(), delegation)
    run = await provider.start(_request())
    await asyncio.sleep(0)

    await asyncio.gather(run.dispose(), run.dispose())
    result = await run.result
    await provider.close()
    await provider.close()

    assert result.stop_reason == "aborted"
    assert result.partial_output == "partial"
    assert external_run.cancel_calls == 1
    assert external_run.close_calls == 0


@pytest.mark.asyncio
async def test_external_provider_does_not_publish_when_catalog_or_submit_fails():
    missing_catalog = _Catalog()
    provider = ExternalDelegationProvider("missing", missing_catalog, _Delegation(_ExternalRun([])))

    with pytest.raises(Exception, match="unavailable"):
        await provider.start(_request())

    class BrokenDelegation:
        async def submit(self, _request: Any) -> Any:
            raise RuntimeError("delegation offline sk-abcdefghij1234567890")

    provider = ExternalDelegationProvider("agent-a", _Catalog(), BrokenDelegation())
    with pytest.raises(Exception, match="delegation offline") as raised:
        await provider.start(_request())
    assert "sk-abcdefghij1234567890" not in str(raised.value)
