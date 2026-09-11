"""Channels product Feature: generation-owned ChannelManager / DeliveryRouter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crew.channels.channel_config import channel_raw as resolved_channel_raw
from crew.channels.channel_manager import ChannelManager
from crew.channels.channel_sessions import register_channel_session_tools
from crew.channels.delivery import DeliveryRouter
from crew.channels.platform_registry import PlatformEntry, PlatformRegistry, platform_registry
from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID, normalize_owner_account_id
from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureServiceDependencies,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    ServiceKey,
)
from crew.state.logging import get_logger

log = get_logger("gateway.channels")

CHANNELS_FEATURE_ID = "product.channels"
CHANNELS_SERVICE_KEY: ServiceKey["ChannelsService"] = ServiceKey("channels", version=1)


@dataclass(frozen=True, slots=True)
class ChannelsService:
    """Generation-owned channel runtime surface exposed to consumers."""

    channel_manager: ChannelManager
    delivery_router: DeliveryRouter
    platform_registry: PlatformRegistry


@dataclass(frozen=True, slots=True)
class ChannelsFeatureBundle:
    """Feature definition plus the pre-built candidate service."""

    definition: FeatureDefinition
    service: ChannelsService


def _register_platform_channel(
    crew: Any,
    channel_manager: ChannelManager,
    entry: PlatformEntry,
    *,
    owner_account_id: str,
    include_env: bool,
) -> bool:
    owner = normalize_owner_account_id(owner_account_id)
    try:
        raw = resolved_channel_raw(crew.config, entry.name, owner)
        pconfig = entry.build_config(raw, include_env=include_env)
    except Exception as exc:  # noqa: BLE001 - 单个平台配置异常不应阻断其它渠道启动
        log.warning("平台 %s 配置解析失败，跳过启动: %s", entry.name, exc)
        channel_manager.record_error(entry.name, "platform config invalid", owner)
        return False

    if not pconfig.enabled:
        return False
    if not entry.configured(pconfig):
        hint = entry.install_hint or "请补全平台凭证或设置 enabled: false"
        log.warning(
            "平台 %s 已由 owner=%s 启用但配置不完整，跳过启动（%s）",
            entry.name,
            owner,
            hint,
        )
        channel_manager.record_error(entry.name, "platform config incomplete", owner)
        return False
    try:
        channel = platform_registry.create_channel(entry.name, pconfig)
    except Exception as exc:  # noqa: BLE001 - 单个平台构造失败按渠道隔离
        log.exception("平台通道创建失败: %s", entry.name)
        channel_manager.record_error(entry.name, str(exc), owner)
        return False
    if hasattr(channel, "bind_app"):
        channel.bind_app(crew)
    channel_manager.register(channel, owner_account_id=owner)
    log.info("平台通道已归属 owner: %s owner=%s", entry.name, owner)
    return True


def _register_enabled_platform_channels(
    crew: Any,
    channel_manager: ChannelManager,
) -> None:
    entries = platform_registry.all_entries()
    bindings = getattr(crew, "channel_bindings", None)
    bound_owners: dict[str, list[str]] = {}
    if bindings is not None:
        for entry in entries:
            try:
                owners = [
                    str(row.get("owner_account_id") or "").strip()
                    for row in bindings.list_for_platform(entry.name)
                ]
            except Exception as exc:  # noqa: BLE001 - 绑定存储异常不能影响其它渠道启动
                log.warning("读取平台绑定失败: %s: %s", entry.name, exc)
                continue
            bound_owners[entry.name] = [owner for owner in owners if owner]

    for entry in entries:
        owners = bound_owners.get(entry.name, [])
        if owners:
            for owner in owners:
                _register_platform_channel(
                    crew, channel_manager, entry, owner_account_id=owner, include_env=False
                )
            continue
        _register_platform_channel(
            crew,
            channel_manager,
            entry,
            owner_account_id=LOCAL_OWNER_ACCOUNT_ID,
            include_env=True,
        )


def _wire_delivery_senders(
    channel_manager: ChannelManager,
    delivery_router: DeliveryRouter,
) -> None:
    for name, owner, channel in channel_manager.iter_channels():
        sender = getattr(channel, "send_to_target", None)
        if callable(sender):
            delivery_router.register(name, sender, owner_account_id=owner)


def _clear_host_bindings(
    crew: Any,
    manager: ChannelManager | None,
    router: DeliveryRouter | None,
) -> None:
    if getattr(crew, "channel_manager", None) is manager:
        crew.channel_manager = None
    if getattr(crew, "delivery_router", None) is router:
        crew.delivery_router = None


def build_channels_feature(
    crew: Any,
    *,
    registry: Any,
    session_store: Any,
    enabled: bool = True,
    desired_config_revision: int = 1,
) -> ChannelsFeatureBundle:
    """Build the Channels Feature definition and its candidate runtime service."""

    service = ChannelsService(
        channel_manager=ChannelManager(),
        delivery_router=DeliveryRouter(),
        platform_registry=platform_registry,
    )

    async def install(context: FeatureInstallContext) -> None:
        manager = service.channel_manager
        router = service.delivery_router

        # Deactivation must physically stop all channels before returning.
        context.register_disposer(
            lambda: manager.stop_all(),
            label="resource:channels.stop-all",
            phase=RegistrationPhase.RESOURCE,
        )

        # Host bindings are cleared in CONTRIBUTION phase before owned resources
        # are torn down, so consumers observe fail-closed immediately.
        context.register_disposer(
            lambda: _clear_host_bindings(crew, manager, router),
            label="binding:channels.host",
            phase=RegistrationPhase.CONTRIBUTION,
        )

        if not enabled:
            crew.channel_manager = None
            crew.delivery_router = None
            return

        crew.channel_manager = manager
        crew.delivery_router = router

        _register_enabled_platform_channels(crew, manager)
        _wire_delivery_senders(manager, router)

        register_channel_session_tools(registry, session_store)
        context.register_disposer(
            lambda: registry.unregister("new_conversation"),
            label="tool:new_conversation",
            phase=RegistrationPhase.CONTRIBUTION,
        )

        context.register_service(
            CHANNELS_SERVICE_KEY,
            service,
            label="service:channels",
        )

    definition = FeatureDefinition(
        CHANNELS_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            CHANNELS_FEATURE_ID,
            provides=(CHANNELS_SERVICE_KEY,) if enabled else (),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.RESTART,
    )

    return ChannelsFeatureBundle(definition=definition, service=service)


__all__ = [
    "CHANNELS_FEATURE_ID",
    "CHANNELS_SERVICE_KEY",
    "ChannelsFeatureBundle",
    "ChannelsService",
    "build_channels_feature",
]
