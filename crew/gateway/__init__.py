"""消息网关：FastAPI + WebSocket 接入层。用于 Gateway / Jiuwen GatewayServer。"""

from crew.gateway.channel_manager import ChannelManager
from crew.gateway.hooks import HookRegistry, hook_registry
from crew.gateway.response_filters import ResponseFilterChain, response_filter_chain
from crew.gateway.session_context import (
    SessionContext,
    SessionSource,
    build_session_context_prompt,
    build_session_key,
)

__all__ = [
    "ChannelManager",
    "HookRegistry",
    "hook_registry",
    "ResponseFilterChain",
    "response_filter_chain",
    "SessionContext",
    "SessionSource",
    "build_session_context_prompt",
    "build_session_key",
]
