"""Skills 子包基础原语：路径、安全 containment、frontmatter 与展示 metadata。

本模块不放任何缓存状态；可变的缓存全局量集中在包命名空间（__init__.py），
测试在 ``crew.agent.skills`` 上 monkeypatch 的目录 getter / 常量经 ``patched()``
动态解析，保证子模块内部调用与包级 patch 看到同一份值。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("crew.agent.skills")

_MISSING = object()


def _ns():
    """包命名空间模块；子模块拆分包期间或解释器退出时可能尚未/不再可用。"""
    return sys.modules.get("crew.agent.skills")


def patched(name: str, default: Any = _MISSING) -> Any:
    """从包命名空间取名字（测试 monkeypatch 生效），缺省回退本模块同名定义。"""
    ns = _ns()
    if ns is not None:
        value = getattr(ns, name, _MISSING)
        if value is not _MISSING:
            return value
    value = globals().get(name, _MISSING)
    if value is not _MISSING:
        return value
    if default is not _MISSING:
        return default
    raise AttributeError(name)


# ── 路径 ───────────────────────────────────────────────────────────────────

# 仓库根目录（crew/agent/skills/ 包的上三层）
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    _REPO_ROOT = Path(sys._MEIPASS)
else:
    _REPO_ROOT = Path(__file__).resolve().parents[3]

# 正则：slug 化 skill 名称（去除非法字符、合并连字符）
_SLUG_INVALID = re.compile(r"[^a-z0-9-]")
_SLUG_MULTI_HYPHEN = re.compile(r"-{2,}")

# 正则：SKILL.md frontmatter
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# 正则：模板变量替换
_TEMPLATE_VAR_RE = re.compile(r"\$\{(CREW_SKILL_DIR|CREW_SESSION_ID)\}")

# 正则：中文检测（用于前端展示元数据审计）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

SKILL_CATEGORY_NAMES: tuple[str, ...] = (
    "通用办公",
    "图像处理",
    "设计与开发",
    "经营管理",
    "人力资源",
    "音视频处理",
)

_LEGACY_SKILL_CATEGORY_MAP = {
    "办公": "通用办公",
    "通用": "通用办公",
    "语言": "通用办公",
    "数据": "经营管理",
    "研究": "经营管理",
}


def _normalize_skill_category(value: object) -> str | None:
    """把当前或历史分类名归一化为公开分类；未知值返回 ``None``。"""
    category = str(value or "").strip()
    if category in SKILL_CATEGORY_NAMES:
        return category
    return _LEGACY_SKILL_CATEGORY_MAP.get(category)

_REPAIRABLE_TEXT_SUFFIXES = {
    ".md", ".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".txt", ".sh", ".ps1", ".conf",
}

# 扫描时跳过的目录名（采用 EXCLUDED_SKILL_DIRS）
_EXCLUDED_DIRS = frozenset({
    ".git", ".github", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".archive", ".hub", "dist", "build",
})


@dataclass(frozen=True)
class SkillEntrypoint:
    """A machine-declared executable entrypoint inside one Skill."""

    id: str
    path: str
    runtime: str
    writable_paths: tuple[str, ...] = ()
    side_effect: str = ""
    timeout_seconds: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "runtime": self.runtime,
            "writable_paths": list(self.writable_paths),
            "side_effect": self.side_effect,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SkillEntrypoint":
        try:
            timeout_seconds = min(
                300.0,
                max(1.0, float(raw.get("timeout_seconds") or 120.0)),
            )
        except (TypeError, ValueError):
            timeout_seconds = 120.0
        return cls(
            id=str(raw.get("id") or "").strip(),
            path=str(raw.get("path") or "").strip(),
            runtime=str(raw.get("runtime") or "").strip(),
            writable_paths=tuple(
                str(item).strip()
                for item in (raw.get("writable_paths") or [])
                if str(item).strip()
            ),
            side_effect=str(raw.get("side_effect") or "").strip(),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class SkillActivation:
    """Immutable current-turn Skill activation passed to every executor.

    This is an ephemeral execution snapshot, not a persisted authorization
    record.  Identity authentication and operation approval remain separate.
    """

    skill_id: str
    name: str
    instruction: str
    skill_root: str
    required_tools: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    entrypoints: tuple[SkillEntrypoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "instruction": self.instruction,
            "skill_root": self.skill_root,
            "required_tools": list(self.required_tools),
            "required_env": list(self.required_env),
            "entrypoints": [item.to_dict() for item in self.entrypoints],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SkillActivation":
        return cls(
            skill_id=str(raw.get("skill_id") or "").strip(),
            name=str(raw.get("name") or "").strip(),
            instruction=str(raw.get("instruction") or ""),
            skill_root=str(raw.get("skill_root") or "").strip(),
            required_tools=tuple(
                str(item).strip()
                for item in (raw.get("required_tools") or [])
                if str(item).strip()
            ),
            required_env=tuple(
                str(item).strip()
                for item in (raw.get("required_env") or [])
                if str(item).strip()
            ),
            entrypoints=tuple(
                SkillEntrypoint.from_dict(item)
                for item in (raw.get("entrypoints") or [])
                if isinstance(item, dict)
            ),
        )


def configure_plugin_skill_roots(provider: Callable[[], list[str] | None] | None) -> None:
    """注入插件 skill roots 提供方；None 表示无插件 skill 层。

    提供方存放在包命名空间（``crew.agent.skills._plugin_skill_roots_provider``），
    让测试与宿主按历史契约直接读写该模块属性。
    """
    ns = _ns()
    if ns is not None:
        ns._plugin_skill_roots_provider = provider


def _plugin_roots_provider() -> Callable[[], list[str]] | None:
    ns = _ns()
    if ns is None:
        return None
    return getattr(ns, "_plugin_skill_roots_provider", None)


def get_plugin_skill_roots() -> list[Path]:
    """当前已加载且启用插件的 skill 根目录列表（provider 异常时按空处理）。"""
    provider = _plugin_roots_provider()
    if provider is None:
        return []
    try:
        roots = provider() or []
    except Exception:  # noqa: BLE001 - skill 层不放大插件故障
        logger.debug("读取插件 skill roots 失败", exc_info=True)
        return []
    return [Path(root) for root in roots]


def configure_skill_filter(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> None:
    """配置全局 skill 白名单/黑名单。

    None 或 ["*"] 表示该维度不限制。
    """
    ns = _ns()
    if ns is not None:
        ns._skill_filter["enabled"] = enabled
        ns._skill_filter["disabled"] = disabled


def _configured_skill_filter() -> dict[str, list[str] | None]:
    ns = _ns()
    if ns is None:
        return {"enabled": None, "disabled": None}
    return getattr(ns, "_skill_filter", {"enabled": None, "disabled": None})


def _skill_allowed(
    slug: str,
    enabled: list[str] | None,
    disabled: list[str] | None,
    aliases: list[str] | None = None,
) -> bool:
    """判断 skill slug（及其 alias）是否通过白名单/黑名单过滤。"""
    check_slugs = {slug}
    if aliases:
        check_slugs.update(a.lstrip("/") for a in aliases)

    if disabled is not None:
        if len(disabled) == 1 and disabled[0] == "*":
            return False
        if any(s in disabled for s in check_slugs):
            return False
    if enabled is not None:
        if len(enabled) == 1 and enabled[0] == "*":
            return True
        if any(s in enabled for s in check_slugs):
            return True
        return False
    return True


def get_builtin_skills_dir() -> Path:
    """仓库内置 skills 目录：<repo>/crew/skills/。"""
    return _REPO_ROOT / "crew" / "skills"


def get_user_skills_dir() -> Path:
    """用户 skills 目录：get_crew_home()/skills/。"""
    from crew.state.home import get_crew_home
    return get_crew_home() / "skills"


def get_optional_skills_dir() -> Path:
    """可安装 skills 目录：<repo>/optional-skills/。"""
    return _REPO_ROOT / "optional-skills"


def get_local_skills_dir() -> Path:
    """本地可安装 skills 目录：~/.agents/skills/。

    跨 agent 共享的 skill 源（如 ``npx skills add`` 安装的飞书 skills）。
    默认 ``~/.agents/skills``，可通过 ``CREW_LOCAL_SKILLS_DIR`` 覆盖（相对路径解析为
    相对于用户家目录）。仅用于"可安装"展示；安装时以软链发布到用户目录，源更新自动同步。
    """
    val = os.environ.get("CREW_LOCAL_SKILLS_DIR", "").strip()
    if val:
        p = Path(val).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        return p
    return Path.home() / ".agents" / "skills"


class SkillPathError(ValueError):
    """Skill 路径无法证明位于允许根内，或解析时遇到悬空/环。"""

    def __init__(self, code: str, path: Path, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _trusted_link_target_roots() -> list[Path]:
    """受信任的软链目标根列表。

    安装本地 skill 时在用户目录建立软链指向 ``~/.agents/skills/<name>``；
    ``resolve_skill_path`` 默认拒绝越界软链，这里放行"目标落在受信任根内"的软链，
    让 scan/validate/view/uninstall 全链路接受本地软链 skill。安全边界：仅这些根
    内的目标被放行，软链到 /etc、~/.ssh 等仍被拒绝（resolve 递归跟随到最终真实路径）。
    """
    roots: list[Path] = []
    for root in (patched("get_local_skills_dir")(),):
        try:
            if root.is_dir():
                roots.append(root.resolve(strict=False))
        except OSError:
            continue
    return roots


def _is_within_trusted_link_target(resolved: Path) -> bool:
    """resolved 是否落在受信任软链目标根内（用于放行本地 skill 软链）。"""
    for root in _trusted_link_target_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def _is_trusted_local_link(path: Path) -> bool:
    """path 是否为指向受信任本地源根的软链（本地 skill 安装产物）。

    卸载时据此区分"本地 skill 软链"（unlink 软链本身，绝不递归删源）与普通目录 /
    不受信软链（拒绝）。非软链、悬空软链、指向白名单外的软链均返回 False。
    """
    if not path.is_symlink():
        return False
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return _is_within_trusted_link_target(resolved)


def resolve_skill_path(path: Path, allowed_root: Path, *, must_exist: bool = True) -> Path:
    """解析 Skill 路径并证明最终目标位于 ``allowed_root`` 内。

    该检查跟随文件符号链接与 Windows junction；悬空、目录环、不可读路径和越界目标
    全部 fail closed。写入方可用 ``must_exist=False`` 校验尚未创建的最终目标。

    例外：目标落在受信任软链根（``_trusted_link_target_roots``，即 ~/.agents/skills）
    内的软链被放行，用于本地 skill 软链安装；其余越界软链仍 fail closed。
    """
    path = Path(path)
    allowed_root = Path(allowed_root)
    try:
        root = allowed_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SkillPathError("skill_root_missing", allowed_root, f"Skill 根目录不存在: {allowed_root}") from exc
    except (OSError, RuntimeError) as exc:
        raise SkillPathError("skill_path_cycle", allowed_root, f"Skill 根目录无法安全解析: {allowed_root}") from exc

    try:
        resolved = path.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise SkillPathError("skill_path_dangling", path, f"Skill 路径悬空或不存在: {path}") from exc
    except (OSError, RuntimeError) as exc:
        raise SkillPathError("skill_path_cycle", path, f"Skill 路径无法安全解析: {path}") from exc

    if resolved != root and root not in resolved.parents:
        if not _is_within_trusted_link_target(resolved):
            raise SkillPathError("skill_path_outside", path, f"Skill 路径越权: {path}")
    return resolved


def read_skill_text(path: Path, allowed_root: Path, *, errors: str = "strict") -> str:
    """在读取前后验证同一 resolved target 始终位于 Skill 根内。"""
    before = resolve_skill_path(path, allowed_root)
    content = before.read_text(encoding="utf-8", errors=errors)
    after = resolve_skill_path(path, allowed_root)
    if after != before:
        raise SkillPathError("skill_path_changed", path, f"Skill 路径在读取期间发生变化: {path}")
    return content


def _containment_finding(exc: SkillPathError) -> dict[str, Any]:
    return {
        "code": exc.code,
        "severity": "error",
        "file": str(exc.path),
        "suggestion": str(exc),
    }


def _lexical_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _resolved_path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_link_or_reparse(path: Path) -> bool:
    """识别符号链接及 Windows reparse/junction，避免交给递归删除。"""
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attrs & reparse_flag)


def _walk_contained(skill_root: Path, findings: list[dict[str, Any]] | None = None):
    """安全遍历 Skill 树；允许 root 内链接，剪枝越界、悬空、环和重复目录。"""

    def record(exc: SkillPathError) -> None:
        if findings is not None:
            findings.append(_containment_finding(exc))
        else:
            logger.warning("跳过不安全 Skill 路径 code=%s path=%s", exc.code, exc.path)

    try:
        root = resolve_skill_path(skill_root, skill_root)
    except SkillPathError as exc:
        record(exc)
        return

    seen: set[str] = set()
    ancestry: dict[str, frozenset[str]] = {_lexical_path_key(skill_root): frozenset()}

    def onerror(exc: OSError) -> None:
        record(SkillPathError("skill_path_unreadable", Path(exc.filename or skill_root), str(exc)))

    for current_raw, dirs, files in os.walk(
        skill_root,
        topdown=True,
        followlinks=True,
        onerror=onerror,
    ):
        current = Path(current_raw)
        parents = ancestry.get(_lexical_path_key(current), frozenset())
        try:
            current_resolved = resolve_skill_path(current, root)
        except SkillPathError as exc:
            dirs[:] = []
            record(exc)
            continue
        current_key = _resolved_path_key(current_resolved)
        if current_key in parents:
            dirs[:] = []
            record(SkillPathError("skill_path_cycle", current, f"Skill 目录环: {current}"))
            continue
        if current_key in seen:
            dirs[:] = []
            continue
        seen.add(current_key)
        lineage = frozenset({*parents, current_key})

        safe_dirs: list[str] = []
        for name in sorted(dirs):
            if name in _EXCLUDED_DIRS or name.startswith("."):
                continue
            child = current / name
            try:
                child_resolved = resolve_skill_path(child, root)
            except SkillPathError as exc:
                record(exc)
                continue
            child_key = _resolved_path_key(child_resolved)
            if child_key in lineage:
                record(SkillPathError("skill_path_cycle", child, f"Skill 目录环: {child}"))
                continue
            if child_key in seen:
                continue
            safe_dirs.append(name)
            ancestry[_lexical_path_key(child)] = lineage
        dirs[:] = safe_dirs

        safe_files: list[Path] = []
        for name in sorted(files):
            path = current / name
            try:
                resolve_skill_path(path, root)
            except SkillPathError as exc:
                record(exc)
                continue
            safe_files.append(path)
        yield current, safe_files


def _registered_skill_dir(path: Path) -> Path:
    """把已发现 Skill 重新绑定到权威根之一（builtin/user/optional/插件 skill roots）。"""
    last_error: SkillPathError | None = None
    for root in (
        patched("get_builtin_skills_dir")(),
        patched("get_user_skills_dir")(),
        patched("get_optional_skills_dir")(),
        *patched("get_plugin_skill_roots")(),
        patched("get_local_skills_dir")(),
    ):
        if not root.is_dir():
            continue
        try:
            return resolve_skill_path(path, root)
        except SkillPathError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise SkillPathError("skill_path_outside", path, f"Skill 不在已配置根目录内: {path}")


def _validate_skill_tree(skill_dir: Path, allowed_root: Path) -> Path:
    """完整遍历并验证一个 Skill；任一不安全入口都会使整个操作失败。"""
    resolved = resolve_skill_path(skill_dir, allowed_root)
    findings: list[dict[str, Any]] = []
    for _ in _walk_contained(skill_dir, findings):
        pass
    if findings:
        first = findings[0]
        raise SkillPathError(str(first["code"]), Path(str(first["file"])), str(first["suggestion"]))
    resolve_skill_path(skill_dir / "SKILL.md", skill_dir)
    return resolved


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 SKILL.md：YAML frontmatter + 正文。

    Returns (frontmatter_dict, body)。frontmatter 解析失败则返回 ({}, content)。
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    yaml_text = match.group(1)
    body = content[match.end():]
    frontmatter: dict = {}

    try:
        import yaml
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # 降级：简单 key: value 解析
        for line in yaml_text.strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                frontmatter[k.strip()] = v.strip()

    return frontmatter, body


def _format_skill_markdown(frontmatter: dict, body: str) -> str:
    """把 frontmatter + body 写回 SKILL.md。"""
    import yaml

    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    body_text = body.lstrip("\n")
    return f"---\n{yaml_text}\n---\n{body_text}"


def _slugify(name: str) -> str:
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = _SLUG_INVALID.sub("", slug)
    slug = _SLUG_MULTI_HYPHEN.sub("-", slug).strip("-")
    return slug


def _contains_cjk(value: Any) -> bool:
    return bool(_CJK_RE.search(str(value or "")))


def _metadata_dict(frontmatter: dict) -> dict:
    meta = frontmatter.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _skill_runtime_metadata(frontmatter: dict) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Normalize external-runtime requirements without leaking raw metadata."""
    metadata = _metadata_dict(frontmatter)
    crew_meta = metadata.get("crew")
    crew_meta = crew_meta if isinstance(crew_meta, dict) else {}
    requires = crew_meta.get("requires")
    requires = requires if isinstance(requires, dict) else {}
    required_tools = [
        str(item).strip()
        for item in (requires.get("tools") or [])
        if str(item).strip()
    ]
    required_env = [
        str(item).strip()
        for item in (requires.get("env") or [])
        if str(item).strip()
    ]

    raw_entrypoints = crew_meta.get("entrypoints") or []
    if isinstance(raw_entrypoints, dict):
        raw_entrypoints = [
            {"id": key, **(value if isinstance(value, dict) else {"path": value})}
            for key, value in raw_entrypoints.items()
        ]
    entrypoints: list[dict[str, Any]] = []
    for raw in raw_entrypoints if isinstance(raw_entrypoints, list) else []:
        if not isinstance(raw, dict):
            continue
        entry_id = str(raw.get("id") or "").strip()
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        runtime = str(raw.get("runtime") or "").strip().lower()
        if not entry_id or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            continue
        if not runtime:
            runtime = "python" if path.endswith(".py") else "node" if path.endswith((".js", ".cjs", ".mjs")) else ""
        if runtime not in {"python", "node"}:
            continue
        timeout = raw.get("timeoutSeconds", raw.get("timeout_seconds", 120))
        try:
            timeout_seconds = min(300.0, max(1.0, float(timeout)))
        except (TypeError, ValueError):
            timeout_seconds = 120.0
        entrypoints.append({
            "id": entry_id,
            "path": path,
            "runtime": runtime,
            "writable_paths": [
                str(item).strip().replace("\\", "/")
                for item in (raw.get("writablePaths") or raw.get("writable_paths") or [])
                if str(item).strip()
            ],
            "side_effect": str(raw.get("sideEffect") or raw.get("side_effect") or "").strip(),
            "timeout_seconds": timeout_seconds,
        })
    return required_tools, required_env, entrypoints


