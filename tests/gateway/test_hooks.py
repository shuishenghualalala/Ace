"""测试网关钩子系统。"""

import pytest

from crew.gateway.hooks import HookRegistry


@pytest.mark.asyncio
async def test_hook_registry_register_and_emit():
    """测试注册与触发钩子。"""
    registry = HookRegistry()
    called = []

    def sync_handler(event_type: str, context: dict) -> None:
        called.append(("sync", event_type, context.get("value")))

    async def async_handler(event_type: str, context: dict) -> None:
        called.append(("async", event_type, context.get("value")))

    registry.register("test:event", sync_handler)
    registry.register("test:event", async_handler)

    await registry.emit("test:event", {"value": 42})

    assert len(called) == 2
    assert ("sync", "test:event", 42) in called
    assert ("async", "test:event", 42) in called


@pytest.mark.asyncio
async def test_hook_registry_wildcard():
    """测试通配符匹配。"""
    registry = HookRegistry()
    called = []

    def wildcard_handler(event_type: str, context: dict) -> None:
        called.append(event_type)

    def exact_handler(event_type: str, context: dict) -> None:
        called.append(f"exact:{event_type}")

    registry.register("command:*", wildcard_handler)
    registry.register("command:reset", exact_handler)

    await registry.emit("command:reset", {})

    # 精确匹配和通配符都应触发
    assert "command:reset" in called
    assert "exact:command:reset" in called


@pytest.mark.asyncio
async def test_hook_registry_error_handling():
    """测试钩子异常不中断其他钩子。"""
    registry = HookRegistry()
    called = []

    def failing_handler(event_type: str, context: dict) -> None:
        raise ValueError("intentional failure")

    def success_handler(event_type: str, context: dict) -> None:
        called.append("success")

    registry.register("test:event", failing_handler)
    registry.register("test:event", success_handler)

    # 不应抛异常，第二个 handler 仍然执行
    await registry.emit("test:event", {})

    assert "success" in called


@pytest.mark.asyncio
async def test_hook_registry_emit_collect():
    """测试收集 handler 返回值。"""
    registry = HookRegistry()

    def handler1(event_type: str, context: dict) -> int:
        return 1

    def handler2(event_type: str, context: dict) -> int:
        return 2

    def handler_none(event_type: str, context: dict) -> None:
        return None

    registry.register("test:collect", handler1)
    registry.register("test:collect", handler2)
    registry.register("test:collect", handler_none)

    results = await registry.emit_collect("test:collect", {})

    assert results == [1, 2]  # None 不收集


@pytest.mark.asyncio
async def test_hook_registry_unregister():
    """测试移除钩子。"""
    registry = HookRegistry()
    called = []

    def handler(event_type: str, context: dict) -> None:
        called.append("called")

    registry.register("test:event", handler)
    await registry.emit("test:event", {})
    assert len(called) == 1

    registry.unregister("test:event", handler)
    await registry.emit("test:event", {})
    assert len(called) == 1  # 未再次调用


@pytest.mark.asyncio
async def test_hook_registry_clear():
    """测试清空钩子。"""
    registry = HookRegistry()
    called = []

    def handler(event_type: str, context: dict) -> None:
        called.append("called")

    registry.register("test:event", handler)
    registry.clear("test:event")
    await registry.emit("test:event", {})

    assert len(called) == 0
