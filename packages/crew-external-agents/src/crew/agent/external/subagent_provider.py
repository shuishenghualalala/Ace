"""Subagent transport backed by the external-agent delegation service.

The external-agent feature already owns runtime discovery, process security and
protocol adaptation.  This module only translates the stable subagent request
into that feature's neutral ``RuntimeExecutionRequest`` and folds its event
stream into a :class:`~crew.agent.subagent.service.SubagentResult`.

The provider is intentionally context-detached: an external process does not
inherit the parent conversation, tools or approval authority.  The parent
workspace and owner are still supplied as host-owned execution context.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
import uuid
from collections.abc import Mapping
from typing import Any, Callable

from crew.agent.external.catalog import ExternalAgentCatalog
from crew.agent.external.feature import DelegationService, ExternalAgentRun
from crew.agent.external.runtime_registry import (
    resolve_runtime_adapter_id,
    resolve_runtime_credential_home_paths,
    resolve_runtime_network_endpoints,
)
from crew.agent.subagent.service import (
    ResolvedStartRequest,
    SubagentCapabilities,
    SubagentProviderError,
    SubagentResult,
    SubagentRun,
)
from crew.core.interfaces import (
    AcpPermissionRequest,
    ExternalStreamEvent,
    RuntimeExecutionRequest,
)
from crew.core.runctx import current_agent_workdir, current_owner_account_id
from crew.core.timeout_policy import DEFAULT_EXTERNAL_IDLE_SECONDS
from crew.state.logging import get_logger
from crew.tools.redact import redact_sensitive_text


log = get_logger("agent.external.subagent")

_PARTIAL_TAIL = 800
_DIAGNOSTIC_LIMIT = 1200
_DEFAULT_PERMISSION_DECISION = "deny"


async def _deny_permission(_request: AcpPermissionRequest) -> str:
    """Deny child-originated external permission requests without prompting."""

    return _DEFAULT_PERMISSION_DECISION


def _text(value: Any) -> str:
    return str(value or "").strip()


def _diagnostic(value: Any, default: str = "外部智能体执行失败") -> str:
    text = redact_sensitive_text(_text(value), force=True)
    if not text:
        return default
    if len(text) <= _DIAGNOSTIC_LIMIT:
        return text
    return f"{text[:_DIAGNOSTIC_LIMIT - 3].rstrip()}..."


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item or "").strip())


def _string_list(value: Any) -> list[str]:
    return list(_string_tuple(value))


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _positive_timeout(value: Any, default: float = DEFAULT_EXTERNAL_IDLE_SECONDS) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    return timeout if timeout > 0 else default


def _runtime_metadata(runtime: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = runtime.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _event_attr(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


class _ExternalRunController:
    """Own one submitted external run and fold its stream exactly once."""

    def __init__(
        self,
        external_run: ExternalAgentRun,
        *,
        run_id: str,
        on_done: Callable[[], None],
    ) -> None:
        self._external_run = external_run
        self._run_id = run_id
        self._on_done = on_done
        self._started_at = time.perf_counter()
        self._text_parts: list[str] = []
        self._tool_calls = 0
        self._last_tool = ""
        self._usage: dict[str, int] = {}
        self._session_id = ""
        self._session_reset = False
        self._cancel_requested = False
        self._stream_terminal = False
        self._external_closed = False
        self._done_notified = False
        self._close_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._external_close_lock = asyncio.Lock()
        self._task: asyncio.Task[SubagentResult] | None = None

    def start(self) -> asyncio.Task[SubagentResult]:
        if self._task is None:
            self._task = asyncio.create_task(
                self._consume(),
                name=f"subagent-external:{self._run_id}",
            )
        return self._task

    async def _consume(self) -> SubagentResult:
        stop_reason = "completed"
        diagnostic = ""
        try:
            async for event in self._external_run.stream():
                event_diagnostic = self._fold_event(event)
                if event_diagnostic is not None:
                    stop_reason = "error"
                    diagnostic = event_diagnostic
                    self._stream_terminal = True
                    break
            else:
                # Mark the stream terminal before the first teardown await.
                # A dispose racing with this point must not rewrite a complete
                # result as aborted.
                self._stream_terminal = True
                if self._cancel_requested:
                    stop_reason = "aborted"
                    diagnostic = "已取消"
        except asyncio.CancelledError:
            if not self._stream_terminal:
                stop_reason = "aborted"
                diagnostic = "已取消"
                self._stream_terminal = True
        except Exception as exc:  # noqa: BLE001 - child failure is result data
            stop_reason = "error"
            log.debug(
                "外部子智能体执行失败 id=%s",
                self._run_id,
                exc_info=True,
            )
            diagnostic = _diagnostic(exc)
            self._stream_terminal = True
        finally:
            await self._close_external(cancel=self._cancel_requested or stop_reason == "aborted")
            self._notify_done()

        joined = "".join(self._text_parts)
        successful = stop_reason == "completed"
        structured: dict[str, Any] = {}
        if self._usage:
            structured["usage"] = dict(self._usage)
        if self._session_id:
            structured["session_id"] = self._session_id
        if self._session_reset:
            structured["session_reset"] = True
        return SubagentResult(
            output=joined if successful else "",
            stop_reason=stop_reason,  # type: ignore[arg-type]
            diagnostic=diagnostic,
            partial_output=joined[-_PARTIAL_TAIL:] if not successful else "",
            structured=structured or None,
            tool_calls=self._tool_calls,
            last_tool=self._last_tool,
            duration_seconds=time.perf_counter() - self._started_at,
        )

    def _fold_event(self, event: ExternalStreamEvent | Mapping[str, Any]) -> str | None:
        kind = _text(_event_attr(event, "kind")).lower()
        if kind == "text":
            value = _event_attr(event, "text", "")
            if value:
                self._text_parts.append(str(value))
            return None

        if kind in {"session", "session_id", "session_reset"}:
            session_id = _text(_event_attr(event, "session_id", ""))
            if session_id:
                self._session_id = session_id
            self._session_reset = self._session_reset or kind == "session_reset" or bool(
                _event_attr(event, "session_reset", False)
            )
            return None

        if kind == "usage":
            raw_usage = _event_attr(event, "usage", {})
            if isinstance(raw_usage, Mapping):
                for key, value in raw_usage.items():
                    try:
                        numeric = int(value)
                    except (TypeError, ValueError):
                        continue
                    name = str(key).strip()
                    if name:
                        # Adapters publish token-usage snapshots (Codex may
                        # send more than one update); the last value is the
                        # authoritative total for each counter.
                        self._usage[name] = numeric
            return None

        if kind == "error":
            return (
                _diagnostic(_event_attr(event, "text", ""), "")
                or _diagnostic(_event_attr(event, "detail", ""), "")
                or _diagnostic(_event_attr(event, "message", ""), "")
                or "外部智能体执行失败"
            )

        if kind != "tool":
            return None
        tool = _event_attr(event, "tool")
        name = _text(_event_attr(tool, "name", ""))
        phase = _text(_event_attr(tool, "phase", "")).lower()
        if phase in {"start", "begin", "call", ""}:
            self._tool_calls += 1
        if name:
            self._last_tool = name
        return None

    async def close(self) -> None:
        """Cancel and close this external run; safe for concurrent callers."""

        if not self._stream_terminal:
            self._cancel_requested = True
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._shutdown(),
                    name=f"subagent-external-close:{self._run_id}",
                )
            task = self._close_task
        await asyncio.shield(task)

    async def _shutdown(self) -> None:
        task = self._task
        current = asyncio.current_task()
        try:
            await self._close_external(cancel=self._cancel_requested)
        finally:
            if (
                task is not None
                and task is not current
                and not task.done()
                and not self._stream_terminal
            ):
                task.cancel()
            if task is not None and task is not current:
                await asyncio.gather(task, return_exceptions=True)
            self._notify_done()

    async def _close_external(self, *, cancel: bool) -> None:
        async with self._external_close_lock:
            if self._external_closed:
                return
            action = getattr(self._external_run, "cancel" if cancel else "close", None)
            try:
                if callable(action):
                    returned = action()
                    if inspect.isawaitable(returned):
                        await returned
            except Exception:  # noqa: BLE001 - best effort teardown
                log.debug("关闭外部子智能体运行失败 id=%s", self._run_id, exc_info=True)
            finally:
                self._external_closed = True

    def _notify_done(self) -> None:
        if self._done_notified:
            return
        self._done_notified = True
        self._on_done()


class ExternalDelegationProvider:
    """Expose one catalog external agent as a subagent transport."""

    inherits_parent_context = False

    def __init__(
        self,
        agent_id: str,
        catalog: ExternalAgentCatalog | None = None,
        delegation: DelegationService | None = None,
        *,
        store: ExternalAgentCatalog | None = None,
        delegation_service: DelegationService | None = None,
        owner_account_id: str | None = None,
    ) -> None:
        normalized_id = _text(agent_id)
        if not normalized_id:
            raise ValueError("external subagent provider requires agent_id")
        catalog = catalog if catalog is not None else store
        delegation = delegation if delegation is not None else delegation_service
        if catalog is None:
            raise ValueError("external subagent provider requires a catalog")
        if delegation is None:
            raise ValueError("external subagent provider requires a delegation service")
        self.agent_id = normalized_id
        self.name = f"external:{normalized_id}"
        self._catalog = catalog
        self._delegation = delegation
        self._owner_account_id = _text(owner_account_id)
        self.capabilities = SubagentCapabilities()
        self._runs: set[_ExternalRunController] = set()
        self._closed = False

    async def start(self, request: ResolvedStartRequest) -> SubagentRun:
        """Resolve catalog metadata, submit one external run and publish it."""

        if self._closed:
            raise SubagentProviderError(f'external provider "{self.name}" is closed')
        agent, runtime = self._resolve_agent(request)
        execution_request = self._build_execution_request(request, agent, runtime)
        try:
            external_run = await self._delegation.submit(execution_request)
        except Exception as exc:  # noqa: BLE001 - no run is published on submit failure
            raise SubagentProviderError(
                f'failed to submit external subagent "{self.agent_id}": '
                f"{_diagnostic(exc)}"
            ) from exc
        run_id = request.descriptor.subagent_id or (
            f"{self.name}::{uuid.uuid4().hex[:8]}"
        )
        controller = _ExternalRunController(
            external_run,
            run_id=run_id,
            on_done=lambda: self._runs.discard(controller),
        )
        self._runs.add(controller)
        result_task = controller.start()
        return SubagentRun(
            run_id,
            result_task,
            dispose=controller.close,
            descriptor=request.descriptor,
        )

    async def close(self) -> None:
        """Close all active external runs; safe to call repeatedly."""

        if self._closed and not self._runs:
            return
        self._closed = True
        runs = tuple(self._runs)
        if runs:
            await asyncio.gather(*(run.close() for run in runs), return_exceptions=True)
        self._runs.clear()

    async def aclose(self) -> None:
        await self.close()

    def _resolve_agent(
        self,
        request: ResolvedStartRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        owner = (
            _text(request.owner_account_id)
            or self._owner_account_id
            or _text(current_owner_account_id.get())
        )
        try:
            agent, runtime = self._catalog.agent_with_runtime(
                self.agent_id,
                owner_account_id=owner,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SubagentProviderError(
                f'external agent "{self.agent_id}" is unavailable: {exc}'
            ) from exc
        if not isinstance(agent, Mapping) or not isinstance(runtime, Mapping):
            raise SubagentProviderError(
                f'external agent "{self.agent_id}" catalog entry is malformed'
            )
        runtime_dict = dict(runtime)
        return dict(agent), runtime_dict

    def _build_execution_request(
        self,
        request: ResolvedStartRequest,
        agent: Mapping[str, Any],
        runtime: Mapping[str, Any],
    ) -> RuntimeExecutionRequest:
        metadata = dict(_runtime_metadata(runtime))
        provider = _text(agent.get("provider") or runtime.get("provider")) or "external"
        protocol = _text(runtime.get("protocol"))
        # Persisted runtime rows from before the runtime registry migration may
        # keep these declarations at the row root.  Preserve those values
        # while routing every fallback through the canonical resolvers.
        for key in ("adapter_id", "credential_home_paths", "network_endpoints"):
            if key not in metadata and runtime.get(key) is not None:
                metadata[key] = runtime[key]
        adapter_id = resolve_runtime_adapter_id(
            provider=provider,
            protocol=protocol,
            metadata=metadata,
        )
        model = _text(agent.get("model")) or _text(metadata.get("default_model_id"))
        runtime_options = request.runtime_options
        cwd = self._resolve_cwd(request, runtime_options)
        configured_timeout = runtime_options.get(
            "timeout",
            runtime_options.get("idle_timeout", DEFAULT_EXTERNAL_IDLE_SECONDS),
        )
        hard_deadline = runtime_options.get("hard_deadline")
        if not isinstance(hard_deadline, (int, float)) or isinstance(hard_deadline, bool):
            hard_deadline = None
        hard_timeout_enabled = bool(runtime_options.get("hard_timeout_enabled", False))
        if hard_deadline is not None:
            hard_deadline = float(hard_deadline)
            hard_timeout_enabled = True
        elif "max_runtime" in runtime_options:
            try:
                max_runtime = float(runtime_options["max_runtime"])
            except (TypeError, ValueError):
                pass
            else:
                if math.isfinite(max_runtime):
                    hard_deadline = (
                        time.monotonic() + max_runtime
                        if max_runtime > 0
                        else None
                    )
                    hard_timeout_enabled = True

        launch_args = (
            metadata.get("launch_args")
            if "launch_args" in metadata
            else runtime.get("launch_args", ())
        )
        if "launch_args" not in metadata and not launch_args and adapter_id == "acp-stdio":
            launch_args = ["acp"]
        credential_home_paths = resolve_runtime_credential_home_paths(
            provider=provider,
            metadata=metadata,
        )
        network_endpoints = resolve_runtime_network_endpoints(
            provider=provider,
            metadata=metadata,
        )
        raw_prompt = (
            request.spec.get("system_prompt", "")
            if isinstance(request.spec, Mapping)
            else ""
        ) or agent.get("system_prompt", "")
        return RuntimeExecutionRequest(
            executable_path=_text(runtime.get("executable_path")),
            provider=provider,
            prompt=request.effective_prompt,
            model=model,
            cwd=cwd,
            system_prompt=_text(raw_prompt),
            launch_args=_string_list(launch_args),
            custom_args=_string_list(agent.get("custom_args")),
            custom_env=_string_mapping(agent.get("custom_env")),
            credential_home_paths=_string_tuple(credential_home_paths),
            network_endpoints=_string_tuple(network_endpoints),
            timeout=_positive_timeout(configured_timeout),
            hard_deadline=hard_deadline,
            hard_timeout_enabled=hard_timeout_enabled,
            permission_handler=_deny_permission,
            adapter_id=adapter_id,
        )

    @staticmethod
    def _resolve_cwd(
        request: ResolvedStartRequest,
        runtime_options: Mapping[str, Any],
    ) -> str:
        configured = _text(runtime_options.get("cwd"))
        if configured:
            return configured
        parent_cwd = _text(getattr(request.parent, "cwd", ""))
        if parent_cwd:
            return parent_cwd
        return _text(current_agent_workdir.get()) or "."


# The longer name is the public contract; this alias keeps imports readable at
# call sites that use the transport-neutral family naming.
ExternalSubagentProvider = ExternalDelegationProvider


__all__ = [
    "ExternalDelegationProvider",
    "ExternalSubagentProvider",
]
