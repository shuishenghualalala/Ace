"""SkillScanner：同步扫描 skill roots，只枚举已知布局、只产 metadata。

布局约定：``<root>/<skill>/SKILL.md``，或 package 形式
``<root>/<pkg>/PACKAGE.md`` + ``<root>/<pkg>/<skill>/SKILL.md``。
不做全树递归；需要整树遍历的审计/安装逻辑在 manage.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import (
    SkillPathError,
    _EXCLUDED_DIRS,
    _display_name_from_frontmatter,
    _extract_query_examples,
    _featured_from_frontmatter,
    _metadata_dict,
    _package_description_from_frontmatter,
    _parse_frontmatter,
    _skill_category_from_frontmatter,
    _skill_runtime_metadata,
    _slugify,
    _walk_contained,
    _zh_description_from_frontmatter,
    logger,
    patched,
    read_skill_text,
    resolve_skill_path,
)


def _parse_package_md(package_md: Path, package_root: Path) -> dict[str, Any] | None:
    """解析 PACKAGE.md，返回 package info dict；解析失败返回 None。"""
    try:
        safe_package_root = resolve_skill_path(package_root, package_root)
        safe_package_md = resolve_skill_path(package_md, safe_package_root)
        content = read_skill_text(safe_package_md, safe_package_root)
        fm, body = _parse_frontmatter(content)
        name = str(fm.get("name") or safe_package_md.parent.name).strip()
        if not name:
            return None
        slug = _slugify(name) or _slugify(safe_package_md.parent.name)
        if not slug:
            return None
        description = str(fm.get("description") or "").strip()
        if not description:
            for line in body.strip().splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    description = line[:80]
                    break
        zh_description = _package_description_from_frontmatter(fm, description)
        display_category = _skill_category_from_frontmatter(fm)
        return {
            "name": name,
            "slug": slug,
            "description": description or f"激活 {name} package",
            "description_zh": zh_description,
            "category": display_category,
            "package_md_path": str(safe_package_md),
            "package_dir": str(safe_package_md.parent),
            "content": body.strip(),
        }
    except Exception as exc:
        logger.debug("跳过 package %s: %s", package_md, exc)
        return None


def _is_excluded_dir(name: str) -> bool:
    """判断目录名是否应被扫描跳过。"""
    return name in _EXCLUDED_DIRS or name.startswith(".")


def _iter_skill_files(skills_dir: Path):
    """安全遍历 skills_dir，yield 最终目标仍在各 Skill 根内的 SKILL.md。"""
    # Optional/local skill source may be absent in a development checkout or
    # release that does not ship a catalog. That is an empty source, not an
    # unsafe path; only existing roots need containment validation.
    if not skills_dir.is_dir():
        return
    matches: list[Path] = []
    for _root, files in _walk_contained(skills_dir):
        for path in files:
            if path.name != "SKILL.md":
                continue
            try:
                resolve_skill_path(path, path.parent)
            except SkillPathError as exc:
                logger.warning("跳过不安全 SKILL.md code=%s path=%s", exc.code, exc.path)
                continue
            matches.append(path)
    yield from sorted(matches)


def _iter_package_skills(skills_dir: Path):
    """按 package 感知方式遍历 skills_dir。

    规则：
    1. skills_dir 的直接子目录若包含 PACKAGE.md，则视为 package；
       遍历该 package 的直接子目录中的 SKILL.md 作为 package members。
    2. 不含 PACKAGE.md 但直接子目录含 SKILL.md 的，视为独立 skill。
    3. 跳过 _EXCLUDED_DIRS 和隐藏目录。

    Yields (skill_md_path, package_info_or_none)。
    package_info_or_none 为 None 表示独立 skill，否则为 package info dict。
    """
    if not skills_dir.is_dir():
        return

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or _is_excluded_dir(entry.name):
            continue

        package_md = entry / "PACKAGE.md"
        if package_md.is_file():
            package_info = _parse_package_md(package_md, entry)
            if package_info is None:
                continue
            # package 内的 skills：只扫描直接子目录
            for sub in sorted(entry.iterdir()):
                if not sub.is_dir() or _is_excluded_dir(sub.name):
                    continue
                skill_md = sub / "SKILL.md"
                if skill_md.is_file():
                    yield skill_md, package_info
            continue

        # 独立 skill
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            yield skill_md, None


def scan_dir(
    skills_dir: Path,
    seen: set[str],
) -> tuple[dict[str, dict], dict[str, dict], dict[str, list[str]]]:
    """扫描单个 skills 目录，返回 (skills, packages, package_members)。

    只解析 frontmatter 产出 metadata；SKILL.md 正文不进扫描结果，
    由 SkillIndex 的 body 缓存按需加载。seen 用于目录内去重。
    """
    result: dict[str, dict] = {}
    local_packages: dict[str, dict] = {}
    local_package_members: dict[str, list[str]] = {}
    if not skills_dir.is_dir():
        return result, local_packages, local_package_members

    for skill_md, package_info in _iter_package_skills(skills_dir):
        try:
            safe_skill_dir = resolve_skill_path(skill_md.parent, skills_dir)
            content = read_skill_text(skill_md, safe_skill_dir)
            fm, body = _parse_frontmatter(content)
            name = str(fm.get("name") or skill_md.parent.name).strip()
            if not name:
                continue

            base_slug = _slugify(name) or _slugify(skill_md.parent.name)
            if not base_slug:
                continue

            if package_info is not None:
                package_slug = package_info["slug"]
                slug = f"{package_slug}/{base_slug}"
                aliases = [base_slug]
            else:
                package_slug = ""
                slug = base_slug
                aliases = []

            if slug in seen:
                continue
            seen.add(slug)

            # 记录 package 与 member 关系（key 统一带前导 /，与 skills 一致）
            if package_info is not None:
                local_packages[f"/{package_slug}"] = package_info
                local_package_members.setdefault(f"/{package_slug}", []).append(f"/{slug}")

            description = str(fm.get("description") or "").strip()
            if not description:
                for line in body.strip().splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        description = line[:80]
                        break
            display_name = _display_name_from_frontmatter(fm, name)
            zh_description = _zh_description_from_frontmatter(fm, description)
            display_category = _skill_category_from_frontmatter(fm)
            required_tools, required_env, entrypoints = _skill_runtime_metadata(fm)
            result[f"/{slug}"] = {
                "name": name,
                "display_name": display_name,
                "slug": slug,
                "base_slug": base_slug,
                "aliases": aliases,
                "description": description or f"激活 {name} skill",
                "description_zh": zh_description,
                "query_examples": _extract_query_examples(fm),
                "category": display_category,
                "featured": _featured_from_frontmatter(fm),
                "version": str(_metadata_dict(fm).get("version") or fm.get("version") or "").strip(),
                "skill_md_path": str(skill_md),
                "skill_dir": str(skill_md.parent),
                "required_tools": required_tools,
                "required_env": required_env,
                "entrypoints": entrypoints,
                "package": package_slug,
                "package_path": package_info["package_dir"] if package_info else "",
            }
        except Exception as exc:
            logger.debug("跳过 skill %s: %s", skill_md, exc)

    return result, local_packages, local_package_members


def scan_all() -> tuple[dict[str, dict], dict[str, dict], dict[str, list[str]]]:
    """扫描 builtin/plugin/user 三层，返回 (skills, packages, package_members)。

    覆盖优先级：user > plugin > builtin（用户 skill 覆盖同名插件/内置 skill）。
    插件层来自已加载插件声明的 skills/ 根（见 configure_plugin_skill_roots）。
    """
    skills: dict[str, dict] = {}
    packages: dict[str, dict] = {}
    package_members: dict[str, list[str]] = {}

    def merge(
        result: dict[str, dict],
        local_packages: dict[str, dict],
        local_members: dict[str, list[str]],
    ) -> None:
        skills.update(result)
        packages.update(local_packages)
        for pkg_slug, members in local_members.items():
            package_members.setdefault(pkg_slug, []).extend(members)

    merge(*scan_dir(patched("get_builtin_skills_dir")(), set()))
    for root in patched("get_plugin_skill_roots")():
        merge(*scan_dir(root, set()))
    merge(*scan_dir(patched("get_user_skills_dir")(), set()))
    return skills, packages, package_members


def _stat_or_zero(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def stat_scan_key() -> tuple:
    """(path, mtime_ns) 序列：stat 回退模式下 get_skills 的缓存失效依据。

    与 scan_dir 同一套一层 + package 感知布局枚举；只 stat 不读取内容，
    失效检测不是内容读取通道，无需逐文件 containment 解析。
    """
    key: list[tuple[str, int]] = []
    roots = (
        patched("get_builtin_skills_dir")(),
        patched("get_user_skills_dir")(),
        *patched("get_plugin_skill_roots")(),
    )
    for root in roots:
        if not root.is_dir():
            key.append((str(root), 0))
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            key.append((str(root), 0))
            continue
        for entry in entries:
            if not entry.is_dir() or _is_excluded_dir(entry.name):
                continue
            package_md = entry / "PACKAGE.md"
            if package_md.is_file():
                key.append((str(package_md), _stat_or_zero(package_md)))
                try:
                    subs = sorted(entry.iterdir())
                except OSError:
                    continue
                for sub in subs:
                    if not sub.is_dir() or _is_excluded_dir(sub.name):
                        continue
                    skill_md = sub / "SKILL.md"
                    if skill_md.is_file():
                        key.append((str(skill_md), _stat_or_zero(skill_md)))
                continue
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                key.append((str(skill_md), _stat_or_zero(skill_md)))
    return tuple(key)
