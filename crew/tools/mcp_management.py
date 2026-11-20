"""Save and connect MCP servers, including explicitly approved chat installations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crew.core.errors import StructuredToolError
from crew.core.runctx import (
    current_mcp_install_allowed,
    current_owner_account_id,
    current_request_id,
    current_session_id,
    current_tool_call_id,
)
from crew.core.types import ToolPermissionDecision
from crew.state.logging import get_logger
from crew.tools.mcp_client import host_stdio_block_reason
from crew.tools.redact import redact_secret_values, redact_sensitive_text
from crew.tools.registry import tool_result

log = get_logger("tools.mcp_management")
MCP_INSTALL_TOOL = "mcp_install"
MCP_STATUS_TOOL = "mcp_status"
MCP_REMOVE_TOOL = "mcp_remove"
SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_VALID_TRANSPORTS = {"stdio", "http", "sse"}
_STATUS_MAX_TOOLS = 50
_STATUS_MAX_ERROR_CHARS = 500


def validate_server_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """校验新增/编辑 payload，返回 (规范化配置, 错误信息)。"""
    cfg: dict[str, Any] = {}
    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()
    transport = str(payload.get("transport") or "stdio").strip().lower()

    if command:
        cfg["command"] = command
        cfg["args"] = list(payload.get("args") or [])
        # transport 仅作记录，stdio 时忽略
    elif url:
        if transport not in _VALID_TRANSPORTS:
            return None, f"transport 必须是 {sorted(_VALID_TRANSPORTS)} 之一"
        cfg["url"] = url
        cfg["transport"] = transport
        headers = payload.get("headers")
        if headers is not None:
            cfg["headers"] = dict(headers)
    else:
        return None, "必须提供 command（stdio）或 url（http/sse）"

    env = payload.get("env")
    if env is not None:
        if not isinstance(env, dict):
            return None, "env 必须是对象"
        cfg["env"] = {str(k): str(v) for k, v in env.items()}
    return cfg, None


async def add_mcp_server(app: Any, name: str, config: dict[str, Any]) -> asyncio.Task[bool] | None:
    """Publish the saved configuration before starting the new connection."""
    app.config.add_mcp_server(name, config)
    manager = app.mcp_manager
    if manager is None:
        return None
    try:
        if manager._registry is None:
            await manager.start(app.registry)
        manager.register_pending(name, config)
    except Exception:
        log.exception("MCP 配置已保存，连接尚未启动 name=%s", name)
        return None
    return asyncio.create_task(manager.add_server(name, config))


@dataclass(slots=True)
class _OneShotApproval:
    digest: str
    consumed: bool = False


_approved_install: ContextVar[_OneShotApproval | None] = ContextVar("approved_mcp_install", default=None)
_approved_remove: ContextVar[_OneShotApproval | None] = ContextVar("approved_mcp_remove", default=None)


def _proposal(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not current_mcp_install_allowed.get() or not current_tool_call_id.get():
        raise StructuredToolError("请在有管理权限的前台对话中安装 MCP。", code="permission_denied")
    name = str(args.get("name") or "").strip()
    if not SERVER_NAME_RE.fullmatch(name):
        raise StructuredToolError("MCP 名称只能使用字母、数字、下划线或连字符。", code="invalid_arguments")

    config = dict(args)
    command = str(config.get("command") or "").strip()
    url = str(config.get("url") or "").strip()
    transport = str(config.get("transport") or ("stdio" if command else "http")).lower()
    if bool(command) == bool(url) or (command and transport != "stdio") or (
        url and transport not in {"http", "sse"}
    ):
        raise StructuredToolError(
            "stdio 需要 command，HTTP 或 SSE 需要 url。请只提供一种连接方式。",
            code="invalid_arguments",
        )
    for field in ("env", "headers"):
        if field in config and (
            not isinstance(config[field], dict)
            or any(not isinstance(value, str) for value in config[field].values())
        ):
            raise StructuredToolError(f"{field} 必须是字符串映射。", code="invalid_arguments")
    if (command and config.get("headers")) or (url and (config.get("env") or config.get("args"))):
        raise StructuredToolError("配置字段与 MCP 传输方式不匹配。", code="invalid_arguments")
    if "args" in config and (
        not isinstance(config["args"], list) or any(not isinstance(arg, str) for arg in config["args"])
    ):
        raise StructuredToolError("args 必须是字符串数组。", code="invalid_arguments")
    if url:
        try:
            target = urlsplit(url)
            valid = (
                target.scheme in {"http", "https"}
                and target.hostname
                and target.username is None
                and target.password is None
                and not target.fragment
            )
            _ = target.port
        except ValueError:
            valid = False
        if not valid:
            raise StructuredToolError("请提供有效的 MCP HTTP(S) 地址。", code="invalid_arguments")
    config["transport"] = transport
    validated, error = validate_server_payload(config)
    if error or validated is None:
        raise StructuredToolError(error or "MCP 配置无效。", code="invalid_arguments")
    return name, validated


def _approval_digest(config_path: str, name: str, config: dict[str, Any]) -> str:
    scope = (
        current_owner_account_id.get(), current_session_id.get(), current_request_id.get(),
        current_tool_call_id.get(), config_path, name, config,
    )
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _removal_name(args: dict[str, Any]) -> str:
    if not current_mcp_install_allowed.get() or not current_tool_call_id.get():
        raise StructuredToolError("请在有管理权限的前台对话中移除 MCP。", code="permission_denied")
    name = str(args.get("name") or "").strip()
    if not SERVER_NAME_RE.fullmatch(name):
        raise StructuredToolError("MCP 名称只能使用字母、数字、下划线或连字符。", code="invalid_arguments")
    return name


def _removal_digest(config_path: str, name: str, config: dict[str, Any]) -> str:
    scope = (
        current_owner_account_id.get(), current_session_id.get(), current_request_id.get(),
        current_tool_call_id.get(), config_path, "remove", name, config,
    )
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _install_status(app: Any, name: str, config: dict[str, Any], *, already: bool = False) -> str:
    rows = app.mcp_manager.status() if app.mcp_manager is not None else ()
    status = next((row for row in rows if row["name"] == name), {})
    connected = bool(status.get("connected"))
    return tool_result(
        name=name,
        transport="stdio" if config.get("command") else config["transport"],
        saved=True,
        connected=connected,
        status="connected" if connected else "saved_not_connected",
        tool_count=len(status.get("tools", ())) if connected else 0,
        already_configured=already,
    )


def _public_server_status(row: dict[str, Any]) -> dict[str, Any]:
    """Project one manager status row without configuration secrets."""
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    secret_values: list[str] = []
    for field in ("env", "headers"):
        values = config.get(field)
        if isinstance(values, dict):
            secret_values.extend(str(value) for value in values.values() if str(value))
    command = str(config.get("command") or "").strip()
    url = str(config.get("url") or "").strip()
    target = Path(command).name if command else _public_url_origin(url)
    error = str(row.get("error") or "").strip()
    if error:
        if url and target and url in error:
            error = error.replace(url, target)
        error = redact_secret_values(error, secret_values) or error
        error = redact_sensitive_text(error, force=True)[:_STATUS_MAX_ERROR_CHARS]
    raw_tools = row.get("tools")
    tools = (
        [str(name)[:120] for name in raw_tools[:_STATUS_MAX_TOOLS]]
        if isinstance(raw_tools, (list, tuple))
        else []
    )
    result: dict[str, Any] = {
        "name": str(row.get("name") or ""),
        "transport": str(row.get("transport") or config.get("transport") or ""),
        "target": target,
        "connected": bool(row.get("connected")),
        "tool_count": len(raw_tools) if isinstance(raw_tools, (list, tuple)) else 0,
        "tools": tools,
    }
    if isinstance(config.get("enabled"), bool):
        result["enabled"] = bool(config["enabled"])
    if error:
        result["error"] = error
    return result


def _public_url_origin(url: str) -> str:
    """Keep only scheme, host and port; paths and query tokens stay private."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    if not host or not parsed.scheme:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _require_management_context() -> None:
    if not current_mcp_install_allowed.get() or not current_tool_call_id.get():
        raise StructuredToolError("请在有管理权限的前台对话中查看 MCP 状态。", code="permission_denied")


