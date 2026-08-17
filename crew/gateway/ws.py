"""WebSocket 流式对话入口。

发 {query, session_id, mode} 收 ResponseChunk；query 以 /skill-name 开头自动激活 skill；
经 SessionDispatcher 同 session 串行、忙时排队；{action:"stop"|"interrupt"|"steer"|plan_*}
控制运行。出站帧经 outbound 过滤/静默检测，可选鉴权 + 30s 心跳。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from crew.agent.skills import (
    _parse_frontmatter,
    build_skill_activation,
    get_skills,
    get_package_members,
    install_skill,
    resolve_package,
    resolve_skill,
    resolve_skill_any,
)
from crew.core.runctx import current_active_skill_packages
from crew.core.envelope import Envelope
from crew.gateway.auth import AuthenticationError, authenticate_websocket
from crew.scenarios import resolve_binding as resolve_scenario_binding
from crew.gateway.helpers import (
    WS_PING_INTERVAL_S,
    WS_RECEIVE_TIMEOUT_S,
    connected_platforms,
    resolve_session_id,
    status_frame,
)
from crew.gateway.broadcast import stream_and_broadcast
from crew.gateway.session_context import session_context_from_envelope
from crew.state.logging import get_logger
from crew.state.active_owner import ActiveOwnerConflict
from crew.core.followup import get_followup_waiter

log = get_logger("gateway.ws")


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


def _apply_browser_skill_policy(crew, skill_key: str, owner: str, session_id: str) -> None:
    """技能激活时校验它声明的浏览器策略形状。

    **不再把策略变成运行期的动作白名单。** 授权来自 V2/V3 record-replay 的
    不可变 plan 与必须精确等于 plan 的 capabilities 声明——那是按这一次录制的
    实际动作精确推导出来的，比"这个会话只读"这种粗粒度档位准确得多，也不会
    在正常流程上产生任何阻碍。

    保留这个函数是为了**格式校验**：一份声明了 `browser_policy` 却写坏了的技能
    应该在激活时就被发现，而不是等回放到一半才炸。校验失败只记日志，不阻断激活。
    """
    try:
        info = get_skills().get(skill_key)
        if not info:
            return
        frontmatter, _ = _parse_frontmatter(
            Path(info["skill_dir"], "SKILL.md").read_text("utf-8")
        )
        metadata = frontmatter.get("metadata")
        policy = metadata.get("browser_policy") if isinstance(metadata, dict) else None
        if not isinstance(policy, dict):
            return
        generated_by = (
            str(metadata.get("generated_by") or "")
            if isinstance(metadata, dict)
            else ""
        )
        # **标记必须与编译器实际写入的完全一致。**
        # 曾经这里写的是 "crew.browser.record-replay"（点号），而编译器写入的是
        # "crew.browser-record-replay"（连字符，见 compile_tool.py 与
        # skills.py 的 validate_generated_skill）——于是整个函数对任何真实技能
        # 都在第一关 return，看似有校验实际没有。
        if generated_by != "crew.browser-record-replay":
            return
        capabilities = policy.get("capabilities")
        if (
            policy.get("schema_version") != "crew.browser.policy.v2"
            or not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            log.warning(
                "录制技能的 browser_policy 格式无效：skill=%s",
                skill_key,
            )
    except Exception:
        # 形状校验是诊断，不是闸门：解析失败不该让技能装不上或跑不起来。
        log.debug("跳过 browser_policy 形状校验：skill=%s", skill_key, exc_info=True)


def create_ws_router(
    crew,
    dispatcher,
    connections,
    channel_manager,
    *,
    logout_coordinator=None,
    startup_waiter=None,
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        """流式对话。并发派发 + 出站过滤/静默检测 + 可选鉴权 + 心跳。"""
        try:
            account = await authenticate_websocket(socket, crew.config)
        except AuthenticationError:
            await socket.close(code=4401, reason="Unauthorized")
            return
        if startup_waiter is not None and not await startup_waiter():
            await socket.close(code=1013, reason="Gateway startup failed")
            return
        owner = account.owner_account_id
        if logout_coordinator is not None and logout_coordinator.is_draining():
            await socket.close(code=4423, reason="Logout in progress")
            return
        try:
            crew.active_owner.claim(owner)
        except ActiveOwnerConflict:
            await socket.close(code=4423, reason="Active owner conflict")
            return
        if logout_coordinator is not None:
            logout_coordinator.activate_owner(owner)

        await socket.accept()
        connections.register_owner(owner, socket)
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
            if session_id not in registered_sessions:
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
            from crew.gateway.channel_sessions import (
                build_outbound_channel_envelope,
                deliver_channel_session_reply,
                is_channel_session_id,
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

                # Wiki Agent：本轮若有待展示卡片 → 推 wiki_cards 帧
                wm = getattr(crew, "wiki_manager", None)
                if wm is not None and not disconnected.is_set():
                    cards = wm.take_pending_cards(envelope.session_id, owner_account_id=owner)
                    if cards:
                        await connections.push_payload(
                            envelope.session_id,
                            {
                                "kind": "wiki_cards",
                                "body": {"pages": cards},
                                "is_final": False,
                                "sequence": 0,
                                "request_id": envelope.request_id,
                                "session_id": envelope.session_id,
                            },
                            owner_account_id=owner,
                        )
                    changes = wm.take_pending_changes(envelope.session_id, owner_account_id=owner)
                    if changes:
                        await connections.push_payload(
                            envelope.session_id,
                            {
                                "kind": "wiki_changed",
                                "body": {"changes": changes},
                                "is_final": False,
                                "sequence": 0,
                                "request_id": envelope.request_id,
                                "session_id": envelope.session_id,
                            },
                            owner_account_id=owner,
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

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
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

                if data.get("kind") == "pong":
                    continue

                session_id = resolve_session_id(data, platform="web")

                if data.get("action") in {"subscribe", "resume"}:
                    sessions = data.get("sessions")
                    if not isinstance(sessions, list):
                        sessions = [session_id]
                    raw_seqs = data.get("last_gateway_sequences")
                    last_seqs: dict[str, int] = {}
                    if isinstance(raw_seqs, dict):
                        for k, v in raw_seqs.items():
                            sid_key = str(k or "").strip()
                            if not sid_key:
                                continue
                            try:
                                last_seqs[sid_key] = max(0, int(v))
                            except (TypeError, ValueError):
                                continue

                    def _replay_filter(payload: dict) -> bool:
                        """过滤已失效的临时交互帧：只回放仍在等待中的追问。"""
                        if payload.get("kind") != "followup_question":
                            return True
                        body = payload.get("body") or {}
                        qid = str(body.get("question_id") or "").strip()
                        sid = str(payload.get("session_id") or "").strip()
                        if not qid or not sid:
                            return True
                        return get_followup_waiter().is_waiting(sid, qid)

                    for raw_sid in sessions:
                        sid = str(raw_sid or "").strip()
                        if not sid:
                            continue
                        # 允许订阅任意 session_id：后台推送（如 Wiki ingest 进度）可能指向前端
                        # 生成但尚未落库的会话。注册按 (owner, session_id) 索引，不会跨账号泄露。
                        _register_session(sid)
                        if _session_owned(sid):
                            after = last_seqs.get(sid, 0)
                            await connections.replay(
                                sid,
                                socket,
                                after_gateway_sequence=after,
                                filter_fn=_replay_filter,
                                owner_account_id=owner,
                            )
                    continue

                if data.get("action") == "stop":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    stopped = dispatcher.stop(session_id, owner_account_id=owner)
                    if not stopped:
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
                    task_id = dispatcher.background(session_id, owner_account_id=owner)
                    msg = f"当前任务已转后台：{task_id}" if task_id else "当前没有可后台化的运行任务"
                    await _send_status(session_id, msg)
                    continue

                # ---- Followup 交互：用户回答追问 ----
                if data.get("action") == "followup_answer":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    from crew.core.followup import resolve_answer
                    qid = str(data.get("question_id") or "").strip()
                    answers = data.get("answers")
                    if qid and isinstance(answers, list):
                        resolved = resolve_answer(session_id, qid, answers)
                        note = "" if resolved else "该交互请求已过期或不存在，请重新发起。"
                        _register_session(session_id)
                        await connections.push_payload(
                            session_id,
                            {
                                "kind": "followup_question",
                                "body": {
                                    "question_id": qid,
                                    "status": "resolved" if resolved else "expired",
                                    "accepted": resolved,
                                    "note": note,
                                },
                                "is_final": False,
                                "sequence": 0,
                                "session_id": session_id,
                            },
                            owner_account_id=owner,
                        )
                    continue

                # ---- Followup 交互：用户取消追问 ----
                if data.get("action") == "followup_cancel":
                    if not _session_owned(session_id):
                        await _reject_missing_session(session_id)
                        continue
                    from crew.core.followup import cancel_followup
                    qid = str(data.get("question_id") or "").strip()
                    if qid:
                        cancel_followup(session_id, qid)
                    continue

                # ---- Plan 模式控制 ----
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
                        await _send_status(
                            session_id, "已保留 Plan 模式，请继续完善计划"
                        )
                    elif act == "plan_reject_and_exit":
                        pm.reject_and_exit(session_id, owner_account_id=owner)
                        await _send_status(session_id, "已拒绝计划并退出 Plan 模式")
                    elif act == "plan_exit":
                        pm.exit(session_id, owner_account_id=owner)
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
                    continue
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
                            if resolve_skill(slug) is None:
                                try:
                                    install_skill(
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
                            info = resolve_skill_any(slug)
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
                        continue
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
                        continue

                # Skill 调度：/skill-name [补充指令] 或 /package-name 或 /package-name/skill-name
                # 保留原始输入作为用户可见消息，skill/package 展开内容作为 is_meta 消息
                skill_meta: str | None = None
                active_skills: list[dict] = []
                active_packages_added: list[str] = []
                if query.startswith("/"):
                    command, _, user_instruction = query[1:].partition(" ")

                    # 1. 先尝试解析为 skill（支持 /package/skill、/skill、alias、中文名）
                    skill_key = resolve_skill(command)
                    if skill_key:
                        activation = build_skill_activation(
                            skill_key,
                            user_instruction,
                            session_id,
                        )
                        if activation is not None:
                            skill_meta = activation.instruction
                            active_skills.append(activation.to_dict())
                            _apply_browser_skill_policy(
                                crew, skill_key, owner, session_id
                            )
                    else:
                        # 2. 尝试解析为 package 并展开
                        pkg = resolve_package(command)
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

                            members = get_package_members(pkg_slug)
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
                        continue
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
                wiki_confirmation_id = str(data.get("wiki_confirmation_id") or "").strip()
                if wiki_confirmation_id:
                    envelope.params["wiki_confirmation_id"] = wiki_confirmation_id
                if team_execution_profile is not None:
                    envelope.params["team_execution_profile"] = team_execution_profile
                if data.get("team_confirm_execution_mode"):
                    envelope.params["team_confirm_execution_mode"] = True
                envelope.attachments = attachments
                envelope.params["session_context"] = session_context_from_envelope(
                    envelope, connected_platforms(channel_manager)
                )
                if active_packages_added:
                    envelope.params["active_skill_packages"] = active_packages_added
                if skill_meta:
                    envelope.params["skill_meta"] = skill_meta
                if active_skills:
                    envelope.params["active_skills"] = active_skills

                _spawn(envelope)
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
