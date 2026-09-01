"""通知中心服务：实现 core 的 NotificationCenter 契约。

publish 写库成功后，若有注入的 push 回调则广播 WS 帧（fire-and-forget）。
通知链路的任何失败只记日志，绝不影响来源业务逻辑。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

from crew.core.interfaces import Notification, NotificationCenter

from .store import NotificationStore

log = logging.getLogger(__name__)

# push(owner_account_id, {"kind": "notification", "notification": {...}})
PushFn = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class NotificationCenterService(NotificationCenter):
    """NotificationCenter 的默认实现：SQLite 存储 + 可选 WS 推送。"""

    def __init__(
        self,
        store: NotificationStore,
        push_fn: PushFn | None = None,
    ) -> None:
        self._store = store
        self._push_fn = push_fn

    def set_push_fn(self, push_fn: PushFn | None) -> None:
        """延迟注入推送回调（gateway 装配 ConnectionManager 后调用）。"""
        self._push_fn = push_fn

    def publish(self, notification: Notification) -> Notification:
        try:
            saved = self._store.insert(notification)
        except Exception:  # noqa: BLE001 - 通知写库失败不能影响来源业务逻辑
            log.exception("通知写入失败 source=%s kind=%s", notification.source, notification.kind)
            return notification
        self._broadcast(saved)
        return saved

    def _broadcast(self, notification: Notification) -> None:
        push = self._push_fn
        if push is None:
            return
        payload = {"kind": "notification", "notification": notification.to_dict()}
        try:
            result = push(notification.owner_account_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("通知推送失败 owner=%s", notification.owner_account_id)
            return
        if inspect.isawaitable(result):

            async def _finish() -> None:
                try:
                    await result
                except Exception:  # noqa: BLE001
                    log.exception("通知推送失败 owner=%s", notification.owner_account_id)

            try:
                asyncio.get_running_loop().create_task(_finish())
            except RuntimeError:
                # 无运行中的事件循环（如非 gateway 进程）：推送直接放弃，写库已成功
                if inspect.iscoroutine(result):
                    result.close()

    def list(
        self,
        owner_account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> list[Notification]:
        return self._store.list(
            owner_account_id,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
        )

    def unread_count(self, owner_account_id: str) -> int:
        return self._store.unread_count(owner_account_id)

    def mark_read(self, owner_account_id: str, notification_id: str) -> bool:
        return self._store.mark_read(owner_account_id, notification_id)

    def mark_all_read(self, owner_account_id: str) -> int:
        return self._store.mark_all_read(owner_account_id)

    def mark_read_by_payload(self, source: str, key: str, owner_account_id: str = "") -> int:
        return self._store.mark_read_by_payload(source, key, owner_account_id)

    def clear(self, owner_account_id: str) -> int:
        return self._store.clear(owner_account_id)
