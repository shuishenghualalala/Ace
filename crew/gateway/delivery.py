"""Cron / Agent 响应投递目标解析与发送。

支持插件平台注册的目标，以及 feishu / local / origin 等常用目标。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from crew.gateway.response_filters import apply_text_filters
from crew.gateway.session_context import SessionSource
from crew.state.logging import get_logger

log = get_logger("gateway.delivery")

SenderFn = Callable[[str, str, SessionSource | None], Awaitable[bool]]


@dataclass
class DeliveryTarget:
    """单个投递目标。"""

    platform: str
    chat_id: str | None = None
    is_origin: bool = False
    is_local: bool = False

    @classmethod
    def parse(cls, target: str, origin: SessionSource | None = None) -> "DeliveryTarget":
        """解析 deliver 字符串，如 origin / local / feishu:chat_id。"""
        raw = target.strip()
        lower = raw.lower()
        if lower == "origin":
            if origin is None:
                return cls(platform="local", is_origin=True, is_local=True)
            return cls(
                platform=origin.platform,
                chat_id=origin.chat_id,
                is_origin=True,
            )
        if lower == "local":
            return cls(platform="local", is_local=True)
        if ":" in raw:
            platform, chat_id = raw.split(":", 1)
            return cls(platform=platform.strip().lower(), chat_id=chat_id.strip())
        return cls(platform=lower)


class DeliveryRouter:
    """按平台与 Owner 路由 outbound 文本到已注册 sender。"""

    def __init__(self) -> None:
        self._senders: dict[tuple[str, str], SenderFn] = {}

    def register(self, platform: str, sender: SenderFn, *, owner_account_id: str = "") -> None:
        key = (str(platform or "").strip().lower(), str(owner_account_id or "").strip())
        self._senders[key] = sender

    def unregister(self, platform: str, *, owner_account_id: str = "") -> None:
        """移除一个 Owner 的平台 sender。"""
        key = (str(platform or "").strip().lower(), str(owner_account_id or "").strip())
        self._senders.pop(key, None)

    async def deliver(
        self,
        target: str,
        text: str,
        *,
        origin: SessionSource | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """投递文本到目标，返回 {ok, platform, error?}。"""
        if not text.strip():
            return {"ok": False, "platform": "", "error": "empty text"}
        parsed = DeliveryTarget.parse(target, origin)
        if parsed.is_local:
            log.info("cron deliver local: %s chars", len(text))
            return {"ok": True, "platform": "local"}
        platform = parsed.platform
        chat_id = parsed.chat_id or (origin.chat_id if origin else None)
        owner = str(owner_account_id or "").strip()
        sender = self._senders.get((platform, owner))
        if sender is None and owner in {"", "local", "dev:dev"}:
            sender = self._senders.get((platform, ""))
        if sender is None:
            return {
                "ok": False,
                "platform": platform,
                "error": f"unsupported platform for owner: {platform}:{owner}",
            }
        # IM 出站统一过滤边界：cron 主动下发 / 桌面续聊都汇聚到此，按平台跑出站过滤链
        # （IM 渠道剥离 <thinking> + 全渠道密钥脱敏），避免思考过程/密钥原样发到外部平台。
        text = apply_text_filters(text, {"channel": platform})
        try:
            ok = await sender(chat_id or "", text, origin)
            return {"ok": bool(ok), "platform": platform}
        except Exception as exc:  # noqa: BLE001 — 平台 sender 为任意平台 API 调用，失败面未知；投递边界须吞住并回传错误
            log.exception("投递失败 platform=%s", platform)
            return {"ok": False, "platform": platform, "error": str(exc)}
