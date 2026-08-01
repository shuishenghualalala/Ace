"""网关生命周期钩子系统。

用于 gateway/hooks.py 的事件驱动机制，但简化实现：
- 不从文件系统动态发现（Crew 从 ~/.Crew/hooks/ 加载）
- 直接注册 Python 函数作为 handler
- 支持同步与异步 handler
- 错误静默记录，不阻塞主流程

事件类型：
  - gateway:startup     -- 网关启动
  - gateway:shutdown    -- 网关关闭
  - session:start       -- 新会话创建（第一条消息）
  - session:end         -- 会话结束（用户删除会话）
  - agent:start         -- Agent 开始处理消息
  - agent:end           -- Agent 处理完成
  - channel:state_change -- 渠道运行态变化（启动/停止/错误）

通配符机制：注册 "command:*" 这类 "<base>:*" 的 handler 会匹配所有
"<base>:..." 事件（见 _resolve_handlers）。当前无内置事件使用该机制，
保留供插件自定义事件使用。

Context dict 传递给 handler 的典型字段：
  platform     -- 来源平台名（"telegram" / "web" / "local"）
  user_id      -- 发送者平台 ID
  chat_id      -- 聊天 ID
  thread_id    -- 线程/主题 ID（空字符串表示非线程）
  chat_type    -- "dm" / "group" / "channel" / "thread"
  session_id   -- Crew 会话 ID
  message      -- 入站消息文本（截断到 500 字符）

agent:end 额外添加：
  response     -- Agent 响应文本（截断到 500 字符）
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from crew.state.logging import get_logger

log = get_logger("gateway.hooks")

# Handler 可以是同步或异步函数
HookHandler = Callable[[str, dict[str, Any]], Any | Awaitable[Any]]


class HookRegistry:
    """钩子注册表：注册、触发事件钩子。

    用法：
        registry = HookRegistry()
        registry.register("agent:start", my_handler)
        await registry.emit("agent:start", {"session_id": "...", ...})
    """

    def __init__(self) -> None:
        # event_type -> [handler_fn, ...]
        self._handlers: dict[str, list[HookHandler]] = {}

    def register(self, event_type: str, handler: HookHandler) -> None:
        """注册一个 handler 到指定事件类型。

        event_type 支持通配符，例如 "command:*" 匹配所有 "command:..." 事件。
        """
        self._handlers.setdefault(event_type, []).append(handler)
        log.debug("注册钩子: %s", event_type)

    def unregister(self, event_type: str, handler: HookHandler) -> bool:
        """移除指定 handler，返回是否成功。"""
        handlers = self._handlers.get(event_type, [])
        try:
            handlers.remove(handler)
            return True
        except ValueError:
            return False

    def clear(self, event_type: str | None = None) -> None:
        """清空钩子。event_type=None 时清空所有。"""
        if event_type is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event_type, None)

    def _resolve_handlers(self, event_type: str) -> list[HookHandler]:
        """返回所有应对 event_type 触发的 handler。

        精确匹配优先，然后通配符匹配（例如 "command:*" 匹配 "command:reset"）。
        """
        handlers = list(self._handlers.get(event_type, []))
        if ":" in event_type:
            base = event_type.split(":")[0]
            wildcard_key = f"{base}:*"
            handlers.extend(self._handlers.get(wildcard_key, []))
        return handlers

    async def emit(self, event_type: str, context: dict[str, Any] | None = None) -> None:
        """触发事件，执行所有已注册 handler，丢弃返回值。

        支持通配符匹配："command:*" 注册的 handler 会对所有 "command:..." 事件触发。
        注册为 "agent" 的 handler 不会匹配 "agent:start" — 只有精确匹配或显式通配符。

        Args:
            event_type: 事件标识（例如 "agent:start"）
            context:    可选的事件特定数据字典
        """
        if context is None:
            context = {}

        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                # 支持同步与异步 handler
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("钩子执行异常: event=%s", event_type)

    async def emit_collect(
        self,
        event_type: str,
        context: dict[str, Any] | None = None,
    ) -> list[Any]:
        """触发 handler 并收集非 None 返回值（按顺序）。

        类似 emit，但捕获每个 handler 的返回值。用于决策型钩子（例如 command:* 策略想要
        在常规调度前 允许/拒绝/重写命令）。

        单个 handler 的异常会被记录但不会中止剩余 handler。
        """
        if context is None:
            context = {}

        results: list[Any] = []
        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception:
                log.exception("钩子执行异常: event=%s", event_type)
        return results


# 全局单例，供 gateway 使用
hook_registry = HookRegistry()
