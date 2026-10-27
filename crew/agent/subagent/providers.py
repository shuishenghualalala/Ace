"""Built-in subagent transports.

``InProcessSpawnProvider`` owns only the transport-specific child loop.  It
does not decide which provider is allowed, compute delegation depth, or
format model-facing tool responses; those concerns belong to
``SubagentRuntime`` and the tool adapter respectively.
"""

from __future__ import annotations

import asyncio
import json
import inspect
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from crew.core.envelope import Envelope
from crew.core.runctx import (
    current_owner_account_id,
    current_structured_output_sink,
    current_workspace_id,
)
from crew.state.logging import get_logger

from crew.agent.subagent.service import (
    ResolvedStartRequest,
    SubagentCapabilities,
    SubagentCapabilityError,
    SubagentResult,
    SubagentRun,
    validate_structured_output,
)

log = get_logger("subagent")

_PARTIAL_CAP = 2000
_PARTIAL_TAIL = 800
INPROCESS_SPAWN_PROVIDER = "inprocess-spawn"
INPROCESS_FORK_PROVIDER = "inprocess-fork"


def _canonical_child_session_id(parent_session_id: str, subagent_id: str) -> str:
    """Return the hidden Session Store id used by a continuable child."""

    if not parent_session_id:
        return subagent_id
    return f"{parent_session_id}::subagent::{subagent_id}"

# 摘要兜底的扩写指令：要求子级在不重跑工具的前提下补全自包含汇报。
SUMMARY_CONTINUATION_PROMPT = (
    "你上一条回复过于简短。请基于已完成的工作，给出更完整的自包含结果汇报："
    "包含关键发现、具体改动或数据、以及结论，让父智能体无需重读过程即可继续。"
)


def _chunk_value(chunk: Any, key: str, default: Any = None) -> Any:
    body = getattr(chunk, "body", None)
    if not isinstance(body, Mapping):
        return default
    return body.get(key, default)


