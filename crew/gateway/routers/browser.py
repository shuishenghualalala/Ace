"""Authenticated Browser panel state and control gateway."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections import deque
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from crew.browser.driver import BrowserDriverError
from crew.browser.electron_bridge import electron_browser_bridge
from crew.browser.electron_driver import runtime_doctor
from crew.gateway.auth import (
    AuthenticationError,
    account_from_request,
    authenticate_websocket,
)
from crew.gateway.instance_auth import verify_gateway_instance_access_token
from crew.gateway.helpers import safe_public_error
from crew.security.local_path import LocalPathReference
from crew.state.home import task_workspace_path
from crew.state.logging import get_logger

log = get_logger("gateway.browser")

_BROWSER_WS_MAX_FRAME_BYTES = 256 * 1024
_BROWSER_WS_MAX_STRING_CHARS = 64 * 1024
_BROWSER_WS_IDLE_TIMEOUT_SECONDS = 5 * 60
_BROWSER_WS_TOTAL_TIMEOUT_SECONDS = 24 * 60 * 60
_BROWSER_WS_SEND_TIMEOUT_SECONDS = 10
_BROWSER_WS_RATE_WINDOW_SECONDS = 60
_BROWSER_WS_MAX_MESSAGES_PER_WINDOW = 600


class _BrowserProtocolError(ValueError):
    def __init__(self, message: str, *, close_code: int | None = None) -> None:
        super().__init__(message)
        self.close_code = close_code


class _BrowserOwnerRegistration:
    """Close-only owner registration used by Gateway logout cleanup."""

    def __init__(self, close_callback) -> None:
        self._close_callback = close_callback

    async def close(self, *, code: int = 4401, reason: str = "") -> None:
        await self._close_callback(code=code, reason=reason)

    async def send_json(self, _payload: dict) -> None:
        # Browser stream events already use the route-local send lock. Owner
        # broadcasts must not introduce a second concurrent sender.
        return None


async def _receive_browser_message(socket: WebSocket) -> dict:
    """Parse the Browser panel frame before any action dispatch."""
    # Small test doubles and legacy embedders expose receive_json only; the
    # production Starlette WebSocket always has receive and takes the strict path.
    if not callable(getattr(socket, "receive", None)):
        message = await socket.receive_json()
        if not isinstance(message, dict):
            raise _BrowserProtocolError("浏览器消息必须是对象")
        return message

    event = await socket.receive()
    if event.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=int(event.get("code") or 1000),
            reason=str(event.get("reason") or ""),
        )
    if event.get("type") != "websocket.receive":
        raise _BrowserProtocolError("浏览器消息类型无效")
    if event.get("bytes") is not None:
        raise _BrowserProtocolError("浏览器仅支持文本消息", close_code=1003)
    text = event.get("text")
    if not isinstance(text, str):
        raise _BrowserProtocolError("浏览器消息必须是文本", close_code=1003)
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _BrowserProtocolError("浏览器消息编码无效") from exc
    if encoded_size > _BROWSER_WS_MAX_FRAME_BYTES:
        raise _BrowserProtocolError("浏览器消息超过大小上限", close_code=1009)

    def strict_object(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        message = json.loads(
            text,
            object_pairs_hook=strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _BrowserProtocolError("浏览器消息格式无效") from exc
    if not isinstance(message, dict):
        raise _BrowserProtocolError("浏览器消息必须是对象")
    message_type = message.get("type")
    if message_type == "ping":
        if set(message) != {"type"}:
            raise _BrowserProtocolError("ping 字段无效")
        return message
    if message_type != "control":
        raise _BrowserProtocolError("不支持的浏览器消息类型")
    if set(message) - {"type", "action", "value"}:
        raise _BrowserProtocolError("浏览器 control 含未知字段")
    action = message.get("action")
    value = message.get("value", "")
    if (
        not isinstance(action, str)
        or not action
        or len(action) > 256
        or not isinstance(value, str)
        or len(value) > _BROWSER_WS_MAX_STRING_CHARS
    ):
        raise _BrowserProtocolError("浏览器 control 参数无效")
    return message


def _opaque(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _loopback_host(value: str | None) -> bool:
    host = str(value or "").strip()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", "testclient"}


def _bearer_token_from_headers(headers) -> str:
    """Extract the ``Authorization: Bearer <token>`` value.

    Browser 控制端点独立校验当前 Gateway 实例的 Bearer Token，因此在本路由内
    自带解析，避免让本机 owner 识别与高权限浏览器控制共用同一层校验。
    """
    raw = ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        raw = getter("authorization") or getter("Authorization") or ""
    parts = str(raw or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _browser_instance_token_matches(headers) -> bool:
    """Require the token derived from this installation's instance key."""
    presented = _bearer_token_from_headers(headers)
    return verify_gateway_instance_access_token(presented)


