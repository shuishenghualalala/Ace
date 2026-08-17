"""响应过滤器：链式处理 Agent 响应文本。

用于 gateway/response_filters.py，提供注册 → 依次执行 → 返回修改后文本的机制。

使用场景：
- 移除平台不支持的 markdown 格式
- 替换敏感信息占位符
- 截断超长响应
- 添加平台特定格式化

过滤器接收 (text, context) 并返回修改后的文本。
context 包含 platform、session_id 等元信息，供过滤器根据平台/会话调整行为。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from crew.state.logging import get_logger
from crew.tools.redact import redact_sensitive_display_text

log = get_logger("gateway.response_filters")

# 过滤器签名：(text: str, context: dict) -> str
ResponseFilter = Callable[[str, dict[str, Any]], str]

# 安全过滤器（redact_secrets）执行失败时的兜底占位：脱敏是出站安全基线，
# 一旦它自身异常，绝不能把可能含密钥的原文继续往下传——直接截断为占位。
REDACT_FAILURE_PLACEHOLDER = "[该消息因安全过滤失败已被截断]"
# 标记为「安全关键」的过滤器名：这些过滤器异常时不返回原输入，而是返回安全占位。
_SAFETY_CRITICAL_FILTERS = frozenset({"redact_secrets"})


class ResponseFilterChain:
    """响应过滤器链。按注册顺序依次执行所有过滤器。"""

    def __init__(self) -> None:
        self._filters: list[tuple[str, ResponseFilter]] = []

    def register(self, name: str, filter_fn: ResponseFilter) -> None:
        """注册一个过滤器。name 仅用于日志。"""
        self._filters.append((name, filter_fn))
        log.debug("注册响应过滤器: %s", name)

    def unregister(self, name: str) -> bool:
        """移除指定名称的过滤器，返回是否成功。"""
        for i, (n, _) in enumerate(self._filters):
            if n == name:
                self._filters.pop(i)
                return True
        return False

    def apply(self, text: str, context: dict[str, Any] | None = None) -> str:
        """依次应用所有过滤器，返回最终文本。

        过滤器异常会被捕获并记录，该过滤器返回原始输入，继续执行后续过滤器。
        """
        if context is None:
            context = {}

        current = text
        for name, filter_fn in self._filters:
            try:
                current = filter_fn(current, context)
            except Exception:
                log.exception("响应过滤器执行异常: %s", name)
                # 安全关键过滤器（如 redact_secrets）失败时，不能把可能含密钥的原文
                # 继续往下传——直接用安全占位替换，避免泄露。其余过滤器保持「继续用
                # 当前文本」的宽容行为。
                if name in _SAFETY_CRITICAL_FILTERS:
                    return REDACT_FAILURE_PLACEHOLDER
        return current


# 全局单例
response_filter_chain = ResponseFilterChain()


# ---------------------------------------------------------------------------
# 内置过滤器示例（可选启用）
# ---------------------------------------------------------------------------

# 仅对 IM 文本渠道剥离内联 <thinking> 标签：这些渠道没有独立渲染「思考过程」的能力，
# 模型偶发把推理内联进正文时会变成噪声。桌面/Web 用 <details> 卡片单独渲染思考块，
# 应保留原文，故不在剥离之列。
_IM_TEXT_CHANNELS = frozenset({"feishu", "dingtalk", "wecom"})


def strip_thinking_tags(text: str, context: dict[str, Any]) -> str:
    """移除 <thinking>...</thinking> 标签及内容。

    仅对 IM 文本渠道生效（context['channel'] 命中 _IM_TEXT_CHANNELS）；
    桌面/Web/MCP 等富 UI 渠道原样返回，保留思考过程供独立渲染。
    """
    channel = (context.get("channel") or context.get("platform") or "").lower()
    if channel not in _IM_TEXT_CHANNELS:
        return text
    # 非贪婪匹配，移除所有 <thinking> 块
    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def truncate_long_response(text: str, context: dict[str, Any]) -> str:
    """截断超长响应（某些平台有消息长度限制）。

    从 context["max_length"] 读取最大长度，默认 4000。
    """
    max_length = context.get("max_length", 4000)
    if len(text) <= max_length:
        return text
    truncated = text[:max_length - 50]
    return truncated + "\n\n[响应过长，已截断]"


def normalize_line_breaks(text: str, context: dict[str, Any]) -> str:
    """规范化换行：将连续 3+ 个换行压缩为 2 个（保持段落分隔）。"""
    return re.sub(r"\n{3,}", "\n\n", text)


# ---------------------------------------------------------------------------
# 安全基线过滤器（P0 A1）与静默回复检测（P0 A2，业务层调用）
# ---------------------------------------------------------------------------

_SILENCE_NARRATION = re.compile(
    r"^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$"
    r"|^[\s*_~`]*[\U0001F507\.\u2026]+[\s*_~`]*$",
    re.IGNORECASE,
)

_NO_REPLY_MARKERS = frozenset({"NO_REPLY", "NOREPLY", "NO REPLY"})


def redact_secrets(text: str, context: dict[str, Any]) -> str:
    """Apply the shared display-boundary redactor to model output."""
    return redact_sensitive_display_text(text)


def is_silent_reply(text: str | None) -> bool:
    """判断是否应跳过出站推送的空/静默回复（业务决策，不走过滤器链）。"""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.upper() in _NO_REPLY_MARKERS:
        return True
    return len(stripped) <= 64 and bool(_SILENCE_NARRATION.match(stripped))


def apply_text_filters(text: str, context: dict[str, Any] | None = None) -> str:
    """对出站文本应用全局过滤器链。"""
    return response_filter_chain.apply(text, context or {})


# 默认启用密钥过滤（用于 出站安全基线）
response_filter_chain.register("redact_secrets", redact_secrets)
# 默认启用 IM 内联 thinking 剥离（函数内按 channel 自门控，桌面/Web 不受影响）
response_filter_chain.register("strip_thinking_tags", strip_thinking_tags)
