"""Semantic subagent runtime.

The runtime is the single preflight and publication boundary for one-shot
delegations.  Providers execute work; this class validates capabilities,
assigns the descriptor, and makes disposal observable and idempotent to the
consumer.

Stage B additions: an append-only run log whose folded outcome is the single
derivation entry for ``stop_reason``; a persisted depth ledger (monotonic
lower bound); and a ramped, rate-limit-aware batch scheduler.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

from crew.agent.subagent.service import (
    ProviderRegistry,
    ResolvedStartRequest,
    StartRequest,
    SubagentCapabilityError,
    SubagentDepthError,
    SubagentDescriptor,
    SubagentError,
    SubagentPublicationError,
    SubagentProvider,
    SubagentResult,
    SubagentRun,
    SubagentService,
    UnknownProviderError,
    coerce_start_request,
)
from crew.core.runctx import normalize_owner_account_id
from crew.state.logging import get_logger


log = get_logger("subagent.runtime")

_LEGACY_TASK_STATUS = {
    "completed": "done",
    "timeout": "failed",
    "error": "failed",
}

# 记账 turn 的合法结局词汇（dsh 事件流同族）：
# blocked = 审批拒绝/策略阻断；interrupted 与 aborted 同归 cancelled 家族。
_TURN_END_REASONS = frozenset(
    {"completed", "aborted", "interrupted", "error", "max_tokens", "blocked"}
)


class RunLogRecorder:
    """Per-run append-only event log; the persisted run log is its snapshot."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, data: Mapping[str, Any] | None = None) -> None:
        self.events.append({"event": kind, "data": dict(data or {})})

    def turn_start(self) -> None:
        self.append("turn/start")

    def turn_end(self, reason: str) -> None:
        normalized = str(reason or "").strip().lower().replace("-", "_")
        if normalized == "timeout":
            normalized = "timed_out"
        self.append("turn/end", {"reason": normalized})

    def output_final(self, text: str) -> None:
        self.append("output/final", {"text": text})

    def run_start(
        self,
        *,
        subagent_id: str,
        parent_session_id: str,
        depth: int,
        mode: str,
        provider: str,
    ) -> None:
        self.append(
            "run/start",
            {
                "subagent_id": subagent_id,
                "parent_session_id": parent_session_id,
                "depth": int(depth),
                "mode": mode,
                "provider": provider,
            },
        )

    def run_settle(
        self,
        *,
        stop_reason: str,
        diagnostic: str = "",
        timeout_kind: str | None = None,
    ) -> None:
        self.append(
            "run/settle",
            {
                "stop_reason": stop_reason,
                "diagnostic": diagnostic[:500],
                "timeout_kind": timeout_kind or "",
            },
        )


def fold_run_outcome(
    events: Iterable[Mapping[str, Any]],
    *,
    fallback: str = "error",
) -> str:
    """Fold the run log into the terminal stop reason (唯一推导入口).

    规则（对齐"日志是真相"）：取最后一次记账 ``turn/end`` 的 reason；
    ``blocked`` 映射 ``refusal``；``aborted|interrupted`` 映射 ``aborted``；
    ``max_tokens`` 直通；不可名状按 ``error``。**已记录的 ``error`` 优先于
    其后的取消**——先失败后取消的算失败。没有任何记账 turn 时退回
    ``fallback``（调用方给 provider 自报的结局）。超时不在此列：它是
    运行时按 ``timeout_kind`` 生成的结论，不由 turn 结局推导。
    """

    last_reason: str | None = None
    error_recorded = False
    for item in events:
        if not isinstance(item, Mapping):
            continue
        if item.get("event") != "turn/end":
            continue
        data = item.get("data") or {}
        reason = str(data.get("reason") or "").strip().lower().replace("-", "_")
        if reason == "timeout":
            reason = "timed_out"
        if reason not in _TURN_END_REASONS:
            reason = "error"
        if reason == "error":
            error_recorded = True
        last_reason = reason
    if last_reason is None:
        return fallback
    if last_reason in {"aborted", "interrupted"} and error_recorded:
        return "error"
    if last_reason == "interrupted":
        return "aborted"
    if last_reason == "blocked":
        return "refusal"
    if last_reason == "timed_out":
        # 超时应由运行时按 timeout_kind 生成；出现在 turn 结局里按 error 复核
        return "error"
    return last_reason


# 限流识别启发式：诊断文本命中即视为瞬时限流（可重试）。配额耗尽是终态，
# 不与 429 混同——它不该被无限重排。
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "rate-limited",
    "too many requests",
    "too_many_requests",
)


def looks_rate_limited(diagnostic: str) -> bool:
    text = str(diagnostic or "").strip().lower()
    if not text:
        return False
    if "quota" in text and "429" not in text:
        return False
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def summary_policy_from_spec(
    spec: Mapping[str, Any] | None,
    floor: Mapping[str, int] | None,
) -> dict[str, int] | None:
    """摘要兜底策略：preset 声明覆盖全局默认；min_chars 或 retries 为 0 即关闭。"""

    preset = spec.get("_preset_spec") if isinstance(spec, Mapping) else None
    source = preset if isinstance(preset, Mapping) else (spec or {})
    base = {
        "min_chars": int((floor or {}).get("min_chars", 0) or 0),
        "retries": int((floor or {}).get("retries", 0) or 0),
    }
    if source.get("summary_min_chars") is not None:
        base["min_chars"] = max(0, int(source["summary_min_chars"]))
    if source.get("summary_retries") is not None:
        base["retries"] = max(0, int(source["summary_retries"]))
    if base["min_chars"] <= 0 or base["retries"] <= 0:
        return None
    return base


@dataclass(frozen=True, slots=True)
class BatchSchedulePolicy:
    """Batch launch pacing and rate-limit recovery knobs (all durations seconds)."""

    max_concurrent: int = 3
    initial_burst: int = 5
    launch_interval: float = 0.7
    backoff_base: float = 3.0
    backoff_factor: float = 2.0
    max_retries: int = 6
    shrink_interval: float = 2.0
    recover_interval: float = 180.0

    @classmethod
    def from_config(cls, cfg: Any) -> "BatchSchedulePolicy":
        return cls(
            max_concurrent=max(1, int(getattr(cfg, "subagent_max_concurrent", 3))),
            initial_burst=max(1, int(getattr(cfg, "subagent_launch_initial", 5))),
            launch_interval=max(
                0.0, float(getattr(cfg, "subagent_launch_interval_ms", 700)) / 1000.0
            ),
            backoff_base=max(
                0.0, float(getattr(cfg, "subagent_rate_backoff_base_seconds", 3.0))
            ),
            backoff_factor=max(
                1.0, float(getattr(cfg, "subagent_rate_backoff_factor", 2.0))
            ),
            max_retries=max(0, int(getattr(cfg, "subagent_rate_max_retries", 6))),
            recover_interval=max(
                0.0, float(getattr(cfg, "subagent_rate_recover_seconds", 180.0))
            ),
        )


@dataclass(slots=True)
class ContinuableActivation:
    """Process-local residency for one persisted subagent identity."""

    descriptor: SubagentDescriptor
    owner_account_id: str
    workspace_id: str | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: bool = False
    pending_messages: deque[str] = field(default_factory=deque)
    last_result: SubagentResult | None = None
    last_state: str | None = None


