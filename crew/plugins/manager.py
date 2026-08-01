"""插件管理器：聚合多个 Plugin，按钩子顺序分发。

Agent 内核只跟 PluginManager 交互，不感知具体有哪些插件。
插件采用以下目录接口：

plugins/<plugin-name>/
  plugin.yaml
  __init__.py        # 必须提供 register(ctx)
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal

import yaml

from crew.core.interfaces import Plugin
from crew.core.types import Message, ToolCall, ToolResult
from crew.tools.redact import redact_sensitive_text
from crew.tools.registry import Registry
from crew.state.logging import get_logger

log = get_logger("plugins")
_NS_PARENT = "crew_runtime_plugins"

OBSERVER_SCHEMA_VERSION = "crew.observer.v1"
MIDDLEWARE_SCHEMA_VERSION = "crew.middleware.v1"

TOOL_REQUEST_MIDDLEWARE = "tool_request"
TOOL_EXECUTION_MIDDLEWARE = "tool_execution"
LLM_REQUEST_MIDDLEWARE = "llm_request"
LLM_EXECUTION_MIDDLEWARE = "llm_execution"
TerminalOutcome = Literal["completed", "failed", "interrupted"]
_TERMINAL_ERROR_SUMMARY_LIMIT = 512
VALID_MIDDLEWARE = {
    TOOL_REQUEST_MIDDLEWARE,
    TOOL_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    LLM_EXECUTION_MIDDLEWARE,
}


BUILTIN_COMMANDS = {
    "help",
    "new",
    "team",
    "agent",
    "plan",
    "todo",
    "quit",
    "exit",
}


def get_bundled_plugins_dir() -> Path:
    """Return packaged/bundled plugins dir without consulting process cwd."""
    from crew.state.home import ROOT

    root = Path(ROOT).resolve()
    bundled = root / "plugins"
    if bundled.is_dir():
        return bundled
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        exe_sibling = Path(sys._MEIPASS).resolve().parent / "plugins"
        if exe_sibling.is_dir():
            return exe_sibling
    return bundled


def get_user_plugins_dir() -> Path:
    """Return third-party plugin dir under the configured Crew home."""
    from crew.state.home import get_crew_home

    return get_crew_home() / "plugins"


@dataclass
class RequestMiddlewareResult:
    payload: Any
    original_payload: Any
    changed: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)


def middleware_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    kwargs.setdefault("middleware_schema_version", MIDDLEWARE_SCHEMA_VERSION)
    return kwargs


def _safe_copy(payload: Any) -> Any:
    try:
        return deepcopy(payload)
    except Exception:
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, list):
            return list(payload)
        return payload


def _trace_entry(result: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for key in ("source", "reason", "name"):
        value = result.get(key)
        if isinstance(value, str) and value:
            entry[key] = value
    if not entry:
        entry["source"] = "plugin"
    return entry


def _normalize_command_name(name: str) -> str:
    return str(name or "").lower().strip().lstrip("/").replace(" ", "-")


BUILTIN_COMMANDS = {
    "help",
    "new",
    "team",
    "agent",
    "plan",
    "todo",
    "quit",
    "exit",
}
VALID_HOOKS = {
    "pre_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "transform_tool_result",
    "transform_terminal_output",
    "transform_llm_output",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "pre_gateway_dispatch",
    "pre_approval_request",
    "post_approval_response",
}

VALID_PLUGIN_KINDS = {"standalone", "backend", "exclusive", "platform", "model-provider"}


@dataclass
class PluginManifest:
    name: str
    label: str = ""
    version: str = ""
    description: str = ""
    author: str = ""
    kind: str = "standalone"
    key: str = ""
    source: str = ""
    requires_env: list[Any] = field(default_factory=list)
    optional_env: list[Any] = field(default_factory=list)
    provides_tools: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    ui_hints: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    enabled: bool = False
    tools_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    api_routers_registered: list[str] = field(default_factory=list)
    platforms_registered: list[str] = field(default_factory=list)
    disposers: list = field(default_factory=list)
    skill_roots: list[str] = field(default_factory=list)
    error: str | None = None


class PluginContext:
    """传给插件 ``register(ctx)`` 的 Crew 插件上下文。"""

    def __init__(self, manifest: PluginManifest, manager: "PluginManager") -> None:
        self.manifest = manifest
        self._manager = manager
        # 由 build_app 注入的共享服务（config / plugin_prefs 等），插件只读消费
        self.services: dict[str, Any] = manager.services

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
        should_defer: bool | None = None,
        search_hint: str = "",
        always_load: bool = False,
        is_mcp: bool = False,
        permission_resolver: Callable[..., Any] | None = None,
        permission_approver: Callable[..., Any] | None = None,
        display_name: str = "",
        ui_label_template: str = "",
    ) -> None:
        if self._manager.registry is None:
            raise RuntimeError("PluginManager 未绑定 ToolRegistry，无法注册工具")
        self._manager.registry.register(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env,
            is_async=is_async,
            description=description,
            emoji=emoji,
            override=override,
            should_defer=should_defer,
            search_hint=search_hint,
            always_load=always_load,
            is_mcp=is_mcp,
            permission_resolver=permission_resolver,
            permission_approver=permission_approver,
            display_name=display_name,
            ui_label_template=ui_label_template,
        )
        self._manager._active_tools.append(name)

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        if hook_name not in VALID_HOOKS:
            log.warning(
                "插件 %s 注册了未知 hook %s，按前向兼容保留",
                self.manifest.name,
                hook_name,
            )
        owner_key = self.manifest.key or self.manifest.name
        self._manager._hooks.setdefault(hook_name, []).append(callback)
        self._manager._hook_owners.setdefault(hook_name, []).append((owner_key, callback))
        self._manager._active_hooks.append(hook_name)

    def register_middleware(self, kind: str, callback: Callable[..., Any]) -> None:
        if kind not in VALID_MIDDLEWARE:
            log.warning(
                "插件 %s 注册了未知 middleware %s，按前向兼容保留",
                self.manifest.name,
                kind,
            )
        owner_key = self.manifest.key or self.manifest.name
        self._manager._middleware.setdefault(kind, []).append(callback)
        self._manager._middleware_owners.setdefault(kind, []).append((owner_key, callback))
        self._manager._active_middleware.append(kind)

    def register_disposer(self, fn: Callable[..., Any]) -> None:
        """登记插件级清理回调（可多个），unload_plugin 时逆序调用。

        回调可以是同步函数或返回 awaitable；抛错只记日志，不中断后续清理。
        """
        self._manager._active_disposers.append(fn)

    def register_skill_root(self, path: str | Path) -> None:
        """声明插件携带的 skills 目录；相对路径按插件目录解析，存绝对路径。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            base = self.manifest.path or Path.cwd()
            p = base / p
        self._manager._active_skill_roots.append(str(p.resolve()))

    def register_command(
        self,
        name: str,
        handler: Callable[..., Any],
        description: str = "",
        args_hint: str = "",
    ) -> None:
        clean = _normalize_command_name(name)
        if not clean:
            log.warning("插件 %s 注册了空 slash command，已跳过", self.manifest.name)
            return
        if clean in BUILTIN_COMMANDS:
            log.warning(
                "插件 %s 注册的 slash command /%s 与内置命令冲突，已跳过",
                self.manifest.name,
                clean,
            )
            return
        self._manager._plugin_commands[clean] = {
            "handler": handler,
            "description": description or "Plugin command",
            "plugin": self.manifest.name,
            "args_hint": (args_hint or "").strip(),
        }
        self._manager._active_commands.append(clean)

    def register_api_router(self, router: Any) -> None:
        """Register a FastAPI APIRouter mounted by gateway under /api/plugins/<name>."""
        self._manager._api_routers[self.manifest.name] = router
        self._manager._active_api_routers.append(self.manifest.name)

    def notify_dashboard(self, kind: str = "audit_updated", body: dict[str, Any] | None = None, owner_id: str = "") -> None:
        """向当前用户的前端 Dashboard 推送自定义事件（通过 WebSocket）。

        插件在 hook 回调中调用此方法通知前端数据变更，前端收到后按需刷新。
        仅在 Gateway 模式下生效（notify_owner_fn 已注入时）。
        """
        fn = self._manager._notify_dashboard_fn
        if fn is None:
            return
        import asyncio
        payload = {
            "kind": kind,
            "body": body or {},
            "is_final": True,
            "sequence": 0,
            "session_id": "",
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(fn(owner_id, payload))
        except RuntimeError:
            pass

    def register_platform(
        self,
        name: str,
        label: str,
        adapter_factory: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        validate_config: Callable[..., bool] | None = None,
        is_connected: Callable[..., bool] | None = None,
        required_env: list[str] | None = None,
        optional_env: list[Any] | None = None,
        install_hint: str = "",
        description: str = "",
        **entry_kwargs: Any,
    ) -> None:
        from crew.gateway.platform_registry import PlatformEntry, platform_registry

        entry_kwargs.setdefault("plugin_name", self.manifest.name)
        entry_kwargs.setdefault("optional_env", list(optional_env or []))
        entry_kwargs = self._manager._normalize_platform_entry_kwargs(PlatformEntry, entry_kwargs)
        platform_registry.register(
            PlatformEntry(
                name=name,
                label=label,
                adapter_factory=adapter_factory,
                check_fn=check_fn or (lambda: True),
                validate_config=validate_config,
                is_connected=is_connected,
                required_env=list(required_env or []),
                install_hint=install_hint,
                source="plugin",
                description=description,
                **entry_kwargs,
            )
        )
        self._manager._active_platforms.append(name)


class PluginManager:
    def __init__(
        self,
        plugins: list[Plugin] | None = None,
        registry: Registry | None = None,
        services: dict | None = None,
    ) -> None:
        self._plugins: list[Plugin] = list(plugins or [])
        self.registry = registry
        # 注入给插件的共享服务（如 config / plugin_prefs），经 PluginContext.services 透传
        self.services: dict[str, Any] = dict(services or {})
        self._hooks: dict[str, list[Callable[..., Any]]] = {}
        self._middleware: dict[str, list[Callable[..., Any]]] = {}
        # hook/middleware 的归属表（plugin_key, callback），与上面两结构平行维护，供按插件摘除
        self._hook_owners: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._middleware_owners: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._plugin_commands: dict[str, dict[str, Any]] = {}
        self._api_routers: dict[str, Any] = {}
        self._loaded: dict[str, LoadedPlugin] = {}
        self._active_tools: list[str] = []
        self._active_hooks: list[str] = []
        self._active_middleware: list[str] = []
        self._active_commands: list[str] = []
        self._active_api_routers: list[str] = []
        self._active_platforms: list[str] = []
        self._active_disposers: list = []
        self._active_skill_roots: list[str] = []
        self._notify_dashboard_fn: Callable[..., Any] | None = None
        self._legacy_session_end_hooks_warned: set[int] = set()

    def add(self, plugin: Plugin) -> None:
        self._plugins.append(plugin)

    @property
    def plugins(self) -> list[Plugin]:
        return list(self._plugins)

    @property
    def loaded_plugins(self) -> list[LoadedPlugin]:
        return list(self._loaded.values())

    @property
    def api_routers(self) -> list[tuple[str, Any]]:
        return list(self._api_routers.items())

    @property
    def plugin_commands(self) -> dict[str, dict[str, Any]]:
        return dict(self._plugin_commands)

    def bind_registry(self, registry: Registry) -> None:
        self.registry = registry

    def discover_and_load(
        self,
        plugin_dirs: list[str | Path] | None = None,
        *,
        enabled: list[str] | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        """扫描并加载目录插件。

        enabled=None 或 ["*"] 表示加载扫描到的插件；enabled=[] 表示全部跳过。
        disabled 优先级最高；disabled=["*"] 表示禁用所有目录插件。
        """
        dirs = plugin_dirs or [
            get_bundled_plugins_dir(),
            get_user_plugins_dir(),
        ]
        disabled_set = set(disabled or [])
        enabled_set = set(enabled) if enabled is not None else None

        # ["*"] 作为“全部”语义
        if enabled is not None and enabled == ["*"]:
            enabled_set = None
        if disabled is not None and disabled == ["*"]:
            enabled_set = set()  # 禁用所有目录插件
            disabled_set = set()

        self._loaded.clear()
        self._hooks.clear()
        self._middleware.clear()
        self._hook_owners.clear()
        self._middleware_owners.clear()
        self._plugin_commands.clear()
        self._api_routers.clear()
        self._clear_plugin_platform_entries()
        for root in [Path(d) for d in dirs]:
            if not root.is_dir():
                continue
            source = self._source_for_root(root)
            for plugin_dir, key in self._iter_plugin_dirs(root):
                manifest = self._read_manifest(plugin_dir, key=key, source=source)
                if manifest is None:
                    continue
                lookup_key = manifest.key or manifest.name
                if lookup_key in disabled_set or manifest.name in disabled_set:
                    self._loaded[lookup_key] = LoadedPlugin(
                        manifest=manifest,
                        enabled=False,
                        error="disabled",
                    )
                    continue
                if not self._should_load_manifest(manifest, enabled_set):
                    self._loaded[lookup_key] = LoadedPlugin(
                        manifest=manifest,
                        enabled=False,
                        error="not enabled",
                    )
                    continue
                self._load_plugin(manifest)

    def get_plugin(self, key: str) -> LoadedPlugin | None:
        """按 key 或 name 查已发现的插件（含未启用的）。"""
        loaded = self._loaded.get(key)
        if loaded is not None:
            return loaded
        for lookup_key, candidate in self._loaded.items():
            if candidate.manifest.name == key:
                return candidate
        return None

    def plugin_skill_roots(self) -> list[str]:
        """所有已加载且启用插件声明的 skills 根目录（绝对路径）。"""
        roots: list[str] = []
        for loaded in self._loaded.values():
            if loaded.enabled:
                roots.extend(loaded.skill_roots)
        return roots

    def unload_plugin(self, key: str) -> bool:
        """按插件归属注销工具/hook/middleware/command/router/platform，并调用 disposer。

        找不到或本未加载（enabled=False）返回 False。清理逐步推进：单项失败只记日志，
        不中断后续清理；插件保留在 _loaded 中（enabled=False）供 API 展示。
        """
        loaded = self.get_plugin(key)
        if loaded is None or not loaded.enabled:
            return False
        owner_key = loaded.manifest.key or loaded.manifest.name

        if self.registry is not None:
            for name in loaded.tools_registered:
                try:
                    self.registry.unregister(name)
                except Exception:  # noqa: BLE001
                    log.exception("注销插件 %s 工具失败: %s", owner_key, name)

        for hook_name in list(loaded.hooks_registered):
            self._remove_owned_callbacks(self._hooks, self._hook_owners, hook_name, owner_key)
        for kind in list(loaded.middleware_registered):
            self._remove_owned_callbacks(
                self._middleware, self._middleware_owners, kind, owner_key
            )

        for command in list(loaded.commands_registered):
            entry = self._plugin_commands.get(command)
            if entry is not None and entry.get("plugin") in (loaded.manifest.name, owner_key):
                self._plugin_commands.pop(command, None)

        self._api_routers.pop(loaded.manifest.name, None)
        self._api_routers.pop(owner_key, None)
        self._unregister_plugin_platforms(loaded.manifest.name)

        for disposer in reversed(loaded.disposers):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    self._schedule_awaitable(result, owner_key)
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 的 disposer 执行失败", owner_key)

        loaded.tools_registered = []
        loaded.hooks_registered = []
        loaded.middleware_registered = []
        loaded.commands_registered = []
        loaded.api_routers_registered = []
        loaded.platforms_registered = []
        loaded.disposers = []
        loaded.skill_roots = []
        loaded.enabled = False
        loaded.error = None
        log.info("插件已卸载: %s", owner_key)
        return True

    @staticmethod
    def _remove_owned_callbacks(
        table: dict[str, list[Callable[..., Any]]],
        owners: dict[str, list[tuple[str, Callable[..., Any]]]],
        name: str,
        owner_key: str,
    ) -> None:
        """从回调表与归属表中摘除某插件在某 hook/middleware 下注册的回调。"""
        owned = [cb for key, cb in owners.get(name, []) if key == owner_key]
        if not owned:
            owners.pop(name, None)
            return
        callbacks = table.get(name, [])
        table[name] = [cb for cb in callbacks if cb not in owned]
        owners[name] = [(key, cb) for key, cb in owners.get(name, []) if key != owner_key]
        if not table[name]:
            table.pop(name, None)
        if not owners[name]:
            owners.pop(name, None)

    @staticmethod
    def _schedule_awaitable(awaitable: Any, owner_key: str) -> None:
        """在卸载同步上下文中执行异步清理：有运行中的事件循环就 create_task，
        否则用 asyncio.run 跑完（对齐打包/CLI 等无循环场景）。"""
        async def _guarded() -> None:
            try:
                await awaitable
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 的异步 disposer 执行失败", owner_key)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_guarded())
        else:
            loop.create_task(_guarded())

    def _source_for_root(self, root: Path) -> str:
        root = root.resolve()
        if root == get_bundled_plugins_dir().resolve():
            return "bundled"
        if root == get_user_plugins_dir().resolve():
            return "project"
        return "local"

    def _read_manifest(self, plugin_dir: Path, *, key: str, source: str) -> PluginManifest | None:
        manifest_path = plugin_dir / "plugin.yaml"
        if not manifest_path.exists():
            manifest_path = plugin_dir / "plugin.yml"
        if not manifest_path.exists():
            return None
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            name = str(raw.get("name") or plugin_dir.name)
            raw_kind = raw.get("kind") or "standalone"
            kind = str(raw_kind).strip().lower() or "standalone"
            if kind not in VALID_PLUGIN_KINDS:
                log.warning(
                    "插件 %s 声明了未知 kind %s，按 standalone 处理",
                    key,
                    raw_kind,
                )
                kind = "standalone"
            return PluginManifest(
                name=name,
                label=str(raw.get("label") or name),
                version=str(raw.get("version") or ""),
                description=str(raw.get("description") or ""),
                author=str(raw.get("author") or ""),
                kind=kind,
                key=str(raw.get("key") or key),
                source=str(raw.get("source") or source),
                requires_env=list(raw.get("requires_env") or []),
                optional_env=list(raw.get("optional_env") or []),
                provides_tools=list(raw.get("provides_tools") or []),
                provides_hooks=list(raw.get("provides_hooks") or []),
                config_schema=dict(raw.get("config_schema") or raw.get("configSchema") or {}),
                ui_hints=dict(raw.get("ui_hints") or raw.get("uiHints") or {}),
                path=plugin_dir,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("读取插件 manifest 失败: %s", plugin_dir)
            self._loaded[plugin_dir.name] = LoadedPlugin(
                manifest=PluginManifest(name=plugin_dir.name, path=plugin_dir),
                enabled=False,
                error=str(exc),
            )
            return None

    def _load_plugin(self, manifest: PluginManifest) -> None:
        loaded = LoadedPlugin(manifest=manifest)
        try:
            module = self._load_module(manifest)
            register = getattr(module, "register", None)
            if register is None:
                raise RuntimeError("缺少 register(ctx) 函数")
            self._active_tools = []
            self._active_hooks = []
            self._active_middleware = []
            self._active_commands = []
            self._active_api_routers = []
            self._active_platforms = []
            self._active_disposers = []
            self._active_skill_roots = []
            register(PluginContext(manifest, self))
            loaded.tools_registered = list(self._active_tools)
            loaded.hooks_registered = list(dict.fromkeys(self._active_hooks))
            loaded.middleware_registered = list(dict.fromkeys(self._active_middleware))
            loaded.commands_registered = list(dict.fromkeys(self._active_commands))
            loaded.api_routers_registered = list(dict.fromkeys(self._active_api_routers))
            loaded.platforms_registered = list(dict.fromkeys(self._active_platforms))
            loaded.disposers = list(self._active_disposers)
            loaded.skill_roots = list(dict.fromkeys(self._active_skill_roots))
            loaded.enabled = True
        except Exception as exc:  # noqa: BLE001
            loaded.error = str(exc)
            self._unregister_plugin_platforms(manifest.name)
            log.exception("加载插件失败: %s", manifest.name)
        finally:
            self._loaded[manifest.key or manifest.name] = loaded
            self._active_tools = []
            self._active_hooks = []
            self._active_middleware = []
            self._active_commands = []
            self._active_api_routers = []
            self._active_platforms = []
            self._active_disposers = []
            self._active_skill_roots = []

    def _iter_plugin_dirs(self, root: Path) -> list[tuple[Path, str]]:
        """Return flat plugin dirs plus one-level category plugin dirs."""
        dirs: list[tuple[Path, str]] = []
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if (child / "plugin.yaml").exists() or (child / "plugin.yml").exists():
                dirs.append((child, child.name))
                continue
            for grandchild in sorted(p for p in child.iterdir() if p.is_dir()):
                if (grandchild / "plugin.yaml").exists() or (grandchild / "plugin.yml").exists():
                    dirs.append((grandchild, f"{child.name}/{grandchild.name}"))
        return dirs

    def _load_module(self, manifest: PluginManifest) -> ModuleType:
        if manifest.path is None:
            raise RuntimeError("插件缺少 path")
        init_file = manifest.path / "__init__.py"
        if not init_file.exists():
            raise RuntimeError(f"缺少 __init__.py: {manifest.path}")
        module_key = (manifest.key or manifest.name).replace("/", "_").replace("-", "_")
        if _NS_PARENT not in sys.modules:
            ns_pkg = ModuleType(_NS_PARENT)
            ns_pkg.__path__ = []  # type: ignore[attr-defined]
            ns_pkg.__package__ = _NS_PARENT
            sys.modules[_NS_PARENT] = ns_pkg
        module_name = f"{_NS_PARENT}.{module_key}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            init_file,
            submodule_search_locations=[str(manifest.path)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载插件模块: {manifest.name}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        module.__path__ = [str(manifest.path)]  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _should_load_manifest(self, manifest: PluginManifest, enabled_set: set[str] | None) -> bool:
        if manifest.source == "bundled" and manifest.kind in {"backend", "platform"}:
            return True
        if enabled_set is None:
            return False
        lookup_key = manifest.key or manifest.name
        return lookup_key in enabled_set or manifest.name in enabled_set

    def _clear_plugin_platform_entries(self) -> None:
        try:
            from crew.gateway.platform_registry import platform_registry

            platform_registry.clear_plugin_entries()
        except Exception as exc:  # noqa: BLE001
            log.warning("清理插件平台注册表失败: %s", exc)

    def _unregister_plugin_platforms(self, plugin_name: str) -> None:
        try:
            from crew.gateway.platform_registry import platform_registry

            platform_registry.unregister_plugin_entries(plugin_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("回滚插件平台 %s 失败: %s", plugin_name, exc)

    def _normalize_platform_entry_kwargs(
        self,
        entry_cls: type[Any],
        entry_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        known = {item.name for item in fields(entry_cls)}
        metadata = dict(entry_kwargs.pop("metadata", {}) or {})
        for key in list(entry_kwargs):
            if key not in known:
                metadata[key] = entry_kwargs.pop(key)
        if metadata:
            entry_kwargs["metadata"] = metadata
        return entry_kwargs

    async def _call(self, callback: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            sig = inspect.signature(callback)
            accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if not accepts_kwargs:
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        result = callback(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _invoke_command_handler(self, handler: Callable[..., Any], raw_args: str, **context: Any) -> Any:
        try:
            sig = inspect.signature(handler)
            params = list(sig.parameters.values())
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            if accepts_kwargs or "raw_args" in sig.parameters:
                result = handler(raw_args=raw_args, **context)
            elif "args" in sig.parameters:
                result = handler(args=raw_args, **context)
            elif any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
                result = handler(raw_args)
            elif len(params) == 1:
                result = handler(raw_args)
            else:
                result = handler()
        except (TypeError, ValueError):
            result = handler(raw_args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _call_hook(self, hook_name: str, callback: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            return await self._call(callback, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "插件 hook %s 回调 %s 执行失败: %s",
                hook_name,
                getattr(callback, "__name__", repr(callback)),
                exc,
            )
            return None

    async def run_plugin_command(self, text: str, **context: Any) -> str | None:
        """Run a registered in-session slash command.

        Returns None when the command is unknown; otherwise returns the command
        result converted to text. Empty handler results become an empty string.
        """
        raw = str(text or "").strip()
        if not raw.startswith("/"):
            return None
        name, _, raw_args = raw[1:].partition(" ")
        clean = _normalize_command_name(name)
        entry = self._plugin_commands.get(clean)
        if entry is None:
            return None
        try:
            result = await self._invoke_command_handler(
                entry["handler"],
                raw_args,
                command=clean,
                plugin=entry.get("plugin", ""),
                **context,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("插件命令 /%s 执行失败: %s", clean, exc)
            return f"插件命令 /{clean} 执行失败: {exc}"
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        return str(result)

    def has_middleware(self, kind: str) -> bool:
        return bool(self._middleware.get(kind))

    async def invoke_middleware(self, kind: str, **kwargs: Any) -> list[Any]:
        results: list[Any] = []
        for cb in self._middleware.get(kind, []):
            try:
                results.append(await self._call(cb, **middleware_payload(**kwargs)))
            except Exception as exc:  # noqa: BLE001
                log.warning("插件 middleware %s 执行失败: %s", kind, exc)
        return results

    async def apply_llm_request_middleware(
        self,
        request: dict[str, Any],
        **context: Any,
    ) -> RequestMiddlewareResult:
        return await self._apply_request_middleware(
            LLM_REQUEST_MIDDLEWARE,
            "request",
            request,
            original_key="original_request",
            **context,
        )

    async def apply_tool_request_middleware(
        self,
        tool_name: str,
        args: dict[str, Any],
        **context: Any,
    ) -> RequestMiddlewareResult:
        return await self._apply_request_middleware(
            TOOL_REQUEST_MIDDLEWARE,
            "args",
            args,
            original_key="original_args",
            tool_name=tool_name,
            **context,
        )

    async def _apply_request_middleware(
        self,
        kind: str,
        payload_key: str,
        payload: Any,
        *,
        original_key: str,
        **context: Any,
    ) -> RequestMiddlewareResult:
        if not self.has_middleware(kind):
            return RequestMiddlewareResult(payload=payload, original_payload=payload, changed=False, trace=[])
        original_payload = _safe_copy(payload)
        current_payload = _safe_copy(original_payload)
        trace: list[dict[str, Any]] = []
        for cb in self._middleware.get(kind, []):
            call_kwargs = {
                **context,
                payload_key: current_payload,
                original_key: original_payload,
            }
            try:
                result = await self._call(cb, **middleware_payload(**call_kwargs))
            except Exception as exc:  # noqa: BLE001
                log.warning("插件 request middleware %s 执行失败: %s", kind, exc)
                continue
            if not isinstance(result, dict):
                continue
            next_payload = result.get(payload_key)
            if not isinstance(next_payload, type(current_payload)):
                continue
            current_payload = _safe_copy(next_payload)
            trace.append(_trace_entry(result))
        return RequestMiddlewareResult(
            payload=current_payload,
            original_payload=original_payload,
            changed=bool(trace),
            trace=trace,
        )

    async def run_tool_execution_middleware(
        self,
        tool_name: str,
        args: dict[str, Any],
        next_call: Callable[[dict[str, Any]], Any],
        **context: Any,
    ) -> Any:
        callbacks = list(self._middleware.get(TOOL_EXECUTION_MIDDLEWARE, []))
        if not callbacks:
            return await self._maybe_await(next_call(args))
        return await self._run_execution_chain(
            TOOL_EXECUTION_MIDDLEWARE,
            callbacks,
            next_call,
            payload_key="args",
            args=args,
            tool_name=tool_name,
            original_args=context.pop("original_args", args),
            **context,
        )

    async def run_llm_execution_middleware(
        self,
        request: dict[str, Any],
        next_call: Callable[[dict[str, Any]], Any],
        **context: Any,
    ) -> Any:
        callbacks = list(self._middleware.get(LLM_EXECUTION_MIDDLEWARE, []))
        if not callbacks:
            return await self._maybe_await(next_call(request))
        return await self._run_execution_chain(
            LLM_EXECUTION_MIDDLEWARE,
            callbacks,
            next_call,
            payload_key="request",
            request=request,
            original_request=context.pop("original_request", request),
            **context,
        )

    async def _run_execution_chain(
        self,
        kind: str,
        callbacks: list[Callable[..., Any]],
        terminal_call: Callable[[Any], Any],
        *,
        payload_key: str,
        **kwargs: Any,
    ) -> Any:
        class _DownstreamExecutionError(Exception):
            def __init__(self, original: BaseException) -> None:
                super().__init__(str(original))
                self.original = original

        def is_async_iterable(value: Any) -> bool:
            return hasattr(value, "__aiter__")

        def wrap_downstream_stream(stream: Any) -> Any:
            async def guarded():
                try:
                    async for item in stream:
                        yield item
                except BaseException as exc:
                    raise _DownstreamExecutionError(exc) from exc

            return guarded()

        def wrap_plugin_stream(callback: Callable[..., Any], stream: Any) -> Any:
            async def guarded():
                try:
                    async for item in stream:
                        yield item
                except _DownstreamExecutionError as exc:
                    raise exc.original
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "插件 execution middleware %s 回调 %s 流式迭代失败: %s",
                        kind,
                        getattr(callback, "__name__", repr(callback)),
                        exc,
                    )

            return guarded()

        async def call_at(index: int, payload: Any) -> Any:
            if index >= len(callbacks):
                return await self._maybe_await(terminal_call(payload))

            callback = callbacks[index]
            next_called = False
            next_succeeded = False
            next_result: Any = None

            async def next_call(next_payload: Any = None) -> Any:
                nonlocal next_called, next_succeeded, next_result
                if next_called:
                    raise RuntimeError(
                        f"Middleware '{kind}' callback "
                        f"{getattr(callback, '__name__', repr(callback))} called next_call() more than once"
                    )
                next_called = True
                try:
                    next_result = await call_at(index + 1, payload if next_payload is None else next_payload)
                    next_succeeded = True
                    if kind == LLM_EXECUTION_MIDDLEWARE and is_async_iterable(next_result):
                        return wrap_downstream_stream(next_result)
                    return next_result
                except BaseException as exc:
                    raise _DownstreamExecutionError(exc) from exc

            call_kwargs = middleware_payload(**kwargs)
            call_kwargs[payload_key] = payload
            call_kwargs["next_call"] = next_call
            try:
                result = await self._call(callback, **call_kwargs)
                if kind == LLM_EXECUTION_MIDDLEWARE and is_async_iterable(result):
                    return wrap_plugin_stream(callback, result)
                return result
            except _DownstreamExecutionError as exc:
                raise exc.original
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "插件 execution middleware %s 回调 %s 失败: %s",
                    kind,
                    getattr(callback, "__name__", repr(callback)),
                    exc,
                )
                if next_succeeded:
                    return next_result
                if next_called:
                    raise
                return await call_at(index + 1, payload)

        return await call_at(0, kwargs[payload_key])

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def pre_llm_call(self, session_id: str, messages: list[Message]) -> dict[str, Any] | None:
        """Call pre_llm_call hooks/plugins.

        Returns:
            None — proceed normally (context injected into messages in-place).
            {"action": "block", "response": "..."} — skip the LLM call entirely
            and use the given response text as the final reply.
        """
        injections: list[str] = []
        for p in self._plugins:
            try:
                result = await p.pre_llm_call(session_id, messages)
            except Exception as exc:  # noqa: BLE001
                log.warning("插件 %s.pre_llm_call 执行失败: %s", getattr(p, "name", p), exc)
                continue
            if isinstance(result, dict) and result.get("action") == "block":
                log.info("插件 %s.pre_llm_call 阻止 LLM 调用", getattr(p, "name", p))
                return result
            if isinstance(result, str) and result:
                injections.append(result)
            elif isinstance(result, dict) and isinstance(result.get("context"), str):
                injections.append(result["context"])
        for cb in self._hooks.get("pre_llm_call", []):
            result = await self._call_hook("pre_llm_call", cb, session_id=session_id, messages=messages)
            if isinstance(result, dict) and result.get("action") == "block":
                log.info("Hook pre_llm_call 阻止 LLM 调用: %s", getattr(cb, "__name__", repr(cb)))
                return result
            if isinstance(result, str) and result:
                injections.append(result)
            elif isinstance(result, dict) and isinstance(result.get("context"), str):
                injections.append(result["context"])
        for context in injections:
            messages.append(
                Message.user(
                    f"<system-reminder>插件上下文：\n{context}\n</system-reminder>",
                    is_meta=True,
                )
            )
        return None

    async def post_llm_call(
        self,
        session_id: str,
        messages: list[Message],
        response: dict[str, Any],
    ) -> None:
        for cb in self._hooks.get("post_llm_call", []):
            await self._call_hook(
                "post_llm_call",
                cb,
                session_id=session_id,
                messages=messages,
                response=response,
            )

    async def post_api_request(
        self,
        session_id: str = "",
        model: str = "",
        provider: str = "",
        usage: dict[str, int] | None = None,
        api_duration: float = 0.0,
        finish_reason: str = "",
    ) -> None:
        for cb in self._hooks.get("post_api_request", []):
            await self._call_hook(
                "post_api_request",
                cb,
                session_id=session_id,
                model=model,
                provider=provider,
                usage=usage or {},
                api_duration=api_duration,
                finish_reason=finish_reason,
            )

    async def pre_tool_call(self, tool_call: ToolCall, **context: Any) -> str | None:
        for p in self._plugins:
            try:
                await p.pre_tool_call(tool_call)
            except Exception as exc:  # noqa: BLE001
                log.warning("插件 %s.pre_tool_call 拦截工具 %s: %s", getattr(p, "name", p), tool_call.name, exc)
                return str(exc) or f"工具被插件拦截: {tool_call.name}"
        for cb in self._hooks.get("pre_tool_call", []):
            result = await self._call_hook(
                "pre_tool_call",
                cb,
                tool_call=tool_call,
                tool_name=tool_call.name,
                args=tool_call.arguments,
                tool_call_id=tool_call.id,
                **context,
            )
            if isinstance(result, dict) and result.get("action") == "block":
                return str(result.get("message") or f"工具被插件拦截: {tool_call.name}")
            if isinstance(result, str) and result:
                return result
        return None

    async def post_tool_call(self, tool_call: ToolCall, result: ToolResult, **context: Any) -> None:
        for p in self._plugins:
            try:
                await p.post_tool_call(tool_call, result)
            except Exception as exc:  # noqa: BLE001
                log.warning("插件 %s.post_tool_call 执行失败: %s", getattr(p, "name", p), exc)
        for cb in self._hooks.get("post_tool_call", []):
            await self._call_hook(
                "post_tool_call",
                cb,
                tool_call=tool_call,
                tool_name=tool_call.name,
                args=tool_call.arguments,
                result=result.content,
                tool_result=result,
                tool_call_id=tool_call.id,
                **context,
            )

    async def transform_tool_result(self, tool_call: ToolCall, result: ToolResult) -> ToolResult:
        content = result.content
        for cb in self._hooks.get("transform_tool_result", []):
            transformed = await self._call_hook(
                "transform_tool_result",
                cb,
                tool_call=tool_call,
                tool_name=tool_call.name,
                args=tool_call.arguments,
                result=content,
                tool_result=result,
                tool_call_id=tool_call.id,
            )
            if isinstance(transformed, str):
                content = transformed
        if content == result.content:
            return result
        return ToolResult(
            result.tool_call_id,
            result.name,
            content,
            result.is_error,
            media=list(result.media),
        )

    async def transform_llm_output(self, session_id: str, text: str, **context: Any) -> str:
        content = text
        for cb in self._hooks.get("transform_llm_output", []):
            transformed = await self._call_hook(
                "transform_llm_output",
                cb,
                session_id=session_id,
                text=content,
                **context,
            )
            if isinstance(transformed, str):
                content = transformed
        return content

    async def on_session_start(self, session_id: str, **context: Any) -> None:
        for cb in self._hooks.get("on_session_start", []):
            await self._call_hook("on_session_start", cb, session_id=session_id, **context)

    async def on_session_end(
        self,
        session_id: str,
        *,
        outcome: TerminalOutcome,
        error_summary: str = "",
    ) -> None:
        """Dispatch one unambiguous terminal outcome for an Agent turn.

        Legacy callbacks receive derived ``completed``/``interrupted`` booleans for one
        migration cycle. Failed maps to ``False/False`` because it is neither completion
        nor interruption; the new ``outcome`` value remains the only source of truth.
        """
        if outcome not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"未知 Plugin terminal outcome: {outcome}")
        safe_summary = ""
        if outcome == "failed" and error_summary:
            safe_summary = redact_sensitive_text(str(error_summary), force=True)[
                :_TERMINAL_ERROR_SUMMARY_LIMIT
            ]
        for cb in self._hooks.get("on_session_end", []):
            try:
                supports_outcome = "outcome" in inspect.signature(cb).parameters
            except (TypeError, ValueError):
                supports_outcome = False
            callback_id = id(cb)
            if not supports_outcome and callback_id not in self._legacy_session_end_hooks_warned:
                self._legacy_session_end_hooks_warned.add(callback_id)
                log.warning(
                    "插件 on_session_end 回调 %s 的 completed/interrupted 参数已废弃；"
                    "请迁移到 outcome/error_summary",
                    getattr(cb, "__name__", repr(cb)),
                )
            await self._call_hook(
                "on_session_end",
                cb,
                session_id=session_id,
                outcome=outcome,
                error_summary=safe_summary,
                completed=outcome == "completed",
                interrupted=outcome == "interrupted",
            )