def _first_text(frontmatter: dict, keys: tuple[str, ...]) -> str:
    """按优先级从 metadata 和顶层 frontmatter 里取第一个非空文本字段。"""
    meta = _metadata_dict(frontmatter)
    for source in (meta, frontmatter):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_query_examples(frontmatter: dict) -> list[str]:
    """从 metadata/frontmatter 里提取 query 示例，兼容 list[str] 和 list[dict]。"""
    meta = _metadata_dict(frontmatter)
    candidates = (
        meta.get("query_examples"),
        meta.get("queries"),
        meta.get("examples"),
        frontmatter.get("query_examples"),
        frontmatter.get("examples"),
    )
    result: list[str] = []
    for value in candidates:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    result.append(item.strip())
                elif isinstance(item, dict):
                    query = item.get("query") or item.get("prompt") or item.get("text")
                    if isinstance(query, str) and query.strip():
                        result.append(query.strip())
        if result:
            break
    return result


def _skill_category_from_frontmatter(frontmatter: dict) -> str:
    """读取正式分类字段，并兼容迁移旧顶层 category。"""
    meta = _metadata_dict(frontmatter)
    value = _normalize_skill_category(meta.get("skillCategoryName"))
    if value:
        return value

    legacy = _normalize_skill_category(frontmatter.get("category"))
    if legacy:
        return legacy
    return "通用办公"


