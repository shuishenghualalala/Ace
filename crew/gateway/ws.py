"""WebSocket 流式对话入口。

发 {query, session_id, mode} 收 ResponseChunk；query 以 /skill-name 开头自动激活 skill；
经 SessionDispatcher 同 session 串行、忙时排队；{action:"stop"|"interrupt"|"steer"|plan_*}
控制运行。出站帧经 outbound 过滤/静默检测，可选鉴权 + 30s 心跳。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from crew.agent.skills import (
    abuild_skill_activation,
    aget_package_members,
    aresolve_package,
    aresolve_skill,
    aresolve_skill_any,
    install_skill,
)
from crew.core.runctx import current_active_skill_packages
from crew.core.envelope import Envelope, TurnOutcome
from crew.channels.channel_sessions import is_channel_session_id
from crew.features import FeatureEventContributionFailedError
from crew.gateway.auth import AuthenticationError, authenticate_websocket
from crew.gateway.send_inbox import DesktopSendInbox, SendExecution, SendKey, SendReceipt, current_send_execution
from crew.gateway.followup_receipts import (
    FollowupKey, FollowupReceipt, FollowupReceiptJournal, followup_decision_hash,
)
from crew.gateway.plan_receipts import PlanReceipt, PlanReceiptJournal, PlanSubmissionKey, plan_submission_hash
from crew.scenarios import resolve_binding as resolve_scenario_binding
from crew.gateway.helpers import (
    WS_PING_INTERVAL_S,
    WS_RECEIVE_TIMEOUT_S,
    connected_platforms,
    resolve_session_id,
    status_frame,
)
from crew.channels.broadcast import stream_and_broadcast
from crew.features.session_context import session_context_from_envelope
from crew.state.logging import get_logger
from crew.core.followup import FollowupAnswerMessage, get_followup_waiter, normalize_followup_answers

log = get_logger("gateway.ws")
_MAX_FORCED_SNAPSHOT_SESSIONS = 16
_REPAIR_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def normalize_team_execution_profile(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    requested_mode = str(raw.get("requested_mode") or "").strip().lower()
    if requested_mode not in {"auto", "fast", "standard", "ai"}:
        return None
    return {
        "requested_mode": requested_mode,
        "profile_source": "user",
    }


def normalize_user_mentions(raw: object) -> list[dict[str, str]] | None:
    """Validate structured Team member mentions before building an Envelope.

    The display text is intentionally not parsed here. The Composer already
    selected a roster item and sends its stable ``member_id`` separately;
    Gateway only validates the transport shape and lets TeamManager validate
    the member against the current Team roster.
    """

    if not isinstance(raw, list):
        return None
    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        if str(item.get("kind") or "").strip() != "team_member":
            return None
        member_id = str(item.get("member_id") or "").strip()
        if not member_id:
            return None
        normalized.append({"kind": "team_member", "member_id": member_id})
    return normalized


def create_ws_router(
    crew,
    dispatcher,
    connections,
    channel_manager,
    *,
    logout_coordinator=None,
    startup_status=None,
    send_inbox: DesktopSendInbox | None = None,
) -> APIRouter:
    router = APIRouter()
    transaction = getattr(crew.session_store, "transaction", None)
    read_query = getattr(crew.session_store, "read_query", None)
    followup_journal = (
        FollowupReceiptJournal(
            transaction, read_query, getattr(crew.session_store, "append_followup_answer_in_transaction", None),
        )
        if callable(transaction) and callable(read_query) else None
    )
    plan_manager = getattr(crew, "plan_manager", None)
    bind_plan_state = getattr(plan_manager, "bind_durable_state", None)
    load_plan_state = getattr(crew.session_store, "load_plan_state", None)
    save_plan_state = getattr(crew.session_store, "save_plan_state", None)
    if callable(bind_plan_state) and callable(load_plan_state) and callable(save_plan_state):
        bind_plan_state(load_plan_state, save_plan_state)
    plan_journal = (
        PlanReceiptJournal(crew.session_store, send_inbox)
        if send_inbox is not None and callable(bind_plan_state) and callable(load_plan_state) else None
    )

    @router.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        """流式对话。并发派发 + 出站过滤/静默检测 + 可选鉴权 + 心跳。"""
        try:
            account = await authenticate_websocket(socket, crew.config)
        except AuthenticationError:
            await socket.close(code=4401, reason="Unauthorized")
            return
        if startup_status is not None:
            status = startup_status()
            if status != "ready":
                # The client has been authenticated. Accept only long enough
                # to deliver the close frame, with no owner claim or routing.
                await socket.accept()
                if status == "starting":
                    await socket.close(code=1013, reason="Gateway starting")
                else:
                    await socket.close(code=1011, reason="Gateway startup failed")
                return
        owner = account.owner_account_id
        if logout_coordinator is not None and logout_coordinator.is_draining(owner):
            await socket.close(code=4423, reason="Logout in progress")
            return
        crew.active_owner.claim(owner)
        if logout_coordinator is not None:
            logout_coordinator.activate_owner(owner)

        await socket.accept()
        connections.register_owner(owner, socket)
        receipt_capability = send_inbox is not None and socket.query_params.get("send_receipts") == "1"
        log.info("WebSocket 已连接")
        registered_sessions: set[str] = set()
        runners: set[asyncio.Task] = set()
        disconnected = asyncio.Event()

        def _session_owned(session_id: str) -> bool:
            belongs = getattr(crew.session_store, "session_belongs_to", None)
            return bool(callable(belongs) and belongs(session_id, owner))

        async def _reject_missing_session(session_id: str) -> None:
            await connections.send_socket(
                socket,
                {
                    "kind": "error",
                    "body": {"message": f"会话不存在: {session_id}"},
                    "is_final": True,
                    "sequence": 0,
                    "session_id": session_id,
                },
            )

        def _register_session(session_id: str) -> None:
            """把当前 socket 订阅到 session，供重连后的后台 chunk 继续投递。"""
            if not disconnected.is_set() and session_id not in registered_sessions:
                connections.register(session_id, socket, owner_account_id=owner)
                registered_sessions.add(session_id)

        async def _send_status(session_id: str, message: str) -> None:
            """向该 session 的活跃连接发送状态帧；当前 socket 先确保已订阅。"""
            _register_session(session_id)
            await connections.push_payload(
                session_id,
                status_frame(session_id, message),
                owner_account_id=owner,
            )

        async def _publish_followup(
            session_id: str, question_id: str, request_id: str, status: str,
            answer: FollowupAnswerMessage | None = None,
        ) -> None:
            _register_session(session_id)
            body = {"question_id": question_id, "status": status, "accepted": status in {"resolved", "cancelled"}, "note": ""}
            if answer is not None:
                body.update(answer_history_id=answer.history_id, answer_text=answer.text)
            if status == "expired":
                body["note"] = "该交互请求已过期或不存在，请重新发起。"
            await connections.push_payload(
                session_id,
                {
                    "kind": "followup_question", "body": body, "is_final": False, "sequence": 0,
                    "request_id": request_id, "session_id": session_id,
                },
                owner_account_id=owner,
            )

        async def _handle_followup(session_id: str, data: dict) -> None:
            action = data["action"]
            query = action == "followup_receipt_query"
            cancel = action == "followup_cancel"
            modern = query or "submission_id" in data
            key = FollowupKey.from_frame(owner, session_id, data) if modern else None
            if not _session_owned(session_id):
                if key is not None:
                    await connections.send_socket(socket, FollowupReceipt(key, "expired", "interaction_expired").frame())
                elif not modern:
                    await _reject_missing_session(session_id)
                return
            answers = None if query or cancel else normalize_followup_answers(data.get("answers"))
            waiter = get_followup_waiter()
            if not query and "submission_id" not in data:
                question = data.get("question_id")
                if not isinstance(question, str) or not _REPAIR_ID.fullmatch(question):
                    return
                request = data.get("request_id")
                request_id = (
                    request if isinstance(request, str) and request
                    else waiter.request_id_for(session_id, question, owner_account_id=owner)
                )
                scope = {"owner_account_id": owner, "request_id": request_id}
                release = asyncio.Event()
                try:
                    valid, display = (True, None) if cancel else (
                        waiter.validated_answer(session_id, question, answers, **scope) if answers else (False, None)
                    )
                    resolved = valid and (
                        waiter.cancel(session_id, question, release=release, **scope) if cancel
                        else waiter.resolve(session_id, question, answers, release=release, **scope)
                    )
                    if resolved:
                        waiter.commit_resolution(session_id, question, answer=display, **scope)
                    await asyncio.wait_for(_publish_followup(
                        session_id, question, request_id or "",
                        ("cancelled" if cancel else "resolved") if resolved else "expired",
                        display if resolved else None,
                    ), timeout=2.0)
                finally:
                    release.set()
                return

            if key is None:
                return
            if followup_journal is None:
                await connections.send_socket(socket, FollowupReceipt(key, "unavailable", "receipt_store_unavailable").frame())
                return
            if not (query or cancel) and answers is None:
                await connections.send_socket(socket, FollowupReceipt(key, "rejected", "invalid_answers").frame())
                return
            digest = None if query else followup_decision_hash(action, answers)
            release = asyncio.Event()
            live_status = ""
            display = None
            claim_acquired = False
            decision_committed = False
            decision_abandoned = False
            try:
                try:
                    receipt = await followup_journal.query(key, digest)
                    if receipt.status == "unknown" and not query:
                        scope = {"owner_account_id": owner, "request_id": key.request_id}
                        pending = waiter.is_waiting(session_id, key.question_id, **scope)
                        if not pending:
                            receipt = FollowupReceipt(key, "expired", "interaction_expired")
                        else:
                            valid, display = (True, None) if cancel else waiter.validated_answer(
                                session_id, key.question_id, answers, **scope,
                            )
                            if not valid:
                                receipt = FollowupReceipt(key, "rejected", "invalid_answers")
                            else:
                                claimed = await followup_journal.claim(key, digest)
                                if claimed is None:
                                    claim_acquired = True
                                    if cancel:
                                        applied = waiter.cancel(session_id, key.question_id, release=release, **scope)
                                    else:
                                        applied = waiter.resolve(
                                            session_id, key.question_id, answers, release=release, **scope,
                                        )
                                    if applied:
                                        receipt = FollowupReceipt(
                                            key, "cancelled" if cancel else "applied",
                                            answer_history_id=display.history_id if display else "",
                                        )
                                    else:
                                        receipt = FollowupReceipt(key, "expired", "interaction_expired")
                                    await asyncio.wait_for(
                                        followup_journal.finish(receipt, answer=display if applied else None), timeout=2.0,
                                    )
                                    if applied and waiter.commit_resolution(
                                        session_id, key.question_id, answer=display, **scope,
                                    ):
                                        decision_committed = True
                                        live_status = "cancelled" if cancel else "resolved"
                                    elif applied:
                                        receipt = FollowupReceipt(key, "uncertain", "interaction_interrupted")
                                    else:
                                        decision_committed = True
                                else:
                                    receipt = claimed
                except Exception:
                    log.exception("追问回执写入失败 request=%s question=%s", key.request_id, key.question_id)
                    receipt = FollowupReceipt(key, "unavailable", "receipt_store_unavailable")
                if claim_acquired and not decision_committed:
                    try:
                        await followup_journal.abandon(key)
                        decision_abandoned = True
                        receipt = FollowupReceipt(key, "uncertain", "interaction_interrupted")
                    except Exception:
                        receipt = FollowupReceipt(key, "unavailable", "receipt_store_unavailable")

                async def publish() -> None:
                    if live_status:
                        await _publish_followup(session_id, key.question_id, key.request_id, live_status, display)
                    await connections.send_socket(socket, receipt.frame())

                with suppress(TimeoutError):
                    await asyncio.wait_for(publish(), timeout=2.0)
            finally:
                if claim_acquired and not decision_committed and not decision_abandoned:
                    with suppress(Exception):
                        await followup_journal.abandon(key)
                release.set()
                followup_journal.release(key)

        async def _handle_plan_submission(session_id: str, data: dict[str, Any]) -> None:
            from crew.agent.plan import PlanSnapshot, PlanTransition, PlanVersionConflict

            key = PlanSubmissionKey.from_frame(owner, session_id, data)
            if key is None:
                await _send_status(session_id, "计划提交信息不完整，请重新打开计划")
                return
            if plan_journal is None or plan_manager is None:
                await connections.send_socket(socket, PlanReceipt(key, "unavailable", "plan_receipts_unavailable").frame())
                return
            if not _session_owned(session_id):
                await connections.send_socket(socket, PlanReceipt(key, "expired", "session_expired").frame())
                return
            _register_session(session_id)
            action = data["action"]
            query = action == "plan_receipt_query"
            receipt = PlanReceipt(key, "unavailable", "plan_unavailable")
            try:
                digest = None if query else plan_submission_hash(data)
                receipt = await plan_journal.query(key, digest)
                if receipt.status == "unknown" and not query:
                    if (
                        ("plan" in data and not isinstance(data["plan"], str))
                        or ("feedback" in data and not isinstance(data["feedback"], str))
                    ):
                        receipt = PlanReceipt(key, "rejected", "invalid_plan_submission")
                    else:
                        transition = await asyncio.to_thread(
                            plan_manager.prepare_transition, session_id, key.expected_version, action,
                            edited_plan=data.get("plan"), owner_account_id=owner,
                        )
                        feedback = str(data.get("feedback") or "").strip()
                        execution = None
                        if action == "plan_approve" or (action == "plan_reject" and feedback):
                            execution = {
                                "session_id": session_id, "request_id": key.request_id,
                                "query": "计划已批准，请按上述计划开始执行。" if action == "plan_approve" else feedback,
                                "mode": data.get("mode") or "agent",
                                "workspace_id": crew.session_store.get_workspace_id(session_id, owner) or "default",
                                "_plan_submission_id": key.submission_id,
                            }
                        receipt = await plan_journal.commit(key, digest, transition, execution)
                        if receipt.status == "applied" and receipt.state is not None:
                            committed = PlanSnapshot.from_body(owner, session_id, receipt.state)
                            if committed is not None:
                                await asyncio.to_thread(
                                    plan_manager.apply_committed_transition,
                                    PlanTransition(transition.base, committed, action),
                                )
                current = await asyncio.to_thread(plan_manager.snapshot, session_id, owner_account_id=owner)
                receipt = receipt.with_state(current.to_body())
                if receipt.status == "applied" and not query:
                    _register_session(session_id)
                    await connections.push_payload(session_id, {
                        "kind": "plan_review", "request_id": "", "session_id": session_id,
                        "is_final": False, "sequence": 0, "body": current.to_body(),
                    }, owner_account_id=owner)
                    with suppress(Exception):
                        crew.mark_plan_review_notification_read(session_id, owner)
            except PlanVersionConflict as exc:
                if receipt.status == "applied":
                    # The decision and inbox row already committed together.
                    # A later external edit supersedes execution, not that receipt.
                    try:
                        current = await asyncio.to_thread(plan_manager.snapshot, session_id, owner_account_id=owner)
                        receipt = receipt.with_state(current.to_body())
                    except Exception:
                        receipt = PlanReceipt(key, "unavailable", "plan_unavailable")
                else:
                    receipt = PlanReceipt(key, "expired", "stale_plan", exc.current.to_body())
            except ValueError as exc:
                receipt = PlanReceipt(key, "rejected", "invalid_plan_submission")
                log.info("计划提交被拒绝 session=%s reason=%s", session_id, exc)
            except Exception:
                log.exception("计划提交失败 session=%s submission=%s", session_id, key.submission_id)
                # A committed decision remains queryable even if materializing its
                # local files or delivering its first receipt failed.
                receipt = PlanReceipt(key, "unavailable", "plan_unavailable")
            try:
                await asyncio.wait_for(connections.send_socket(socket, receipt.frame()), timeout=2.0)
            finally:
                if receipt.status == "applied" and receipt.execution_request_id and not query:
                    queued = await send_inbox.query(SendKey(owner, session_id, receipt.execution_request_id))
                    send_inbox.reserve(queued, _execute_accepted)

        async def _heartbeat() -> None:
            with suppress(asyncio.CancelledError):
                while not disconnected.is_set():
                    await asyncio.sleep(WS_PING_INTERVAL_S)
                    if disconnected.is_set():
                        return
                    try:
                        await connections.send_socket(
                            socket,
                            {"kind": "ping", "body": {}, "is_final": False, "sequence": 0},
                        )
                    except Exception:  # noqa: BLE001 — 心跳为后台任务顶层，任意发送失败须静默终止循环而非逸出到 asyncio
                        return

        async def _run(envelope: Envelope) -> None:
            from crew.channels.channel_sessions import (
                build_outbound_channel_envelope,
                deliver_channel_session_reply,
            )

            is_channel = is_channel_session_id(envelope.session_id)
            if is_channel:
                build_outbound_channel_envelope(crew, envelope, owner=owner)
            final_text = ""
            try:
                # 消费 dispatch 流 + 逐帧广播给该会话的 WS 观察者（含断线回放缓存）；共享实现见 broadcast.py。
                # 广播统一用桌面渲染规则（保留 <thinking> 供前端卡片），故不再按渠道 channel 传 ctx。
                final_text, _ = await stream_and_broadcast(
                    crew, connections, envelope, owner
                )
                if is_channel and final_text.strip():
                    await deliver_channel_session_reply(
                        crew,
                        envelope.session_id,
                        owner,
                        final_text,
                    )
                # Plan 模式：本轮若调了 exit_plan_mode → 推一帧 plan_review。
                # 触发条件改为 take_pending_review（由 submit_review 登记）：
                #   plan 非空 → 推正常审批卡（empty=False）
                #   plan 为空 → 推「计划为空」提示卡（empty=True，无审批按钮）
                # 取代旧的 is_awaiting_approval 唯一条件，确保「调了 exit_plan_mode 就一定有卡片」。
                pm = getattr(crew, "plan_manager", None)
                if pm is not None and not disconnected.is_set():
                    review = pm.take_pending_review(envelope.session_id, owner_account_id=owner)
                    if review is not None:
                        from crew.agent.plan import plan_display_path

                        sid = envelope.session_id
                        await connections.push_payload(
                            sid,
                            {
                                "kind": "plan_review",
                                "body": {
                                    **review,
                                    "plan": review.get("plan") or "",
                                    "plan_file": plan_display_path(sid, owner_account_id=owner),
                                    "empty": bool(review.get("empty")),
                                    "phase": review.get("phase") or "review",
                                    "status": review.get("status") or ("empty" if review.get("empty") else "pending"),
                                },
                                "is_final": False,
                                "sequence": 0,
                                "request_id": envelope.request_id,
                                "session_id": sid,
                            },
                            owner_account_id=owner,
                        )
                        # 通知中心待办：仅可审批（非 empty）的 review 发通知；
                        # 通知失败不得影响卡片推送主流程。
                        try:
                            crew.publish_plan_review_notification(sid, owner, review)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("plan 待办通知发布失败 session=%s: %s", sid, exc)

                # Feature 事件由各自 Bundle 注册的中立 Source 生产。断开时不 dispatch，
                # 让待处理事件留在其 Feature 的会话状态中，供后续回合或重连消费。
                event_contributors = getattr(crew, "event_contributors", None)
                if not disconnected.is_set() and event_contributors is not None:
                    async def push_feature_event(event) -> None:
                        await connections.push_payload(
                            envelope.session_id,
                            {
                                "kind": "feature_event",
                                "body": event.as_body(),
                                "is_final": False,
                                "sequence": 0,
                                "request_id": envelope.request_id,
                                "session_id": envelope.session_id,
                            },
                            owner_account_id=owner,
                        )

                    try:
                        event_report = await event_contributors.dispatch(
                            envelope,
                            push_feature_event,
                        )
                    except FeatureEventContributionFailedError as error:
                        log.warning(
                            "Feature event source failed closed session=%s contributor=%s generation=%s: %s",
                            envelope.session_id,
                            error.contributor_id,
                            error.generation.key,
                            error,
                        )
                    else:
                        for failure in event_report.failures:
                            log.warning(
                                "Feature event source degraded session=%s contributor=%s generation=%s: %s",
                                envelope.session_id,
                                failure.contributor_id,
                                failure.generation.key,
                                failure.message,
                            )
            except Exception:  # noqa: BLE001 — WS runner 为并发派发的后台 task 顶层，dispatch 已内部回报错帧，此处仅兜底记录
                log.exception("WS runner 异常 session=%s", envelope.session_id)
                try:
                    await connections.push_payload(
                        envelope.session_id,
                        {
                            "kind": "error",
                            "body": {"message": "服务内部异常，请稍后重试", "category": "unknown"},
                            "is_final": True,
                            "sequence": 0,
                            "request_id": envelope.request_id,
                            "session_id": envelope.session_id,
                        },
                        owner_account_id=owner,
                    )
                except Exception:  # noqa: BLE001 — 兜底推送失败不得逸出
                    log.exception("WS runner 兜底 error chunk 推送失败 session=%s", envelope.session_id)

        def _spawn(env: Envelope) -> None:
            """并发派发：接收循环立即继续读下一条，实现忙时排队。"""
            task = asyncio.create_task(_run(env))
            runners.add(task)
            task.add_done_callback(runners.discard)

        def _request_id_kw(data: dict) -> dict[str, str]:
            request_id = str(data.get("request_id") or "").strip()
            return {"request_id": request_id} if request_id else {}

        async def _handle_chat(
            data: dict[str, Any], session_id: str, execution: SendExecution | None = None,
            *, approved_plan: dict[str, Any] | None = None, revision_plan: dict[str, Any] | None = None,
        ) -> None:
            attachments = data.get("attachments", [])
            if not isinstance(attachments, list):
                attachments = []
            query = data.get("query", "")
            if not isinstance(query, str):
                query = str(query or "")
            # 空 query 且无附件：禁止起一轮。否则会把 content 为空的 user 写入历史，
            # 后续 OpenAI 兼容网关（如 MiniMax）对 messages 校验 400。
            if not query.strip() and not attachments:
                if session_id and _session_owned(session_id):
                    await _send_status(session_id, "消息内容为空，请输入后再发送")
                return
            if execution is not None and (
                data.get("sub_scenario") or data.get("plan_active")
                or query.startswith("/") or is_channel_session_id(session_id)
                or not _session_owned(session_id)
            ):
                if not await execution.begin():
                    return
                if dispatcher.closed or dispatcher.owner_is_blocked(owner):
                    await execution.defer()
                    return
            external_team_id = str(data.get("external_team_id") or "").strip()
            raw_team_profile = data.get("team_execution_profile")
            team_execution_profile = normalize_team_execution_profile(raw_team_profile)

            # 场景化推荐：sub_scenario 反查绑定 → 懒装 skill / 注入提示词。
            scenario_meta: str | None = None
            sub_scenario = str(data.get("sub_scenario") or "").strip()
            if sub_scenario:
                binding = resolve_scenario_binding(sub_scenario)
                if binding:
                    # a) 懒加载安装 optional skills（仅装尚未可用的）
                    for slug in binding.get("skills") or []:
                        if await aresolve_skill(slug) is None:
                            try:
                                # 安装是重 IO（copytree + 审计），移出事件循环
                                await asyncio.to_thread(
                                    install_skill,
                                    slug,
                                    operator_account_id=owner,
                                    source="scenario-auto-install",
                                )
                            except Exception:  # noqa: BLE001
                                log.warning("场景 skill 自动安装失败: %s", slug)
                    # a2) 场景绑定的 skill 若属于某个 package，自动展开该 package，
                    #     避免模型因 progressive disclosure 看不到内部 skills 而多轮交互。
                    active_packages = set(current_active_skill_packages.get())
                    for slug in binding.get("skills") or []:
                        info = await aresolve_skill_any(slug)
                        pkg = info.get("package") if info else None
                        if pkg:
                            active_packages.add(pkg)
                    if active_packages:
                        current_active_skill_packages.set(active_packages)
                    # b) 注入提示词（手写文案，原样透传）
                    if binding.get("inject"):
                        scenario_meta = binding["inject"]
                    # c) 可选运行模式（不覆盖前端显式 mode）
                    if binding.get("mode") and not data.get("mode"):
                        data["mode"] = binding["mode"]

            # 注册：该 WS 正在服务 session_id（首次则登记，供后台任务推送）
            ensure = getattr(crew.session_store, "ensure_session", None)
            if not _session_owned(session_id):
                if callable(ensure):
                    ensure(
                        session_id,
                        workspace_id=str(data.get("workspace_id", "default")),
                        title="",
                        owner_account_id=owner,
                    )
                if not _session_owned(session_id):
                    await _reject_missing_session(session_id)
                    return
            _register_session(session_id)

            # 新会话在首条消息发送前只存在于前端；Plan 选择随首条消息带入，
            # 必须等 ensure_session 后再进入 Plan，避免空会话点击 Plan 报“会话不存在”。
            if data.get("plan_active"):
                pm = getattr(crew, "plan_manager", None)
                if pm is not None:
                    pm.enter(session_id, owner_account_id=owner)

            # 斜杠命令：直接走插件命令分发，不走 Agent 回合。
            if query.startswith("/"):
                command_result = await crew.plugins.run_plugin_command(
                    query,
                    session_id=session_id,
                    owner_account_id=owner,
                    channel="web",
                    workspace_id=str(data.get("workspace_id", "default")),
                )
                if command_result is not None:
                    if execution is not None:
                        await execution.finish("completed")
                    await connections.push_payload(
                        session_id,
                        {
                            "kind": "final",
                            "body": {"text": command_result},
                            "is_final": True,
                            "status": "succeeded",
                            "sequence": 1,
                            "request_id": str(data.get("request_id") or ""),
                            "session_id": session_id,
                        },
                        owner_account_id=owner,
                    )
                    return

            # Skill 调度：/skill-name [补充指令] 或 /package-name 或 /package-name/skill-name
            # 保留原始输入作为用户可见消息，skill/package 展开内容作为 is_meta 消息
            skill_meta: str | None = None
            active_skills: list[dict] = []
            active_packages_added: list[str] = []
            if query.startswith("/"):
                command, _, user_instruction = query[1:].partition(" ")

                # 1. 先尝试解析为 skill（支持 /package/skill、/skill、alias、中文名）
                skill_key = await aresolve_skill(command)
                if skill_key:
                    activation = await abuild_skill_activation(
                        skill_key,
                        user_instruction,
                        session_id,
                    )
                    if activation is not None:
                        skill_meta = activation.instruction
                        active_skills.append(activation.to_dict())
                else:
                    # 2. 尝试解析为 package 并展开
                    pkg = await aresolve_package(command)
                    if pkg is not None:
                        pkg_slug = pkg["slug"]
                        try:
                            active = set(current_active_skill_packages.get())
                        except Exception:
                            active = set()
                        if pkg_slug not in active:
                            active.add(pkg_slug)
                            current_active_skill_packages.set(active)
                            active_packages_added.append(pkg_slug)

                        members = await aget_package_members(pkg_slug)
                        lines = [
                            f'[IMPORTANT: 用户激活了 "{pkg_slug}" skill package，以下 skills 已展开并可用。]',
                            "",
                        ]
                        for m in members:
                            desc = m.get("description_zh") or m.get("description") or ""
                            lines.append(f"- /{m['slug']}: {desc}")
                        if user_instruction.strip():
                            lines += ["", f"用户补充指令：{user_instruction.strip()}"]
                        skill_meta = "\n".join(lines)

            # 场景注入提示词：与手输 /skill 互斥时拼接
            if scenario_meta:
                skill_meta = f"{skill_meta}\n\n{scenario_meta}" if skill_meta else scenario_meta

            mode = data.get("mode") or "agent"

            envelope = Envelope.of(
                query,
                session_id=session_id,
                channel="web",
                **_request_id_kw(data),
                workspace_id=data.get("workspace_id", "default"),
                user_id=owner,
                mode=mode,
            )
            if approved_plan is not None:
                envelope.params["approved_plan"] = approved_plan
            if revision_plan is not None:
                envelope.params["plan_revision"] = revision_plan
            log.info(
                "[PERF] gateway_request_received request_id=%s session=%s query_len=%d mode=%s",
                envelope.request_id,
                envelope.session_id,
                len(query),
                envelope.mode,
            )
            if external_team_id:
                envelope.params["external_team_id"] = external_team_id
            raw_user_mentions = data.get("user_mentions")
            if raw_user_mentions is not None:
                user_mentions = normalize_user_mentions(raw_user_mentions)
                if user_mentions is None:
                    await _send_status(
                        session_id,
                        "用户 Agent mention 格式无效，请重新从候选列表中选择成员",
                    )
                    return
                envelope.params["user_mentions"] = user_mentions
            intent = str(data.get("intent") or "").strip()
            if intent:
                envelope.params["intent"] = intent
            client_intent = str(data.get("client_intent") or "").strip()
            if client_intent == "revision":
                envelope.params["client_intent"] = client_intent
            # 沉淀开关状态透传（暂不触发实际编译）
            if data.get("wiki_ingest"):
                envelope.params["wiki_ingest"] = True
            # 专用 Wiki Agent 的知识库透传。客户端未显式携带
            # kb_id 时，从持久化会话配置恢复，不依赖临时模式状态。
            wiki_kb_id = str(data.get("wiki_kb_id") or data.get("kb_id") or "").strip()
            if not wiki_kb_id:
                get_agent_config = getattr(crew.session_store, "get_agent_config", None)
                if callable(get_agent_config):
                    agent_config = get_agent_config(session_id, owner_account_id=owner) or {}
                    if agent_config.get("wiki_agent_session"):
                        wiki_kb_id = str(agent_config.get("wiki_kb_id") or "default").strip()
            if wiki_kb_id:
                envelope.params["wiki_kb_id"] = wiki_kb_id
            external_wiki_read_kb_id = str(data.get("external_wiki_read_kb_id") or "").strip()
            if mode == "team" and external_wiki_read_kb_id:
                envelope.params["external_wiki_read_kb_id"] = external_wiki_read_kb_id
            if mode == "team" and data.get("collaboration_contract") == "project_review_v1":
                envelope.params["collaboration_contract"] = "project_review_v1"
            wiki_confirmation_id = str(data.get("wiki_confirmation_id") or "").strip()
            if wiki_confirmation_id:
                envelope.params["wiki_confirmation_id"] = wiki_confirmation_id
            if team_execution_profile is not None:
                envelope.params["team_execution_profile"] = team_execution_profile
            if data.get("team_confirm_execution_mode"):
                envelope.params["team_confirm_execution_mode"] = True
            envelope.attachments = attachments
            envelope.params["session_context"] = session_context_from_envelope(
                envelope, connected_platforms(channel_manager, owner)
            )
            if active_packages_added:
                envelope.params["active_skill_packages"] = active_packages_added
            if skill_meta:
                envelope.params["skill_meta"] = skill_meta
            if active_skills:
                envelope.params["active_skills"] = active_skills

            if execution is None:
                _spawn(envelope)
            else:
                token = current_send_execution.set(execution)
                try:
                    await _run(envelope)
                finally:
                    current_send_execution.reset(token)

        async def _execute_accepted(receipt: SendReceipt) -> None:
            if dispatcher.closed or dispatcher.owner_is_blocked(owner) or (
                logout_coordinator is not None and not logout_coordinator.allows_work(owner)
            ):
                return
            assert send_inbox is not None and receipt.payload is not None
            execution = SendExecution(send_inbox, receipt.key)

            async def fail_plan(code: str, message: str) -> None:
                outcome = TurnOutcome("failed", reason=code, message=message)
                persist = getattr(crew.session_store, "record_turn_outcome", None)
                if callable(persist):
                    await asyncio.to_thread(
                        persist, receipt.key.session_id, receipt.key.request_id, outcome.to_body(),
                        owner_account_id=owner, last_status="failed", error=message,
                        settle_receipt=lambda conn: execution.finish_in_transaction(conn, "failed", code, outcome),
                    )
                    execution.finished = True
                else:
                    await execution.finish("failed", code, outcome=outcome)
                await connections.push_payload(receipt.key.session_id, {
                    "kind": "error", "is_final": True, "sequence": 0,
                    "session_id": receipt.key.session_id, "request_id": receipt.key.request_id,
                    "body": {"message": message, "code": code, "outcome": outcome.to_body()},
                }, owner_account_id=owner)

            try:
                approved_plan = None
                revision_plan = None
                if "_plan_submission_id" in receipt.payload:
                    if plan_journal is None or plan_manager is None:
                        await fail_plan("plan_unavailable", "计划暂时无法恢复，请核对当前计划后重试。")
                        return
                    committed = await plan_journal.execution_state(owner, receipt.key.session_id, receipt.key.request_id)
                    try:
                        current = await asyncio.to_thread(plan_manager.snapshot, receipt.key.session_id, owner_account_id=owner)
                    except Exception:
                        log.exception("恢复已批准计划失败 session=%s", receipt.key.session_id)
                        await fail_plan("plan_unavailable", "计划暂时无法恢复，请核对当前计划后重试。")
                        return
                    if committed is None or current.version != committed.get("version"):
                        await fail_plan("plan_superseded", "计划已更新，此次执行已取消，请审阅当前计划。")
                        return
                    if committed.get("phase") == "approved":
                        approved_plan = committed
                    elif committed.get("phase") == "revising":
                        revision_plan = committed
                await _handle_chat(
                    receipt.payload, receipt.key.session_id, execution,
                    approved_plan=approved_plan, revision_plan=revision_plan,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Desktop send failed before or during dispatch request=%s", receipt.key.request_id)
                if not dispatcher.closed and not execution.started:
                    await execution.finish("failed", "turn_failed")
            finally:
                if execution.started and not execution.finished and not dispatcher.closed:
                    try:
                        await execution.finish("uncertain", "dispatch_outcome_unverified")
                    except Exception:
                        log.exception("Desktop send outcome persistence failed request=%s", receipt.key.request_id)

        async def _handle_receipt(data: dict[str, Any]) -> bool:
            action = data.get("action")
            query_receipt = action == "send_receipt_query"
            cancel_receipt = action == "send_receipt_cancel"
            if not (query_receipt or cancel_receipt or (not action and data.get("send_receipt") is True)):
                return False
            key = SendKey.from_frame(owner, data)
            if key is None:
                sid, rid = data.get("session_id"), data.get("request_id")
                if isinstance(sid, str) and len(sid) <= 256 and isinstance(rid, str) and len(rid) <= 128:
                    await connections.send_socket(socket, SendReceipt(
                        SendKey(owner, sid, rid), "rejected", "invalid_request_id",
                    ).frame())
                return True
            if send_inbox is None:
                receipt = SendReceipt(key, "unavailable", "receipt_store_unavailable")
            else:
                try:
                    if query_receipt:
                        receipt = await send_inbox.query(key)
                    elif cancel_receipt:
                        receipt = await send_inbox.cancel(key)
                    else:
                        raw_query = data.get("query", "")
                        attachments = data.get("attachments", [])
                        mentions = data.get("user_mentions")
                        if (
                            not isinstance(raw_query, str) or not isinstance(attachments, list)
                            or any(not isinstance(item, dict) for item in attachments)
                            or not (raw_query.strip() or attachments)
                            or (mentions is not None and normalize_user_mentions(mentions) is None)
                        ):
                            receipt = SendReceipt(key, "rejected", "invalid_payload")
                        elif dispatcher.closed or dispatcher.owner_is_blocked(owner):
                            receipt = await send_inbox.query(key)
                            if receipt.status == "unknown":
                                receipt = SendReceipt(key, "unavailable", "gateway_shutting_down")
                        else:
                            receipt = await send_inbox.accept(key, data)
                except Exception:
                    log.exception("Desktop send receipt store unavailable request=%s", key.request_id)
                    receipt = SendReceipt(key, "unavailable", "receipt_store_unavailable")
            if receipt.status in {"accepted", "running"}:
                _register_session(key.session_id)
            try:
                # Preserve receipt-before-new-execution ordering without
                # leaving committed work stranded if the original socket dies.
                await asyncio.wait_for(connections.send_socket(socket, receipt.frame()), timeout=2.0)
            finally:
                if send_inbox is not None and not cancel_receipt and not dispatcher.closed:
                    send_inbox.reserve(receipt, _execute_accepted)
            return True

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            if receipt_capability:
                await connections.send_socket(socket, {
                    "kind": "send_receipt_capability",
                    "body": {
                        "version": 1, "store_id": send_inbox.store_id, "followup_receipts": 1,
                        **({"plan_receipts": 1} if plan_journal is not None else {}),
                    },
                    "sequence": 0,
                    "is_final": False,
                })
            while True:
                try:
                    data = await asyncio.wait_for(socket.receive_json(), timeout=WS_RECEIVE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.info("WebSocket 心跳超时，断开连接")
                    break
                except json.JSONDecodeError:
                    log.warning("收到非法 JSON 帧，忽略")
                    continue
                except WebSocketDisconnect:
                    break
                except RuntimeError as exc:
                    # 连接在未 accept/已断开状态下被读取，优雅退出
                    log.warning("WebSocket receive RuntimeError: %s", exc)
                    break
                except Exception:
                    log.exception("WebSocket receive 异常")
                    break

                if (
                    logout_coordinator is not None
                    and not logout_coordinator.allows_work(owner)
                ):
                    await socket.close(code=4401, reason="Login required")
                    break

                if not isinstance(data, dict) or data.get("kind") == "pong":
                    continue
                if await _handle_receipt(data):
                    continue

                session_id = resolve_session_id(data, platform="web")

                if data.get("action") in {"subscribe", "resume"}:
                    sessions = data.get("sessions")
                    if not isinstance(sessions, list):
                        sessions = [session_id]
                    raw_seqs = data.get("last_gateway_sequences")
                    raw_cursors = data.get("gateway_cursors")
                    cursors = raw_cursors if isinstance(raw_cursors, dict) else {}
                    supports_snapshots = data.get("gateway_snapshots") is True
                    snapshot_request = getattr(dispatcher, "snapshot_request_id", None)
                    repair_id = data.get("repair_id")
                    raw_forced = data.get("force_snapshot_sessions")
                    forced_sessions = set()
                    if (
                        supports_snapshots and isinstance(repair_id, str) and _REPAIR_ID.fullmatch(repair_id)
                        and isinstance(raw_forced, list)
                    ):
                        forced_sessions = {
                            raw_sid.strip() for raw_sid in raw_forced[:_MAX_FORCED_SNAPSHOT_SESSIONS]
                            if isinstance(raw_sid, str) and raw_sid.strip()
                        }
                    last_seqs: dict[str, int] = {}
                    if isinstance(raw_seqs, dict):
                        for k, v in raw_seqs.items():
                            sid_key = str(k or "").strip()
                            if not sid_key:
                                continue
                            try:
                                last_seqs[sid_key] = max(0, int(v))
                            except (TypeError, ValueError, OverflowError):
                                continue

                    def _replay_filter(payload: dict) -> bool:
                        """过滤已失效的临时帧：status 是瞬时态，回放只会把过期状态
                        （如"排队中"）重新贴到前端；追问只回放仍在等待中的。"""
                        kind = payload.get("kind")
                        if kind == "status":
                            return False
                        if kind != "followup_question":
                            return True
                        body = payload.get("body") or {}
                        qid = str(body.get("question_id") or "").strip()
                        sid = str(payload.get("session_id") or "").strip()
                        if not qid or not sid:
                            return True
                        return get_followup_waiter().is_waiting(sid, qid, owner_account_id=owner)

                    for raw_sid in sessions:
                        sid = str(raw_sid or "").strip()
                        if not sid:
                            continue
                        # 允许订阅任意 session_id：后台推送（如 Wiki ingest 进度）可能指向前端
                        # 生成但尚未落库的会话。注册按 (owner, session_id) 索引，不会跨账号泄露。
                        if _session_owned(sid):
                            after = last_seqs.get(sid, 0)
                            cursor_arg = {"resume_cursor": cursors[sid]} if sid in cursors else {}
                            if supports_snapshots and callable(snapshot_request):
                                cursor_arg["snapshot_request_id"] = lambda session=sid: snapshot_request(session, owner)
                            if sid in forced_sessions:
                                forced_sessions.remove(sid)
                                cursor_arg["repair_id"] = repair_id
                            if not await connections.replay(
                                sid,
                                socket,
                                after_gateway_sequence=after,
                                filter_fn=_replay_filter,
                                owner_account_id=owner,
                                register_for_live=True,
                                **cursor_arg,
                            ):
                                return
                            registered_sessions.add(sid)
                        else:
                            _register_session(sid)
                    continue

                if data.get("action") == "stop":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    stopped_preparation = False
                    if send_inbox is not None:
                        try:
                            await send_inbox.cancel_pending(owner, session_id)
                        except Exception:
                            log.exception("Desktop pending sends could not be cancelled session=%s", session_id)
                        stopped_preparation = send_inbox.stop_preparation(
                            owner, session_id, dispatcher.active_tasks_snapshot(),
                        )
                    stopped = await dispatcher.stop(session_id, owner_account_id=owner)
                    if not stopped and not stopped_preparation:
                        await _send_status(session_id, "当前没有正在运行的回复")
                    continue

                if data.get("action") == "interrupt":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    interrupted = dispatcher.interrupt(session_id, "被用户中断", owner_account_id=owner)
                    if not interrupted:
                        await _send_status(session_id, "当前没有正在运行的回复")
                    continue

                if data.get("action") == "steer":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    steer_text = str(data.get("text", "")).strip()
                    steered = dispatcher.steer(session_id, steer_text, owner_account_id=owner)
                    msg = "补充指令已注入" if steered else "当前没有正在运行的回复，无法注入"
                    await _send_status(session_id, msg)
                    continue

                if data.get("action") == "background":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    task_id = await dispatcher.background(session_id, owner_account_id=owner)
                    msg = f"当前任务已转后台：{task_id}" if task_id else "当前没有可后台化的运行任务"
                    await _send_status(session_id, msg)
                    continue

                if data.get("action") in {"followup_answer", "followup_cancel", "followup_receipt_query"}:
                    await _handle_followup(session_id, data)
                    continue

                # ---- Plan 模式控制 ----
                if data.get("action") == "plan_receipt_query" or (
                    data.get("action") in {"plan_approve", "plan_update", "plan_reject", "plan_reject_and_exit"}
                    and ("submission_id" in data or "expected_version" in data)
                ):
                    await _handle_plan_submission(session_id, data)
                    continue
                if data.get("action") in (
                    "plan_enter",
                    "plan_approve",
                    "plan_reject",
                    "plan_reject_and_exit",
                    "plan_exit",
                    "plan_update",
                ):
                    pm = getattr(crew, "plan_manager", None)
                    act = data["action"]
                    if pm is None:
                        await _send_status(session_id, "Plan 模式不可用")
                        continue
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue

                    def _mark_plan_notification_read() -> None:
                        """计划被批准/拒绝/退出后，对应待办通知自动已读；失败不阻断控制流。"""
                        try:
                            crew.mark_plan_review_notification_read(session_id, owner)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("plan 通知自动已读失败 session=%s: %s", session_id, exc)

                    if act == "plan_enter":
                        pm.enter(session_id, owner_account_id=owner)
                        await _send_status(
                            session_id, "已进入 Plan 模式（只读探索→写计划→审批后执行）"
                        )
                    elif act == "plan_update":
                        # 看板手改：写回 plan 文件，并推一帧 plan_review 刷新审阅面。
                        if not pm.is_active(session_id, owner_account_id=owner):
                            await _send_status(session_id, "当前不在 Plan 模式，无法更新计划")
                            continue
                        raw_plan = data.get("plan", "")
                        plan_text = raw_plan if isinstance(raw_plan, str) else str(raw_plan or "")
                        try:
                            review = pm.update_plan(
                                session_id, plan_text, owner_account_id=owner
                            )
                        except ValueError:
                            await _send_status(session_id, "当前不在 Plan 模式，无法更新计划")
                            continue
                        from crew.agent.plan import plan_display_path

                        await _send_status(session_id, "计划已更新")
                        await connections.push_payload(
                            session_id,
                            {
                                "kind": "plan_review",
                                "body": {
                                    **review,
                                    "plan": review.get("plan") or "",
                                    "plan_file": plan_display_path(
                                        session_id, owner_account_id=owner
                                    ),
                                    "empty": bool(review.get("empty")),
                                    "phase": review.get("phase") or "review",
                                    "status": review.get("status")
                                    or ("empty" if review.get("empty") else "pending"),
                                },
                                "is_final": False,
                                "sequence": 0,
                                "request_id": "",
                                "session_id": session_id,
                            },
                            owner_account_id=owner,
                        )
                    elif act == "plan_reject":
                        pm.reject(session_id, owner_account_id=owner)
                        _mark_plan_notification_read()
                        await _send_status(
                            session_id, "已保留 Plan 模式，请继续完善计划"
                        )
                    elif act == "plan_reject_and_exit":
                        pm.reject_and_exit(session_id, owner_account_id=owner)
                        _mark_plan_notification_read()
                        await _send_status(session_id, "已拒绝计划并退出 Plan 模式")
                    elif act == "plan_exit":
                        pm.exit(session_id, owner_account_id=owner)
                        _mark_plan_notification_read()
                        await _send_status(session_id, "已退出 Plan 模式")
                    else:  # plan_approve → 退出只读并自动起一轮执行
                        # 批准前若客户端附带 plan 正文，先落盘再批准（看板「手改后批准」原子路径）。
                        raw_plan = data.get("plan")
                        if isinstance(raw_plan, str):
                            try:
                                pm.update_plan(session_id, raw_plan, owner_account_id=owner)
                            except ValueError:
                                await _send_status(session_id, "当前不在 Plan 模式，无法批准")
                                continue
                        if not pm.is_awaiting_approval(session_id, owner_account_id=owner):
                            # 手改清空后可能已退出 review：拒绝空批准。
                            from crew.agent.plan import read_plan as _read_plan

                            if not _read_plan(session_id, owner_account_id=owner):
                                await _send_status(session_id, "计划为空，请先完善计划再批准")
                                continue
                            # revising/active 且有正文：允许直接批准（看板手改后未再走 exit_plan_mode）。
                            if not pm.is_active(session_id, owner_account_id=owner):
                                await _send_status(session_id, "当前不在 Plan 模式，无法批准")
                                continue
                            pm.request_approval(session_id, owner_account_id=owner)
                        pm.approve(session_id, owner_account_id=owner)
                        _mark_plan_notification_read()
                        _register_session(session_id)
                        approval_text = "计划已批准，请按上述计划开始执行。"
                        exec_env = Envelope.of(
                            approval_text,
                            session_id=session_id, channel="web",
                            **_request_id_kw(data),
                            workspace_id=data.get("workspace_id", "default"),
                            user_id=owner,
                            mode=data.get("mode", "agent"),
                        )
                        _spawn(exec_env)
                    continue

                await _handle_chat(data, session_id)
        except WebSocketDisconnect:
            log.info("WebSocket 断开")
        except RuntimeError as exc:
            log.warning("WebSocket handler RuntimeError: %s", exc)
        except Exception:
            log.exception("WebSocket handler 异常")
        finally:
            disconnected.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            connections.unregister_all(socket, registered_sessions, owner_account_id=owner)

    return router
