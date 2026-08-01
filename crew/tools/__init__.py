"""工具系统：注册表 + 内置工具。

扩展点：新工具 = schema + handler，在 register_builtin_tools 或插件 register(ctx) 里注册。
"""

from crew.tools.registry import Registry, register_builtin_tools

__all__ = ["Registry", "register_builtin_tools"]
