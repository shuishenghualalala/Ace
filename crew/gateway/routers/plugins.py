"""插件开关 API：用户级启用/禁用（per-owner 偏好），系统级状态查询。

语义：
- ``GET /api/plugins`` 在 misc.py，这里补充 ``GET /api/plugins/states`` 返回
  installed / system_allowed / role_allowed / user_enabled / effective_enabled。
- ``PUT /api/plugins/{plugin_key}/enabled`` 只写当前账号偏好（user_enabled 层）；
  system 与 role 层始终更高优先级，external 未获角色授权直接 403（fail-closed）。
- 禁用 browser：立即撤销（revoke_owner）+ 失效该 owner 的 Agent 缓存，下一轮
  schema/skill 不再出现 browser_use / browser-use。
- 启用 browser：递增 capability generation（旧 ref/截图/标签页句柄/审批 token
  全部不可复用）+ 失效 Agent 缓存。
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.gateway.helpers import safe_public_error
from crew.plugins.manager import PluginSecurityError
from crew.state.logging import get_logger
from crew.state.plugin_preferences import plugin_effective_enabled, plugin_role_allowed

log = get_logger("gateway.plugins")

_BROWSER_RUNTIME_ERROR = (
    "浏览器上次关闭状态无法确认，运行时已保持锁定；请重启应用后再启用"
)


def _user_type_for(crew, request: Request) -> str:
    """账号级 user_type：取全局默认（插件开关是账号级语义，与会话级 agent_config 无关）。"""
    return str(crew.config.access_control.user_type or "internal").strip().lower()


def browser_runtime_status(crew, owner: str, plugin_key: str) -> tuple[dict[str, bool], str]:
    """Return the runtime layer that sits below plugin preference state."""
    default = {
        "ready": True,
        "closing": False,
        "actions_blocked": False,
        "stop_unconfirmed": False,
    }
    if plugin_key != "browser":
        return default, ""
    manager = getattr(crew, "browser_manager", None)
    runtime_state = getattr(manager, "capability_runtime_state", None)
    if not callable(runtime_state):
        return {**default, "ready": False}, "浏览器运行时未就绪"
    state = dict(runtime_state(owner))
    if state.get("ready") is True:
        return state, ""
    return state, _BROWSER_RUNTIME_ERROR


def _drop_owner_agent_cache(crew, owner: str) -> None:
    agents = getattr(crew, "agents", None)
    drop_owner = getattr(agents, "drop_owner", None)
    if callable(drop_owner):
        # 下一轮工具 schema 与 skill index 必须按新偏好重建。禁用路径要在
        # await revoke_owner 之前完成，避免请求取消跳过缓存失效。
        drop_owner(owner)


def _drop_all_agent_cache(crew) -> None:
    """System plugin transitions change every owner's available tool surface."""
    agents = getattr(crew, "agents", None)
    clear = getattr(agents, "clear", None)
    if callable(clear):
        clear()


def _plugin_states(crew, owner: str, user_type: str) -> list[dict]:
    mgr = getattr(crew, "plugins", None)
    prefs = getattr(crew, "plugin_prefs", None)
    if mgr is None:
        return []
    ac = crew.config.access_control.resolve_for(user_type)
    states = []
    for loaded in mgr.loaded_plugins:
        manifest = loaded.manifest
        key = manifest.key or manifest.name
        system_allowed = bool(loaded.enabled)
        role_allowed = plugin_role_allowed(ac, key)
        user_enabled = None
        if prefs is not None and owner:
            try:
                user_enabled = prefs.get_enabled(owner, key)
            except Exception:  # noqa: BLE001 - 读取失败 fail-closed
                user_enabled = False
        policy_effective = plugin_effective_enabled(
            system_enabled=system_allowed,
            role_allowed=role_allowed,
            user_enabled=user_enabled,
            user_type=user_type,
        )
        runtime_state, runtime_error = browser_runtime_status(crew, owner, key)
        effective = policy_effective and runtime_state["ready"]
        errors = [safe_public_error(item, "插件运行时不可用") for item in (loaded.error, runtime_error) if item]
        states.append(
            {
                "name": manifest.name,
                "key": key,
                "label": manifest.label or manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "kind": manifest.kind,
                "enabled": loaded.enabled,
                "declarative_only": loaded.declarative_only,
                "execution_trusted": manifest.execution_trusted,
                "installed": True,
                "system_allowed": system_allowed,
                "role_allowed": role_allowed,
                "user_enabled": (
                    policy_effective if user_enabled is None else user_enabled
                ),
                "user_enabled_explicit": user_enabled is not None,
                "effective_enabled": effective,
                "runtime_ready": runtime_state["ready"],
                "runtime_state": runtime_state,
                "toggle_endpoint": (
                    str(manifest.ui_hints.get("toggle_endpoint") or "") or None
                ),
                "tools": loaded.tools_registered,
                "hooks": loaded.hooks_registered,
                "platforms": loaded.platforms_registered,
                "error": "；".join(errors) or None,
            }
        )
    return states


