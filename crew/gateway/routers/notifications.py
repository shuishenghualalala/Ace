"""通知中心 REST API：列表 / 未读数 / 已读 / 全部已读 / 清空。

WS 实时推送经 ConnectionManager.notify_owner 广播，本路由负责兜底拉取与已读管理。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from crew.gateway.auth import account_from_request


def create_notifications_router(crew) -> APIRouter:
    router = APIRouter(prefix="/api/notifications", tags=["notifications"])

    def _owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    @router.get("")
    async def list_notifications(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        unread_only: bool = Query(False),
    ):
        owner = _owner(request)
        notifications = crew.notifications.list(
            owner,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
        )
        return {
            "notifications": [item.to_dict() for item in notifications],
            "unread_count": crew.notifications.unread_count(owner),
        }

    @router.get("/unread-count")
    async def unread_count(request: Request):
        return {"unread_count": crew.notifications.unread_count(_owner(request))}

    @router.post("/{notification_id}/read")
    async def mark_read(notification_id: str, request: Request):
        crew.notifications.mark_read(_owner(request), notification_id)
        return {"ok": True}

    @router.post("/read-all")
    async def mark_all_read(request: Request):
        crew.notifications.mark_all_read(_owner(request))
        return {"ok": True}

    @router.delete("")
    async def clear(request: Request):
        crew.notifications.clear(_owner(request))
        return {"ok": True}

    return router