def register_mcp_tools(app: Any) -> None:
    """Add the interactive MCP install and read-only status tools."""

    def describe_permission(args: dict[str, Any]) -> ToolPermissionDecision:
        try:
            name, config = _proposal(args)
        except StructuredToolError as exc:
            return ToolPermissionDecision("deny", str(exc))
        existing = app.config.mcp_servers.get(name)
        if existing is not None:
            if existing == config:
                return ToolPermissionDecision("allow")
            return ToolPermissionDecision("deny", f"已存在同名 MCP 服务 {name}，请使用其他名称。")
        if config.get("command") and host_stdio_block_reason():
            return ToolPermissionDecision(
                "deny", "当前运行策略不允许启动宿主 stdio MCP。可使用同服务的 HTTP 或 SSE 地址。",
            )
        if not app.config.config_path:
            return ToolPermissionDecision("deny", "当前配置没有可写入的配置文件。")
        return ToolPermissionDecision(
            "ask",
            f"将 {name} 添加到此 Ace 实例的 MCP 设置并尝试连接。",
            allow_always=False,
            approval_token=_approval_digest(app.config.config_path, name, config),
        )

    def confirm_permission(digest: str, args: dict[str, Any]) -> bool:
        try:
            name, config = _proposal(args)
        except StructuredToolError:
            return False
        if digest != _approval_digest(app.config.config_path, name, config):
            return False
        _approved_install.set(_OneShotApproval(digest))
        return True

    async def install(args: dict[str, Any]) -> str:
        name, config = _proposal(args)
        existing = app.config.mcp_servers.get(name)
        if existing is not None:
            if existing == config:
                return _install_status(app, name, config, already=True)
            raise StructuredToolError(f"已存在同名 MCP 服务 {name}。", code="mcp_server_exists")

        approved = _approved_install.get()
        if (
            approved is None or approved.consumed
            or approved.digest != _approval_digest(app.config.config_path, name, config)
        ):
            raise StructuredToolError("此 MCP 配置缺少有效的一次性批准。", code="approval_required")
        approved.consumed = True
        if config.get("command") and host_stdio_block_reason():
            raise StructuredToolError("当前运行策略不允许启动宿主 stdio MCP。", code="permission_denied")

        try:
            connection = await add_mcp_server(app, name, config)
        except ValueError as exc:
            raise StructuredToolError(f"已存在同名 MCP 服务 {name}。", code="mcp_server_exists") from exc
        except Exception as exc:
            log.exception("保存 MCP 配置失败 name=%s", name)
            raise StructuredToolError("MCP 配置保存失败。", code="mcp_save_failed") from exc
        if connection is not None:
            await asyncio.shield(connection)
        return _install_status(app, name, config)

    string_map = {"type": "object", "additionalProperties": {"type": "string"}}
    app.registry.register(
        name=MCP_INSTALL_TOOL,
        toolset="mcp_management",
        schema={
            "name": MCP_INSTALL_TOOL,
            "description": (
                "Install an MCP server requested by the user and report its actual connection status. "
                "Use an explicitly supplied or verified server address or command. HTTP and SSE use url. "
                "Stdio uses command and a separate args list. The user approves the exact configuration. "
                "When connected, the new server tools can be discovered in the next turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Stable server name, e.g. docs-search"},
                    "transport": {"type": "string", "enum": ["stdio", "http", "sse"]},
                    "command": {"type": "string", "description": "Executable name or absolute path for stdio"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "url": {"type": "string", "description": "Full HTTP or SSE endpoint"},
                    "env": string_map,
                    "headers": string_map,
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        handler=install,
        is_async=True,
        display_name="安装 MCP",
        permission_resolver=describe_permission,
        permission_approver=confirm_permission,
    )

    async def list_status(args: dict[str, Any]) -> str:
        _require_management_context()
        manager = getattr(app, "mcp_manager", None)
        rows = manager.status() if manager is not None else []
        wanted = str(args.get("name") or "").strip()
        if wanted:
            rows = [row for row in rows if str(row.get("name") or "") == wanted]
        servers = [_public_server_status(row) for row in rows]
        return tool_result(count=len(servers), servers=servers)

    app.registry.register(
        name=MCP_STATUS_TOOL,
        toolset="mcp_management",
        schema={
            "name": MCP_STATUS_TOOL,
            "description": (
                "List configured MCP servers and their actual connection state. "
                "Use this to diagnose why a server or its tools are unavailable. "
                "Read-only: it never changes configuration. Configuration values and "
                "credentials are not returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional exact server name to inspect",
                    },
                },
                "additionalProperties": False,
            },
        },
        handler=list_status,
        is_async=True,
        display_name="查看 MCP 状态",
    )

    def describe_remove_permission(args: dict[str, Any]) -> ToolPermissionDecision:
        try:
            name = _removal_name(args)
        except StructuredToolError as exc:
            return ToolPermissionDecision("deny", str(exc))
        existing = app.config.mcp_servers.get(name)
        if existing is None:
            return ToolPermissionDecision("allow")
        if not app.config.config_path:
            return ToolPermissionDecision("deny", "当前配置没有可写入的配置文件。")
        return ToolPermissionDecision(
            "ask",
            f"将从此 Ace 实例移除 MCP 服务 {name}，并断开它的连接。",
            allow_always=False,
            approval_token=_removal_digest(app.config.config_path, name, existing),
        )

    def confirm_remove_permission(digest: str, args: dict[str, Any]) -> bool:
        try:
            name = _removal_name(args)
        except StructuredToolError:
            return False
        existing = app.config.mcp_servers.get(name)
        if existing is None or digest != _removal_digest(app.config.config_path, name, existing):
            return False
        _approved_remove.set(_OneShotApproval(digest))
        return True

    async def remove(args: dict[str, Any]) -> str:
        name = _removal_name(args)
        existing = app.config.mcp_servers.get(name)
        if existing is None:
            return tool_result(name=name, removed=False, already_absent=True)
        approved = _approved_remove.get()
        if (
            approved is None or approved.consumed
            or approved.digest != _removal_digest(app.config.config_path, name, existing)
        ):
            raise StructuredToolError("此 MCP 移除缺少有效的一次性批准。", code="approval_required")
        approved.consumed = True
        try:
            app.config.delete_mcp_server(name)
        except KeyError:
            return tool_result(name=name, removed=False, already_absent=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP 配置删除失败 name=%s", name)
            raise StructuredToolError("MCP 配置删除失败。", code="mcp_delete_failed") from exc

        disconnected = True
        warning = ""
        manager = getattr(app, "mcp_manager", None)
        if manager is not None:
            try:
                await manager.remove_server(name)
            except Exception:  # noqa: BLE001
                # Configuration is already gone; report the stale runtime honestly.
                disconnected = False
                warning = "配置已删除，但运行实例移除失败；重启后生效。"
                log.exception("MCP 配置已删除，但运行实例移除失败 name=%s", name)
        result: dict[str, Any] = {
            "name": name,
            "removed": True,
            "already_absent": False,
            "disconnected": disconnected,
        }
        if warning:
            result["warning"] = warning
        return tool_result(**result)

    app.registry.register(
        name=MCP_REMOVE_TOOL,
        toolset="mcp_management",
        schema={
            "name": MCP_REMOVE_TOOL,
            "description": (
                "Remove an MCP server from this Ace instance after the user approves "
                "the exact server. Deleting an already absent server is a no-op."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact server name to remove"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        handler=remove,
        is_async=True,
        display_name="移除 MCP",
        permission_resolver=describe_remove_permission,
        permission_approver=confirm_remove_permission,
    )
