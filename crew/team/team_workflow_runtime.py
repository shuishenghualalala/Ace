"""Team WorkflowRun 的规划进度和节点执行编排。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from crew.agent.file_changes import merge_changes
from crew.core.envelope import Envelope, ResponseChunk
from crew.state.logging import get_logger
from crew.team import result_presenter as team_presenter
from crew.team.models import TeamPlan, TeamPlanNode

log = get_logger("team.workflow_runtime")


def _visible_session_id(session_id: str) -> str:
    marker = "::turn::"
    return session_id.split(marker, 1)[0] if marker in session_id else session_id


def _join_stream_fragments(parts: list[str]) -> str:
    return "".join(str(part or "") for part in parts).strip()


class TeamWorkflowRuntime:
    """执行 Team WorkflowRun，业务决策通过 host 的稳定回调完成。

    ``host`` 是 InProcessTeamManager 的运行时服务集合；本类只组织规划进度、
    Leader 节点和成员节点的执行顺序，不拥有 TeamPlan、Team 或持久化缓存。
    """

    def __init__(self, host: Any) -> None:
        self.host = host

    async def stream_runtime_plan(
        self,
        envelope: Envelope,
        *,
        team: Any,
        goal: str,
        external_team_id: str,
        owner_account_id: str,
        execution_profile: dict[str, Any] | None,
        team_spec: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[ResponseChunk | None, TeamPlan | None]]:
        queue: asyncio.Queue[ResponseChunk] = asyncio.Queue()

        def on_progress(event: dict[str, Any]) -> None:
            queue.put_nowait(self.host._planning_progress_chunk(envelope, event))

        task = asyncio.create_task(self.host._ensure_runtime_plan_async(
            envelope.session_id,
            team,
            goal,
            external_team_id,
            owner_account_id=owner_account_id,
            execution_profile=execution_profile,
            team_spec=team_spec,
            planning_progress=on_progress,
        ))
        while not task.done():
            queue_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait([task, queue_task], return_when=asyncio.FIRST_COMPLETED)
            if queue_task in done:
                yield queue_task.result(), None
            else:
                queue_task.cancel()
        while not queue.empty():
            yield queue.get_nowait(), None
        yield None, task.result()

    async def run_required_workflow(
        self,
        envelope: Envelope,
        *,
        team: Any,
        external_team_id: str,
        execution_profile: dict[str, Any] | None = None,
        team_spec: dict[str, Any] | None = None,
    ) -> AsyncIterator[ResponseChunk]:
        goal = str(envelope.query or "").strip()
        explicit_profile = envelope.params.get("team_execution_profile")
        if execution_profile is not None:
            resolved_execution_profile = execution_profile
        elif envelope.params.get("team_confirm_execution_mode") and not isinstance(explicit_profile, dict):
            resolved_execution_profile = await self.host._confirm_team_execution_mode(envelope)
        else:
            resolved_execution_profile = self.host._team_execution_profile(envelope)
        plan = None
        async for planning_chunk, planned in self.stream_runtime_plan(
            envelope,
            team=team,
            goal=goal,
            external_team_id=external_team_id,
            owner_account_id=envelope.user_id,
            execution_profile=resolved_execution_profile,
            team_spec=team_spec,
        ):
            if planning_chunk is not None:
                yield planning_chunk
            if planned is not None:
                plan = planned
        planning_key = self.host._key(envelope.session_id, envelope.user_id)
        missing_info = self.host._planning_missing_info.pop(planning_key, [])
        if plan is None and missing_info:
            question = "为了正确拆分本轮任务，请补充：" + "；".join(missing_info)
            try:
                followup_session_id, question_id = await self.host._send_followup_question_to(
                    _visible_session_id(envelope.session_id),
                    [{"id": "workflow_planning_missing_info", "question": question, "inputMode": "text"}],
                    title="Leader 需要补充任务信息",
                    origin={
                        "agent_id": "leader",
                        "agent_name": "Leader",
                        "team_session_id": envelope.session_id,
                        "mention_intent": "workflow_planning",
                    },
                )
                answers = await self.host._wait_for_answer(followup_session_id, question_id)
            except Exception as exc:  # noqa: BLE001
                log.info("Workflow PlanningDecision followup failed session=%s err=%s", envelope.session_id, exc)
                yield ResponseChunk.error(envelope.request_id, "Leader 追问任务缺失信息失败，请补充目标后重试。")
                return
            answer_texts = [
                str(value or "").strip()
                for item in answers
                if isinstance(item, dict)
                for value in (item.get("answers") if isinstance(item.get("answers"), list) else [])
                if str(value or "").strip()
            ]
            if not answer_texts:
                yield ResponseChunk.final(
                    envelope.request_id,
                    "任务还缺少关键信息，已暂停本轮规划。请补充必要信息后继续。",
                    reason="planning_missing_info",
                )
                return
            goal = f"{goal}\n\n用户补充：{'；'.join(answer_texts)}"
            plan = None
            async for planning_chunk, planned in self.stream_runtime_plan(
                envelope,
                team=team,
                goal=goal,
                external_team_id=external_team_id,
                owner_account_id=envelope.user_id,
                execution_profile=execution_profile,
            ):
                if planning_chunk is not None:
                    yield planning_chunk
                if planned is not None:
                    plan = planned
        if plan is None:
            if self.host._planning_missing_info.pop(planning_key, []):
                yield ResponseChunk.final(
                    envelope.request_id,
                    "补充信息后任务仍不够明确，已暂停规划，请重新描述目标。",
                    reason="planning_missing_info",
                )
            else:
                yield ResponseChunk.final(
                    envelope.request_id,
                    "Team 没有可委派成员，无法创建团队执行计划。",
                    reason="team_plan_empty",
                )
            return

        yield ResponseChunk.status_event(envelope.request_id, "Team Runtime 已创建 TeamPlan，开始按节点派活…")
        max_rounds = max(1, len(plan.nodes) * 3)
        profile = execution_profile or {}
        budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
        try:
            max_attempts = max(1, int(budget.get("max_retries") or 2))
        except (TypeError, ValueError):
            max_attempts = 2
        try:
            max_review_revisions = max(0, int(budget.get("max_review_revisions") or 2))
        except (TypeError, ValueError):
            max_review_revisions = 2
        for _ in range(max_rounds):
            progressed = False
            pause_dispatch_this_round = False
            for node in list(plan.nodes.values()):
                if node.status not in {"pending", "failed"}:
                    continue
                if node.assignee != "leader" or not self.host._node_ready(plan, node):
                    continue
                attempt = node.attempt_count + 1
                should_run_leader = (
                    node.node_id in {"leader_review", "leader_summary"}
                    or (
                        node.node_id not in {"leader_plan"}
                        and not node.node_id.startswith("runtime_diagnosis_")
                    )
                )
                if should_run_leader:
                    try:
                        result = ""
                        async for live_chunk, final_result in self.host._stream_leader_node(
                            envelope,
                            team=team,
                            plan=plan,
                            node=node,
                            attempt=attempt,
                        ):
                            if live_chunk is not None:
                                yield live_chunk
                            if final_result is not None:
                                result = final_result
                        if not self.host._leader_model_result_usable(result):
                            result = self.host._leader_control_text(plan, node, fallback_error="Leader 模型返回内容不可用")
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "team leader node model execution failed, fallback to control text: session=%s node=%s error=%s",
                            envelope.session_id,
                            node.node_id,
                            exc,
                        )
                        result = self.host._leader_control_text(plan, node, fallback_error=str(exc))
                else:
                    result = self.host._leader_control_text(plan, node)
                is_review = node.node_id == "leader_review" or node.node_id.startswith("leader_review_")
                decision: dict[str, str] | None = None
                followup_resumed = False
                leader_node_requeued = False
                if is_review:
                    parsed_decision = self.host._parse_leader_review_decision(result)
                    if self.host._leader_review_decision_conflicts(plan, node, parsed_decision):
                        result = ""
                        async for live_chunk, final_result in self.host._stream_leader_node(
                            envelope,
                            team=team,
                            plan=plan,
                            node=node,
                            attempt=attempt,
                            correction=(
                                "上一次判断声称缺少成员方案，但结构化上游摘要和产物引用均已提供。"
                                "请重新阅读“已完成节点”和 Team Context Summary 后审阅，不要要求用户重复提供已有内容。"
                            ),
                        ):
                            if live_chunk is not None:
                                yield live_chunk
                            if final_result is not None:
                                result = final_result
                        parsed_decision = self.host._parse_leader_review_decision(result)
                        if self.host._leader_review_decision_conflicts(plan, node, parsed_decision):
                            parsed_decision = {
                                "action": "approve",
                                "target_node_id": "",
                                "message": "成员方案和产物引用已齐备，矛盾重审仍未正确消费上下文，按结构化提交放行后续执行。",
                                "instructions": "",
                            }
                    if (node.metadata or {}).get("user_followup_answers") and parsed_decision.get("action") == "ask_user":
                        parsed_decision = {
                            **parsed_decision,
                            "action": "block",
                            "message": (
                                f"{parsed_decision.get('message') or 'Leader 仍无法决策'} "
                                "用户已回答本次追问，Review 不再重复 ask_user，请人工检查审阅条件。"
                            ).strip(),
                        }
                    decision = self.host._apply_leader_review_decision(
                        plan,
                        node,
                        parsed_decision,
                        owner_account_id=envelope.user_id,
                        max_revisions=max_review_revisions,
                    )
                    result = str(decision.get("message") or result)
                    node.attempt_count = attempt
                    if decision.get("action") == "ask_user":
                        questions = [{
                            "id": "leader_review_decision",
                            "question": result or "Leader 需要你确认后续处理方式。",
                            "options": ["确认并继续", "需要调整"],
                        }]
                        try:
                            visible_session_id = _visible_session_id(envelope.session_id)
                            followup_session_id, question_id = await self.host._send_followup_question_to(
                                visible_session_id,
                                questions,
                                title="Leader 需要确认",
                                origin={
                                    "agent_id": "leader",
                                    "agent_name": "Leader",
                                    "team_session_id": envelope.session_id,
                                    "node_id": node.node_id,
                                    "mention_intent": "ask_user",
                                },
                            )
                            try:
                                answers = await self.host._wait_for_answer(followup_session_id, question_id)
                            except TypeError:
                                answers = await self.host._wait_for_answer(followup_session_id, question_id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "Leader review followup failed session=%s node=%s err=%s",
                                envelope.session_id,
                                node.node_id,
                                exc,
                            )
                            answers = []
                        if self.host._review_followup_answered(answers):
                            review_meta = dict(node.metadata or {})
                            review_meta["user_followup_answers"] = answers
                            review_meta["followup_count"] = int(review_meta.get("followup_count") or 0) + 1
                            node.metadata = review_meta
                            self.host._mark_plan_node(
                                envelope.session_id,
                                node.node_id,
                                owner_account_id=envelope.user_id,
                                status="pending",
                                result_summary="",
                                last_error="",
                            )
                            followup_resumed = True
                            pause_dispatch_this_round = True
                else:
                    leader_node_requeued = self.host._consume_plan_change_requeue(node)
                    if leader_node_requeued:
                        result = result or "TeamPlan 已更新，等待新增节点完成后重新汇总。"
                        self.host._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="pending",
                            result_summary="TeamPlan 已更新，等待新增节点完成后重新执行。",
                            attempt_count=attempt,
                            last_error="",
                        )
                    else:
                        self.host._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="completed",
                            result_summary=result,
                            attempt_count=attempt,
                        )
                        if node.node_id == "leader_summary":
                            await self.host._refresh_final_display_metadata(
                                plan,
                                owner_account_id=envelope.user_id,
                                final_summary=result,
                            )
                leader_event_type = (
                    "team_planning_progress" if node.node_id == "leader_plan"
                    else "team_summary" if node.node_id == "leader_summary"
                    and not leader_node_requeued
                    else "team_review" if is_review
                    else "team_decision"
                )
                leader_artifacts = (
                    team_presenter.artifact_cards(team.bus.list_artifacts(envelope.session_id))
                    if node.node_id == "leader_summary"
                    else None
                )
                yield self.host._recorded_team_internal_chunk(
                    envelope,
                    agent_id="leader",
                    role="leader",
                    is_leader=True,
                    source_session_id=f"{envelope.session_id}::leader",
                    text=result,
                    node_id=node.node_id,
                    event_type=leader_event_type,
                    artifacts=leader_artifacts,
                )
                if decision is not None:
                    action = str(decision.get("action") or "approve")
                    target = str(decision.get("target_node_id") or "")
                    review_meta = dict(node.metadata or {})
                    timeout_default_note = str(review_meta.get("user_followup_timeout_note") or "")
                    decision_text = {
                        "approve": "审阅通过，继续后续流程。",
                        "revise": f"审阅未通过，@{plan.nodes[target].assignee} 请继续修订。" if target in plan.nodes else "审阅未通过，请继续修订。",
                        "ask_user": (
                            f"{timeout_default_note} Leader 将带着默认选择重新审阅。".strip()
                            if timeout_default_note
                            else "已收到用户回答，Leader 将带着回答重新审阅。"
                            if followup_resumed
                            else "需要用户补充信息，团队流程已暂停。"
                        ),
                        "block": "当前条件下无法继续，团队流程已阻塞。",
                    }[action]
                    yield self.host._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=decision_text,
                        node_id=node.node_id,
                        event_type="team_decision",
                        mention_from="leader",
                        mention_to=[plan.nodes[target].assignee] if target in plan.nodes else [],
                        mention_intent=action,
                    )
                yield ResponseChunk.status_event(envelope.request_id, f"完成节点「{node.title}」")
                progressed = True
                if pause_dispatch_this_round:
                    break

            if pause_dispatch_this_round:
                progressed = True
                continue

            dispatch_nodes: list[TeamPlanNode] = []
            occupied_assignees: set[str] = set()
            for node in list(plan.nodes.values()):
                if node.status not in {"pending", "failed"}:
                    continue
                if not self.host._node_ready(plan, node):
                    continue
                if node.assignee == "leader":
                    continue
                staffing_trigger = self.host._runtime_staffing_trigger(
                    team,
                    node,
                    owner_account_id=envelope.user_id,
                    max_attempts=max_attempts,
                )
                if staffing_trigger is not None:
                    if staffing_trigger.get("trigger_type") == "existing_member_reassignment":
                        team, staffing_status = await self.host._handle_runtime_staffing(
                            envelope,
                            plan,
                            node,
                            team,
                            staffing_trigger,
                        )
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"已有成员 {staffing_trigger.get('replacement_assignee')} 可以承担，已改派「{node.title}」，准备继续。",
                        )
                        progressed = True
                        continue
                    yield ResponseChunk.status_event(
                        envelope.request_id,
                        f"「{node.title}」需要一位协作助手，等待你的选择…",
                    )
                    team, staffing_status = await self.host._handle_runtime_staffing(
                        envelope,
                        plan,
                        node,
                        team,
                        staffing_trigger,
                    )
                    if staffing_status == "applied":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"协作助手已加入本次任务，正在继续「{node.title}」。",
                        )
                    elif staffing_status == "declined":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"这次先不添加协作助手，「{node.title}」暂时停在这里。",
                        )
                    elif staffing_status == "failed":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"暂时没能找到可加入的协作助手，「{node.title}」先停在这里。",
                        )
                    progressed = True
                    continue
                if node.assignee not in team.teammates:
                    self.host._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"未知或不可委派成员 {node.assignee}",
                        decision="保持用户团队不变，停止自动改派。",
                        suggested_action="请确认是否补充成员、改派节点或由 Leader 临时承接。",
                    )
                    self.host._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"无法派活：未知或不可委派成员 {node.assignee}",
                        last_error=f"unknown assignee: {node.assignee}",
                    )
                    progressed = True
                    continue
                if node.attempt_count >= max_attempts:
                    self.host._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"节点连续失败 {node.attempt_count} 次",
                        decision="停止自动重试，保留当前团队并等待用户确认下一步。",
                        suggested_action="可选择补员、改派、缩小任务范围或手动重试。",
                    )
                    self.host._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"节点连续失败 {node.attempt_count} 次，已停止重试，等待 Leader/用户介入。",
                    )
                    progressed = True
                    continue
                if node.assignee in occupied_assignees:
                    continue
                occupied_assignees.add(node.assignee)
                dispatch_nodes.append(node)

            dispatch_team = team
            live_queue: asyncio.Queue[ResponseChunk] = asyncio.Queue()
            member_stream_text: dict[str, list[str]] = {}
            member_runtime_events: dict[str, list[dict[str, Any]]] = {}
            member_file_changes: dict[str, list[dict[str, Any]]] = {}

            def _relay_child_chunk(node: TeamPlanNode, member: str, chunk: ResponseChunk) -> None:
                text = ""
                append = False
                started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                now = time.time()
                if chunk.kind == "file_changes":
                    files = chunk.body.get("files") if isinstance(chunk.body, dict) else None
                    if isinstance(files, list):
                        member_file_changes[node.node_id] = merge_changes(
                            member_file_changes.get(node.node_id, []),
                            [item for item in files if isinstance(item, dict)],
                        )
                    return
                runtime_event = self.host._child_chunk_execution_event(node, member, chunk)
                if runtime_event is not None:
                    runtime_event = {
                        **runtime_event,
                        "plan_id": plan.plan_id,
                        "node_id": node.node_id,
                        "request_id": envelope.request_id,
                        "attempt_id": str((node.metadata or {}).get("execution_attempt") or ""),
                        "actor_id": member,
                        "timestamp": now,
                    }
                    events = member_runtime_events.setdefault(node.node_id, [])
                    events.append(runtime_event)
                    # Thinking commonly arrives as many small chunks. Keeping only
                    # the last ten events evicted early tools/thoughts before the
                    # final team_submit was built, so the timeline disappeared on
                    # completion or refresh. Bound generously and compact below.
                    member_runtime_events[node.node_id] = events[-200:]
                    self.host._append_plan_node_event(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        event=runtime_event,
                    )
                    live_queue.put_nowait(self.host._recorded_team_internal_chunk(
                        envelope,
                        agent_id=member,
                        role=node.title,
                        source_session_id=f"{envelope.session_id}::{member}",
                        text="",
                        append=True,
                        node_id=node.node_id,
                        event_type="team_stream",
                        display_mode="stream",
                        collapsed_title=f"{node.title} 的执行过程",
                        thinking=str(runtime_event.get("event_text") or "") if runtime_event.get("event_type") == "thinking" else "",
                        tool_calls=[dict(runtime_event.get("tool_call") or {})]
                        if runtime_event.get("event_type") == "tool" and isinstance(runtime_event.get("tool_call"), dict)
                        else None,
                        turn_started_at=started_at,
                        turn_duration=max(0.0, now - started_at),
                        timestamp=now,
                    ))
                if chunk.kind == "delta":
                    text = str(chunk.body.get("text") or "")
                    append = True
                    if text:
                        member_stream_text.setdefault(node.node_id, []).append(text)
                elif chunk.kind == "final":
                    final_text = str(chunk.body.get("text") or "")
                    if final_text:
                        member_stream_text.setdefault(node.node_id, []).append(final_text)
                    return
                elif chunk.kind == "tool":
                    return
                elif chunk.kind == "thinking":
                    return
                elif chunk.kind == "status":
                    return
                elif chunk.kind == "error":
                    text = "我这边执行遇到问题，需要看板详情继续排查。"
                if not text:
                    return
                live_queue.put_nowait(self.host._recorded_team_internal_chunk(
                    envelope,
                    agent_id=member,
                    role=node.title,
                    source_session_id=f"{envelope.session_id}::{member}",
                    text=text,
                    append=append,
                    node_id=node.node_id,
                    event_type="team_stream",
                    display_mode="stream",
                    collapsed_title=f"{node.title} 的执行过程",
                    turn_started_at=started_at,
                    turn_duration=max(0.0, time.time() - started_at),
                    timestamp=time.time(),
                ))

            async def _dispatch_node(node: TeamPlanNode) -> tuple[TeamPlanNode, dict[str, Any] | None, Exception | None]:
                attempt = node.attempt_count + 1
                node.metadata = {
                    **dict(node.metadata or {}),
                    "execution_started_at": time.time(),
                    "execution_attempt": attempt,
                    "execution_snapshot": self.host.execution_snapshot(
                        envelope.session_id,
                        node.assignee,
                        owner_account_id=envelope.user_id,
                        plan_node_id=node.node_id,
                    ),
                }
                self.host._mark_plan_node(
                    envelope.session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="in_progress",
                    attempt_count=attempt,
                    last_error="",
                )
                try:
                    before_artifact_ids = {
                        str(item.get("artifact_id") or "")
                        for item in dispatch_team.bus.list_artifacts(envelope.session_id)
                    }
                    delegate_cwd = self.host._team_delegate_cwd(
                        envelope,
                        goal,
                        node_id=node.node_id,
                        agent_id=node.assignee,
                    )
                    workspace_scope = "isolated_turn_workspace" if delegate_cwd else "shared_workspace"
                    member_cwd = delegate_cwd or self.host._team_shared_cwd(envelope)
                    workspace_snapshot = self.host._workspace_file_snapshot(delegate_cwd) if delegate_cwd else {}
                    upstream_artifact_refs = self.host._node_upstream_artifact_refs(plan, node)
                    upstream_artifact_refs.extend(
                        str(item.get("path") or "")
                        for item in (envelope.params.get("referenced_paths") or [])
                        if isinstance(item, dict) and str(item.get("path") or "").strip()
                    )
                    upstream_artifact_text = self.host._format_upstream_artifacts(upstream_artifact_refs)
                    upstream_summary = self.host._node_upstream_summary(plan, node)
                    workspace_guard = self.host._workspace_guard_config(
                        workspace_scope,
                        delegate_cwd,
                        upstream_artifact_refs,
                    )
                    instruction_detail = node.detail
                    revision_instructions = str((node.metadata or {}).get("revision_instructions") or "").strip()
                    if revision_instructions:
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "Leader 审阅未通过，请针对以下意见修订后重新提交：\n"
                            f"{revision_instructions}"
                        )
                    if upstream_artifact_text:
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "上游产物路径（优先读取这些文件作为当前节点输入）：\n"
                            f"{upstream_artifact_text}"
                        )
                    if team_presenter.is_verify_execution_node(node):
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "验证执行要求：先根据上游产物路径复核并必要时补充测试方案，再执行功能验证、回归检查和缺陷记录；"
                            "如果上游产物路径缺失或不可读，请明确报告阻塞。"
                        )
                    task_payload_meta = {
                        "team_goal": goal,
                        "team_member_id": node.assignee,
                        "team_plan_node_id": node.node_id,
                        "team_node_title": node.title,
                        "team_node_detail": instruction_detail,
                        "team_upstream_summary": upstream_summary,
                        "team_upstream_artifacts": upstream_artifact_refs,
                        "team_display_name": dispatch_team.display_name,
                        "external_team_role": "member",
                        "external_task_budget": "focused",
                        "team_workspace_scope": workspace_scope,
                        "external_output_contract": self.host._delegate_output_contract(workspace_scope),
                        "workspace_instructions": self.host._team_roster_summary(dispatch_team),
                        "execution_snapshot": self.host.execution_snapshot(
                            envelope.session_id,
                            node.assignee,
                            owner_account_id=envelope.user_id,
                            plan_node_id=node.node_id,
                        ),
                    }
                    if envelope.params.get("active_skills"):
                        task_payload_meta["active_skills"] = list(
                            envelope.params.get("active_skills") or []
                        )
                    if member_cwd:
                        task_payload_meta["cwd"] = member_cwd
                    if workspace_guard:
                        task_payload_meta["workspace_guard"] = workspace_guard
                    result = await self.host.request_delegate(
                        envelope.session_id,
                        member=node.assignee,
                        instruction=f"{node.title}\n\n{instruction_detail}",
                        requester_member_id="leader",
                        external_team_id=external_team_id,
                        plan_node_id=node.node_id,
                        wait_for_result=True,
                        owner_account_id=envelope.user_id,
                        on_child_chunk=lambda member, chunk, current=node: _relay_child_chunk(current, member, chunk),
                        task_payload_meta=task_payload_meta,
                        finalize_plan_node=False,
                        attachments=envelope.attachments,
                    )
                    snapshot_changes = (
                        self.host._workspace_file_changes(delegate_cwd, workspace_snapshot)
                        if delegate_cwd
                        else []
                    )
                    result["_workspace_file_changes"] = merge_changes(
                        snapshot_changes,
                        member_file_changes.get(node.node_id, []),
                    )
                    result["_workspace_root"] = member_cwd
                    result["_workspace_changed_paths"] = [
                        str(item.get("path") or "")
                        for item in result["_workspace_file_changes"]
                        if item.get("status") != "deleted" and str(item.get("path") or "")
                    ]
                    artifacts = self.host._node_owned_artifacts([
                        item for item in dispatch_team.bus.list_artifacts(envelope.session_id)
                        if str(item.get("artifact_id") or "") not in before_artifact_ids
                    ], node=node, task_id=str((result or {}).get("task_id") or ""), workspace_root=member_cwd)
                    result["artifacts"] = artifacts
                    return node, result, None
                except asyncio.CancelledError as exc:
                    return node, None, exc
                except Exception as exc:  # noqa: BLE001
                    return node, None, exc

            if dispatch_nodes:
                progressed = True
                if len(dispatch_nodes) > 1:
                    names = "、".join(f"{node.title}→{node.assignee}" for node in dispatch_nodes)
                    yield ResponseChunk.status_event(envelope.request_id, f"并发派发节点：{names}")
                else:
                    node = dispatch_nodes[0]
                    yield ResponseChunk.status_event(
                        envelope.request_id,
                        f"派发节点「{node.title}」给 {node.assignee}（第 {node.attempt_count + 1} 次）…",
                    )
                for node in dispatch_nodes:
                    if not team_presenter.should_show_assignment(plan, node):
                        continue
                    yield self.host._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=team_presenter.assignment_text(node),
                        node_id=node.node_id,
                        event_type="team_assign",
                        mention_from="leader",
                        mention_to=[node.assignee],
                        mention_intent="assign",
                    )
                def _finish_dispatch_result(
                    node: TeamPlanNode,
                    result: dict[str, Any] | None,
                    error: Exception | None,
                ) -> list[ResponseChunk]:
                    chunks: list[ResponseChunk] = []
                    attempt = node.attempt_count
                    if error is not None:
                        started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                        finished_at = time.time()
                        self.host._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome="neutral",
                            quality_weight=0.0,
                            assessment_source="execution_assessment",
                            failure_kind="cancelled" if isinstance(error, asyncio.CancelledError) else "runtime",
                        )
                        chunks.append(self.host._recorded_team_internal_chunk(
                            envelope,
                            agent_id=node.assignee,
                            role=node.title,
                            source_session_id=f"{envelope.session_id}::{node.assignee}",
                            text=f"@leader {node.title} 执行失败：{error}",
                            node_id=node.node_id,
                            event_type="team_submit",
                            turn_started_at=started_at,
                            turn_duration=max(0.0, finished_at - started_at),
                            timestamp=finished_at,
                            mention_from=node.assignee,
                            mention_to=["leader"],
                            mention_intent="submit",
                        ))
                        if isinstance(error, asyncio.CancelledError):
                            self.host._mark_plan_node(
                                envelope.session_id,
                                node.node_id,
                                owner_account_id=envelope.user_id,
                                status="cancelled",
                                result_summary="已停止当前回复",
                                attempt_count=attempt,
                                last_error="cancelled",
                            )
                            raise error
                        retryable = attempt < max_attempts
                        self.host._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=str(error),
                            decision="补充失败上下文后按原成员重试。" if retryable else "达到自动重试上限，停止重试并等待用户确认。",
                            suggested_action="" if retryable else "请确认是否补员、改派或调整任务目标。",
                            retryable=retryable,
                        )
                        if retryable:
                            self.host._insert_runtime_diagnostic_node(
                                plan,
                                node,
                                owner_account_id=envelope.user_id,
                                reason=str(error),
                            )
                        self.host._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="failed" if attempt < max_attempts else "blocked",
                            result_summary=f"节点执行失败，Runtime 将在下一轮尝试重排或阻塞：{error}",
                            attempt_count=attempt,
                            last_error=str(error),
                        )
                        return chunks

                    task_id = str((result or {}).get("task_id") or "")
                    started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                    finished_at = time.time()
                    output = str((result or {}).get("output") or "").strip()
                    artifacts = team_presenter.artifact_cards(list((result or {}).get("artifacts") or []))
                    turn_file_changes = [
                        dict(item)
                        for item in (result or {}).get("_workspace_file_changes") or []
                        if isinstance(item, dict) and str(item.get("path") or "").strip()
                    ]
                    changed_paths = {
                        str(path)
                        for path in (result or {}).get("_workspace_changed_paths") or []
                        if str(path).strip()
                    }
                    artifact_refs = [
                        str(item.get("path") or item.get("artifact_id") or "")
                        for item in artifacts
                        if str(item.get("path") or item.get("artifact_id") or "").strip()
                    ]
                    node_result = (
                        self.host._first_nonempty_text(
                            output,
                            str((result or {}).get("result") or "").strip(),
                            "".join(member_stream_text.get(node.node_id, [])).strip(),
                            str(node.result_summary or "").strip(),
                        )
                        or "当前节点已完成，详细过程可在看板中查看。"
                    )
                    is_review_submission = team_presenter.is_review_submission_node(node)
                    for runtime_event in member_runtime_events.get(node.node_id, [])[-8:]:
                        self.host._append_plan_node_event(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            event=runtime_event,
                        )
                    if is_review_submission and node_result:
                        auto_artifact = self.host._write_node_markdown_artifact(
                            envelope,
                            team=dispatch_team,
                            node=node,
                            task_id=task_id,
                            content=node_result,
                        )
                        if auto_artifact:
                            artifacts.extend(team_presenter.artifact_cards([auto_artifact]))
                            artifact_ref = str(auto_artifact.get("path") or auto_artifact.get("artifact_id") or "")
                            if artifact_ref and artifact_ref not in artifact_refs:
                                artifact_refs.append(artifact_ref)
                    elif node_result:
                        runtime_artifact_text = "\n".join(
                            str(item.get("event_text") or "")
                            for item in member_runtime_events.get(node.node_id, [])
                            if str(item.get("event_type") or "") == "tool"
                        )
                        auto_file_artifacts = self.host._auto_file_artifacts_from_result(
                            envelope,
                            team=dispatch_team,
                            node=node,
                            task_id=task_id,
                            text="\n".join(part for part in [node_result, runtime_artifact_text] if part),
                            existing_artifacts=artifacts,
                            changed_paths=changed_paths,
                            workspace_root=str((result or {}).get("_workspace_root") or ""),
                        )
                        if auto_file_artifacts:
                            artifacts.extend(team_presenter.artifact_cards(auto_file_artifacts))
                            for artifact in auto_file_artifacts:
                                artifact_ref = str(artifact.get("path") or artifact.get("artifact_id") or "")
                                if artifact_ref and artifact_ref not in artifact_refs:
                                    artifact_refs.append(artifact_ref)
                    result_summary = team_presenter.business_result_summary(
                        node,
                        node_result,
                        is_review_submission=is_review_submission,
                    )
                    result_contract = team_presenter.extract_result_contract(node_result)
                    result_contract["status_signal"] = (
                        self.host._runtime_result_status(
                            node,
                            member_runtime_events.get(node.node_id, []),
                        )
                        or "unknown"
                    )
                    assessment = self.host._assess_node_execution(
                        node,
                        runtime_events=member_runtime_events.get(node.node_id, []),
                        artifact_refs=artifact_refs,
                        changed_paths=changed_paths,
                        result_contract=result_contract,
                    )
                    summary_text = (
                        f"@leader {result_summary}。"
                        if assessment.execution_status == "completed"
                        else f"@leader 「{node.title}」未通过执行验收：{assessment.reason}"
                    )
                    process_text = "".join(member_stream_text.get(node.node_id, [])).strip()
                    runtime_events = member_runtime_events.get(node.node_id, [])
                    runtime_thinking = _join_stream_fragments([
                        str(item.get("event_text") or "")
                        for item in runtime_events
                        if str(item.get("event_type") or "") == "thinking"
                        and str(item.get("event_text") or "").strip()
                    ])
                    runtime_tool_calls_by_id: dict[str, dict[str, Any]] = {}
                    for item in runtime_events:
                        tool_call = item.get("tool_call")
                        if str(item.get("event_type") or "") != "tool" or not isinstance(tool_call, dict):
                            continue
                        tool_id = str(tool_call.get("id") or f"tool_{len(runtime_tool_calls_by_id)}")
                        previous = runtime_tool_calls_by_id.get(tool_id, {})
                        runtime_tool_calls_by_id[tool_id] = {
                            **previous,
                            **tool_call,
                            "arguments": tool_call.get("arguments") or previous.get("arguments") or {},
                            "result": tool_call.get("result") or previous.get("result") or "",
                        }
                    runtime_tool_calls = list(runtime_tool_calls_by_id.values())
                    chunks.append(self.host._recorded_team_internal_chunk(
                        envelope,
                        agent_id=node.assignee,
                        role=node.title,
                        source_session_id=f"{envelope.session_id}::{node.assignee}",
                        text=summary_text,
                        node_id=node.node_id,
                        event_type="team_submit",
                        process_text=process_text,
                        artifacts=artifacts,
                        turn_file_changes=turn_file_changes,
                        thinking=runtime_thinking,
                        tool_calls=runtime_tool_calls,
                        turn_started_at=started_at,
                        turn_duration=max(0.0, finished_at - started_at),
                        timestamp=finished_at,
                        mention_from=node.assignee,
                        mention_to=["leader"],
                        mention_intent="submit" if is_review_submission else "handoff",
                    ))
                    if assessment.execution_status != "completed":
                        ack_text = f"@{node.assignee} 「{node.title}」未通过执行验收，已标记为{assessment.execution_status}。"
                    elif is_review_submission:
                        ack_text = f"@{node.assignee} 已收到「{node.title}」方案，将进入 Leader 审阅。"
                    elif self.host._result_requires_user_input(result_summary, result_contract):
                        ack_text = f"@{node.assignee} 已收到，我会结合团队状态判断是否需要向用户补充信息。"
                    else:
                        ack_text = f"@{node.assignee} 已收到「{node.title}」提交，我会结合计划状态推进下一步。"
                    chunks.append(self.host._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=ack_text,
                        node_id=node.node_id,
                        event_type="team_ack",
                        mention_from="leader",
                        mention_to=[node.assignee],
                        mention_intent="ack",
                    ))
                    node_meta = dict(node.metadata or {})
                    full_result_ref, full_result_bytes = self.host._persist_node_full_result(
                        envelope,
                        node,
                        node_result,
                    )
                    if full_result_ref:
                        node_meta["full_result_ref"] = full_result_ref
                        node_meta["full_result_bytes"] = full_result_bytes
                    node_meta["result_contract"] = result_contract
                    node_meta["execution_assessment"] = assessment.to_dict()
                    node_meta["execution_snapshot"] = self.host._execution_snapshot_for_attempt(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        source_attempt_id=task_id,
                    )
                    node.metadata = node_meta
                    if assessment.execution_status != "completed":
                        outcome, quality_weight, failure_kind = self.host._profile_outcome_from_execution(assessment)
                        self.host._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome=outcome,
                            quality_weight=quality_weight,
                            assessment_source="execution_assessment",
                            failure_kind=failure_kind,
                            source_attempt_id=task_id,
                        )
                        retryable = assessment.execution_status == "failed" and attempt < max_attempts
                        self.host._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=assessment.reason,
                            decision="保留结构化失败证据并按原成员重试。" if retryable else "阻止下游节点继续执行，等待 Leader/用户处理。",
                            suggested_action="" if retryable else "请检查权限、输入产物或调整任务后再继续。",
                            retryable=retryable,
                        )
                        if retryable:
                            self.host._insert_runtime_diagnostic_node(
                                plan,
                                node,
                                owner_account_id=envelope.user_id,
                                reason=assessment.reason,
                            )
                        failure_summary = f"节点未通过执行验收：{assessment.reason}"
                        self.host._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status=assessment.execution_status,
                            result_summary=failure_summary,
                            artifact_refs=artifact_refs,
                            delegate_task_id=task_id,
                            attempt_count=attempt,
                            last_error=assessment.reason,
                        )
                        return chunks
                    review_reason = ""
                    if self.host._node_contract_requires_leader_review(node):
                        review_reason = "节点契约要求 Leader review"
                    elif self.host._result_needs_leader_review(result_summary, result_contract):
                        review_reason = "成员提交需要 Leader 确认或补充信息"
                    elif self.host._has_open_member_question(dispatch_team, task_id):
                        review_reason = "成员通过 Team Bus 向 Leader 提出待确认问题"
                    if review_reason:
                        self.host._insert_leader_review_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=review_reason,
                        )
                    else:
                        outcome, quality_weight, failure_kind = self.host._profile_outcome_from_execution(assessment)
                        self.host._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome=outcome,
                            quality_weight=quality_weight,
                            assessment_source="execution_assessment",
                            failure_kind=failure_kind,
                            source_attempt_id=task_id,
                        )
                    self.host._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="completed",
                        result_summary=result_summary,
                        artifact_refs=artifact_refs,
                        delegate_task_id=task_id,
                        attempt_count=attempt,
                    )
                    return chunks

                pending_tasks = [asyncio.create_task(_dispatch_node(node)) for node in dispatch_nodes]
                for task in pending_tasks:
                    self.host._track_delegate_task(envelope.session_id, envelope.user_id, task)
                try:
                    while pending_tasks:
                        queue_task = asyncio.create_task(live_queue.get())
                        done, _ = await asyncio.wait(
                            [*pending_tasks, queue_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if queue_task in done:
                            yield queue_task.result()
                        else:
                            queue_task.cancel()
                            await asyncio.gather(queue_task, return_exceptions=True)
                        for task in done:
                            if task is queue_task:
                                continue
                            while not live_queue.empty():
                                yield live_queue.get_nowait()
                            for chunk in _finish_dispatch_result(*task.result()):
                                yield chunk
                        pending_tasks = [task for task in pending_tasks if not task.done()]
                finally:
                    for task in pending_tasks:
                        if not task.done():
                            task.cancel()
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                while not live_queue.empty():
                    yield live_queue.get_nowait()

            for node in list(plan.nodes.values()):
                if node.status == "failed" and node.attempt_count >= max_attempts:
                    if self.host._runtime_staffing_trigger(
                        team,
                        node,
                        owner_account_id=envelope.user_id,
                        max_attempts=max_attempts,
                    ) is not None:
                        continue
                    self.host._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"节点连续失败 {node.attempt_count} 次",
                        decision="停止自动重试，保留当前团队并等待用户确认下一步。",
                        suggested_action="可选择补员、改派、缩小任务范围或手动重试。",
                    )
                    self.host._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"节点连续失败 {node.attempt_count} 次，已停止重试，等待 Leader/用户介入。",
                    )
                    progressed = True

            if plan.nodes and all(node.status == "completed" for node in plan.nodes.values()):
                plan.status = "completed"
                break
            if plan.status == "blocked":
                feasibility = self.host._workflow_feasibility(plan)
                if not feasibility["runnable_nodes"]:
                    break
            if any(node.status == "needs_info" for node in plan.nodes.values()):
                break
            if not progressed:
                for node in plan.nodes.values():
                    if node.status == "pending":
                        self.host._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason="依赖未满足或无可执行进展",
                            decision="防止工作流空转，暂停节点并等待用户确认。",
                            suggested_action="请确认是否补充信息、调整依赖或改派成员。",
                        )
                        self.host._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="blocked",
                            result_summary="依赖未满足或无可执行进展，防止工作流空转。",
                        )
                break

        yield ResponseChunk.final(envelope.request_id, self.host._format_workflow_result(plan))
