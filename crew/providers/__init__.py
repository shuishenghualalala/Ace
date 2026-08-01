"""LLM Provider 层。当前实现 OpenAI 兼容与 Anthropic Messages 接口。

扩展点：新增厂商只需新建一个实现 LLMProvider 的类，在 app.py 注册即可。
"""

from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider

__all__ = ["AnthropicProvider", "OpenAIProvider"]
