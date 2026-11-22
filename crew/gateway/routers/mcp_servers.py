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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crew.tools.mcp_management import SERVER_NAME_RE as _NAME_RE
from crew.tools.mcp_management import add_mcp_server, validate_server_payload as _validate_server_payload
from crew.tools.mcp_client import probe_remote_server
from crew.tools.redact import redact_sensitive_text
from crew.state.config import merge_mcp_url_secrets
# env key 命中这些后缀（不区分大小写）视为敏感，GET 脱敏。
_SECRET_KEY_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)$", re.IGNORECASE)
_SENSITIVE_NAME_RE = re.compile(r"(AUTH|COOKIE|KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
_STATUS_MAX_TOOLS = 50


def _redact_env(env: Any) -> dict[str, Any]:
    """脱敏 env 里的敏感值为 ***，供 GET 响应。"""
    if not isinstance(env, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in env.items():
        out[str(k)] = "***" if _SECRET_KEY_RE.search(str(k)) else v
    return out


def _redact_headers(headers: Any) -> dict[str, Any]:
    if not isinstance(headers, dict):
        return {}
    return {
        str(key): "***" if _SENSITIVE_NAME_RE.search(str(key)) else value
        for key, value in headers.items()
    }


def _redact_url(url: Any) -> str:
    text = str(url or "")
    parts = urlsplit(text)
    if not parts.query:
        return text
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    redacted = []
    for key, value in pairs:
        if _SENSITIVE_NAME_RE.search(key):
            redacted.append((key, "***"))
            changed = True
        else:
            redacted.append((key, value))
    if not changed:
        return text
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(redacted, safe="*"), parts.fragment,
    ))


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """脱敏单个 server 配置（env、headers 与 URL 敏感查询参数）。"""
    out = dict(cfg)
    out["env"] = _redact_env(cfg.get("env"))
    if "headers" in cfg:
        out["headers"] = _redact_headers(cfg.get("headers"))
    if "url" in cfg:
        out["url"] = _redact_url(cfg.get("url"))
    return out


def create_mcp_servers_router(crew) -> APIRouter:
    router = APIRouter()
    probe_slots = asyncio.Semaphore(2)

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
        configured = crew.config.mcp_servers if isinstance(crew.config.mcp_servers, dict) else {}
        result = []
        for row in mgr.status():
            config = configured.get(row["name"], row.get("config"))
            if not isinstance(config, dict):
                config = row.get("config")
            if not isinstance(config, dict):
                config = {}
            tools = [str(item) for item in (row.get("tools") or [])]
            result.append({
                "name": row["name"],
                "transport": config.get("transport") or row["transport"],
                "connected": row["connected"],
                "error": row["error"],
                "tools": tools[:_STATUS_MAX_TOOLS],
                "tool_count": len(tools),
                # The persisted config is the source of truth immediately after an
                # update; the worker may still be reconnecting with the previous one.
                "config": _redact_config(config),
            })
        return result

    @router.get("/api/mcp/servers")
    async def list_servers() -> JSONResponse:
        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.post("/api/mcp/servers/probe")
    async def probe_server(payload: dict[str, Any] | None = None) -> JSONResponse:
        raw = payload or {}
        cfg, err = _validate_server_payload(raw)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        name = str(raw.get("name") or "").strip()
        stored = (crew.config.mcp_servers or {}).get(name) if name else None
        if isinstance(stored, dict):
            for field in ("env", "headers"):
                current = cfg.get(field)
                previous = stored.get(field)
                if isinstance(current, dict) and isinstance(previous, dict):
                    cfg[field] = {
                        **current,
                        **{key: previous[key] for key, value in current.items()
                           if value == "***" and key in previous},
                    }
            if isinstance(cfg.get("url"), str) and isinstance(stored.get("url"), str):
                cfg["url"] = merge_mcp_url_secrets(cfg["url"], stored["url"])
        if cfg.get("command") or not cfg.get("url"):
            return JSONResponse(
                {"ok": False, "error": "只支持探测 HTTP/SSE 端点；stdio 请保存后连接"},
                status_code=400,
            )
        try:
            await asyncio.wait_for(probe_slots.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"ok": False, "code": "mcp_probe_busy", "error": "同时进行的 MCP 探测过多，请稍后再试"},
                status_code=429,
            )
        try:
            result = await probe_remote_server(cfg)
        finally:
            probe_slots.release()
        error = result.get("error")
        tools = list(result.get("tools") or [])
        return JSONResponse({
            "ok": bool(result.get("ok")),
            "tools": tools,
            "tool_count": int(result.get("tool_count") or len(tools)),
            "elapsed_ms": int(result.get("elapsed_ms") or 0),
            "error": redact_sensitive_text(str(error)) if error else None,
        })

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

        # 统一配置事务（Config.add_mcp_server）：候选值 → 持久化 → 发布。
        # 持久化失败时内存与磁盘一致保留旧值，必须先于任何运行资源操作返回。
        try:
            await add_mcp_server(crew, name, cfg)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"持久化失败: {exc}"}, status_code=500)

        return JSONResponse({"ok": True, "servers": _servers_view()}, status_code=201)

    @router.put("/api/mcp/servers/{name}")
    async def update_server(name: str, payload: dict[str, Any] | None = None) -> JSONResponse:
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        payload = payload or {}
        cfg, err = _validate_server_payload(payload)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        # 统一配置事务（Config.update_mcp_server）：候选值 → 持久化 → 发布，
        # 持久化失败内存与磁盘保持旧 command，不会出现"内存已改、磁盘仍旧"。
        try:
            crew.config.update_mcp_server(name, cfg)
        except KeyError:
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
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

        # 统一配置事务（Config.delete_mcp_server）：候选值 → 持久化 → 发布。
        # 持久化失败时内存与磁盘均保留该 server，运行中的实例原样不动，可直接重试。
        try:
            crew.config.delete_mcp_server(name)
        except KeyError:
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"持久化失败: {exc}"}, status_code=500)

        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            try:
                await crew.mcp_manager.remove_server(name)
            except Exception as exc:  # noqa: BLE001
                # 已保存但连接失败：配置（磁盘+内存）保持已删除的新值，如实上报运行
                # 资源操作错误，不回滚伪装成保存失败。恢复策略：状态如实——运行实例
                # 可能残留至进程重启或下一次 reload，配置侧重试 DELETE 幂等（404）。
                return JSONResponse(
                    {"ok": False, "error": f"配置已保存，但移除运行实例失败: {exc}"},
                    status_code=500,
                )

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