class InProcessSpawnProvider:
    """Run one child through Ace's existing in-process Agent interface.

    The provider is intentionally constructed with a child factory.  This
    keeps application composition (tool filtering, preset policy, and model
    setup) outside the transport seam while still allowing unit tests to use a
    tiny fake child.
    """

    name = INPROCESS_SPAWN_PROVIDER
    inherits_parent_context = False
    capabilities = SubagentCapabilities(
        output_schema=True,
        depth_limit=True,
        tool_filter=True,
        persona=False,
        agent_options=True,
    )

    def __init__(
        self,
        build_child: Any | None = None,
        *,
        active: Any | None = None,
        idle_timeout: float = 120.0,
        max_runtime: float = 1800.0,
    ) -> None:
        self._build_child = build_child
        self._active = active
        self._idle_timeout = idle_timeout
        self._max_runtime = max_runtime

    def prepare_continuable(self, _request: ResolvedStartRequest) -> None:
        """Declare support for continuable identities.

        The Agent runtime persists the child session under the stable
        ``subagent_id``.  Each turn can therefore rebuild the lightweight
        in-process Agent while retaining the canonical conversation history.
        """

        return None

    async def start(self, request: ResolvedStartRequest) -> SubagentRun:
        """Publish a child run and return its holder-owned handle."""

        factory = self._build_child
        if factory is None:
            raise ValueError("inprocess-spawn requires a build_child factory")

        label = request.label or request.descriptor.label or "subagent"
        child_spec = dict(request.spec)
        child_spec["_delegation_depth"] = request.descriptor.delegation_depth
        if request.tool_filter:
            child_spec["toolsets"] = None
            child_spec["tools"] = (
                [request.tool_filter]
                if isinstance(request.tool_filter, str)
                else list(request.tool_filter)
            )
        for key in ("model", "max_iterations"):
            if request.agent_options is not None and key in request.agent_options:
                child_spec[key] = request.agent_options[key]
        try:
            create_child = getattr(factory, "create_child", None)
            if callable(create_child):
                from crew.core.interfaces import ChildAgentRequest

                child = create_child(
                    ChildAgentRequest.from_mapping(
                        child_spec,
                        trusted_preset_spec=request.runtime_options.get(
                            "trusted_preset_spec"
                        ),
                    )
                )
            elif callable(factory):
                child = factory(child_spec)
            else:
                raise TypeError("child factory must be callable or expose create_child()")
        except Exception as exc:  # noqa: BLE001 - publication has not happened
            raise RuntimeError(f"failed to create subagent {label!r}: {exc}") from exc

        child_id = request.descriptor.subagent_id or (
            f"sub::{label}::{uuid.uuid4().hex[:8]}"
        )
        parent_session_id = request.parent_session_id or ""
        child_session_id = (
            _canonical_child_session_id(parent_session_id, child_id)
            if request.session_persistence == "hidden"
            else child_id
        )
        owner_account_id = request.owner_account_id or current_owner_account_id.get()
        workspace_id = request.workspace_id or current_workspace_id.get()
        env = Envelope.of(
            request.effective_prompt,
            session_id=child_session_id,
            params={
                "task_session_id": (
                    child_session_id
                    if request.session_persistence == "hidden"
                    else parent_session_id or child_id
                ),
                "parent_session_id": parent_session_id,
                "agent_id": label,
                "subagent_id": child_id,
            },
            channel="subagent",
            mode="agent",
            workspace_id=workspace_id,
            user_id=request.user_id or owner_account_id,
        )

        runtime_options = request.runtime_options
        active = runtime_options.get("active") or self._active
        try:
            if active is not None and parent_session_id:
                active.register(
                    parent_session_id,
                    child_id,
                    {
                        "child_id": child_id,
                        "label": label,
                        "agent": child,
                        "started_at": time.time(),
                        "owner_account_id": owner_account_id,
                    },
                )
        except Exception:
            if active is not None and parent_session_id:
                try:
                    active.unregister(parent_session_id, child_id)
                except Exception:  # noqa: BLE001 - preserve publication failure
                    log.debug("回滚子智能体活跃记录失败 %s", label, exc_info=True)
            await _close_child_quietly(child, label, phase="发布回滚")
            raise

        started = time.perf_counter()
        disposed = False
        run_task: asyncio.Task[SubagentResult] | None = None

        async def run_child() -> SubagentResult:
            nonlocal disposed
            partial = ""
            last_tool = ""
            tool_calls = 0

            recorder = runtime_options.get("run_log")
            idle_timeout = runtime_options.get("idle_timeout", self._idle_timeout)
            max_runtime = runtime_options.get("max_runtime", self._max_runtime)
            progress_callback = runtime_options.get("progress_callback")
            summary_policy = runtime_options.get("summary_policy")
            seed_messages = runtime_options.get("seed_messages")
            seed_store = runtime_options.get("session_store")
            if seed_messages and seed_store is not None:
                save_async = getattr(seed_store, "save_async", None)
                if callable(save_async):
                    await save_async(
                        child_session_id,
                        list(seed_messages),
                        workspace_id=workspace_id or "default",
                        owner_account_id=owner_account_id,
                    )
            summary_min_chars = 0
            summary_retries = 0
            if isinstance(summary_policy, Mapping):
                summary_min_chars = max(
                    0, int(summary_policy.get("min_chars", 0) or 0)
                )
                summary_retries = max(
                    0, int(summary_policy.get("retries", 0) or 0)
                )
            submitted_structured: list[dict[str, Any]] = []

            def submit_structured(value: Any) -> None:
                if submitted_structured:
                    raise ValueError("structured_output 只能提交一次")
                if not isinstance(value, Mapping):
                    raise TypeError("structured_output.value 必须是 JSON 对象")
                if request.output_schema is not None:
                    validate_structured_output(value, request.output_schema)
                submitted_structured.append(dict(value))

            async def consume_turn(
                env: Envelope,
            ) -> tuple[str, str, str | None, str, dict[str, Any] | None]:
                """消费一个回合；返回结局、诊断、超时、正文和结构化结果。

                记账 turn 边界：进入记 ``turn/start``，退出记 ``turn/end``（refusal
                按 dsh 词汇记 ``blocked``，折叠端再映射回来）与 ``output/final``。
                """

                nonlocal partial, tool_calls, last_tool
                if recorder is not None:
                    recorder.turn_start()
                final_text = ""
                stop_reason = "completed"
                diagnostic = ""
                timeout_kind: str | None = None
                structured_value: dict[str, Any] | None = None
                generator: Any = None
                sink_token = current_structured_output_sink.set(
                    submit_structured if request.output_schema is not None else None
                )

                async def note_turn_end() -> None:
                    if recorder is None:
                        return
                    log_reason = "blocked" if stop_reason == "refusal" else stop_reason
                    recorder.turn_end(log_reason)
                    if final_text:
                        recorder.output_final(final_text)

                try:
                    generator = child.run(env).__aiter__()
                    while True:
                        remaining: float | None = (
                            idle_timeout if idle_timeout and idle_timeout > 0 else None
                        )
                        timeout_reason = "idle"
                        if max_runtime and max_runtime > 0:
                            left = max_runtime - (time.perf_counter() - started)
                            if left <= 0:
                                stop_reason = "timed_out"
                                timeout_kind = "absolute"
                                break
                            if remaining is None or left <= remaining:
                                remaining = left
                                timeout_reason = "absolute"
                        try:
                            if remaining is None:
                                chunk = await generator.__anext__()
                            else:
                                chunk = await asyncio.wait_for(
                                    generator.__anext__(),
                                    timeout=remaining,
                                )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            stop_reason = "timed_out"
                            timeout_kind = timeout_reason
                            break

                        kind = str(getattr(chunk, "kind", "") or "")
                        if kind == "final":
                            final_text = str(_chunk_value(chunk, "text", "") or "")
                            raw_structured = _chunk_value(chunk, "structured")
                            if isinstance(raw_structured, Mapping):
                                structured_value = dict(raw_structured)
                            reason = _chunk_value(chunk, "reason")
                            if reason:
                                normalized = str(reason).strip().lower().replace("-", "_")
                                if normalized in {"max_tokens", "refusal", "aborted"}:
                                    stop_reason = normalized
                        elif kind == "delta":
                            text = str(_chunk_value(chunk, "text", "") or "")
                            if text:
                                partial += text
                                if len(partial) > _PARTIAL_CAP:
                                    partial = partial[-_PARTIAL_CAP:]
                        elif kind == "tool" and _chunk_value(chunk, "phase") == "start":
                            tool_calls += 1
                            last_tool = str(_chunk_value(chunk, "name", "") or last_tool)
                        if submitted_structured:
                            structured_value = dict(submitted_structured[0])
                            final_text = json.dumps(structured_value, ensure_ascii=False)
                            break
                        elif kind == "error":
                            stop_reason = "error"
                            diagnostic = str(
                                _chunk_value(chunk, "message", "子智能体执行出错")
                                or "子智能体执行出错"
                            )
                            break

                        callback = progress_callback
                        if callback is not None:
                            callback(
                                {
                                    "agent": label,
                                    "tool_calls": tool_calls,
                                    "last_tool": last_tool,
                                    "partial_output": (final_text or partial)[-_PARTIAL_TAIL:],
                                    "last_chunk": kind,
                                }
                            )
                except asyncio.CancelledError:
                    # Disposal is a normal one-shot outcome, not an unhandled task
                    # rejection.  The child still receives interrupt below.
                    stop_reason = "aborted"
                    diagnostic = "已取消"
                except Exception as exc:  # noqa: BLE001 - one child must not poison siblings
                    stop_reason = "error"
                    diagnostic = str(exc)
                    log.exception("子智能体 %s 执行异常", label)
                finally:
                    # 先下中断标志再关流：aclose() 会把 GeneratorExit 推进子 agent 的收尾
                    # 逻辑，interrupt 必须在关流前设置，收尾阶段才观察得到
                    if stop_reason in {"timed_out", "aborted"}:
                        interrupt = getattr(child, "interrupt", None)
                        if callable(interrupt):
                            try:
                                interrupt(
                                    "子任务超时"
                                    if stop_reason == "timed_out"
                                    else "子任务已取消"
                                )
                            except Exception:  # noqa: BLE001
                                log.debug("中断子智能体 %s 失败", label, exc_info=True)
                    if generator is not None:
                        try:
                            await generator.aclose()
                        except Exception:  # noqa: BLE001
                            log.debug("关闭子智能体迭代器 %s 失败", label, exc_info=True)
                    current_structured_output_sink.reset(sink_token)
                    if submitted_structured:
                        structured_value = dict(submitted_structured[0])
                    if request.output_schema is not None and stop_reason == "completed":
                        candidate: Any = structured_value
                        if candidate is None and final_text.strip():
                            try:
                                candidate = json.loads(final_text)
                            except (TypeError, ValueError):
                                candidate = None
                        if not isinstance(candidate, Mapping):
                            stop_reason = "error"
                            diagnostic = "结构化输出缺失或不是 JSON 对象"
                            final_text = ""
                        else:
                            try:
                                validate_structured_output(candidate, request.output_schema)
                            except ValueError as exc:
                                stop_reason = "error"
                                diagnostic = f"结构化输出校验失败：{exc}"
                                structured_value = None
                                final_text = ""
                            else:
                                structured_value = dict(candidate)
                    await note_turn_end()
                return stop_reason, diagnostic, timeout_kind, final_text, structured_value

            (
                stop_reason,
                diagnostic,
                timeout_kind,
                final_text,
                structured_value,
            ) = await consume_turn(env)

            # 摘要兜底：正常交卷但过短时，在同一子级上追加扩写轮。非正常交卷
            # （超时/取消/错误/上限/拒绝）与绝对预算耗尽都不触发；扩写轮只补
            # 正文，不把主轮结论改写成失败。
            summary_attempts = 0
            while (
                summary_min_chars > 0
                and summary_retries > summary_attempts
                and stop_reason == "completed"
                and structured_value is None
                and len((final_text or "").strip()) < summary_min_chars
                and (not max_runtime or max_runtime <= 0
                     or time.perf_counter() - started < max_runtime)
            ):
                summary_attempts += 1
                log.info(
                    "子智能体 %s 交卷过短（%d < %d），追加第 %d 轮扩写",
                    label,
                    len((final_text or "").strip()),
                    summary_min_chars,
                    summary_attempts,
                )
                continuation = Envelope.of(
                    SUMMARY_CONTINUATION_PROMPT,
                    session_id=child_session_id,
                    params={
                        "task_session_id": (
                            child_session_id
                            if request.session_persistence == "hidden"
                            else parent_session_id or child_id
                        ),
                        "parent_session_id": parent_session_id,
                        "agent_id": label,
                        "subagent_id": child_id,
                    },
                    channel="subagent",
                    mode="agent",
                    workspace_id=workspace_id,
                    user_id=request.user_id or owner_account_id,
                )
                _reason, _diag, _kind, continued_text, continued_structured = await consume_turn(
                    continuation
                )
                if continued_text and continued_text.strip():
                    final_text = continued_text
                if continued_structured is not None:
                    structured_value = continued_structured
                if _reason != "completed":
                    break

            # 超时/取消的中断已在 consume_turn 收尾（关流前）或 dispose() 中下发，
            # 此处不再重复设置。

            # Error/timeout text is diagnostic data, not assistant output.  A
            # parent may still present it through ``SubagentResult.summary``;
            # keeping the channels separate prevents runtime failures from
            # being mistaken for something the child said.
            output = final_text
            if stop_reason == "timed_out":
                elapsed = time.perf_counter() - started
                why = "无活动超时" if timeout_kind == "idle" else "达到运行上限"
                diagnostic = (
                    diagnostic
                    or f"子任务于 {elapsed:.0f}s {why}中止：已执行 "
                    f"{tool_calls} 次工具调用"
                )
                if last_tool:
                    diagnostic += f"（最后工具：{last_tool}）"
            return SubagentResult(
                output=output,
                stop_reason=stop_reason,  # type: ignore[arg-type]
                diagnostic=diagnostic,
                partial_output=partial[-_PARTIAL_TAIL:],
                timeout_kind=timeout_kind,  # type: ignore[arg-type]
                structured=structured_value,
                tool_calls=tool_calls,
                last_tool=last_tool,
                duration_seconds=time.perf_counter() - started,
            )

        async def cleanup_child() -> None:
            close = getattr(child, "aclose", None)
            if callable(close):
                try:
                    returned = close()
                    if inspect.isawaitable(returned):
                        await returned
                except Exception:  # noqa: BLE001 - cleanup never rewrites outcome
                    log.exception("关闭子智能体 %s 的 owned 资源失败", label)
            if active is not None and parent_session_id:
                try:
                    active.unregister(parent_session_id, child_id)
                except Exception:  # noqa: BLE001
                    log.debug("注销子智能体 %s 失败", label, exc_info=True)

        async def run_child_with_cleanup() -> SubagentResult:
            try:
                return await run_child()
            finally:
                await cleanup_child()

        loop = asyncio.get_running_loop()
        run_task = loop.create_task(run_child_with_cleanup(), name=f"subagent:{child_id}")

        async def dispose() -> None:
            nonlocal disposed
            if disposed:
                if run_task is not None:
                    await _await_task_quietly(run_task)
                return
            disposed = True
            interrupt = getattr(child, "interrupt", None)
            if callable(interrupt):
                try:
                    interrupt("子任务已取消")
                except Exception:  # noqa: BLE001
                    log.debug("中断子智能体 %s 失败", label, exc_info=True)
            if run_task is not None and not run_task.done():
                run_task.cancel()
            if run_task is not None:
                await _await_task_quietly(run_task)

        descriptor = request.descriptor
        if not descriptor.subagent_id:
            descriptor = type(descriptor)(
                version=descriptor.version,
                lifecycle=descriptor.lifecycle,
                provider=descriptor.provider,
                parent_session_id=descriptor.parent_session_id,
                label=descriptor.label,
                model=descriptor.model,
                persona=descriptor.persona,
                tool_filter=descriptor.tool_filter,
                delegation_depth=descriptor.delegation_depth,
                subagent_id=child_id,
                session_persistence=descriptor.session_persistence,
                spec=descriptor.spec,
            )
        return SubagentRun(
            child_id,
            run_task,
            dispose=dispose,
            local_agent=child,
            descriptor=descriptor,
        )


