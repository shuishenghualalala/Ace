"""WebSocket 流式对话入口。

发 {query, session_id, mode} 收 ResponseChunk；query 以 /skill-name 开头自动激活 skill；
经 SessionDispatcher 同 session 串行、忙时排队；{action:"stop"|"interrupt"|"steer"|plan_*}
控制运行。出站帧经 outbound 过滤/静默检测，可选鉴权 + 30s 心跳。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from crew.agent.skills import (
    _parse_frontmatter,
    build_skill_activation,
    get_package_members,
    get_skills,
    install_skill,
    resolve_package,
    resolve_skill,
    resolve_skill_any,
)
from crew.core.envelope import Envelope
from crew.core.followup import get_followup_waiter
from crew.core.runctx import current_active_skill_packages
from crew.gateway.auth import (
    AuthenticationError,
    authenticate_websocket,
    process_authority_for_account,
    require_admin,
)
from crew.gateway.broadcast import stream_and_broadcast
from crew.gateway.context import normalize_agent_attachments
from crew.gateway.helpers import (
    WS_PING_INTERVAL_S,
    WS_RECEIVE_TIMEOUT_S,
    connected_platforms,
    resolve_session_id,
    status_frame,
)
from crew.gateway.session_context import session_context_from_envelope
from crew.scenarios import resolve_binding as resolve_scenario_binding
from crew.state.active_owner import ActiveOwnerConflict
from crew.state.logging import get_logger

log = get_logger("gateway.ws")

WS_PROTOCOL_VERSION = 1
WS_MAX_FRAME_BYTES = 1024 * 1024
WS_MAX_QUERY_CHARS = 128 * 1024
WS_MAX_PLAN_CHARS = 256 * 1024
WS_MAX_TEXT_CHARS = 64 * 1024
WS_MAX_ATTACHMENT_CONTENT_CHARS = 100_000
WS_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
WS_MAX_REQUEST_ATTACHMENT_BYTES = 128 * 1024 * 1024
WS_MAX_SESSIONS = 100
WS_MAX_ATTACHMENTS = 20
WS_MAX_ANSWERS = 50
WS_MAX_JSON_DEPTH = 12
WS_MAX_JSON_NODES = 10_000
_MAX_IDENTIFIER_CHARS = 256
_MAX_REQUEST_ID_CHARS = 128
_MAX_NONCE_CHARS = 128
_PROTOCOL_FIELDS = frozenset({"protocol_version", "client_sequence", "nonce"})
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")

_MESSAGE_FIELDS = frozenset({
    "query",
    "session_id",
    "request_id",
    "mode",
    "workspace_id",
    "attachments",
    "sub_scenario",
    "client_intent",
    "external_team_id",
    "team_execution_profile",
    "team_confirm_execution_mode",
    "intent",
    "wiki_ingest",
    "wiki_kb_id",
    "kb_id",
    "wiki_confirmation_id",
    "web_search_enabled",
    "work_disabled_preference_ids",
    "plan_active",
}) | _PROTOCOL_FIELDS

_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "subscribe": frozenset({"action", "session_id", "sessions", "last_gateway_sequences"}),
    "resume": frozenset({"action", "session_id", "sessions", "last_gateway_sequences"}),
    "stop": frozenset({"action", "session_id"}),
    "interrupt": frozenset({"action", "session_id"}),
    "steer": frozenset({"action", "session_id", "text"}),
    "background": frozenset({"action", "session_id"}),
    "followup_answer": frozenset({"action", "session_id", "question_id", "answers"}),
    "followup_cancel": frozenset({"action", "session_id", "question_id"}),
    "plan_enter": frozenset({"action", "session_id"}),
    "plan_approve": frozenset({
        "action", "session_id", "request_id", "mode", "workspace_id", "plan"
    }),
    "plan_reject": frozenset({"action", "session_id"}),
    "plan_reject_and_exit": frozenset({"action", "session_id"}),
    "plan_exit": frozenset({"action", "session_id"}),
    "plan_update": frozenset({"action", "session_id", "plan"}),
    # These are currently compatibility control frames. They are deliberately
    # schema-bound even though their mode state is handled outside this module.
    "wiki_enter": frozenset({"action", "session_id", "kb_id", "web_search_enabled"}),
    "wiki_exit": frozenset({"action", "session_id"}),
}
_ACTION_FIELDS = {
    action: fields | _PROTOCOL_FIELDS for action, fields in _ACTION_FIELDS.items()
}


class WebSocketProtocolError(ValueError):
    """Fail-closed protocol rejection with a stable public error code."""

    def __init__(self, code: str = "PROTOCOL_INVALID", *, close_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.close_code = close_code


def _protocol_error(code: str = "PROTOCOL_INVALID") -> WebSocketProtocolError:
    return WebSocketProtocolError(code)


def _require_string(
    value: object,
    *,
    minimum: int = 0,
    maximum: int = WS_MAX_TEXT_CHARS,
    strip_stable: bool = False,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise _protocol_error()
    if "\x00" in value or any(ord(char) < 0x20 and char not in "\t\r\n" for char in value):
        raise _protocol_error()
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise _protocol_error()
    if strip_stable and value != value.strip():
        raise _protocol_error()
    return value


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _protocol_error()
    return value


def _require_int(value: object, *, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _protocol_error()
    return value


def _validate_json_budget(value: object) -> None:
    nodes = 0

    def walk(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > WS_MAX_JSON_NODES or depth > WS_MAX_JSON_DEPTH:
            raise _protocol_error()
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            _require_string(item, maximum=WS_MAX_PLAN_CHARS)
            return
        if isinstance(item, int):
            _require_int(item, minimum=-(1 << 63), maximum=(1 << 63) - 1)
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _protocol_error()
            return
        if isinstance(item, list):
            if len(item) > WS_MAX_JSON_NODES:
                raise _protocol_error()
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > WS_MAX_JSON_NODES:
                raise _protocol_error()
            for key, child in item.items():
                _require_string(key, maximum=_MAX_IDENTIFIER_CHARS)
                walk(child, depth + 1)
            return
        raise _protocol_error()

    walk(value, 0)


def _validate_protocol_identity(data: dict[str, Any]) -> None:
    present = _PROTOCOL_FIELDS.intersection(data)
    if present != _PROTOCOL_FIELDS:
        raise _protocol_error()
    if _require_int(data["protocol_version"], minimum=1, maximum=1) != WS_PROTOCOL_VERSION:
        raise _protocol_error()
    _require_int(data["client_sequence"], minimum=1)
    nonce = _require_string(
        data["nonce"],
        minimum=16,
        maximum=_MAX_NONCE_CHARS,
        strip_stable=True,
    )
    if _NONCE_RE.fullmatch(nonce) is None:
        raise _protocol_error()


def _validate_identifier(value: object, *, maximum: int = _MAX_IDENTIFIER_CHARS) -> str:
    return _require_string(value, minimum=1, maximum=maximum, strip_stable=True)


def _validate_public_session_id(value: object) -> str:
    session_id = _validate_identifier(value)
    if "::turn::" in session_id:
        raise _protocol_error()
    return session_id


def _validate_string_list(
    value: object,
    *,
    maximum_items: int,
    maximum_string: int = _MAX_IDENTIFIER_CHARS,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise _protocol_error()
    return [
        _require_string(item, minimum=1, maximum=maximum_string, strip_stable=True)
        for item in value
    ]


def _validate_attachments(value: object) -> None:
    total_bytes = 0
    if not isinstance(value, list) or len(value) > WS_MAX_ATTACHMENTS:
        raise _protocol_error()
    allowed = frozenset({"id", "name", "path", "type", "size", "previewUrl", "content"})
    for item in value:
        if not isinstance(item, dict) or not set(item).issubset(allowed):
            raise _protocol_error()
        if "id" in item:
            _validate_identifier(item["id"], maximum=_MAX_REQUEST_ID_CHARS)
        if "name" in item:
            _require_string(item["name"], minimum=1, maximum=_MAX_IDENTIFIER_CHARS)
        if "path" in item:
            _require_string(item["path"], minimum=1, maximum=4096)
        if "type" in item and item["type"] not in {"file", "image"}:
            raise _protocol_error()
        if "size" in item:
            _require_int(item["size"], maximum=WS_MAX_ATTACHMENT_BYTES)
            total_bytes += item["size"]
        if "previewUrl" in item and item["previewUrl"] is not None:
            _require_string(item["previewUrl"], maximum=4096)
        if "content" in item:
            _require_string(item["content"], maximum=WS_MAX_ATTACHMENT_CONTENT_CHARS)
            total_bytes += len(item["content"].encode("utf-8"))
        if total_bytes > WS_MAX_REQUEST_ATTACHMENT_BYTES:
            raise _protocol_error()
        if not item.get("path") and "content" not in item:
            raise _protocol_error()


def _validate_answers(value: object) -> None:
    if not isinstance(value, list) or len(value) > WS_MAX_ANSWERS:
        raise _protocol_error()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"question_id", "answers"}:
            raise _protocol_error()
        _validate_identifier(item["question_id"])
        _validate_string_list(
            item["answers"],
            maximum_items=WS_MAX_ANSWERS,
            maximum_string=4096,
        )


def validate_ws_message(data: object) -> dict[str, Any]:
    """Validate one decoded client message with an exact, fail-closed schema."""
    _validate_json_budget(data)
    if not isinstance(data, dict):
        raise _protocol_error()
    _validate_protocol_identity(data)

    if "kind" in data:
        if data.get("kind") != "pong" or not set(data).issubset({"kind"} | _PROTOCOL_FIELDS):
            raise _protocol_error()
        return data

    action = data.get("action")
    if action is not None:
        if not isinstance(action, str) or action not in _ACTION_FIELDS:
            raise _protocol_error()
        if not set(data).issubset(_ACTION_FIELDS[action]):
            raise _protocol_error()
    elif not set(data).issubset(_MESSAGE_FIELDS):
        raise _protocol_error()

    _validate_public_session_id(data.get("session_id"))

    if "request_id" in data:
        _validate_identifier(data["request_id"], maximum=_MAX_REQUEST_ID_CHARS)
    if "workspace_id" in data:
        _validate_identifier(data["workspace_id"])
    if "mode" in data and data["mode"] not in {"agent", "team", "dynamic_kanban"}:
        raise _protocol_error()

    if action in {"subscribe", "resume"}:
        if "sessions" in data:
            sessions = _validate_string_list(
                data["sessions"],
                maximum_items=WS_MAX_SESSIONS,
            )
            if len(sessions) != len(set(sessions)):
                raise _protocol_error()
            for session_id in sessions:
                _validate_public_session_id(session_id)
        if "last_gateway_sequences" in data:
            sequences = data["last_gateway_sequences"]
            if not isinstance(sequences, dict) or len(sequences) > WS_MAX_SESSIONS:
                raise _protocol_error()
            for sid, sequence in sequences.items():
                _validate_public_session_id(sid)
                _require_int(sequence)
    elif action == "steer":
        _require_string(data.get("text"), minimum=1, maximum=WS_MAX_TEXT_CHARS)
    elif action == "followup_answer":
        _validate_identifier(data.get("question_id"))
        _validate_answers(data.get("answers"))
    elif action == "followup_cancel":
        _validate_identifier(data.get("question_id"))
    elif action == "plan_update":
        _require_string(data.get("plan"), maximum=WS_MAX_PLAN_CHARS)
    elif action == "plan_approve":
        _validate_identifier(data.get("request_id"), maximum=_MAX_REQUEST_ID_CHARS)
        if "plan" in data:
            _require_string(data["plan"], maximum=WS_MAX_PLAN_CHARS)
    elif action == "wiki_enter":
        if "kb_id" in data:
            _validate_identifier(data["kb_id"])
        if "web_search_enabled" in data:
            _require_bool(data["web_search_enabled"])
    elif action is None:
        query = _require_string(data.get("query", ""), maximum=WS_MAX_QUERY_CHARS)
        attachments = data.get("attachments", [])
        _validate_attachments(attachments)
        if query.strip() or attachments:
            _validate_identifier(data.get("request_id"), maximum=_MAX_REQUEST_ID_CHARS)
        for field in (
            "sub_scenario",
            "client_intent",
            "external_team_id",
            "intent",
            "wiki_kb_id",
            "kb_id",
            "wiki_confirmation_id",
        ):
            if field in data:
                _validate_identifier(data[field])
        for field in (
            "team_confirm_execution_mode",
            "wiki_ingest",
            "web_search_enabled",
            "plan_active",
        ):
            if field in data:
                _require_bool(data[field])
        if "work_disabled_preference_ids" in data:
            _validate_string_list(
                data["work_disabled_preference_ids"],
                maximum_items=WS_MAX_SESSIONS,
            )
        if "team_execution_profile" in data:
            profile = data["team_execution_profile"]
            if not isinstance(profile, dict) or set(profile) != {"requested_mode"}:
                raise _protocol_error()
            if profile["requested_mode"] not in {"auto", "fast", "standard", "ai"}:
                raise _protocol_error()
    return data


def decode_ws_text_frame(text: object) -> dict[str, Any]:
    """Decode and validate a text frame after enforcing its encoded byte budget."""
    if not isinstance(text, str):
        raise WebSocketProtocolError("BINARY_UNSUPPORTED", close_code=1003)
    if len(text) > WS_MAX_FRAME_BYTES:
        raise WebSocketProtocolError("FRAME_TOO_LARGE", close_code=1009)
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _protocol_error() from exc
    if encoded_size > WS_MAX_FRAME_BYTES:
        raise WebSocketProtocolError("FRAME_TOO_LARGE", close_code=1009)

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError("duplicate object key")
            value[key] = child
        return value

    try:
        data = json.loads(
            text,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            object_pairs_hook=strict_object,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _protocol_error() from exc
    return validate_ws_message(data)


async def receive_ws_message(
    socket: WebSocket,
    *,
    admit: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Receive one raw ASGI frame so binary and byte limits are enforced pre-parse."""
    event = await socket.receive()
    event_type = event.get("type")
    if event_type == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=int(event.get("code") or 1000),
            reason=str(event.get("reason") or ""),
        )
    if event_type != "websocket.receive":
        raise _protocol_error()
    if admit is not None and not admit():
        raise WebSocketProtocolError("RATE_LIMITED", close_code=1008)
    if event.get("bytes") is not None:
        raise WebSocketProtocolError("BINARY_UNSUPPORTED", close_code=1003)
    return decode_ws_text_frame(event.get("text"))


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
        try:
            require_admin(account, crew.config)
            account_is_admin = True
        except AuthenticationError:
            account_is_admin = False
        if logout_coordinator is not None and logout_coordinator.is_draining():
            await socket.close(code=4423, reason="Logout in progress")
            return
        try:
            lease = crew.active_owner.claim(owner)
            generation, expires_at = process_authority_for_account(
                account,
                lease_claimed_at=lease.claimed_at,
                ttl_seconds=crew.config.auth_session_ttl_seconds,
            )
        except ActiveOwnerConflict:
            await socket.close(code=4423, reason="Active owner conflict")
            return
        except AuthenticationError:
            await socket.close(code=4401, reason="Unauthorized")
            return
        if logout_coordinator is not None:
            logout_coordinator.activate_owner(
                owner,
                process_authorization_generation=generation,
                process_authorization_expires_at=expires_at,
            )
        activate_connections = getattr(connections, "activate_owner", None)
        if callable(activate_connections):
            activate_connections(owner)

        await socket.accept()
        connections.register_owner(owner, socket)
        log.info("WebSocket 已连接")
        registered_sessions: set[str] = set()
        runners: dict[asyncio.Task, str] = {}
        disconnected = asyncio.Event()

        def _session_owned(session_id: str) -> bool:
            belongs = getattr(crew.session_store, "session_belongs_to", None)
            return bool(callable(belongs) and belongs(session_id, owner))

        async def _reject_missing_session(session_id: str) -> None:
            await connections.send_socket(
                socket,
                {
                    "kind": "error",
                    "body": {
                        "message": "会话不存在",
                        "code": "SESSION_NOT_FOUND",
                        "category": "protocol",
                    },
                    "is_final": True,
                    "sequence": 0,
                },
            )

        def _register_session(session_id: str) -> None:
            """把当前 socket 订阅到 session，供重连后的后台 chunk 继续投递。"""
            if session_id not in registered_sessions:
                connections.register(session_id, socket, owner_account_id=owner)
                registered_sessions.add(session_id)
                security_service = getattr(crew, "security_service", None)
                resume_session = getattr(security_service, "resume_session", None)
                if callable(resume_session):
                    resume_session(owner, session_id)

        async def _send_status(session_id: str, message: str) -> None:
            """向该 session 的活跃连接发送状态帧；当前 socket 先确保已订阅。"""
            _register_session(session_id)
            await connections.push_payload(
                session_id,
                status_frame(session_id, message),
                owner_account_id=owner,
            )

        async def _send_protocol_error(code: str) -> None:
            await connections.send_socket(
                socket,
                {
                    "kind": "error",
                    "body": {
                        "message": "消息协议校验失败",
                        "code": code,
                    },
                    "is_final": True,
                    "sequence": 0,
                },
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
            except Exception:
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
                except Exception:
                    log.exception("WS runner 兜底 error chunk 推送失败 session=%s", envelope.session_id)

        def _spawn(env: Envelope) -> None:
            """并发派发：接收循环立即继续读下一条，实现忙时排队。"""
            task = asyncio.create_task(_run(env))
            runners[task] = env.session_id
            task.add_done_callback(runners.pop)

        def _request_id_kw(data: dict) -> dict[str, str]:
            request_id = str(data.get("request_id") or "").strip()
            return {"request_id": request_id} if request_id else {}

        def _admit_frame() -> bool:
            admit_inbound = getattr(connections, "admit_inbound", None)
            return not callable(admit_inbound) or bool(admit_inbound(owner, socket))

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        receive_ws_message(socket, admit=_admit_frame),
                        timeout=WS_RECEIVE_TIMEOUT_S,
                    )
                except TimeoutError:
                    log.info("WebSocket 心跳超时，断开连接")
                    break
                except WebSocketProtocolError as exc:
                    if exc.close_code is not None:
                        if exc.code == "RATE_LIMITED":
                            try:
                                await _send_protocol_error(exc.code)
                            except Exception:
                                log.debug("发送限流协议错误失败", exc_info=True)
                        await socket.close(code=exc.close_code, reason=exc.code)
                        break
                    log.warning("拒绝非法 WebSocket 消息 code=%s", exc.code)
                    try:
                        await _send_protocol_error(exc.code)
                    except Exception:  # noqa: BLE001 - any socket failure terminates this receive loop
                        break
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

                claim = getattr(connections, "claim_inbound_identity", None)
                if callable(claim):
                    identity_error = claim(
                        owner,
                        socket,
                        session_id=str(data.get("session_id") or ""),
                        request_id=str(data.get("request_id") or ""),
                        client_sequence=data.get("client_sequence"),
                        nonce=str(data.get("nonce") or ""),
                    )
                    if identity_error:
                        await _send_protocol_error(identity_error)
                        continue

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
                try:
                    attachments = normalize_agent_attachments(attachments, owner)
                except ValueError:
                    if session_id:
                        await _send_status(session_id, "附件总量超过安全上限")
                    continue
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
                        missing_skills = [
                            slug
                            for slug in binding.get("skills") or []
                            if resolve_skill(slug) is None
                        ]
                        if missing_skills and not account_is_admin:
                            await _send_status(
                                session_id,
                                "该场景需要管理员先安装可选技能",
                            )
                            continue
                        for slug in missing_skills:
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
                            active = set(current_active_skill_packages.get())
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
            orphaned_sessions = (
                connections.unregister_all(
                    socket,
                    registered_sessions,
                    owner_account_id=owner,
                )
                or set()
            )
            orphaned_runners = [
                task
                for task, session_id in list(runners.items())
                if session_id in orphaned_sessions and not task.done()
            ]
            for task in orphaned_runners:
                task.cancel()
            if orphaned_runners:
                await asyncio.gather(*orphaned_runners, return_exceptions=True)
            # A disconnected last observer must not leave pending approvals or
            # session-scoped grants usable by an orphaned turn. Conversation
            # history remains intact and can be resumed after re-authentication.
            security_service = getattr(crew, "security_service", None)
            freeze_session = getattr(security_service, "freeze_session", None)
            for sid in orphaned_sessions:
                if callable(freeze_session):
                    try:
                        freeze_session(owner, sid)
                    except Exception:
                        log.exception("WebSocket 断开权限回收失败 session=%s", sid)
                try:
                    dispatcher.stop(
                        sid,
                        reason="最后一个认证观察者已断开，执行权限已撤销",
                        owner_account_id=owner,
                    )
                except Exception:
                    log.exception("WebSocket 断开任务终止失败 session=%s", sid)
                revoke_runtime_tools = getattr(
                    getattr(crew, "registry", None),
                    "revoke_runtime_tool_session",
                    None,
                )
                if callable(revoke_runtime_tools):
                    try:
                        await revoke_runtime_tools(owner, sid)
                    except Exception:
                        log.exception(
                            "WebSocket 断开运行期工具回收失败 session=%s",
                            sid,
                        )
                try:
                    from crew.tools.process_registry import process_registry

                    await asyncio.to_thread(
                        process_registry.revoke_session,
                        owner,
                        sid,
                        reason="LAST_OBSERVER_DISCONNECTED",
                    )
                except Exception:
                    # ProcessRegistry keeps a durable cleanup tombstone and its
                    # maintenance loop retries; the frozen session cannot issue
                    # replacement authority meanwhile.
                    log.exception("WebSocket 断开进程回收失败 session=%s", sid)
                browser_manager = getattr(crew, "browser_manager", None)
                close_browser_session = getattr(browser_manager, "close_session", None)
                if callable(close_browser_session):
                    try:
                        await close_browser_session(owner, sid)
                    except Exception:
                        log.exception("WebSocket 断开浏览器回收失败 session=%s", sid)

    return router