class SubagentRuntime:
    """Validate and dispatch one-shot requests to named providers."""

    def __init__(
        self,
        providers: ProviderRegistry | SubagentProvider | Mapping[str, SubagentProvider]
        | Iterable[SubagentProvider] | None = None,
        *,
        provider_registry: ProviderRegistry | None = None,
        default_provider: str = "inprocess-spawn",
        max_depth: int = 3,
        build_child: Any | None = None,
        active: Any | None = None,
        idle_timeout: float = 120.0,
        max_runtime: float = 1800.0,
        store: Any | None = None,
        continuable_enabled: bool = False,
    ) -> None:
        if provider_registry is not None and providers is not None:
            raise ValueError("provide either provider_registry or providers, not both")
        self.providers = (
            provider_registry
            if provider_registry is not None
            else ProviderRegistry()
        )
        self.default_provider = str(default_provider or "inprocess-spawn").strip()
        if not self.default_provider:
            raise ValueError("default_provider must not be empty")
        self.max_depth = self._validate_non_negative_int(max_depth, "max_depth")
        self.store = store
        self.active = active
        self.continuable_enabled = bool(continuable_enabled)
        self._runs: dict[str, SubagentRun] = {}
        self._activations: dict[str, ContinuableActivation] = {}
        self._lifecycle_events: dict[tuple[str, str], deque[dict[str, Any]]] = {}
        self._lifecycle_events_lock = threading.Lock()
        self._closed = False

        if providers is not None:
            if isinstance(providers, ProviderRegistry):
                for provider in providers.list():
                    self.register_provider(provider)
            elif isinstance(providers, Mapping):
                for provider in providers.values():
                    self.register_provider(provider)
            elif hasattr(providers, "start") and hasattr(providers, "name"):
                self.register_provider(providers)  # type: ignore[arg-type]
            else:
                for provider in providers:
                    self.register_provider(provider)

        if build_child is not None and self.providers.get(self.default_provider) is None:
            # Lazy import keeps the service contract independent of the built-in
            # transport and makes custom providers cheap to use in tests.
            from crew.agent.subagent.providers import InProcessSpawnProvider

            self.register_provider(
                InProcessSpawnProvider(
                    build_child,
                    active=active,
                    idle_timeout=idle_timeout,
                    max_runtime=max_runtime,
                )
            )

    def register_provider(self, provider: SubagentProvider):
        return self.providers.register(provider)

    def register_provider_resolver(
        self,
        resolver: Callable[[str, str | None], SubagentProvider | None],
    ) -> Callable[[], None]:
        return self.providers.register_resolver(resolver)

    def unregister_provider(
        self,
        name: str,
        *,
        expected: SubagentProvider | None = None,
    ) -> bool:
        return self.providers.unregister(name, expected=expected)

    def get_provider(
        self,
        name: str,
        *,
        owner_account_id: str | None = None,
    ) -> SubagentProvider | None:
        return self.providers.get(name, owner_account_id=owner_account_id)

    def list_providers(self) -> list[str]:
        return self.providers.names()

    def active_snapshot(
        self,
        parent_session_id: str | None = None,
        *,
        owner_account_id: str | None = None,
    ) -> Any:
        """Return the process-local active child view for the feature route."""

        if self.active is None:
            return [] if parent_session_id is not None else {}
        snapshot = getattr(self.active, "snapshot", None)
        if not callable(snapshot):
            return [] if parent_session_id is not None else {}
        return snapshot(parent_session_id, owner_account_id=owner_account_id)

    def list_runs(
        self,
        *,
        parent_session_id: str | None = None,
        owner_account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Expose the store's read-only run projection to Feature consumers."""

        if self.store is None:
            return []
        return self.store.list_runs(
            parent_session_id=parent_session_id,
            owner_account_id=owner_account_id,
            limit=limit,
        )

    def list_agents(
        self,
        *,
        parent_session_id: str,
        owner_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return authorized direct children with derived residency state."""

        owner = normalize_owner_account_id(owner_account_id)
        if self.store is None:
            return []
        rows = self.store.list_runs(
            parent_session_id=parent_session_id,
            owner_account_id=owner,
            limit=500,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            subagent_id = str(row.get("subagent_id") or "")
            descriptor = self.store.read_descriptor(subagent_id) or {}
            if not self._persisted_identity_matches(
                subagent_id=subagent_id,
                parent_session_id=parent_session_id,
                row=row,
                descriptor=descriptor,
            ):
                continue
            activation = self._activations.get(subagent_id)
            if activation is None:
                state = (
                    "ready"
                    if descriptor.get("lifecycle") == "continuable"
                    and self.store.has_pending_messages(subagent_id, owner_account_id=owner)
                    else (
                        "idle"
                        if descriptor.get("lifecycle") == "continuable"
                        else "settled"
                    )
                )
            else:
                state = self._activation_state(activation)
            result.append({
                "subagent_id": subagent_id,
                "parent_session_id": row.get("parent_session_id"),
                "owner_account_id": row.get("owner_account_id"),
                "label": descriptor.get("label") or row.get("label"),
                "provider": descriptor.get("provider") or row.get("provider"),
                "lifecycle": descriptor.get("lifecycle") or row.get("mode"),
                "depth": descriptor.get("delegation_depth", row.get("depth")),
                "state": state,
                "status": row.get("status"),
                "stop_reason": row.get("stop_reason"),
                "created_at": row.get("created_at"),
                "settled_at": row.get("settled_at"),
            })
        return result

    def publish_lifecycle_event(
        self,
        *,
        owner_account_id: str | None,
        session_id: str,
        event: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Queue one lifecycle event for the owning parent session.

        The runtime stays independent from the Feature Event envelope.  The
        Bundle turns these small records into versioned ``feature_event``
        values when the current request boundary dispatches them.
        """

        session_id = str(session_id or "").strip()
        if not session_id:
            return
        key = (normalize_owner_account_id(owner_account_id), session_id)
        record = {
            "event": str(event or "").strip(),
            "payload": dict(payload),
        }
        if not record["event"]:
            return
        with self._lifecycle_events_lock:
            queue = self._lifecycle_events.setdefault(key, deque(maxlen=100))
            queue.append(record)

    def drain_lifecycle_events(
        self,
        *,
        owner_account_id: str | None,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Drain lifecycle events exactly once for one parent request."""

        key = (normalize_owner_account_id(owner_account_id), str(session_id or ""))
        with self._lifecycle_events_lock:
            events = self._lifecycle_events.pop(key, None)
        return list(events or ())

    async def start(
        self,
        provider: str | StartRequest | Mapping[str, Any] | None = None,
        request: StartRequest | Mapping[str, Any] | None = None,
        *,
        provider_name: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> SubagentRun:
        """Start one published run.

        Both ``start(request)`` and ``start("provider", request)`` are
        accepted.  The latter is useful to adapters that resolve deployment
        routing outside the model-facing request.
        """

        if self._closed:
            raise RuntimeError("subagent runtime is closed")

        if request is None and provider is not None and not isinstance(provider, str):
            request = provider
            provider = None
        if request is None:
            raise ValueError("subagent start requires a request")
        start_request = coerce_start_request(request)
        if start_request.lifecycle == "continuable":
            start_request = replace(start_request, session_persistence="hidden")
        elif start_request.session_persistence != "none":
            raise ValueError("one-shot subagents cannot use hidden session persistence")
        start_request = self._apply_depth_ledger(start_request)
        selected_provider = (
            provider_name
            or (provider if isinstance(provider, str) else "")
            or start_request.provider
            or self.default_provider
        ).strip()
        if not selected_provider:
            selected_provider = self.default_provider
        provider_impl = self.providers.get(
            selected_provider,
            owner_account_id=start_request.owner_account_id,
        )
        if provider_impl is None:
            raise UnknownProviderError(
                f'no subagent provider registered for "{selected_provider}"',
            )
        self._validate_request(start_request, provider_impl)

        label = (start_request.label or "subagent").strip()
        subagent_id = start_request.subagent_id or (
            f"sub::{label}::{uuid.uuid4().hex[:8]}"
        )
        model = None
        if isinstance(start_request.spec, Mapping):
            raw_model = start_request.spec.get("model")
            if raw_model not in (None, "", "inherit"):
                model = str(raw_model)
        if start_request.agent_options is not None:
            raw_model = start_request.agent_options.get("model")
            if raw_model not in (None, "", "inherit"):
                model = str(raw_model)
        descriptor = SubagentDescriptor(
            version=1,
            lifecycle=start_request.lifecycle,
            provider=selected_provider,
            parent_session_id=start_request.parent_session_id,
            label=label,
            model=model,
            persona=start_request.persona,
            tool_filter=self._as_tool_filter(start_request.tool_filter),
            delegation_depth=start_request.child_depth,
            subagent_id=subagent_id,
            session_persistence=start_request.session_persistence,
            spec=dict(start_request.spec),
        )
        resolved = self._resolved_request(
            start_request,
            provider=selected_provider,
            descriptor=descriptor,
            runtime_options=runtime_options,
        )
        recorder = None
        if isinstance(runtime_options, Mapping):
            candidate_log = runtime_options.get("run_log")
            if isinstance(candidate_log, RunLogRecorder):
                recorder = candidate_log
        if recorder is None:
            recorder = RunLogRecorder()
        recorder.run_start(
            subagent_id=subagent_id,
            parent_session_id=start_request.parent_session_id,
            depth=descriptor.delegation_depth,
            mode=start_request.lifecycle,
            provider=selected_provider,
        )
        self._persist_publication(
            descriptor,
            start_request=start_request,
            events=list(recorder.events),
        )
        try:
            run = await provider_impl.start(resolved)
        except SubagentError:
            self._settle_persisted_failure(subagent_id, events=list(recorder.events))
            raise
        except Exception as exc:  # noqa: BLE001 - provider setup is pre-publication
            self._settle_persisted_failure(
                subagent_id,
                events=list(recorder.events),
                diagnostic=str(exc),
            )
            raise SubagentPublicationError(
                f'failed to publish subagent "{label}": {exc}',
            ) from exc
        if not isinstance(run, SubagentRun):
            self._settle_persisted_failure(subagent_id, events=list(recorder.events))
            raise SubagentPublicationError(
                f'provider "{selected_provider}" returned an invalid subagent run',
            )

        async def dispose() -> None:
            try:
                await run.dispose()
            finally:
                self._runs.pop(run.id, None)

        wrapped = SubagentRun(
            run.id,
            run.result,
            dispose=dispose,
            local_agent=run.local_agent,
            descriptor=descriptor,
        )
        self._runs[wrapped.id] = wrapped
        if descriptor.lifecycle == "continuable":
            self._ensure_activation(
                descriptor,
                owner_account_id=start_request.owner_account_id,
                workspace_id=start_request.workspace_id,
            )
        self.publish_lifecycle_event(
            owner_account_id=start_request.owner_account_id,
            session_id=start_request.parent_session_id,
            event="run_started",
            payload={
                "subagent_id": descriptor.subagent_id,
                "label": descriptor.label or "subagent",
                "provider": descriptor.provider,
                "depth": descriptor.delegation_depth,
                "lifecycle": descriptor.lifecycle,
            },
        )
        return wrapped

    async def execute(
        self,
        provider: str | StartRequest | Mapping[str, Any] | None = None,
        request: StartRequest | Mapping[str, Any] | None = None,
        *,
        provider_name: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> SubagentResult:
        """Run and dispose one child, returning its terminal data."""

        options = dict(runtime_options or {})
        activation_locked = bool(options.pop("_activation_locked", False))
        lease = options.pop("lease", None)
        lease_factory = options.pop("lease_factory", None)
        if lease is not None and lease_factory is not None:
            raise ValueError("pass either lease or lease_factory, not both")
        if lease_factory is not None:
            lease = lease_factory()
        recorder = RunLogRecorder()
        options.setdefault("run_log", recorder)

        async def execute_once() -> SubagentResult:
            run = await self.start(
                provider,
                request,
                provider_name=provider_name,
                runtime_options=options,
            )
            subagent_id = (
                run.descriptor.subagent_id if run.descriptor is not None else run.id
            )
            activation = self._activations.get(subagent_id)
            if activation is not None:
                self._publish_activation_state(activation)
            try:
                result = await run.result
            except asyncio.CancelledError:
                await self._dispose_and_settle(
                    run,
                    subagent_id,
                    recorder=recorder,
                    fallback_diag="已取消",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - infra failure still settles the log
                await self._dispose_and_settle(
                    run,
                    subagent_id,
                    recorder=recorder,
                    fallback_diag=str(exc),
                )
                raise
            try:
                outcome = (
                    result
                    if isinstance(result, SubagentResult)
                    else fold_result(result)
                )
                settled = self._settle_completed(
                    subagent_id,
                    outcome,
                    recorder=recorder,
                )
                if run.descriptor is not None and run.descriptor.lifecycle == "continuable":
                    settled = replace(settled, subagent_id=subagent_id)
                    activation = self._activations.get(subagent_id)
                    if activation is not None:
                        activation.last_result = settled
                return settled
            finally:
                try:
                    await run.dispose()
                except Exception:  # noqa: BLE001 - cleanup never replaces the outcome
                    log.exception("关闭子智能体运行 %s 失败", run.id)
                if run.descriptor is None or run.descriptor.lifecycle != "continuable":
                    self._close_edge_quietly(subagent_id)
                activation = self._activations.get(subagent_id)
                if activation is not None:
                    activation.running = False
                    self._publish_activation_state(activation)

        continuable_request = None
        raw_request = request
        if raw_request is None and provider is not None and not isinstance(provider, str):
            raw_request = provider
        if raw_request is not None:
            candidate = coerce_start_request(raw_request)
            if candidate.lifecycle == "continuable":
                subagent_id = candidate.subagent_id or (
                    f"sub::{candidate.label or 'subagent'}::{uuid.uuid4().hex[:8]}"
                )
                continuable_request = replace(candidate, subagent_id=subagent_id)
                request = continuable_request
                if raw_request is provider:
                    provider = None
        activation_lock = None
        if continuable_request is not None and not activation_locked:
            activation_lock = self._activation_lock(continuable_request.subagent_id or "")

        async def run_with_locks() -> SubagentResult:
            if activation_lock is not None:
                async with activation_lock:
                    activation = self._activations.get(continuable_request.subagent_id or "")
                    if activation is not None:
                        activation.running = True
                    return await execute_once()
            return await execute_once()

        if lease is None:
            return await run_with_locks()
        async with lease:
            return await run_with_locks()

    async def send_message(
        self,
        *,
        subagent_id: str,
        message: str,
        parent_session_id: str,
        owner_account_id: str | None = None,
        trigger_turn: bool = True,
    ) -> dict[str, Any]:
        """Deliver a message to an authorized continuable child."""

        message = str(message or "").strip()
        if not message:
            raise ValueError("message must not be empty")
        if self.store is None:
            raise ValueError("continuable subagents require persistence")
        owner = normalize_owner_account_id(owner_account_id)
        row = self.store.get_run(subagent_id)
        if row is None or normalize_owner_account_id(row.get("owner_account_id")) != owner:
            raise KeyError(subagent_id)
        if str(row.get("parent_session_id") or "") != str(parent_session_id):
            raise PermissionError("subagent is not owned by this parent session")
        descriptor = self.store.read_descriptor(subagent_id)
        if not descriptor or descriptor.get("lifecycle") != "continuable":
            raise ValueError("subagent is not continuable")
        if not self._persisted_identity_matches(
            subagent_id=subagent_id,
            parent_session_id=parent_session_id,
            row=row,
            descriptor=descriptor,
        ):
            raise PermissionError("subagent identity does not match its persisted parent")
        activation = self._ensure_activation_from_payload(descriptor, row)
        async with activation.lock:
            if not trigger_turn:
                self.store.enqueue_message(
                    subagent_id=subagent_id,
                    parent_session_id=parent_session_id,
                    owner_account_id=owner,
                    body=message,
                )
                self._publish_activation_state(activation)
                return {
                    "status": "queued",
                    "subagent_id": subagent_id,
                    "state": "ready",
                }
            pending_rows = self.store.take_messages(
                subagent_id=subagent_id,
                parent_session_id=parent_session_id,
                owner_account_id=owner,
            )
            messages = [
                *[str(row.get("body") or "") for row in pending_rows],
                *activation.pending_messages,
                message,
            ]
            activation.pending_messages.clear()
            activation.running = True
            self._publish_activation_state(activation)
            try:
                result = await self.execute(
                    StartRequest(
                        label=str(descriptor.get("label") or row.get("label") or "subagent"),
                        goal_text="\n\n".join(messages),
                        parent_session_id=parent_session_id,
                        owner_account_id=owner,
                        workspace_id=row.get("workspace_id"),
                        provider=str(descriptor.get("provider") or row.get("provider") or self.default_provider),
                        lifecycle="continuable",
                        subagent_id=subagent_id,
                        parent_depth=int(descriptor.get("delegation_depth") or row.get("depth") or 0) - 1,
                        spec=descriptor.get("spec") if isinstance(descriptor.get("spec"), Mapping) else {},
                    ),
                    runtime_options={
                        "_activation_locked": True,
                        "trusted_preset_spec": (
                            descriptor.get("spec", {}).get("_preset_spec")
                            if isinstance(descriptor.get("spec"), Mapping)
                            else None
                        ),
                    },
                )
            finally:
                activation.running = False
                self._publish_activation_state(activation)
            return {
                "status": result.status,
                "subagent_id": subagent_id,
                "state": "idle",
                "result": result.to_legacy_dict(
                    agent=str(descriptor.get("label") or row.get("label") or "subagent")
                ),
            }

    def interrupt_agent(
        self,
        *,
        subagent_id: str,
        parent_session_id: str,
        owner_account_id: str | None = None,
        message: str | None = None,
    ) -> bool:
        if self.store is None:
            return False
        row = self.store.get_run(subagent_id)
        if row is None:
            return False
        if normalize_owner_account_id(row.get("owner_account_id")) != normalize_owner_account_id(owner_account_id):
            return False
        if str(row.get("parent_session_id") or "") != str(parent_session_id):
            return False
        descriptor = self.store.read_descriptor(subagent_id)
        if not descriptor or not self._persisted_identity_matches(
            subagent_id=subagent_id,
            parent_session_id=parent_session_id,
            row=row,
            descriptor=descriptor,
        ):
            return False
        interrupt_child = getattr(self.active, "interrupt_child", None)
        if callable(interrupt_child) and interrupt_child(subagent_id, message):
            return True
        activation = self._activations.get(subagent_id)
        if activation is not None:
            activation.pending_messages.clear()
            self.store.cancel_messages(
                subagent_id,
                owner_account_id=normalize_owner_account_id(owner_account_id),
            )
            self._publish_activation_state(activation)
            return True
        return False

    async def execute_batch(
        self,
        items: Iterable[
            tuple[StartRequest | Mapping[str, Any], Mapping[str, Any] | None]
        ],
        *,
        policy: BatchSchedulePolicy | None = None,
        now_fn: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> list[SubagentResult]:
        """Execute a batch with launch ramp, rate-limit backoff and stable order.

        调度语义：首发立即放 ``initial_burst`` 个，此后每 ``launch_interval``
        放 1 个；并发受容量（初始 ``max_concurrent``）约束。识别为限流的结果
        不落槽——条目按指数退避重排（同一请求原样重放，不要求父级重写简报），
        容量收缩（进入限流时取已成功起跑数、此后按 ``shrink_interval`` 再降，
        下限 1），``recover_interval`` 无新限流才 +1 恢复；重试超过
        ``max_retries`` 或该条目已是唯一未完成槽时按失败落槽（防死等）。
        结果按原始索引定序聚合。``now_fn``/``sleep`` 可注入假时钟做确定性
        时序测试。
        """

        schedule = policy or BatchSchedulePolicy()
        now_fn = now_fn or time.monotonic
        sleep = sleep or asyncio.sleep
        materialized = list(items)
        total = len(materialized)
        results: list[SubagentResult | None] = [None] * total
        if total == 0:
            return []

        @dataclass(slots=True)
        class _Item:
            index: int
            request: StartRequest | Mapping[str, Any]
            options: Mapping[str, Any] | None
            retry_count: int = 0
            retry_ready_at: float = 0.0

        pending: deque[_Item] = deque(
            _Item(index, request_item, options)
            for index, (request_item, options) in enumerate(materialized)
        )
        in_flight: dict[asyncio.Task[SubagentResult], _Item] = {}
        wake = asyncio.Event()
        capacity = max(1, int(schedule.max_concurrent))
        launched_total = 0
        last_launch_at = float("-inf")
        successful_launches = 0
        rate_limit_mode = False
        last_rate_limit_at: float | None = None
        last_shrink_at: float | None = None
        last_recover_at: float | None = None
        rate_launch_gate: float | None = None

        def unfinished_slots() -> int:
            return sum(1 for slot in results if slot is None)

        def enter_rate_limit_mode(now: float) -> None:
            nonlocal capacity, rate_limit_mode, rate_launch_gate
            nonlocal last_rate_limit_at, last_shrink_at
            if not rate_limit_mode:
                # 进入限流：容量取此前成功起跑数，随后立即先收缩 1。
                rate_limit_mode = True
                capacity = max(1, successful_launches)
                capacity = max(1, capacity - 1)
                last_shrink_at = now
            elif (
                last_shrink_at is None
                or now - last_shrink_at >= schedule.shrink_interval
            ):
                capacity = max(1, capacity - 1)
                last_shrink_at = now
            gate_base = now + schedule.backoff_base
            rate_launch_gate = (
                gate_base
                if rate_launch_gate is None
                else max(rate_launch_gate, gate_base)
            )
            last_rate_limit_at = now

        def requeue(item: _Item, now: float) -> None:
            delay = schedule.backoff_base * (
                schedule.backoff_factor ** max(0, item.retry_count - 1)
            )
            item.retry_ready_at = now + delay
            pending.appendleft(item)

        async def run_item(item: _Item) -> SubagentResult:
            try:
                return await self.execute(
                    item.request,
                    runtime_options=item.options,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one batch item
                return SubagentResult(stop_reason="error", diagnostic=str(exc))

        def on_done(task: asyncio.Task[SubagentResult]) -> None:
            wake.set()

        try:
            while True:
                now = now_fn()
                # 1) 收割已完成任务
                for task in list(in_flight):
                    if not task.done():
                        continue
                    item = in_flight.pop(task)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        results[item.index] = SubagentResult(
                            stop_reason="aborted", diagnostic="已取消"
                        )
                        continue
                    if looks_rate_limited(result.diagnostic):
                        item.retry_count += 1
                        # 两条终止路径：重试超上限；或该条目已是唯一未完成槽
                        # （继续独自重排无法推进批次，防死等）。
                        only_unfinished = (
                            unfinished_slots() == 1 and not pending and not in_flight
                        )
                        hard_cap = item.retry_count > schedule.max_retries
                        if hard_cap or only_unfinished:
                            results[item.index] = result
                        else:
                            requeue(item, now)
                            enter_rate_limit_mode(now)
                            log.info(
                                "批量条目 %d 疑似限流，第 %d 次重排（容量 %d）",
                                item.index,
                                item.retry_count,
                                capacity,
                            )
                    else:
                        successful_launches += 1
                        results[item.index] = result
                if all(slot is not None for slot in results):
                    break

                # 2) 容量恢复：限流静默足够久才 +1，逐次间隔 recover_interval
                if (
                    rate_limit_mode
                    and capacity < schedule.max_concurrent
                    and last_rate_limit_at is not None
                ):
                    reference = max(
                        last_rate_limit_at, last_recover_at or float("-inf")
                    )
                    if now - reference >= schedule.recover_interval:
                        capacity += 1
                        last_recover_at = now
                        # 恢复即放行发射闸门；条目级重试就绪时间仍独立生效。
                        rate_launch_gate = None

                # 3) 发射：并发 < 容量 且 过爬坡闸门 与 限流闸门 且 条目重试就绪
                while pending and len(in_flight) < capacity:
                    now = now_fn()
                    if launched_total >= schedule.initial_burst:
                        if now - last_launch_at < schedule.launch_interval:
                            break
                    if (
                        rate_limit_mode
                        and rate_launch_gate is not None
                        and now < rate_launch_gate
                    ):
                        break
                    ready = next(
                        (
                            entry
                            for entry in pending
                            if entry.retry_ready_at <= now
                        ),
                        None,
                    )
                    if ready is None:
                        break
                    pending.remove(ready)
                    launched_total += 1
                    last_launch_at = now
                    task = asyncio.get_running_loop().create_task(run_item(ready))
                    in_flight[task] = ready
                    task.add_done_callback(on_done)
                if all(slot is not None for slot in results):
                    break

                # 4) 等待：任一完成事件 或 下一个时间闸门
                wake.clear()
                now = now_fn()
                wakeups: list[float] = []
                if pending:
                    if (
                        launched_total >= schedule.initial_burst
                        and schedule.launch_interval > 0
                    ):
                        wakeups.append(last_launch_at + schedule.launch_interval)
                    if rate_limit_mode and rate_launch_gate is not None:
                        wakeups.append(rate_launch_gate)
                    for entry in pending:
                        if entry.retry_ready_at > now:
                            wakeups.append(entry.retry_ready_at)
                delay = min(wakeups) - now if wakeups else None
                waiters = {
                    asyncio.ensure_future(wake.wait()),
                }
                if delay is not None and delay > 0:
                    waiters.add(asyncio.ensure_future(sleep(delay)))
                if len(waiters) == 1 and not in_flight and delay is None:
                    # 无在途、无闸门、仍有 pending —— 条目全部未就绪且无唤醒源
                    # 是不可达态（重试就绪时间必在 wakeups 里）；防御性让步。
                    await asyncio.sleep(0)
                    continue
                done_waiters, _ = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED
                )
                for waiter in waiters - done_waiters:
                    waiter.cancel()
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            raise

        return [slot if slot is not None else SubagentResult(
            stop_reason="error", diagnostic="批次调度提前结束"
        ) for slot in results]

    async def execute_many(
        self,
        items: Iterable[
            tuple[StartRequest | Mapping[str, Any], Mapping[str, Any] | None]
        ],
        *,
        max_concurrent: int,
    ) -> list[SubagentResult]:
        """Compatibility entry: bounded, order-stable batch with default pacing."""

        return await self.execute_batch(
            items,
            policy=BatchSchedulePolicy(max_concurrent=max(1, int(max_concurrent))),
        )

    # Explicit alias makes the intent clear to callers migrating from a
    # direct runner while keeping ``execute`` convenient for adapters.
    run = execute

    async def aclose(self) -> None:
        """Dispose all published runs; safe to call repeatedly."""

        if self._closed:
            return
        self._closed = True
        runs = list(self._runs.values())
        for run in reversed(runs):
            try:
                await run.dispose()
            except Exception:
                # Continue releasing sibling children; callers still receive a
                # quiescent runtime as far as the provider permits.
                pass
        self._runs.clear()
        for subagent_id in tuple(self._activations):
            self._close_edge_quietly(subagent_id)
        self._activations.clear()

    close = aclose

    async def _dispose_and_settle(
        self,
        run: SubagentRun,
        subagent_id: str,
        *,
        recorder: RunLogRecorder,
        fallback_diag: str,
    ) -> None:
        """Cancel/exception path: dispose first, then fold whatever was logged.

        provider 的 finally 会把 turn 结局记入日志；dispose 等待其收尾后，
        结算仍走同一条折叠入口，不留 running 悬行。
        """

        try:
            await run.dispose()
        except BaseException:  # noqa: BLE001 - 取消传播优先，结算尽力而为
            log.debug("取消路径关闭子智能体运行 %s 失败", subagent_id, exc_info=True)
        self._close_edge_quietly(subagent_id)
        outcome = SubagentResult(
            stop_reason=fold_run_outcome(recorder.events, fallback="error"),
            diagnostic=fallback_diag,
        )
        self._settle_completed(subagent_id, outcome, recorder=recorder)

    def _apply_depth_ledger(self, request: StartRequest) -> StartRequest:
        """Depth ledger: the persisted parent depth is a monotonic lower bound.

        持久值只抬高不降低——恢复/重放的子会话即便带着归零的运行时深度，
        也无法借道持久层冒充顶层继续嵌套。
        """

        if self.store is None or not request.parent_session_id:
            return request
        parent_depth = max(0, int(request.parent_depth or 0))
        try:
            parent_run = self.store.get_run(request.parent_session_id)
        except Exception:  # noqa: BLE001 - 账本读失败不阻断委派，按运行时值走
            log.exception("深度账本读取失败 parent=%s", request.parent_session_id)
            return request
        if parent_run is not None:
            parent_depth = max(parent_depth, int(parent_run.get("depth") or 0))
        if parent_depth == request.parent_depth:
            return request
        return replace(request, parent_depth=parent_depth)

    def _persist_publication(
        self,
        descriptor: SubagentDescriptor,
        *,
        start_request: StartRequest,
        events: list[dict[str, Any]],
    ) -> None:
        """唯一发布点：runs 行 + descriptor(first-wins) + 父子边。"""

        if self.store is None:
            return
        try:
            existing = self.store.get_run(descriptor.subagent_id)
            if existing is not None and descriptor.lifecycle == "continuable":
                self.store.reopen_run(descriptor.subagent_id, events=events)
            else:
                self.store.record_run_start(
                    subagent_id=descriptor.subagent_id,
                    parent_session_id=start_request.parent_session_id,
                    owner_account_id=start_request.owner_account_id or "",
                    workspace_id=start_request.workspace_id,
                    depth=descriptor.delegation_depth,
                    mode=start_request.lifecycle,
                    provider=descriptor.provider,
                    label=descriptor.label,
                    events=events,
                )
            self.store.write_descriptor(asdict(descriptor))
            if start_request.parent_session_id:
                self.store.open_edge(
                    descriptor.subagent_id, start_request.parent_session_id
                )
        except SubagentError:
            raise
        except Exception as exc:  # noqa: BLE001 - 落库失败=发布失败，fail-loud
            raise SubagentPublicationError(
                f'failed to persist subagent "{descriptor.label}": {exc}',
            ) from exc

    def _settle_persisted_failure(
        self,
        subagent_id: str,
        *,
        events: list[dict[str, Any]],
        diagnostic: str = "",
    ) -> None:
        """发布后、provider 起跑失败：已落库的 run 按失败结算，不留 running 悬行。"""

        if self.store is None:
            return
        try:
            run = self.store.get_run(subagent_id)
            self.store.settle_run(
                subagent_id,
                stop_reason="error",
                events=[
                    *events,
                    {"event": "run/settle", "data": {
                        "stop_reason": "error",
                        "diagnostic": diagnostic[:500],
                        "timeout_kind": "",
                    }},
                ],
            )
            self.store.close_edge(subagent_id)
            if run is not None:
                self.publish_lifecycle_event(
                    owner_account_id=run.get("owner_account_id"),
                    session_id=str(run.get("parent_session_id") or ""),
                    event="run_settled",
                    payload={
                        "subagent_id": subagent_id,
                        "stop_reason": "error",
                        "status": "settled",
                    },
                )
        except Exception:  # noqa: BLE001 - 结算尽力而为，不掩盖原始失败
            log.exception("失败结算落库失败 subagent_id=%s", subagent_id)

    def _settle_completed(
        self,
        subagent_id: str,
        result: SubagentResult,
        *,
        recorder: RunLogRecorder,
    ) -> SubagentResult:
        """Fold the run log, persist the terminal record, return the result.

        折叠是结局的唯一推导入口：日志可复算的 ``stop_reason`` 优先于
        provider 自报值；超时由运行时按 ``timeout_kind`` 生成（不进入折叠）。
        """

        folded = fold_run_outcome(recorder.events, fallback=result.stop_reason)
        if result.timeout_kind:
            stop_reason = "timed_out"
        else:
            stop_reason = folded
        settled = result
        if settled.stop_reason != stop_reason:
            settled = replace(settled, stop_reason=stop_reason)  # type: ignore[arg-type]
            log.warning(
                "run log 折叠结局与 provider 自报不一致（%s -> %s）subagent_id=%s",
                result.stop_reason,
                stop_reason,
                subagent_id,
            )
        if self.store is None:
            return settled
        run = self.store.get_run(subagent_id)
        recorder.run_settle(
            stop_reason=settled.stop_reason,
            diagnostic=settled.diagnostic,
            timeout_kind=settled.timeout_kind,
        )
        try:
            self.store.settle_run(
                subagent_id,
                stop_reason=settled.stop_reason,
                timeout_kind=settled.timeout_kind,
                events=recorder.events,
                append=bool(run and run.get("mode") == "continuable"),
            )
        except Exception:  # noqa: BLE001 - 结算落库失败不吞掉子级结果
            log.exception("run 结算落库失败 subagent_id=%s", subagent_id)
        if run is not None:
            self.publish_lifecycle_event(
                owner_account_id=run.get("owner_account_id"),
                session_id=str(run.get("parent_session_id") or ""),
                event="run_settled",
                payload={
                    "subagent_id": subagent_id,
                    "stop_reason": settled.stop_reason,
                    "status": "settled",
                },
            )
        return settled

    def _activation_lock(self, subagent_id: str) -> asyncio.Lock:
        activation = self._activations.get(subagent_id)
        if activation is not None:
            return activation.lock
        return self._ensure_activation_placeholder(subagent_id).lock

    def _ensure_activation_placeholder(self, subagent_id: str) -> ContinuableActivation:
        activation = self._activations.get(subagent_id)
        if activation is not None:
            return activation
        activation = ContinuableActivation(
            descriptor=SubagentDescriptor(subagent_id=subagent_id, lifecycle="continuable"),
            owner_account_id="",
            workspace_id=None,
        )
        self._activations[subagent_id] = activation
        return activation

    def _activation_state(self, activation: ContinuableActivation) -> str:
        if activation.running:
            return "running"
        if activation.pending_messages:
            return "ready"
        if self.store is not None and self.store.has_pending_messages(
            activation.descriptor.subagent_id,
            owner_account_id=activation.owner_account_id,
        ):
            return "ready"
        return "idle"

    def _publish_activation_state(
        self,
        activation: ContinuableActivation,
        *,
        force: bool = False,
    ) -> None:
        descriptor = activation.descriptor
        state = self._activation_state(activation)
        if not descriptor.parent_session_id:
            return
        if not force and activation.last_state == state:
            return
        activation.last_state = state
        self.publish_lifecycle_event(
            owner_account_id=activation.owner_account_id,
            session_id=descriptor.parent_session_id,
            event=f"activation_{state}",
            payload={
                "subagent_id": descriptor.subagent_id,
                "label": descriptor.label or "subagent",
                "provider": descriptor.provider,
                "lifecycle": descriptor.lifecycle,
                "state": state,
            },
        )

    @staticmethod
    def _persisted_identity_matches(
        *,
        subagent_id: str,
        parent_session_id: str,
        row: Mapping[str, Any],
        descriptor: Mapping[str, Any],
    ) -> bool:
        expected_id = str(subagent_id or "")
        if not expected_id:
            return False
        if str(row.get("subagent_id") or expected_id) != expected_id:
            return False
        if str(descriptor.get("subagent_id") or "") != expected_id:
            return False
        expected_parent = str(parent_session_id or "")
        if str(row.get("parent_session_id") or "") != expected_parent:
            return False
        if str(descriptor.get("parent_session_id") or "") != expected_parent:
            return False
        return descriptor.get("lifecycle") == "continuable"

    def _ensure_activation(
        self,
        descriptor: SubagentDescriptor,
        *,
        owner_account_id: str | None,
        workspace_id: str | None,
    ) -> ContinuableActivation:
        current = self._activations.get(descriptor.subagent_id)
        if current is not None:
            if (
                not current.descriptor.parent_session_id
                and descriptor.parent_session_id
            ):
                current.descriptor = descriptor
                current.owner_account_id = normalize_owner_account_id(owner_account_id)
                current.workspace_id = workspace_id
                return current
            if current.descriptor != descriptor and current.descriptor.spec:
                raise SubagentPublicationError(
                    f'continuable descriptor collision for "{descriptor.subagent_id}"'
                )
            return current
        activation = ContinuableActivation(
            descriptor=descriptor,
            owner_account_id=normalize_owner_account_id(owner_account_id),
            workspace_id=workspace_id,
        )
        self._activations[descriptor.subagent_id] = activation
        return activation

    def _ensure_activation_from_payload(
        self,
        payload: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> ContinuableActivation:
        descriptor = SubagentDescriptor(
            version=int(payload.get("version", 1)),
            lifecycle="continuable",
            provider=str(payload.get("provider") or row.get("provider") or self.default_provider),
            parent_session_id=str(payload.get("parent_session_id") or row.get("parent_session_id") or ""),
            label=payload.get("label") or row.get("label"),
            model=payload.get("model"),
            persona=payload.get("persona"),
            tool_filter=tuple(payload.get("tool_filter") or ()),
            delegation_depth=int(payload.get("delegation_depth") or row.get("depth") or 1),
            subagent_id=str(payload.get("subagent_id") or row.get("subagent_id") or ""),
            session_persistence=(
                "hidden"
                if payload.get("session_persistence") == "hidden"
                or payload.get("lifecycle") == "continuable"
                else "none"
            ),
            spec=dict(payload.get("spec") or {}),
        )
        return self._ensure_activation(
            descriptor,
            owner_account_id=row.get("owner_account_id"),
            workspace_id=row.get("workspace_id"),
        )

    def _close_edge_quietly(self, subagent_id: str) -> None:
        if self.store is None:
            return
        try:
            self.store.close_edge(subagent_id)
        except Exception:  # noqa: BLE001
            log.debug("关闭父子边失败 subagent_id=%s", subagent_id, exc_info=True)

    def _validate_request(
        self,
        request: StartRequest,
        provider: SubagentProvider,
    ) -> None:
        prompt = request.effective_prompt
        if not prompt:
            raise ValueError("subagent prompt must not be empty")
        if request.lifecycle != "one_shot":
            if not callable(getattr(provider, "prepare_continuable", None)):
                raise SubagentCapabilityError(provider.name, "continuable")
        if request.output_schema is not None and not provider.capabilities.output_schema:
            raise SubagentCapabilityError(provider.name, "output_schema")
        if request.max_depth is not None and not provider.capabilities.depth_limit:
            raise SubagentCapabilityError(provider.name, "depth_limit")
        if request.tool_filter is not None and not provider.capabilities.tool_filter:
            raise SubagentCapabilityError(provider.name, "tool_filter")
        if request.persona is not None and not provider.capabilities.persona:
            raise SubagentCapabilityError(provider.name, "persona")
        if request.agent_options is not None and not provider.capabilities.agent_options:
            raise SubagentCapabilityError(provider.name, "agent_options")
        if request.agent_options is not None:
            unsupported = set(request.agent_options) - {"model", "max_iterations"}
            if unsupported:
                names = ", ".join(sorted(str(name) for name in unsupported))
                raise ValueError(f"unsupported in-process agent options: {names}")

        depth = request.child_depth
        if depth > self.max_depth:
            raise SubagentDepthError(depth, self.max_depth)
        if request.max_depth is not None:
            maximum = self._validate_non_negative_int(request.max_depth, "max_depth")
            if depth > maximum:
                raise SubagentDepthError(depth, maximum)

    @staticmethod
    def _validate_non_negative_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _as_tool_filter(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("tool_filter must be a sequence of names")
        result: list[str] = []
        for item in value:
            name = str(item).strip()
            if name and name not in result:
                result.append(name)
        return tuple(result)

    @staticmethod
    def _resolved_request(
        request: StartRequest,
        *,
        provider: str,
        descriptor: SubagentDescriptor,
        runtime_options: Mapping[str, Any] | None,
    ) -> ResolvedStartRequest:
        payload = {
            item.name: getattr(request, item.name)
            for item in fields(StartRequest)
        }
        payload["provider"] = provider
        payload["subagent_id"] = descriptor.subagent_id
        return ResolvedStartRequest(
            **payload,
            descriptor=descriptor,
            runtime_options=dict(runtime_options or {}),
        )


# 已完成后台任务完成事件的保留时长：模型从不 collect 时的兜底清扫 TTL
_BG_EVENT_TTL_SECONDS = 3600.0


class _BgEventEntry:
    """后台子任务的完成事件表项：事件 + 完成时间戳（done_at=None 表示仍在运行）。"""

    __slots__ = ("event", "done_at")

    def __init__(self, event: asyncio.Event) -> None:
        self.event = event
        self.done_at: float | None = None


def _sweep_stale_bg_events(
    entries: dict[str, _BgEventEntry], *, now: float, ttl: float,
) -> None:
    """清扫已完成且超过 TTL 的后台任务 entry（collect 之外的兜底，防永久驻留）。"""
    for task_id, entry in list(entries.items()):
        if entry.done_at is not None and now - entry.done_at > ttl:
            entries.pop(task_id, None)


class LegacySubagentBackgroundRuntime:
    """Own the old task-board/background lifecycle behind the service seam."""

    def __init__(
        self,
        service: SubagentService,
        *,
        tasks: Any,
        launch_background: Callable[[Awaitable[Any]], None],
        idle_timeout: float,
        max_runtime: float,
        on_done: Callable[[str, dict[str, Any]], None] | None = None,
        on_collected: Callable[[str, str, str], None] | None = None,
        capacity: Callable[[], bool] | None = None,
        summary_floor: Mapping[str, int] | None = None,
    ) -> None:
        self._service = service
        self._tasks = tasks
        self._launch_background = launch_background
        self._idle_timeout = idle_timeout
        self._max_runtime = max_runtime
        self._on_done = on_done
        self._on_collected = on_collected
        self._capacity = capacity
        self._summary_floor = dict(summary_floor or {})
        self._events: dict[str, _BgEventEntry] = {}

    async def _touch_activity(self, task_id: str, progress: dict[str, Any]) -> None:
        """活动落库：优先走异步门面（内部 to_thread），无门面时退回同步方法。"""
        try:
            touch_async = getattr(self._tasks, "touch_activity_async", None)
            if callable(touch_async):
                await touch_async(task_id, progress)
                return
            touch = getattr(self._tasks, "touch_activity", None)
            if callable(touch):
                touch(task_id, progress)
        except Exception:  # noqa: BLE001 - 活动戳是 best-effort
            log.debug("后台子任务活动落库失败 task_id=%s", task_id, exc_info=True)

    async def launch(
        self,
        *,
        item: Mapping[str, Any],
        parent_session_id: str,
        parent_task_id: str | None,
        owner_account_id: str,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        if self._capacity is not None and not self._capacity():
            return {
                "status": "rejected",
                "reason": "后台子任务已达并发上限，请先用 collect_subagent 取回已完成的，或稍后再试",
            }

        label = str(item.get("label") or "subagent")
        goal_text = str(item.get("goal_text") or "")
        # 任务存储走异步门面（内部 to_thread），事件循环上的调用不直接同步写库；
        # 无门面的轻量实现（如测试替身）退回同步方法。
        create_runtime_async = getattr(self._tasks, "create_runtime_async", None)
        create_runtime = getattr(self._tasks, "create_runtime", None)
        if callable(create_runtime_async) or callable(create_runtime):
            create_kwargs = {
                "kind": "subagent",
                "session_id": parent_session_id or "subagent",
                "parent_task_id": parent_task_id,
                "title": goal_text[:40],
                "detail": goal_text,
                "assignee": label,
                "inactivity_timeout": self._idle_timeout,
                "execution_timeout": self._max_runtime,
                "backgrounded": True,
                "owner_account_id": owner_account_id,
            }
            if callable(create_runtime_async):
                task = await create_runtime_async(**create_kwargs)
            else:
                task = create_runtime(**create_kwargs)
            task_id = str(task.get("task_id") or task.get("id") or "")
            mark_running_async = getattr(self._tasks, "mark_running_async", None)
            if callable(mark_running_async):
                await mark_running_async(task_id)
            else:
                self._tasks.mark_running(task_id)
        else:
            create_kwargs = {
                "title": goal_text[:40],
                "detail": goal_text,
                "assignee": label,
                "owner_account_id": owner_account_id,
            }
            create_async = getattr(self._tasks, "create_async", None)
            if callable(create_async):
                task = await create_async(parent_session_id or "subagent", **create_kwargs)
            else:
                task = self._tasks.create(parent_session_id or "subagent", **create_kwargs)
            task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id:
            raise RuntimeError("background task manager returned no task id")

        subagent_id = ""
        if bool(getattr(self._service, "continuable_enabled", False)):
            subagent_id = str(item.get("subagent_id") or "").strip() or (
                f"sub::{label}::{uuid.uuid4().hex[:8]}"
            )
            item["subagent_id"] = subagent_id
            await self._touch_activity(task_id, {"subagent_id": subagent_id})

        done_event = asyncio.Event()
        # 写入前顺带清扫：模型从不 collect 时，已完成超 TTL 的 entry 在此兜底回收
        _sweep_stale_bg_events(
            self._events, now=time.monotonic(), ttl=_BG_EVENT_TTL_SECONDS,
        )
        entry = _BgEventEntry(done_event)
        self._events[task_id] = entry

        async def _run_and_mark_done() -> None:
            try:
                await self._run(
                    task_id=task_id,
                    item=dict(item),
                    parent_session_id=parent_session_id,
                    owner_account_id=owner_account_id,
                    workspace_id=workspace_id,
                    done_event=done_event,
                )
            finally:
                entry.done_at = time.monotonic()

        self._launch_background(_run_and_mark_done())
        return {
            "status": "launched",
            "task_id": task_id,
            **({"subagent_id": subagent_id} if subagent_id else {}),
            "agent": label,
            "hint": "用 collect_subagent(task_id) 取结果；完成时也会推送通知",
        }

    async def _run(
        self,
        *,
        task_id: str,
        item: dict[str, Any],
        parent_session_id: str,
        owner_account_id: str,
        workspace_id: str | None,
        done_event: asyncio.Event,
    ) -> None:
        label = str(item.get("label") or "subagent")
        spec = dict(item.get("spec") or {})
        has_touch = callable(getattr(self._tasks, "touch_activity_async", None)) or callable(
            getattr(self._tasks, "touch_activity", None)
        )

        def _touch_progress(progress: dict[str, Any]) -> None:
            # 活动戳是 best-effort 且每个 chunk 都可能触发：走异步门面调度，避免在事件循环上忙等
            asyncio.ensure_future(self._touch_activity(task_id, progress))

        options: dict[str, Any] = {
            "idle_timeout": self._idle_timeout,
            "max_runtime": self._max_runtime,
            "progress_callback": _touch_progress if has_touch else None,
        }
        binding = item.get("preset_binding")
        if binding is not None:
            options["lease_factory"] = lambda: binding.acquire_lease(
                f"agent-preset:{label}"
            )
            trusted = spec.get("_preset_spec")
            if isinstance(trusted, dict):
                options["trusted_preset_spec"] = dict(trusted)
        summary_policy = summary_policy_from_spec(spec, self._summary_floor)
        if summary_policy is not None:
            options["summary_policy"] = summary_policy

        result: dict[str, Any]
        agent_message = ""
        subagent_id = str(item.get("subagent_id") or "").strip()
        try:
            raw_agent_options = spec.get("_agent_options")
            requests_tool_filter = any(
                spec.get(key) is not None for key in ("toolsets", "tools")
            )
            outcome = await self._service.execute(
                StartRequest(
                    label=label,
                    goal_text=str(item.get("goal_text") or ""),
                    parent_session_id=parent_session_id,
                    spec=spec,
                    provider=str(spec.get("provider") or "").strip(),
                    agent_options=(
                        dict(raw_agent_options)
                        if isinstance(raw_agent_options, dict)
                        else None
                    ),
                    tool_filter=() if requests_tool_filter else None,
                    owner_account_id=owner_account_id,
                    workspace_id=workspace_id,
                    subagent_id=subagent_id or None,
                    lifecycle=(
                        "continuable"
                        if bool(getattr(self._service, "continuable_enabled", False))
                        else "one_shot"
                    ),
                ),
                runtime_options=options,
            )
            agent_message = outcome.output
            result = outcome.to_legacy_dict(agent=label)
            subagent_id = str(outcome.subagent_id or subagent_id).strip()
            if subagent_id:
                await self._touch_activity(task_id, {"subagent_id": subagent_id})
        except asyncio.CancelledError:
            result = {
                "agent": label,
                "status": "cancelled",
                "summary": "已取消",
                "duration_seconds": 0,
                "tool_calls": 0,
                **({"subagent_id": subagent_id} if subagent_id else {}),
            }
            raise
        except Exception as exc:  # noqa: BLE001 - task failure is persisted as data
            result = {
                "agent": label,
                "status": "error",
                "summary": str(exc),
                "duration_seconds": 0,
                "tool_calls": 0,
                **({"subagent_id": subagent_id} if subagent_id else {}),
            }
        finally:
            result["task_id"] = task_id
            if subagent_id:
                result["subagent_id"] = subagent_id
            # 先路由通知、后落库触发恢复回合：update_status 的 on_completion
            # 会调度恢复轮，若它先于入队执行，恢复轮会 drain 到空队列。
            if self._on_done is not None and parent_session_id:
                try:
                    notification = dict(result)
                    notification.update(
                        {
                            "task_id": task_id,
                            "owner_account_id": owner_account_id,
                            "source": "subagent-settled",
                            "agent_message": agent_message,
                            "settlement": {
                                "status": result.get("status"),
                                "stop_reason": result.get("stop_reason"),
                            },
                        }
                    )
                    self._on_done(parent_session_id, notification)
                except Exception:  # noqa: BLE001 - completion notification is best effort
                    log.debug("后台子任务完成回调失败 task_id=%s", task_id, exc_info=True)
            try:
                # 任务与子身份账本需要能从 collect 和任务看板双向定位。
                persisted = {
                    key: value
                    for key, value in result.items()
                    if key != "owner_account_id"
                }
                update_status_async = getattr(self._tasks, "update_status_async", None)
                if callable(update_status_async):
                    await update_status_async(
                        task_id,
                        _LEGACY_TASK_STATUS.get(result["status"], "failed"),
                        json.dumps(persisted, ensure_ascii=False),
                    )
                else:
                    self._tasks.update_status(
                        task_id,
                        _LEGACY_TASK_STATUS.get(result["status"], "failed"),
                        json.dumps(persisted, ensure_ascii=False),
                    )
            except Exception:  # noqa: BLE001 - notification still settles
                log.exception("后台子任务落库失败 task_id=%s", task_id)
            done_event.set()

    async def _get_task(self, task_id: str, owner_account_id: str) -> dict[str, Any]:
        get_async = getattr(self._tasks, "get_async", None)
        if callable(get_async):
            return await get_async(task_id, owner_account_id=owner_account_id)
        return self._tasks.get(task_id, owner_account_id=owner_account_id)

    async def collect(
        self,
        task_id: str,
        *,
        wait: bool,
        owner_account_id: str,
    ) -> str:
        task = await self._get_task(task_id, owner_account_id)

        def consume(current: dict[str, Any]) -> str:
            self._events.pop(task_id, None)
            if self._on_collected is not None:
                self._on_collected(
                    str(current.get("session_id") or ""),
                    task_id,
                    str(current.get("owner_account_id") or owner_account_id),
                )
            return current.get("result") or json.dumps(
                {"status": current.get("status")},
                ensure_ascii=False,
            )

        if task.get("status") in {
            "done", "completed", "failed", "cancelled", "timed_out",
        }:
            return consume(task)
        if not wait:
            return json.dumps(
                {"status": "running", "task_id": task_id},
                ensure_ascii=False,
            )

        entry = self._events.get(task_id)
        if entry is not None:
            try:
                cap = max(self._idle_timeout or 0, self._max_runtime or 0)
                if cap > 0:
                    await asyncio.wait_for(entry.event.wait(), timeout=cap + 30)
                else:
                    await entry.event.wait()
            except asyncio.TimeoutError:
                return json.dumps(
                    {
                        "status": "running",
                        "task_id": task_id,
                        "note": "等待超时，稍后再 collect",
                    },
                    ensure_ascii=False,
                )
        return consume(await self._get_task(task_id, owner_account_id))


def fold_result(value: SubagentResult | Mapping[str, Any] | Any) -> SubagentResult:
    """Fold a provider result or legacy result mapping into seam data."""

    if isinstance(value, SubagentResult):
        return value
    if not isinstance(value, Mapping):
        return SubagentResult(stop_reason="error", diagnostic=str(value))
    reason = value.get("stop_reason", value.get("status", "error"))
    output = str(value.get("output", "") or "")
    partial = str(value.get("partial_output", "") or "")
    if not output and value.get("summary") and str(reason) == "completed":
        output = str(value["summary"])
    timeout_kind = value.get("timeout_kind")
    if timeout_kind not in {None, "idle", "absolute"}:
        timeout_kind = None
    return SubagentResult(
        output=output,
        stop_reason=str(reason).replace("-", "_"),  # type: ignore[arg-type]
        diagnostic=str(value.get("diagnostic", value.get("error", "")) or ""),
        partial_output=partial,
        timeout_kind=timeout_kind,
        structured=value.get("structured") if isinstance(value.get("structured"), dict) else None,
        tool_calls=int(value.get("tool_calls", 0) or 0),
        last_tool=str(value.get("last_tool", "") or ""),
        duration_seconds=float(value.get("duration_seconds", 0.0) or 0.0),
    )


fold_subagent_result = fold_result


__all__ = [
    "BatchSchedulePolicy",
    "LegacySubagentBackgroundRuntime",
    "RunLogRecorder",
    "SubagentRuntime",
    "fold_result",
    "fold_run_outcome",
    "fold_subagent_result",
    "looks_rate_limited",
]
