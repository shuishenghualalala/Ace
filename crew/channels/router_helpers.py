"""渠道路由与 CLI 共享的纯函数/配置工具。

本模块从 crew.gateway.routers.channels 下沉，供 router 与 CLI 共同消费，
避免 CLI 反向依赖 gateway router 的私有实现。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from crew.channels.channel_config import (
    PLATFORM_ACCOUNT_FIELDS as _PLATFORM_ACCOUNT_FIELDS,
)
from crew.channels.channel_config import (
    PLATFORM_ENV_FIELDS as _PLATFORM_ENV_FIELDS,
)
from crew.channels.channel_config import (
    PLATFORM_SECRET_ENV as _PLATFORM_SECRET_ENV,
)
from crew.channels.channel_config import (
    channel_raw as _resolved_channel_raw,
)
from crew.channels.channel_config import (
    owner_env_map as _owner_env_map,
)
from crew.channels.channel_presets import (
    detect_environment,
    has_environment_presets,
    list_environment_presets,
    resolve_environment_preset,
)
from crew.channels.platform_registry import platform_registry
from crew.state.config import remove_env_key, resolve_writable_env_path, write_env_key
from crew.state.logging import get_logger

log = get_logger("channels.router_helpers")

_SECRET_MARKERS = ("secret", "token", "api_key", "apikey", "password")

# 微信扫码登录的进程内状态：qr_id → {base_url, updated_at}，仅用于跟踪 redirect_host。
_WEIXIN_QR_TTL_S = 600.0
_WEIXIN_QR_STATES: dict[str, dict[str, Any]] = {}


def _prune_weixin_qr_states(now: float) -> None:
    for key in [k for k, v in _WEIXIN_QR_STATES.items()
                if now - float(v.get("updated_at", 0)) > _WEIXIN_QR_TTL_S]:
        _WEIXIN_QR_STATES.pop(key, None)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _redact(value: Any, secret_values: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(k): ("***" if _is_secret_key(str(k)) and v not in (None, "") else _redact(v, secret_values))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, str):
        return _safe_error(value, secret_values)
    return value


def _redact_secret_text(text: str) -> str:
    redacted = re.sub(
        r"([?&](?:secret_key|api_key|token|password|app_secret|secret|key)=)[^&\s]+",
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"//([^:/@\s]+):([^/@\s]+)@", r"//\1:***@", redacted)
    return redacted


def _safe_error(error: Any, secret_values: tuple[str, ...] = ()) -> str:
    text = str(error or "")
    for env_name in os.environ:
        if _is_secret_key(env_name):
            secret = os.getenv(env_name, "")
            if secret:
                text = text.replace(secret, "***")
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "***")
    return _redact_secret_text(text)


def _public_channel_config(
    name: str,
    raw: dict[str, Any],
    *,
    crew_config,
    owner_account_id: str,
) -> dict[str, Any]:
    secret_envs = set(_PLATFORM_SECRET_ENV.get(name, {}).values())
    env_map = _owner_env_map(crew_config, owner_account_id)
    public = {
        key: value
        for key, value in raw.items()
        if key not in {"extra"} and not _is_secret_key(str(key))
    }
    extra = raw.get("extra")
    if isinstance(extra, dict):
        public.update({key: value for key, value in extra.items() if not _is_secret_key(str(key))})
    payload: dict[str, Any] = {
        "name": name,
        "config": _redact(public),
        "secret_fields": sorted(secret_envs),
        "has_secret": {env_name: bool(env_map.get(env_name, "")) for env_name in sorted(secret_envs)},
        "has_account": _has_channel_account(name, raw, owner_account_id=owner_account_id),
    }
    if has_environment_presets(name):
        payload["presets"] = list_environment_presets(name)
        payload["environment"] = detect_environment(name, raw)
    return payload


def _has_channel_account(name: str, raw: dict[str, Any], *, owner_account_id: str) -> bool:
    try:
        entry = platform_registry.get(name)
    except KeyError:
        return False
    try:
        return entry.configured(entry.build_config(raw, include_env=not bool(owner_account_id)))
    except Exception:  # noqa: BLE001 — 插件配置校验失败仅表示账号未完整配置
        return False


def _normalize_config_payload(payload: dict[str, Any]) -> tuple[bool, dict[str, Any], dict[str, str], str]:
    enabled = bool(payload.get("enabled", True))
    raw_config = payload.get("config")
    config = raw_config if isinstance(raw_config, dict) else {
        key: value
        for key, value in payload.items()
        if key not in {"enabled", "secrets", "config", "reconnect", "environment"}
    }
    secrets = payload.get("secrets")
    secret_payload = secrets if isinstance(secrets, dict) else {}
    environment = str(payload.get("environment") or config.pop("environment", "") or "").strip()
    return enabled, dict(config), {str(k): str(v) for k, v in secret_payload.items() if v not in (None, "")}, environment


def _has_manual_preset_fields(platform: str, config: dict[str, Any]) -> bool:
    """未选 environment 时，若用户已提供 URL/domain 字段则视为手动配置。"""
    checks: dict[str, tuple[str, ...]] = {
        "feishu": ("domain",),
    }
    for key in checks.get(platform.strip().lower(), ()):
        if config.get(key) not in (None, ""):
            return True
    return False


def _apply_environment_preset(platform: str, config: dict[str, Any], environment: str) -> dict[str, Any]:
    """有内置预设的渠道优先用 environment 展开 URL；未选时允许保留已有 URL 字段。"""
    if not has_environment_presets(platform):
        return dict(config)
    merged = dict(config)
    env_id = str(environment or merged.pop("environment", "") or "").strip()
    if env_id:
        merged.update(resolve_environment_preset(platform, env_id))
        merged["environment"] = env_id
        return merged
    if _has_manual_preset_fields(platform, merged):
        return merged
    if platform.strip().lower() == "feishu":
        # 飞书 domain 有默认值，仅 appId/appSecret 即可 configured
        return merged
    raise ValueError("请选择环境")


def _write_env_fields(
    name: str,
    config: dict[str, Any],
    secrets: dict[str, str],
    *,
    owner_account_id: str,
) -> None:
    env_path = resolve_writable_env_path(owner_account_id)
    field_map = {**_PLATFORM_ENV_FIELDS.get(name, {}), **_PLATFORM_SECRET_ENV.get(name, {})}
    for field, env_name in field_map.items():
        if field in config and config[field] not in (None, ""):
            value = str(config[field])
            write_env_key(env_path, env_name, value, sync_process_env=not bool(owner_account_id))
    for field, value in secrets.items():
        env_name = _PLATFORM_SECRET_ENV.get(name, {}).get(field)
        if not env_name and field in set(_PLATFORM_SECRET_ENV.get(name, {}).values()):
            env_name = field
        if not env_name:
            raise ValueError(f"不支持的密钥字段: {field}")
        if not env_name.replace("_", "").isalnum():
            raise ValueError(f"非法环境变量名: {env_name!r}")
        write_env_key(env_path, env_name, value, sync_process_env=not bool(owner_account_id))


def _validate_secret_fields(name: str, secrets: dict[str, str]) -> None:
    allowed = set(_PLATFORM_SECRET_ENV.get(name, {})) | set(_PLATFORM_SECRET_ENV.get(name, {}).values())
    for field in secrets:
        if field not in allowed:
            raise ValueError(f"不支持的密钥字段: {field}")


def _sanitize_for_yaml(name: str, enabled: bool, config: dict[str, Any]) -> dict[str, Any]:
    secret_fields = set(_PLATFORM_SECRET_ENV.get(name, {}))
    remove_keys = {key for key in config if key in secret_fields or _is_secret_key(str(key))}
    data: dict[str, Any] = {"enabled": enabled, "_remove_keys": sorted(remove_keys)}
    for key, value in config.items():
        if key in secret_fields or _is_secret_key(str(key)):
            continue
        if value in (None, ""):
            continue
        data[key] = value
    return data


def _account_remove_keys(name: str, raw: dict[str, Any]) -> list[str]:
    fields = set(_PLATFORM_ACCOUNT_FIELDS.get(name, set())) | set(_PLATFORM_SECRET_ENV.get(name, {}))
    fields.update(key for key in raw if _is_secret_key(str(key)))
    extra = raw.get("extra")
    if isinstance(extra, dict):
        fields.update(key for key in extra if _is_secret_key(str(key)))
    return sorted(fields)


def _prospective_channel_config(
    name: str,
    crew,
    config: dict[str, Any],
    secrets: dict[str, str],
    *,
    owner_account_id: str,
) -> dict[str, Any]:
    """合并当前配置与待保存字段，供保存前校验（不落盘）。

    secrets 中可能直接传环境变量名，校验前需反向映射回配置字段名，
    否则 build_config 后 validate_config 读不到凭证。
    """
    merged = _resolved_channel_raw(crew.config, name, owner_account_id)
    merged.update(config)
    field_map = {**_PLATFORM_ENV_FIELDS.get(name, {}), **_PLATFORM_SECRET_ENV.get(name, {})}
    env_to_field: dict[str, str] = {}
    for field, env_name in field_map.items():
        env_to_field.setdefault(env_name, field)
    for field, value in secrets.items():
        # field 本身已是字段名则直接保留；是环境变量名则反向映射回字段名
        canonical = field if field in field_map else env_to_field.get(field)
        merged[canonical or field] = value
    return merged


def _validate_platform_config_ready(
    name: str,
    crew,
    config: dict[str, Any],
    secrets: dict[str, str],
    *,
    owner_account_id: str,
) -> bool:
    entry = platform_registry.get(name)
    prospective = _prospective_channel_config(name, crew, config, secrets, owner_account_id=owner_account_id)
    cfg = entry.build_config(prospective, include_env=not bool(owner_account_id))
    return entry.configured(cfg)


_CONNECT_WAIT_TIMEOUT_S = 15.0
_CONNECT_POLL_S = 0.25


def _read_channel_detail(
    channel_manager,
    name: str,
    owner_account_id: str,
) -> dict[str, Any]:
    """读取渠道可选的运行态快照（鸭子类型 status_detail）。"""
    channel = channel_manager.get(name, owner_account_id)
    if channel is None and owner_account_id in {"local", "dev:dev"}:
        channel = channel_manager.get(name, "")
    if channel is None:
        return {}
    detail_fn = getattr(channel, "status_detail", None)
    if not callable(detail_fn):
        return {}
    try:
        detail = detail_fn()
        return detail if isinstance(detail, dict) else {}
    except Exception:  # noqa: BLE001 — 状态展示不应因快照失败而中断
        return {}


def _channel_supports_live_probe(channel_manager, name: str, owner_account_id: str) -> bool:
    """是否应等待真实连通（有 status_detail 且含 connected / bot_identity_known）。"""
    detail = _read_channel_detail(channel_manager, name, owner_account_id)
    return "connected" in detail or "bot_identity_known" in detail


def _is_live_connected(name: str, detail: dict[str, Any]) -> bool:
    """真实连通：飞书看 bot 身份，其它插件可提供通用 connected 状态。"""
    platform = name.strip().lower()
    if platform == "feishu":
        return bool(detail.get("bot_identity_known"))
    return bool(detail.get("connected"))


def _platform_error_kind(name: str, error: Any) -> str:
    """把可安全展示的连接异常归类，供前端给出可执行提示。"""
    if name.strip().lower() != "weixin":
        return ""
    message = str(error or "").lower()
    network_markers = (
        "cannot connect to host",
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "connection refused",
        "network is unreachable",
        "connection timed out",
    )
    return "network" if any(marker in message for marker in network_markers) else ""


async def _wait_for_live_connected(
    channel_manager,
    name: str,
    owner_account_id: str,
) -> tuple[bool, str]:
    """连接后等待真实握手成功；无探针的渠道（测试桩）直接通过。"""
    if not _channel_supports_live_probe(channel_manager, name, owner_account_id):
        return True, ""
    deadline = asyncio.get_event_loop().time() + _CONNECT_WAIT_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        detail = _read_channel_detail(channel_manager, name, owner_account_id)
        if _is_live_connected(name, detail):
            return True, ""
        lifecycle = {
            (item["name"], item.get("owner_account_id", "")): item
            for item in channel_manager.status(owner_account_id)
        }
        state = lifecycle.get((name, owner_account_id), {})
        if state.get("error"):
            return False, str(state.get("error") or "连接失败")
        last_error = detail.get("last_error")
        if last_error and not detail.get("connected"):
            return False, str(last_error)
        await asyncio.sleep(_CONNECT_POLL_S)
    detail = _read_channel_detail(channel_manager, name, owner_account_id)
    if _is_live_connected(name, detail):
        return True, ""
    err = detail.get("last_error") or "连接超时，请检查凭据与环境是否正确"
    return False, str(err)


def _enrich_platform_row(
    name: str,
    row: dict[str, Any],
    channel_manager,
    *,
    owner_account_id: str,
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    """补充 live_connected：区分「进程已启动」与「远端已连通」。"""
    detail = _read_channel_detail(channel_manager, name, owner_account_id)
    if detail:
        row["detail"] = _redact(detail, secret_values)
    row["live_connected"] = _is_live_connected(name, detail) if detail else False
    error = row.get("error") or detail.get("last_error")
    error_kind = _platform_error_kind(name, error)
    if error_kind:
        row["error_kind"] = error_kind
    return row


def _remove_secret_envs(name: str, *, owner_account_id: str) -> None:
    env_path = resolve_writable_env_path(owner_account_id)
    for env_name in set(_PLATFORM_SECRET_ENV.get(name, {}).values()):
        remove_env_key(env_path, env_name, sync_process_env=not bool(owner_account_id))


def _platform_raw(crew_config, name: str, owner_account_id: str) -> dict[str, Any]:
    return _resolved_channel_raw(crew_config, name, owner_account_id)


def _platform_secret_values(
    crew_config,
    name: str,
    owner_account_id: str,
) -> tuple[str, ...]:
    raw = _platform_raw(crew_config, name, owner_account_id)
    values: list[str] = []
    secret_fields = set(_PLATFORM_SECRET_ENV.get(name, {}))
    for key, value in raw.items():
        if (key in secret_fields or _is_secret_key(str(key))) and value not in (None, ""):
            values.append(str(value))
    extra = raw.get("extra")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if _is_secret_key(str(key)) and value not in (None, ""):
                values.append(str(value))
    env_map = _owner_env_map(crew_config, owner_account_id)
    for env_name in set(_PLATFORM_SECRET_ENV.get(name, {}).values()):
        value = env_map.get(env_name)
        if value not in (None, ""):
            values.append(str(value))
    return tuple(values)


def _safe_platform_error(crew_config, name: str, owner_account_id: str, error: Any) -> str:
    return _safe_error(error, _platform_secret_values(crew_config, name, owner_account_id))


def _platform_configs(crew_config, owner_account_id: str) -> dict[str, Any]:
    return {
        entry.name: entry.build_config(
            _platform_raw(crew_config, entry.name, owner_account_id),
            include_env=not bool(owner_account_id),
        )
        for entry in platform_registry.all_entries()
    }


def _owner_can_see_runtime(channel_manager, name: str, owner_account_id: str) -> bool:
    owner = str(owner_account_id or "").strip()
    if channel_manager is None:
        return False
    if channel_manager.get(name, owner) is not None or (
        owner in {"local", "dev:dev"} and channel_manager.get(name, "") is not None
    ):
        return True
    return any(
        row.get("name") == name and row.get("owner_account_id", "") == owner
        for row in channel_manager.status(owner)
    )


def _runtime_state(channel_manager, name: str, owner_account_id: str) -> dict[str, Any]:
    if channel_manager is None:
        return {}
    owner = str(owner_account_id or "").strip()
    rows = channel_manager.status(owner)
    for row in rows:
        if row["name"] == name and row.get("owner_account_id", "") == owner:
            return row
    return {}


def _single_platform_status(
    crew_config,
    channel_manager,
    name: str,
    owner_account_id: str,
) -> dict[str, Any]:
    configs = _platform_configs(crew_config, owner_account_id)
    for item in platform_registry.list(configs):
        if item["name"] != name:
            continue
        cfg = configs.get(name)
        state = _runtime_state(channel_manager, name, owner_account_id) if _owner_can_see_runtime(channel_manager, name, owner_account_id) else {}
        row = {
            **item,
            "enabled": bool(cfg.enabled) if cfg is not None else False,
            "running": bool(state.get("running", False)),
            "error": _safe_platform_error(crew_config, name, owner_account_id, state.get("error", "")),
            "operation": str(state.get("operation", "")),
            "reason": str(state.get("reason", "")),
            "has_account": _has_channel_account(name, _platform_raw(crew_config, name, owner_account_id), owner_account_id=owner_account_id),
        }
        if _owner_can_see_runtime(channel_manager, name, owner_account_id):
            row = _enrich_platform_row(
                name,
                row,
                channel_manager,
                owner_account_id=owner_account_id,
                secret_values=_platform_secret_values(crew_config, name, owner_account_id),
            )
        else:
            row["live_connected"] = False
        return row
    raise KeyError(name)


async def _restart_channel(
    crew,
    name: str,
    owner_account_id: str,
    channel_manager=None,
) -> tuple[bool, dict[str, Any]]:
    """重启/停止单个平台通道，供 router 与 CLI 共享。

    channel_manager 显式传入时优先使用；否则从 crew.channel_manager 动态解析，
    确保 Feature 更新后不会持有过期引用。
    """
    manager = channel_manager if channel_manager is not None else crew.channel_manager
    if manager is None:
        raise RuntimeError("渠道管理器未就绪")
    if manager.is_busy(name, owner_account_id):
        raise RuntimeError("渠道正在重连，请稍后再操作")

    entry = platform_registry.get(name)
    cfg = entry.build_config(
        _platform_raw(crew.config, name, owner_account_id),
        include_env=not bool(owner_account_id),
    )
    if not cfg.enabled:
        await manager.stop_one(name, owner_account_id)
        delivery_router = crew.delivery_router
        if delivery_router is not None:
            delivery_router.unregister(name, owner_account_id=owner_account_id)
        return True, _single_platform_status(crew.config, manager, name, owner_account_id)
    try:
        channel = platform_registry.create_channel(name, cfg)
    except Exception as exc:  # noqa: BLE001 — 插件构造/校验失败必须隔离到目标平台
        manager.record_error(name, str(exc), owner_account_id)
        return False, _single_platform_status(crew.config, manager, name, owner_account_id)
    if hasattr(channel, "bind_app"):
        channel.bind_app(crew)
    handler = getattr(crew, "channel_handler", None) or crew.dispatch
    state = await manager.restart_one(
        name,
        channel,
        handler,
        owner_account_id=owner_account_id,
    )
    sender = getattr(channel, "send_to_target", None)
    delivery_router = crew.delivery_router
    if delivery_router is not None:
        if state.running and callable(sender):
            delivery_router.register(name, sender, owner_account_id=owner_account_id)
        elif not state.running:
            delivery_router.unregister(name, owner_account_id=owner_account_id)
    if not state.running:
        return False, _single_platform_status(crew.config, manager, name, owner_account_id)
    live_ok, live_err = await _wait_for_live_connected(manager, name, owner_account_id)
    if not live_ok:
        await manager.stop_one(name, owner_account_id)
        if delivery_router is not None:
            delivery_router.unregister(name, owner_account_id=owner_account_id)
        manager.record_error(name, live_err, owner_account_id)
        status = _single_platform_status(crew.config, manager, name, owner_account_id)
        return False, status
    return True, _single_platform_status(crew.config, manager, name, owner_account_id)
