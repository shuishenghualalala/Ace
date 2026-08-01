"""DeliveryRouter 出站统一过滤。

cron 主动下发和桌面续聊都经此边界，按平台 IM 剥离 <thinking> 并执行全渠道脱敏。
"""

from __future__ import annotations

import asyncio

from crew.gateway.delivery import DeliveryRouter


def _router_with_capture(platform: str):
    router = DeliveryRouter()
    seen: dict = {}

    async def _sender(chat_id, text, origin):
        seen["chat_id"] = chat_id
        seen["text"] = text
        return True

    router.register(platform, _sender)
    return router, seen


def test_deliver_strips_thinking_for_im_platforms():
    """IM 渠道主动下发时剥离 <thinking>，思考过程不外发。"""
    for platform in ("feishu", "dingtalk", "wecom"):
        router, seen = _router_with_capture(platform)
        out = asyncio.run(router.deliver(f"{platform}:u1", "<thinking>内部推理</thinking>最终答案"))
        assert out["ok"] and out["platform"] == platform
        assert "<thinking>" not in seen["text"], f"{platform} 应剥离 thinking"
        assert "最终答案" in seen["text"]


def test_deliver_empty_text_short_circuits_before_sender():
    router, seen = _router_with_capture("testchat")
    out = asyncio.run(router.deliver("testchat:u1", "   "))
    assert out["ok"] is False and "empty" in out["error"]
    assert "text" not in seen  # 空文本不调用 sender，也不进过滤