def fork_completed_prefix(messages: list[Any]) -> list[Any]:
    """Keep the completed user-visible prefix for a forked child session."""

    last_completed = -1
    for index, message in enumerate(messages):
        if (
            getattr(message, "role", "") == "assistant"
            and not getattr(message, "tool_calls", None)
        ):
            last_completed = index
    if last_completed < 0:
        return []
    return [
        message
        for message in messages[: last_completed + 1]
        if not bool(getattr(message, "is_meta", False))
        and getattr(message, "source_kind", None)
        not in {"system_reminder", "skill_context", "agent_reminder"}
    ]


class InProcessForkProvider(InProcessSpawnProvider):
    """Start a child from a structurally filtered parent-session snapshot."""

    name = INPROCESS_FORK_PROVIDER
    inherits_parent_context = True
    capabilities = SubagentCapabilities(
        output_schema=True,
        depth_limit=True,
        tool_filter=True,
        persona=False,
        agent_options=False,
    )

    def __init__(self, *args: Any, session_store: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_store = session_store

    def prepare_continuable(self, _request: ResolvedStartRequest) -> None:
        raise SubagentCapabilityError(self.name, "continuable")

    async def start(self, request: ResolvedStartRequest) -> SubagentRun:
        store = self._session_store or request.runtime_options.get("session_store")
        if store is None:
            raise ValueError("inprocess-fork requires a session store")
        parent_session_id = request.parent_session_id.strip()
        owner_account_id = request.owner_account_id or current_owner_account_id.get()
        messages: list[Any] = []
        if parent_session_id:
            load_async = getattr(store, "load_async", None)
            if callable(load_async):
                messages = list(
                    await load_async(parent_session_id, owner_account_id=owner_account_id)
                )
        options = dict(request.runtime_options)
        options["session_store"] = store
        options["seed_messages"] = fork_completed_prefix(messages)
        return await super().start(replace(request, runtime_options=options))


async def _await_task_quietly(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - disposal must remain idempotent
        log.debug("等待子任务收尾失败", exc_info=True)


async def _close_child_quietly(child: Any, label: str, *, phase: str) -> None:
    close = getattr(child, "aclose", None)
    if not callable(close):
        return
    try:
        returned = close()
        if inspect.isawaitable(returned):
            await returned
    except Exception:  # noqa: BLE001 - rollback must preserve the original failure
        log.exception("%s关闭子智能体 %s 失败", phase, label)


__all__ = [
    "INPROCESS_FORK_PROVIDER",
    "INPROCESS_SPAWN_PROVIDER",
    "InProcessForkProvider",
    "InProcessSpawnProvider",
    "fork_completed_prefix",
]
