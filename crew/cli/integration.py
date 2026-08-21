"""外部集成命令：MCP、插件、渠道、外部 Runtime/Agent/Team、浏览器、微信登录。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from crew.cli.app import CliContext, CliError, CliResult, parse_json
from crew.gateway.channel_config import channel_raw as resolved_channel_raw
from crew.gateway.channel_manager import ChannelManager
from crew.gateway.channel_sessions import bind_channel_platform_for_owner
from crew.gateway.helpers import require_external_agents_enabled
from crew.gateway.platform_registry import platform_registry
from crew.gateway.routers.channels import (
    _account_remove_keys,
    _apply_environment_preset,
    _enrich_platform_row,
    _has_channel_account,
    _normalize_config_payload,
    _public_channel_config,
    _remove_secret_envs,
    _sanitize_for_yaml,
    _validate_platform_config_ready,
    _validate_secret_fields,
    _wait_for_live_connected,
    _write_env_fields,
)
from crew.gateway.routers.mcp_servers import _NAME_RE, _redact_config, _validate_server_payload
from crew.gateway.routers.plugins import (
    _drop_owner_agent_cache,
    _plugin_states,
    browser_runtime_status,
)
from crew.gateway.routers.runtimes import (
    _external_agent_payloads,
    _external_team_payloads,
    _runtime_availability,
)
from crew.state.logging import get_logger
from crew.state.plugin_preferences import plugin_effective_enabled, plugin_role_allowed

log = get_logger("cli.integration")


def register(subparsers, handlers: dict[str, Any]) -> None:
    _register_mcp(subparsers)
    _register_plugin(subparsers)
    _register_channel(subparsers)
    _register_runtime(subparsers)
    _register_browser(subparsers)

    weixin = subparsers.add_parser("weixin-login", help="微信扫码登录（旧入口）")
    weixin.set_defaults(handler=_weixin_login)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

def _register_mcp(subparsers) -> None:
    parser = subparsers.add_parser("mcp", help="MCP Server 管理")
    cmds = parser.add_subparsers(dest="mcp_cmd")

    serve = cmds.add_parser("serve", help="启动 MCP Server（stdio）")
    serve.set_defaults(handler=_mcp_serve)

    proxy = cmds.add_parser("interaction-proxy", help="启动交互代理 MCP Server")
    proxy.set_defaults(handler=_mcp_interaction_proxy)

    servers = cmds.add_parser("servers", help="MCP Server 配置管理")
    servers_cmds = servers.add_subparsers(dest="mcp_servers_cmd")
    servers_cmds.add_parser("list").set_defaults(handler=_mcp_servers_list)

    add = servers_cmds.add_parser("add")
    _add_mcp_server_args(add, require_name=True)
    add.set_defaults(handler=_mcp_servers_add)

    update = servers_cmds.add_parser("update")
    _add_mcp_server_args(update, require_name=True)
    update.set_defaults(handler=_mcp_servers_update)

    delete = servers_cmds.add_parser("delete")
    delete.add_argument("--name", required=True)
    delete.set_defaults(handler=_mcp_servers_delete)

    reload_one = servers_cmds.add_parser("reload")
    reload_one.add_argument("--name", required=True)
    reload_one.set_defaults(handler=_mcp_servers_reload)

    cua = cmds.add_parser("cua-driver", help="CUA Driver 安装管理")
    cua_cmds = cua.add_subparsers(dest="mcp_cua_cmd")
    cua_cmds.add_parser("status").set_defaults(handler=_mcp_cua_status)
    setup = cua_cmds.add_parser("setup")
    setup.add_argument("--force-reinstall", action="store_true")
    setup.add_argument("--no-daemon", action="store_true", help="安装后不启动 daemon")
    setup.add_argument("--wait", type=float, default=0.0, help="等待任务完成的秒数，0=立即返回")
    setup.set_defaults(handler=_mcp_cua_setup)
    setup_status = cua_cmds.add_parser("task-status")
    setup_status.add_argument("--task-id", required=True)
    setup_status.set_defaults(handler=_mcp_cua_task_status)
    cancel = cua_cmds.add_parser("cancel")
    cancel.add_argument("--task-id", required=True)
    cancel.set_defaults(handler=_mcp_cua_cancel)


def _add_mcp_server_args(parser, *, require_name: bool = False) -> None:
    parser.add_argument("--name", required=require_name)
    parser.add_argument("--command", help="stdio 命令")
    parser.add_argument("--args", default="", help="逗号分隔的命令参数")
    parser.add_argument("--url", help="http/sse 地址")
    parser.add_argument("--transport", choices=("http", "sse"), help="url 模式的传输协议")
    parser.add_argument("--env", default="", help="env JSON 对象")
    parser.add_argument("--json", dest="json_payload", help="完整配置 JSON（与显式参数合并）")


def _mcp_payload(args: Any) -> dict[str, Any]:
    payload = parse_json(getattr(args, "json_payload", None), name="MCP 配置")
    for field in ("command", "url", "transport"):
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    if args.args:
        payload["args"] = [item.strip() for item in args.args.split(",") if item.strip()]
    if args.env:
        env = parse_json(args.env, name="env")
        if not isinstance(env, dict):
            raise CliError("env 必须是 JSON 对象")
        payload["env"] = env
    return payload


def _mcp_serve(args: Any, ctx: CliContext) -> CliResult:
    from crew.gateway.mcp_server import serve

    serve()
    return CliResult(data=None)


def _mcp_interaction_proxy(args: Any, ctx: CliContext) -> CliResult:
    from crew.gateway.mcp_server import serve_interaction_proxy

    serve_interaction_proxy()
    return CliResult(data=None)


def _mcp_servers_view(app: Any) -> list[dict[str, Any]]:
    mgr = getattr(app, "mcp_manager", None)
    if mgr is None:
        return []
    return [
        {
            "name": row["name"],
            "transport": row["transport"],
            "connected": row["connected"],
            "error": row["error"],
            "tools": row["tools"],
            "config": _redact_config(row["config"]),
        }
        for row in mgr.status()
    ]


async def _ensure_mcp_started(app: Any) -> None:
    mgr = getattr(app, "mcp_manager", None)
    if mgr is not None and getattr(mgr, "_registry", None) is None:
        await mgr.start(app.registry)


def _mcp_servers_list(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data={"ok": True, "servers": _mcp_servers_view(ctx.app)}, text="")


async def _mcp_servers_add(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    payload = _mcp_payload(args)
    name = str(payload.get("name") or args.name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise CliError("name 非法（仅字母数字下划线连字符）")
    if name in (app.config.mcp_servers or {}):
        raise CliError(f"MCP server 已存在: {name}", exit_code=409)
    cfg, err = _validate_server_payload(payload)
    if err is not None:
        raise CliError(err)
    app.config.set_mcp_server(name, cfg)
    try:
        app.config.persist_mcp_servers()
    except Exception as exc:
        app.config.remove_mcp_server(name)
        raise CliError(f"持久化失败: {exc}") from exc
    await _ensure_mcp_started(app)
    if app.mcp_manager is not None:
        app.mcp_manager.register_pending(name, cfg)
        asyncio.create_task(app.mcp_manager.add_server(name, cfg))
    return CliResult(data={"ok": True, "servers": _mcp_servers_view(app)}, text=f"已添加 MCP server {name}")


async def _mcp_servers_update(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    name = args.name
    if name not in (app.config.mcp_servers or {}):
        raise CliError(f"MCP server 不存在: {name}", exit_code=404)
    payload = _mcp_payload(args)
    payload.pop("name", None)
    cfg, err = _validate_server_payload(payload)
    if err is not None:
        raise CliError(err)
    app.config.set_mcp_server(name, cfg)
    try:
        app.config.persist_mcp_servers()
    except Exception as exc:
        raise CliError(f"持久化失败: {exc}") from exc
    await _ensure_mcp_started(app)
    if app.mcp_manager is not None:
        asyncio.create_task(app.mcp_manager.reload_one(name, cfg))
    return CliResult(data={"ok": True, "servers": _mcp_servers_view(app)}, text=f"已更新 MCP server {name}")


async def _mcp_servers_delete(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    name = args.name
    if name not in (app.config.mcp_servers or {}):
        raise CliError(f"MCP server 不存在: {name}", exit_code=404)
    app.config.remove_mcp_server(name)
    try:
        app.config.persist_mcp_servers()
    except Exception as exc:
        raise CliError(f"持久化失败，磁盘配置可能未更新: {exc}") from exc
    await _ensure_mcp_started(app)
    if app.mcp_manager is not None:
        await app.mcp_manager.remove_server(name)
    return CliResult(data={"ok": True, "servers": _mcp_servers_view(app)}, text=f"已删除 MCP server {name}")


async def _mcp_servers_reload(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    name = args.name
    if name not in (app.config.mcp_servers or {}):
        raise CliError(f"MCP server 不存在: {name}", exit_code=404)
    await _ensure_mcp_started(app)
    if app.mcp_manager is None:
        raise CliError("MCP 管理器未初始化")
    await app.mcp_manager.reload_one(name)
    return CliResult(data={"ok": True, "servers": _mcp_servers_view(app)}, text=f"已重载 MCP server {name}")


def _cua_service():
    from crew.tools.cua_setup import CuaDriverSetupService, task_to_dict

    return CuaDriverSetupService(), task_to_dict


async def _mcp_cua_status(args: Any, ctx: CliContext) -> CliResult:
    service, _ = _cua_service()
    result = await service.status(ctx.app.registry)
    return CliResult(data=result)


async def _mcp_cua_setup(args: Any, ctx: CliContext) -> CliResult:
    service, _task_to_dict = _cua_service()
    task = service.start_setup(
        crew=ctx.app,
        force_reinstall=args.force_reinstall,
        start_daemon=not args.no_daemon,
    )
    if args.wait > 0:
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            current = service.get_task(task.task_id)
            if current is None or current.status in {"success", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.25)
    return CliResult(data={"ok": True, "task_id": task.task_id, "status": task.status})


async def _mcp_cua_task_status(args: Any, ctx: CliContext) -> CliResult:
    service, task_to_dict = _cua_service()
    task = service.get_task(args.task_id)
    if task is None:
        raise CliError("任务不存在", exit_code=404)
    return CliResult(data={"ok": True, **task_to_dict(task)})


async def _mcp_cua_cancel(args: Any, ctx: CliContext) -> CliResult:
    service, _ = _cua_service()
    ok = await service.cancel_task(args.task_id)
    if not ok:
        raise CliError("任务不存在或已结束")
    return CliResult(data={"ok": True, "task_id": args.task_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------

def _register_plugin(subparsers) -> None:
    parser = subparsers.add_parser("plugin", help="插件偏好管理")
    cmds = parser.add_subparsers(dest="plugin_cmd")
    cmds.add_parser("list").set_defaults(handler=_plugin_list)
    enable = cmds.add_parser("enable")
    enable.add_argument("--key", required=True)
    enable.set_defaults(handler=_plugin_set_enabled)
    disable = cmds.add_parser("disable")
    disable.add_argument("--key", required=True)
    disable.set_defaults(handler=_plugin_set_disabled)


def _plugin_list(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    user_type = str(app.config.access_control.user_type or "internal").strip().lower()
    items = _plugin_states(app, ctx.owner, user_type)
    text = "\n".join(
        f"{item['key']}  {item['label']}  effective={item['effective_enabled']}  error={item['error'] or ''}"
        for item in items
    )
    return CliResult(data=items, text=text or "(无插件)")


async def _plugin_set_enabled(args: Any, ctx: CliContext) -> CliResult:
    return await _plugin_set_enabled_value(args, ctx, True)


async def _plugin_set_disabled(args: Any, ctx: CliContext) -> CliResult:
    return await _plugin_set_enabled_value(args, ctx, False)


async def _plugin_set_enabled_value(args: Any, ctx: CliContext, enabled: bool) -> CliResult:
    app = ctx.app
    key = args.key.strip()
    mgr = getattr(app, "plugins", None)
    prefs = getattr(app, "plugin_prefs", None)
    if mgr is None or prefs is None:
        raise CliError("插件系统未就绪")
    loaded = mgr.get_plugin(key)
    if loaded is None:
        raise CliError("插件不存在", exit_code=404)
    user_type = str(app.config.access_control.user_type or "internal").strip().lower()
    ac = app.config.access_control.resolve_for(user_type)
    if not loaded.enabled:
        raise CliError("插件已被系统级禁用", exit_code=403)
    if not plugin_role_allowed(ac, loaded.manifest.key or loaded.manifest.name):
        raise CliError("当前账号角色未授权该插件", exit_code=403)
    current_user_enabled = prefs.get_enabled(ctx.owner, key)
    current_effective = plugin_effective_enabled(
        system_enabled=True,
        role_allowed=True,
        user_enabled=current_user_enabled,
        user_type=user_type,
    )
    manager = getattr(app, "browser_manager", None)
    if key == "browser":
        if manager is None:
            raise CliError("浏览器运行时未就绪", exit_code=503)
        if enabled:
            runtime_state, runtime_error = browser_runtime_status(app, ctx.owner, key)
            if not runtime_state["ready"]:
                raise CliError(runtime_error, exit_code=409)
            if not current_effective:
                manager.renew_capability(ctx.owner)
            prefs.set_enabled(ctx.owner, key, True)
            _drop_owner_agent_cache(app, ctx.owner)
        else:
            prefs.set_enabled(ctx.owner, key, False)
            _drop_owner_agent_cache(app, ctx.owner)
            if current_effective:
                await manager.revoke_owner(ctx.owner)
    else:
        prefs.set_enabled(ctx.owner, key, enabled)
        _drop_owner_agent_cache(app, ctx.owner)
    states = {item["key"]: item for item in _plugin_states(app, ctx.owner, user_type)}
    return CliResult(
        data={"ok": True, "plugin": states.get(key)},
        text=f"已{'启用' if enabled else '禁用'}插件 {key}",
    )


# ---------------------------------------------------------------------------
# 渠道
# ---------------------------------------------------------------------------

def _register_channel(subparsers) -> None:
    parser = subparsers.add_parser("channel", help="外部渠道管理")
    cmds = parser.add_subparsers(dest="channel_cmd")
    cmds.add_parser("list").set_defaults(handler=_channel_list)

    config = cmds.add_parser("config", help="渠道配置")
    config_cmds = config.add_subparsers(dest="channel_config_cmd")
    show = config_cmds.add_parser("show")
    show.add_argument("--platform", required=True)
    show.set_defaults(handler=_channel_config_show)
    save = config_cmds.add_parser("save")
    save.add_argument("--platform", required=True)
    save.add_argument("--json", dest="json_payload", required=True, help="配置 JSON：{enabled, config, secrets, environment}")
    save.set_defaults(handler=_channel_config_save)

    connect = cmds.add_parser("connect")
    connect.add_argument("--platform", required=True)
    connect.set_defaults(handler=_channel_connect)
    disconnect = cmds.add_parser("disconnect")
    disconnect.add_argument("--platform", required=True)
    disconnect.set_defaults(handler=_channel_disconnect)
    reconnect = cmds.add_parser("reconnect")
    reconnect.add_argument("--platform", required=True)
    reconnect.set_defaults(handler=_channel_reconnect)
    delete = cmds.add_parser("account-delete")
    delete.add_argument("--platform", required=True)
    delete.set_defaults(handler=_channel_account_delete)

    qr = cmds.add_parser("qr-login", help="微信扫码登录")
    qr_cmds = qr.add_subparsers(dest="channel_qr_cmd")
    qr_cmds.add_parser("start").set_defaults(handler=_channel_qr_start)
    status = qr_cmds.add_parser("status")
    status.add_argument("--qr-id", required=True)
    status.set_defaults(handler=_channel_qr_status)


def _channel_manager(app: Any) -> ChannelManager:
    manager = getattr(app, "channel_manager", None)
    if manager is None:
        manager = ChannelManager()
        app.channel_manager = manager
    return manager


def _channel_row(app: Any, name: str, manager: ChannelManager, owner: str) -> dict[str, Any]:
    raw = resolved_channel_raw(app.config, name, owner)
    entry = platform_registry.get(name)
    cfg = entry.build_config(raw, include_env=not bool(owner))
    state = next((item for item in manager.status() if item.get("name") == name), {})
    row = {
        "name": name,
        "label": entry.label,
        "enabled": bool(cfg.enabled),
        "running": bool(state.get("running", False)),
        "error": str(state.get("error") or ""),
        "operation": str(state.get("operation") or ""),
        "reason": str(state.get("reason") or ""),
        "has_account": _has_channel_account(name, raw, owner_account_id=owner),
    }
    return _enrich_platform_row(name, row, manager, secret_values=())


def _channel_list(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    manager = _channel_manager(app)
    items = [_channel_row(app, entry.name, manager, ctx.owner) for entry in platform_registry.all_entries()]
    text = "\n".join(
        f"{item['name']}  enabled={item['enabled']} running={item['running']} "
        f"account={item['has_account']} error={item['error'] or ''}"
        for item in items
    )
    return CliResult(data=items, text=text or "(无平台)")


def _channel_config_show(args: Any, ctx: CliContext) -> CliResult:
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    data = _public_channel_config(
        name,
        resolved_channel_raw(ctx.app.config, name, ctx.owner),
        crew_config=ctx.app.config,
        owner_account_id=ctx.owner,
    )
    return CliResult(data={"ok": True, **data})


def _channel_config_save(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    manager = _channel_manager(app)
    payload = parse_json(args.json_payload, name="渠道配置")
    enabled, config, secrets, environment = _normalize_config_payload(payload)
    try:
        config = _apply_environment_preset(name, config, environment)
        _validate_secret_fields(name, secrets)
        if not _validate_platform_config_ready(name, app, config, secrets, owner_account_id=ctx.owner):
            raise CliError("平台配置校验失败，请检查必填字段")
        safe_config = _sanitize_for_yaml(name, enabled, config)
        app.config.persist_channel_config(name, safe_config, owner_account_id=ctx.owner)
        _write_env_fields(name, config, secrets, owner_account_id=ctx.owner)
    except (ValueError, RuntimeError) as exc:
        raise CliError(str(exc)) from exc
    channel = manager.channels.get(name)
    apply = getattr(channel, "apply_config", None)
    if channel is not None and callable(apply):
        try:
            entry = platform_registry.get(name)
            apply(entry.build_config(resolved_channel_raw(app.config, name, ctx.owner), include_env=not bool(ctx.owner)))
        except Exception as exc:  # noqa: BLE001 - 热应用失败不影响保存结果
            log.warning("platform %s 配置热应用失败: %s", name, exc)
    return CliResult(
        data={
            "ok": True,
            "saved": True,
            **_public_channel_config(name, resolved_channel_raw(app.config, name, ctx.owner), crew_config=app.config, owner_account_id=ctx.owner),
            "status": _channel_row(app, name, manager, ctx.owner),
        },
        text=f"已保存渠道 {name} 配置",
    )


async def _restart_channel(app: Any, manager: ChannelManager, name: str, owner: str) -> tuple[bool, dict[str, Any]]:
    if manager.is_busy(name):
        raise CliError("渠道正在重连，请稍后再操作", exit_code=409)
    entry = platform_registry.get(name)
    raw = resolved_channel_raw(app.config, name, owner)
    cfg = entry.build_config(raw, include_env=not bool(owner))
    if not cfg.enabled:
        await manager.stop_one(name)
        return True, _channel_row(app, name, manager, owner)
    try:
        channel = platform_registry.create_channel(name, cfg)
    except Exception as exc:  # noqa: BLE001
        manager.record_error(name, str(exc))
        return False, _channel_row(app, name, manager, owner)
    if hasattr(channel, "bind_app"):
        channel.bind_app(app)
    handler = getattr(app, "channel_handler", None) or app.dispatch
    state = await manager.restart_one(name, channel, handler, owner_account_id=owner)
    if not state.running:
        return False, _channel_row(app, name, manager, owner)
    live_ok, live_err = await _wait_for_live_connected(manager, name)
    if not live_ok:
        await manager.stop_one(name)
        manager.record_error(name, live_err)
        return False, _channel_row(app, name, manager, owner)
    return True, _channel_row(app, name, manager, owner)


async def _channel_connect(args: Any, ctx: CliContext) -> CliResult:
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    app = ctx.app
    manager = _channel_manager(app)
    app.config.persist_channel_config(name, {"enabled": True}, owner_account_id=ctx.owner)
    try:
        ok, status = await _restart_channel(app, manager, name, ctx.owner)
    except RuntimeError as exc:
        raise CliError(str(exc), exit_code=409) from exc
    if ok and getattr(app, "channel_bindings", None) is not None:
        bind_channel_platform_for_owner(app, name, ctx.owner)
    return CliResult(
        data={"ok": ok, "status": status, "error": status.get("error") or ""},
        text=f"渠道 {name} 已连接" if ok else f"渠道 {name} 连接失败",
    )


async def _channel_disconnect(args: Any, ctx: CliContext) -> CliResult:
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    app = ctx.app
    manager = _channel_manager(app)
    app.config.persist_channel_config(name, {"enabled": False}, owner_account_id=ctx.owner)
    state = await manager.stop_one(name)
    return CliResult(
        data={"ok": not bool(state.error), "status": _channel_row(app, name, manager, ctx.owner), "error": str(state.error or "")},
        text=f"渠道 {name} 已断开",
    )


async def _channel_reconnect(args: Any, ctx: CliContext) -> CliResult:
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    app = ctx.app
    manager = _channel_manager(app)
    try:
        ok, status = await _restart_channel(app, manager, name, ctx.owner)
    except RuntimeError as exc:
        raise CliError(str(exc), exit_code=409) from exc
    return CliResult(
        data={"ok": ok, "status": status, "error": status.get("error") or ""},
        text=f"渠道 {name} 已重连" if ok else f"渠道 {name} 重连失败",
    )


async def _channel_account_delete(args: Any, ctx: CliContext) -> CliResult:
    name = args.platform.strip().lower()
    if not platform_registry.is_registered(name):
        raise CliError(f"未知平台: {name}", exit_code=404)
    app = ctx.app
    manager = _channel_manager(app)
    state = await manager.stop_one(name)
    raw = resolved_channel_raw(app.config, name, ctx.owner)
    remove_keys = _account_remove_keys(name, raw)
    app.config.persist_channel_config(
        name,
        {"enabled": False, "_remove_keys": remove_keys},
        owner_account_id=ctx.owner,
    )
    _remove_secret_envs(name, owner_account_id=ctx.owner)
    bindings = getattr(app, "channel_bindings", None)
    if bindings is not None:
        bindings.unbind(name)
    return CliResult(
        data={
            "ok": not bool(state.error),
            "deleted": True,
            **_public_channel_config(name, resolved_channel_raw(app.config, name, ctx.owner), crew_config=app.config, owner_account_id=ctx.owner),
            "status": _channel_row(app, name, manager, ctx.owner),
            "error": str(state.error or ""),
        },
        text=f"已删除渠道 {name} 账号",
    )


def _weixin_ilink():
    try:
        from plugins.platforms.weixin import ilink
    except ImportError as exc:
        raise CliError("weixin 插件未安装，请先安装 .[weixin] extra", exit_code=404) from exc
    return ilink


async def _channel_qr_start(args: Any, ctx: CliContext) -> CliResult:
    ilink = _weixin_ilink()
    fetched = await ilink.fetch_qr_code()
    if fetched is None:
        raise CliError("获取二维码失败，请稍后重试")
    qrcode_value, qr_scan_data, qrcode_url = fetched
    svg = ilink.render_qr_svg(qr_scan_data)
    qr_image = ""
    if svg:
        qr_image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return CliResult(
        data={"ok": True, "qr_id": qrcode_value, "qr_image": qr_image, "qrcode_url": qrcode_url},
        text=f"扫码登录已开始 qr_id={qrcode_value}",
    )


async def _channel_qr_status(args: Any, ctx: CliContext) -> CliResult:
    ilink = _weixin_ilink()
    status_resp = await ilink.poll_qr_status(args.qr_id, base_url=ilink.ILINK_BASE_URL)
    if status_resp is None:
        return CliResult(data={"ok": True, "status": "pending"}, text="pending")
    return CliResult(data={"ok": True, **status_resp})


def _weixin_login(args: Any, ctx: CliContext) -> CliResult:
    from crew.cli.weixin_login import main as weixin_login_main

    weixin_login_main()
    return CliResult(data=None)


# ---------------------------------------------------------------------------
# 外部 Runtime / Agent / Team
# ---------------------------------------------------------------------------

def _register_runtime(subparsers) -> None:
    parser = subparsers.add_parser("runtime", help="外部 Runtime/Agent/Team 管理")
    cmds = parser.add_subparsers(dest="runtime_cmd")

    runtime = cmds.add_parser("runtimes", help="外部运行时")
    runtime_cmds = runtime.add_subparsers(dest="runtime_runtimes_cmd")
    runtime_cmds.add_parser("scan").set_defaults(handler=_runtime_scan)
    runtime_cmds.add_parser("list").set_defaults(handler=_runtime_list)
    register = runtime_cmds.add_parser("register")
    register.add_argument("--id", required=True)
    register.add_argument("--type", required=True)
    register.add_argument("--provider", required=True)
    register.add_argument("--executable-path", default="")
    register.add_argument("--metadata", default="", help="metadata JSON")
    register.set_defaults(handler=_runtime_register)
    delete = runtime_cmds.add_parser("delete")
    delete.add_argument("--id", dest="runtime_id", required=True)
    delete.set_defaults(handler=_runtime_delete)

    agent = cmds.add_parser("agents", help="外部智能体")
    agent_cmds = agent.add_subparsers(dest="runtime_agents_cmd")
    agent_cmds.add_parser("list").set_defaults(handler=_runtime_agents_list)
    create = agent_cmds.add_parser("create")
    create.add_argument("--name", default="")
    create.add_argument("--runtime-id", required=True)
    create.add_argument("--model", required=True)
    create.add_argument("--system-prompt", default="")
    create.add_argument("--custom-args", default="", help="JSON 数组")
    create.add_argument("--custom-env", default="", help="JSON 对象")
    create.set_defaults(handler=_runtime_agents_create)
    delete_agent = agent_cmds.add_parser("delete")
    delete_agent.add_argument("--id", dest="agent_id", required=True)
    delete_agent.set_defaults(handler=_runtime_agents_delete)

    team = cmds.add_parser("teams", help="外部团队")
    team_cmds = team.add_subparsers(dest="runtime_teams_cmd")
    team_cmds.add_parser("list").set_defaults(handler=_runtime_teams_list)
    create_team = team_cmds.add_parser("create")
    create_team.add_argument("--name", required=True)
    create_team.add_argument("--leader-agent-id", required=True)
    create_team.add_argument("--description", default="")
    create_team.add_argument("--members", required=True, help="成员 JSON 数组：[{agent_id, role_key}]")
    create_team.set_defaults(handler=_runtime_teams_create)
    delete_team = team_cmds.add_parser("delete")
    delete_team.add_argument("--id", dest="team_id", required=True)
    delete_team.set_defaults(handler=_runtime_teams_delete)


def _external_store(app: Any):
    require_external_agents_enabled(app)
    if app.external_agents is None:
        raise CliError("外部智能体存储未初始化")
    return app.external_agents


async def _runtime_scan(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.external.detector import discover_local_runtimes
    from crew.security.launch import ProcessLaunch, current_process_launch
    from crew.security.models import PermissionProfile, PermissionProfileKind

    store = _external_store(ctx.app)
    token = current_process_launch.set(
        ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))
    )
    try:
        detected = await discover_local_runtimes()
    finally:
        current_process_launch.reset(token)
    synced = store.sync_runtimes(detected)
    items = [_runtime_availability(runtime) for runtime in synced]
    return CliResult(data=items, text=f"发现 {len(items)} 个运行时")


def _runtime_list(args: Any, ctx: CliContext) -> CliResult:
    items = [_runtime_availability(runtime) for runtime in _external_store(ctx.app).list_runtimes()]
    text = "\n".join(
        f"{item.get('id')}  {item.get('provider')}  {item.get('availability_status')}  {item.get('executable_path', '')}"
        for item in items
    )
    return CliResult(data=items, text=text or "(无运行时)")


def _runtime_register(args: Any, ctx: CliContext) -> CliResult:
    store = _external_store(ctx.app)
    payload = {
        "id": args.id,
        "type": args.type,
        "provider": args.provider,
        "executable_path": args.executable_path,
    }
    if args.metadata:
        metadata = parse_json(args.metadata, name="metadata")
        if not isinstance(metadata, dict):
            raise CliError("metadata 必须是 JSON 对象")
        payload["metadata"] = metadata
    runtime = _runtime_availability(store.upsert_runtime(payload))
    return CliResult(data=runtime, text=f"已注册运行时 {runtime.get('id')}")


def _runtime_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _external_store(ctx.app).delete_runtime(args.runtime_id)
    except KeyError as exc:
        raise CliError("运行时不存在", exit_code=404) from exc
    except ValueError as exc:
        raise CliError(str(exc), exit_code=409) from exc
    return CliResult(data={"ok": True}, text="运行时已删除")


def _runtime_agents_list(args: Any, ctx: CliContext) -> CliResult:
    from crew.gateway.routers.runtimes import _managed_temporary_agent_ids

    store = _external_store(ctx.app)
    hidden = _managed_temporary_agent_ids(store, owner_account_id=ctx.owner)
    items = _external_agent_payloads(
        store,
        [
            agent
            for agent in store.list_agents(owner_account_id=ctx.owner)
            if str(agent.get("id") or "") not in hidden
        ],
    )
    text = "\n".join(f"{item.get('id')}  {item.get('name', '')}  {item.get('runtime_id', '')}" for item in items)
    return CliResult(data=items, text=text or "(无外部智能体)")


def _runtime_agents_create(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.external.runtime_profile import normalize_runtime_models

    store = _external_store(ctx.app)
    try:
        runtime = store.get_runtime(args.runtime_id)
    except KeyError as exc:
        raise CliError(f"运行时不存在: {args.runtime_id}", exit_code=404) from exc
    if _runtime_availability(runtime).get("availability_status") != "ready":
        raise CliError("运行时尚未就绪，请重新探测", exit_code=409)
    models = normalize_runtime_models((runtime.get("metadata") or {}).get("models"))
    if args.model not in {item.id for item in models}:
        raise CliError("所选模型不属于当前运行时")
    custom_args = parse_json(args.custom_args, name="custom-args")
    custom_env = parse_json(args.custom_env, name="custom-env")
    if not isinstance(custom_args, list):
        raise CliError("custom-args 必须是 JSON 数组")
    if not isinstance(custom_env, dict):
        raise CliError("custom-env 必须是 JSON 对象")
    agent = store.create_agent(
        owner_account_id=ctx.owner,
        name=args.name or "未命名智能体",
        runtime_id=args.runtime_id,
        model=args.model,
        system_prompt=args.system_prompt,
        custom_args=custom_args,
        custom_env=custom_env,
    )
    return CliResult(data=_external_agent_payloads(store, [agent])[0], text=f"已创建智能体 {agent.get('id')}")


def _runtime_agents_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _external_store(ctx.app).delete_agent(args.agent_id, owner_account_id=ctx.owner)
    except KeyError as exc:
        raise CliError("智能体不存在", exit_code=404) from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data={"ok": True}, text="智能体已删除")


def _runtime_teams_list(args: Any, ctx: CliContext) -> CliResult:
    store = _external_store(ctx.app)
    items = _external_team_payloads(
        store,
        store.list_teams(owner_account_id=ctx.owner),
        owner_account_id=ctx.owner,
    )
    text = "\n".join(f"{item.get('id')}  {item.get('name', '')}  leader={item.get('leader_agent_id', '')}" for item in items)
    return CliResult(data=items, text=text or "(无外部团队)")


def _runtime_teams_create(args: Any, ctx: CliContext) -> CliResult:
    store = _external_store(ctx.app)
    members = parse_json(args.members, name="members")
    if not isinstance(members, list) or not all(isinstance(item, dict) for item in members):
        raise CliError("members 必须是 JSON 数组")
    team = store.create_team(
        owner_account_id=ctx.owner,
        name=args.name,
        leader_agent_id=args.leader_agent_id,
        members=members,
        description=args.description,
    )
    return CliResult(
        data=_external_team_payloads(store, [team], owner_account_id=ctx.owner)[0],
        text=f"已创建团队 {team.get('id')}",
    )


def _runtime_teams_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _external_store(ctx.app).delete_team(args.team_id, owner_account_id=ctx.owner)
    except KeyError as exc:
        raise CliError("团队不存在", exit_code=404) from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data={"ok": True}, text="团队已删除")


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------

def _register_browser(subparsers) -> None:
    parser = subparsers.add_parser("browser", help="浏览器控制台")
    cmds = parser.add_subparsers(dest="browser_cmd")
    cmds.add_parser("doctor").set_defaults(handler=_browser_doctor)
    state = cmds.add_parser("state")
    state.add_argument("--session-id", required=True)
    state.set_defaults(handler=_browser_state)
    control = cmds.add_parser("control")
    control.add_argument("--session-id", required=True)
    control.add_argument("--action", required=True)
    control.add_argument("--value", default="")
    control.set_defaults(handler=_browser_control)
    artifact = cmds.add_parser("artifact")
    artifact.add_argument("--session-id", required=True)
    artifact.add_argument("--path", required=True)
    artifact.add_argument("--new-tab", action="store_true")
    artifact.set_defaults(handler=_browser_artifact)
    cmds.add_parser("clear").set_defaults(handler=_browser_clear)


def _browser_manager(app: Any):
    manager = getattr(app, "browser_manager", None)
    if manager is None:
        raise CliError("Browser Use 未启用", exit_code=503)
    return manager


def _browser_safe_error(exc: BaseException) -> str:
    from crew.tools.redact import redact_sensitive_display_text

    return redact_sensitive_display_text(str(exc))[:500]


def _browser_doctor(args: Any, ctx: CliContext) -> CliResult:
    from crew.browser.electron_driver import runtime_doctor

    runtime_key = f"crew_{hashlib.sha256(ctx.owner.encode('utf-8')).hexdigest()[:12]}"
    return CliResult(data=runtime_doctor(ctx.app.config.browser, runtime_key))


def _browser_allowed(app: Any, owner: str, session_id: str) -> bool:
    from crew.gateway.routers.browser import _browser_access_allowed

    getter = getattr(app.session_store, "get_agent_config", None)
    try:
        raw = getter(session_id, owner_account_id=owner) if callable(getter) else {}
    except (TypeError, ValueError):
        raw = {}
    user_type = str((raw or {}).get("user_type") or app.config.access_control.user_type)
    checker = getattr(app, "_browser_plugin_effective", None)
    if callable(checker) and not checker(owner, user_type):
        return False
    policy = app.config.access_control.resolve_for(user_type)
    return _browser_access_allowed(getattr(app, "registry", None), policy)


def _browser_state(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    if not app.session_store.session_belongs_to(args.session_id, ctx.owner):
        raise CliError("会话不存在", exit_code=404)
    if not _browser_allowed(app, ctx.owner, args.session_id):
        raise CliError("该会话未开放 Browser Use", exit_code=403)
    return CliResult(data={"ok": True, "state": _browser_manager(app).state(ctx.owner, args.session_id)})


async def _browser_control(args: Any, ctx: CliContext) -> CliResult:
    from crew.browser.driver import BrowserDriverError

    app = ctx.app
    if not app.session_store.session_belongs_to(args.session_id, ctx.owner):
        raise CliError("会话不存在", exit_code=404)
    if not _browser_allowed(app, ctx.owner, args.session_id):
        raise CliError("该会话未开放 Browser Use", exit_code=403)
    mgr = _browser_manager(app)
    action = args.action
    value = args.value
    try:
        if action in {"open", "new_tab"}:
            state = await mgr.open_for_user(ctx.owner, args.session_id, url=value, new_tab=action == "new_tab")
            return CliResult(data={"ok": True, "state": state})
        if action == "record_discard":
            removed = await asyncio.to_thread(mgr.discard_recording, ctx.owner, args.session_id, value)
            return CliResult(data={"ok": True, "discarded": removed})
        if action in {
            "record_start",
            "record_pause",
            "record_resume",
            "record_stop",
            "record_note",
            "record_status",
        }:
            recording = await mgr.user_recording(
                ctx.owner,
                args.session_id,
                action.removeprefix("record_"),
                value,
            )
            return CliResult(data={"ok": True, "recording": recording})
        if action in {"takeover", "return", "pause", "stop"}:
            result = await mgr.user_control(ctx.owner, args.session_id, action)
            return CliResult(data={"ok": True, "result": result, "state": mgr.state(ctx.owner, args.session_id)})
        state = await mgr.human_command(ctx.owner, args.session_id, action, value)
        return CliResult(data={"ok": True, "state": state})
    except (BrowserDriverError, ValueError) as exc:
        raise CliError(_browser_safe_error(exc), exit_code=409) from exc


async def _browser_artifact(args: Any, ctx: CliContext) -> CliResult:
    from crew.browser.driver import BrowserDriverError
    from crew.state.home import task_workspace_path

    app = ctx.app
    if not app.session_store.session_belongs_to(args.session_id, ctx.owner):
        raise CliError("会话不存在", exit_code=404)
    if not _browser_allowed(app, ctx.owner, args.session_id):
        raise CliError("该会话未开放 Browser Use", exit_code=403)
    mgr = _browser_manager(app)
    raw_path = args.path
    if not raw_path or len(raw_path) > 4096:
        raise CliError("HTML 文件路径无效")
    workspace_id = app.session_store.get_workspace_id(args.session_id, owner_account_id=ctx.owner)
    if not workspace_id:
        raise CliError("会话工作区不存在", exit_code=404)
    try:
        workspace = app.workspace_store.get(workspace_id, owner_account_id=ctx.owner)
        configured_root = str(workspace.get("root_path") or "").strip()
        root = (
            Path(configured_root).expanduser().resolve(strict=True)
            if configured_root
            else task_workspace_path(workspace_id, owner_account_id=ctx.owner, create=False).resolve(strict=True)
        )
        if raw_path.lower().startswith("file:"):
            parsed = urlsplit(raw_path)
            raw_path = url2pathname(unquote(parsed.path))
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=True)
        state = await mgr.open_for_user(
            ctx.owner,
            args.session_id,
            artifact_path=str(candidate),
            artifact_root=str(root),
            new_tab=args.new_tab,
        )
        return CliResult(data={"ok": True, "state": state})
    except (BrowserDriverError, KeyError, OSError, ValueError) as exc:
        raise CliError(_browser_safe_error(exc), exit_code=409) from exc


async def _browser_clear(args: Any, ctx: CliContext) -> CliResult:
    from crew.browser.driver import BrowserDriverError

    try:
        result = await _browser_manager(ctx.app).clear_owner_data(ctx.owner)
    except (BrowserDriverError, OSError) as exc:
        raise CliError(_browser_safe_error(exc), exit_code=409) from exc
    return CliResult(data=result)


__all__ = ["register"]
