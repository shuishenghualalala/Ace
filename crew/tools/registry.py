"""工具注册表实现。

用于 的工具注册方式：
registry.register(name=..., toolset=..., schema=..., handler=...)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable
from typing import Any

from crew.core.errors import ToolError, ToolNotFoundError
from crew.core.interfaces import Tool, ToolRegistry
from crew.core.types import ToolCall, ToolOutput, ToolPermissionDecision, ToolResult
from crew.state.logging import get_logger
from crew.tools.pipeline import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    truncate_or_persist,
    validate_arguments,
    validate_input_hook,
)
from crew.tools.redact import redact_sensitive_display_text

log = get_logger("tools")

_TOOL_ERROR_HOST_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\|/(?:users|home|private|tmp|var|etc|opt|workspace)(?:[\\/]|$))"
)
_SAFE_TOOL_ERROR_CODE_RE = re.compile(r"^SECURITY_[A-Z0-9_]{2,80}$")
_UNTRUSTED_RESULT_SOURCES = frozenset(
    {"browser", "external_agent", "file", "mcp", "plugin", "subagent", "web"}
)


def _safe_tool_error_message(exc: BaseException | str, *, limit: int = 500) -> str:
    """Keep business errors useful while blocking host paths and secrets."""
    raw = str(exc or "").strip()
    try:
        structured = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        structured = None
    if isinstance(structured, dict):
        code = str(structured.get("code") or "").strip()
        if _SAFE_TOOL_ERROR_CODE_RE.fullmatch(code):
            reason = redact_sensitive_display_text(str(structured.get("reason") or "")).strip()
            if not reason or _TOOL_ERROR_HOST_PATH_RE.search(reason):
                reason = "安全策略拒绝"
            return json.dumps(
                {"code": code, "reason": reason[:limit]},
                ensure_ascii=False,
                sort_keys=True,
            )
    message = redact_sensitive_display_text(raw).strip()
    if not message or _TOOL_ERROR_HOST_PATH_RE.search(message):
        return "工具执行失败：内部错误"
    return message[:limit]


def tool_error(message: str, **extra: Any) -> str:
    """Crew工具错误结果。handler 可直接 return 这个字符串。"""
    payload: dict[str, Any] = dict(extra)
    # ``error`` is diagnostic text, never caller-controlled metadata.  Keep it
    # last so an extra field cannot reintroduce a raw secret/path.
    payload["error"] = _safe_tool_error_message(message)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: Any | None = None, **kwargs: Any) -> str:
    """Crew工具成功结果。"""
    if data is not None and kwargs:
        raise ToolError("tool_result 不能同时传 data 和 kwargs")
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)


def _tool_result_provenance(tool: Tool) -> tuple[str, str]:
    """Derive result trust from host-owned registration metadata."""
    source = str(getattr(tool, "result_source", "") or "").strip().lower()
    toolset = str(getattr(tool, "toolset", "") or "").strip().lower()
    name = str(getattr(tool, "name", "") or "").strip().lower()
    if not source:
        if bool(getattr(tool, "is_mcp", False)) or toolset.startswith("mcp:"):
            source = "mcp"
        elif toolset == "web":
            source = "web"
        elif toolset in {"file", "vision"} or name == "file_read":
            source = "file"
        elif toolset == "browser" or name.startswith("browser_") or name == "record_replay":
            source = "browser"
        elif toolset in {"external_agent", "subagent"}:
            source = toolset
    if source in _UNTRUSTED_RESULT_SOURCES:
        return "untrusted", source
    return "trusted", "tool"


def _tool_result(
    tool: Tool | None,
    tool_call_id: str,
    name: str,
    content: str,
    *,
    is_error: bool = False,
    media: list[Any] | None = None,
) -> ToolResult:
    trust, source = _tool_result_provenance(tool) if tool is not None else ("trusted", "tool")
    if trust == "trusted":
        return ToolResult.host_trusted(
            tool_call_id,
            name,
            content,
            is_error=is_error,
            media=list(media or []),
            content_source=source,
        )
    return ToolResult(
        tool_call_id,
        name,
        content,
        is_error=is_error,
        media=list(media or []),
        content_source=source,
    )


class FunctionTool(Tool):
    """把 Crew handler 包装成 Crew Tool。"""

    def __init__(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        display_name: str = "",
        ui_label_template: str = "",
        should_defer: bool | None = None,
        search_hint: str = "",
        always_load: bool = False,
        is_mcp: bool = False,
        aliases: list[str] | None = None,
        validate_input: Callable[[dict[str, Any]], str | None] | None = None,
        max_result_size_chars: int | None = None,
        on_progress: str | None = None,
        permission_resolver: Callable[[dict[str, Any]], ToolPermissionDecision | None] | None = None,
        permission_approver: Callable[[str, dict[str, Any]], bool] | None = None,
        result_source: str = "",
    ) -> None:
        self.name = name
        self.toolset = toolset
        self.schema = dict(schema)
        self.description = description or str(schema.get("description", ""))
        self.parameters = dict(schema.get("parameters") or {"type": "object", "properties": {}})
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = list(requires_env or [])
        self.is_async = is_async
        self.emoji = emoji
        self.display_name = display_name
        self.ui_label_template = ui_label_template
        self.should_defer = should_defer
        self.search_hint = search_hint
        self.always_load = always_load
        self.is_mcp = is_mcp
        # Crew Stage 1：别名 + 废弃警告。alias 命中时记一条 deprecation note 回灌模型。
        self.aliases = list(aliases or [])
        # 业务级 validate 钩子（如 FileEdit 检查文件是否已读过）
        self.validate_input = validate_input
        # Crew Stage 6：结果超过此字符数则落盘，模型收路径+预览（对齐 DEFAULT_MAX_RESULT_SIZE_CHARS）
        self.max_result_size_chars = (
            int(max_result_size_chars) if max_result_size_chars else DEFAULT_MAX_RESULT_SIZE_CHARS
        )
        # Crew Stage 5：onProgress 进度流提示文本（仅 terminal 等流式工具实际使用）
        self.on_progress = on_progress
        self.permission_resolver = permission_resolver
        self.permission_approver = permission_approver
        self.result_source = result_source

    def to_schema(self) -> dict[str, Any]:
        function_schema = dict(self.schema)
        function_schema.setdefault("name", self.name)
        function_schema.setdefault("description", self.description)
        function_schema.setdefault("parameters", self.parameters)
        return {"type": "function", "function": function_schema}

    def ui_meta(self) -> dict[str, str]:
        meta: dict[str, str] = {}
        if self.display_name:
            meta["display_name"] = self.display_name
        if self.ui_label_template:
            meta["ui_label_template"] = self.ui_label_template
        return meta

    def available(self) -> bool:
        if self.check_fn is None:
            return True
        try:
            return bool(self.check_fn())
        except Exception:  # noqa: BLE001 - 可用性检查失败按不可用处理
            log.exception("工具 %s 可用性检查失败", self.name)
            return False

    async def run(self, args: dict[str, Any]) -> str | ToolOutput:
        if self.is_async or inspect.iscoroutinefunction(self.handler):
            result = self.handler(args)
            if inspect.isawaitable(result):
                result = await result
        else:
            # 同步 handler 丢线程池执行，避免阻塞事件循环（拖垮网关心跳）。
            result = await asyncio.to_thread(self.handler, args)
        if isinstance(result, (str, ToolOutput)):
            return result
        return json.dumps(result, ensure_ascii=False)


class Registry(ToolRegistry):
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # alias -> canonical name（Crew Stage 1 别名查找）
        self._aliases: dict[str, str] = {}
        self._runtime_tool_providers: list[Any] = []

    def register_runtime_tool_provider(self, provider: Any) -> None:
        """Register a task-scoped tool provider by object identity."""
        if not any(item is provider for item in self._runtime_tool_providers):
            self._runtime_tool_providers.append(provider)

    def unregister_runtime_tool_provider(self, provider: Any) -> None:
        self._runtime_tool_providers = [
            item for item in self._runtime_tool_providers if item is not provider
        ]

    async def activate_runtime_tool_context(
        self,
        **context: Any,
    ) -> tuple[tuple[Any, object], ...]:
        """Activate providers before schemas for one task are collected."""
        leases: list[tuple[Any, object]] = []
        for provider in tuple(self._runtime_tool_providers):
            prepare = getattr(provider, "prepare_runtime_tools", None)
            if not callable(prepare):
                continue
            try:
                lease = prepare(**context)
                if inspect.isawaitable(lease):
                    lease = await lease
            except Exception:  # noqa: BLE001 - one optional provider must not break a turn
                log.exception(
                    "运行期工具 Provider 激活失败: %s",
                    type(provider).__name__,
                )
                continue
            if lease is not None:
                leases.append((provider, lease))
        return tuple(leases)

    async def release_runtime_tool_context(
        self,
        leases: tuple[tuple[Any, object], ...],
    ) -> None:
        """Release task-scoped provider leases in reverse activation order."""
        for provider, lease in reversed(leases):
            release = getattr(provider, "release_runtime_tools", None)
            if not callable(release):
                continue
            try:
                result = release(lease)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - continue releasing remaining providers
                log.exception(
                    "运行期工具 Provider 释放失败: %s",
                    type(provider).__name__,
                )

    async def revoke_runtime_tool_session(
        self,
        owner_account_id: str,
        session_id: str,
    ) -> None:
        await self._revoke_runtime_tools(
            "revoke_session",
            owner_account_id,
            session_id,
        )

    async def revoke_runtime_tool_owner(self, owner_account_id: str) -> None:
        await self._revoke_runtime_tools("revoke_owner", owner_account_id)

    async def _revoke_runtime_tools(self, method_name: str, *args: str) -> None:
        for provider in tuple(self._runtime_tool_providers):
            revoke = getattr(provider, method_name, None)
            if not callable(revoke):
                continue
            try:
                result = revoke(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - continue fencing other providers
                log.exception(
                    "运行期工具 Provider 撤销失败: %s",
                    type(provider).__name__,
                )

    def register(self, tool: Tool | None = None, **kwargs: Any) -> None:
        if tool is None:
            tool = FunctionTool(
                name=kwargs["name"],
                toolset=kwargs.get("toolset", "default"),
                schema=kwargs["schema"],
                handler=kwargs["handler"],
                check_fn=kwargs.get("check_fn"),
                requires_env=kwargs.get("requires_env"),
                is_async=bool(kwargs.get("is_async", False)),
                description=kwargs.get("description", ""),
                emoji=kwargs.get("emoji", ""),
                display_name=kwargs.get("display_name", ""),
                ui_label_template=kwargs.get("ui_label_template", ""),
                should_defer=kwargs.get("should_defer"),
                search_hint=kwargs.get("search_hint", ""),
                always_load=bool(kwargs.get("always_load", False)),
                is_mcp=bool(kwargs.get("is_mcp", False)),
                aliases=kwargs.get("aliases"),
                validate_input=kwargs.get("validate_input"),
                max_result_size_chars=kwargs.get("max_result_size_chars"),
                on_progress=kwargs.get("on_progress"),
                permission_resolver=kwargs.get("permission_resolver"),
                permission_approver=kwargs.get("permission_approver"),
                result_source=kwargs.get("result_source", ""),
            )
        if not tool.name:
            raise ToolError(f"工具缺少 name: {tool!r}")
        if tool.name in self._tools and not kwargs.get("override", False):
            raise ToolError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool
        # 索引别名（冲突时保留先注册的——内置优先于 MCP）
        for alias in getattr(tool, "aliases", None) or []:
            alias = str(alias).strip()
            if alias and alias not in self._aliases and alias != tool.name:
                self._aliases[alias] = tool.name
                log.debug("注册别名: %s -> %s", alias, tool.name)
        log.debug("注册工具: %s", tool.name)

    def unregister(self, name: str) -> bool:
        """注销一个工具（含其别名）。用于删除/重连单个 MCP server 时清理旧工具。

        返回是否确实移除了工具。内置工具理论上不会被调用方 unregister，但不做硬限制——
        调用方（MCPClientManager）只对本 server 注册过的工具名调用。
        """
        removed = self._tools.pop(name, None) is not None
        # 清理指向该 name 的别名
        stale_aliases = [a for a, c in self._aliases.items() if c == name]
        for a in stale_aliases:
            self._aliases.pop(a, None)
        if removed:
            log.debug("注销工具: %s", name)
        return removed

    def _lookup(self, name: str) -> tuple[Tool, str]:
        """按 name/alias 查找。返回 (tool, deprecation_note)；未找到抛 ToolNotFoundError。

        命中 alias 时返回废弃提示，调用方可附在 tool_result 里引导模型改用新名称
        废弃别名只用于兼容旧调用。
        """
        tool = self._tools.get(name)
        if tool is not None:
            return tool, ""
        canonical = self._aliases.get(name)
        if canonical is not None:
            real = self._tools.get(canonical)
            if real is not None:
                log.info("工具通过别名调用: %s -> %s", name, canonical)
                return real, f"注意：'{name}' 是已废弃的别名，请改用 '{canonical}'。"
        raise ToolNotFoundError(f"未注册的工具: {name}")

    def get(self, name: str) -> Tool:
        return self._lookup(name)[0]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def toolset_for(self, name: str) -> str | None:
        tool = self._tools.get(name)
        return getattr(tool, "toolset", "default") if tool else None

    async def resolve_permission(self, tool_call: ToolCall) -> ToolPermissionDecision | None:
        """Return a tool-specific permission decision, if the tool declares one."""
        try:
            tool, _note = self._lookup(tool_call.name)
        except ToolNotFoundError:
            return None
        resolver = getattr(tool, "permission_resolver", None)
        if not callable(resolver):
            return None
        value = resolver(tool_call.arguments if isinstance(tool_call.arguments, dict) else {})
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, ToolPermissionDecision) else None

    async def confirm_permission(self, tool_call: ToolCall, decision: ToolPermissionDecision) -> bool:
        """Confirm an opaque one-shot approval immediately before execution."""
        try:
            tool, _note = self._lookup(tool_call.name)
        except ToolNotFoundError:
            return False
        approver = getattr(tool, "permission_approver", None)
        if not decision.approval_token:
            return True
        if not callable(approver):
            return False
        value = approver(
            decision.approval_token,
            tool_call.arguments if isinstance(tool_call.arguments, dict) else {},
        )
        if inspect.isawaitable(value):
            value = await value
        return bool(value)

    def ui_meta(self, name: str) -> dict[str, str]:
        tool = self._tools.get(name)
        if tool is None:
            return {}
        meta_fn = getattr(tool, "ui_meta", None)
        if callable(meta_fn):
            meta = meta_fn()
            if isinstance(meta, dict):
                return {str(k): str(v) for k, v in meta.items() if v is not None}
        return {
            key: str(value)
            for key, value in {
                "display_name": getattr(tool, "display_name", ""),
                "ui_label_template": getattr(tool, "ui_label_template", ""),
            }.items()
            if value
        }

    def render_ui_label(self, name: str, args: dict[str, Any] | None = None) -> str:
        """按工具私有 UI 模板渲染展示标题；不会写入 LLM tool schema。"""
        meta = self.ui_meta(name)
        template = meta.get("ui_label_template") or meta.get("display_name") or ""
        if not template:
            return ""
        values = {k: _short_ui_value(v) for k, v in (args or {}).items()}
        try:
            return template.format_map(_SafeFormatDict(values)).strip()
        except Exception:
            return meta.get("display_name", "").strip()

    def toolsets(self) -> list[str]:
        return sorted({getattr(t, "toolset", "default") for t in self._tools.values()})

    def names_for_toolset(self, toolset: str) -> list[str]:
        return [
            t.name for t in self._tools.values()
            if getattr(t, "toolset", "default") == toolset
        ]

    def list_schemas(
        self,
        only: list[str] | None = None,
        *,
        enabled_toolsets: list[str] | None = None,
        disabled_toolsets: list[str] | None = None,
        enabled_tools: list[str] | None = None,
        disabled_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[Tool] = list(self._tools.values())
        if only is not None:
            allow = set(only)
            items = [t for t in items if t.name in allow]
        if enabled_toolsets is not None and enabled_toolsets != ["*"]:
            allow_toolsets = set(enabled_toolsets)
            items = [t for t in items if getattr(t, "toolset", "default") in allow_toolsets]
        if disabled_toolsets and disabled_toolsets != ["*"]:
            deny_toolsets = set(disabled_toolsets)
            items = [t for t in items if getattr(t, "toolset", "default") not in deny_toolsets]
        if disabled_toolsets == ["*"]:
            items = []
        if enabled_tools is not None and enabled_tools != ["*"]:
            allow_tools = set(enabled_tools)
            items = [t for t in items if t.name in allow_tools]
        if disabled_tools and disabled_tools != ["*"]:
            deny_tools = set(disabled_tools)
            items = [t for t in items if t.name not in deny_tools]
        if disabled_tools == ["*"]:
            items = []
        items = [
            t for t in items
            if not hasattr(t, "available") or t.available()  # type: ignore[attr-defined]
        ]
        schemas: list[dict[str, Any]] = []
        for tool in items:
            schema = tool.to_schema()
            schema["_crew_toolset"] = str(getattr(tool, "toolset", "default") or "default")
            should_defer = getattr(tool, "should_defer", None)
            if should_defer is not None:
                schema["_crew_should_defer"] = bool(should_defer)
            search_hint = str(getattr(tool, "search_hint", "") or "")
            if search_hint:
                schema["_crew_search_hint"] = search_hint
            if bool(getattr(tool, "always_load", False)):
                schema["_crew_always_load"] = True
            if bool(getattr(tool, "is_mcp", False)):
                schema["_crew_is_mcp"] = True
            schemas.append(schema)
        return schemas

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        try:
            tool, deprecation_note = self._lookup(tool_call.name)
        except ToolNotFoundError as exc:
            del exc
            return ToolResult(
                tool_call.id,
                tool_call.name,
                f"工具不存在：{tool_call.name}",
                is_error=True,
            )

        args = tool_call.arguments
        # JSON Schema 结构校验
        schema_err = validate_arguments(tool.name, tool.parameters, args)
        if schema_err:
            return _tool_result(
                tool,
                tool_call.id,
                tool.name,
                _wrap_tool_use_error(tool.name, schema_err, deprecation_note),
                is_error=True,
            )
        # 业务级 validate 钩子
        biz_err = validate_input_hook(tool, args if isinstance(args, dict) else {})
        if biz_err:
            return _tool_result(
                tool,
                tool_call.id,
                tool.name,
                _wrap_tool_use_error(tool.name, biz_err, deprecation_note),
                is_error=True,
            )

        try:
            output = await tool.run(args)
            media = output.media if isinstance(output, ToolOutput) else []
            content = output.content if isinstance(output, ToolOutput) else output
            # Crew Stage 6：大结果落盘 + 预览回灌（非 str 原样返回）
            max_chars = getattr(tool, "max_result_size_chars", DEFAULT_MAX_RESULT_SIZE_CHARS)
            content = truncate_or_persist(tool_call.id, tool.name, content, max_chars=max_chars)
            if deprecation_note:
                content = f"{deprecation_note}\n\n{content}"
            return _tool_result(
                tool,
                tool_call.id,
                tool.name,
                content,
                media=media,
            )
        except ToolError as exc:
            message = _safe_tool_error_message(exc)
            if message == "工具执行失败：内部错误":
                return _tool_result(tool, tool_call.id, tool.name, message, is_error=True)
            return _tool_result(
                tool,
                tool_call.id,
                tool.name,
                f"工具执行失败: {message}",
                is_error=True,
            )
        except Exception:  # noqa: BLE001 - 工具内部异常也回灌给模型而非崩溃
            log.exception("工具 %s 异常", tool.name)
            return _tool_result(
                tool,
                tool_call.id,
                tool.name,
                "工具执行失败：内部错误",
                is_error=True,
            )


def register_builtin_tools(
    registry: Registry,
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> None:
    """注册所有内置工具。"""
    from crew.tools.builtin import register_builtin_tools as _register_builtin_tools

    _register_builtin_tools(
        registry,
        workspace_store=workspace_store,
        security_service=security_service,
    )


def _wrap_tool_use_error(tool_name: str, message: str, deprecation_note: str = "") -> str:
    """Crew 风格：把校验错误包进 <tool_use_error> 标签回灌模型，附带废弃提示。"""
    prefix = f"{deprecation_note}\n\n" if deprecation_note else ""
    return f"{prefix}<tool_use_error> 工具 `{tool_name}` 调用被拒绝：\n{message}\n</tool_use_error>"


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


# UI 标题里的长绝对路径：≥16 字符、以 / 开头、前字符不是 word/:/ /（保护 URL 与已有路径片段）。
_LONG_ABS_PATH_RE = re.compile(r"(?<![\w:/])/[^\s'\"`(),;|]{16,}")


def _shrink_long_paths(text: str) -> str:
    """长绝对路径只留 basename（如 python3 /a/b/c/d.py → python3 d.py），仅影响展示。"""
    def _basename(match: re.Match[str]) -> str:
        path = match.group(0)
        return path.rstrip("/").rsplit("/", 1)[-1] or path

    return _LONG_ABS_PATH_RE.sub(_basename, text)


def _short_ui_value(value: Any, *, max_len: int = 160) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    text = _shrink_long_paths(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
