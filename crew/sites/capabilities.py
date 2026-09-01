"""Capability Profiles contributed by the Sites and Blueprint domains."""

from crew.agent.capabilities import (
    CapabilityDisplay,
    CapabilityProfile,
    CapabilityProfileRegistry,
)


def register_site_capability_profiles(registry: CapabilityProfileRegistry) -> None:
    registry.register(
        CapabilityProfile(
            id="blueprint.authoring",
            feature="blueprint",
            toolsets=("blueprint",),
            skills=("blueprint", "automation", "widget", "widgetdesign", "binding", "canvas"),
            prompt=(
                "当前会话已启用 Blueprint 创作能力。需要声明式交互界面时，"
                "按 Blueprint Skill 选择最短的 Automation、Widget、Binding、Canvas 资产链。"
            ),
        )
    )
    registry.register(
        CapabilityProfile(
            id="sites.authoring",
            feature="sites",
            toolsets=("sites",),
            skills=("webapp-building",),
            includes=("blueprint.authoring",),
            prompt=(
                "当前会话用于创建或维护 Ace 灵感 App。根据需求选择普通 Web App 或 Blueprint；"
                "只有用户明确要求发布或部署时，才调用 publish_site。"
            ),
            display=CapabilityDisplay(name="灵感", provider="sites", display_badge="◇"),
        )
    )
