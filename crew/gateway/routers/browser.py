"""Authenticated Browser panel state and control gateway."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

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
from crew.state.home import task_workspace_path
from crew.state.logging import get_logger
from crew.tools.redact import redact_sensitive_display_text

log = get_logger("gateway.browser")


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
    return redact_sensitive_display_text(str(exc))[:500]


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
        raw_path = str(payload.get("path") or "").strip()
        new_tab = payload.get("new_tab") is True
        if not raw_path or len(raw_path) > 4096:
            return JSONResponse({"ok": False, "error": "HTML 文件路径无效"}, status_code=400)
        workspace_id = crew.session_store.get_workspace_id(session_id, owner_account_id=owner)
        if not workspace_id:
            return JSONResponse({"ok": False, "error": "会话工作区不存在"}, status_code=404)
        try:
            workspace = crew.workspace_store.get(workspace_id, owner_account_id=owner)
            configured_root = str(workspace.get("root_path") or "").strip()
            root = (
                Path(configured_root).expanduser().resolve(strict=True)
                if configured_root
                else task_workspace_path(workspace_id, owner_account_id=owner, create=False).resolve(strict=True)
            )
            if raw_path.lower().startswith("file:"):
                parsed = urlsplit(raw_path)
                raw_path = url2pathname(unquote(parsed.path))
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve(strict=True)
            state = await mgr.open_for_user(
                owner,
                session_id,
                artifact_path=str(candidate),
                artifact_root=str(root),
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
            session_id = mgr.session_for_target(owner, target_id)
            if session_id is None:
                return
            if not tool_allowed(session_id, owner, "browser_use"):
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

        async def send_json(event: dict) -> None:
            # Starlette's ASGI websocket sender is not concurrency-safe. State,
            # debug, command-error and pong events share one serialized path.
            async with send_lock:
                await socket.send_json(event)

        async def state_producer() -> None:
            async for event in mgr.subscribe(owner, session_id):
                if event.get("type") == "frame":
                    continue
                if (
                    event.get("type") == "debug"
                    and not tool_allowed(session_id, owner, "browser_use")
                ):
                    continue
                await send_json(event)

        async def consumer() -> None:
            while True:
                try:
                    message = await socket.receive_json()
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
                except (BrowserDriverError, ValueError) as exc:
                    # A malformed/unsupported control is local to that message;
                    # keep the state/debug channel connected.
                    await send_json({"type": "command_error", "error": _safe_error(exc)})

        tasks = {
            asyncio.create_task(state_producer()),
            asyncio.create_task(consumer()),
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

    return router
