"""Cron 投递目标解析测试。"""

import pytest

from crew.gateway.delivery import DeliveryRouter, DeliveryTarget
from crew.gateway.session_context import SessionSource


def test_delivery_target_parse_origin():
    origin = SessionSource(platform="feishu", chat_id="oc_1", chat_type="group")
    target = DeliveryTarget.parse("origin", origin)
    assert target.is_origin is True
    assert target.platform == "feishu"
    assert target.chat_id == "oc_1"


def test_delivery_target_parse_platform_chat():
    target = DeliveryTarget.parse("feishu:user123")
    assert target.platform == "feishu"
    assert target.chat_id == "user123"


@pytest.mark.asyncio
async def test_delivery_router_origin_uses_source():
    calls: list[tuple[str, str, str | None]] = []

    async def sender(chat_id: str, text: str, origin: SessionSource | None) -> bool:
        calls.append((chat_id, text, origin.platform if origin else None))
        return True

    origin = SessionSource(platform="testchat", chat_id="line-1", chat_type="dm")
    router = DeliveryRouter()
    router.register("testchat", sender)

    result = await router.deliver("origin", "cron result", origin=origin)

    assert result["ok"] is True
    assert result["platform"] == "testchat"
    assert calls == [("line-1", "cron result", "testchat")]


@pytest.mark.asyncio
async def test_delivery_router_mock_sender():
    calls: list[tuple[str, str]] = []

    async def sender(chat_id: str, text: str, _origin) -> bool:
        calls.append((chat_id, text))
        return True

    router = DeliveryRouter()
    router.register("feishu", sender)
    result = await router.deliver("feishu:oc_99", "cron result")
    assert result["ok"] is True
    assert calls == [("oc_99", "cron result")]
