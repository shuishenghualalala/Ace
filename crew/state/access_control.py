"""访问控制配置解析。

user_type 区分 external/internal 用户，控制：
- 加载哪个 prompt profile（config/prompts/profiles/*.md）
- 可用 toolsets / tools
- 可用 plugins
- 可用 skills

约定：
- enabled_* 为 None、缺失或 ["*"] 时表示“全部可用”
- enabled_* 为 [] 时表示“全部不可用”
- disabled_* 为 None、缺失或 [] 时表示“不禁用”
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_PROFILES_DIR = _REPO_ROOT / "config" / "prompts" / "profiles"


@dataclass
class AccessControlConfig:
    """访问控制配置。"""

    user_type: str = "internal"  # "external" | "internal"
    external: dict[str, Any] = field(default_factory=dict)
    internal: dict[str, Any] = field(default_factory=dict)

    def resolve_for(self, user_type: str | None = None) -> dict[str, Any]:
        """为指定用户类型解析出统一配置字典。

        Returns:
            {
                "user_type": "external" | "internal",
                "prompt_profile": str,
                "prompt_profile_path": str,
                "enabled_toolsets": list[str] | None,
                "disabled_toolsets": list[str] | None,
                "enabled_plugins": list[str] | None,
                "disabled_plugins": list[str] | None,
                "enabled_skills": list[str] | None,
                "disabled_skills": list[str] | None,
            }
        """
        ut = (user_type or self.user_type).lower()
        if ut not in ("external", "internal"):
            ut = self.user_type
        if ut not in ("external", "internal"):
            ut = "internal"

        section = self.external if ut == "external" else self.internal

        prompt_profile = str(section.get("prompt_profile") or ut)
        profile_path = _PROMPT_PROFILES_DIR / f"{prompt_profile}.md"

        enabled_toolsets = _normalize_list(section.get("enabled_toolsets"))
        disabled_toolsets = _normalize_list(section.get("disabled_toolsets"))
        # Browser 是高权限工具集，对 external 采用 fail-closed：只有明确将
        # "browser" 列入 enabled_toolsets 才视为授权；宽松的 "*" 不构成显式授权。
        if ut == "external":
            explicit_browser_optin = enabled_toolsets is not None and "browser" in enabled_toolsets
            if not explicit_browser_optin and "browser" not in (disabled_toolsets or []):
                disabled_toolsets = [*(disabled_toolsets or []), "browser"]

        return {
            "user_type": ut,
            "prompt_profile": prompt_profile,
            "prompt_profile_path": str(profile_path),
            "enabled_toolsets": enabled_toolsets,
            "disabled_toolsets": disabled_toolsets,
            "enabled_tools": _normalize_list(section.get("enabled_tools")),
            "disabled_tools": _normalize_list(section.get("disabled_tools")),
            "enabled_plugins": _normalize_list(section.get("enabled_plugins")),
            "disabled_plugins": _normalize_list(section.get("disabled_plugins")),
            "enabled_skills": _normalize_list(section.get("enabled_skills")),
            "disabled_skills": _normalize_list(section.get("disabled_skills")),
        }


def _normalize_list(value: Any) -> list[str] | None:
    """把配置值归一化为字符串列表或 None。

    None / 缺失 → None（无限制）
    ["*"] → ["*"]（显式全部）
    [] → []（空白名单/黑名单，按语义解读）
    其他列表 → 字符串化并去空
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        result = [str(item).strip() for item in value if str(item).strip()]
        # 显式保留 ["*"]，让它与 None 区分
        return result
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        return [stripped]
    return None


def is_star_or_empty(items: list[str] | None) -> bool:
    """判断列表是否表示“无限制”（None 或显式 ["*"]）。"""
    return items is None or (len(items) == 1 and items[0] == "*")