def create_plugins_router(crew) -> APIRouter:
    router = APIRouter()
    transition_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _admin_account(request: Request):
        account = account_from_request(request)
        try:
            require_admin(account, crew.config)
        except AuthenticationError as exc:
            return None, JSONResponse(
                {"ok": False, "error": safe_public_error(exc, "权限不足")},
                status_code=403,
            )
        return account, None

    @router.post("/api/plugins/install")
    async def install_remote_plugin(request: Request, payload: dict) -> JSONResponse:
        """Install a signed HTTPS bundle; only structured administrator input is accepted."""
        account, denied = _admin_account(request)
        if denied is not None:
            return denied
        allowed_fields = {"source_url", "sha256", "enabled"}
        unknown = sorted(str(key) for key in set(payload) - allowed_fields)
        if unknown:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "插件安装请求含不支持字段",
                    "fields": unknown,
                },
                status_code=400,
            )
        source_url = str(payload.get("source_url") or "").strip()
        digest = str(payload.get("sha256") or "").strip().lower()
        enabled = payload.get("enabled", False)
        try:
            parsed = urlsplit(source_url)
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            return JSONResponse(
                {"ok": False, "error": "source_url 必须是无凭据、无 fragment 的 HTTPS URL"},
                status_code=400,
            )
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return JSONResponse(
                {"ok": False, "error": "sha256 必须是 64 位十六进制摘要"},
                status_code=400,
            )
        if not isinstance(enabled, bool):
            return JSONResponse(
                {"ok": False, "error": "enabled 必须是布尔值"},
                status_code=400,
            )
        manager = getattr(crew, "plugins", None)
        if manager is None:
            return JSONResponse({"ok": False, "error": "插件系统未就绪"}, status_code=503)
        try:
            loaded = await asyncio.to_thread(
                manager.install_remote_bundle,
                source_url,
                expected_sha256=digest,
                actor_id=account.owner_account_id,
                enable=enabled,
            )
        except PluginSecurityError as exc:
            return JSONResponse(
                {"ok": False, "error": safe_public_error(exc, "插件安全校验失败"), "code": exc.code},
                status_code=400,
            )
        except Exception:
            log.exception("远程插件安装失败")
            return JSONResponse(
                {"ok": False, "error": "插件安装失败，请查看 Gateway 日志"},
                status_code=500,
            )
        if loaded.enabled:
            _drop_all_agent_cache(crew)
        manifest = loaded.manifest
        return JSONResponse(
            {
                "ok": True,
                "plugin": {
                    "name": manifest.name,
                    "key": manifest.key or manifest.name,
                    "version": manifest.version,
                    "enabled": loaded.enabled,
                    "declarative_only": loaded.declarative_only,
                    "execution_trusted": manifest.execution_trusted,
                    "error": loaded.error,
                    "source": getattr(manifest, "source", "installed"),
                    "signer_key_id": getattr(manifest, "signer_key_id", ""),
                    "tree_sha256": getattr(manifest, "tree_sha256", ""),
                },
            }
        )

    @router.put("/api/plugins/{plugin_key}/system-enabled")
    async def set_system_plugin_enabled(
        request: Request,
        plugin_key: str,
        payload: dict,
    ) -> JSONResponse:
        """Enable/disable a host plugin; this is an administrator mutation."""
        account, denied = _admin_account(request)
        if denied is not None:
            return denied
        if set(payload) != {"enabled"} or not isinstance(payload.get("enabled"), bool):
            return JSONResponse(
                {"ok": False, "error": "请求只能包含布尔字段 enabled"},
                status_code=400,
            )
        manager = getattr(crew, "plugins", None)
        if manager is None:
            return JSONResponse({"ok": False, "error": "插件系统未就绪"}, status_code=503)
        key = str(plugin_key or "").strip()
        try:
            if payload["enabled"]:
                ok = await asyncio.to_thread(
                    manager.enable_plugin,
                    key,
                    actor_id=account.owner_account_id,
                )
            else:
                ok = await asyncio.to_thread(
                    manager.unload_plugin,
                    key,
                    actor_id=account.owner_account_id,
                )
        except PluginSecurityError as exc:
            return JSONResponse(
                {"ok": False, "error": safe_public_error(exc, "插件安全校验失败"), "code": exc.code},
                status_code=400,
            )
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "插件不存在或状态未改变"},
                status_code=404,
            )
        _drop_all_agent_cache(crew)
        return JSONResponse({"ok": True, "key": key, "enabled": payload["enabled"]})

    @router.delete("/api/plugins/{plugin_key}")
    async def uninstall_plugin(request: Request, plugin_key: str) -> JSONResponse:
        """Uninstall one signed user plugin and clean all runtime registrations."""
        account, denied = _admin_account(request)
        if denied is not None:
            return denied
        manager = getattr(crew, "plugins", None)
        if manager is None:
            return JSONResponse({"ok": False, "error": "插件系统未就绪"}, status_code=503)
        key = str(plugin_key or "").strip()
        try:
            ok = await asyncio.to_thread(
                manager.uninstall_plugin,
                key,
                actor_id=account.owner_account_id,
            )
        except PluginSecurityError as exc:
            return JSONResponse(
                {"ok": False, "error": safe_public_error(exc, "插件安全校验失败"), "code": exc.code},
                status_code=400,
            )
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "插件不存在或不是可卸载的远程插件"},
                status_code=404,
            )
        _drop_all_agent_cache(crew)
        return JSONResponse({"ok": True, "key": key})

    @router.get("/api/plugins/states")
    async def plugin_states(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        user_type = _user_type_for(crew, request)
        return JSONResponse(_plugin_states(crew, owner, user_type))

    @router.put("/api/plugins/{plugin_key}/enabled")
    async def set_plugin_enabled(request: Request, plugin_key: str, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        key = str(plugin_key or "").strip()
        mgr = getattr(crew, "plugins", None)
        prefs = getattr(crew, "plugin_prefs", None)
        if mgr is None or prefs is None:
            return JSONResponse({"ok": False, "error": "插件系统未就绪"}, status_code=503)
        loaded = mgr.get_plugin(key)
        if loaded is None:
            return JSONResponse({"ok": False, "error": "插件不存在"}, status_code=404)
        if not isinstance(payload.get("enabled"), bool):
            return JSONResponse({"ok": False, "error": "enabled 必须是布尔值"}, status_code=400)
        enabled = payload["enabled"]

        user_type = _user_type_for(crew, request)
        ac = crew.config.access_control.resolve_for(user_type)
        if not loaded.enabled:
            return JSONResponse(
                {"ok": False, "error": "插件已被系统级禁用"}, status_code=403
            )
        if not plugin_role_allowed(ac, loaded.manifest.key or loaded.manifest.name):
            # external 等角色未获授权：fail-closed，不因用户偏好放开
            return JSONResponse(
                {"ok": False, "error": "当前账号角色未授权该插件"}, status_code=403
            )

        transition_lock = transition_locks.setdefault((owner, key), asyncio.Lock())
        async with transition_lock:
            current_user_enabled = prefs.get_enabled(owner, key)
            current_effective = plugin_effective_enabled(
                system_enabled=True,
                role_allowed=True,
                user_enabled=current_user_enabled,
                user_type=user_type,
            )
            manager = getattr(crew, "browser_manager", None)
            if key == "browser":
                if manager is None:
                    return JSONResponse(
                        {"ok": False, "error": "浏览器运行时未就绪"}, status_code=503
                    )
                if enabled:
                    runtime_state, runtime_error = browser_runtime_status(crew, owner, key)
                    if not runtime_state["ready"]:
                        # Keep the disabled preference intact. Bumping generation
                        # cannot recover an unconfirmed Host/Profile tombstone.
                        return JSONResponse(
                            {"ok": False, "error": runtime_error}, status_code=409
                        )
                    if not current_effective:
                        # 先切代次再开放偏好：并发伪造调用要么仍被偏好拒绝，要么只能拿到
                        # 新代次，绝不会夹在 user_enabled=True 与 generation bump 之间。
                        manager.renew_capability(owner)
                    prefs.set_enabled(owner, key, True)
                    _drop_owner_agent_cache(crew, owner)
                else:
                    prefs.set_enabled(owner, key, False)
                    _drop_owner_agent_cache(crew, owner)
                    if current_effective:
                        # 新调用先被偏好拒绝；关键 fence/close 即使请求取消也会完成。
                        await manager.revoke_owner(owner)
            else:
                prefs.set_enabled(owner, key, enabled)
                _drop_owner_agent_cache(crew, owner)
        log.info("插件开关 owner=%s plugin=%s enabled=%s", owner, key, enabled)

        states = {item["key"]: item for item in _plugin_states(crew, owner, user_type)}
        return JSONResponse({"ok": True, "plugin": states.get(key)})

    return router
