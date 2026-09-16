"""Semantic subagent runtime.

The runtime is the single preflight and publication boundary for one-shot
delegations.  Providers execute work; this class validates capabilities,
assigns the descriptor, and makes disposal observable and idempotent to the
consumer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import fields
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
from crew.state.logging import get_logger


log = get_logger("subagent.runtime")

_LEGACY_TASK_STATUS = {
    "completed": "done",
    "timeout": "failed",
    "error": "failed",
}


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
        self._runs: dict[str, SubagentRun] = {}
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
        )
        resolved = self._resolved_request(
            start_request,
            provider=selected_provider,
            descriptor=descriptor,
            runtime_options=runtime_options,
        )
        try:
            run = await provider_impl.start(resolved)
        except SubagentError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider setup is pre-publication
            raise SubagentPublicationError(
                f'failed to publish subagent "{label}": {exc}',
            ) from exc
        if not isinstance(run, SubagentRun):
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
        lease = options.pop("lease", None)
        lease_factory = options.pop("lease_factory", None)
        if lease is not None and lease_factory is not None:
            raise ValueError("pass either lease or lease_factory, not both")
        if lease_factory is not None:
            lease = lease_factory()

        async def execute_once() -> SubagentResult:
            run = await self.start(
                provider,
                request,
                provider_name=provider_name,
                runtime_options=options,
            )
            try:
                result = await run.result
                return result if isinstance(result, SubagentResult) else fold_result(result)
            finally:
                try:
                    await run.dispose()
                except Exception:  # noqa: BLE001 - cleanup never replaces the outcome
                    log.exception("关闭子智能体运行 %s 失败", run.id)

        if lease is None:
            return await execute_once()
        async with lease:
            return await execute_once()

    async def execute_many(
        self,
        items: Iterable[
            tuple[StartRequest | Mapping[str, Any], Mapping[str, Any] | None]
        ],
        *,
        max_concurrent: int,
    ) -> list[SubagentResult]:
        """Execute independent requests with bounded, order-stable concurrency.

        A request that fails before publication becomes an error result for its
        own batch slot; it never cancels sibling work. External cancellation of
        the whole batch still propagates normally.
        """

        materialized = list(items)
        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def execute_one(
            request_item: StartRequest | Mapping[str, Any],
            options: Mapping[str, Any] | None,
        ) -> SubagentResult:
            async with semaphore:
                try:
                    return await self.execute(
                        request_item,
                        runtime_options=options,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate one batch item
                    return SubagentResult(
                        stop_reason="error",
                        diagnostic=str(exc),
                    )

        return list(
            await asyncio.gather(
                *(execute_one(request_item, options) for request_item, options in materialized)
            )
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

    close = aclose

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
    ) -> None:
        self._service = service
        self._tasks = tasks
        self._launch_background = launch_background
        self._idle_timeout = idle_timeout
        self._max_runtime = max_runtime
        self._on_done = on_done
        self._on_collected = on_collected
        self._capacity = capacity
        self._events: dict[str, asyncio.Event] = {}

    def launch(
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
        create_runtime = getattr(self._tasks, "create_runtime", None)
        if callable(create_runtime):
            task = create_runtime(
                kind="subagent",
                session_id=parent_session_id or "subagent",
                parent_task_id=parent_task_id,
                title=goal_text[:40],
                detail=goal_text,
                assignee=label,
                inactivity_timeout=self._idle_timeout,
                execution_timeout=self._max_runtime,
                backgrounded=True,
                owner_account_id=owner_account_id,
            )
            task_id = str(task.get("task_id") or task.get("id") or "")
            self._tasks.mark_running(task_id)
        else:
            task = self._tasks.create(
                parent_session_id or "subagent",
                title=goal_text[:40],
                detail=goal_text,
                assignee=label,
                owner_account_id=owner_account_id,
            )
            task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id:
            raise RuntimeError("background task manager returned no task id")

        done_event = asyncio.Event()
        self._events[task_id] = done_event
        self._launch_background(
            self._run(
                task_id=task_id,
                item=dict(item),
                parent_session_id=parent_session_id,
                owner_account_id=owner_account_id,
                workspace_id=workspace_id,
                done_event=done_event,
            )
        )
        return {
            "status": "launched",
            "task_id": task_id,
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
        options: dict[str, Any] = {
            "idle_timeout": self._idle_timeout,
            "max_runtime": self._max_runtime,
            "progress_callback": (
                lambda progress: self._tasks.touch_activity(task_id, progress)
                if hasattr(self._tasks, "touch_activity")
                else None
            ),
        }
        binding = item.get("preset_binding")
        if binding is not None:
            options["lease_factory"] = lambda: binding.acquire_lease(
                f"agent-preset:{label}"
            )
            trusted = spec.get("_preset_spec")
            if isinstance(trusted, dict):
                options["trusted_preset_spec"] = dict(trusted)

        result: dict[str, Any]
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
                ),
                runtime_options=options,
            )
            result = outcome.to_legacy_dict(agent=label)
        except asyncio.CancelledError:
            result = {
                "agent": label,
                "status": "cancelled",
                "summary": "已取消",
                "duration_seconds": 0,
                "tool_calls": 0,
            }
            raise
        except Exception as exc:  # noqa: BLE001 - task failure is persisted as data
            result = {
                "agent": label,
                "status": "error",
                "summary": str(exc),
                "duration_seconds": 0,
                "tool_calls": 0,
            }
        finally:
            try:
                self._tasks.update_status(
                    task_id,
                    _LEGACY_TASK_STATUS.get(result["status"], "failed"),
                    json.dumps(result, ensure_ascii=False),
                )
            except Exception:  # noqa: BLE001 - notification still settles
                log.exception("后台子任务落库失败 task_id=%s", task_id)
            done_event.set()

        if self._on_done is not None and parent_session_id:
            try:
                result["task_id"] = task_id
                result["owner_account_id"] = owner_account_id
                self._on_done(parent_session_id, result)
            except Exception:  # noqa: BLE001 - completion notification is best effort
                log.debug("后台子任务完成回调失败 task_id=%s", task_id, exc_info=True)

    async def collect(
        self,
        task_id: str,
        *,
        wait: bool,
        owner_account_id: str,
    ) -> str:
        task = self._tasks.get(task_id, owner_account_id=owner_account_id)

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

        event = self._events.get(task_id)
        if event is not None:
            try:
                cap = max(self._idle_timeout or 0, self._max_runtime or 0)
                if cap > 0:
                    await asyncio.wait_for(event.wait(), timeout=cap + 30)
                else:
                    await event.wait()
            except asyncio.TimeoutError:
                return json.dumps(
                    {
                        "status": "running",
                        "task_id": task_id,
                        "note": "等待超时，稍后再 collect",
                    },
                    ensure_ascii=False,
                )
        return consume(
            self._tasks.get(task_id, owner_account_id=owner_account_id)
        )


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
    "LegacySubagentBackgroundRuntime",
    "SubagentRuntime",
    "fold_result",
    "fold_subagent_result",
]
