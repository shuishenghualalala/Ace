"""插件管理器：聚合多个 Plugin，按钩子顺序分发。

Agent 内核只跟 PluginManager 交互，不感知具体有哪些插件。
插件采用以下目录接口：

plugins/<plugin-name>/
  plugin.yaml
  __init__.py        # 必须提供 register(ctx)
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import inspect
import sys
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal, TypeVar

import yaml

from crew.core.interfaces import Plugin
from crew.core.types import Message, ToolCall, ToolResult
from crew.features.manager import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRecord,
    FeatureRuntime,
    FeatureStartupAudit,
    FeatureUpdateResult,
    FeatureUpdateStrategy,
    run_async_compat,
)
from crew.features.context import (
    ContextContributor,
    ContextContributorHandler,
    ContextContributorPredicate,
    ContextFailurePolicy,
    ContextPhase,
)
from crew.features.drivers import ExecutionDriver, ExecutionDriverHandler
from crew.features.dependencies import FeatureServiceDependencies
from crew.features.events import (
    EventFailurePolicy,
    FeatureEventContributor,
    FeatureEventContributorHandler,
    FeatureEventPredicate,
)
from crew.features.runtime import (
    FeatureActivationError,
    FeatureGeneration,
    FeatureLease,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
    RegistrationPhase,
    RegistrationToken,
)

from crew.features.services import ServiceKey, ServiceScopeKind, ServiceScopePath
from crew.tools.redact import redact_sensitive_text
from crew.tools.registry import Registry
from crew.state.logging import get_logger

log = get_logger("plugins")
ServiceT = TypeVar("ServiceT")
_NS_PARENT = "crew_runtime_plugins"

OBSERVER_SCHEMA_VERSION = "crew.observer.v1"
MIDDLEWARE_SCHEMA_VERSION = "crew.middleware.v1"

TOOL_REQUEST_MIDDLEWARE = "tool_request"
TOOL_EXECUTION_MIDDLEWARE = "tool_execution"
LLM_REQUEST_MIDDLEWARE = "llm_request"
LLM_EXECUTION_MIDDLEWARE = "llm_execution"
TerminalOutcome = Literal["completed", "failed", "interrupted"]
_TERMINAL_ERROR_SUMMARY_LIMIT = 512
# 同步回调已在线程中执行时，取消方愿意等待其收尾的最长时间；
# 超时后放弃等待（线程无法强杀），记 warning 并让 CancelledError 传播。
_SYNC_CALLBACK_CANCEL_GRACE_SECONDS = 2.0
_CONTEXT_WRITE_BACK_UNSET = object()
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


def _run_async_compat(awaitable: Any) -> Any:
    """同步宿主桥：委托给 Feature Runtime 的共享实现 run_async_compat。"""
    return run_async_compat(awaitable)


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
VALID_ACTIVATION_PHASES = {"build", "startup"}


def _manifest_service_names(raw: dict[str, Any], name: str) -> list[str]:
    """Read one service declaration, accepting the explicit legacy-safe alias."""
    alias = f"{name}_services"
    declared = raw.get(name)
    aliased = raw.get(alias)
    if declared is not None and aliased is not None:
        raise ValueError(f"plugin manifest cannot declare both {name!r} and {alias!r}")
    value = declared if declared is not None else aliased
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"plugin manifest field {name!r} must be a list of strings")
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        raise ValueError(f"plugin manifest field {name!r} contains an empty service key")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"plugin manifest field {name!r} contains duplicate service keys")
    return normalized


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
    requires_services: list[str] = field(default_factory=list)
    optional_services: list[str] = field(default_factory=list)
    provides_services: list[str] = field(default_factory=list)
    provides_tools: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    ui_hints: dict[str, Any] = field(default_factory=dict)
    stop_policy: FeatureStopPolicy = FeatureStopPolicy.DRAIN
    drain_timeout_seconds: float | None = 30.0
    update_strategy: FeatureUpdateStrategy = FeatureUpdateStrategy.RESTART
    # ``build`` preserves the historical synchronous discovery contract.
    # ``startup`` defers installation until the host owns its long-lived event
    # loop, which is required by plugins that create background tasks.
    activation_phase: str = "build"
    path: Path | None = None


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    enabled: bool = False
    # Selected definitions remain discoverable after build.  A suppression is
    # an explicit lifecycle decision and prevents a later phase retry from
    # silently bringing an unloaded plugin back.
    activation_suppressed: bool = False
    tools_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    api_routers_registered: list[str] = field(default_factory=list)
    platforms_registered: list[str] = field(default_factory=list)
    disposers: list = field(default_factory=list)
    skill_roots: list[str] = field(default_factory=list)
    error: str | None = None
    feature_record: FeatureRecord | None = None


class PluginContext:
    """传给插件 ``register(ctx)`` 的 Crew 插件上下文。"""

    def __init__(
        self,
        manifest: PluginManifest,
        manager: "PluginManager",
        feature_context: FeatureInstallContext,
        loaded: LoadedPlugin,
    ) -> None:
        self.manifest = manifest
        self._manager = manager
        self._feature_context = feature_context
        self._loaded = loaded
        # 由 build_app 注入的共享服务（config / plugin_prefs 等），插件只读消费
        self.services: dict[str, Any] = manager.services

    @property
    def generation(self):
        """当前目录插件所属的 Feature Generation。"""
        return self._feature_context.generation

    @staticmethod
    def _discard_once(values: list[Any], value: Any) -> None:
        try:
            values.remove(value)
        except ValueError:
            pass

    def _own(
        self,
        disposer: Callable[..., Any],
        *,
        label: str,
        phase: RegistrationPhase = RegistrationPhase.CONTRIBUTION,
    ) -> None:
        self._feature_context.register_disposer(disposer, label=label, phase=phase)

    def _lease_callback(
        self,
        callback: Callable[..., Any],
        *,
        label: str,
        run_sync_in_thread: bool = False,
    ) -> Callable[..., Any]:
        """Wrap one callable so every invocation owns a generation lease."""

        @wraps(callback)
        async def leased(*args: Any, **kwargs: Any) -> Any:
            async with self._feature_context.acquire_lease(label):
                call_kwargs = kwargs
                try:
                    signature = inspect.signature(callback)
                    accepts_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                    if not accepts_kwargs:
                        call_kwargs = {
                            key: value
                            for key, value in kwargs.items()
                            if key in signature.parameters
                        }
                except (TypeError, ValueError):
                    pass
                if run_sync_in_thread and not inspect.iscoroutinefunction(callback):
                    # 同步回调必须离开事件循环执行（避免冻结 loop），但线程里运行的是
                    # 调用方 ContextVar 上下文的副本，回调内的 ContextVar.set 不会
                    # 回传到调用协程。因此先 copy_context，把副本交给线程执行，await
                    # 成功后把副本中发生变化的值写回调用协程的上下文——语义与同步回调
                    # 内联执行一致。写回安全的前提：await 期间调用协程处于挂起状态，
                    # 其上下文不会被并发修改；只写回值不同的变量，避免无谓的 set。
                    # 异常与取消路径不写回，保持"失败无副作用"。
                    ctx = contextvars.copy_context()

                    async def _run_in_copied_context() -> Any:
                        loop = asyncio.get_running_loop()
                        return await loop.run_in_executor(
                            None, lambda: ctx.run(callback, *args, **call_kwargs)
                        )

                    work = asyncio.create_task(
                        _run_in_copied_context(),
                        name=f"plugin-sync-callback:{self.manifest.name}:{label}",
                    )
                    try:
                        result = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(work),
                                timeout=_SYNC_CALLBACK_CANCEL_GRACE_SECONDS,
                            )
                            work.result()
                        except TimeoutError:
                            log.warning(
                                "插件 %s 的同步回调 %s 取消后 %ss 内未结束，"
                                "放弃等待（该线程回调将继续泄漏运行）",
                                self.manifest.name,
                                label,
                                _SYNC_CALLBACK_CANCEL_GRACE_SECONDS,
                            )
                        raise
                    for var in ctx:
                        new_value = ctx[var]
                        if var.get(_CONTEXT_WRITE_BACK_UNSET) != new_value:
                            var.set(new_value)
                    return result
                result = callback(*args, **call_kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

        return leased

    @staticmethod
    def _service_key(key: str | ServiceKey[Any]) -> ServiceKey[Any]:
        return key if isinstance(key, ServiceKey) else ServiceKey(str(key))

    def register_service(
        self,
        key: str | ServiceKey[Any],
        value: Any,
        *,
        scope_kind: ServiceScopeKind = ServiceScopeKind.GLOBAL,
        scope_path: ServiceScopePath | None = None,
    ) -> RegistrationToken:
        """Publish one Manifest-declared service under this plugin scope."""
        service_key = self._service_key(key)
        if service_key.name not in self.manifest.provides_services:
            raise ValueError(
                f"plugin {self.manifest.name!r} must declare provided service "
                f"{service_key.name!r} in its manifest"
            )
        return self._feature_context.register_service(
            service_key,
            value,
            scope_kind=scope_kind,
            scope_path=scope_path,
        )

    def register_execution_driver(
        self,
        mode: str,
        handler: ExecutionDriverHandler,
        *,
        capabilities: tuple[str, ...] = (),
        description: str = "",
    ) -> RegistrationToken:
        """Publish an open execution mode under this plugin Generation."""
        return self._feature_context.register_execution_driver(
            ExecutionDriver(
                mode=mode,
                execute=handler,
                capabilities=capabilities,
                description=description,
            )
        )

    def register_context_contributor(
        self,
        contributor_id: str,
        handler: ContextContributorHandler,
        *,
        phase: ContextPhase = ContextPhase.REQUEST,
        priority: int = 100,
        failure_policy: ContextFailurePolicy = ContextFailurePolicy.DEGRADE,
        timeout_seconds: float | None = None,
        predicate: ContextContributorPredicate | None = None,
        model_visible: bool = True,
        persistent: bool = False,
        description: str = "",
    ) -> RegistrationToken:
        """Publish ordered request context under this plugin Generation."""
        return self._feature_context.register_context_contributor(
            ContextContributor(
                contributor_id=contributor_id,
                handler=handler,
                phase=phase,
                priority=priority,
                failure_policy=failure_policy,
                timeout_seconds=timeout_seconds,
                predicate=predicate,
                model_visible=model_visible,
                persistent=persistent,
                description=description,
            )
        )

    def register_event_contributor(
        self,
        contributor_id: str,
        handler: FeatureEventContributorHandler,
        *,
        priority: int = 100,
        failure_policy: EventFailurePolicy = EventFailurePolicy.DEGRADE,
        timeout_seconds: float | None = None,
        predicate: FeatureEventPredicate | None = None,
        description: str = "",
    ) -> RegistrationToken:
        """Publish ordered feature events under this plugin Generation."""
        return self._feature_context.register_event_contributor(
            FeatureEventContributor(
                contributor_id=contributor_id,
                handler=handler,
                priority=priority,
                failure_policy=failure_policy,
                timeout_seconds=timeout_seconds,
                predicate=predicate,
                description=description,
            )
        )

    def resolve_service(self, key: str | ServiceKey[Any]) -> Any:
        """Resolve one required or optional service declared by this plugin."""
        service_key = self._service_key(key)
        declared = {
            *self.manifest.requires_services,
            *self.manifest.optional_services,
        }
        if service_key.name not in declared:
            raise ValueError(
                f"plugin {self.manifest.name!r} must declare consumed service "
                f"{service_key.name!r} in its manifest"
            )
        return self._feature_context.resolve_service(service_key)

    def get_service(
        self,
        key: str | ServiceKey[Any],
        default: Any = None,
    ) -> Any:
        """Resolve a declared optional service with an explicit fallback."""
        service_key = self._service_key(key)
        declared = {
            *self.manifest.requires_services,
            *self.manifest.optional_services,
        }
        if service_key.name not in declared:
            raise ValueError(
                f"plugin {self.manifest.name!r} must declare consumed service "
                f"{service_key.name!r} in its manifest"
            )
        return self._feature_context.get_service(service_key, default)

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
        result_retention: str = "important",
        result_identity_fields: list[str] | tuple[str, ...] | None = None,
        result_policy_resolver: Callable[..., Any] | None = None,
    ) -> None:
        if self._manager.registry is None:
            raise RuntimeError("PluginManager 未绑定 ToolRegistry，无法注册工具")
        registry = self._manager.registry
        previous = registry.get(name) if name in registry.names() else None
        leased_handler = self._lease_callback(
            handler,
            label=f"tool:{name}",
            run_sync_in_thread=not (is_async or inspect.iscoroutinefunction(handler)),
        )
        registry.register(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=leased_handler,
            check_fn=check_fn,
            requires_env=requires_env,
            is_async=True,
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
            result_retention=result_retention,
            result_identity_fields=result_identity_fields,
            result_policy_resolver=result_policy_resolver,
        )
        registered = registry.get(name)
        self._loaded.tools_registered.append(name)

        def unregister() -> None:
            if name in registry.names() and registry.get(name) is registered:
                registry.unregister(name)
                if previous is not None:
                    registry.register(previous, override=True)
            self._discard_once(self._loaded.tools_registered, name)

        try:
            self._own(unregister, label=f"tool:{name}")
        except BaseException:
            unregister()
            raise

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        if hook_name not in VALID_HOOKS:
            log.warning(
                "插件 %s 注册了未知 hook %s，按前向兼容保留",
                self.manifest.name,
                hook_name,
            )
        owner_key = self.manifest.key or self.manifest.name
        leased_callback = self._lease_callback(
            callback,
            label=f"hook:{hook_name}",
            run_sync_in_thread=not inspect.iscoroutinefunction(callback),
        )
        self._manager._hooks.setdefault(hook_name, []).append(leased_callback)
        self._manager._hook_owners.setdefault(hook_name, []).append(
            (owner_key, leased_callback)
        )
        self._loaded.hooks_registered.append(hook_name)

        def unregister() -> None:
            self._manager._remove_owned_callback(
                self._manager._hooks,
                self._manager._hook_owners,
                hook_name,
                owner_key,
                leased_callback,
            )
            self._discard_once(self._loaded.hooks_registered, hook_name)

        try:
            self._own(unregister, label=f"hook:{hook_name}")
        except BaseException:
            unregister()
            raise

    def register_middleware(self, kind: str, callback: Callable[..., Any]) -> None:
        if kind not in VALID_MIDDLEWARE:
            log.warning(
                "插件 %s 注册了未知 middleware %s，按前向兼容保留",
                self.manifest.name,
                kind,
            )
        owner_key = self.manifest.key or self.manifest.name
        leased_callback = self._lease_callback(
            callback,
            label=f"middleware:{kind}",
            run_sync_in_thread=not inspect.iscoroutinefunction(callback),
        )
        self._manager._middleware.setdefault(kind, []).append(leased_callback)
        self._manager._middleware_owners.setdefault(kind, []).append(
            (owner_key, leased_callback)
        )
        self._loaded.middleware_registered.append(kind)

        def unregister() -> None:
            self._manager._remove_owned_callback(
                self._manager._middleware,
                self._manager._middleware_owners,
                kind,
                owner_key,
                leased_callback,
            )
            self._discard_once(self._loaded.middleware_registered, kind)

        try:
            self._own(unregister, label=f"middleware:{kind}")
        except BaseException:
            unregister()
            raise

    def register_disposer(self, fn: Callable[..., Any]) -> None:
        """登记插件级清理回调（可多个），unload_plugin 时逆序调用。

        回调可以是同步函数或返回 awaitable；抛错只记日志，不中断后续清理。
        """
        self._loaded.disposers.append(fn)
        try:
            self._own(
                fn,
                label=f"resource:{getattr(fn, '__name__', 'anonymous')}",
                phase=RegistrationPhase.RESOURCE,
            )
        except BaseException:
            self._discard_once(self._loaded.disposers, fn)
            raise

    def register_skill_root(self, path: str | Path) -> None:
        """声明插件携带的 skills 目录；相对路径按插件目录解析，存绝对路径。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            base = self.manifest.path or Path.cwd()
            p = base / p
        root = str(p.resolve())
        self._loaded.skill_roots.append(root)

        def unregister() -> None:
            self._discard_once(self._loaded.skill_roots, root)

        try:
            self._own(unregister, label=f"skill-root:{root}")
        except BaseException:
            unregister()
            raise

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
        previous = self._manager._plugin_commands.get(clean)
        leased_handler = self._lease_callback(
            handler,
            label=f"command:{clean}",
        )
        entry = {
            "handler": leased_handler,
            "description": description or "Plugin command",
            "plugin": self.manifest.name,
            "args_hint": (args_hint or "").strip(),
        }
        self._manager._plugin_commands[clean] = entry
        self._loaded.commands_registered.append(clean)

        def unregister() -> None:
            if self._manager._plugin_commands.get(clean) is entry:
                if previous is None:
                    self._manager._plugin_commands.pop(clean, None)
                else:
                    self._manager._plugin_commands[clean] = previous
            self._discard_once(self._loaded.commands_registered, clean)

        try:
            self._own(unregister, label=f"command:{clean}")
        except BaseException:
            unregister()
            raise

    def register_api_router(self, router: Any) -> None:
        """Register a FastAPI APIRouter mounted by gateway under /api/plugins/<name>."""
        key = self.manifest.name
        previous = self._manager._api_routers.get(key)
        self._manager._api_routers[key] = router
        self._loaded.api_routers_registered.append(key)

        def unregister() -> None:
            if self._manager._api_routers.get(key) is router:
                if previous is None:
                    self._manager._api_routers.pop(key, None)
                else:
                    self._manager._api_routers[key] = previous
            self._discard_once(self._loaded.api_routers_registered, key)

        try:
            self._own(unregister, label=f"route:{key}")
        except BaseException:
            unregister()
            raise

        # 同一贡献同步落入 Feature Runtime 的 Route Registry：Gateway 启动期
        # 从注册表装配并加闸门；Scope 释放时注册项摘除，闸门随即对该插件的
        # API 返回 capability_unavailable（FastAPI 路由树本身不热卸载）。
        self._feature_context.register_api_router(
            router,
            prefix=f"/api/plugins/{key.strip('/')}",
            contribution_id=f"plugin:{key}",
            description=f"directory plugin {key} API",
        )

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
        from crew.channels.platform_registry import PlatformEntry, platform_registry

        entry_kwargs.setdefault("plugin_name", self.manifest.name)
        entry_kwargs.setdefault("optional_env", list(optional_env or []))
        entry_kwargs = self._manager._normalize_platform_entry_kwargs(PlatformEntry, entry_kwargs)
        try:
            previous = platform_registry.get(name)
        except KeyError:
            previous = None
        entry = PlatformEntry(
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
        platform_registry.register(entry)
        self._loaded.platforms_registered.append(name)

        def unregister() -> None:
            try:
                current = platform_registry.get(name)
            except KeyError:
                current = None
            if current is entry:
                platform_registry.unregister(name)
                if previous is not None:
                    platform_registry.register(previous)
            self._discard_once(self._loaded.platforms_registered, name)

        try:
            self._own(unregister, label=f"platform:{name}")
        except BaseException:
            unregister()
            raise


class PluginManager:
    def __init__(
        self,
        plugins: list[Plugin] | None = None,
        registry: Registry | None = None,
        services: dict | None = None,
        feature_runtime: FeatureRuntime | None = None,
    ) -> None:
        self._plugins: list[Plugin] = list(plugins or [])
        self.registry = registry
        # 注入给插件的共享服务（如 config / plugin_prefs），经 PluginContext.services 透传
        self.feature_runtime = feature_runtime or FeatureRuntime()
        self.event_contributors = self.feature_runtime.event_contributors
        self.services: dict[str, Any] = {}
        self._host_scope = FeatureScope(FeatureGeneration("ace.host", 1))
        self.publish_host_services(services or {})
        self._host_scope.activate()
        self._hooks: dict[str, list[Callable[..., Any]]] = {}
        self._middleware: dict[str, list[Callable[..., Any]]] = {}
        # hook/middleware 的归属表（plugin_key, callback），与上面两结构平行维护，供按插件摘除
        self._hook_owners: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._middleware_owners: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._plugin_commands: dict[str, dict[str, Any]] = {}
        self._api_routers: dict[str, Any] = {}
        self._loaded: dict[str, LoadedPlugin] = {}
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

    def publish_host_services(self, services: dict[str, Any]) -> None:
        """Publish composition-root services to both compatibility and typed views."""
        for raw_name, value in services.items():
            key = ServiceKey[Any](str(raw_name))
            if key.name in self.services:
                current = self.services[key.name]
                if current is not value:
                    raise ValueError(f"host service {key.name!r} is already published")
                continue
            existing = self.feature_runtime.services.get(key)
            if existing is not None:
                if existing is not value:
                    raise ValueError(
                        f"runtime service {key.name!r} conflicts with the host value"
                    )
            else:
                self.feature_runtime.services.register(
                    self._host_scope,
                    key,
                    value,
                    label=f"service:{key.name}@host",
                )
            self.services[key.name] = value

    def resolve_service(
        self,
        key: str | ServiceKey[Any],
        default: Any = None,
    ) -> Any:
        """Resolve a currently visible global service for host integrations."""
        service_key = key if isinstance(key, ServiceKey) else ServiceKey(str(key))
        return self.feature_runtime.services.get(service_key, default=default)

    def acquire_service_lease(
        self,
        key: ServiceKey[ServiceT],
        *,
        label: str = "service-request",
        generation: FeatureGeneration | None = None,
    ) -> tuple[ServiceT, FeatureLease] | None:
        """Resolve a service and lease its active Feature generation together."""
        return self.feature_runtime.services.acquire_lease(
            key,
            label=label,
            generation=generation,
        )

    def retry_waiting(self) -> None:
        """Synchronously retry plugins whose required host services arrived later."""
        _run_async_compat(self.retry_waiting_async())

    async def retry_waiting_async(self) -> None:
        definitions = [
            loaded.feature_record.definition
            for loaded in self._loaded.values()
            if loaded.feature_record is not None
            and loaded.feature_record.state is FeatureState.WAITING
            and loaded.manifest.activation_phase == "build"
            and not loaded.activation_suppressed
        ]
        if not definitions:
            return
        records = await self.feature_runtime.activate_many(definitions)
        for record in records:
            loaded = self._loaded.get(record.definition.feature_id)
            if loaded is not None:
                self._apply_feature_record(loaded, record)

    def activate_phase(self, phase: str = "startup") -> None:
        """Synchronously activate plugins assigned to one host lifecycle phase."""
        _run_async_compat(self.activate_phase_async(phase))

    async def activate_phase_async(self, phase: str = "startup") -> tuple[LoadedPlugin, ...]:
        """Activate discovered plugins for ``phase`` on the caller's event loop.

        Discovery always records every selected definition first.  Only the
        ``build`` phase is installed during synchronous construction; phases
        such as ``startup`` are deliberately activated by the long-lived host
        loop so background tasks and async resources keep the right ownership.
        """
        normalized = str(phase or "").strip().lower()
        if normalized == "deferred":
            normalized = "startup"
        if normalized not in VALID_ACTIVATION_PHASES:
            raise ValueError(
                "plugin activation phase must be one of: build, startup"
            )
        definitions = [
            loaded.feature_record.definition
            for loaded in self._loaded.values()
            if loaded.feature_record is not None
            and loaded.manifest.activation_phase == normalized
            and not loaded.activation_suppressed
            and loaded.feature_record.state
            in {FeatureState.DISCOVERED, FeatureState.WAITING}
        ]
        if not definitions:
            return ()
        records = await self.feature_runtime.activate_many(definitions)
        activated: list[LoadedPlugin] = []
        for record in records:
            loaded = self._loaded.get(record.definition.feature_id)
            if loaded is None:
                continue
            self._apply_feature_record(loaded, record)
            activated.append(loaded)
        return tuple(activated)

    async def activate_deferred_async(self) -> tuple[LoadedPlugin, ...]:
        """Compatibility name for the host startup phase."""
        return await self.activate_phase_async("startup")

    def activate_deferred(self) -> None:
        """Synchronously activate the deferred startup phase."""
        self.activate_phase("startup")

    def update_plugin(
        self,
        key: str,
        *,
        desired_config_revision: int | None = None,
    ) -> FeatureUpdateResult:
        """Synchronously restart one active directory plugin generation."""
        return _run_async_compat(
            self.update_plugin_async(
                key,
                desired_config_revision=desired_config_revision,
            )
        )

    async def update_plugin_async(
        self,
        key: str,
        *,
        desired_config_revision: int | None = None,
    ) -> FeatureUpdateResult:
        """Prepare and restart one plugin without an unload/activate gap.

        The candidate LoadedPlugin is kept private until FeatureRuntime has
        switched to its new Generation.  Restart recovery therefore continues
        to use the previous definition and metadata when installation fails.
        """
        loaded = self.get_plugin(key)
        if loaded is None:
            raise KeyError(f"unknown plugin {key!r}")
        record = loaded.feature_record
        if not loaded.enabled or record is None or record.scope is None:
            state = record.state.value if record is not None else "undiscovered"
            raise RuntimeError(f"plugin {key!r} cannot update while {state}")
        revision = (
            record.desired_config_revision + 1
            if desired_config_revision is None
            else int(desired_config_revision)
        )
        if revision <= record.desired_config_revision:
            raise ValueError("plugin config revision must increase monotonically")

        candidate, definition = self._prepare_feature(loaded.manifest)
        definition = replace(definition, desired_config_revision=revision)
        owner_key = loaded.manifest.key or loaded.manifest.name
        result = await self.feature_runtime.update(definition)
        current = self.feature_runtime.get(owner_key)
        if current is None:
            raise RuntimeError(f"plugin {owner_key!r} update lost its runtime record")
        if result.updated:
            self._apply_feature_record(candidate, current)
            self._loaded[owner_key] = candidate
        else:
            # Runtime restart recovery may have rebuilt the old definition;
            # refresh its metadata without replacing the live LoadedPlugin.
            self._apply_feature_record(loaded, current)
        return result

    async def reload_plugin_async(
        self,
        key: str,
        *,
        desired_config_revision: int | None = None,
    ) -> FeatureUpdateResult:
        """Alias for the explicit Generation-preserving update operation."""
        return await self.update_plugin_async(
            key,
            desired_config_revision=desired_config_revision,
        )

    def startup_audit(self) -> FeatureStartupAudit:
        """Audit every enabled candidate that entered the Feature Runtime."""
        feature_ids = [
            loaded.manifest.key or loaded.manifest.name
            for loaded in self._loaded.values()
            if loaded.feature_record is not None
        ]
        return self.feature_runtime.startup_audit(feature_ids)

    def log_startup_audit(self) -> FeatureStartupAudit:
        """Emit one explicit startup diagnostic for every unresolved feature."""
        report = self.startup_audit()
        for issue in report.issues:
            if issue.missing_required:
                detail = f"missing required services: {', '.join(issue.missing_required)}"
            else:
                detail = issue.error or f"state is {issue.state}"
            log.error("插件启动审计失败: %s: %s", issue.feature_id, detail)
        return report

    def discover_and_load(
        self,
        plugin_dirs: list[str | Path] | None = None,
        *,
        enabled: list[str] | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        """同步宿主兼容入口；完整等待发现、卸载、回滚和重新加载。"""
        _run_async_compat(
            self.discover_and_load_async(
                plugin_dirs,
                enabled=enabled,
                disabled=disabled,
            )
        )

    async def discover_and_load_async(
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

        for loaded in list(self._loaded.values()):
            if loaded.feature_record is None or loaded.feature_record.scope is None:
                continue
            if not await self.unload_plugin_async(loaded.manifest.key or loaded.manifest.name):
                raise RuntimeError(
                    f"插件 {loaded.manifest.key or loaded.manifest.name} 未能完整卸载: "
                    f"{loaded.error or 'unknown cleanup error'}"
                )

        self._loaded.clear()
        self._hooks.clear()
        self._middleware.clear()
        self._hook_owners.clear()
        self._middleware_owners.clear()
        self._plugin_commands.clear()
        self._api_routers.clear()
        self._clear_plugin_platform_entries()
        build_definitions: list[FeatureDefinition] = []
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
                loaded, definition = self._prepare_feature(manifest)
                self._loaded[lookup_key] = loaded
                # Discover every selected plugin before activation so startup
                # audit and dependency diagnostics include deferred features.
                loaded.feature_record = self.feature_runtime.discover(definition)
                if manifest.activation_phase == "build":
                    build_definitions.append(definition)

        records = await self.feature_runtime.activate_many(build_definitions)
        for record in records:
            loaded = self._loaded[record.definition.feature_id]
            self._apply_feature_record(loaded, record)

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
        """同步卸载入口；异步宿主必须改用 ``unload_plugin_async``。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.unload_plugin_async(key))
        raise RuntimeError("事件循环中请使用 await unload_plugin_async(key)")

    async def unload_plugin_async(
        self,
        key: str,
        *,
        host_shutdown: bool = False,
    ) -> bool:
        """停用插件并等待该 Generation 的全部注册和资源完成清理。"""
        loaded = self.get_plugin(key)
        if loaded is None:
            return False
        if not loaded.enabled:
            # A selected deferred plugin has no Generation to stop yet.  Treat
            # unloading that discovered/waiting definition as an idempotent
            # success while preserving failed and explicitly disabled states.
            record = loaded.feature_record
            if (
                record is not None
                and record.scope is None
                and record.state in {
                    FeatureState.DISCOVERED,
                    FeatureState.WAITING,
                }
                and not loaded.activation_suppressed
            ):
                loaded.activation_suppressed = True
                return True
            return False
        owner_key = loaded.manifest.key or loaded.manifest.name
        policy = (
            FeatureStopPolicy.IMMEDIATE
            if host_shutdown
            and loaded.manifest.stop_policy is FeatureStopPolicy.RESTART_REQUIRED
            else None
        )
        stopped = await self.feature_runtime.deactivate(owner_key, policy=policy)
        record = loaded.feature_record
        retained = (
            not stopped
            and record is not None
            and record.state in {FeatureState.ACTIVE, FeatureState.DRAINING}
        )
        loaded.enabled = retained
        if not retained:
            loaded.activation_suppressed = True
            loaded.tools_registered.clear()
            loaded.hooks_registered.clear()
            loaded.middleware_registered.clear()
            loaded.commands_registered.clear()
            loaded.api_routers_registered.clear()
            loaded.platforms_registered.clear()
            loaded.disposers.clear()
            loaded.skill_roots.clear()
        loaded.error = None if stopped else str(record.error if record else "cleanup failed")
        if stopped:
            log.info("插件已卸载: %s", owner_key)
        else:
            log.error("插件 %s 未能完整卸载: %s", owner_key, loaded.error)
        return stopped

    async def aclose(self) -> tuple[str, ...]:
        """Stop every loaded directory plugin in reverse discovery order."""
        failed: list[str] = []
        for loaded in reversed(list(self._loaded.values())):
            if not loaded.enabled:
                continue
            owner_key = loaded.manifest.key or loaded.manifest.name
            if not await self.unload_plugin_async(owner_key, host_shutdown=True):
                failed.append(owner_key)
        return tuple(failed)

    @staticmethod
    def _remove_owned_callback(
        table: dict[str, list[Callable[..., Any]]],
        owners: dict[str, list[tuple[str, Callable[..., Any]]]],
        name: str,
        owner_key: str,
        callback: Callable[..., Any],
    ) -> None:
        """Remove one exact callback without touching a replacement generation."""
        callbacks = table.get(name, [])
        for index, candidate in enumerate(callbacks):
            if candidate is callback:
                callbacks.pop(index)
                break
        owned_callbacks = owners.get(name, [])
        for index, (candidate_key, candidate) in enumerate(owned_callbacks):
            if candidate_key == owner_key and candidate is callback:
                owned_callbacks.pop(index)
                break
        if not callbacks:
            table.pop(name, None)
        if not owned_callbacks:
            owners.pop(name, None)

    def _source_for_root(self, root: Path) -> str:
        root = root.resolve()
        if root == get_bundled_plugins_dir().resolve():
            return "bundled"
        if root == get_user_plugins_dir().resolve():
            return "project"
        return "local"

    @staticmethod
    def _manifest_drain_timeout(raw: dict[str, Any]) -> float | None:
        value = raw.get("drain_timeout_seconds", 30.0)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("plugin manifest drain_timeout_seconds must be a number or null")
        timeout = float(value)
        if timeout < 0:
            raise ValueError("plugin manifest drain_timeout_seconds must not be negative")
        return timeout

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
                requires_services=_manifest_service_names(raw, "requires"),
                optional_services=_manifest_service_names(raw, "optional"),
                provides_services=_manifest_service_names(raw, "provides"),
                provides_tools=list(raw.get("provides_tools") or []),
                provides_hooks=list(raw.get("provides_hooks") or []),
                config_schema=dict(raw.get("config_schema") or raw.get("configSchema") or {}),
                ui_hints=dict(raw.get("ui_hints") or raw.get("uiHints") or {}),
                stop_policy=FeatureStopPolicy(raw.get("stop_policy") or "drain"),
                drain_timeout_seconds=self._manifest_drain_timeout(raw),
                update_strategy=FeatureUpdateStrategy(
                    raw.get("update_strategy") or "restart"
                ),
                activation_phase=self._manifest_activation_phase(raw),
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

    @staticmethod
    def _manifest_activation_phase(raw: dict[str, Any]) -> str:
        value = str(
            raw.get("activation_phase")
            if raw.get("activation_phase") is not None
            else raw.get("activation") or "build"
        ).strip().lower()
        # ``deferred`` was used by early local manifests; accepting it keeps
        # discovery forward-compatible while exposing one stable phase name.
        if value == "deferred":
            value = "startup"
        if value not in VALID_ACTIVATION_PHASES:
            raise ValueError(
                "plugin manifest activation_phase must be one of: build, startup"
            )
        return value

    def _load_plugin(self, manifest: PluginManifest) -> None:
        """Compatibility wrapper for callers that load one parsed manifest."""
        _run_async_compat(self._load_plugin_async(manifest))

    def _prepare_feature(
        self,
        manifest: PluginManifest,
    ) -> tuple[LoadedPlugin, FeatureDefinition]:
        loaded = LoadedPlugin(manifest=manifest)

        def install(feature_context: FeatureInstallContext) -> Any:
            module = self._load_module(manifest)
            register = getattr(module, "register", None)
            if register is None:
                raise RuntimeError("缺少 register(ctx) 函数")
            return register(PluginContext(manifest, self, feature_context, loaded))

        owner_key = manifest.key or manifest.name
        dependencies = FeatureServiceDependencies(
            owner_key,
            requires=tuple(ServiceKey[Any](name) for name in manifest.requires_services),
            optional=tuple(ServiceKey[Any](name) for name in manifest.optional_services),
            provides=tuple(ServiceKey[Any](name) for name in manifest.provides_services),
        )
        return loaded, FeatureDefinition(
            owner_key,
            install,
            dependencies=dependencies,
            stop_policy=manifest.stop_policy,
            drain_timeout_seconds=manifest.drain_timeout_seconds,
            update_strategy=manifest.update_strategy,
        )

    def _apply_feature_record(
        self,
        loaded: LoadedPlugin,
        record: FeatureRecord,
    ) -> None:
        loaded.feature_record = record
        loaded.enabled = record.state is FeatureState.ACTIVE
        loaded.hooks_registered = list(dict.fromkeys(loaded.hooks_registered))
        loaded.middleware_registered = list(dict.fromkeys(loaded.middleware_registered))
        loaded.commands_registered = list(dict.fromkeys(loaded.commands_registered))
        loaded.api_routers_registered = list(dict.fromkeys(loaded.api_routers_registered))
        loaded.platforms_registered = list(dict.fromkeys(loaded.platforms_registered))
        loaded.skill_roots = list(dict.fromkeys(loaded.skill_roots))
        if loaded.enabled:
            loaded.error = str(record.error) if record.error else None
        else:
            error = record.error
            if isinstance(error, FeatureActivationError):
                error = error.cause
            if record.state is FeatureState.DISCOVERED:
                # Deferred plugins are intentionally quiet until their host
                # lifecycle phase runs; discovery is not an activation error.
                loaded.error = None
                return
            if record.state is FeatureState.WAITING and record.dependency_resolution:
                missing = ", ".join(
                    key.name for key in record.dependency_resolution.missing_required
                )
                loaded.error = f"missing required services: {missing}"
            else:
                loaded.error = str(error or f"feature state is {record.state.value}")
            loaded.tools_registered.clear()
            loaded.hooks_registered.clear()
            loaded.middleware_registered.clear()
            loaded.commands_registered.clear()
            loaded.api_routers_registered.clear()
            loaded.platforms_registered.clear()
            loaded.disposers.clear()
            loaded.skill_roots.clear()
            log_method = log.info if record.state is FeatureState.WAITING else log.error
            log_method("加载插件失败: %s: %s", loaded.manifest.name, loaded.error)

    async def _load_plugin_async(self, manifest: PluginManifest) -> None:
        loaded, definition = self._prepare_feature(manifest)
        record = await self.feature_runtime.activate(definition)
        self._apply_feature_record(loaded, record)
        owner_key = manifest.key or manifest.name
        self._loaded[owner_key] = loaded

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
            from crew.channels.platform_registry import platform_registry

            platform_registry.clear_plugin_entries()
        except Exception as exc:  # noqa: BLE001
            log.warning("清理插件平台注册表失败: %s", exc)

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
