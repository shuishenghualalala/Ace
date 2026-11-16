"""渠道（平台）状态、配置、生命周期与飞书 webhook 入口。"""

from __future__ import annotations

import base64
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.channels.channel_sessions import bind_channel_platform_for_owner
from crew.channels.platform_registry import platform_registry
from crew.channels.router_helpers import (
    _WEIXIN_QR_STATES,
    _account_remove_keys,
    _apply_environment_preset,
    _enrich_platform_row,
    _has_channel_account,
    _normalize_config_payload,
    _platform_secret_values,
    _prune_weixin_qr_states,
    _public_channel_config,
    _remove_secret_envs,
    _resolved_channel_raw,
    _safe_error,
    _sanitize_for_yaml,
    _validate_platform_config_ready,
    _validate_secret_fields,
    _write_env_fields,
)
from crew.channels.router_helpers import (
    _owner_can_see_runtime as _rh_owner_can_see_runtime,
)
from crew.channels.router_helpers import (
    _platform_configs as _rh_platform_configs,
)
from crew.channels.router_helpers import (
    _restart_channel as _rh_restart_channel,
)
from crew.channels.router_helpers import (
    _runtime_state as _rh_runtime_state,
)
from crew.channels.router_helpers import (
    _single_platform_status as _rh_single_platform_status,
)
from crew.gateway.auth import account_from_request
from crew.state.logging import get_logger

log = get_logger("gateway.channels")


