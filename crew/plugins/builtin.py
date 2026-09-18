"""内置插件示例：把工具调用写进日志。供同事参考如何写插件。"""

from __future__ import annotations

from typing import Any

from crew.core.interfaces import Plugin
from crew.core.types import ToolCall, ToolResult
from crew.state.logging import get_logger

log = get_logger("plugin.logging")

# file_write 等工具的 content 可达数千字；全量打日志会拖慢控制台并放大编码风险。
_ARGS_PREVIEW_CHARS = 200


def _preview_args(arguments: Any, limit: int = _ARGS_PREVIEW_CHARS) -> str:
    """把工具入参压成短预览，避免把整份 plan/HTML 打进控制台。"""
    text = arguments if isinstance(arguments, str) else repr(arguments)
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


class LoggingPlugin(Plugin):
    name = "logging"

    async def pre_tool_call(self, tool_call: ToolCall) -> None:
        if tool_call.name.startswith("browser_"):
            # Browser arguments may contain passwords, prompt text, signed
            # URLs, upload paths, or other page secrets. Tool-specific safe
            # labels are emitted elsewhere; raw browser values never belong in
            # the process log.
            log.info("[工具调用] %s args=<browser_args_redacted>", tool_call.name)
            return
        log.info("[工具调用] %s args=%s", tool_call.name, _preview_args(tool_call.arguments))

    async def post_tool_call(self, tool_call: ToolCall, result: ToolResult) -> None:
        flag = "ERR" if result.is_error else "OK"
        if tool_call.name.startswith("browser_"):
            # Snapshot, Console, URLs and form validation errors are all
            # untrusted page content and can contain credentials.
            log.info("[工具结果:%s] %s -> <browser_result_redacted>", flag, tool_call.name)
            return
        preview = result.content[:120].replace("\n", " ")
        log.info("[工具结果:%s] %s -> %s", flag, tool_call.name, preview)
