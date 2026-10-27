"""Capability Profiles contributed by the Sites and Blueprint domains."""

from crew.agent.capabilities import (
    CapabilityDisplay,
    CapabilityProfile,
    CapabilityProfileRegistry,
)


def register_site_capability_profiles(
    registry: CapabilityProfileRegistry,
) -> tuple[CapabilityProfile, ...]:
    """Register Sites profiles and return their owned identities for cleanup."""
    profiles = (
        CapabilityProfile(
            id="blueprint.authoring",
            feature="blueprint",
            toolsets=("blueprint",),
            skills=("blueprint", "automation", "widget", "widgetdesign", "binding", "canvas"),
            prompt=(
                "当前会话已启用 Blueprint 创作能力。需要声明式交互界面时，"
                "桌面回复中的普通图表、参数探索、输入表单，优先读取 widget/references/native-visual.md 并调用 Widget.render(spec) 原生展示。"
                "这一路径不写 HTML/JS，不创建文件、网站、Canvas 或 Automation，不打开浏览器；只返回结构化数据与受支持的运算。"
                "标题、单位、假设写在 spec，工具完成后简短解释结果，避免重复标题与表格；纯静态图不添加多余控件或续问动作。"
                "只有明确需要独立 App 或复杂自定义页面时才按 Blueprint Skill 选择 Automation、Widget、Binding、Canvas 资产链。"
            ),
        ),
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
        ),
    )
    registered: list[CapabilityProfile] = []
    try:
        for profile in profiles:
            registered.append(registry.register(profile))
    except Exception:
        for profile in reversed(registered):
            registry.unregister(profile.id, expected=profile)
        raise
    return tuple(registered)
