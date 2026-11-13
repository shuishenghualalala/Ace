"""可选渠道环境预设：桌面端下拉选择，后端展开为连接配置。"""

from __future__ import annotations

from typing import Any

# 当前内置渠道不需要环境预设；第三方插件可在此注册公开、非敏感的预设。
_PLATFORM_PRESETS: dict[str, dict[str, dict[str, str]]] = {}


def list_environment_presets(platform: str) -> list[dict[str, str]]:
    """返回供前端下拉使用的 `{id, label}` 列表。"""
    presets = _PLATFORM_PRESETS.get(platform.strip().lower(), {})
    return [{"id": key, "label": value["label"]} for key, value in presets.items()]


def resolve_environment_preset(platform: str, environment: str) -> dict[str, str]:
    """将 environment id 展开为写入 channels 的配置字段（不含 label / environment）。"""
    name = platform.strip().lower()
    env_id = str(environment or "").strip()
    preset = _PLATFORM_PRESETS.get(name, {}).get(env_id)
    if preset is None:
        raise ValueError(f"未知环境: {env_id}")
    return {key: value for key, value in preset.items() if key != "label"}


def detect_environment(platform: str, raw: dict[str, Any]) -> str:
    """从已保存配置推断 environment；优先读显式 `environment` 字段。"""
    name = platform.strip().lower()
    presets = _PLATFORM_PRESETS.get(name, {})
    explicit = str(raw.get("environment") or "").strip()
    if explicit in presets:
        return explicit
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    merged = {**extra, **{k: v for k, v in raw.items() if k != "extra"}}
    for env_id, preset in presets.items():
        matched = True
        for key, expected in preset.items():
            if key == "label":
                continue
            val = merged.get(key)
            if not val or str(val).strip() != str(expected).strip():
                matched = False
                break
        if matched:
            return env_id
    return ""


def has_environment_presets(platform: str) -> bool:
    return platform.strip().lower() in _PLATFORM_PRESETS
