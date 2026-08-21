"""Gateway FastAPI 应用装配：create_app + lifespan + 渠道/投递接线 + SPA 托管 + run()。

端点逻辑按域拆到 routers/，WS 在 ws.py，辅助在 helpers.py。本模块只做「把内核
对象按需喂给各 router 工厂、挂路由、管生命周期、托管前端」。
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import threading
import time
from collections import deque
from uuid import uuid4
from contextlib import asynccontextmanager
from typing import BinaryIO

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from crew.app import CrewApp, build_app
from crew.gateway.auth import (
    AuthenticationError,
    authenticate_gateway_instance_request,
    authenticate_http_request,
    authenticate_websocket,
    is_loopback_client,
    is_loopback_host,
    process_authority_for_account,
    require_trusted_request_origin,
)
from crew.gateway.auth_policy import (
    INSTANCE_ONLY_AUTH_EXEMPT_EXACT,
    requires_gateway_auth,
    requires_gateway_instance_auth,
)
from crew.gateway.broadcast import make_broadcasting_handler
from crew.gateway.channel_config import channel_raw as resolved_channel_raw
from crew.gateway.channel_manager import ChannelManager
from crew.gateway.connections import ConnectionManager
from crew.gateway.delivery import DeliveryRouter
from crew.gateway.helpers import (
    DIST_DIR,
    EXTERNAL_AGENTS_DISABLED_BODY,
    NOT_BUILT_HTML,
    ExternalAgentsDisabledError,
    safe_public_error,
)
from crew.gateway.hooks import hook_registry
from crew.gateway.interaction_bridge import create_interaction_router, interaction_bridge
from crew.gateway.json_budget_middleware import GatewayJSONStructureBudgetMiddleware
from crew.gateway.logout import LogoutCoordinator
from crew.gateway.platform_registry import platform_registry
from crew.gateway.route_auth import include_router_with_auth
from crew.gateway.routers.auth_session import create_auth_session_router
from crew.gateway.routers.browser import create_browser_router
from crew.gateway.routers.channels import create_channels_router
from crew.gateway.routers.config import create_config_router
from crew.gateway.routers.cron import create_cron_router
from crew.gateway.routers.dynamic_kanban import create_dynamic_kanban_router
from crew.gateway.routers.mcp_servers import create_mcp_servers_router
from crew.gateway.routers.mcp_setup import create_mcp_setup_router
from crew.gateway.routers.misc import create_misc_router
from crew.gateway.routers.plugins import create_plugins_router
from crew.gateway.routers.remote_auth import create_remote_auth_router
from crew.gateway.routers.runtimes import create_runtimes_router
from crew.gateway.routers.scenarios import create_scenarios_router
from crew.gateway.routers.security import create_security_router
from crew.gateway.routers.sessions import create_sessions_router
from crew.gateway.routers.sites import create_sites_router
from crew.gateway.routers.system import create_system_router
from crew.gateway.routers.wiki import create_wiki_router
from crew.gateway.routers.work import create_work_router
from crew.gateway.ws import create_ws_router
from crew.security.audit import AuditEvent
from crew.security.settings import strict_security_enabled
from crew.state.active_owner import ActiveOwnerConflict
from crew.state.logging import get_logger

log = get_logger("gateway")


class _GatewayJSONStructureLimitMiddleware(GatewayJSONStructureBudgetMiddleware):
    """Focused-test name for the reusable Gateway JSON budget middleware."""


def _register_platform_channel(
    crew: CrewApp,
    channel_manager: ChannelManager,
    entry,
    *,
    owner_account_id: str = "",
) -> bool:
    owner = str(owner_account_id or "").strip()
    try:
        raw = resolved_channel_raw(crew.config, entry.name, owner)
        pconfig = entry.build_config(raw, include_env=not bool(owner))
    except Exception as exc:  # noqa: BLE001 - 单个平台配置异常不应阻断其它渠道启动
        log.warning("平台 %s 配置解析失败，跳过启动: %s", entry.name, exc)
        channel_manager.record_error(entry.name, "platform config invalid")
        return False

    if not pconfig.enabled:
        return False
    if not entry.configured(pconfig):
        hint = entry.install_hint or "请补全平台凭证或设置 enabled: false"
        if owner:
            log.warning(
                "平台 %s 已由 owner=%s 启用但配置不完整，跳过启动（%s）",
                entry.name,
                owner,
                hint,
            )
        else:
            log.warning("平台 %s 已启用但配置不完整，跳过启动（%s）", entry.name, hint)
        channel_manager.record_error(entry.name, "platform config incomplete")
        return False
    try:
        channel = platform_registry.create_channel(entry.name, pconfig)
    except Exception as exc:  # noqa: BLE001 - 单个平台构造失败按渠道隔离
        log.error("平台通道创建失败: %s type=%s", entry.name, type(exc).__name__)
        channel_manager.record_error(entry.name, safe_public_error(exc, "平台通道创建失败"))
        return False
    # 注入 CrewApp：供需要调用后端能力的渠道插件使用。
    if hasattr(channel, "bind_app"):
        channel.bind_app(crew)
    channel_manager.register(channel, owner_account_id=owner)
    if owner:
        log.info("恢复 owner 绑定平台通道: %s owner=%s", entry.name, owner)
    return True


def _register_enabled_platform_channels(crew: CrewApp, channel_manager: ChannelManager) -> None:
    entries = platform_registry.all_entries()
    bindings = getattr(crew, "channel_bindings", None)
    bound_owners: dict[str, str] = {}
    if bindings is not None:
        for entry in entries:
            try:
                bound_owner = str(bindings.get_binding(entry.name) or "").strip()
            except Exception as exc:  # noqa: BLE001 - 绑定存储异常不能影响其它渠道启动
                log.warning("读取平台绑定失败: %s: %s", entry.name, exc)
                continue
            if bound_owner:
                bound_owners[entry.name] = bound_owner

    for entry in entries:
        bound_owner = bound_owners.get(entry.name, "")
        if bound_owner:
            _register_platform_channel(crew, channel_manager, entry, owner_account_id=bound_owner)
            continue
        _register_platform_channel(crew, channel_manager, entry)


def _wire_delivery_senders(
    channel_manager: ChannelManager, delivery_router: DeliveryRouter
) -> None:
    """把各渠道的 send_to_target 注册进 DeliveryRouter。

    cron 投递经 app._cron_runner 读 crew.delivery_router.deliver()，不需要再把
    router 挂到 cron_service 上。
    """
    for name, channel in channel_manager.channels.items():
        sender = getattr(channel, "send_to_target", None)
        if callable(sender):
            delivery_router.register(name, sender)


class _GatewayWebSocketAuthenticationMiddleware:
    """Authenticate every mounted WebSocket before route code can run."""

    def __init__(self, app: ASGIApp, *, config) -> None:
        self.app = app
        self.config = config

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        socket = WebSocket(scope, receive=receive, send=send)
        try:
            account = await authenticate_websocket(socket, self.config)
        except AuthenticationError:
            await socket.close(code=4401, reason="Unauthorized")
            return
        state = scope.setdefault("state", {})
        state["account"] = account
        state["gateway_instance_authenticated"] = True
        await self.app(scope, receive, send)


class _GatewayRequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI allocates/parses them."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    @staticmethod
    async def _reject(send: Send, *, status: int, error: str) -> None:
        body = (f'{{"ok":false,"error":"{error}"}}').encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        framing_error = self._validate_request_framing(scope)
        if framing_error is not None:
            await self._reject(send, status=400, error=framing_error)
            return
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
            if isinstance(key, bytes)
        }
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                declared = int(raw_length.decode("ascii"), 10)
            except (UnicodeDecodeError, ValueError):
                await self._reject(send, status=400, error="请求体无效")
                return
            if declared < 0 or declared > self.max_bytes:
                await self._reject(send, status=413, error="请求体超过安全上限")
                return

        total = 0

        async def limited_receive() -> dict:
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(send, status=413, error="请求体超过安全上限")

    @staticmethod
    def _validate_request_framing(scope: Scope) -> str | None:
        content_lengths = 0
        transfer_encodings: list[str] = []
        for raw_name, raw_value in scope.get("headers", []):
            try:
                name = raw_name.decode("latin-1").lower()
                value = raw_value.decode("latin-1").strip()
            except (UnicodeDecodeError, AttributeError):
                return "请求 header 无效"
            if name == "content-length":
                content_lengths += 1
            elif name == "transfer-encoding":
                transfer_encodings.append(value.lower())
        if content_lengths > 1:
            return "重复 Content-Length"
        if len(transfer_encodings) > 1:
            return "重复 Transfer-Encoding"
        if content_lengths and transfer_encodings:
            return "Content-Length 与 Transfer-Encoding 同时出现"
        if transfer_encodings and transfer_encodings[0] != "chunked":
            return "不支持的 Transfer-Encoding"
        return None


class _RequestBodyTooLarge(Exception):
    pass


def create_app(crew: CrewApp | None = None) -> FastAPI:
    crew = crew or build_app()
    gateway_is_loopback = is_loopback_host(crew.config.gateway_host)
    compatibility_dev_bind = crew.config.gateway_dev_mode and not strict_security_enabled()
    if not gateway_is_loopback and not compatibility_dev_bind:
        raise RuntimeError("production or strict gateway must bind to a loopback host")
    # 会话调度器在 CrewApp 内共享：gateway、cron、后续平台入口走同一队列/全局并发上限。
    dispatcher = crew.dispatcher
    log.info("调度器忙时策略: %s", crew.config.gateway_busy_mode)
    # 连接管理器：session → 活跃 WS，供 cron/后台任务主动推送
    connections = ConnectionManager(min_interval=crew.config.gateway_push_min_interval)
    crew.set_push(
        connections.push,
        push_payload_fn=connections.push_payload,
        notify_owner_fn=connections.notify_owner,
    )
    crew.connections = connections  # 供平台 channel 复用 stream_and_broadcast 广播对话流
    # 渠道入站统一入口：start_all / connect / reconnect / webhook fallback 都消费这个广播包装 handler，
    # 保证任何渠道入口进来的消息都实时广播到桌面端（单一来源，避免各入口漏接）。
    crew.channel_handler = make_broadcasting_handler(crew, connections)
    host = crew.config.gateway_host
    proxy_host = host if host in {"127.0.0.1", "::1", "localhost"} else "127.0.0.1"
    if proxy_host == "::1":
        proxy_host = "[::1]"
    interaction_bridge.configure(
        push_fn=connections.push_payload,
        gateway_url=f"http://{proxy_host}:{crew.config.gateway_port}",
        crew=crew,
    )
    crew.interaction_bridge = interaction_bridge
    if crew.team is not None:
        crew.team.interaction_bridge = interaction_bridge
    channel_manager = ChannelManager()
    delivery_router = DeliveryRouter()
    crew.delivery_router = delivery_router

    _register_enabled_platform_channels(crew, channel_manager)
    _wire_delivery_senders(channel_manager, delivery_router)
    from crew.tools.process_registry import process_registry

    logout_coordinator = LogoutCoordinator(
        active_owner=crew.active_owner,
        dispatcher=dispatcher,
        task_runtime=crew.tasks,
        channel_manager=channel_manager,
        connections=connections,
        channel_handler=crew.channel_handler,
        cron_service=crew.cron_service,
        team_manager=crew.team,
        interaction_bridge=interaction_bridge,
        security_service=crew.security_service,
        process_registry=process_registry,
        runtime_tool_registry=crew.registry,
        agent_manager=crew.agents,
        credential_provider_manager=crew,
    )
    crew.logout_coordinator = logout_coordinator
    startup_ready = asyncio.Event()
    startup_error = ""
    startup_task: asyncio.Task[None] | None = None

    async def _wait_for_gateway_startup() -> bool:
        """Wait for deferred services when the ASGI lifespan is active."""
        task = startup_task
        if task is None:
            # ASGI unit tests may intentionally call the app without running
            # lifespan. Production servers always install the startup task.
            return True
        await startup_ready.wait()
        return not startup_error

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal startup_error, startup_task
        startup_ready.clear()
        startup_error = ""
        _app.state.deferred_startup_status = "starting"

        # Restart-finalized logout is a physical disconnection boundary and
        # must complete before even health readiness becomes visible.
        completed_restart_logout = crew.active_owner.complete_restart_logout()
        if completed_restart_logout:
            log.info(
                "Gateway 重启已完成渠道物理断连，释放退出 Owner: %s",
                completed_restart_logout,
            )
        # Registered channels remain physically and logically disconnected
        # until a verified Active Owner is activated after Crew startup.
        await channel_manager.stop_all(reason="login_required")

        # 从旧版 local 免登录切换到 email/remote 登录时，SQLite 里可能仍保留
        # local 的排他租约。该租约没有可恢复的登录凭据，却会阻止新账号接管，
        # 因此在所有渠道已断开、业务请求尚未放行的启动边界安全释放它。
        auth_mode = str(getattr(crew.config, "auth_mode", "local") or "local").strip().lower()
        legacy_lease = crew.active_owner.current()
        if (
            auth_mode in {"email", "remote"}
            and legacy_lease is not None
            and legacy_lease.owner_account_id == "local"
            and crew.active_owner.release("local")
        ):
            log.info("认证模式已切换为 %s，释放遗留 local Owner 租约", auth_mode)

        # Keep remote early-health startup, but fence every business request
        # until MCP/Cron and Owner lifecycle services are ready.
        async def _deferred_startup() -> None:
            nonlocal startup_error
            try:
                await hook_registry.emit(
                    "gateway:startup",
                    {
                        "gateway_host": crew.config.gateway_host,
                        "gateway_port": crew.config.gateway_port,
                    },
                )
                await crew.startup()  # 连接 MCP server、启动 cron 引擎
                lease = crew.active_owner.current()
                if lease is not None:
                    # Cron must be running before this call; otherwise its
                    # Owner mount can be skipped permanently.
                    logout_coordinator.activate_owner(lease.owner_account_id)
                else:
                    await channel_manager.stop_all(reason="login_required")
                cron_error = str(getattr(crew.cron_service, "start_error", "") or "")
                if cron_error:
                    log.warning("Gateway 基础能力已就绪，但 CronService 启动失败")
                else:
                    log.info("Gateway 延迟初始化完成（MCP / cron / Owner 门禁已就绪）")
                _app.state.deferred_startup_status = "ready"
            except asyncio.CancelledError:
                startup_error = "cancelled"
                _app.state.deferred_startup_status = "failed"
                raise
            except Exception as exc:  # noqa: BLE001
                startup_error = type(exc).__name__
                _app.state.deferred_startup_status = "failed"
                log.exception("Gateway 延迟初始化失败（部分功能可能不可用）")
            finally:
                startup_ready.set()

        startup_task = asyncio.create_task(_deferred_startup())
        try:
            yield
        finally:
            if not startup_task.done():
                startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
            startup_task = None
            await logout_coordinator.shutdown()
            await channel_manager.stop_all()
            await crew.shutdown()
            # 反注册本实例的渠道会话通知钩子：hook_registry 是全局单例，
            # create_app 每次调用都会新建闭包，不注销会跨实例累积扇出
            hook_registry.unregister("agent:start", _notify_channel_session_updated)
            hook_registry.unregister("agent:end", _notify_channel_session_updated)
            # 触发 gateway:shutdown hook
            await hook_registry.emit("gateway:shutdown", {})

    publish_api_docs = crew.config.gateway_dev_mode and not strict_security_enabled()
    api = FastAPI(
        title="Crew Gateway",
        lifespan=lifespan,
        docs_url="/docs" if publish_api_docs else None,
        redoc_url="/redoc" if publish_api_docs else None,
        openapi_url="/openapi.json" if publish_api_docs else None,
    )
    api.add_middleware(
        _GatewayWebSocketAuthenticationMiddleware,
        config=crew.config,
    )
    api.add_middleware(_GatewayJSONStructureLimitMiddleware)
    api.add_middleware(_GatewayRequestBodyLimitMiddleware)

    @api.exception_handler(ExternalAgentsDisabledError)
    async def external_agents_disabled_handler(
        _request: Request,
        _exc: ExternalAgentsDisabledError,
    ) -> JSONResponse:
        return JSONResponse(EXTERNAL_AGENTS_DISABLED_BODY, status_code=403)

    @api.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        # The original exception stays in the process only; never serialize its
        # message, traceback, cwd, or environment into a Gateway response.
        log.error("Gateway request failed type=%s", type(exc).__name__)
        return JSONResponse(
            {"ok": False, "error": "内部错误", "code": "INTERNAL_ERROR"},
            status_code=500,
        )

    @api.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default body echoes locations and input values.  Security
        # routes must not reflect a submitted path, token, or raw payload.
        return JSONResponse(
            {"ok": False, "error": "请求参数无效", "code": "INVALID_REQUEST"},
            status_code=422,
        )

    @api.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": safe_public_error(exc.detail, "请求失败")},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    denial_audit_windows: dict[tuple[str, str, str], deque[float]] = {}
    denial_audit_lock = threading.Lock()

    def _reserve_denial_audit(request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        key = (request.method.upper(), request.url.path, client)
        now = time.monotonic()
        with denial_audit_lock:
            window = denial_audit_windows.setdefault(key, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= 32:
                return False
            window.append(now)
            if not window:
                denial_audit_windows.pop(key, None)
        return True

    def _gateway_denial_event(
        request: Request,
        status_code: int,
        owner_account_id: str,
    ) -> AuditEvent:
        method = request.method.upper()
        route = request.url.path
        fingerprint = f"{method}\\n{route}\\n{status_code}".encode("utf-8")
        return AuditEvent(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest(),
            owner_account_id=owner_account_id or "unauthenticated",
            workspace_id="",
            session_id="",
            task_id="",
            request_id="",
            action_type="gateway_authorization",
            normalized_action_hash=hashlib.sha256(fingerprint).hexdigest(),
            rule_id="",
            rule_scope="gateway-route",
            permission_profile_hash="",
            additional_permissions_summary="{}",
            decision="deny",
            decision_source="gateway-auth-middleware",
            sandbox_backend="",
            capabilities=(method,),
            network_target_summary="",
            exit_code=None,
            stable_error_code=f"HTTP_{status_code}",
            tool_name="gateway",
            action_summary="Gateway authorization denied",
            action_detail=f"{method} {route} denied with HTTP {status_code}",
        )

    def _record_gateway_denial(
        request: Request,
        status_code: int,
        owner_account_id: str = "",
    ) -> bool:
        if not requires_gateway_auth(request.url.path):
            return True
        if not _reserve_denial_audit(request):
            route_digest = hashlib.sha256(request.url.path.encode("utf-8")).hexdigest()[:12]
            log.warning("gateway denial audit rate limit engaged route_digest=%s", route_digest)
            return True
        try:
            crew.security_audit.record(_gateway_denial_event(request, status_code, owner_account_id))
        except Exception as exc:  # noqa: BLE001 - denial must not silently lose durable audit
            log.error("gateway denial audit failed type=%s", type(exc).__name__)
            return False
        return True

    @api.middleware("http")
    async def require_gateway_login(request: Request, call_next):
        """Restrict protected REST APIs to the trusted local Gateway client."""
        path = request.url.path
        if path.startswith("/api/auth/"):
            client_host = request.client.host if request.client else None
            if not is_loopback_client(client_host):
                return JSONResponse({"ok": False, "error": "仅允许本机访问"}, status_code=401)
        if path.startswith("/api/") and path != "/api/health":
            if not await _wait_for_gateway_startup():
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Gateway 初始化失败",
                        "code": "GATEWAY_STARTUP_FAILED",
                    },
                    status_code=503,
                )
        if requires_gateway_auth(path):
            try:
                account = await authenticate_http_request(request, crew.config)
            except AuthenticationError as exc:
                if not _record_gateway_denial(request, 401, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                    return JSONResponse(
                        {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                        status_code=503,
                    )
                return JSONResponse(
                    {"ok": False, "error": safe_public_error(exc, "认证失败")},
                    status_code=401,
                )
            request.state.account = account
            is_logout = path == "/api/auth/logout"
            retrying_logout = is_logout and logout_coordinator.is_draining(account.owner_account_id)
            if logout_coordinator.is_draining() and not retrying_logout:
                if not _record_gateway_denial(request, 423, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                    return JSONResponse(
                        {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                        status_code=503,
                    )
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Gateway 正在清理退出账号",
                        "code": "LOGOUT_IN_PROGRESS",
                    },
                    status_code=423,
                )
            if is_logout:
                lease = crew.active_owner.current()
                if lease is not None and lease.owner_account_id != account.owner_account_id:
                    if not _record_gateway_denial(request, 423, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                        return JSONResponse(
                            {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                            status_code=503,
                        )
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Gateway 已由其他账号登录",
                            "code": "ACTIVE_OWNER_CONFLICT",
                        },
                        status_code=423,
                    )
            else:
                try:
                    lease = crew.active_owner.claim(account.owner_account_id)
                    generation, expires_at = process_authority_for_account(
                        account,
                        lease_claimed_at=lease.claimed_at,
                        ttl_seconds=crew.config.auth_session_ttl_seconds,
                    )
                except ActiveOwnerConflict:
                    if not _record_gateway_denial(request, 423, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                        return JSONResponse(
                            {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                            status_code=503,
                        )
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Gateway 已由其他账号登录",
                            "code": "ACTIVE_OWNER_CONFLICT",
                        },
                        status_code=423,
                    )
                except AuthenticationError as exc:
                    if not _record_gateway_denial(request, 401, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                        return JSONResponse(
                            {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                            status_code=503,
                        )
                    return JSONResponse(
                        {"ok": False, "error": safe_public_error(exc, "认证失败")},
                        status_code=401,
                    )
                logout_coordinator.activate_owner(
                    account.owner_account_id,
                    process_authorization_generation=generation,
                    process_authorization_expires_at=expires_at,
                )
            request.state.account = account
            request.state.gateway_instance_authenticated = True
        elif requires_gateway_instance_auth(path):
            try:
                await authenticate_gateway_instance_request(request)
                if path in INSTANCE_ONLY_AUTH_EXEMPT_EXACT and request.method.upper() not in {
                    "GET",
                    "HEAD",
                    "OPTIONS",
                }:
                    require_trusted_request_origin(
                        str(request.headers.get("origin") or ""),
                        crew.config,
                    )
            except AuthenticationError as exc:
                if not _record_gateway_denial(request, 401, getattr(getattr(request.state, "account", None), "owner_account_id", "")):
                    return JSONResponse(
                        {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                        status_code=503,
                    )
                return JSONResponse(
                    {"ok": False, "error": safe_public_error(exc, "认证失败")},
                    status_code=401,
                )
            request.state.gateway_instance_authenticated = True
        response = await call_next(request)
        owner = getattr(getattr(request.state, "account", None), "owner_account_id", "")
        denial_status = response.status_code in {401, 403, 423} or (
            response.status_code == 404 and bool(owner)
        )
        if denial_status:
            if not _record_gateway_denial(request, response.status_code, owner):
                return JSONResponse(
                    {"ok": False, "error": "安全审计不可用", "code": "SECURITY_AUDIT_UNAVAILABLE"},
                    status_code=503,
                )
        return response

    @api.middleware("http")
    async def add_gateway_security_headers(request: Request, call_next):
        """Apply browser hardening headers to every response, including auth failures."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # 各 router 工厂按需声明依赖（多数只要 crew），显式挂载，不套不透明的依赖 bundle。
    include_router_with_auth(api, create_config_router(crew, dispatcher))
    include_router_with_auth(api, create_remote_auth_router(crew.config))
    include_router_with_auth(api, create_auth_session_router(logout_coordinator))
    include_router_with_auth(api, create_browser_router(crew))
    include_router_with_auth(api, create_sessions_router(crew, dispatcher))
    include_router_with_auth(api, create_cron_router(crew))
    include_router_with_auth(api, create_dynamic_kanban_router(crew))
    include_router_with_auth(api, create_runtimes_router(crew))
    include_router_with_auth(api, create_scenarios_router(crew))
    include_router_with_auth(
        api,
        create_channels_router(crew, dispatcher, channel_manager),
    )
    include_router_with_auth(api, create_misc_router(crew))
    include_router_with_auth(api, create_plugins_router(crew))
    include_router_with_auth(api, create_mcp_setup_router(crew))
    include_router_with_auth(api, create_wiki_router(crew))
    include_router_with_auth(api, create_work_router(crew))
    include_router_with_auth(api, create_sites_router(crew))
    include_router_with_auth(api, create_mcp_servers_router(crew))
    include_router_with_auth(api, create_interaction_router(interaction_bridge, crew))
    include_router_with_auth(api, create_system_router(crew))
    include_router_with_auth(api, create_security_router(crew))
    include_router_with_auth(
        api,
        create_ws_router(
            crew,
            dispatcher,
            connections,
            channel_manager,
            logout_coordinator=logout_coordinator,
            startup_waiter=_wait_for_gateway_startup,
        ),
    )

    for plugin_name, plugin_router in crew.plugins.api_routers:
        prefix = f"/api/plugins/{plugin_name.strip('/')}"
        try:
            include_router_with_auth(
                api,
                plugin_router,
                prefix=prefix,
            )
            log.info("插件 API 已挂载: %s", prefix)
        except Exception as exc:  # noqa: BLE001
            log.warning("插件 API 挂载失败 %s: %s", plugin_name, exc)

    from crew.gateway.channel_sessions import (
        channel_platform_from_session_id,
        is_channel_session_id,
    )

    async def _notify_channel_session_updated(_event: str, ctx: dict) -> None:
        sid = str(ctx.get("session_id") or "")
        owner = str(ctx.get("owner_account_id") or "")
        if not is_channel_session_id(sid) or not owner:
            return
        platform = channel_platform_from_session_id(sid) or str(ctx.get("channel") or "")
        await connections.notify_owner(
            owner,
            {
                "kind": "channel_session_updated",
                "body": {
                    "platform": platform,
                    "session_id": sid,
                    "event": _event,
                    # 渠道入站的用户消息不作为 WS 帧广播；agent:start 时带上原文，
                    # 桌面端据此先把用户消息渲染出来，避免回答「串到上一轮」。
                    "query": str(ctx.get("message") or ""),
                },
                "session_id": sid,
                "is_final": True,
                "sequence": 0,
            },
        )

    # agent:start 即通知桌面端：渠道会话开始处理，前端可立即订阅以接收实时 delta。
    # agent:end 再通知一次：会话列表需要刷新（消息已落库、title 可能更新）。
    hook_registry.register("agent:start", _notify_channel_session_updated)
    hook_registry.register("agent:end", _notify_channel_session_updated)

    # ---- 静态前端（SPA）。放在最后，避免吃掉 /api 与 /ws ----
    if DIST_DIR.exists():
        api.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

        @api.get("/")
        async def index() -> FileResponse:
            return FileResponse(DIST_DIR / "index.html")

        # SPA fallback：非 api/ws 的路径都回 index.html
        @api.get("/{full_path:path}")
        async def spa(full_path: str) -> FileResponse:
            return FileResponse(DIST_DIR / "index.html")
    else:

        @api.get("/")
        async def not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT_HTML)

    return api


