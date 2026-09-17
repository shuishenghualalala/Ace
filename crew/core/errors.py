"""统一异常类型。业务模块抛这些异常，上层（agent/gateway）统一捕获转成 error 帧。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar


class CrewErrorKind(StrEnum):
    """Crew 错误的语义分类。

    枚举穷举保证新错误不会静默落入错误分支；UNKNOWN 兜底未识别情形。
    字符串值即序列化形式（gateway error 帧 / 日志直接可用）。
    """

    STREAM = "stream"
    TIMEOUT = "timeout"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    USAGE_LIMIT = "usage_limit"
    QUOTA_EXCEEDED = "quota_exceeded"
    SERVER_OVERLOADED = "server_overloaded"
    AUTH = "auth"
    FORBIDDEN = "forbidden"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    TOOL = "tool"
    CONFIG = "config"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


# kind ↔ category 双向兼容映射。category 是历史遗留的展示用自由字符串，
# gateway 出站 error 帧仍有消费方；kind 是新的结构化分类。
# 只传 category 构造时映射到 kind，只传 kind 时映射回 category。
_KIND_TO_CATEGORY: dict[CrewErrorKind, str] = {
    CrewErrorKind.STREAM: "connection",
    CrewErrorKind.TIMEOUT: "timeout",
    CrewErrorKind.CONTEXT_WINDOW_EXCEEDED: "provider",
    CrewErrorKind.USAGE_LIMIT: "rate_limit",
    CrewErrorKind.QUOTA_EXCEEDED: "rate_limit",
    CrewErrorKind.SERVER_OVERLOADED: "server",
    CrewErrorKind.AUTH: "auth",
    CrewErrorKind.FORBIDDEN: "forbidden",
    CrewErrorKind.UNSUPPORTED_CAPABILITY: "unsupported_capability",
    CrewErrorKind.INTERNAL: "server",
}
_DEFAULT_CATEGORY = "provider"

_CATEGORY_TO_KIND: dict[str, CrewErrorKind] = {
    "timeout": CrewErrorKind.TIMEOUT,
    "connection": CrewErrorKind.STREAM,
    "auth": CrewErrorKind.AUTH,
    "forbidden": CrewErrorKind.FORBIDDEN,
    "rate_limit": CrewErrorKind.USAGE_LIMIT,
    "server": CrewErrorKind.INTERNAL,
    "unsupported_capability": CrewErrorKind.UNSUPPORTED_CAPABILITY,
    "provider": CrewErrorKind.UNKNOWN,
}


def category_for_kind(kind: CrewErrorKind) -> str:
    """kind 对应的兼容 category 字符串（未映射的 kind 落到默认 "provider"）。"""
    return _KIND_TO_CATEGORY.get(kind, _DEFAULT_CATEGORY)


def kind_for_category(category: str | None) -> CrewErrorKind:
    """category 字符串对应的 kind（未识别 / 空值落到 UNKNOWN）。"""
    if not category:
        return CrewErrorKind.UNKNOWN
    return _CATEGORY_TO_KIND.get(category, CrewErrorKind.UNKNOWN)


# 各家 provider 在「上下文超长」时返回的典型报错关键词（小写匹配）。
# 供 provider 分类与 loop 的溢出检测共用，避免两处清单漂移。
CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "maximum number of tokens",
    "reduce the length",
    "reduce the number of tokens",
    "string too long",
    "prompt is too long",
    "input is too long",
    "context_length_exceeded",
)


class CrewError(Exception):
    """所有 Crew 异常的基类。kind 标识语义分类，is_retryable() 按类级白名单判定。"""

    #: 子类默认 kind：未显式传 kind 时回落到它。
    DEFAULT_KIND: ClassVar[CrewErrorKind] = CrewErrorKind.UNKNOWN

    #: 可重试白名单：流中断 / 超时 / 内部瞬时错误可重试；
    #: 鉴权、配额、取消、上下文超长、不支持的能力等重试无意义。
    RETRYABLE_KINDS: ClassVar[frozenset[CrewErrorKind]] = frozenset({
        CrewErrorKind.STREAM,
        CrewErrorKind.TIMEOUT,
        CrewErrorKind.INTERNAL,
    })

    def __init__(
        self,
        message: str,
        *,
        kind: CrewErrorKind | None = None,
        retry_delay: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind if kind is not None else self.DEFAULT_KIND
        self.retry_delay = retry_delay

    def is_retryable(self) -> bool:
        """按 kind 白名单判定是否瞬时错误（子类可用实例属性覆盖）。"""
        return self.kind in self.RETRYABLE_KINDS


class ProviderError(CrewError):
    """LLM Provider 调用失败。

    kind / status / retry_delay 是结构化分类：kind 为语义分类枚举，status 是
    HTTP 状态码（裸连接错误为 None），retry_delay 是服务端建议的退避秒数
    （Retry-After 头 / body retry_delay 字段，无则 None）。
    retryable 表示瞬时错误（限流/超时/连接/5xx），上层可重试；未显式传入时
    回落到 kind 白名单。category 为兼容别名，与 kind 双向映射，保留给仍读
    字符串的外部消费方。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        category: str | None = None,
        capability: str | None = None,
        kind: CrewErrorKind | None = None,
        status: int | None = None,
        retry_delay: float | None = None,
    ) -> None:
        if kind is None:
            kind = kind_for_category(category)
        if category is None:
            category = category_for_kind(kind)
        super().__init__(message, kind=kind, retry_delay=retry_delay)
        self.retryable = kind in self.RETRYABLE_KINDS if retryable is None else retryable
        self.category = category
        self.status = status
        self.capability = capability

    def is_retryable(self) -> bool:
        """显式传入的 retryable 优先，否则已在构造时按 kind 白名单回落。"""
        return self.retryable


def contains_image_input(value: Any) -> bool:
    """Return whether a provider request payload contains an image block."""
    if isinstance(value, list):
        return any(contains_image_input(item) for item in value)
    if not isinstance(value, dict):
        return False
    block_type = str(value.get("type") or "").strip().lower()
    if block_type in {"image", "image_url", "input_image"}:
        return True
    if "image_url" in value:
        return True
    return any(contains_image_input(item) for item in value.values())


def is_unsupported_image_input_error(
    error: Exception | str,
    *,
    request_has_images: bool,
    status: int | None = None,
) -> bool:
    """Recognize an upstream rejection of image input without swallowing other 400s."""
    if not request_has_images:
        return False
    effective_status = status if status is not None else getattr(error, "status_code", None)
    if effective_status is not None and effective_status not in {400, 415, 422}:
        return False
    message = str(error).lower()
    image_marker = any(
        marker in message
        for marker in ("image_url", "image input", "image inputs", "images", "图片", "图像", "视觉")
    )
    unsupported_marker = any(
        marker in message
        for marker in (
            "do not support",
            "does not support",
            "not support",
            "unsupported",
            "not allowed",
            "invalidparameter",
            "invalid parameter",
            # serde 风格反序列化拒绝（如 DeepSeek 端点 "unknown variant `image_url`,
            # expected `text`"）：端点的消息 schema 根本不认识 image 变体。
            "unknown variant",
            "不支持",
            "不具备",
        )
    )
    return image_marker and unsupported_marker


class ToolError(CrewError):
    """工具执行失败（业务可恢复，会回灌给模型）。"""

    DEFAULT_KIND: ClassVar[CrewErrorKind] = CrewErrorKind.TOOL


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class ConfigError(CrewError):
    """配置缺失或非法。"""

    DEFAULT_KIND: ClassVar[CrewErrorKind] = CrewErrorKind.CONFIG
