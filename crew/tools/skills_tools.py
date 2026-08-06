"""技能工具：skills_list、skill_view。

把 crew.agent.skills 的能力以工具形式暴露给 LLM。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crew.agent.skills import (
    audit_skills,
    get_package_members,
    get_skill_packages,
    get_skills,
    list_skills,
    repair_skills,
    resolve_package,
    resolve_skill_any,
)
from crew.core.errors import ToolError
from crew.core.runctx import current_active_skill_packages, current_skill_scope
from crew.tools.registry import Registry, tool_result


SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "列出当前专家/上下文中允许使用的技能 metadata。",
    "parameters": {
        "type": "object",
        "properties": {"category": {"type": "string", "description": "可选分类过滤"}},
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "读取某个技能的 SKILL.md、package 的 PACKAGE.md 或技能目录内指定文件。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名、package 名或 package/skill 路径"},
            "file_path": {"type": "string", "description": "技能目录内文件路径，可选"},
        },
        "required": ["name"],
    },
}

SKILL_PACKAGE_OPEN_SCHEMA = {
    "name": "skill_package_open",
    "description": "展开一个 skill package，使其内部 skills 对当前 agent 可见。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "package slug 或 frontmatter name，如 business-travel",
            }
        },
        "required": ["name"],
    },
}

SKILLS_AUDIT_SCHEMA = {
    "name": "skills_audit",
    "description": "检测 skills 的中文展示 metadata、中文 query 示例，以及旧项目固定路径引用。",
    "parameters": {
        "type": "object",
        "properties": {
            "include_optional": {
                "type": "boolean",
                "description": "是否同时检测 optional-skills 中尚未安装的技能，默认 false",
            },
            "only": {
                "type": "string",
                "description": "只检测指定 skill，可传 slug、frontmatter name 或目录名",
            },
        },
        "required": [],
    },
}

SKILLS_REPAIR_SCHEMA = {
    "name": "skills_repair",
    "description": "显式修复 skills：调用模型补全中文 metadata，并把旧路径迁移为 CREW_SESSION_FILE/CREW_ENV_FILE。",
    "parameters": {
        "type": "object",
        "properties": {
            "include_optional": {
                "type": "boolean",
                "description": "是否同时修复 optional-skills 中尚未安装的技能，默认 false",
            },
            "only": {
                "type": "string",
                "description": "只修复指定 skill，可传 slug、frontmatter name 或目录名",
            },
            "dry_run": {
                "type": "boolean",
                "description": "只返回将要修改的内容，不调用模型、不写文件，默认 false",
            },
        },
        "required": [],
    },
}

def _get_skill_category(skills: dict, slug: str) -> str:
    """从 SKILL.md frontmatter 取 category 字段（懒读取）。"""
    info = skills.get(f"/{slug}")
    if not info:
        return ""
    try:
        from crew.agent.skills import _parse_frontmatter as _pfm
        from crew.agent.skills import _registered_skill_dir, read_skill_text

        skill_dir = _registered_skill_dir(Path(info["skill_dir"]))
        content = read_skill_text(Path(info["skill_md_path"]), skill_dir, errors="replace")
        fm, _ = _pfm(content)
        return str(fm.get("category") or "").strip()
    except Exception:
        return ""


def _current_skill_scope() -> tuple[list[str] | None, list[str] | None]:
    """获取当前 agent 上下文中生效的技能范围（enabled, disabled）。"""
    try:
        enabled, disabled = current_skill_scope.get()
        return enabled, disabled
    except Exception:
        return None, None


def handle_skills_list(args: dict[str, Any]) -> str:
    """列出当前专家/上下文允许的技能。委托 crew.agent.skills.list_skills()，统一真相源。

    可选 category 过滤：技能 frontmatter 有 category 字段时生效。
    """
    category = str(args.get("category") or "").strip()
    enabled, disabled = _current_skill_scope()
    items = list_skills(enabled=enabled, disabled=disabled)
    if category:
        # list_skills() 返回的 info 不含 category，从 get_skills() 取完整 info 做过滤
        all_skills = get_skills()
        slug_set = {
            info["slug"]
            for info in all_skills.values()
            if str(all_skills.get(f"/{info['slug']}", {}).get("category") or "") == category
            or _get_skill_category(all_skills, info["slug"]) == category
        }
        items = [s for s in items if s["slug"] in slug_set]
    return tool_result(success=True, skills=items, count=len(items))


def handle_skill_view(args: dict[str, Any]) -> str:
    """读取某个技能的 SKILL.md、package 的 PACKAGE.md 或技能目录内指定文件。

    支持三种 name 形式：
    1. package slug/name（如 business-travel）→ 读取 PACKAGE.md；
    2. skill slug/frontmatter name/目录名 → 读取 SKILL.md；
    3. package/skill 路径（如 business-travel/query-flights）→ 读取对应 SKILL.md。

    越权防护：file_path 不得用 .. 逃出技能目录。
    """
    name = str(args.get("name", "")).strip()
    if not name:
        raise ToolError("name 不能为空")

    # 1. 先尝试作为 package 读取
    pkg = resolve_package(name)
    if pkg is not None:
        pkg_md_path = Path(pkg["package_md_path"])
        content = pkg_md_path.read_text(encoding="utf-8", errors="replace")
        return tool_result(
            success=True,
            name=pkg["name"],
            slug=pkg["slug"],
            type="package",
            path=str(pkg_md_path),
            content=content,
        )

    # 2. 作为 skill 读取
    info = resolve_skill_any(name)
    if info is None:
        available = sorted(v["name"] for v in get_skills().values())
        raise ToolError(f"未找到技能或 package: {name}。可用技能: {', '.join(available)}")

    from crew.agent.skills import (
        SkillPathError,
        _registered_skill_dir,
        _skill_allowed,
        read_skill_text,
        resolve_skill_path,
    )

    enabled, disabled = _current_skill_scope()
    if not _skill_allowed(info["slug"], enabled, disabled, info.get("aliases")):
        raise ToolError(f"技能 {name} 在当前任务上下文中不可用")

    skill_dir = Path(info["skill_dir"])
    file_path = str(args.get("file_path") or "").strip()

    if not file_path:
        # 返回 SKILL.md 内容
        try:
            safe_skill_dir = _registered_skill_dir(skill_dir)
            skill_md = resolve_skill_path(Path(info["skill_md_path"]), safe_skill_dir)
        except SkillPathError as exc:
            raise ToolError(f"路径越权：{exc}") from exc
        content = info.get("content") or read_skill_text(skill_md, safe_skill_dir, errors="replace")
        return tool_result(
            success=True,
            name=info["name"],
            slug=info["slug"],
            type="skill",
            skill_dir=str(skill_dir),
            path=info["skill_md_path"],
            content=content,
        )

    # 指定文件：路径越权防护（禁止 .. 逃出技能目录）
    try:
        safe_skill_dir = _registered_skill_dir(skill_dir)
        target = resolve_skill_path(skill_dir / file_path, safe_skill_dir)
    except SkillPathError as exc:
        raise ToolError(f"路径越权：{file_path} 超出技能目录范围") from exc
    if not target.is_file():
        raise ToolError(f"技能文件不存在: {file_path}")
    return tool_result(
        success=True,
        name=info["name"],
        slug=info["slug"],
        type="skill",
        skill_dir=str(skill_dir),
        path=str(target),
        content=read_skill_text(target, safe_skill_dir, errors="replace"),
    )


def handle_skill_package_open(args: dict[str, Any]) -> str:
    """展开一个 skill package，使其内部 skills 在当前 agent 上下文中可见。

    将 package slug 加入 current_active_skill_packages，后续 system prompt 会展开其内部 skills。
    若 package 不存在或当前 scope 不允许，则报错。
    """
    name = str(args.get("name", "")).strip()
    if not name:
        raise ToolError("name 不能为空")

    pkg = resolve_package(name)
    if pkg is None:
        available = sorted(p["name"] for p in get_skill_packages().values())
        raise ToolError(f"未找到 package: {name}。可用 packages: {', '.join(available)}")

    pkg_slug = pkg["slug"]

    # 检查 package 内至少有一个 skill 被当前 scope 允许
    members = get_package_members(pkg_slug)
    if not members:
        return tool_result(
            success=True,
            package=pkg_slug,
            opened=True,
            members=[],
            hint="该 package 下暂无可用 skills。",
        )

    from crew.agent.skills import _skill_allowed

    enabled, disabled = _current_skill_scope()
    allowed_members = [
        m for m in members
        if _skill_allowed(m["slug"], enabled, disabled, m.get("aliases"))
    ]
    if not allowed_members:
        raise ToolError(f"package {name} 在当前专家上下文中不可用")

    # 加入当前激活集合
    try:
        active = current_active_skill_packages.get()
        active = set(active)
    except Exception:
        active = set()
    active.add(pkg_slug)
    current_active_skill_packages.set(active)

    return tool_result(
        success=True,
        package=pkg_slug,
        opened=True,
        members=[
            {
                "name": m["name"],
                "slug": m["slug"],
                "description": m.get("description_zh") or m.get("description") or "",
            }
            for m in allowed_members
        ],
        hint=f"package '{pkg_slug}' 已展开。你现在可以直接调用其中 skill，或用 /{pkg_slug}/<skill-name> 激活。",
    )


def handle_skills_audit(args: dict[str, Any]) -> str:
    result = audit_skills(
        include_optional=bool(args.get("include_optional", False)),
        only=str(args.get("only") or "").strip() or None,
    )
    return tool_result(success=True, **result)


async def handle_skills_repair(args: dict[str, Any]) -> str:
    result = await repair_skills(
        include_optional=bool(args.get("include_optional", False)),
        only=str(args.get("only") or "").strip() or None,
        dry_run=bool(args.get("dry_run", False)),
    )
    return tool_result(success=True, **result)


def register_skills_tools(registry: Registry) -> None:
    registry.register(
        name="skills_list",
        toolset="skills",
        schema=SKILLS_LIST_SCHEMA,
        handler=handle_skills_list,
        is_async=False,
        display_name="查看技能列表",
        ui_label_template="查看技能列表",
        always_load=True,
        search_hint="list skills discover skill metadata",
    )
    registry.register(
        name="skill_view",
        toolset="skills",
        schema=SKILL_VIEW_SCHEMA,
        handler=handle_skill_view,
        is_async=False,
        display_name="阅读技能",
        ui_label_template="阅读 {name}",
        always_load=True,
        search_hint="view skill read SKILL.md package instructions",
    )
    registry.register(
        name="skill_package_open",
        toolset="skills",
        schema=SKILL_PACKAGE_OPEN_SCHEMA,
        handler=handle_skill_package_open,
        is_async=False,
        display_name="展开技能包",
        ui_label_template="展开技能包 {name}",
        always_load=True,
        search_hint="open skill package expand package skills",
    )
    registry.register(
        name="skills_audit",
        toolset="skills",
        schema=SKILLS_AUDIT_SCHEMA,
        handler=handle_skills_audit,
        is_async=False,
        display_name="检查技能",
        ui_label_template="检查技能",
        always_load=True,
        search_hint="audit skills validate metadata paths",
    )
    registry.register(
        name="skills_repair",
        toolset="skills",
        schema=SKILLS_REPAIR_SCHEMA,
        handler=handle_skills_repair,
        is_async=True,
        display_name="修复技能",
        ui_label_template="修复技能",
        always_load=True,
        search_hint="repair skills update metadata migrate paths",
    )