def _browser_access_allowed(registry, policy: dict) -> bool:
    """Use the registry's canonical four-dimensional tool filter.

    The Browser panel is another way to exercise browser tools, so it must not
    remain reachable when the model-facing registry filtered those tools out
    through either a toolset rule or an individual-tool rule.
    """
    list_schemas = getattr(registry, "list_schemas", None)
    if not callable(list_schemas):
        return False
    schemas = list_schemas(
        enabled_toolsets=policy.get("enabled_toolsets"),
        disabled_toolsets=policy.get("disabled_toolsets"),
        enabled_tools=policy.get("enabled_tools"),
        disabled_tools=policy.get("disabled_tools"),
    )
    return any(schema.get("_crew_toolset") == "browser" for schema in schemas)


def _browser_tool_allowed(registry, policy: dict, tool_name: str) -> bool:
    """Return whether one concrete Browser capability survived policy filters."""
    list_schemas = getattr(registry, "list_schemas", None)
    if not callable(list_schemas):
        return False
    schemas = list_schemas(
        only=[tool_name],
        enabled_toolsets=policy.get("enabled_toolsets"),
        disabled_toolsets=policy.get("disabled_toolsets"),
        enabled_tools=policy.get("enabled_tools"),
        disabled_tools=policy.get("disabled_tools"),
    )
    return any(
        schema.get("_crew_toolset") == "browser"
        and (schema.get("function") or {}).get("name") == tool_name
        for schema in schemas
    )


def _safe_error(exc: BaseException) -> str:
    return safe_public_error(exc, "浏览器操作失败")


