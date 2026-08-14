"""Shared capability vocabulary for TeamSpec, AgentProfile and FormationPlan."""

from __future__ import annotations

from collections.abc import Iterable

AGENT_PROFILE_VERSION = 4

# Keep the existing capability keys stable and extend them with domain-neutral
# research, reasoning and review capabilities.  The order is also the stable
# presentation and tie-breaking order used by Formation.
CAPABILITIES = (
    "planning",
    "requirements",
    "information_retrieval",
    "research",
    "analysis",
    "synthesis",
    "review",
    "design",
    "frontend",
    "backend",
    "implementation",
    "testing",
    "verification",
    "documentation",
)

CAPABILITY_LABELS = {
    "planning": "规划统筹",
    "requirements": "需求澄清",
    "information_retrieval": "信息检索",
    "research": "研究调研",
    "analysis": "分析论证",
    "synthesis": "综合汇总",
    "review": "独立审阅",
    "design": "体验设计",
    "frontend": "前端实现",
    "backend": "后端实现",
    "implementation": "执行实现",
    "testing": "测试验证",
    "verification": "核验复核",
    "documentation": "文档交付",
}

CAPABILITY_ROLE_KEYS = {
    "planning": "project_manager",
    "requirements": "product_manager",
    "information_retrieval": "research_analyst",
    "research": "research_analyst",
    "analysis": "research_analyst",
    "synthesis": "technical_writer",
    "review": "independent_reviewer",
    "design": "ui_designer",
    "frontend": "frontend_developer",
    "backend": "backend_developer",
    "implementation": "fullstack_developer",
    "testing": "qa_engineer",
    "verification": "independent_reviewer",
    "documentation": "technical_writer",
}

CAPABILITY_SIGNALS = {
    "planning": ("leader", "负责人", "队长", "统筹", "规划", "拆解", "协调", "管理"),
    "requirements": ("需求", "范围", "验收标准", "用户故事", "prd", "咨询"),
    "information_retrieval": (
        "检索", "搜索", "查资料", "搜集", "资料", "文献", "来源", "search", "retrieval", "browse",
    ),
    "research": ("调研", "研究", "竞品", "综述", "research", "literature"),
    "analysis": ("分析", "比较", "对比", "归纳", "推理", "论证", "评估", "咨询", "analysis"),
    "synthesis": ("汇总", "综合", "综述", "总结", "整合", "归纳", "synthesis"),
    "review": ("审阅", "评审", "复核", "交叉检查", "反方", "审查", "review"),
    "design": ("设计", "视觉", "ui", "ux", "交互", "样式", "像素风", "界面"),
    "frontend": ("前端", "页面", "浏览器", "react", "vue", "css", "组件", "web"),
    "backend": ("后端", "接口", "api", "服务端", "数据库", "鉴权"),
    "implementation": ("开发", "实现", "编码", "执行", "工程", "代码", "功能", "build", "implement"),
    "testing": ("测试", "qa", "回归", "质量", "缺陷", "bug", "test"),
    "verification": ("核验", "复核", "验证", "验收", "事实检查", "引用检查", "证据", "verify"),
    "documentation": ("文档", "说明", "报告", "记录", "写作", "材料", "交付材料", "document"),
}

CAPABILITY_ALIASES = {
    "plan": "planning",
    "coordination": "planning",
    "scope": "requirements",
    "acceptance": "requirements",
    "retrieval": "information_retrieval",
    "search": "information_retrieval",
    "summary": "synthesis",
    "writing": "documentation",
    "docs": "documentation",
    "handoff": "documentation",
    "test_report": "documentation",
    "qa": "testing",
    "test": "testing",
    "verify": "verification",
    "build": "implementation",
    "code": "implementation",
    "integration": "implementation",
    "ui": "design",
    "ux": "design",
    "visual": "design",
}

# A stronger specialised capability can provide a weaker prior for its parent
# capability.  It does not work in reverse: generic implementation evidence is
# not evidence of frontend or backend specialization.
CAPABILITY_IMPLICATIONS = {
    "research": ("information_retrieval", "analysis"),
    "frontend": ("implementation",),
    "backend": ("implementation",),
    "testing": ("verification",),
    "documentation": ("synthesis",),
}


def normalize_capability(value: object) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = CAPABILITY_ALIASES.get(key, key)
    return key if key in CAPABILITIES else ""


def normalize_capabilities(values: Iterable[object]) -> list[str]:
    normalized = {normalize_capability(value) for value in values}
    return [capability for capability in CAPABILITIES if capability in normalized]


def capabilities_from_text(text: object, *, include_planning: bool = False) -> list[str]:
    lowered = str(text or "").lower()
    detected = {
        capability
        for capability, signals in CAPABILITY_SIGNALS.items()
        if (include_planning or capability != "planning")
        and any(signal in lowered for signal in signals)
    }
    return [capability for capability in CAPABILITIES if capability in detected]


def capability_label(capability: object) -> str:
    key = normalize_capability(capability)
    return CAPABILITY_LABELS.get(key, str(capability or ""))


def implied_capabilities(capability: object) -> tuple[str, ...]:
    key = normalize_capability(capability)
    return CAPABILITY_IMPLICATIONS.get(key, ())
