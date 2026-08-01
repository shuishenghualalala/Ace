"""插件系统：生命周期钩子的注册与分发。"""

from crew.plugins.manager import (
    LLM_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    LoadedPlugin,
    PluginContext,
    PluginManager,
    PluginManifest,
    RequestMiddlewareResult,
    TerminalOutcome,
    TOOL_EXECUTION_MIDDLEWARE,
    TOOL_REQUEST_MIDDLEWARE,
)
from crew.plugins.builtin import LoggingPlugin

__all__ = [
    "PluginManager",
    "PluginContext",
    "PluginManifest",
    "RequestMiddlewareResult",
    "TerminalOutcome",
    "TOOL_REQUEST_MIDDLEWARE",
    "TOOL_EXECUTION_MIDDLEWARE",
    "LLM_REQUEST_MIDDLEWARE",
    "LLM_EXECUTION_MIDDLEWARE",
    "LoadedPlugin",
    "LoggingPlugin",
]
