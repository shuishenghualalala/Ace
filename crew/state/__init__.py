"""会话与状态层：配置加载、日志、SQLite 会话存储、Workspace Home。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "Config": ("crew.state.config", "Config"),
    "load_config": ("crew.state.config", "load_config"),
    "get_crew_home": ("crew.state.home", "get_crew_home"),
    "get_task_workspace_root": ("crew.state.home", "get_task_workspace_root"),
    "task_workspace_path": ("crew.state.home", "task_workspace_path"),
    "agent_workspace_path": ("crew.state.home", "agent_workspace_path"),
    "ensure_crew_home": ("crew.state.home", "ensure_crew_home"),
    "load_soul_md": ("crew.state.home", "load_soul_md"),
    "load_memory_md": ("crew.state.home", "load_memory_md"),
    "load_user_md": ("crew.state.home", "load_user_md"),
    "get_logger": ("crew.state.logging", "get_logger"),
    "setup_logging": ("crew.state.logging", "setup_logging"),
    "Workspace": ("crew.state.models", "Workspace"),
    "SQLiteSessionStore": ("crew.state.session_store", "SQLiteSessionStore"),
    "SQLiteWorkspaceStore": ("crew.state.workspace_store", "SQLiteWorkspaceStore"),
}

__all__ = [
    "Config",
    "SQLiteSessionStore",
    "SQLiteWorkspaceStore",
    "Workspace",
    "agent_workspace_path",
    "ensure_crew_home",
    "get_crew_home",
    "get_logger",
    "get_task_workspace_root",
    "load_config",
    "load_memory_md",
    "load_soul_md",
    "load_user_md",
    "setup_logging",
    "task_workspace_path",
]


def __getattr__(name: str) -> Any:
    """Resolve public state exports without importing the whole state graph."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
