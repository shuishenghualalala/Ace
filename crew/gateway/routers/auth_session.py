"""Gateway-local authenticated session lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import REMOTE_AUTH_COOKIE, account_from_request
from crew.gateway.logout import LogoutCleanupError, LogoutCoordinator


def create_auth_session_router(coordinator: LogoutCoordinator) -> APIRouter:
    """Expose explicit logout while keeping cleanup policy in one coordinator."""
    router = APIRouter()

    @router.post("/api/auth/logout")
    async def logout(request: Request):
        owner = account_from_request(request).owner_account_id
        try:
            result = await coordinator.logout(owner)
        except LogoutCleanupError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "released": False,
                    "code": "LOGOUT_CLEANUP_FAILED",
                    "error": str(exc),
                },
                status_code=503,
            )
        response = JSONResponse({"ok": True, **result.to_dict()})
        response.delete_cookie(REMOTE_AUTH_COOKIE, path="/", samesite="strict")
        return response

    return router
