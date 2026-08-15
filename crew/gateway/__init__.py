"""消息网关：FastAPI + WebSocket 接入层。用于 Gateway / Jiuwen GatewayServer。"""

__all__ = [
    "ChannelManager",
    "HookRegistry",
    "ResponseFilterChain",
    "SessionContext",
    "SessionSource",
    "build_session_context_prompt",
    "build_session_key",
    "hook_registry",
    "response_filter_chain",
]

_LAZY_EXPORTS = {
    "ChannelManager": ("crew.gateway.channel_manager", "ChannelManager"),
    "HookRegistry": ("crew.gateway.hooks", "HookRegistry"),
    "hook_registry": ("crew.gateway.hooks", "hook_registry"),
    "ResponseFilterChain": ("crew.gateway.response_filters", "ResponseFilterChain"),
    "response_filter_chain": ("crew.gateway.response_filters", "response_filter_chain"),
    "SessionContext": ("crew.gateway.session_context", "SessionContext"),
    "SessionSource": ("crew.gateway.session_context", "SessionSource"),
    "build_session_context_prompt": (
        "crew.gateway.session_context",
        "build_session_context_prompt",
    ),
    "build_session_key": ("crew.gateway.session_context", "build_session_key"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    from importlib import import_module

    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
