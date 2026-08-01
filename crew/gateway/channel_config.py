"""Shared channel configuration resolution helpers."""

from __future__ import annotations

from typing import Any

PLATFORM_SECRET_ENV: dict[str, dict[str, str]] = {
    "feishu": {
        "appSecret": "FEISHU_APP_SECRET",
        "app_secret": "FEISHU_APP_SECRET",
    },
}

PLATFORM_ACCOUNT_FIELDS: dict[str, set[str]] = {
    "feishu": {"appId", "app_id", "appSecret", "app_secret"},
}

PLATFORM_ENV_FIELDS: dict[str, dict[str, str]] = {
    "feishu": {
        "appId": "FEISHU_APP_ID",
        "app_id": "FEISHU_APP_ID",
        "domain": "FEISHU_DOMAIN",
    },
}


def owner_env_map(config: Any, owner_account_id: str = "") -> dict[str, str]:
    """Read a normalized owner env overlay through Config's home resolver."""
    reader = getattr(config, "owner_env_map", None)
    if not callable(reader):
        return {}
    try:
        raw = reader(str(owner_account_id or "").strip())
    except Exception:  # noqa: BLE001 - env overlay read failures are handled by config consumers.
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v not in (None, "")}


def merge_owner_env_fields(
    config: Any,
    name: str,
    raw: dict[str, Any],
    owner_account_id: str = "",
) -> dict[str, Any]:
    """Overlay owner-scoped env values onto a channel's YAML config."""
    owner = str(owner_account_id or "").strip()
    if not owner:
        return dict(raw)
    env_map = owner_env_map(config, owner)
    if not env_map:
        return dict(raw)
    merged = dict(raw)
    field_map = {**PLATFORM_ENV_FIELDS.get(name, {}), **PLATFORM_SECRET_ENV.get(name, {})}
    for field, env_name in field_map.items():
        value = env_map.get(env_name)
        if value not in (None, ""):
            merged[field] = value
    return merged


def channel_raw(config: Any, name: str, owner_account_id: str = "") -> dict[str, Any]:
    """Return channel config with owner env overlay applied when owner is set."""
    raw_reader = getattr(config, "channel_config", None)
    raw = raw_reader(name, owner_account_id=owner_account_id) if callable(raw_reader) else {}
    return merge_owner_env_fields(config, name, raw if isinstance(raw, dict) else {}, owner_account_id)