def create_browser_router(crew) -> APIRouter:
    router = APIRouter()

    def manager():
        # 动态取引用：系统级卸载 browser 插件后 crew.browser_manager 会变，
        # 捕获旧引用会让面板继续驱动一个应已关闭的运行时。
        return getattr(crew, "browser_manager", None)

    def owned(session_id: str, owner: str) -> bool:
        belongs = getattr(crew.session_store, "session_belongs_to", None)
        return bool(callable(belongs) and belongs(session_id, owner))

    def user_type_for(session_id: str, owner: str) -> str:
        getter = getattr(crew.session_store, "get_agent_config", None)
        try:
            raw = getter(session_id, owner_account_id=owner) if callable(getter) else {}
        except (TypeError, ValueError):
            raw = {}
        return str((raw or {}).get("user_type") or crew.config.access_control.user_type)

    def policy_for(session_id: str, owner: str) -> dict:
        return crew.config.access_control.resolve_for(user_type_for(session_id, owner))

    def allowed(session_id: str, owner: str) -> bool:
        # 面板是另一条使用 Browser 能力的通路：除 access_control 工具过滤外，
        # 还必须过 per-owner 插件有效状态（用户关闭插件后面板同样立即失效）。
        checker = getattr(crew, "_browser_plugin_effective", None)
        if callable(checker) and not checker(owner, user_type_for(session_id, owner)):
            return False
        return _browser_access_allowed(
            getattr(crew, "registry", None),
            policy_for(session_id, owner),
        )

    def tool_allowed(session_id: str, owner: str, tool_name: str) -> bool:
        return _browser_tool_allowed(
            getattr(crew, "registry", None),
            policy_for(session_id, owner),
            tool_name,
        )

    def tools_allowed(session_id: str, owner: str, tool_names: tuple[str, ...]) -> bool:
        policy = policy_for(session_id, owner)
        registry = getattr(crew, "registry", None)
        return all(_browser_tool_allowed(registry, policy, name) for name in tool_names)

    def control_tools(action: str, value: str = "") -> tuple[str, ...]:
        # 单一 browser_use 工具承载全部浏览器动作；面板动作只需校验该工具是否
        # 通过策略（action 级细分由 browser_use 自身的 permission_resolver 执行）。
        return ("browser_use",)

    @router.get("/api/browser/doctor")
    async def browser_doctor(request: Request) -> JSONResponse:
        account = account_from_request(request)
        if not _browser_instance_token_matches(request.headers):
            return JSONResponse({"ok": False, "error": "实例校验失败"}, status_code=401)
        runtime_key = f"crew_{_opaque(account.owner_account_id)}"
        result = runtime_doctor(crew.config.browser, runtime_key)
        return JSONResponse(result)

    @router.get("/api/browser/{session_id}/state")
    async def browser_state(request: Request, session_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        if not _browser_instance_token_matches(request.headers):
            return JSONResponse({"ok": False, "error": "实例校验失败"}, status_code=401)
        if not owned(session_id, owner):
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        if not allowed(session_id, owner):
            return JSONResponse({"ok": False, "error": "该会话未开放 Browser Use"}, status_code=403)
        mgr = manager()
        if mgr is None:
            return JSONResponse({"ok": False, "error": "Browser Use 未启用"}, status_code=503)
        return JSONResponse({"ok": True, "state": mgr.state(owner, session_id)})

    @router.delete("/api/browser/data")
    async def clear_browser_data(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        if not _browser_instance_token_matches(request.headers):
            return JSONResponse({"ok": False, "error": "实例校验失败"}, status_code=401)
        mgr = manager()
        if mgr is None:
            return JSONResponse({"ok": False, "error": "Browser Use 未启用"}, status_code=503)
        try:
            return JSONResponse(await mgr.clear_owner_data(owner))
        except (BrowserDriverError, OSError) as exc:
            return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=409)

    @router.post("/api/browser/{session_id}/control")
    async def browser_control(request: Request, session_id: str, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        if not _browser_instance_token_matches(request.headers):
            return JSONResponse({"ok": False, "error": "实例校验失败"}, status_code=401)
        if not owned(session_id, owner):
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        if not allowed(session_id, owner):
            return JSONResponse({"ok": False, "error": "该会话未开放 Browser Use"}, status_code=403)
        mgr = manager()
        if mgr is None:
            return JSONResponse({"ok": False, "error": "Browser Use 未启用"}, status_code=503)
        action = str(payload.get("action") or "")
        value = str(payload.get("value") or "")
        if not tools_allowed(session_id, owner, control_tools(action, value)):
            return JSONResponse({"ok": False, "error": "该浏览器操作未开放"}, status_code=403)
        try:
            if action in {"open", "new_tab"}:
                state = await mgr.open_for_user(
                    owner,
                    session_id,
                    url=value,
                    new_tab=action == "new_tab",
                )
                return JSONResponse({"ok": True, "state": state})
            if action == "record_discard":
                # 「丢弃」必须真的删盘：轨迹里有用户看到的真实业务数据，
                # 只把按钮藏起来等于骗人——用户以为丢了，文件还在。
                #
                # 走线程：`discard_recording` 内部是同步 `shutil.rmtree`，
                # 一段长录制的目录里可能有上千个文件，在 async 端点里直接跑
                # 会把整个事件循环卡住——同一个 Gateway 上所有会话一起卡。
                removed = await asyncio.to_thread(
                    mgr.discard_recording, owner, session_id, value
                )
                return JSONResponse({"ok": True, "discarded": removed})
            if action in {
                "record_start", "record_pause", "record_resume", "record_stop",
                "record_note", "record_status",
            }:
                # 录制由用户从面板发起，模型不持有任何录制控制工具（见设计文档）。
                recording = await mgr.user_recording(
                    owner, session_id, action.removeprefix("record_"), value
                )
                return JSONResponse({"ok": True, "recording": recording})
            if action in {"takeover", "return", "pause", "stop"}:
                result = await mgr.user_control(owner, session_id, action)
                return JSONResponse(
                    {"ok": True, "result": result, "state": mgr.state(owner, session_id)}
                )
            state = await mgr.human_command(owner, session_id, action, value)
            return JSONResponse({"ok": True, "state": state})
        except (BrowserDriverError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=409)

    @router.post("/api/browser/{session_id}/artifact")
    async def browser_artifact(request: Request, session_id: str, payload: dict) -> JSONResponse:
        """Open one workspace HTML file through the Host's private preview origin."""
        owner = account_from_request(request).owner_account_id
        if not _browser_instance_token_matches(request.headers):
            return JSONResponse({"ok": False, "error": "实例校验失败"}, status_code=401)
        if not owned(session_id, owner):
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        if not allowed(session_id, owner):
            return JSONResponse({"ok": False, "error": "该会话未开放 Browser Use"}, status_code=403)
        mgr = manager()
        if mgr is None:
            return JSONResponse({"ok": False, "error": "Browser Use 未启用"}, status_code=503)
        if not tools_allowed(session_id, owner, ("browser_use",)):
            return JSONResponse({"ok": False, "error": "本地 HTML 预览能力未开放"}, status_code=403)
        raw_path = payload.get("path")
        new_tab = payload.get("new_tab") is True
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or len(raw_path) > 4096
        ):
            return JSONResponse({"ok": False, "error": "HTML 文件路径无效"}, status_code=400)
        workspace_id = crew.session_store.get_workspace_id(session_id, owner_account_id=owner)
        if not workspace_id:
            return JSONResponse({"ok": False, "error": "会话工作区不存在"}, status_code=404)
        try:
            path_reference = LocalPathReference.parse(raw_path)
            workspace = crew.workspace_store.get(workspace_id, owner_account_id=owner)
            configured_root = str(workspace.get("root_path") or "").strip()
            root = (
                Path(configured_root).expanduser().resolve(strict=True)
                if configured_root
                else task_workspace_path(workspace_id, owner_account_id=owner, create=False).resolve(strict=True)
            )
            state = await mgr.open_for_user(
                owner,
                session_id,
                artifact_path=path_reference,
                artifact_root=root,
                new_tab=new_tab,
            )
            return JSONResponse({"ok": True, "state": state})
        except (BrowserDriverError, KeyError, OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=409)

    @router.websocket("/ws/browser-host")
    async def browser_host(socket: WebSocket) -> None:
        """Register the authenticated Electron main process as browser executor.

        The desktop process opens an outbound loopback connection to the Gateway.
        The instance-derived bearer token prevents another local process from
        servicing this installation's browser tools.
        """
        if not _loopback_host(socket.client.host if socket.client else None):
            await socket.close(code=4403)
            return
        try:
            account = await authenticate_websocket(socket, crew.config)
        except AuthenticationError:
            await socket.close(code=4401)
            return
        owner = account.owner_account_id
        if not _browser_instance_token_matches(socket.headers):
            await socket.close(code=4401)
            return

        runtime_key = f"crew_{_opaque(owner)}"

        async def registered() -> None:
            # Reset the newly authenticated Host first. This is idempotent when
            # it has no owner and closes stale WebContents after a reconnect.
            await electron_browser_bridge.request(
                runtime_key,
                "close_owner",
                {},
                timeout=crew.config.browser.command_timeout_seconds,
                mutating=True,
                _allow_unready=True,
            )
            mgr = manager()
            if mgr is not None:
                await mgr.reset_host_registration(owner)

        async def host_event(event: dict) -> None:
            mgr = manager()
            if mgr is None:
                return
            target_id = str(event.get("targetId") or "")
            # 录制只发生在 human 模式，必须用包含 human 会话的解析器；用 debug 那个
            # 会让每条录制事件在这里被静默丢弃（功能看似跑通、轨迹永远是空的）。
            if event.get("type") == "recording":
                recording_id = str(event.get("recordingId") or "")
                by_ledger = mgr.session_for_recording_id(owner, recording_id)
                by_target = mgr.session_for_recording_target(owner, target_id)
                # Existing pages should agree on both identities. A newly
                # created popup is not yet in Manager's tab cache, so its
                # authenticated shared ledger is the authoritative route.
                if by_ledger is not None and by_target is not None and by_ledger != by_target:
                    return
                session_id = by_ledger or by_target
            elif event.get("type") == "download":
                by_hash = mgr.session_for_hash(
                    owner,
                    str(event.get("sessionHash") or ""),
                )
                by_target = mgr.session_for_target(owner, target_id)
                if by_hash is not None and by_target is not None and by_hash != by_target:
                    return
                session_id = by_hash or by_target
            else:
                session_id = mgr.session_for_target(owner, target_id)
            if session_id is None:
                return
            if not tool_allowed(session_id, owner, "browser_use"):
                return
            if event.get("type") == "recording":
                # 录制事件只落盘，**不进模型历史、不走 publish**。这是对
                # 「用户接管期间页面内容绝不进模型上下文」那条不变量的遵守：
                # 轨迹只在用户显式要求编译技能时，由模型用文件工具主动读入。
                await mgr.append_recording_step(
                    owner,
                    session_id,
                    event,
                    recording_id=str(event.get("recordingId") or ""),
                )
                return
            if event.get("type") == "download":
                await mgr.publish_host_download(
                    owner,
                    session_id,
                    event,
                )
                return
            await mgr.publish_host_debug(
                owner,
                session_id,
                target_id,
                str(event.get("channel") or ""),
                event.get("record") if isinstance(event.get("record"), dict) else {},
            )

        try:
            await electron_browser_bridge.serve(
                socket,
                runtime_key,
                on_registered=registered,
                on_event=host_event,
            )
        except WebSocketDisconnect:
            pass
        except Exception:
            log.info("electron browser host disconnected owner=%s", _opaque(account.owner_account_id))

    @router.websocket("/ws/browser/{session_id}")
    async def browser_stream(socket: WebSocket, session_id: str) -> None:
        if not _loopback_host(socket.client.host if socket.client else None):
            await socket.close(code=4403)
            return
        try:
            account = await authenticate_websocket(socket, crew.config)
        except AuthenticationError:
            await socket.close(code=4401)
            return
        owner = account.owner_account_id
        if not _browser_instance_token_matches(socket.headers):
            await socket.close(code=4401)
            return
        mgr = manager()
        if mgr is None or not owned(session_id, owner):
            await socket.close(code=4404)
            return
        if not allowed(session_id, owner):
            await socket.close(code=4403)
            return
        await socket.accept()
        send_lock = asyncio.Lock()
        owner_close_requested = asyncio.Event()

        async def close_from_owner(*, code: int = 4401, reason: str = "") -> None:
            if owner_close_requested.is_set():
                return
            owner_close_requested.set()
            with suppress(Exception):
                await socket.close(code=code, reason=reason)

        owner_registration = _BrowserOwnerRegistration(close_from_owner)
        connections = getattr(crew, "connections", None)
        register_owner = getattr(connections, "register_owner", None)
        unregister_all = getattr(connections, "unregister_all", None)
        registered_owner = False
        if callable(register_owner):
            register_owner(owner, owner_registration)
            registered_owner = True

        async def send_json(event: dict) -> None:
            # Starlette's ASGI websocket sender is not concurrency-safe. State,
            # debug, command-error and pong events share one serialized path.
            try:
                encoded = json.dumps(
                    event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise WebSocketDisconnect(code=1003) from exc
            if len(encoded) > _BROWSER_WS_MAX_FRAME_BYTES:
                with suppress(Exception):
                    await socket.close(code=1009)
                raise WebSocketDisconnect(code=1009)
            async with send_lock:
                try:
                    await asyncio.wait_for(
                        socket.send_json(event),
                        timeout=_BROWSER_WS_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    with suppress(Exception):
                        await socket.close(code=1013)
                    raise WebSocketDisconnect(code=1013) from exc

        async def state_producer() -> None:
            async for event in mgr.subscribe(owner, session_id):
                if event.get("type") == "owner_revoked":
                    await close_from_owner(
                        code=int(event.get("code") or 4401),
                        reason=str(event.get("reason") or ""),
                    )
                    return
                if event.get("type") == "frame":
                    continue
                if (
                    event.get("type") == "debug"
                    and not tool_allowed(session_id, owner, "browser_use")
                ):
                    continue
                await send_json(event)

        async def consumer() -> None:
            deadline = asyncio.get_running_loop().time() + _BROWSER_WS_TOTAL_TIMEOUT_SECONDS
            message_times: deque[float] = deque()
            while True:
                try:
                    now = asyncio.get_running_loop().time()
                    while message_times and now - message_times[0] >= _BROWSER_WS_RATE_WINDOW_SECONDS:
                        message_times.popleft()
                    if len(message_times) >= _BROWSER_WS_MAX_MESSAGES_PER_WINDOW:
                        with suppress(Exception):
                            await socket.close(code=1013)
                        return
                    message_times.append(now)
                    remaining = min(
                        _BROWSER_WS_IDLE_TIMEOUT_SECONDS,
                        deadline - asyncio.get_running_loop().time(),
                    )
                    if remaining <= 0:
                        with suppress(Exception):
                            await socket.close(code=1000)
                        return
                    message = await asyncio.wait_for(
                        _receive_browser_message(socket),
                        timeout=remaining,
                    )
                    if not isinstance(message, dict):
                        raise ValueError("浏览器消息必须是对象")
                    message_type = str(message.get("type") or "")
                    if message_type == "control":
                        action = str(message.get("action") or "")
                        value = str(message.get("value") or "")
                        if not tools_allowed(session_id, owner, control_tools(action, value)):
                            raise ValueError("该浏览器操作未开放")
                        if action in {"takeover", "return", "pause", "stop"}:
                            await mgr.user_control(owner, session_id, action)
                        else:
                            await mgr.human_command(
                                owner,
                                session_id,
                                action,
                                value,
                            )
                    elif message_type == "ping":
                        await send_json({"type": "pong"})
                    else:
                        raise ValueError("不支持的浏览器消息类型")
                except asyncio.TimeoutError:
                    with suppress(Exception):
                        await socket.close(code=1001)
                    return
                except _BrowserProtocolError as exc:
                    if exc.close_code is not None:
                        await socket.close(code=exc.close_code)
                        return
                    await send_json({"type": "command_error", "error": _safe_error(exc)})
                except (BrowserDriverError, ValueError) as exc:
                    # A malformed/unsupported control is local to that message;
                    # keep the state/debug channel connected.
                    await send_json({"type": "command_error", "error": _safe_error(exc)})

        async def owner_closer() -> None:
            await owner_close_requested.wait()

        tasks = {
            asyncio.create_task(state_producer()),
            asyncio.create_task(consumer()),
            asyncio.create_task(owner_closer()),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                with suppress(WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.info("browser websocket closed owner=%s session=%s", _opaque(owner), _opaque(session_id))
        finally:
            for task in tasks:
                task.cancel()
            if registered_owner and callable(unregister_all):
                unregister_all(
                    owner_registration,
                    set(),
                    owner_account_id=owner,
                )

    return router
