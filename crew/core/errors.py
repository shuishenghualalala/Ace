"""统一异常类型。业务模块抛这些异常，上层（agent/gateway）统一捕获转成 error 帧。"""

from __future__ import annotations

from typing import Any


class CrewError(Exception):
    """所有 Crew 异常的基类。"""


class ProviderError(CrewError):
    """LLM Provider 调用失败。

    retryable=True 表示瞬时错误（限流/超时/连接/5xx），上层可重试；
    False 表示业务/鉴权类错误，重试无意义。
    category 供 gateway 出站 error 帧分类展示。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        category: str = "provider",
        capability: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category
        self.capability = capability


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
            "不支持",
            "不具备",
        )
    )
    return image_marker and unsupported_marker


class ToolError(CrewError):
    """工具执行失败（业务可恢复，会回灌给模型）。"""


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class ConfigError(CrewError):
    """配置缺失或非法。"""
