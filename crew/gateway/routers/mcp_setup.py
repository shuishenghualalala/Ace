"""CUA Driver MCP 一键安装路由。

本地桌面端为单用户场景，安装 CUA Driver（装二进制 / 启 daemon / 写本地 config）是本地操作，
故本路由对**所有登录用户**开放（登录校验由 gateway require_gateway_login 中间件保证，未登录 401）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crew.tools.cua_setup import CuaDriverSetupService, task_to_dict


# 全局单例，跨请求共享任务状态
_cua_setup_service: CuaDriverSetupService | None = None


def _get_service() -> CuaDriverSetupService:
    global _cua_setup_service
    if _cua_setup_service is None:
        _cua_setup_service = CuaDriverSetupService()
    return _cua_setup_service


def create_mcp_setup_router(crew) -> APIRouter:
    router = APIRouter()
    service = _get_service()

    @router.get("/api/mcp/cua-driver/status")
    async def cua_driver_status() -> JSONResponse:
        try:
            result = await service.status(crew.registry)
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/api/mcp/cua-driver/setup")
    async def cua_driver_setup(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        force_reinstall = bool(payload.get("force_reinstall", False))
        start_daemon = bool(payload.get("start_daemon", True))

        try:
            task = service.start_setup(
                crew=crew,
                force_reinstall=force_reinstall,
                start_daemon=start_daemon,
            )
            return JSONResponse({"ok": True, "task_id": task.task_id, "status": task.status})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/api/mcp/cua-driver/setup/{task_id}")
    async def cua_driver_setup_status(task_id: str) -> JSONResponse:
        task = service.get_task(task_id)
        if task is None:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        return JSONResponse({"ok": True, **task_to_dict(task)})

    @router.post("/api/mcp/cua-driver/setup/{task_id}/cancel")
    async def cua_driver_setup_cancel(task_id: str) -> JSONResponse:
        ok = await service.cancel_task(task_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "任务不存在或已结束"}, status_code=400)
        return JSONResponse({"ok": True, "task_id": task_id, "status": "cancelled"})

    return router
