"""Dynamic Kanban Manager：按 session 管理 workflow 生命周期。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.interfaces import Agent, LLMProvider, MemoryProvider, SessionStore
from crew.core.types import Message
from crew.dynamickanban.orchestrator import WorkflowOrchestrator
from crew.dynamickanban.runtime import WorkflowRuntime
from crew.dynamickanban.runtime_models import (
    WORKFLOW_DEFINITION_SCHEMA_VERSION,
    RuntimeState,
    WorkflowDefinition,
    WorkflowDefinitionMigrationError,
)
from crew.dynamickanban.store import SQLiteKanbanStore
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.state.logging import get_logger
from crew.tools.registry import Registry

log = get_logger("dynamickanban.manager")

AgentFactory = Callable[..., Agent]


class DynamicKanbanManager:
    """Dynamic Kanban 的入口管理器。"""

    def __init__(
        self,
        store: SQLiteKanbanStore,
        provider: LLMProvider,
        base_registry: Registry,
        session_store: SessionStore,
        memory: MemoryProvider,
        plugins: PluginManager,
        config: Config,
        agent_factory: AgentFactory | None = None,
        on_runtime_chunk: Callable[[str, ResponseChunk, str], Any] | None = None,
        provider_for_owner: Callable[[str], LLMProvider] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.base_registry = base_registry
        self.session_store = session_store
        self.memory = memory
        self.plugins = plugins
        self.config = config
        self.agent_factory = agent_factory
        self.on_runtime_chunk = on_runtime_chunk
        self.provider_for_owner = provider_for_owner
        self._orchestrators: dict[str, WorkflowOrchestrator] = {}
        self._engines: dict[tuple[str, str], WorkflowRuntime] = {}
        self._lock = threading.Lock()
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._max_concurrent = getattr(config, "dk_max_concurrent", 0) or config.team_max_concurrent_children or 3

    def _provider_for_owner(self, owner_account_id: str = "") -> LLMProvider:
        resolver = self.provider_for_owner
        if callable(resolver):
            resolved = resolver(str(owner_account_id or ""))
            if resolved is not None:
                return resolved
        return self.provider

    def _orchestrator_for_owner(self, owner_account_id: str = "") -> WorkflowOrchestrator:
        owner = str(owner_account_id or "")
        orchestrator = self._orchestrators.get(owner)
        if orchestrator is None:
            orchestrator = WorkflowOrchestrator(self._provider_for_owner(owner))
            self._orchestrators[owner] = orchestrator
        return orchestrator

    def drop_owner_provider_state(self, owner_account_id: str) -> None:
        """让指定 owner 的下一次编排使用刚更新的默认模型。"""
        self._orchestrators.pop(str(owner_account_id or ""), None)

    def clear_provider_state(self) -> None:
        self._orchestrators.clear()

    def _default_agent_factory(
        self,
        *,
        registry: Registry,
        system_prompt: str,
        agent_id: str = "dk_worker",
        lightweight: bool = True,
        user_type: str = "internal",
        **kwargs: Any,
    ) -> Agent:
        """默认 agent 工厂：如果外部没注入，就用一个最小化的 SingleAgent。"""
        from crew.agent.runtime import SingleAgent
        from crew.agent.executor import BuiltinExecutor
        from crew.agent.loop import ToolCallGuardrailConfig
        from crew.agent.compact import ContextCompactor

        cfg = self.config
        assignee = str(kwargs.get("assignee") or agent_id).strip()
        provider = self._provider_for_owner(str(kwargs.get("owner_account_id") or ""))

        # access control 与 runtime 限制共同决定 worker 可用工具。
        ac = cfg.access_control.resolve_for(user_type)
        tool_filter = self._build_tool_filter(
            ac,
            registry,
            extra_enabled_toolsets=kwargs.get("extra_enabled_toolsets") or None,
            extra_disabled_toolsets=kwargs.get("extra_disabled_toolsets") or None,
        )

        # runtime 可额外禁用某些具名工具（例如 clarify 阶段禁止 file_write）
        extra_disabled = kwargs.get("extra_disabled_tools") or []
        if extra_disabled and tool_filter is not None:
            tool_filter = [t for t in tool_filter if t not in set(extra_disabled)]

        enabled_skills, disabled_skills = self._build_skill_scope(ac)

        # Dynamic Kanban worker 可由调用方显式指定迭代上限；未指定时沿用全局配置
        agent_max_iterations = kwargs.get("max_iterations")
        if agent_max_iterations is None:
            agent_max_iterations = cfg.max_iterations
        else:
            agent_max_iterations = int(agent_max_iterations)

        compactor = ContextCompactor(
            provider,
            enabled=False,
            store=None,
        )
        guardrail_config = ToolCallGuardrailConfig(
            warnings_enabled=True,
            hard_stop_enabled=cfg.guardrail_enabled and cfg.guardrail_hard_stop,
            exact_failure_block_after=cfg.guardrail_exact_failure_block_after,
            no_progress_block_after=cfg.guardrail_no_progress_block_after,
        )
        executor = BuiltinExecutor(
            provider,
            registry,
            self.plugins,
            max_iterations=agent_max_iterations,
            max_retries=cfg.retry_max,
            backoff_seconds=cfg.retry_backoff,
            guardrail_config=guardrail_config,
            parallel_tools=cfg.parallel_tools,
            empty_retry_max=cfg.empty_retry_max,
            continuation_max=cfg.continuation_max,
            max_parallel_tool_calls=cfg.max_parallel_tool_calls,
            max_delegate_tool_calls=0,
            compactor=compactor,
        )
        return SingleAgent(
            provider=provider,
            registry=registry,
            session_store=self.session_store,
            memory=self.memory,
            plugins=self.plugins,
            system_prompt=system_prompt,
            max_iterations=agent_max_iterations,
            executor=executor,
            compactor=compactor,
            lightweight=lightweight,
            user_type=user_type,
            agent_id=assignee or "dk_worker",
            tool_filter=tool_filter,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
            include_optional_skills=False,
        )

    def _build_tool_filter(
        self,
        ac: dict[str, Any],
        registry: Registry,
        *,
        extra_enabled_toolsets: list[str] | None = None,
        extra_disabled_toolsets: list[str] | None = None,
    ) -> list[str] | None:
        """计算 worker 可用工具：access_control 与 runtime 额外限制的交集。"""
        base_enabled = ac.get("enabled_toolsets") if ac is not None else None
        base_disabled = ac.get("disabled_toolsets") if ac is not None else None
        base_enabled_tools = ac.get("enabled_tools") if ac is not None else None
        base_disabled_tools = ac.get("disabled_tools") if ac is not None else None

        # runtime 可进一步收窄可用 toolset，同时仍受 access_control 约束
        enabled_toolsets = base_enabled
        if extra_enabled_toolsets is not None:
            if enabled_toolsets is not None:
                enabled_toolsets = [s for s in enabled_toolsets if s in extra_enabled_toolsets]
            else:
                enabled_toolsets = list(extra_enabled_toolsets)
        disabled_toolsets = list(base_disabled or [])
        if extra_disabled_toolsets:
            disabled_toolsets = list(set(disabled_toolsets) | set(extra_disabled_toolsets))

        base_names = {
            s["function"]["name"]
            for s in registry.list_schemas(
                enabled_toolsets=enabled_toolsets or None,
                disabled_toolsets=disabled_toolsets or None,
                enabled_tools=base_enabled_tools,
                disabled_tools=base_disabled_tools,
            )
        }
        allowed = list(base_names)

        # Dynamic Kanban worker 不直接调用 external_agent 工具。
        toolset_for = getattr(registry, "toolset_for", None)
        if callable(toolset_for):
            allowed = [name for name in allowed if toolset_for(name) != "external_agent"]

        # Dynamic Kanban 工具必须始终对 worker 可见；空交集时不返回 None（None 会被视为“允许全部”）
        kanban_names = [
            name for name in registry.names()
            if getattr(registry.get(name), "toolset", None) == "dynamic_kanban"
        ]
        allowed = list(dict.fromkeys(allowed + kanban_names))
        return allowed

    def _build_skill_scope(
        self,
        ac: dict[str, Any],
    ) -> tuple[list[str] | None, list[str] | None]:
        """读取 worker 的 skills 黑白名单。"""
        enabled = ac.get("enabled_skills") if ac is not None else None
        disabled = ac.get("disabled_skills") if ac is not None else None
        return enabled or None, disabled or None

    def _make_runtime(
        self,
        store: SQLiteKanbanStore,
        owner_account_id: str = "",
    ) -> WorkflowRuntime:
        provider = self._provider_for_owner(owner_account_id)
        return WorkflowRuntime(
            store=store,
            agent_factory=self.agent_factory or self._default_agent_factory,
            base_registry=self.base_registry,
            provider=provider,
            max_concurrent=self._max_concurrent,
            task_timeout_seconds=self.config.dk_task_timeout_seconds,
            config=self.config,
            extra_disabled_tools=["run_dynamic_kanban", "get_dynamic_kanban_status", "steer_dynamic_kanban"],
            orchestrator=self._orchestrator_for_owner(owner_account_id),
            max_replans=getattr(self.config, "dk_max_replans", 2) or 0,
        )

    async def _run_workflow_core(
        self,
        query: str,
        envelope: Envelope,
        original_session_id: str,
    ) -> AsyncIterator[ResponseChunk]:
        """workflow 执行核心：复用/创建 workflow、生成 definition、执行 runtime。

        不负责持久化到主会话历史，也不负责实时推送；调用方自行决定。
        """
        owner = str(envelope.user_id or "").strip()
        store = self.store.for_owner(owner)
        session_key = (owner, original_session_id)
        session_lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with session_lock:
            workspace_id = envelope.workspace_id or "default"
            existing_wf = store.get_latest_active_workflow_by_session(
                original_session_id,
                active_statuses={"active"},
            )
            if existing_wf is not None:
                workflow = existing_wf
                workflow = store.update_workflow_status(
                    workflow.id,
                    # 必须是 workflow 状态词（active|paused|done|failed）。这里曾误用
                    # TaskRun 的 "running"：写进去后该 workflow 对 interrupt/steer/pause
                    # 的 {"active"} 查询和这里的复用查询同时不可见——点「停止」静默无效，
                    # 记录也变成再也复用不到的僵尸。
                    "active",
                    title=query,
                    context={
                        **(workflow.context or {}),
                        "workspace_id": workspace_id,
                        "user_id": envelope.user_id,
                        "channel": envelope.channel,
                    },
                )
                log.info("复用 session %s 的 workflow %s", original_session_id, workflow.id)
            else:
                workflow = store.create_workflow(
                    session_id=original_session_id,
                    title=query,
                    context={
                        "workspace_id": workspace_id,
                        "user_id": envelope.user_id,
                        "channel": envelope.channel,
                    },
                )

            definition = await self._load_or_build_definition(
                workflow,
                query,
                owner_account_id=owner,
            )
            workflow.context["workflow_definition"] = definition.to_dict()
            workflow = store.update_workflow_status(
                workflow.id,
                workflow.status,
                context=workflow.context,
            )

            runtime = self._make_runtime(store, owner_account_id=owner)
            engine_key = (owner, workflow.id)
            with self._lock:
                self._engines[engine_key] = runtime
            try:
                async for chunk in runtime.run(workflow, definition, envelope.request_id, envelope):
                    yield chunk
            finally:
                with self._lock:
                    # 仅当登记的仍是当前 runtime 自己时才注销，避免把
                    # resume 新建的运行实例挤掉（pause/resume 竞态窗口）
                    if self._engines.get(engine_key) is runtime:
                        self._engines.pop(engine_key, None)

        if store.get_latest_active_workflow_by_session(
            original_session_id, active_statuses={"active"}
        ) is None:
            self._session_locks.pop(session_key, None)

    async def _run_workflow_with_persistence(
        self,
        query: str,
        envelope: Envelope,
        original_session_id: str,
    ) -> AsyncIterator[ResponseChunk]:
        """直接走 workflow 并持久化运行状态到主会话历史。"""
        async for chunk in self._stream_runtime_with_background(
            self._run_workflow_core(query, envelope, original_session_id),
            request_id=envelope.request_id,
            envelope=envelope,
            session_id=original_session_id,
            background_after_seconds=getattr(
                self.config, "tasks_auto_background_after_seconds", 0.0
            ),
        ):
            yield chunk

    async def _load_or_build_definition(
        self,
        workflow: Any,
        query: str,
        *,
        owner_account_id: str = "",
    ) -> WorkflowDefinition:
        """复用 workflow 中已持久化的 definition，否则让 orchestrator 生成。"""
        stored = (workflow.context or {}).get("workflow_definition")
        if stored is not None:
            try:
                if not isinstance(stored, dict):
                    raise ValueError("stored workflow definition 必须是对象")
                return WorkflowDefinition.from_dict(stored)
            except WorkflowDefinitionMigrationError as exc:
                scoped_store = self.store.for_owner(workflow.owner_account_id)
                scoped_store.quarantine_workflow_definition(
                    workflow.id,
                    exc.diagnostic(),
                )
                log.error(
                    "stored definition 拓扑冲突，workflow=%s 已隔离: %s",
                    workflow.id,
                    exc,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 持久化坏数据必须统一隔离
                scoped_store = self.store.for_owner(workflow.owner_account_id)
                scoped_store.quarantine_workflow_definition(
                    workflow.id,
                    {
                        "error": str(exc),
                        "target_schema_version": WORKFLOW_DEFINITION_SCHEMA_VERSION,
                    },
                )
                log.error(
                    "stored definition 非法，workflow=%s 已隔离: %s",
                    workflow.id,
                    exc,
                )
                raise
        return await self._orchestrator_for_owner(owner_account_id).build_definition(
            query,
            workflow.context or {},
        )

    async def interact(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """处理一次 Dynamic Kanban 请求并把消息持久化到主会话。"""
        query = envelope.query.strip() or "未提供请求"

        # Gateway dispatcher 会为每一轮创建 sidechain session（如 sid::turn::rid），
        # 但 Dynamic Kanban 的 workflow 必须绑定到原始 session，否则前端用原始 session
        # 调 /api/dynamic-kanban/{session_id}/board 会查不到 workflow。
        # task_session_id 可能是第一层 sidechain，仍不是根 session，因此递归去掉
        # ::turn::... 后缀直到拿到根 session。
        def _root_session_id(sid: str) -> str:
            while "::turn::" in sid:
                sid = sid.split("::turn::", 1)[0]
            return sid

        task_session_id = str(envelope.params.get("task_session_id") or "").strip()
        original_session_id = _root_session_id(task_session_id or envelope.session_id)

        owner = str(envelope.user_id or "").strip()
        if not owner:
            log.warning("[DK] interact 缺少 envelope.user_id session=%s", envelope.session_id)
            yield ResponseChunk(
                kind="final",
                request_id=envelope.request_id,
                body={"text": "请求缺少用户身份，无法创建 workflow。"},
                sequence=0,
            )
            return
        # 持久化用户消息（使用原始 session_id，避免写入 sidechain session）
        self._persist_message(
            envelope,
            Message(role="user", content=query, timestamp=time.time()),
            session_id=original_session_id,
        )

        async for chunk in self._run_workflow_with_persistence(
            query,
            envelope,
            original_session_id,
        ):
            yield chunk

    def _persist_message(
        self,
        envelope: Envelope,
        message: Message,
        *,
        session_id: str | None = None,
    ) -> None:
        """把单条消息追加到会话存储，失败仅记录日志不中断流程。

        使用 envelope.user_id 作为 owner_account_id，与 gateway dispatcher
        的会话所有权保持一致；否则消息会写入空 owner，导致按 owner 查询的历史接口返回空。
        """
        append_fn = getattr(self.session_store, "append", None)
        if not callable(append_fn):
            return
        target_sid = session_id if session_id else envelope.session_id
        owner = envelope.user_id or ""
        try:
            append_fn(target_sid, [message], owner_account_id=owner)
            log.info(
                "Dynamic Kanban 已持久化消息 session=%s owner=%s role=%s content_len=%d",
                target_sid,
                owner,
                message.role,
                len(message.content or ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Dynamic Kanban 持久化会话消息失败 session=%s owner=%s: %s", target_sid, owner, exc)

    async def _stream_runtime_with_background(
        self,
        runtime_gen: AsyncIterator[ResponseChunk],
        *,
        request_id: str,
        envelope: Envelope,
        session_id: str,
        background_after_seconds: float,
    ) -> AsyncIterator[ResponseChunk]:
        """流式输出 runtime 结果；超过后台化阈值后自动 detached 继续执行并返回提示。"""
        if background_after_seconds <= 0:
            async for chunk in runtime_gen:
                self._persist_runtime_chunk(envelope, chunk, session_id)
                yield chunk
            return

        queue: asyncio.Queue[ResponseChunk | None] = asyncio.Queue()

        async def _persist_and_maybe_push(chunk: ResponseChunk) -> None:
            self._persist_runtime_chunk(envelope, chunk, session_id)
            if backgrounded and self.on_runtime_chunk:
                owner = envelope.user_id or ""
                # 后台 status / workflow_progress 帧若仍携带原 request_id，桌面端回合封口后会按“旧回合迟到生成帧”丢弃。
                # 清空 request_id 让桌面把它当作无归属控制帧，始终渲染。
                push_chunk = chunk
                if chunk.kind in {"status", "workflow_progress"} and chunk.request_id:
                    push_chunk = ResponseChunk(
                        request_id="",
                        kind=chunk.kind,
                        body=dict(chunk.body),
                        sequence=chunk.sequence,
                        is_final=chunk.is_final,
                        status=chunk.status,
                        ts=chunk.ts,
                    )
                log.info(
                    "Dynamic Kanban 后台推送 chunk session=%s kind=%s request_id=%s",
                    session_id,
                    push_chunk.kind,
                    push_chunk.request_id,
                )
                try:
                    result = self.on_runtime_chunk(session_id, push_chunk, owner)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    log.warning("Dynamic Kanban 后台 chunk 推送失败 session=%s: %s", session_id, exc)

        async def _consumer() -> None:
            log.info("Dynamic Kanban 后台 consumer 启动 session=%s request_id=%s", session_id, request_id)
            try:
                async for chunk in runtime_gen:
                    await _persist_and_maybe_push(chunk)
                    await queue.put(chunk)
            except Exception as exc:  # noqa: BLE001
                log.warning("Dynamic Kanban 后台 consumer 异常 session=%s: %s", session_id, exc)
            finally:
                log.info("Dynamic Kanban 后台 consumer 结束 session=%s", session_id)
                await queue.put(None)

        consumer_task = asyncio.create_task(_consumer())
        deadline = time.time() + background_after_seconds
        backgrounded = False
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0 and not backgrounded:
                    backgrounded = True
                    yield ResponseChunk.status_event(
                        request_id,
                        "⏳ workflow 已转入后台继续执行，可在右侧看板查看进度",
                        0,
                    )
                    return
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    if not backgrounded:
                        continue
                    return
                if chunk is None:
                    break
                yield chunk
        finally:
            if consumer_task.done():
                try:
                    await consumer_task
                except Exception as exc:  # noqa: BLE001
                    log.warning("runtime 后台消费任务异常：%s", exc)
            elif backgrounded:
                # 前台已返回，后台任务继续消费并持久化
                log.info("runtime 前台流已结束，后台任务继续执行 session=%s", session_id)
            else:
                # 前台正常结束但 consumer 可能还在收尾，温和等待
                consumer_task.cancel()

    def _persist_runtime_chunk(
        self,
        envelope: Envelope,
        chunk: ResponseChunk,
        session_id: str,
    ) -> None:
        """把 runtime chunk 中的 status/final 持久化到原始会话。"""
        if chunk.kind == "status":
            body = chunk.body or {}
            agent_name = body.get("agent_name")
            detail = body.get("detail")
            if agent_name:
                return
            message = body.get("message") or ""
            if not message and not detail:
                return
            text = f"{message}\n\n{detail}" if detail else message
            self._persist_message(
                envelope,
                Message(role="assistant", content=text, timestamp=time.time()),
                session_id=session_id,
            )
        elif chunk.kind == "final":
            text = chunk.body.get("text") or chunk.body.get("message") or ""
            if text:
                self._persist_message(
                    envelope,
                    Message(role="assistant", content=text, timestamp=time.time()),
                    session_id=session_id,
                )

    def steer(
        self,
        session_id: str,
        text: str,
        owner_account_id: str = "",
    ) -> bool:
        """向运行中的 workflow 注入补充指令。

        指令追加到 workflow.context["steer_notes"]；运行中的 Runtime 会在下一轮
        主循环读取，并结合指令重规划后续阶段（复用 replan 通道）。
        没有活跃 workflow 时返回 False。
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            log.warning("[DK] steer 缺少 owner_account_id session=%s", session_id)
            return False
        store = self.store.for_owner(owner)
        log.info("[DK] steer owner=%s session=%s text=%s", owner, session_id, text)
        workflow = store.get_latest_active_workflow_by_session(
            session_id, active_statuses={"active"}
        )
        if workflow is None:
            return False
        ctx = dict(workflow.context or {})
        notes = list(ctx.get("steer_notes") or [])
        notes.append({"text": str(text or "").strip(), "ts": time.time()})
        ctx["steer_notes"] = notes
        store.update_workflow_status(workflow.id, workflow.status, context=ctx)
        return True

    def interrupt(
        self,
        session_id: str,
        message: str | None = None,
        owner_account_id: str = "",
    ) -> bool:
        """请求中断某 session 最新活跃的 workflow。

        runtime 在内存中时设置停止标志，由 runtime 在主循环顶部收尾；
        没有 runtime（paused 或崩溃残留的 active 僵尸）时直接在 DB 层
        把剩余任务和 workflow 标记为失败，保证中止始终生效。
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            log.warning("[DK] interrupt 缺少 owner_account_id session=%s", session_id)
            return False
        store = self.store.for_owner(owner)
        log.info("[DK] interrupt owner=%s session=%s message=%s", owner, session_id, message)
        workflow = store.get_latest_active_workflow_by_session(
            session_id, active_statuses={"active", "paused"}
        )
        if workflow is None:
            return False
        with self._lock:
            runtime = self._engines.get((owner, workflow.id))
        if runtime is not None:
            runtime.request_stop()
            return True
        # 没有运行中的 runtime：paused（runtime 已退出）或崩溃残留的 active 僵尸。
        # 直接在 DB 层收尾：剩余任务标失败，workflow 落 failed 终态。
        reason = str(message or "").strip() or "被用户中断"
        for task in store.list_tasks(workflow.id):
            if task.status in ("pending", "ready", "running"):
                store.update_task_status(task.id, "failed", result_summary=reason)
        state = store.load_runtime_state(workflow.id)
        if state is not None:
            state.status = "failed"
            store.save_runtime_state(state)
        store.update_workflow_status(
            workflow.id,
            "failed",
            context={**(workflow.context or {}), "error": reason},
        )
        return True

    def pause(
        self,
        session_id: str,
        reason: str = "用户请求暂停",
        owner_account_id: str = "",
    ) -> bool:
        """暂停某 session 最新活跃的 workflow。

        如果 runtime 正在内存中运行，则设置 pause 标志；
        否则直接把 workflow 状态置为 paused，下次 resume 时恢复。
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            log.warning("[DK] pause 缺少 owner_account_id session=%s", session_id)
            return False
        store = self.store.for_owner(owner)
        log.info("[DK] pause owner=%s session=%s reason=%s", owner, session_id, reason)
        workflow = store.get_latest_active_workflow_by_session(
            session_id, active_statuses={"active"}
        )
        if workflow is None:
            return False
        with self._lock:
            runtime = self._engines.get((owner, workflow.id))
            if runtime is not None:
                runtime.request_pause()
        state = store.load_runtime_state(workflow.id)
        if state is None:
            state = RuntimeState(workflow_id=workflow.id)
        state.pause_requested = True
        state.pause_reason = reason
        state.status = "paused"
        store.save_runtime_state(state)
        store.pause_workflow(workflow.id, reason)
        return True

    async def resume_stream(
        self,
        session_id: str,
        request_id: str,
        envelope: Envelope,
    ) -> AsyncIterator[ResponseChunk]:
        """恢复某 session 最新暂停的 workflow 并流式输出后续结果。"""
        owner = str(envelope.user_id or "").strip()
        if not owner:
            log.warning("[DK] resume_stream 缺少 envelope.user_id session=%s", session_id)
            yield ResponseChunk.error(
                request_id,
                f"session {session_id} 请求缺少用户身份",
            )
            return
        store = self.store.for_owner(owner)
        log.info("[DK] resume owner=%s session=%s", owner, session_id)
        workflow = store.get_latest_active_workflow_by_session(
            session_id, active_statuses={"paused"}
        )
        if workflow is None:
            yield ResponseChunk.error(
                request_id,
                f"session {session_id} 没有可恢复的 workflow",
            )
            return

        # 立即给客户端一个可渲染的反馈帧；等待旧 runtime 退出期间不再“静默”
        yield ResponseChunk.status_event(request_id, "▶️ 正在恢复 workflow…", 0)

        # pause 落盘与旧 runtime 实际退出之间存在窗口（旧 runtime 要跑完当前
        # phase 才退出）。直接新建 runtime 会造成两个实例并发跑同一 workflow。
        # 先等旧 runtime 让出 engine 注册位，超时按错误返回。
        engine_key = (owner, workflow.id)
        wait_timeout = getattr(self.config, "dk_resume_wait_timeout_seconds", 30.0) or 30.0
        deadline = time.monotonic() + wait_timeout
        while True:
            with self._lock:
                old_runtime = self._engines.get(engine_key)
            if old_runtime is None:
                break
            if time.monotonic() >= deadline:
                yield ResponseChunk.error(
                    request_id,
                    f"workflow {workflow.id} 上一个运行实例尚未退出，请稍后重试恢复",
                )
                return
            await asyncio.sleep(0.05)

        definition = await self._load_or_build_definition(
            workflow,
            workflow.title,
        )
        workflow.context["workflow_definition"] = definition.to_dict()
        workflow = store.resume_workflow(workflow.id)
        workflow = store.update_workflow_status(
            workflow.id,
            "active",
            context=workflow.context,
        )

        runtime = self._make_runtime(store)
        with self._lock:
            self._engines[engine_key] = runtime
        try:
            async for chunk in runtime.run(
                workflow,
                definition,
                request_id,
                envelope,
            ):
                # 与正常执行路径一致，把 status/final 落进会话历史，
                # 否则 resume 的产出在客户端重载后丢失。
                self._persist_runtime_chunk(envelope, chunk, session_id)
                yield chunk
        finally:
            with self._lock:
                if self._engines.get(engine_key) is runtime:
                    self._engines.pop(engine_key, None)

    def status(self, session_id: str, owner_account_id: str = "") -> dict[str, Any] | None:
        """返回某 session 最新 workflow 的状态快照。"""
        owner = str(owner_account_id or "").strip()
        if not owner:
            log.warning("[DK] status 缺少 owner_account_id session=%s", session_id)
            return None
        store = self.store.for_owner(owner)
        workflow = store.get_latest_active_workflow_by_session(
            session_id, active_statuses={"active", "paused", "done", "failed"}
        )
        if workflow is None:
            return None
        state = store.load_runtime_state(workflow.id)
        return {
            "workflow": workflow.to_dict(),
            "workflow_definition": workflow.context.get("workflow_definition") if workflow.context else None,
            "runtime_state": state.to_dict() if state else None,
            "board": store.get_board_state(workflow.id),
        }

    def clear_session_workspaces(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> list[Path]:
        """删除某 session 关联的所有 workflow 工作目录，并清理 DB 记录。"""
        owner = str(owner_account_id or "").strip()
        if not owner:
            log.warning("[DK] clear_session_workspaces 缺少 owner_account_id session=%s", session_id)
            return []
        from crew.state.home import get_task_workspace_root

        allowed_roots: list[Path] = [Path(get_task_workspace_root(create=False))]
        store = self.store.for_owner(owner)

        # 收集该 session 下所有 workflow 关联的项目工作空间根目录，允许清理其下的产物
        for wf in store.list_workflows_by_session(session_id):
            root_path = (wf.context or {}).get("workspace_root_path")
            if not root_path:
                continue
            try:
                resolved = Path(str(root_path)).expanduser().resolve()
                if resolved.is_dir() and resolved not in allowed_roots:
                    allowed_roots.append(resolved)
            except (OSError, ValueError) as exc:
                log.warning(
                    "clear_session_workspaces 无法解析项目工作空间根目录 "
                    "workflow=%s root_path=%s: %s",
                    wf.id,
                    root_path,
                    exc,
                )

        return store.clear_session(session_id, allowed_roots=allowed_roots, remove_workdirs=True)

    def clear(self) -> None:
        with self._lock:
            self._engines.clear()
