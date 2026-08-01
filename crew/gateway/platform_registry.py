"""Gateway platform registry.

Platform plugins register channel adapters here. Bundled and user-installed
plugins share the same metadata and config bridges while Crew keeps a compact
Channel abstraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from crew.core.interfaces import Channel
from crew.state.logging import get_logger

log = get_logger("gateway")


@dataclass
class HomeChannel:
    platform: str
    chat_id: str
    name: str = "Home"
    thread_id: str | None = None


@dataclass
class PlatformConfig:
    name: str
    enabled: bool = False
    token: str | None = None
    api_key: str | None = None
    home_channel: HomeChannel | None = None
    reply_to_mode: str = "first"
    gateway_restart_notification: bool = True
    env_enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformEntry:
    name: str
    label: str
    adapter_factory: Callable[[PlatformConfig], Channel]
    check_fn: Callable[[], bool] = lambda: True
    validate_config: Callable[[PlatformConfig], bool] | None = None
    is_connected: Callable[[PlatformConfig], bool] | None = None
    required_env: list[Any] = field(default_factory=list)
    optional_env: list[Any] = field(default_factory=list)
    install_hint: str = ""
    setup_fn: Callable[[], None] | None = None
    source: str = "plugin"
    plugin_name: str = ""
    description: str = ""
    allowed_users_env: str = ""
    allow_all_env: str = ""
    max_message_length: int = 0
    pii_safe: bool = False
    emoji: str = ""
    allow_update_command: bool = True
    platform_hint: str = ""
    env_enablement_fn: Callable[[], dict[str, Any] | None] | None = None
    apply_yaml_config_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None
    cron_deliver_env_var: str = ""
    standalone_sender_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def build_config(
        self,
        raw_config: dict[str, Any] | None = None,
        *,
        include_env: bool = True,
    ) -> PlatformConfig:
        raw_config = raw_config or {}
        extra: dict[str, Any] = {}
        raw_extra = raw_config.get("extra")
        if isinstance(raw_extra, dict):
            extra.update(raw_extra)

        for key, value in raw_config.items():
            if key not in {
                "enabled",
                "extra",
                "token",
                "api_key",
                "home_channel",
                "reply_to_mode",
                "gateway_restart_notification",
            }:
                extra[key] = value

        env_extra: dict[str, Any] = {}
        if include_env and self.env_enablement_fn is not None:
            try:
                resolved = self.env_enablement_fn()
                if isinstance(resolved, dict):
                    env_extra = dict(resolved)
            except Exception as exc:  # noqa: BLE001 — env_enablement_fn 为插件回调，失败面未知；探测失败回退空额外配置
                log.warning("平台 %s env_enablement_fn 失败: %s", self.name, exc)
                env_extra = {}

        env_configured = bool(env_extra)
        home_channel = _coerce_home_channel(self.name, raw_config.get("home_channel"))
        if env_configured:
            env_home = env_extra.pop("home_channel", None)
            if home_channel is None:
                home_channel = _coerce_home_channel(self.name, env_home)
            extra.update(env_extra)

        enabled_explicit = "enabled" in raw_config
        enabled = bool(raw_config.get("enabled", False))
        if not enabled_explicit and env_configured:
            enabled = True

        cfg = PlatformConfig(
            name=self.name,
            enabled=enabled,
            token=_as_optional_str(raw_config.get("token")),
            api_key=_as_optional_str(raw_config.get("api_key")),
            home_channel=home_channel,
            reply_to_mode=str(raw_config.get("reply_to_mode") or "first"),
            gateway_restart_notification=_as_bool(
                raw_config.get("gateway_restart_notification"),
                True,
            ),
            env_enabled=include_env,
            extra=extra,
        )

        # Env-only platforms are enabled when is_connected reports credentials.
        # are present. Probe with enabled=True because several adapters include
        # that flag in their configured check.
        if not enabled_explicit and not cfg.enabled and self.is_connected is not None:
            probe = PlatformConfig(
                name=self.name,
                enabled=True,
                token=cfg.token,
                api_key=cfg.api_key,
                home_channel=cfg.home_channel,
                reply_to_mode=cfg.reply_to_mode,
                gateway_restart_notification=cfg.gateway_restart_notification,
                env_enabled=include_env,
                extra=dict(cfg.extra),
            )
            try:
                cfg.enabled = bool(self.is_connected(probe))
            except Exception as exc:  # noqa: BLE001 — is_connected 为插件回调（含网络凭证探测），失败面未知；探测失败视为未连接
                log.debug("平台 %s is_connected 探测失败: %s", self.name, exc)
        return cfg

    def available(self) -> bool:
        try:
            return bool(self.check_fn())
        except Exception:
            return False

    def configured(self, config: PlatformConfig | None = None) -> bool:
        if not self.available():
            return False
        if self.validate_config is None:
            return True
        try:
            return bool(self.validate_config(config or PlatformConfig(name=self.name)))
        except Exception:
            return False

    def connected(self, config: PlatformConfig | None = None) -> bool:
        cfg = config or PlatformConfig(name=self.name)
        if not self.configured(cfg):
            return False
        if self.is_connected is not None:
            try:
                return bool(self.is_connected(cfg))
            except Exception:
                return False
        return self.configured(cfg)


class PlatformRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        if not entry.name:
            raise ValueError("platform entry missing name")
        self._entries[entry.name] = entry

    def clear_plugin_entries(self) -> None:
        for name, entry in list(self._entries.items()):
            if entry.source == "plugin" or entry.plugin_name:
                self._entries.pop(name, None)

    def unregister_plugin_entries(self, plugin_name: str) -> None:
        for name, entry in list(self._entries.items()):
            if entry.plugin_name == plugin_name:
                self._entries.pop(name, None)

    def get(self, name: str) -> PlatformEntry:
        if name not in self._entries:
            raise KeyError(f"unknown platform: {name}")
        return self._entries[name]

    def names(self) -> list[str]:
        return list(self._entries.keys())

    def all_entries(self) -> list[PlatformEntry]:
        return list(self._entries.values())

    def plugin_entries(self) -> list[PlatformEntry]:
        return [entry for entry in self._entries.values() if entry.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def list(self, configs: dict[str, PlatformConfig] | None = None) -> list[dict[str, Any]]:
        configs = configs or {}
        return [
            {
                "name": entry.name,
                "label": entry.label,
                "available": entry.available(),
                "configured": entry.configured(configs.get(entry.name)),
                "connected": entry.connected(configs.get(entry.name)),
                "required_env": entry.required_env,
                "optional_env": entry.optional_env,
                "install_hint": entry.install_hint,
                "source": entry.source,
                "plugin_name": entry.plugin_name,
                "description": entry.description,
                "max_message_length": entry.max_message_length,
                "emoji": entry.emoji,
                "platform_hint": entry.platform_hint,
            }
            for entry in self._entries.values()
        ]

    def create_channel(self, name: str, config: PlatformConfig | None = None) -> Channel:
        entry = self.get(name)
        cfg = config or PlatformConfig(name=name)
        if not entry.available():
            hint = f" ({entry.install_hint})" if entry.install_hint else ""
            raise RuntimeError(f"platform requirements not met: {name}{hint}")
        if entry.validate_config is not None and not entry.configured(cfg):
            raise RuntimeError(f"platform config validation failed: {name}")
        channel = entry.adapter_factory(cfg)
        if not hasattr(channel, "start"):
            raise RuntimeError(
                f"platform adapter must return a Crew Channel: {name}; "
                "Platform adapter requires a Channel wrapper"
            )
        return channel


platform_registry = PlatformRegistry()


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _coerce_home_channel(platform: str, raw: Any) -> HomeChannel | None:
    if not isinstance(raw, dict) or not raw.get("chat_id"):
        return None
    return HomeChannel(
        platform=str(raw.get("platform") or platform),
        chat_id=str(raw["chat_id"]),
        name=str(raw.get("name") or "Home"),
        thread_id=_as_optional_str(raw.get("thread_id")),
    )
