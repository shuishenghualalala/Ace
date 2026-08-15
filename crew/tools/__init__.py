"""工具系统：注册表 + 内置工具。

扩展点：新工具 = schema + handler，在 register_builtin_tools 或插件 register(ctx) 里注册。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["Registry", "register_builtin_tools"]


def __getattr__(name: str) -> Any:
    """Load registry exports lazily so leaf tools remain independently importable."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("crew.tools.registry"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
