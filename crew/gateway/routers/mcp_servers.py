"""通用 MCP Server 管理 API：增删改查 + 状态 + 单 server 重连。

供桌面端「MCP 服务」管理面板调用。MCP server 配置在 config.yaml 的 mcp_servers 段是
全局共享、无 owner 维度；本地桌面端为单用户场景，故本路由对**所有登录用户**开放
（登录校验由 gateway require_gateway_login 中间件保证，未登录返回 401）。

密钥处理：env 里的敏感值（key 匹配 KEY/SECRET/TOKEN/PASSWORD）随 yaml 明文持久化，
GET 响应时脱敏为 ***。
"""

from __future__ import annotations

import re
from typing import Any

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

# 合法 server 名：防注入 Registry 命名 {server}__{tool}，且作 yaml key 安全。
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# env key 命中这些后缀（不区分大小写）视为敏感，GET 脱敏。
_SECRET_KEY_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)$", re.IGNORECASE)
_VALID_TRANSPORTS = {"stdio", "http", "sse"}


def _redact_env(env: Any) -> dict[str, Any]:
    """脱敏 env 里的敏感值为 ***，供 GET 响应。"""
    if not isinstance(env, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in env.items():
        out[str(k)] = "***" if _SECRET_KEY_RE.search(str(k)) else v
    return out


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """脱敏单个 server 配置（仅 env）。"""
    out = dict(cfg)
    out["env"] = _redact_env(cfg.get("env"))
    return out


def _validate_server_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """校验新增/编辑 payload，返回 (规范化配置, 错误信息)。"""
    cfg: dict[str, Any] = {}
    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()
    transport = str(payload.get("transport") or "stdio").strip().lower()

    if command:
        cfg["command"] = command
        cfg["args"] = list(payload.get("args") or [])
        # transport 仅作记录，stdio 时忽略
    elif url:
        if transport not in _VALID_TRANSPORTS:
            return None, f"transport 必须是 {sorted(_VALID_TRANSPORTS)} 之一"
        cfg["url"] = url
        cfg["transport"] = transport
        headers = payload.get("headers")
        if headers is not None:
            cfg["headers"] = dict(headers)
    else:
        return None, "必须提供 command（stdio）或 url（http/sse）"

    env = payload.get("env")
    if env is not None:
        if not isinstance(env, dict):
            return None, "env 必须是对象"
        cfg["env"] = {str(k): str(v) for k, v in env.items()}
    return cfg, None


def create_mcp_servers_router(crew) -> APIRouter:
    router = APIRouter()

    async def _ensure_mgr_started():
        """确保 mcp_manager 已 start（注入 registry）。

        生产路径下 startup() 已调 start()，此处为 no-op；测试或不启 lifespan 的场景下
        首次管理操作触发 start，使 add_server/reload_one 能拿到 registry。
        """
        mgr = crew.mcp_manager
        if mgr is not None and getattr(mgr, "_registry", None) is None:
            await mgr.start(crew.registry)

    def _servers_view() -> list[dict[str, Any]]:
        mgr = crew.mcp_manager
        if mgr is None:
            return []
        return [
            {
                "name": row["name"],
                "transport": row["transport"],
                "connected": row["connected"],
                "error": row["error"],
                "tools": row["tools"],
                "config": _redact_config(row["config"]),
            }
            for row in mgr.status()
        ]

    @router.get("/api/mcp/servers")
    async def list_servers() -> JSONResponse:
        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.post("/api/mcp/servers")
    async def create_server(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return JSONResponse({"ok": False, "error": "name 非法（仅字母数字下划线连字符）"}, status_code=400)
        if name in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 已存在: {name}"}, status_code=409)
        cfg, err = _validate_server_payload(payload)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        crew.config.set_mcp_server(name, cfg)
        try:
            crew.config.persist_mcp_servers()
        except Exception as exc:  # noqa: BLE001
            crew.config.remove_mcp_server(name)
            return JSONResponse({"ok": False, "error": f"持久化失败: {exc}"}, status_code=500)

        # 增量启动单 server（后台连接，不阻塞响应）：worker.start() 最多等 30s
        # 启动超时，若同步等待会让前端 create 请求 hang 30s，弹层不关、列表不刷新。
        # 改为 fire-and-forget：配置已持久化，立即返回 201，连接在后台进行；前端刷新
        # 列表时该 server 会以 connected=false 出现，连上后下次 status() 轮询转为 true。
        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            crew.mcp_manager.register_pending(name, cfg)
            asyncio.create_task(crew.mcp_manager.add_server(name, cfg))

        return JSONResponse({"ok": True, "servers": _servers_view()}, status_code=201)

    @router.put("/api/mcp/servers/{name}")
    async def update_server(name: str, payload: dict[str, Any] | None = None) -> JSONResponse:
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        payload = payload or {}
        cfg, err = _validate_server_payload(payload)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        crew.config.set_mcp_server(name, cfg)
        try:
            crew.config.persist_mcp_servers()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"持久化失败: {exc}"}, status_code=500)

        # 增量重连单 server（后台进行，不阻塞响应，理由同 create）
        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            asyncio.create_task(crew.mcp_manager.reload_one(name, cfg))

        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.delete("/api/mcp/servers/{name}")
    async def delete_server(name: str) -> JSONResponse:
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)

        crew.config.remove_mcp_server(name)
        try:
            crew.config.persist_mcp_servers()
        except Exception as exc:  # noqa: BLE001
            # 内存已移除但磁盘写失败：不回滚内存（用户本意即删除），报错提示磁盘可能不一致。
            return JSONResponse({"ok": False, "error": f"持久化失败，磁盘配置可能未更新: {exc}"}, status_code=500)

        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            await crew.mcp_manager.remove_server(name)

        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.post("/api/mcp/servers/{name}/reload")
    async def reload_server(name: str) -> JSONResponse:
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        await _ensure_mgr_started()
        if crew.mcp_manager is None:
            return JSONResponse({"ok": False, "error": "MCP 管理器未初始化"}, status_code=500)
        # 后台重连，不阻塞响应（reload_one 最多等 30s 启动超时）
        asyncio.create_task(crew.mcp_manager.reload_one(name))
        return JSONResponse({"ok": True, "servers": _servers_view()})

    return router
