"""Gateway FastAPI 应用装配：create_app + lifespan + 渠道/投递接线 + SPA 托管 + run()。

端点逻辑按域拆到 routers/，WS 在 ws.py，辅助在 helpers.py。本模块只做「把内核
对象按需喂给各 router 工厂、挂路由、管生命周期、托管前端」。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import uvicorn
from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from crew.app import CrewApp, build_app
from crew.channels.feature import CHANNELS_SERVICE_KEY
from crew.channels.broadcast import make_broadcasting_handler
from crew.gateway.auth import (
    AuthenticationError,
    authenticate_http_request,
    is_loopback_client,
    is_loopback_host,
)
from crew.gateway.auth_policy import requires_gateway_auth
from crew.gateway.connections import ConnectionManager
from crew.gateway.desktop_parent_lease import DesktopParentLease
from crew.gateway.health_server import (
    GatewayHealthServer,
    build_app_health_components,
    note_loop_tick,
    set_health_components,
)
from crew.gateway.instance_auth import GATEWAY_INSTANCE_CHALLENGE_HEADER, build_gateway_health_payload
from crew.gateway.helpers import (
    DIST_DIR,
    EXTERNAL_AGENTS_DISABLED_BODY,
    NOT_BUILT_HTML,
    ExternalAgentsDisabledError,
)
from crew.features.hooks import hook_registry
from crew.gateway.interaction_bridge import create_interaction_router, interaction_bridge
from crew.gateway.logout import LogoutCoordinator
from crew.gateway.routers.auth_session import create_auth_session_router
from crew.gateway.routers.browser import create_browser_router
from crew.gateway.routers.channels import create_channels_router
from crew.gateway.routers.config import create_config_router
from crew.gateway.routers.cron import create_cron_router
from crew.gateway.routers.mcp_servers import create_mcp_servers_router
from crew.gateway.routers.mcp_setup import create_mcp_setup_router
from crew.gateway.routers.misc import create_misc_router
from crew.gateway.routers.notifications import create_notifications_router
from crew.gateway.routers.plugins import create_plugins_router
from crew.gateway.routers.remote_auth import create_remote_auth_router
from crew.gateway.routers.runtimes import create_runtimes_router
from crew.gateway.routers.collaboration_planning import create_collaboration_planning_router
from crew.gateway.routers.collaboration_execution import create_collaboration_execution_router
from crew.gateway.routers.scenarios import create_scenarios_router
from crew.gateway.routers.security import create_security_router
from crew.gateway.routers.sessions import create_sessions_router, read_session_history_window
from crew.gateway.routers.sites import create_sites_router
from crew.gateway.routers.system import create_system_router
from crew.gateway.routers.tracing import create_tracing_router
from crew.gateway.ws import create_ws_router
from crew.gateway.send_inbox import DesktopSendInbox
from crew.security.settings import strict_security_enabled
from crew.state.logging import get_logger

log = get_logger("gateway")
_CORE_STARTUP_TIMEOUT_SECONDS = 90.0


def make_route_gate(route_registry: Any, contribution_id: str) -> Any:
    """运行期闸门依赖：在整个请求期间持有同代 Route Lease。

    FastAPI 路由树在构造期固化，Feature 停用不拆路由；闸门口径以
    Route Registry 的可见性为准（演进方案 §6.5 运行期能力门控）。
    Async-generator dependency 的 ``finally`` 在 endpoint 返回后执行，
    因而 draining 只拒绝新请求，在途请求可安全完成。
    """
    from fastapi import HTTPException

    async def _gate(request: Request):
        acquired = route_registry.acquire_lease(
            contribution_id,
            label=f"route:{contribution_id}",
        )
        if acquired is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error": f"功能暂不可用：{contribution_id}",
                    "code": "capability_unavailable",
                },
            )
        binding, lease = acquired
        # Feature-owned routes can use this binding to resolve other
        # generation-bound capabilities without a second, drifting lookup.
        request.state.route_binding = binding
        try:
            yield
        finally:
            lease.release()

    return _gate


def create_app(crew: CrewApp | None = None, *, desktop_managed: bool = False) -> FastAPI:
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

    async def capture_turn_history(owner, sid, request_id, started_at, is_current):
        def read_history():
            store = crew.session_store
            if not callable(getattr(store, "load_window", None)):
                return None
            get_config = getattr(store, "get_agent_config", None)
            binding = get_config(sid, owner_account_id=owner) if callable(get_config) else None
            if str((binding or {}).get("executor") or "").lower() == "team":
                return None
            result = read_session_history_window(store, sid, owner, 100)
            items = result["items"]
            if not items or items[-1]["role"] != "user" or items[-1].get("source_session_id") != sid:
                return None
            items[-1]["request_id"] = request_id
            return result

        await connections.capture_turn_history(owner, sid, request_id, started_at, is_current, read_history)

    dispatcher.capture_turn_history = capture_turn_history
    crew.set_push(connections.push, push_payload_fn=connections.push_payload, notify_owner_fn=connections.notify_owner)
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
    # Team 可能未启用或 Generation 未就绪；通过动态 getter 取值避免直接写 None 时抛异常。
    team = getattr(crew, "team", None)
    if team is not None:
        team.interaction_bridge = interaction_bridge
    channel_manager = crew.channel_manager
    delivery_router = crew.delivery_router
    if channel_manager is None or delivery_router is None:
        from crew.channels.feature import build_channels_feature

        fallback = build_channels_feature(
            crew,
            registry=getattr(crew, "registry", None),
            session_store=crew.session_store,
        ).service
        channel_manager = channel_manager or fallback.channel_manager
        delivery_router = delivery_router or fallback.delivery_router
        crew.channel_manager = channel_manager
        crew.delivery_router = delivery_router

    def _resolve_channels_service() -> Any:
        return crew.plugins.resolve_service(CHANNELS_SERVICE_KEY)

    async def _stop_all_channels(reason: str = "disconnected") -> None:
        service = _resolve_channels_service()
        if service is None:
            return
        await service.channel_manager.stop_all(reason=reason)
    logout_coordinator = LogoutCoordinator(
        active_owner=crew.active_owner,
        dispatcher=dispatcher,
        task_runtime=crew.tasks,
        connections=connections,
        channel_handler=crew.channel_handler,
        cron_service_getter=lambda: crew.cron_service,
        channels_service_getter=lambda: crew.plugins.acquire_service_lease(
            CHANNELS_SERVICE_KEY,
            label="logout:channels",
        ),
        team_manager=crew.team,
        interaction_bridge=interaction_bridge,
        security_service=crew.security_service,
    )
    crew.logout_coordinator = logout_coordinator
    def _prepare_owner_storage() -> None:
        completed = crew.active_owner.complete_restart_logout()
        if completed:
            log.info("Gateway 重启已完成渠道物理断连，释放退出 Owner: %s", completed)
        # An old anonymous local lease cannot be recovered in login mode.
        auth_mode = str(getattr(crew.config, "auth_mode", "local") or "local").strip().lower()
        if auth_mode in {"email", "remote"} and crew.active_owner.get("local") is not None:
            if crew.active_owner.release("local"):
                log.info("认证模式已切换为 %s，释放遗留 local Owner 租约", auth_mode)

    async def _startup_core() -> None:
        # All ingress stays disconnected while leases from an earlier Gateway
        # are reconciled and the core runtime becomes usable.
        await _stop_all_channels(reason="login_required")
        await asyncio.to_thread(_prepare_owner_storage)
        await crew.startup_core()

    def _read_startup_status() -> str:
        return api.state.gateway_readiness["status"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        generation = str(uuid4())
        _app.state.gateway_readiness = {"status": "starting", "generation": generation}
        stopping = False

        def publish(status: str, error_code: str | None = None) -> bool:
            current = _app.state.gateway_readiness
            if stopping or current["generation"] != generation or current["status"] != "starting":
                return False
            _app.state.gateway_readiness = {"status": status, "generation": generation}
            if error_code is not None:
                _app.state.gateway_readiness["error_code"] = error_code
            return True

        health_port = int(crew.config.gateway_port) + 1
        if not desktop_managed:
            health_port = int(getattr(crew.config, "health_port", 0) or 0) or health_port
        health_server = GatewayHealthServer(port=health_port)
        bound_health_port = health_server.start()

        def components() -> dict:
            return build_app_health_components(_read_startup_status(), crew)

        async def _deferred_startup(core: asyncio.Task[None]) -> None:
            completed, _ = await asyncio.wait({core}, timeout=_CORE_STARTUP_TIMEOUT_SECONDS)
            if not completed:
                publish("failed", "GATEWAY_STARTUP_TIMEOUT")
                log.error("Gateway 核心启动超时 generation=%s", generation)
                core.cancel()
                return
            try:
                core.result()
            except asyncio.CancelledError:
                publish("failed", "GATEWAY_STARTUP_CANCELLED")
                return
            except Exception:
                publish("failed", "GATEWAY_CORE_STARTUP_FAILED")
                log.exception("Gateway 核心启动失败 generation=%s", generation)
                return
            if not publish("ready"):
                return
            try:
                # Optional services may work on their own clocks. Core chat
                # does not wait for a remote MCP or local scheduler.
                await hook_registry.emit("gateway:startup", {
                    "gateway_host": crew.config.gateway_host,
                    "gateway_port": crew.config.gateway_port,
                })
                await crew.startup_services()
                if not stopping and _app.state.gateway_readiness["generation"] == generation:
                    # Remount current Owners once the optional scheduler has started.
                    for lease in crew.active_owner.list():
                        logout_coordinator.activate_owner(lease.owner_account_id)
            except Exception:
                log.exception("Gateway 可选服务启动失败 generation=%s", generation)

        startup_tasks = {asyncio.create_task(_health_loop_sentinel(components), name="gateway-health-sentinel")}
        if desktop_managed and bound_health_port is None:
            # Only the authenticated health endpoint serves this failed launch.
            # Desktop will retire it before trying another pair.
            publish("failed", "GATEWAY_HEALTH_PORT_UNAVAILABLE")
        else:
            core = asyncio.create_task(_startup_core(), name="gateway-startup-core")
            startup_tasks.update((asyncio.create_task(_deferred_startup(core), name="gateway-startup"), core))
        try:
            yield
        finally:
            stopping = True
            dispatcher.begin_shutdown()
            for task in startup_tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            completed, pending = await asyncio.wait(startup_tasks, timeout=2.0)
            for task in completed:
                if not task.cancelled():
                    task.exception()
            if pending:
                log.warning("Gateway 启动任务关停超时 generation=%s count=%d", generation, len(pending))
            await logout_coordinator.shutdown()
            await _stop_all_channels()
            await crew.shutdown()
            # 反注册本实例的渠道会话通知钩子：hook_registry 是全局单例，
            # create_app 每次调用都会新建闭包，不注销会跨实例累积扇出
            hook_registry.unregister("agent:start", _notify_channel_session_updated)
            hook_registry.unregister("agent:end", _notify_channel_session_updated)
            # 触发 gateway:shutdown hook
            await hook_registry.emit("gateway:shutdown", {})
            # 最后停 health 线程：shutdown 期间桌面端仍能看到进程活性与循环滞后。
            health_server.stop()

    publish_api_docs = crew.config.gateway_dev_mode and not strict_security_enabled()
    api = FastAPI(
        title="Crew Gateway",
        lifespan=lifespan,
        docs_url="/docs" if publish_api_docs else None,
        redoc_url="/redoc" if publish_api_docs else None,
        openapi_url="/openapi.json" if publish_api_docs else None,
    )
    # Embedded ASGI users that explicitly omit lifespan keep their existing
    # behavior. Uvicorn replaces this state before serving any request.
    api.state.gateway_readiness = {"status": "ready", "generation": str(uuid4())}

    @api.exception_handler(ExternalAgentsDisabledError)
    async def external_agents_disabled_handler(
        _request: Request,
        _exc: ExternalAgentsDisabledError,
    ) -> JSONResponse:
        return JSONResponse(EXTERNAL_AGENTS_DISABLED_BODY, status_code=403)

    @api.middleware("http")
    async def require_gateway_login(request: Request, call_next):
        """Restrict protected REST APIs to the trusted local Gateway client."""
        path = request.url.path
        if path.startswith("/api/auth/"):
            client_host = request.client.host if request.client else None
            if not is_loopback_client(client_host):
                return JSONResponse({"ok": False, "error": "仅允许本机访问"}, status_code=401)
        account = None
        if requires_gateway_auth(path):
            try:
                account = await authenticate_http_request(request, crew.config)
            except AuthenticationError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        startup_status = _read_startup_status()
        if path.startswith("/api/") and path != "/api/health" and startup_status != "ready":
            starting = startup_status == "starting"
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Gateway 正在初始化" if starting else "Gateway 初始化失败",
                    "code": "GATEWAY_STARTING" if starting else "GATEWAY_STARTUP_FAILED",
                },
                status_code=503,
                headers={"Retry-After": "1"} if starting else {},
            )
        if account is not None:
            is_logout = path == "/api/auth/logout"
            if logout_coordinator.is_draining(account.owner_account_id) and not is_logout:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Gateway 正在清理退出账号",
                        "code": "LOGOUT_IN_PROGRESS",
                    },
                    status_code=423,
                )
            if not is_logout:
                crew.active_owner.claim(account.owner_account_id)
                logout_coordinator.activate_owner(account.owner_account_id)
            request.state.account = account
        return await call_next(request)

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
    api.include_router(create_config_router(crew, dispatcher))
    api.include_router(create_remote_auth_router(crew.config))
    api.include_router(create_auth_session_router(logout_coordinator))
    api.include_router(create_browser_router(crew))
    api.include_router(create_sessions_router(crew, dispatcher))
    api.include_router(create_cron_router(crew))
    api.include_router(create_runtimes_router(crew))
    api.include_router(create_collaboration_planning_router(crew))
    api.include_router(create_collaboration_execution_router(crew))
    api.include_router(create_scenarios_router(crew))
    api.include_router(create_channels_router(crew, dispatcher))
    api.include_router(create_notifications_router(crew))
    api.include_router(create_misc_router(crew))
    api.include_router(create_plugins_router(crew))
    api.include_router(create_mcp_setup_router(crew))
    api.include_router(create_sites_router(crew))
    api.include_router(create_mcp_servers_router(crew))
    api.include_router(create_interaction_router(interaction_bridge, crew))
    api.include_router(create_system_router(crew))
    api.include_router(create_tracing_router(crew))
    api.include_router(create_security_router(crew))
    session_transaction = getattr(crew.session_store, "transaction", None)
    session_read_query = getattr(crew.session_store, "read_query", None)
    ensure_session_row = getattr(crew.session_store, "ensure_session_in_transaction", None)
    desktop_send_store_id = getattr(crew.session_store, "desktop_send_store_id", None)
    send_inbox = (
        DesktopSendInbox(session_transaction, session_read_query, ensure_session_row, desktop_send_store_id)
        if callable(session_transaction) and callable(session_read_query)
        and callable(ensure_session_row) and desktop_send_store_id else None
    )
    api.include_router(
        create_ws_router(
            crew,
            dispatcher,
            connections,
            channel_manager,
            logout_coordinator=logout_coordinator,
            startup_status=_read_startup_status,
            send_inbox=send_inbox,
        )
    )

    route_registry = crew.plugins.feature_runtime.routes

    for binding in route_registry.bindings():
        contribution = binding.contribution
        try:
            api.include_router(
                contribution.router,
                prefix=contribution.prefix,
                dependencies=[Depends(make_route_gate(route_registry, contribution.contribution_id))],
            )
            log.info(
                "插件 API 已挂载: %s (contribution=%s, owner=%s)",
                contribution.prefix,
                contribution.contribution_id,
                binding.generation.key,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("插件 API 挂载失败 %s: %s", contribution.prefix, exc)

    from crew.channels.channel_sessions import (
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
            json.dumps({
                "host": host,
                "port": port,
                "pid": os.getpid(),
                "started_at": time.time(),
            }),
            encoding="utf-8",
        )
        atexit.register(lambda: path.unlink(missing_ok=True))
    except OSError:
        log.warning("Gateway 发现文件写入失败", exc_info=True)


class _GatewayWebSocketProtocol(AutoWebSocketsProtocol):
    def shutdown(self) -> None:
        # A peer close can start draining the transport one loop turn before
        # connection_lost removes this protocol from the server's active set.
        if not self.transport.is_closing():
            super().shutdown()


class _DesktopLeaseServer(uvicorn.Server):
    def __init__(
        self,
        config: uvicorn.Config,
        parent_lease: DesktopParentLease,
        begin_shutdown: Callable[[], object],
    ) -> None:
        super().__init__(config)
        self._parent_lease = parent_lease
        self._begin_shutdown = begin_shutdown

    async def on_tick(self, counter: int) -> bool:
        if self._parent_lease.closed and not self.should_exit:
            self._begin_shutdown()
            self.should_exit = True
        return await super().on_tick(counter)


async def _health_loop_sentinel(components: Callable[[], dict] | None = None) -> None:
    """Publish the main loop's liveness and optional component snapshot."""
    loop = asyncio.get_running_loop()
    while True:
        note_loop_tick(loop.time())
        if components is not None:
            set_health_components(components())
        await asyncio.sleep(1.0)


