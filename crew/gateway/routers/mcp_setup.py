"""CUA Driver MCP 一键安装路由。

安装 CUA Driver 会执行安装器、启动 daemon 并修改全局 MCP 配置，因此状态、安装和
取消入口都要求 Gateway 管理员。
"""

from __future__ import annotations

import getpass
import hashlib
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import (
    AuthenticationError,
    account_from_request,
    require_admin,
)
from crew.gateway.helpers import safe_public_error
from crew.security.audit import AuditEvent
from crew.security.context import SecurityContext
from crew.security.launch import current_process_launch, issue_process_launch
from crew.security.models import (
    PermissionProfile,
    PermissionProfileKind,
    SandboxablePreference,
)
from crew.tools.cua_setup import CuaDriverSetupService, task_to_dict

# 全局单例，跨请求共享任务状态
_cua_setup_service: CuaDriverSetupService | None = None
_LOGGER = logging.getLogger(__name__)


def _get_service() -> CuaDriverSetupService:
    global _cua_setup_service
    if _cua_setup_service is None:
        _cua_setup_service = CuaDriverSetupService()
    return _cua_setup_service


def create_mcp_setup_router(crew) -> APIRouter:
    router = APIRouter()
    service = _get_service()

    def _admin_or_403(
        request: Request,
        *,
        action: str,
        task_id: str = "",
    ) -> JSONResponse | None:
        account = account_from_request(request)
        try:
            require_admin(account, crew.config)
        except AuthenticationError as exc:
            canonical = f"{request.method.upper()}\n{action}\n{task_id}".encode()
            try:
                os_user = getpass.getuser()
            except (OSError, RuntimeError):
                os_user = "unknown"
            event = AuditEvent(
                event_id=uuid4().hex,
                os_user_hash=hashlib.sha256(os_user.encode()).hexdigest(),
                owner_account_id=account.owner_account_id,
                workspace_id="gateway",
                session_id="",
                task_id="",
                request_id="",
                action_type="cua_setup_admin_denied",
                normalized_action_hash=hashlib.sha256(canonical).hexdigest(),
                rule_id="",
                rule_scope="",
                permission_profile_hash="",
                additional_permissions_summary="",
                decision="deny",
                decision_source="gateway_admin_policy",
                sandbox_backend="",
                capabilities=(),
                network_target_summary="",
                exit_code=None,
                stable_error_code="gateway_admin_required",
                tool_name="gateway:cua_setup",
                action_summary=f"CUA setup {action} denied",
                action_detail=f"{request.method.upper()} action={action}",
            )
            try:
                crew.security_audit.record(event)
            except Exception:  # noqa: BLE001 - denial remains fail closed
                _LOGGER.exception("failed to record CUA setup admin denial audit")
            return JSONResponse(
                {"ok": False, "error": safe_public_error(exc, "权限不足")},
                status_code=403,
            )
        return None

    def _audit_mutation_or_503(
        request: Request,
        *,
        action: str,
        task_id: str = "",
    ) -> JSONResponse | None:
        account = account_from_request(request)
        canonical = (
            f"{request.method.upper()}\n{action}\n"
            f"{account.owner_account_id}\n{task_id}"
        ).encode()
        try:
            os_user = getpass.getuser()
        except (OSError, RuntimeError):
            os_user = "unknown"
        event = AuditEvent(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(os_user.encode()).hexdigest(),
            owner_account_id=account.owner_account_id,
            workspace_id="gateway",
            session_id="",
            task_id=task_id,
            request_id="",
            action_type="cua_setup_admin_action",
            normalized_action_hash=hashlib.sha256(canonical).hexdigest(),
            rule_id="gateway-admin",
            rule_scope="global",
            permission_profile_hash="",
            additional_permissions_summary="",
            decision="allow",
            decision_source="gateway_admin_policy",
            sandbox_backend="",
            capabilities=("cua_setup",),
            network_target_summary="",
            exit_code=None,
            stable_error_code="",
            tool_name="gateway:cua_setup",
            action_summary=f"CUA setup {action} authorized",
            action_detail=f"{request.method.upper()} action={action}",
        )
        try:
            crew.security_audit.record(event)
        except Exception:  # noqa: BLE001 - sensitive mutation must fail closed
            _LOGGER.exception("failed to record CUA setup mutation audit")
            return JSONResponse(
                {"ok": False, "error": "security audit unavailable"},
                status_code=503,
            )
        return None

    def _admin_process_launch(request: Request):
        account = account_from_request(request)
        return issue_process_launch(
            SecurityContext(
                os_user=getpass.getuser(),
                owner_account_id=account.owner_account_id,
                workspace_id="gateway",
                workspace_root=None,
                session_id=f"cua-setup:{account.owner_account_id}",
                request_id="",
                task_id="",
                cwd=None,
            ),
            PermissionProfile(PermissionProfileKind.DISABLED),
            sandbox_preference=SandboxablePreference.FORBID,
            sandbox_system_surface="cua-driver-admin",
        )

    @router.get("/api/mcp/cua-driver/status")
    async def cua_driver_status(request: Request) -> JSONResponse:
        denied = _admin_or_403(request, action="status")
        if denied is not None:
            return denied
        token = current_process_launch.set(_admin_process_launch(request))
        try:
            result = await service.status(crew.registry)
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "CUA Driver 操作失败")}, status_code=500)
        finally:
            current_process_launch.reset(token)

    @router.post("/api/mcp/cua-driver/setup")
    async def cua_driver_setup(
        request: Request,
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        denied = _admin_or_403(request, action="setup")
        if denied is not None:
            return denied
        audit_denied = _audit_mutation_or_503(request, action="setup")
        if audit_denied is not None:
            return audit_denied
        payload = payload or {}
        if set(payload) - {"force_reinstall", "start_daemon"}:
            return JSONResponse(
                {"ok": False, "error": "unsupported CUA setup field"},
                status_code=400,
            )
        for field in ("force_reinstall", "start_daemon"):
            if field in payload and not isinstance(payload[field], bool):
                return JSONResponse(
                    {"ok": False, "error": f"{field} must be boolean"},
                    status_code=400,
                )
        force_reinstall = payload.get("force_reinstall", False)
        start_daemon = payload.get("start_daemon", True)

        try:
            task = service.start_setup(
                crew=crew,
                force_reinstall=force_reinstall,
                start_daemon=start_daemon,
                process_launch=_admin_process_launch(request),
            )
            return JSONResponse({"ok": True, "task_id": task.task_id, "status": task.status})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "CUA Driver 操作失败")}, status_code=500)

    @router.get("/api/mcp/cua-driver/setup/{task_id}")
    async def cua_driver_setup_status(
        task_id: str,
        request: Request,
    ) -> JSONResponse:
        denied = _admin_or_403(request, action="task_status", task_id=task_id)
        if denied is not None:
            return denied
        task = service.get_task(task_id)
        if task is None:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        return JSONResponse({"ok": True, **task_to_dict(task)})

    @router.post("/api/mcp/cua-driver/setup/{task_id}/cancel")
    async def cua_driver_setup_cancel(
        task_id: str,
        request: Request,
    ) -> JSONResponse:
        denied = _admin_or_403(request, action="cancel", task_id=task_id)
        if denied is not None:
            return denied
        audit_denied = _audit_mutation_or_503(
            request,
            action="cancel",
            task_id=task_id,
        )
        if audit_denied is not None:
            return audit_denied
        ok = await service.cancel_task(task_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "任务不存在或已结束"}, status_code=400)
        return JSONResponse({"ok": True, "task_id": task_id, "status": "cancelled"})

    return router