def _write_gateway_discovery_file(host: str, port: int) -> None:
    """把网关实际监听地址写入 {CREW_HOME}/run/gateway.json，进程退出时清理。

    本地客户端（web dev server 的 vite proxy 等）读这个文件即可事先知道端口，
    无需扫描候选端口。读侧仍会先探测 /api/health 再采信，容忍崩溃后的残留文件。
    """
    import atexit
    import json
    import os
    import time

    from crew.state.home import get_crew_home

    try:
        run_dir = get_crew_home() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "gateway.json"
        path.write_text(
            json.dumps(
                {
                    "host": host,
                    "port": port,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        atexit.register(lambda: path.unlink(missing_ok=True))
    except OSError:
        log.warning("Gateway 发现文件写入失败", exc_info=True)


def _start_desktop_parent_monitor(
    stream: BinaryIO,
    server: uvicorn.Server,
) -> threading.Thread:
    """Request graceful shutdown when the Desktop-owned stdin lease closes."""

    def monitor() -> None:
        try:
            stream.read(1)
        except (OSError, ValueError):
            pass
        finally:
            server.should_exit = True

    thread = threading.Thread(
        target=monitor,
        name="desktop-parent-liveness",
        daemon=True,
    )
    thread.start()
    return thread


def run() -> None:
    # 忽略 SIGPIPE：MCP stdio 子进程的 stdout 管道断开时不应杀死网关主进程。
    # 没有此处理时，BrokenPipeError 通过 anyio TaskGroup 冒泡到 uvicorn 事件循环，
    # 导致网关以 exit code 3 崩溃（MCP SDK 未隔离 stdout_writer 的 BrokenPipeError）。
    # Windows 无 SIGPIPE（POSIX 专属），跳过；Windows 下 MCP 走另一条路不受此影响。
    import os
    import re
    import signal
    import sys

    from crew.gateway.instance_auth import configure_gateway_launch_key

    desktop_parent_stream: BinaryIO | None = None
    if os.environ.pop("ACE_GATEWAY_LAUNCH_SECRET_STDIN", "") == "1":
        desktop_parent_stream = sys.stdin.buffer
        encoded_launch_key = desktop_parent_stream.readline(66).rstrip(b"\r\n")
        if re.fullmatch(rb"[0-9a-f]{64}", encoded_launch_key) is None:
            raise RuntimeError("Desktop Gateway launch key was not delivered")
        configure_gateway_launch_key(bytes.fromhex(encoded_launch_key.decode("ascii")))

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    crew = build_app()
    api = create_app(crew)
    cfg = crew.config

    # 直接绑定 cfg.gateway_port（Linux 打包态由 desktop 子进程托管：desktop 先扫描
    # 8000-8009 空闲端口，通过 GATEWAY_PORT env 指定，gateway 直接绑即可）。
    # 绑不上（端口被占）则 EADDRINUSE 崩溃，desktop 检测到子进程退出会重试扫下一个端口。
    # 不在 gateway 侧做 fuser-k / 端口扫描 / 退让重试——那些曾导致 sibling 互杀循环和
    # 静默换端口（Linux 桌面端写死 8000，回退即失联）。
    log.info("Gateway 启动: http://%s:%s", cfg.gateway_host, cfg.gateway_port)
    _write_gateway_discovery_file(cfg.gateway_host, cfg.gateway_port)
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host=cfg.gateway_host,
            port=cfg.gateway_port,
            log_level="warning",
        )
    )
    if desktop_parent_stream is not None:
        _start_desktop_parent_monitor(desktop_parent_stream, server)
    server.run()