def _serve_managed_build_failure(parent_lease: DesktopParentLease, port: int) -> None:
    """Keep a failed managed generation observable when constructing its stores failed."""
    readiness = {"status": "failed", "generation": str(uuid4()), "error_code": "GATEWAY_CORE_BUILD_FAILED"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        health = GatewayHealthServer(port=port + 1) if port < 65535 else None
        if health is not None:
            health.start()
        sentinel = asyncio.create_task(_health_loop_sentinel())
        try:
            yield
        finally:
            sentinel.cancel()
            await asyncio.gather(sentinel, return_exceptions=True)
            if health is not None:
                health.stop()

    api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        challenge = request.headers.get(GATEWAY_INSTANCE_CHALLENGE_HEADER)
        payload, status = build_gateway_health_payload(challenge)
        if status == 200:
            payload["readiness"] = readiness
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    config = uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning", ws=_GatewayWebSocketProtocol)
    _DesktopLeaseServer(config, parent_lease, lambda: None).run()


def run(*, parent_lease: DesktopParentLease | None = None) -> None:
    if parent_lease is not None and parent_lease.closed:
        return
    # 忽略 SIGPIPE：MCP stdio 子进程的 stdout 管道断开时不应杀死网关主进程。
    # 没有此处理时，BrokenPipeError 通过 anyio TaskGroup 冒泡到 uvicorn 事件循环，
    # 导致网关以 exit code 3 崩溃（MCP SDK 未隔离 stdout_writer 的 BrokenPipeError）。
    # Windows 无 SIGPIPE（POSIX 专属），跳过；Windows 下 MCP 走另一条路不受此影响。
    import signal
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    try:
        crew = build_app()
    except Exception:
        if parent_lease is None:
            raise
        configured_port = os.environ.get("GATEWAY_PORT", "")
        if not configured_port.isascii() or not configured_port.isdecimal() or len(configured_port) > 5:
            raise
        port = int(configured_port)
        if not 1 <= port <= 65535:
            raise
        log.exception("Gateway 核心装配失败")
        if not parent_lease.closed:
            _serve_managed_build_failure(parent_lease, port)
        return
    if parent_lease is not None and parent_lease.closed:
        asyncio.run(crew.shutdown())
        return
    api = create_app(crew, desktop_managed=parent_lease is not None)
    cfg = crew.config

    # 直接绑定 cfg.gateway_port（Linux 打包态由 desktop 子进程托管：desktop 先扫描
    # 8000-8009 空闲端口，通过 GATEWAY_PORT env 指定，gateway 直接绑即可）。
    # 绑不上（端口被占）则 EADDRINUSE 崩溃，desktop 检测到子进程退出会重试扫下一个端口。
    # 不在 gateway 侧做 fuser-k / 端口扫描 / 退让重试——那些曾导致 sibling 互杀循环和
    # 静默换端口（Linux 桌面端写死 8000，回退即失联）。
    log.info("Gateway 启动: http://%s:%s", cfg.gateway_host, cfg.gateway_port)
    _write_gateway_discovery_file(cfg.gateway_host, cfg.gateway_port)
    if parent_lease is None:
        uvicorn.run(api, host=cfg.gateway_host, port=cfg.gateway_port, log_level="warning", ws=_GatewayWebSocketProtocol)
    else:
        config = uvicorn.Config(
            api, host=cfg.gateway_host, port=cfg.gateway_port, log_level="warning",
            ws=_GatewayWebSocketProtocol, timeout_graceful_shutdown=5,
        )
        _DesktopLeaseServer(config, parent_lease, crew.dispatcher.begin_shutdown).run()
        if crew.dispatcher.shutdown_timed_out:
            raise SystemExit(1)