def create_channels_router(crew, dispatcher, channel_manager=None) -> APIRouter:
    """创建渠道路由。

    ``channel_manager`` 为可选参数：显式传入时作为当前 router 的 override；
    否则每个请求从 ``crew.channel_manager`` 动态解析，确保 Channels Feature
    生命周期更新后不会持有过期引用。
    """
    router = APIRouter()

    def _get_manager() -> Any:
        if channel_manager is not None:
            return channel_manager
        manager = crew.channel_manager
        if manager is None:
            raise RuntimeError("Channels manager is not available")
        return manager

    def _platform_raw(name: str, owner_account_id: str) -> dict[str, Any]:
        return _resolved_channel_raw(crew.config, name, owner_account_id)

    def _platform_configs(owner_account_id: str) -> dict[str, Any]:
        return _rh_platform_configs(crew.config, owner_account_id)

    def _owner_can_see_runtime(name: str, owner_account_id: str) -> bool:
        return _rh_owner_can_see_runtime(_get_manager(), name, owner_account_id)

    def _runtime_state(name: str, owner_account_id: str) -> dict[str, Any]:
        return _rh_runtime_state(_get_manager(), name, owner_account_id)

    def _single_platform_status(name: str, owner_account_id: str) -> dict[str, Any]:
        return _rh_single_platform_status(crew.config, _get_manager(), name, owner_account_id)

    async def _restart_platform(name: str, owner_account_id: str) -> tuple[bool, dict[str, Any]]:
        return await _rh_restart_channel(crew, name, owner_account_id, channel_manager=_get_manager())

    def _busy_response(name: str, owner_account_id: str) -> JSONResponse | None:
        if _get_manager().is_busy(name, owner_account_id):
            return JSONResponse({"ok": False, "error": "渠道正在重连，请稍后再操作"}, status_code=409)
        return None

    def _hot_apply_platform_config(name: str, owner_account_id: str) -> None:
        """保存配置后热应用：运行中的渠道若实现 apply_config，就地刷新非连接类设置（不断连）。"""
        channel = _get_manager().get(name, owner_account_id)
        apply = getattr(channel, "apply_config", None)
        if channel is None or not callable(apply):
            return
        try:
            entry = platform_registry.get(name)
            cfg = entry.build_config(
                _platform_raw(name, owner_account_id),
                include_env=not bool(owner_account_id),
            )
            apply(cfg)
        except Exception as exc:  # noqa: BLE001 — 热应用失败不影响保存结果，重连后自然生效
            log.warning("platform %s 配置热应用失败: %s", name, exc)

    @router.get("/api/platforms")
    async def platforms(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        manager = _get_manager()
        configs = _platform_configs(owner)
        rows = []
        for item in platform_registry.list(configs):
            cfg = configs.get(item["name"])
            state = _runtime_state(item["name"], owner) if _owner_can_see_runtime(item["name"], owner) else {}
            row = {
                **item,
                "enabled": bool(cfg.enabled) if cfg is not None else False,
                "running": bool(state.get("running", False)),
                "error": _safe_error(state.get("error", "")),
                "operation": str(state.get("operation", "")),
                "reason": str(state.get("reason", "")),
                "has_account": _has_channel_account(item["name"], _platform_raw(item["name"], owner), owner_account_id=owner),
            }
            if _owner_can_see_runtime(item["name"], owner):
                row = _enrich_platform_row(
                    item["name"],
                    row,
                    manager,
                    owner_account_id=owner,
                    secret_values=_platform_secret_values(crew.config, item["name"], owner),
                )
            else:
                row["live_connected"] = False
            rows.append(row)
        return JSONResponse(rows)

    @router.get("/api/platforms/{name}/config")
    async def get_platform_config(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        return JSONResponse({
            "ok": True,
            **_public_channel_config(
                platform,
                _platform_raw(platform, owner),
                crew_config=crew.config,
                owner_account_id=owner,
            ),
        })

    @router.put("/api/platforms/{name}/config")
    async def save_platform_config(request: Request, name: str, payload: dict) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        busy = _busy_response(platform, owner)
        if busy is not None:
            return busy
        async with _get_manager().lock_for(platform, owner):
            enabled, config, secrets, environment = _normalize_config_payload(payload)
            try:
                config = _apply_environment_preset(platform, config, environment)
                _validate_secret_fields(platform, secrets)
                if not _validate_platform_config_ready(platform, crew, config, secrets, owner_account_id=owner):
                    return JSONResponse(
                        {"ok": False, "error": f"platform config validation failed: {platform}"},
                        status_code=400,
                    )
                safe_config = _sanitize_for_yaml(platform, enabled, config)
                crew.config.persist_channel_config(platform, safe_config, owner_account_id=owner)
                _write_env_fields(
                    platform,
                    config,
                    secrets,
                    owner_account_id=owner,
                )
            except (ValueError, RuntimeError) as exc:
                return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)
        _hot_apply_platform_config(platform, owner)
        return JSONResponse({
            "ok": True,
            "saved": True,
            **_public_channel_config(
                platform,
                _platform_raw(platform, owner),
                crew_config=crew.config,
                owner_account_id=owner,
            ),
            "status": _single_platform_status(platform, owner),
        })

    @router.post("/api/platforms/{name}/connect")
    async def connect_platform(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        busy = _busy_response(platform, owner)
        if busy is not None:
            return busy
        crew.config.persist_channel_config(platform, {"enabled": True}, owner_account_id=owner)
        try:
            ok, status = await _restart_platform(platform, owner)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        if ok and getattr(crew, "channel_bindings", None) is not None:
            bind_channel_platform_for_owner(crew, platform, owner)
        error = _safe_error(status.get("error", ""))
        return JSONResponse(
            {"ok": ok, "status": {**status, "error": error}, "error": error},
            status_code=200 if ok else 500,
        )

    @router.post("/api/platforms/{name}/disconnect")
    async def disconnect_platform(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        busy = _busy_response(platform, owner)
        if busy is not None:
            return busy
        crew.config.persist_channel_config(platform, {"enabled": False}, owner_account_id=owner)
        state = await _get_manager().stop_one(platform, owner)
        delivery_router = crew.delivery_router
        if delivery_router is not None:
            delivery_router.unregister(platform, owner_account_id=owner)
        error = _safe_error(state.error)
        return JSONResponse({
            "ok": not bool(state.error),
            "status": _single_platform_status(platform, owner),
            "error": error,
        }, status_code=200 if not state.error else 500)

    @router.delete("/api/platforms/{name}/account")
    async def delete_platform_account(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        busy = _busy_response(platform, owner)
        if busy is not None:
            return busy
        async with _get_manager().lock_for(platform, owner):
            state = await _get_manager().stop_one_locked(platform, owner, operation="deleting_account")
            error = _safe_error(state.error)
            delivery_router = crew.delivery_router
            if delivery_router is not None:
                delivery_router.unregister(platform, owner_account_id=owner)
            raw = _platform_raw(platform, owner)
            remove_keys = _account_remove_keys(platform, raw)
            crew.config.persist_channel_config(platform, {"enabled": False, "_remove_keys": remove_keys}, owner_account_id=owner)
            _remove_secret_envs(platform, owner_account_id=owner)
            if getattr(crew, "channel_bindings", None) is not None:
                crew.channel_bindings.unbind(platform, owner)
        return JSONResponse({
            "ok": not bool(state.error),
            "deleted": True,
            **_public_channel_config(
                platform,
                _platform_raw(platform, owner),
                crew_config=crew.config,
                owner_account_id=owner,
            ),
            "status": _single_platform_status(platform, owner),
            "error": error,
        }, status_code=200 if not state.error else 500)

    @router.post("/api/platforms/{name}/reconnect")
    async def reconnect_platform(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        owner = account_from_request(request).owner_account_id
        if not platform_registry.is_registered(platform):
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"}, status_code=404)
        try:
            ok, status = await _restart_platform(platform, owner)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        error = _safe_error(status.get("error", ""))
        return JSONResponse(
            {"ok": ok, "status": {**status, "error": error}, "error": error},
            status_code=200 if ok else 500,
        )

    @router.post("/api/feishu/events")
    async def feishu_events(request: Request) -> JSONResponse:
        """飞书 webhook 入口：登录、渠道和 token 前置门禁通过后才读取并入队正文。"""
        manager = _get_manager()
        candidates = [
            (owner, channel)
            for name, owner, channel in manager.iter_channels()
            if name == "feishu"
        ]
        if not candidates:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Gateway 未登录，飞书渠道已断开",
                    "code": "LOGIN_REQUIRED",
                },
                status_code=503,
            )
        allow_missing_token = bool(crew.config.gateway_dev_mode)
        if not allow_missing_token and not any(
            str(getattr(channel.settings, "verification_token", "") or "").strip()
            for _owner, channel in candidates
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "生产模式必须配置飞书 verification_token",
                    "code": "FEISHU_WEBHOOK_TOKEN_REQUIRED",
                },
                status_code=503,
            )
        try:
            payload = await request.json()
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "invalid webhook json", "code": "INVALID_EVENT"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "error": "invalid webhook event", "code": "INVALID_EVENT"},
                status_code=400,
            )
        selected = None
        for owner, feishu in candidates:
            ingress_available = getattr(feishu, "ingress_available", None)
            if callable(ingress_available) and not ingress_available(owner):
                continue
            verify = getattr(feishu, "verify_webhook", None)
            if callable(verify) and verify(payload, allow_missing_token=allow_missing_token):
                selected = (owner, feishu)
                break
        if selected is None:
            log.warning("飞书 webhook 校验失败，拒绝请求")
            return JSONResponse({"ok": False, "error": "invalid verification token"}, status_code=403)
        owner, feishu = selected
        challenge = feishu.challenge_response(payload)
        if challenge is not None:
            return JSONResponse(challenge)
        result = feishu.enqueue_webhook_event(payload)
        if result == "accepted":
            return JSONResponse({"ok": True, "accepted": True})
        if result == "queue_full":
            return JSONResponse(
                {"ok": False, "error": "feishu ingress queue is full", "code": "INGRESS_BUSY"},
                status_code=503,
            )
        if result == "invalid_event":
            return JSONResponse(
                {"ok": False, "error": "invalid webhook event", "code": "INVALID_EVENT"},
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": False,
                "error": "飞书渠道未连接或正在退出登录",
                "code": "CHANNEL_DISCONNECTED",
            },
            status_code=503,
        )

    # -- 微信扫码登录（桌面端内置扫码）-------------------------------------- #
    @router.post("/api/platforms/{name}/qr-login/start")
    async def weixin_qr_login_start(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        if platform != "weixin":
            return JSONResponse({"ok": False, "error": "该平台不支持扫码登录"}, status_code=400)
        try:
            from plugins.platforms.weixin import ilink
        except ImportError:
            return JSONResponse({"ok": False, "error": "weixin 插件未安装"}, status_code=404)
        fetched = await ilink.fetch_qr_code()
        if fetched is None:
            return JSONResponse({"ok": False, "error": "获取二维码失败，请稍后重试"}, status_code=500)
        qrcode_value, qr_scan_data, qrcode_url = fetched
        svg = ilink.render_qr_svg(qr_scan_data)
        qr_image = ""
        if svg:
            qr_image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
        now = time.time()
        _WEIXIN_QR_STATES[qrcode_value] = {"base_url": ilink.ILINK_BASE_URL, "updated_at": now}
        _prune_weixin_qr_states(now)
        return JSONResponse({
            "ok": True,
            "qr_id": qrcode_value,
            "qr_image": qr_image,
            "qrcode_url": qrcode_url,
        })

    @router.post("/api/platforms/{name}/qr-login/status")
    async def weixin_qr_login_status(request: Request, name: str) -> JSONResponse:
        platform = name.strip().lower()
        if platform != "weixin":
            return JSONResponse({"ok": False, "error": "该平台不支持扫码登录"}, status_code=400)
        try:
            from plugins.platforms.weixin import ilink
            from plugins.platforms.weixin.config import WeixinSettings
        except ImportError:
            return JSONResponse({"ok": False, "error": "weixin 插件未安装"}, status_code=404)
        try:
            payload = await request.json()
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid qr_id"}, status_code=400)
        qr_id = str(payload.get("qr_id") or "").strip()
        if not qr_id:
            return JSONResponse({"ok": False, "error": "missing qr_id"}, status_code=400)
        state = _WEIXIN_QR_STATES.get(qr_id)
        base_url = state["base_url"] if state else ilink.ILINK_BASE_URL
        status_resp = await ilink.poll_qr_status(qr_id, base_url=base_url)
        if status_resp is None:
            return JSONResponse({"ok": True, "status": "pending"})
        status = str(status_resp.get("status") or "wait")
        if status == "scaned_but_redirect":
            redirect_host = str(status_resp.get("redirect_host") or "")
            if redirect_host:
                _WEIXIN_QR_STATES.setdefault(qr_id, {})["base_url"] = f"https://{redirect_host}"
            return JSONResponse({"ok": True, "status": "scaned"})
        if status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            token = str(status_resp.get("bot_token") or "")
            base_url = str(status_resp.get("baseurl") or ilink.ILINK_BASE_URL)
            user_id = str(status_resp.get("ilink_user_id") or "")
            if not account_id or not token:
                return JSONResponse({"ok": True, "status": "error", "error": "扫码确认但凭证不完整"})
            settings = WeixinSettings.from_extra({})
            ilink.save_account(
                settings.accounts_dir(),
                account_id=account_id,
                token=token,
                base_url=base_url,
                user_id=user_id,
            )
            _WEIXIN_QR_STATES.pop(qr_id, None)
            return JSONResponse({
                "ok": True, "status": "confirmed", "account_id": account_id, "token": token,
            })
        _WEIXIN_QR_STATES[qr_id] = {**_WEIXIN_QR_STATES.get(qr_id, {}), "updated_at": time.time()}
        return JSONResponse({"ok": True, "status": status})

    return router
