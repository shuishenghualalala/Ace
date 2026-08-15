"""Workflow Runtime：支持动态规划与并行执行的编排运行时。

核心设计：
- WorkflowDefinition 是可持久化的编排脚本（phase + agent_call DAG）。
- RuntimeState 保存执行进度，支持跨请求/跨进程 pause/resume。
- 每个 agent_call 映射到 kanban_tasks 的一张卡片，前端看板实时可见。
- 调度器按 phase 推进，phase 内并行执行，phase 间通过 verification gate 验收。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import time
from typing import Any, AsyncIterator, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.interfaces import Agent, LLMProvider
from crew.core.types import Message
from crew.dynamickanban.models import PlanDelta, Workflow
from crew.dynamickanban.plan_graph import WorkflowGraphValidationError, validate_workflow_dag
from crew.dynamickanban.runtime_models import (
    AgentCall,
    AgentCallResult,
    Phase,
    PhaseResult,
    RuntimeState,
    WorkflowDefinition,
)
from crew.agent.compact import estimate_tokens
from crew.dynamickanban.prompts import build_handoff_context, runtime_worker_system_prompt
from crew.dynamickanban.store import SQLiteKanbanStore
from crew.state.home import task_workspace_path
from crew.state.logging import get_logger, log_role_prefix
from crew.tools.registry import Registry
from crew.tools.redact import safe_public_error

log = get_logger("dynamickanban.runtime")

AgentFactory = Callable[..., Agent]


class WorkflowRuntime:
    """执行一个 WorkflowDefinition 的运行时。"""

    def __init__(
        self,
        store: SQLiteKanbanStore,
        agent_factory: AgentFactory,
        base_registry: Registry,
        provider: LLMProvider,
        *,
        max_concurrent: int = 3,
        max_loops: int = 100,
        task_timeout_seconds: float = 3600.0,
        config: Any | None = None,
        extra_disabled_tools: list[str] | None = None,
        orchestrator: Any | None = None,
        max_replans: int = 2,
    ) -> None:
        self.store = store
        self.agent_factory = agent_factory
        self.base_registry = base_registry
        self.provider = provider
        self.max_concurrent = max_concurrent
        self.max_loops = max_loops
        self.task_timeout_seconds = task_timeout_seconds
        self.config = config
        self.extra_disabled_tools = list(extra_disabled_tools or [])
        # 可选：用于失败自动 replan / steer 重规划时生成修复性 phase
        self.orchestrator = orchestrator
        self.max_replans = max(0, max_replans)
        self._pause_requested = False
        self._stop_requested = False
        # 当前正在执行的 definition / workflow，供 kanban_plan_next 扩图写回
        self._active_definition: WorkflowDefinition | None = None
        self._active_workflow_id: str = ""

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_stop(self) -> None:
        self._stop_requested = True

    def _resolve_workflow_workdir(
        self,
        workflow: Workflow,
        parent_envelope: Envelope,
    ) -> Path:
        """根据项目工作空间决定 workflow 产物目录。

        - 当会话关联了带 root_path 的项目工作空间时，产物落到
          ``{root_path}/workflows/{workflow_id}/``。
        - 否则回退到 ``task_workspace_path(workspace_id)/workflows/{workflow_id}/``。
        """
        # 已有持久化目录时保持路径稳定，支持 pause/resume 以及重启后路径不变
        existing = str((workflow.context or {}).get("workflow_workdir") or "").strip()
        if existing:
            try:
                existing_path = Path(existing).expanduser().resolve()
                return existing_path
            except (OSError, ValueError) as exc:
                log.warning(
                    "workflow 已有 workdir 无效，重新计算: workflow=%s workdir=%s error=%s",
                    workflow.id,
                    existing,
                    exc,
                )

        workspace_id = parent_envelope.workspace_id or "default"
        root_path = str(parent_envelope.params.get("workspace_root_path") or "").strip()

        if root_path:
            try:
                resolved_root = Path(root_path).expanduser().resolve()
                if resolved_root.is_dir():
                    candidate = resolved_root / "workflows" / workflow.id
                    # 安全校验：解析后必须仍位于 root_path 下，防止路径穿越
                    candidate.resolve().relative_to(resolved_root)
                    return candidate
            except (OSError, ValueError) as exc:
                log.warning(
                    "workflow workdir 项目空间路径无效，回退到 task_workspace: "
                    "workspace=%s root_path=%s error=%s",
                    workspace_id,
                    root_path,
                    exc,
                )

        return (
            task_workspace_path(
                workspace_id,
                owner_account_id=parent_envelope.user_id,
            )
            / "workflows"
            / workflow.id
        )

    # ------------------------------------------------------------------ #
    # Public entry
    # ------------------------------------------------------------------ #
    async def run(
        self,
        workflow: Workflow,
        definition: WorkflowDefinition,
        request_id: str,
        parent_envelope: Envelope,
    ) -> AsyncIterator[ResponseChunk]:
        seq = 0

        def _seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        # 确保独立工作目录
        workflow_workdir = self._resolve_workflow_workdir(workflow, parent_envelope)
        workflow_workdir.mkdir(parents=True, exist_ok=True)
        workspace_root_path = str(parent_envelope.params.get("workspace_root_path") or "").strip()
        workflow.context = {
            **workflow.context,
            "workflow_workdir": str(workflow_workdir),
            "workspace_root_path": workspace_root_path,
        }
        self.store.update_workflow_status(
            workflow.id,
            workflow.status,
            context=workflow.context,
        )

        # 加载或初始化 runtime state
        state = self.store.load_runtime_state(workflow.id)
        if state is None:
            state = RuntimeState(workflow_id=workflow.id)
            entries = definition.entry_phase_ids()
            if entries:
                state.current_phase_id = entries[0]
        else:
            state.status = "active"
            state.pause_requested = False
            state.pause_reason = ""

        # workflow_workdir 供 prompt 渲染使用（如 verification_gate 检查 plan.md）
        state.variables["workflow_workdir"] = str(workflow_workdir)
        state.variables["request"] = workflow.title

        self.store.save_runtime_state(state)

        # 把 definition 同步为 kanban 任务图（幂等：已存在的 task 不重复创建）
        self._sync_definition_to_board(workflow.id, definition)
        # 记录活动 definition，供 kanban_plan_next 扩图写回闭环使用
        self._active_definition = definition
        self._active_workflow_id = workflow.id

        yield ResponseChunk.kanban_event(request_id, "started", {"workflow_id": workflow.id}, _seq())
        yield ResponseChunk.status_event(
            request_id,
            f"🚀 Workflow Runtime 已启动：{definition.summary}",
            _seq(),
        )
        yield self._progress_chunk(
            workflow,
            definition,
            state,
            request_id,
            _seq(),
            status="running",
            message="Workflow 已启动",
        )

        try:
            while state.loop_count < self.max_loops:
                # stop/pause 检测放在 loop_count 计数之前：
                # 控制类操作不应消耗执行循环配额。
                if self._stop_requested:
                    yield ResponseChunk.status_event(request_id, "⏹ workflow 被停止", _seq())
                    self._fail_remaining_tasks(workflow.id, "被用户中断")
                    self._mark_workflow_failed(workflow.id, "被用户中断")
                    break

                if self._pause_requested or state.pause_requested:
                    state.status = "paused"
                    state.pause_reason = state.pause_reason or "用户请求暂停"
                    self.store.save_runtime_state(state)
                    self.store.pause_workflow(workflow.id, state.pause_reason)
                    yield ResponseChunk.status_event(
                        request_id,
                        f"⏸ workflow 已暂停：{state.pause_reason}",
                        _seq(),
                    )
                    yield ResponseChunk.kanban_event(
                        request_id, "board_changed", {"workflow_id": workflow.id}, _seq()
                    )
                    break

                state.loop_count += 1
                self.store.save_runtime_state(state)

                # steer：应用用户在运行中注入的补充指令（重规划后续阶段）
                steer_chunks = await self._try_apply_steer(
                    workflow=workflow,
                    definition=definition,
                    state=state,
                    request_id=request_id,
                    seq_fn=_seq,
                )
                if steer_chunks:
                    for steer_chunk in steer_chunks:
                        yield steer_chunk

                phase = self._current_phase(definition, state)
                if phase is None:
                    # 所有 phase 完成，进入综合
                    break

                # 执行当前 phase
                async for chunk in self._run_phase(
                    workflow=workflow,
                    definition=definition,
                    phase=phase,
                    state=state,
                    request_id=request_id,
                    parent_envelope=parent_envelope,
                    seq_fn=_seq,
                ):
                    yield chunk

                # phase 完成后保存状态
                self.store.save_runtime_state(state)

                # 验证门：只要所有 call 都已完成（done 或 failed），就运行验收
                phase_passed = True
                if phase.verification_gate and phase.id in state.phase_results:
                    pr = state.phase_results[phase.id]
                    if pr.status in ("done", "failed"):
                        phase_passed = await self._run_verification_gate(
                            definition=definition,
                            workflow=workflow,
                            phase=phase,
                            state=state,
                            request_id=request_id,
                            parent_envelope=parent_envelope,
                        )
                        self.store.save_runtime_state(state)
                    # 验证通过后，无论是否有单个 call 失败，阶段整体都视为 done，
                    # 让 workflow 能继续推进；失败的 call 信息保留在 call_results 中。
                    if phase_passed:
                        pr.status = "done"
                        self.store.save_runtime_state(state)

                if not phase_passed:
                    gate = phase.verification_gate
                    assert gate is not None
                    valid_phase_ids = {p.id for p in definition.phases}
                    retries = state.phase_retry_counts.get(phase.id, 0)
                    max_retries = max(0, gate.max_retries or 2)
                    if retries >= max_retries:
                        # 失败自动 replan：生成修复 phase 接入 DAG 并重跑，而不是直接失败
                        replan_chunks = await self._try_replan(
                            workflow=workflow,
                            definition=definition,
                            phase=phase,
                            state=state,
                            request_id=request_id,
                            seq_fn=_seq,
                        )
                        if replan_chunks is not None:
                            for replan_chunk in replan_chunks:
                                yield replan_chunk
                            continue
                        pr = state.phase_results[phase.id]
                        pr.status = "failed"
                        reason = f"阶段 {phase.id} 验证失败超过最大重试次数 {max_retries}"
                        pr.error = reason
                        self.store.save_runtime_state(state)
                        yield self._progress_chunk(
                            workflow,
                            definition,
                            state,
                            request_id,
                            _seq(),
                            status="failed",
                            message=reason,
                        )
                        yield ResponseChunk.status_event(
                            request_id,
                            f"❌ {reason}，workflow 失败",
                            _seq(),
                        )
                        break
                    if gate.fallback_phase_id and gate.fallback_phase_id in valid_phase_ids:
                        # 回退到上一阶段，并标记为待重新执行
                        state.phase_retry_counts[phase.id] = retries + 1
                        fallback_pr = state.phase_results.get(gate.fallback_phase_id)
                        if fallback_pr and fallback_pr.status == "done":
                            fallback_pr.status = "pending"
                            fallback_pr.call_results.clear()
                        if gate.fallback_phase_id in state.completed_phase_ids:
                            state.completed_phase_ids.remove(gate.fallback_phase_id)
                        current_pr = state.phase_results.get(phase.id)
                        if current_pr is not None:
                            current_pr.status = "pending"
                            current_pr.call_results.clear()
                        state.current_phase_id = gate.fallback_phase_id
                        self.store.save_runtime_state(state)
                        yield self._progress_chunk(
                            workflow,
                            definition,
                            state,
                            request_id,
                            _seq(),
                            status="running",
                            message=f"验证未通过，回退到阶段 {gate.fallback_phase_id}（第 {retries + 1}/{max_retries} 次重试）",
                        )
                    else:
                        pr = state.phase_results[phase.id]
                        pr.status = "blocked"
                        self.store.save_runtime_state(state)
                        yield self._progress_chunk(
                            workflow,
                            definition,
                            state,
                            request_id,
                            _seq(),
                            status="failed",
                            message="验证未通过且未配置回退阶段，阶段阻塞",
                        )
                        # 不再推进，直接结束 workflow
                        break
                    # 回退后继续下一轮循环，重新执行 fallback phase
                    continue

                # 推进到下一阶段
                phase_advanced = self._advance_phase(definition, state)
                self.store.save_runtime_state(state)
                if phase_advanced:
                    yield self._progress_chunk(
                        workflow,
                        definition,
                        state,
                        request_id,
                        _seq(),
                        status="running",
                        message="阶段推进",
                    )
                else:
                    # edges 没有更多 ready phase：终点完成，或失败前驱使剩余 join 不可达。
                    # 若 phase 执行期间收到暂停，先让循环顶部持久化 paused，而不是误收敛 done。
                    if self._pause_requested or state.pause_requested:
                        continue
                    break

            # 最终综合
            has_failed = any(
                pr.status in ("failed", "blocked")
                for pr in state.phase_results.values()
            )
            status = "done"
            if has_failed:
                status = "failed"
            elif state.status == "paused":
                status = "paused"

            if status == "failed":
                failed_phases = [
                    f"{phase.name}({phase.id})"
                    for phase in definition.phases
                    if state.phase_results.get(
                        phase.id, PhaseResult(phase_id=phase.id, status="pending")
                    ).status in ("failed", "blocked")
                ]
                final_text = (
                    f"Dynamic Kanban workflow 未能完成。失败/阻塞阶段：{', '.join(failed_phases)}。"
                    "请检查右侧看板中的阶段详情，或重试。"
                )
            elif status == "paused":
                final_text = (
                    f"workflow 已暂停，当前阶段：{state.current_phase_id}。请在右侧看板点击 ▶️ 继续按钮恢复执行。"
                )
            else:
                final_text = await self._synthesize(workflow, definition, state)

            if status != "paused":
                self.store.update_workflow_status(workflow.id, status)
                state.status = status
                self.store.save_runtime_state(state)

            yield ResponseChunk.kanban_event(
                request_id, "board_changed", {"workflow_id": workflow.id}, _seq()
            )
            final_message = ""
            if status == "done":
                final_message = "Workflow 已完成"
            elif status == "failed":
                final_message = "Workflow 失败"
            elif status == "paused":
                final_message = "Workflow 已暂停"
            yield self._progress_chunk(
                workflow,
                definition,
                state,
                request_id,
                _seq(),
                status=status,
                message=final_message,
            )
            yield ResponseChunk.final(request_id, final_text, _seq())

        except asyncio.CancelledError:
            log.warning("[DK Runtime] workflow %s 被取消", workflow.id)
            if self._stop_requested:
                # 用户显式停止（dispatcher.stop 先 interrupt 置标志再 task.cancel()）：
                # 与协作式 stop 路径一致，失败收尾，不残留 running/pending 任务
                self._fail_remaining_tasks(workflow.id, "被用户中断")
                self._mark_workflow_failed(workflow.id, "被用户中断")
            else:
                # 非显式取消（如 resume SSE 客户端断连）：落 paused 保证可再次
                # resume，而不是 failed 终态；同样清理残留的运行中任务
                self._fail_remaining_tasks(workflow.id, "连接中断")
                state.status = "paused"
                state.pause_reason = state.pause_reason or "连接中断，workflow 已暂停，可恢复"
                self.store.save_runtime_state(state)
                try:
                    self.store.pause_workflow(workflow.id, state.pause_reason)
                except ValueError:
                    pass  # 已是终态，无需再落 paused
            yield ResponseChunk.kanban_event(
                request_id, "board_changed", {"workflow_id": workflow.id}, _seq()
            )
            raise

    # ------------------------------------------------------------------ #
    # Phase execution
    # ------------------------------------------------------------------ #
    async def _run_phase(
        self,
        *,
        workflow: Workflow,
        definition: WorkflowDefinition,
        phase: Phase,
        state: RuntimeState,
        request_id: str,
        parent_envelope: Envelope,
        seq_fn: Callable[[], int],
    ) -> AsyncIterator[ResponseChunk]:
        pr = state.phase_results.get(phase.id)
        if pr is None:
            pr = PhaseResult(phase_id=phase.id, status="running")
            state.phase_results[phase.id] = pr

        if pr.status == "done":
            return

        pr.status = "running"
        self.store.save_runtime_state(state)
        yield self._progress_chunk(
            workflow,
            definition,
            state,
            request_id,
            seq_fn(),
            status="running",
            active_calls=phase.agent_calls,
            message=f"进入阶段 {phase.name}",
        )

        # 构造变量池：合并上游 phase 的输出
        variables = dict(state.variables)

        limit = min(phase.max_concurrent or definition.max_concurrent or self.max_concurrent, self.max_concurrent)
        semaphore = asyncio.Semaphore(max(1, limit))

        async def _run_one(call: AgentCall) -> tuple[AgentCall, AgentCallResult | None]:
            async with semaphore:
                if self._pause_requested:
                    # 暂停期间不再启动新 call：返回 None 表示未执行，
                    # 不写入 call_results，resume 时会作为 pending 重跑。
                    return call, None
                result = await self._execute_call(
                    workflow=workflow,
                    call=call,
                    phase_id=phase.id,
                    variables=variables,
                    request_id=request_id,
                    parent_envelope=parent_envelope,
                )
                return call, result

        # 失败 call 自动重试一次，避免单次超时/抖动导致整个 workflow 失败
        max_call_retries = 1
        for attempt in range(max_call_retries + 1):
            if attempt > 0 and (self._stop_requested or self._pause_requested):
                # 收到 stop/pause 后不再重跑剩余 call，尽快回到主循环顶部的处理
                break
            pending_calls = [
                c for c in phase.agent_calls
                if c.id not in pr.call_results or pr.call_results[c.id].status == "failed"
            ]
            if not pending_calls:
                break

            coros = [_run_one(c) for c in pending_calls]
            for call, result in await asyncio.gather(*coros, return_exceptions=True):
                if result is None:
                    # 暂停未执行的 call：保持无结果状态，留待 resume
                    continue
                if isinstance(result, Exception):
                    result = AgentCallResult(
                        call_id=call.id,
                        status="failed",
                        error=str(result),
                    )
                pr.call_results[call.id] = result
                # 把 call 输出合并到变量池
                if result.status == "done":
                    for key, value in result.outputs.items():
                        state.variables[f"{phase.id}.{call.id}.{key}"] = value
                    # 同时把 text 作为默认变量
                    if result.text:
                        state.variables[f"{phase.id}.{call.id}.text"] = result.text
                    if result.artifacts:
                        state.variables[f"{phase.id}.{call.id}.artifacts"] = result.artifacts

                # 更新看板任务状态
                self._sync_call_result_to_board(workflow.id, call, result)

                self.store.save_runtime_state(state)
                log.info(
                    "[DK Runtime] call %s/%s 完成 status=%s text_len=%d artifacts=%d",
                    phase.id,
                    call.id,
                    result.status,
                    len(result.text or ""),
                    len(result.artifacts or []),
                )

                # 进度面板：更新剩余未完成的调用，让前端实时看到谁还在执行。
                remaining_calls = [
                    c for c in phase.agent_calls
                    if c.id not in pr.call_results or pr.call_results[c.id].status != "done"
                ]
                yield self._progress_chunk(
                    workflow,
                    definition,
                    state,
                    request_id,
                    seq_fn(),
                    status="running",
                    active_calls=remaining_calls,
                )

                # 把角色最终输出摘要推到原始会话，前台流可直接看到，后台执行时也能在历史中回看。
                output_summary = ""
                if result.status == "done":
                    if result.text:
                        output_summary = result.text[:4000]
                    elif result.artifacts:
                        output_summary = "产出文件：\n" + "\n".join(f"- {a}" for a in result.artifacts[:20])
                    else:
                        output_summary = "已完成（无文本输出）"
                elif result.status == "failed":
                    output_summary = result.error or "执行失败"
                role_label = call.role or call.id
                status_chunk = ResponseChunk.status_event(
                    request_id,
                    f"**{phase.name} / {role_label}**\n\n{output_summary}",
                    seq_fn(),
                )
                # desktop 会把带 agent_name 的 status 消息渲染成独立角色卡片（而不是折叠到过程区）
                status_chunk.body["agent_name"] = role_label
                status_chunk.body["agent_avatar"] = self._actor_avatar(call.role)
                yield status_chunk

                # 触发右侧看板刷新；详细结果仍保存在 runtime_state 中供 /status API 查询。
                yield ResponseChunk.kanban_event(
                    request_id,
                    "call_completed",
                    {
                        "workflow_id": workflow.id,
                        "phase_id": phase.id,
                        "call_id": call.id,
                        "role": call.role,
                        "status": result.status,
                    },
                    seq_fn(),
                )

        # 判定 phase 整体状态；存在未执行的 call（暂停中断跳过的）时保持 running，
        # 等待 resume 续跑，不能误判为 done/failed。
        unexecuted = [c.id for c in phase.agent_calls if c.id not in pr.call_results]
        if unexecuted:
            pr.status = "running"
        elif all(r.status == "done" for r in pr.call_results.values()):
            pr.status = "done"
        elif any(r.status == "failed" for r in pr.call_results.values()):
            pr.status = "failed"
        elif any(r.status == "blocked" for r in pr.call_results.values()):
            pr.status = "blocked"
        else:
            pr.status = "failed"

    def _max_iterations_for_phase(self, phase_id: str) -> int:
        """按 phase 类型动态设置 worker 迭代预算，减少空转。"""
        pid = phase_id.lower()
        if "clarify" in pid:
            return 6
        if "verify" in pid or "review" in pid:
            return 6
        if "plan" in pid or "design" in pid:
            return 6
        return 100

    def _toolsets_for_call(self, phase_id: str, role: str | None) -> list[str] | None:
        """按 phase/role 限制可用 toolset，减少无关节点的 schema token。"""
        pid = phase_id.lower()
        role_l = (role or "").lower()
        if "clarify" in pid:
            # clarify 阶段只允许看板操作 + 用户交互式追问，禁止写文件/执行命令/搜索
            return ["dynamic_kanban", "interaction"]
        if "plan" in pid or role_l in {"planner", "planning", "architect"}:
            return ["dynamic_kanban", "file", "skills"]
        if role_l in {"tester", "qa"}:
            return ["dynamic_kanban", "file", "terminal", "skills"]
        # coder / implementer 等不额外限制，由 access_control 控制
        return None

    def _workflow_roles(self) -> list[str]:
        """返回当前 definition 中按首次出现顺序排列的角色。"""
        definition = self._active_definition
        if definition is None:
            return []
        return list(dict.fromkeys(
            call.role
            for phase in definition.phases
            for call in phase.agent_calls
            if call.role
        ))

    # ------------------------------------------------------------------ #
    # Worker registry + 扩图闭环
    # ------------------------------------------------------------------ #
    def _build_worker_registry(self, workflow_id: str, call: AgentCall) -> Registry:
        """为单个 worker call 构建 registry：base 工具 + 该 workflow 的看板工具。"""
        from crew.dynamickanban.tools import create_kanban_registry

        registry = Registry()
        for name in self.base_registry.names():
            try:
                registry.register(tool=self.base_registry.get(name))
            except Exception:  # noqa: BLE001 - 忽略重名等异常，保证 worker 可用
                continue
        kanban_tools = create_kanban_registry(
            self.store,
            workflow_id,
            actor=call.role or "worker",
            # 角色由 workflow definition 动态产生，扩图时允许新增角色。
            valid_roles=[],
            on_plan_extension=self._make_plan_extension_handler(workflow_id),
        )
        for tool in kanban_tools.values():
            registry.register(tool=tool, override=True)
        return registry

    def _make_plan_extension_handler(
        self,
        workflow_id: str,
    ) -> Callable[[PlanDelta, list[Any]], None]:
        def _handler(delta: PlanDelta, added_tasks: list[Any]) -> None:
            self._apply_plan_extension_to_definition(workflow_id, delta, added_tasks)

        return _handler

    def _phase_of_task(self, task_id: str, task_phases: dict[str, str]) -> str | None:
        """解析看板任务所属的 definition phase。

        - 扩展任务：查 context 中持久化的 runtime_task_phases 映射；
        - Runtime 同步的任务：标题形如 ``[phase_id:call_id] ...``，直接解析。
        """
        if task_id in task_phases:
            return task_phases[task_id]
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            return None
        m = re.match(r"^\[([^:\]]+):[^\]]+\]", task.title or "")
        if m:
            return m.group(1)
        return None

    def _apply_plan_extension_to_definition(
        self,
        workflow_id: str,
        delta: PlanDelta,
        added_tasks: list[Any],
    ) -> None:
        """把 kanban_plan_next 的看板扩展写回 WorkflowDefinition，形成扩图闭环。

        每个新增任务映射为一个单 call 的 Phase；父任务依赖映射为 phase 间 edge。
        写回后：当前运行中的 Runtime 会调度新 phase，pause/resume 也不丢失。
        DAG 校验失败时抛异常（看板侧扩展已生效，由工具层降级提示）。
        """
        definition = self._active_definition
        if definition is None or self._active_workflow_id != workflow_id:
            raise RuntimeError("当前没有活动的 workflow definition，无法写回扩图")

        wf = self.store.get_workflow(workflow_id)
        ctx = dict(wf.context or {}) if wf is not None else {}
        task_phases: dict[str, str] = dict(ctx.get("runtime_task_phases") or {})
        call_titles: dict[str, str] = dict(ctx.get("runtime_call_titles") or {})
        new_phases: list[Phase] = []
        new_edges: list[tuple[str, str]] = []
        for spec, task in zip(delta.add_tasks, added_tasks):
            phase_id = f"plan_{task.id}"
            call_id = f"{phase_id}_call"
            role = str(task.assignee or "").strip() or "worker"
            prompt = str(task.detail or task.title or "").strip() or str(task.title)
            new_phases.append(
                Phase(
                    id=phase_id,
                    name=str(task.title or phase_id)[:60],
                    description=str(task.detail or "")[:300],
                    agent_calls=[
                        AgentCall(
                            id=call_id,
                            role=role,
                            prompt=prompt,
                            outputs=["text", "artifacts"],
                        )
                    ],
                )
            )
            task_phases[task.id] = phase_id
            call_titles[call_id] = str(task.title)
            for parent_id in spec.get("parent_task_ids") or []:
                parent_phase = self._phase_of_task(str(parent_id), task_phases)
                if parent_phase and parent_phase != phase_id:
                    new_edges.append((parent_phase, phase_id))

        for parent_id, child_id in delta.add_dependencies:
            parent_phase = self._phase_of_task(str(parent_id), task_phases)
            child_phase = self._phase_of_task(str(child_id), task_phases)
            if parent_phase and child_phase and parent_phase != child_phase:
                new_edges.append((parent_phase, child_phase))

        all_phase_ids = [p.id for p in definition.phases] + [p.id for p in new_phases]
        merged_edges = list(dict.fromkeys([*definition.edges, *new_edges]))
        try:
            merged_edges = validate_workflow_dag(all_phase_ids, merged_edges)
        except WorkflowGraphValidationError as exc:
            raise RuntimeError(
                f"扩图写回 definition 时 DAG 校验失败: "
                f"{safe_public_error(exc, 'DAG 校验失败')}"
            ) from exc

        definition.phases.extend(new_phases)
        definition.edges = merged_edges

        ctx["workflow_definition"] = definition.to_dict()
        ctx["runtime_task_phases"] = task_phases
        ctx["runtime_call_titles"] = call_titles
        self.store.update_workflow_status(
            workflow_id,
            wf.status if wf is not None else "active",
            context=ctx,
        )
        log.info(
            "[DK Runtime] plan extension 已写回 definition：workflow=%s 新增 phase=%d edge=%d",
            workflow_id,
            len(new_phases),
            len(new_edges),
        )

    # ------------------------------------------------------------------ #
    # 失败自动 replan / steer 重规划
    # ------------------------------------------------------------------ #
    def _persist_definition(self, workflow_id: str, definition: WorkflowDefinition) -> None:
        """把内存中的 definition 持久化到 workflow.context（pause/resume 后仍生效）。"""
        wf = self.store.get_workflow(workflow_id)
        ctx = dict(wf.context or {}) if wf is not None else {}
        ctx["workflow_definition"] = definition.to_dict()
        self.store.update_workflow_status(
            workflow_id,
            wf.status if wf is not None else "active",
            context=ctx,
        )

    def _append_phases_to_definition(
        self,
        definition: WorkflowDefinition,
        new_phases: list[Phase],
        new_edges: list[tuple[str, str]],
    ) -> None:
        """向 definition 追加 phase 与 edge，写前做 DAG 校验（失败抛异常，definition 不变）。

        调用方负责保证 new_phases 的 id 不与现有 phase 冲突。
        """
        all_phase_ids = [p.id for p in definition.phases] + [p.id for p in new_phases]
        merged_edges = validate_workflow_dag(
            all_phase_ids,
            list(dict.fromkeys([*definition.edges, *new_edges])),
        )
        definition.phases.extend(new_phases)
        definition.edges = merged_edges

    async def _try_replan(
        self,
        *,
        workflow: Workflow,
        definition: WorkflowDefinition,
        phase: Phase | None,
        state: RuntimeState,
        request_id: str,
        seq_fn: Callable[[], int],
        steer_instruction: str = "",
    ) -> list[ResponseChunk] | None:
        """失败自动 replan / steer 重规划：生成修复 phase 并接入 DAG。

        - 失败 replan：传入失败的 phase，修复链接到它之前并重置其状态重跑；
        - steer 重规划：传入下一个待执行 phase（或 None 表示附加到末尾），
          修复链落实用户新指令后继续推进。

        返回 None 表示未执行 replan（未配置 orchestrator、超限或 LLM 失败），
        调用方按原有路径处理；返回 chunk 列表表示 replan 成功，调用方应继续主循环。
        """
        if self.orchestrator is None:
            return None
        if state.replan_count >= self.max_replans:
            log.info(
                "[DK Runtime] workflow=%s replan 次数已达上限 %d，不再重规划",
                workflow.id,
                self.max_replans,
            )
            return None

        pr = state.phase_results.get(phase.id) if phase is not None else None
        fail_error = (pr.error if pr else "") or ""
        replan_context: dict[str, Any] = {
            "steer_instruction": steer_instruction,
        }
        if phase is not None:
            replan_context.update(
                {
                    "failed_phase_id": phase.id,
                    "failed_phase_name": phase.name,
                    "error": fail_error,
                    "verification_result": pr.verification_result if pr else {},
                    "failed_calls": [
                        {"call_id": c.id, "role": c.role, "error": r.error}
                        for c in phase.agent_calls
                        for r in [pr.call_results.get(c.id) if pr else None]
                        if r is not None and r.status == "failed"
                    ],
                }
            )

        try:
            repair_phases = await self.orchestrator.build_repair_phases(
                workflow.title,
                replan_context,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[DK Runtime] replan 生成修复阶段失败: %s", exc)
            return None
        if not repair_phases:
            return None

        # 修复 phase id 去重，避免与已有 phase 冲突导致 edges 失效
        existing_ids = {p.id for p in definition.phases}
        for repair_phase in repair_phases:
            base = repair_phase.id
            suffix = 2
            while repair_phase.id in existing_ids:
                repair_phase.id = f"{base}_{suffix}"
                suffix += 1
            existing_ids.add(repair_phase.id)

        # 修复链串行相接；失败 replan 时末尾指回失败 phase，
        # steer 且目标 phase 存在时指回目标 phase，否则附加在已完成阶段之后
        new_edges: list[tuple[str, str]] = []
        for prev, cur in zip(repair_phases, repair_phases[1:]):
            new_edges.append((prev.id, cur.id))
        if phase is not None:
            new_edges.append((repair_phases[-1].id, phase.id))
        elif state.completed_phase_ids:
            new_edges.append((state.completed_phase_ids[-1], repair_phases[0].id))
        try:
            self._append_phases_to_definition(definition, repair_phases, new_edges)
        except WorkflowGraphValidationError as exc:
            log.warning("[DK Runtime] replan 接入 DAG 校验失败: %s", exc)
            return None

        # 失败 replan：重置失败 phase，让它在修复 phase 完成后重跑（重试次数清零）
        if phase is not None:
            if pr is not None:
                pr.status = "pending"
                pr.call_results.clear()
                pr.verification_result = {}
                pr.error = ""
            state.phase_retry_counts.pop(phase.id, None)
        state.replan_count += 1
        state.current_phase_id = repair_phases[0].id
        self._persist_definition(workflow.id, definition)
        self._sync_definition_to_board(workflow.id, definition)
        self.store.save_runtime_state(state)

        target_label = phase.name if phase is not None else "workflow 末尾"
        reason = steer_instruction or fail_error or "验证未通过"
        log.info(
            "[DK Runtime] workflow=%s 第 %d 次 replan：新增修复 phase=%s，目标=%s",
            workflow.id,
            state.replan_count,
            [p.id for p in repair_phases],
            target_label,
        )
        action = "已按新指令重规划" if steer_instruction and not fail_error else "未通过，已自动重规划"
        return [
            ResponseChunk.status_event(
                request_id,
                f"🔧 阶段 {target_label} {action}：新增 {len(repair_phases)} 个调整阶段"
                f"（第 {state.replan_count}/{self.max_replans} 次 replan）",
                seq_fn(),
            ),
            self._progress_chunk(
                workflow,
                definition,
                state,
                request_id,
                seq_fn(),
                status="running",
                message=f"自动重规划（原因：{reason[:80]}）",
            ),
            ResponseChunk.kanban_event(
                request_id, "board_changed", {"workflow_id": workflow.id}, seq_fn()
            ),
        ]

    async def _try_apply_steer(
        self,
        *,
        workflow: Workflow,
        definition: WorkflowDefinition,
        state: RuntimeState,
        request_id: str,
        seq_fn: Callable[[], int],
    ) -> list[ResponseChunk] | None:
        """检查并应用用户 steer 指令（manager.steer 写入 workflow.context["steer_notes"]）。

        在主循环每轮开始时调用；指令落实为插入到下一待执行 phase 之前的调整阶段。
        """
        wf = self.store.get_workflow(workflow.id)
        ctx = dict(wf.context or {}) if wf is not None else {}
        notes = [n for n in (ctx.get("steer_notes") or []) if isinstance(n, dict)]
        applied_count = int(ctx.get("steer_applied") or 0)
        if applied_count >= len(notes):
            return None
        instruction = "\n".join(
            str(n.get("text") or "").strip() for n in notes[applied_count:]
        ).strip()
        # 无论是否能重规划都标记已应用，避免每轮重复消费
        ctx["steer_applied"] = len(notes)
        if wf is not None:
            self.store.update_workflow_status(workflow.id, wf.status, context=ctx)
        if not instruction:
            return None

        # 目标：下一个待执行 phase；没有则在 workflow 末尾追加调整阶段
        target: Phase | None = None
        if state.current_phase_id:
            target = next(
                (p for p in definition.phases if p.id == state.current_phase_id),
                None,
            )
        if target is None:
            target = self._current_phase(definition, state)

        log.info(
            "[DK Runtime] workflow=%s 应用 steer 指令：%s（目标 phase=%s）",
            workflow.id,
            instruction[:200],
            target.id if target else "末尾",
        )
        return await self._try_replan(
            workflow=workflow,
            definition=definition,
            phase=target,
            state=state,
            request_id=request_id,
            seq_fn=seq_fn,
            steer_instruction=instruction,
        )

    def _build_upstream_summary(
        self,
        call: AgentCall,
        variables: dict[str, Any],
    ) -> str:
        """只把 call.inputs 显式引用的上游输出做成精简 handoff。"""
        if not call.inputs:
            return ""
        results: list[dict[str, Any]] = []
        for ref in call.inputs.values():
            full_key = f"{ref.source_phase_id}.{ref.source_call_id}.{ref.output_key}"
            text = variables.get(full_key, "")
            artifacts = variables.get(
                f"{ref.source_phase_id}.{ref.source_call_id}.artifacts", []
            ) or []
            results.append(
                {
                    "assignee": ref.source_call_id,
                    "title": f"{ref.source_phase_id}.{ref.source_call_id}",
                    "result_summary": str(text),
                    "artifact_paths": artifacts,
                }
            )
        return build_handoff_context(results, max_summary_chars=300, max_artifacts=3)

    async def _execute_call(
        self,
        *,
        workflow: Workflow,
        call: AgentCall,
        phase_id: str,
        variables: dict[str, Any],
        request_id: str,
        parent_envelope: Envelope,
    ) -> AgentCallResult:
        """执行单个 agent_call，返回结果（不抛异常）。"""
        log.info("[DK Runtime] 执行 call %s role=%s", call.id, call.role)
        call_start = time.time()

        # 构造 prompt：替换变量引用
        prompt = self._render_prompt(call.prompt, variables, call.inputs)

        # 构造系统提示：稳定约定 + 动态任务上下文
        valid_roles = self._workflow_roles()
        workflow_workdir = workflow.context.get("workflow_workdir")
        handoff = self._build_upstream_summary(call, variables)
        system_prompt = runtime_worker_system_prompt(
            role=call.role or "worker",
            task_prompt=prompt,
            handoff=handoff,
            valid_roles=valid_roles,
            workflow_workdir=str(workflow_workdir) if workflow_workdir else None,
            task_id=call.id,
            is_planning_role=(call.role or "").lower() in {"planner", "planning", "architect"},
        )
        log.info(
            "[DK Runtime] call %s system_prompt ~%d tokens (%d chars)",
            call.id,
            estimate_tokens([Message.system(system_prompt)]),
            len(system_prompt),
        )

        # clarify 阶段只许提问或文本回答，禁止写文件、执行命令、调用技能
        extra_disabled_tools: list[str] = list(self.extra_disabled_tools)
        extra_disabled_toolsets: list[str] | None = None
        if "clarify" in phase_id.lower():
            extra_disabled_toolsets = ["terminal"]
            extra_disabled_tools.extend([
                "file_write",
                "file_append",
                "skills_list",
                "skill_view",
                "skill_activate",
            ])

        extra_enabled_toolsets = self._toolsets_for_call(phase_id, call.role)

        # 每个 call 使用独立 registry：base 工具 + 该 workflow 绑定的看板工具
        # （看板工具携带 workflow_id 与扩图回调，不能注册进全局共享 registry）
        worker_registry = self._build_worker_registry(workflow.id, call)

        agent = self.agent_factory(
            registry=worker_registry,
            system_prompt=system_prompt,
            agent_id=call.role or "dk_worker",
            lightweight=True,
            user_type=parent_envelope.user_type,
            assignee=call.role,
            extra_enabled_toolsets=extra_enabled_toolsets,
            extra_disabled_toolsets=extra_disabled_toolsets,
            extra_disabled_tools=extra_disabled_tools,
            max_iterations=self._max_iterations_for_phase(phase_id),
            owner_account_id=parent_envelope.user_id,
        )

        task_session_id = f"{parent_envelope.session_id}::dk::{call.id}"
        task_params: dict[str, Any] = {
            "query": prompt,
            "task_id": call.id,
            "workflow_id": workflow.id,
            "phase_id": call.id.split("::")[0] if "::" in call.id else "",
            # 追问/交互事件必须推送到原始会话，前端只订阅原始 session
            "task_session_id": parent_envelope.session_id,
        }
        if workflow_workdir:
            task_params["cwd"] = str(workflow_workdir)

        env = Envelope(
            session_id=task_session_id,
            params=task_params,
            request_id=request_id,
            channel=parent_envelope.channel,
            user_id=parent_envelope.user_id,
            user_type=parent_envelope.user_type,
            workspace_id=parent_envelope.workspace_id,
            mode="agent",
        )

        final_text = ""
        has_error = False
        role_prefix = f"{call.role}:{call.id}" if call.role else call.id

        # per-call 超时保护：优先取 call 自身配置，否则用 runtime 默认值
        call_timeout = call.timeout_seconds or self.task_timeout_seconds

        async def _collect_output() -> None:
            nonlocal final_text, has_error
            with log_role_prefix(role_prefix):
                async for chunk in agent.run(env):
                    if chunk.kind == "final":
                        final_text = chunk.body.get("text", "")
                    elif chunk.kind == "error":
                        has_error = True

        try:
            if call_timeout and call_timeout > 0:
                await asyncio.wait_for(_collect_output(), timeout=call_timeout)
            else:
                await _collect_output()
        except asyncio.TimeoutError:
            log.warning(
                "call %s 执行超时（%.0fs），强制结束",
                call.id,
                call_timeout,
            )
            return AgentCallResult(
                call_id=call.id,
                status="failed",
                error=f"任务执行超时（超过 {call_timeout:.0f} 秒）",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("call %s agent 执行异常：%s", call.id, type(exc).__name__)
            return AgentCallResult(
                call_id=call.id,
                status="failed",
                error="任务执行失败：内部错误",
            )
        finally:
            close_fn = getattr(agent, "aclose", None)
            if callable(close_fn):
                try:
                    await close_fn()
                except Exception:  # noqa: BLE001 - 清理失败不覆盖任务业务终态
                    log.exception("call %s 关闭 Agent-owned Provider 失败", call.id)

        if has_error and not final_text:
            return AgentCallResult(
                call_id=call.id,
                status="failed",
                error="执行中出现错误且没有最终输出",
            )

        # 执行过程中收到停止请求，按中断失败处理
        if self._stop_requested:
            return AgentCallResult(
                call_id=call.id,
                status="failed",
                error="被用户中断",
            )

        # 尝试从 final_text 解析结构化 outputs
        outputs: dict[str, Any] = {}
        try:
            m = None
            for pat in [r"```(?:json)?\s*([\s\S]*?)```", r"\{[\s\S]*\}"]:
                import re
                m = re.search(pat, final_text)
                if m:
                    break
            if m:
                data = json.loads(m.group(1).strip() if m.group(1) else m.group(0).strip())
                if isinstance(data, dict):
                    outputs = data
        except Exception:  # noqa: BLE001
            pass

        # 默认把 final_text 作为 text 输出
        outputs.setdefault("text", final_text)

        log.info(
            "[DK Runtime] call %s 完成，耗时 %.2fs，状态 done",
            call.id,
            time.time() - call_start,
        )
        return AgentCallResult(
            call_id=call.id,
            status="done",
            text=final_text,
            outputs=outputs,
            artifacts=outputs.get("artifacts") or outputs.get("artifact_paths") or [],
        )

    # ------------------------------------------------------------------ #
    # Verification gate
    # ------------------------------------------------------------------ #
    async def _run_verification_gate(
        self,
        *,
        definition: WorkflowDefinition,
        workflow: Workflow,
        phase: Phase,
        state: RuntimeState,
        request_id: str,
        parent_envelope: Envelope,
    ) -> bool:
        """执行阶段验证门，返回是否通过。

        先做轻量规则预检：所有 call 都成功且有实际输出时直接通过，
        避免每个 phase 都调一次 LLM。只有出现失败/空完成或配置强制启用时才走 LLM gate。
        """
        gate = phase.verification_gate
        if gate is None:
            return True

        pr = state.phase_results[phase.id]

        # 轻量规则预检
        all_done = bool(pr.call_results) and all(
            r.status == "done" for r in pr.call_results.values()
        )
        has_substance = all_done and all(
            (r.text and r.text.strip()) or (r.artifacts)
            for r in pr.call_results.values()
        )
        if has_substance:
            pr.verification_result = {
                "passed": True,
                "text": "规则预检通过：所有子任务均成功产出",
            }
            log.info("[DK Runtime] 阶段 %s 规则预检通过，跳过 LLM gate", phase.id)
            return True

        # 配置关闭 LLM gate 时，失败/空完成直接按不通过处理
        gate_enabled = True
        if self.config is not None:
            gate_enabled = getattr(self.config, "dk_verification_gate_enabled", True)
        if not gate_enabled:
            pr.verification_result = {
                "passed": False,
                "text": "verification gate 已关闭，阶段存在失败或空完成任务",
            }
            return False

        gate_start = time.time()
        variables = dict(state.variables)
        variables["phase_results"] = pr.to_dict()

        prompt = self._render_prompt(gate.prompt, variables)
        system_prompt = (
            f"你是验证者「{gate.role}」。\n"
            "你的职责是验收上一阶段产出。请严格只输出一行 JSON，不要任何解释：\n"
            f'{{"{gate.pass_key}": true, "reason": "..."}} 或 '
            f'{{"{gate.pass_key}": false, "reason": "...", "suggestions": "..."}}'
        )

        agent = self.agent_factory(
            registry=self.base_registry,
            system_prompt=system_prompt,
            agent_id=gate.role or "verifier",
            lightweight=True,
            user_type=parent_envelope.user_type,
            assignee=gate.role,
            extra_enabled_toolsets=["dynamic_kanban"],
            max_iterations=3,
            owner_account_id=parent_envelope.user_id,
        )

        env = Envelope(
            session_id=f"{parent_envelope.session_id}::dk::verify::{phase.id}",
            params={
                "query": prompt,
                "workflow_id": workflow.id,
                # 验证门若需追问，也推送到原始会话
                "task_session_id": parent_envelope.session_id,
            },
            request_id=request_id,
            channel=parent_envelope.channel,
            user_id=parent_envelope.user_id,
            user_type=parent_envelope.user_type,
            workspace_id=parent_envelope.workspace_id,
            mode="agent",
        )

        final_text = ""
        try:
            async for chunk in agent.run(env):
                if chunk.kind == "final":
                    final_text = chunk.body.get("text", "")
        except Exception as exc:  # noqa: BLE001
            log.error("verification gate %s 执行异常：%s", phase.id, type(exc).__name__)
            final_text = "验证执行失败：内部错误"
        finally:
            close_fn = getattr(agent, "aclose", None)
            if callable(close_fn):
                try:
                    await close_fn()
                except Exception:  # noqa: BLE001 - 清理失败不覆盖验证业务结果
                    log.exception("verification gate %s 关闭 Agent-owned Provider 失败", phase.id)

        passed = self._parse_gate_passed(final_text, gate.pass_key or "passed")
        pr.verification_result = {"passed": passed, "text": final_text}
        log.info(
            "[DK Runtime] 阶段 %s 验证结果 passed=%s 耗时 %.2fs text=%s",
            phase.id,
            passed,
            time.time() - gate_start,
            final_text[:500],
        )
        return passed

    def _parse_gate_passed(self, text: str, pass_key: str) -> bool:
        """从验证门输出中解析是否通过，支持 JSON 和常见自然语言表达。"""
        import re

        if not text:
            return False
        # 优先严格匹配 JSON: "passed": true
        m = re.search(rf'"{re.escape(pass_key)}"\s*:\s*(true|false)', text, re.IGNORECASE)
        if m:
            return m.group(1).lower() == "true"
        # 宽松匹配：包含 yes/通过/满足/合格/ok
        lower = text.lower()
        positive = {"yes", "通过", "满足", "合格", "ok", "true", "是"}
        negative = {"no", "不通过", "不满足", "不合格", "false", "否"}
        has_positive = any(p in lower for p in positive)
        has_negative = any(n in lower for n in negative)
        if has_positive and not has_negative:
            return True
        if has_negative and not has_positive:
            return False
        return False

    # ------------------------------------------------------------------ #
    # Orchestration helpers
    # ------------------------------------------------------------------ #
    def _current_phase(
        self,
        definition: WorkflowDefinition,
        state: RuntimeState,
    ) -> Phase | None:
        if not definition.phases:
            return None
        completed = {
            phase_id
            for phase_id, result in state.phase_results.items()
            if result.status == "done"
        }
        terminal = {
            phase_id
            for phase_id, result in state.phase_results.items()
            if result.status in ("done", "failed", "blocked")
        }
        ready = definition.ready_phase_ids(
            completed_phase_ids=completed,
            terminal_phase_ids=terminal,
        )
        if state.current_phase_id in ready:
            return next(p for p in definition.phases if p.id == state.current_phase_id)
        if ready:
            state.current_phase_id = ready[0]
            return next(p for p in definition.phases if p.id == ready[0])
        state.current_phase_id = ""
        return None

    def _advance_phase(
        self,
        definition: WorkflowDefinition,
        state: RuntimeState,
    ) -> bool:
        current = next(
            (phase for phase in definition.phases if phase.id == state.current_phase_id),
            None,
        )
        if current is None:
            return False
        pr = state.phase_results.get(current.id)
        if not pr or pr.status not in ("done", "failed", "blocked"):
            return True  # 当前 phase 还没跑完，继续

        if pr.status == "done" and current.id not in state.completed_phase_ids:
            state.completed_phase_ids.append(current.id)
        state.current_phase_id = ""
        return self._current_phase(definition, state) is not None

    # ------------------------------------------------------------------ #
    # Board synchronization
    # ------------------------------------------------------------------ #
    def _sync_definition_to_board(self, workflow_id: str, definition: WorkflowDefinition) -> None:
        """把 definition 中所有 agent_call 映射为 kanban_tasks（幂等：已存在的 task 不重复创建）。

        扩展任务（kanban_plan_next 写回的 phase）在 context 里有 call.id -> 看板标题
        的持久化映射，优先复用该映射，避免 resume 时按生成标题重复建卡。
        """
        wf = self.store.get_workflow(workflow_id)
        persisted_titles = (
            dict((wf.context or {}).get("runtime_call_titles") or {}) if wf is not None else {}
        )
        tasks_by_title = {task.title: task for task in self.store.list_tasks(workflow_id)}
        call_to_title: dict[str, str] = {}
        phase_task_ids: dict[str, list[str]] = {phase.id: [] for phase in definition.phases}
        for phase in definition.phases:
            for call in phase.agent_calls:
                mapped_title = persisted_titles.get(call.id)
                task = tasks_by_title.get(mapped_title) if mapped_title else None
                if task is None:
                    title = f"[{phase.id}:{call.id}] {call.prompt[:40]}"
                    task = tasks_by_title.get(title)
                    if task is None:
                        task = self.store.add_task(
                            workflow_id=workflow_id,
                            title=title,
                            detail=call.prompt,
                            assignee=call.role,
                            status="pending",
                            auto_promote=False,
                        )
                        tasks_by_title[title] = task
                call_to_title[call.id] = task.title
                phase_task_ids[phase.id].append(task.id)

        dependencies = [
            (parent_task_id, child_task_id)
            for parent_phase_id, child_phase_id in definition.edges
            for parent_task_id in phase_task_ids[parent_phase_id]
            for child_task_id in phase_task_ids[child_phase_id]
        ]
        managed_task_ids = {
            task_id
            for task_ids in phase_task_ids.values()
            for task_id in task_ids
        }
        self.store.replace_workflow_dependencies(
            workflow_id,
            managed_task_ids,
            dependencies,
        )
        # 将 call->title 映射写回 workflow context 以便后续更新
        wf = self.store.get_workflow(workflow_id)
        if wf is not None:
            wf.context["runtime_call_titles"] = call_to_title
            self.store.update_workflow_status(workflow_id, wf.status, context=wf.context)
        self.store.promote_all_pending(workflow_id)

    def _sync_call_result_to_board(
        self,
        workflow_id: str,
        call: AgentCall,
        result: AgentCallResult,
    ) -> None:
        wf = self.store.get_workflow(workflow_id)
        titles = (wf.context or {}).get("runtime_call_titles") if wf else {}
        title = titles.get(call.id) if isinstance(titles, dict) else None
        if not title:
            # fallback：扫描标题包含 call.id 的任务
            for t in self.store.list_tasks(workflow_id):
                if f":{call.id}]" in t.title:
                    title = t.title
                    break
        if not title:
            return
        for t in self.store.list_tasks(workflow_id):
            if t.title == title:
                self.store.update_task_status(
                    t.id,
                    result.status,
                    result_summary=result.text or result.error,
                    artifacts=result.artifacts,
                )
                return

    # ------------------------------------------------------------------ #
    # Final synthesis
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_structured_summary(
        workflow: Workflow,
        definition: WorkflowDefinition,
        state: RuntimeState,
        *,
        max_text_length: int = 4000,
    ) -> dict[str, Any]:
        """从 runtime state 提取结构化执行摘要，供 Host Agent 或上层做二次总结。

        不调用 LLM，只汇总事实数据，避免直接 workflow 模式与 Host Agent 模式互相干扰。
        """
        if state.status == "paused":
            return {
                "workflow_id": workflow.id,
                "status": state.status,
                "title": workflow.title,
                "message": f"workflow 已暂停，当前阶段：{state.current_phase_id}。请在右侧看板点击 ▶️ 继续按钮恢复执行。",
                "phases": [],
                "failed_calls": [],
                "output_variables": {},
            }

        phases_summary: list[dict[str, Any]] = []
        failed_calls: list[dict[str, Any]] = []

        for phase in definition.phases:
            pr = state.phase_results.get(phase.id)
            phase_summary: dict[str, Any] = {
                "phase_id": phase.id,
                "phase_name": phase.name,
                "calls": [],
            }
            for call in phase.agent_calls or []:
                r = pr.call_results.get(call.id) if pr else None
                call_summary: dict[str, Any] = {
                    "call_id": call.id,
                    "role": call.role,
                    "status": r.status if r else "pending",
                }
                if r:
                    if r.status == "failed":
                        call_summary["error"] = r.error
                        failed_calls.append(
                            {
                                "phase": phase.name,
                                "role": call.role,
                                "call_id": call.id,
                                "error": r.error,
                            }
                        )
                    else:
                        text = r.text or ""
                        if len(text) > max_text_length:
                            text = text[:max_text_length] + "\n...[truncated]"
                        call_summary["text"] = text
                        if r.outputs:
                            call_summary["outputs"] = dict(r.outputs)
                        if r.artifacts:
                            call_summary["artifacts"] = list(r.artifacts)
                phase_summary["calls"].append(call_summary)
            phases_summary.append(phase_summary)

        return {
            "workflow_id": workflow.id,
            "status": state.status,
            "title": workflow.title,
            "phases": phases_summary,
            "failed_calls": failed_calls,
            "output_variables": dict(state.variables),
        }

    async def _synthesize(
        self,
        workflow: Workflow,
        definition: WorkflowDefinition,
        state: RuntimeState,
    ) -> str:
        """生成用户可直接阅读的最终答案（直接 workflow 模式使用）。"""
        if state.status == "paused":
            return f"workflow 已暂停，当前阶段：{state.current_phase_id}。请在右侧看板点击 ▶️ 继续按钮恢复执行。"

        summary_payload = self._build_structured_summary(
            workflow, definition, state, max_text_length=300
        )

        warning_text = ""
        failed_calls = summary_payload.get("failed_calls") or []
        if failed_calls:
            failed_desc = "\n".join(
                f"- [{fc['phase']}] {fc['role']}（{fc['call_id']}）：{fc['error'][:80]}"
                for fc in failed_calls
            )
            warning_text = (
                "⚠️ 部分子任务执行失败，但已通过其他子任务的产出继续完成 workflow。\n"
                f"失败子任务：\n{failed_desc}\n\n"
            )

        prompt = (
            f"用户原始请求：{workflow.title}\n"
            f"阶段执行结果：\n{json.dumps(summary_payload['phases'], ensure_ascii=False, indent=2)}\n"
            "请综合以上结果生成最终答案。"
        )
        synth_start = time.time()
        try:
            resp = await self.provider.chat([Message.user(prompt)])
            result = warning_text + (resp.text.strip() or "所有任务已执行完毕。")
            log.info("[DK Runtime] 综合结果完成，耗时 %.2fs", time.time() - synth_start)
            return result
        except Exception as exc:  # noqa: BLE001
            log.error("综合结果失败 type=%s", type(exc).__name__)
            return warning_text + "执行结束，但综合结果生成失败：内部错误"

    # ------------------------------------------------------------------ #
    # Utils
    # ------------------------------------------------------------------ #
    def _render_prompt(
        self,
        template: str,
        variables: dict[str, Any],
        inputs: dict[str, Any] | None = None,
    ) -> str:
        """渲染 prompt：支持 ${var} 和 {{var}} 两种占位符。"""
        rendered = template
        for key, value in variables.items():
            for pat in [f"${{{key}}}", f"{{{{{key}}}}}"]:
                rendered = rendered.replace(pat, str(value))
        # 处理 inputs 引用
        if inputs:
            for key, ref in inputs.items():
                full_key = f"{ref.source_phase_id}.{ref.source_call_id}.{ref.output_key}"
                value = variables.get(full_key, "" if ref.optional else f"<{key} 未就绪>")
                for pat in [f"${{{key}}}", f"{{{{{key}}}}}"]:
                    rendered = rendered.replace(pat, str(value))
        return rendered

    def _progress_chunk(
        self,
        workflow: Workflow,
        definition: WorkflowDefinition,
        state: RuntimeState,
        request_id: str,
        sequence: int,
        *,
        status: str | None = None,
        active_calls: list[AgentCall] | None = None,
        message: str = "",
    ) -> ResponseChunk:
        """构造 workflow_progress 帧，结构化描述当前阶段、已完成阶段和正在执行的调用。"""
        current_phase_id = state.current_phase_id
        current_phase: dict[str, Any] | None = None
        if current_phase_id:
            phase = next((p for p in definition.phases if p.id == current_phase_id), None)
            if phase:
                pr = state.phase_results.get(phase.id)
                phase_status = pr.status if pr else "running"
                current_phase = {
                    "id": phase.id,
                    "name": phase.name,
                    "description": phase.description or "",
                    "status": phase_status,
                }
        completed_phases: list[dict[str, Any]] = []
        for pid in state.completed_phase_ids:
            phase = next((p for p in definition.phases if p.id == pid), None)
            if phase:
                completed_phases.append({
                    "id": phase.id,
                    "name": phase.name,
                    "status": "done",
                })
        return ResponseChunk.workflow_progress(
            request_id,
            workflow.id,
            status=status or state.status or "running",
            current_phase=current_phase,
            completed_phases=completed_phases or None,
            active_calls=[
                {"call_id": c.id, "role": c.role or c.id, "phase_id": current_phase_id}
                for c in (active_calls or [])
            ] or None,
            message=message,
            sequence=sequence,
        )

    def _fail_remaining_tasks(self, workflow_id: str, reason: str) -> None:
        """把未完成的任务统一标记为失败（用于 stop/interrupt）。"""
        for t in self.store.list_tasks(workflow_id):
            if t.status in ("pending", "ready", "running"):
                self.store.update_task_status(t.id, "failed", result_summary=reason)

    def _mark_workflow_failed(self, workflow_id: str, reason: str) -> None:
        wf = self.store.get_workflow(workflow_id)
        new_context = {"error": reason}
        if wf is not None and wf.context:
            new_context = {**wf.context, "error": reason}
        self.store.update_workflow_status(workflow_id, "failed", context=new_context)
        state = self.store.load_runtime_state(workflow_id)
        if state:
            state.status = "failed"
            self.store.save_runtime_state(state)

    def _actor_label(self, role: str | None) -> str:
        labels = {
            "product_manager": "产品经理",
            "architect": "架构师",
            "coder": "开发",
            "tester": "测试",
            "pm": "产品经理",
            "dev": "开发",
            "qa": "测试",
            "lead": "研究组长",
            "analyst": "研究分析师",
            "writer": "报告架构师",
        }
        return labels.get(role or "", role) or "执行角色"

    def _actor_avatar(self, role: str | None) -> str:
        avatars = {
            "product_manager": "📋",
            "architect": "🏗️",
            "coder": "💻",
            "tester": "🧪",
            "pm": "📋",
            "dev": "💻",
            "qa": "🧪",
            "lead": "🔬",
            "analyst": "🔍",
            "writer": "📝",
        }
        return avatars.get(role or "", "🤖")
