"""Gateway FastAPI 应用装配：create_app + lifespan + 渠道/投递接线 + SPA 托管 + run()。

端点逻辑按域拆到 routers/，WS 在 ws.py，辅助在 helpers.py。本模块只做「把内核
对象按需喂给各 router 工厂、挂路由、管生命周期、托管前端」。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from crew.app import CrewApp, build_app
from crew.gateway.auth import (
    AuthenticationError,
    authenticate_http_request,
    is_loopback_client,
    is_loopback_host,
)
from crew.gateway.auth_policy import requires_gateway_auth
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
)
from crew.gateway.hooks import hook_registry
from crew.security.settings import strict_security_enabled
from crew.gateway.interaction_bridge import create_interaction_router, interaction_bridge
from crew.gateway.logout import LogoutCoordinator
from crew.gateway.platform_registry import platform_registry
from crew.gateway.routers.auth_session import create_auth_session_router
from crew.gateway.routers.channels import create_channels_router
from crew.gateway.routers.browser import create_browser_router
from crew.gateway.routers.config import create_config_router
from crew.gateway.routers.cron import create_cron_router
from crew.gateway.routers.dynamic_kanban import create_dynamic_kanban_router
from crew.gateway.routers.misc import create_misc_router
from crew.gateway.routers.mcp_servers import create_mcp_servers_router
from crew.gateway.routers.mcp_setup import create_mcp_setup_router
from crew.gateway.routers.plugins import create_plugins_router
from crew.gateway.routers.remote_auth import create_remote_auth_router
from crew.gateway.routers.runtimes import create_runtimes_router
from crew.gateway.routers.scenarios import create_scenarios_router
from crew.gateway.routers.sessions import create_sessions_router
from crew.gateway.routers.system import create_system_router
from crew.gateway.routers.security import create_security_router
from crew.gateway.routers.wiki import create_wiki_router
from crew.gateway.routers.work import create_work_router
from crew.gateway.ws import create_ws_router
from crew.state.active_owner import ActiveOwnerConflict
from crew.state.logging import get_logger

log = get_logger("gateway")


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
        log.exception("平台通道创建失败: %s", entry.name)
        channel_manager.record_error(entry.name, str(exc))
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
    if crew.team is not None:
        crew.team.interaction_bridge = interaction_bridge
    channel_manager = ChannelManager()
    delivery_router = DeliveryRouter()
    crew.delivery_router = delivery_router

    _register_enabled_platform_channels(crew, channel_manager)
    _wire_delivery_senders(channel_manager, delivery_router)
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

        # Keep remote early-health startup, but fence every business request
        # until MCP/Cron and Owner lifecycle services are ready.
        async def _deferred_startup() -> None:
            nonlocal startup_error
            try:
                await hook_registry.emit("gateway:startup", {
                    "gateway_host": crew.config.gateway_host,
                    "gateway_port": crew.config.gateway_port,
                })
                await crew.startup()   # 连接 MCP server、启动 cron 引擎
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
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
            is_logout = path == "/api/auth/logout"
            retrying_logout = is_logout and logout_coordinator.is_draining(
                account.owner_account_id
            )
            if logout_coordinator.is_draining() and not retrying_logout:
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
                    crew.active_owner.claim(account.owner_account_id)
                except ActiveOwnerConflict:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Gateway 已由其他账号登录",
                            "code": "ACTIVE_OWNER_CONFLICT",
                        },
                        status_code=423,
                    )
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
    api.include_router(create_dynamic_kanban_router(crew))
    api.include_router(create_runtimes_router(crew))
    api.include_router(create_scenarios_router(crew))
    api.include_router(create_channels_router(crew, dispatcher, channel_manager))
    api.include_router(create_misc_router(crew))
    api.include_router(create_plugins_router(crew))
    api.include_router(create_mcp_setup_router(crew))
    api.include_router(create_wiki_router(crew))
    api.include_router(create_work_router(crew))
    api.include_router(create_mcp_servers_router(crew))
    api.include_router(create_interaction_router(interaction_bridge, crew))
    api.include_router(create_system_router(crew))
    api.include_router(create_security_router(crew))
    api.include_router(
        create_ws_router(
            crew,
            dispatcher,
            connections,
            channel_manager,
            logout_coordinator=logout_coordinator,
            startup_waiter=_wait_for_gateway_startup,
        )
    )

    for plugin_name, plugin_router in crew.plugins.api_routers:
        prefix = f"/api/plugins/{plugin_name.strip('/')}"
        try:
            api.include_router(plugin_router, prefix=prefix)
            log.info("插件 API 已挂载: %s", prefix)
        except Exception as exc:  # noqa: BLE001
            log.warning("插件 API 挂载失败 %s: %s", plugin_name, exc)

    from crew.gateway.channel_sessions import channel_platform_from_session_id, is_channel_session_id

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


def run() -> None:
    # 忽略 SIGPIPE：MCP stdio 子进程的 stdout 管道断开时不应杀死网关主进程。
    # 没有此处理时，BrokenPipeError 通过 anyio TaskGroup 冒泡到 uvicorn 事件循环，
    # 导致网关以 exit code 3 崩溃（MCP SDK 未隔离 stdout_writer 的 BrokenPipeError）。
    # Windows 无 SIGPIPE（POSIX 专属），跳过；Windows 下 MCP 走另一条路不受此影响。
    import signal
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
    uvicorn.run(api, host=cfg.gateway_host, port=cfg.gateway_port, log_level="warning")
