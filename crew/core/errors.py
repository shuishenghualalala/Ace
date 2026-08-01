"""统一异常类型。业务模块抛这些异常，上层（agent/gateway）统一捕获转成 error 帧。"""

from __future__ import annotations


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
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category


class ToolError(CrewError):
    """工具执行失败（业务可恢复，会回灌给模型）。"""


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class ConfigError(CrewError):
    """配置缺失或非法。"""