def _display_name_from_frontmatter(frontmatter: dict, fallback: str) -> str:
    value = _first_text(
        frontmatter,
        ("zh_name", "name_zh", "display_name_zh", "display_name", "title_zh", "title"),
    )
    return value if value and _contains_cjk(value) else fallback


def _zh_description_from_frontmatter(frontmatter: dict, fallback: str) -> str:
    value = _first_text(
        frontmatter,
        ("zh_description", "description_zh", "display_description_zh", "summary_zh"),
    )
    if value and _contains_cjk(value):
        return value
    return fallback if _contains_cjk(fallback) else ""


def _featured_from_frontmatter(frontmatter: dict) -> bool:
    """读取首页精选标记；缺省或非明确真值时均视为非精选。"""
    value = frontmatter.get("featured", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _package_description_from_frontmatter(frontmatter: dict, fallback: str) -> str:
    """按优先级取 package 描述，优先中文。"""
    value = _first_text(
        frontmatter,
        ("zh_description", "description_zh", "display_description_zh", "summary_zh", "description"),
    )
    if value and _contains_cjk(value):
        return value
    value = str(frontmatter.get("description") or "").strip()
    if value:
        return value
    return fallback if _contains_cjk(fallback) else ""


def _preprocess_content(content: str, skill_dir: Path, session_id: str | None = None) -> str:
    """替换正文中的模板变量。"""
    def _replace(m: re.Match) -> str:
        token = m.group(1)
        if token == "CREW_SKILL_DIR":
            # 该值会写进 Markdown/prompt；POSIX 风格在 Windows/POSIX 上都更稳定可读。
            return skill_dir.as_posix()
        if token == "CREW_SESSION_ID" and session_id:
            return session_id
        return m.group(0)
    return _TEMPLATE_VAR_RE.sub(_replace, content)


def _compact_text(text: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
