"""SkillIndex：组合 catalog + scanner + watcher + body cache 的索引单例。

缓存失效双通道：
  - watcher 通道：watchdog 监听 skill roots，文件事件 → catalog.revision+1，
    get 路径零 stat；
  - stat 回退通道：watcher 不可用时退化为带短 TTL 的 mtime key 检测，
    行为与拆分前一致（``_MTIME_KEY_TTL_S`` / ``_scan_mtime_key`` 可在
    包命名空间 monkeypatch）。
进程内安装/卸载经 ``_invalidate_cache()`` 立即失效，两种通道一致生效。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import scanner
from .catalog import SkillCatalog
from .core import (
    SkillActivation,
    SkillEntrypoint,
    SkillPathError,
    _compact_text,
    _configured_skill_filter,
    _is_trusted_local_link,
    _parse_frontmatter,
    _preprocess_content,
    _registered_skill_dir,
    _skill_allowed,
    _slugify,
    _zh_description_from_frontmatter,
    logger,
    patched,
    read_skill_text,
    resolve_skill_path,
)
from .scanner import _iter_skill_files
from .watcher import SkillWatcher

# 扫描/缓存写锁：提示词组装经 asyncio.to_thread 移出事件循环后，
# get_skills()/scan_skills() 与 WS 线程的读取可能并发，统一用这把锁串行化。
_SCAN_LOCK = threading.RLock()

# stat 回退模式下 mtime key 的短 TTL：全目录 stat 在 Windows + 杀软下昂贵，
# TTL 内复用；进程内安装/卸载走 _invalidate_cache() 立即失效。
_MTIME_KEY_TTL_S = 2.0

# watcher 构造工厂，测试可替换为失败桩以覆盖 stat 回退路径
_watcher_factory = SkillWatcher


def _ns():
    return sys.modules.get("crew.agent.skills")


def _catalog_key():
    ns = _ns()
    return getattr(ns, "_cache_key", ()) if ns is not None else ()


def _index_prompt_cache() -> dict:
    ns = _ns()
    return ns._skills_index_cache if ns is not None else {}


def _current_packages() -> dict[str, dict]:
    ns = _ns()
    return getattr(ns, "_packages", {}) if ns is not None else {}


def _current_package_members() -> dict[str, list[str]]:
    ns = _ns()
    return getattr(ns, "_package_members", {}) if ns is not None else {}


def _watched_roots() -> list[Path]:
    return [
        patched("get_builtin_skills_dir")(),
        patched("get_user_skills_dir")(),
        patched("get_optional_skills_dir")(),
        *patched("get_plugin_skill_roots")(),
    ]


class SkillIndex:
    """skill 索引单例：metadata 缓存 + 失效通道 + 正文 body 缓存。"""

    def __init__(self) -> None:
        self._catalog = SkillCatalog()
        self._watcher: SkillWatcher | None = None
        self._watcher_failed = False
        self._body_cache: dict[str, tuple[int, str]] = {}
        self._stat_key_cache: tuple | None = None
        self._stat_key_ts: float = 0.0

    # ── 失效 ─────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """revision+1 并清空所有缓存；允许下次访问重试 watcher。"""
        with _SCAN_LOCK:
            self._catalog.invalidate()
            self._stat_key_cache = None
            self._stat_key_ts = 0.0
            self._watcher_failed = False
            self._body_cache.clear()
            ns = _ns()
            if ns is not None:
                ns._skills_index_cache.clear()
                ns._mtime_key_cache = None

    # ── watcher ──────────────────────────────────────────────────────

    def _ensure_watcher(self) -> bool:
        """watcher 可用返回 True；不可用（未装/启动失败）回退 stat。"""
        if self._watcher is not None:
            return True
        if self._watcher_failed:
            return False
        watcher = _watcher_factory(roots=_watched_roots, on_change=self.invalidate)
        if watcher.start():
            self._watcher = watcher
            return True
        self._watcher_failed = True
        return False

    def stop_watcher(self) -> None:
        """停止 watcher 并允许下次访问重新拉起（测试与进程关停用）。"""
        with _SCAN_LOCK:
            watcher = self._watcher
            self._watcher = None
            self._watcher_failed = False
        if watcher is not None:
            watcher.stop()

    # ── 缓存键 ───────────────────────────────────────────────────────

    def _fallback_ttl(self) -> float:
        ns = _ns()
        if ns is None:
            return _MTIME_KEY_TTL_S
        return float(getattr(ns, "_MTIME_KEY_TTL_S", _MTIME_KEY_TTL_S))

    def _fallback_key(self) -> tuple:
        """stat 回退模式的缓存键，带短 TTL。"""
        now = time.monotonic()
        if self._stat_key_cache is not None and now - self._stat_key_ts < self._fallback_ttl():
            return self._stat_key_cache
        key = patched("_scan_mtime_key")()
        self._stat_key_cache = key
        self._stat_key_ts = now
        ns = _ns()
        if ns is not None:
            ns._mtime_key_cache = key
        return key

    def _current_key(self):
        if self._ensure_watcher():
            self._watcher.sync()
            return self._catalog.revision
        return self._fallback_key()

    # ── 扫描 ─────────────────────────────────────────────────────────

    def scan(self) -> dict[str, dict]:
        """强制重新扫描全部 roots 并发布到 catalog。"""
        with _SCAN_LOCK:
            return self._scan_locked(self._current_key())

    def get(self) -> dict[str, dict]:
        """返回当前 skills 映射，目录有变化时自动重新扫描。"""
        with _SCAN_LOCK:
            key = self._current_key()
            skills, stored_key = self._catalog.stored()
            if not skills or stored_key != key:
                return self._scan_locked(key)
            return skills

    def _scan_locked(self, key) -> dict[str, dict]:
        skills, packages, package_members = scanner.scan_all()
        self._catalog.publish(
            skills=skills,
            key=key,
            packages=packages,
            package_members=package_members,
        )
        # 目录内容已变化：body 缓存一并失效（async 命中路径不做 mtime 校验）
        self._body_cache.clear()
        ns = _ns()
        if ns is not None:
            ns._skills_index_cache.clear()
        return skills

    # ── async 访问器 ─────────────────────────────────────────────────

    async def aget(self) -> dict[str, dict]:
        """async 版 get：watcher 通道 revision 命中纯内存返回；其余进线程池。"""
        if self._watcher is not None:
            self._watcher.sync()
            with _SCAN_LOCK:
                key = self._catalog.revision
                skills, stored_key = self._catalog.stored()
                if skills and stored_key == key:
                    return skills
        return await asyncio.to_thread(self.get)

    async def abody(self, info: dict) -> str:
        """async 版 body：缓存命中纯内存返回；miss 读盘进线程池。

        命中路径不重复 stat 校验，新鲜度由失效通道保证（watcher 事件、
        进程内装卸、stat 回退检测到的重扫都会清空 body 缓存）。
        """
        cache_key = self._body_key(info)
        if cache_key is not None:
            cached = self._body_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        return await asyncio.to_thread(self.body, info)

    # ── body 缓存 ────────────────────────────────────────────────────

    @staticmethod
    def _body_key(info: dict) -> str | None:
        """body 缓存键：SKILL.md 的字面路径（不经文件系统解析，可纯内存计算）。"""
        skill_md_raw = str(info.get("skill_md_path") or "")
        if not skill_md_raw:
            return None
        return os.path.normcase(os.path.abspath(skill_md_raw))

    def body(self, info: dict) -> str:
        """SKILL.md 正文（frontmatter 已剥离），按 (path, mtime_ns) 缓存。

        读取失败（文件消失、containment 校验失败）返回 ""，调用方自行回退。
        """
        skill_dir_raw = str(info.get("skill_dir") or "")
        cache_key = self._body_key(info)
        if not skill_dir_raw or cache_key is None:
            return ""
        skill_dir = Path(skill_dir_raw)
        skill_md = Path(str(info["skill_md_path"]))
        try:
            resolved = resolve_skill_path(skill_md, skill_dir)
            mtime_ns = resolved.stat().st_mtime_ns
        except (OSError, SkillPathError):
            return ""
        cached = self._body_cache.get(cache_key)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        try:
            text = read_skill_text(resolved, skill_dir)
        except (OSError, SkillPathError):
            return ""
        _, body = _parse_frontmatter(text)
        body = body.strip()
        with _SCAN_LOCK:
            if len(self._body_cache) > 512:
                self._body_cache.clear()
            self._body_cache[cache_key] = (mtime_ns, body)
        return body


_INDEX = SkillIndex()


def skill_index() -> SkillIndex:
    """进程级 SkillIndex 单例。"""
    return _INDEX


def scan_skills() -> dict[str, dict]:
    """扫描内置/插件/用户三层目录，返回 {"/slug": info}（info 不含正文）。

    覆盖优先级：user > plugin > builtin（用户 skill 覆盖同名插件/内置 skill）。
    """
    return _INDEX.scan()


def get_skills() -> dict[str, dict]:
    """返回当前 skills 映射，目录有变化时自动重新扫描。"""
    return _INDEX.get()


def get_skill_body(info: dict) -> str:
    """按需加载 skill 正文；扫描结果不再携带 content，统一走 body 缓存。"""
    return _INDEX.body(info)


async def aget_skills() -> dict[str, dict]:
    """get_skills 的 async 变体：revision 命中零开销，miss 扫描进线程池。"""
    return await _INDEX.aget()


async def aget_skill_body(info: dict) -> str:
    """get_skill_body 的 async 变体：缓存命中零开销，miss 读盘进线程池。"""
    return await _INDEX.abody(info)


def _invalidate_cache() -> None:
    """进程内安装/卸载后立即失效；同时重置 stat TTL 缓存。"""
    _INDEX.invalidate()


# ── 调度 ──────────────────────────────────────────────────────────────────


def resolve_skill(command: str) -> Optional[str]:
    """将用户输入的命令字符串解析为 /slug key。

    支持：/coding、coding、/my_skill（下划线转连字符）、中文显示名 / frontmatter name、
    package skill 的完整路径（如 /business-travel/query-flights）以及旧 slug alias。
    后者用于 composer chip 显示中文名时，直接发送 ``/中文名`` 也能命中技能。
    Returns skill key（如 "/coding"），未找到返回 None。
    """
    return _resolve_skill_from(get_skills(), command)


async def aresolve_skill(command: str) -> Optional[str]:
    """resolve_skill 的 async 变体：命中走内存，miss 扫描进线程池。"""
    return _resolve_skill_from(await aget_skills(), command)


def _resolve_skill_from(skills: dict[str, dict], command: str) -> Optional[str]:
    if not command:
        return None
    bare = command.lstrip("/").replace("_", "-")
    key = f"/{bare}"
    if key in skills:
        return key

    # alias 匹配（如旧 slug query-flights → business-travel/query-flights）
    alias_matches: list[str] = []
    for k, info in skills.items():
        aliases = info.get("aliases") or []
        if bare in {a.lstrip("/") for a in aliases}:
            alias_matches.append(k)
    if len(alias_matches) == 1:
        return alias_matches[0]

    # 中文显示名 / frontmatter name 精确匹配（大小写不敏感）。
    # 重名时返回 None，让调用方回退到稳定 slug，避免随机命中第一个 skill。
    norm = command.strip().lstrip("/").lower()
    if norm:
        matches: list[str] = []
        for k, info in skills.items():
            display = str(info.get("display_name") or "").strip().lower()
            name = str(info.get("name") or "").strip().lower()
            if (display and display == norm) or (name and name == norm):
                matches.append(k)
        if len(matches) == 1:
            return matches[0]
    return None


def resolve_skill_any(name: str) -> Optional[dict]:
    """根据任意形式的名称解析技能，返回对应的 info dict。

    匹配优先级（依次尝试，命中即返回）：
    1. canonical slug：去掉前导 /、下划线转连字符
    2. alias：旧 slug 或别名
    3. frontmatter name 精确匹配：对比 info["name"]
    4. 目录名匹配：对比 Path(info["skill_dir"]).name

    适用场景：agent 拿 frontmatter name 调 skill_view 时，目录名与 frontmatter name 不一致
    （如目录 slides/、frontmatter name presentation-assistant），此函数均可命中。

    Returns 匹配到的 info dict，未找到返回 None。
    """
    return _resolve_skill_any_from(get_skills(), name)


async def aresolve_skill_any(name: str) -> Optional[dict]:
    """resolve_skill_any 的 async 变体：命中走内存，miss 扫描进线程池。"""
    return _resolve_skill_any_from(await aget_skills(), name)


def _resolve_skill_any_from(skills: dict[str, dict], name: str) -> Optional[dict]:
    if not name:
        return None

    # 优先级 1：canonical slug 匹配（去掉前导 /，下划线转连字符）
    bare = name.lstrip("/").replace("_", "-")
    key = f"/{bare}"
    if key in skills:
        return skills[key]

    # 优先级 2：alias 匹配
    for info in skills.values():
        aliases = info.get("aliases") or []
        if bare in {a.lstrip("/") for a in aliases}:
            return info

    # 优先级 3：frontmatter name 精确匹配
    for info in skills.values():
        if info["name"] == name:
            return info

    # 优先级 4：目录名匹配
    for info in skills.values():
        if Path(info["skill_dir"]).name == name:
            return info

    return None


def build_skill_message(
    cmd_key: str,
    user_instruction: str = "",
    session_id: str | None = None,
) -> Optional[str]:
    """构建 skill 激活消息（注入到对话的 user message）。

    Returns 格式化的消息字符串，skill 不存在则返回 None。
    """
    info = get_skills().get(cmd_key)
    if not info:
        return None
    return _skill_message_from(info, get_skill_body(info), user_instruction, session_id)


async def abuild_skill_message(
    cmd_key: str,
    user_instruction: str = "",
    session_id: str | None = None,
) -> Optional[str]:
    """build_skill_message 的 async 变体：metadata/body 均走 async 访问器。"""
    info = (await aget_skills()).get(cmd_key)
    if not info:
        return None
    return _skill_message_from(info, await aget_skill_body(info), user_instruction, session_id)


def _skill_message_from(
    info: dict,
    body: str,
    user_instruction: str,
    session_id: str | None,
) -> str:
    skill_name = info["name"]
    skill_dir = Path(info["skill_dir"])
    content = _preprocess_content(body, skill_dir, session_id)

    parts = [
        f'[IMPORTANT: 用户激活了 "{skill_name}" skill，请遵循以下指令。]',
        "",
        content,
    ]

    # 注入 skill 目录路径（方便 agent 引用 scripts/ 等子目录）
    parts += ["", f"[Skill 目录: {skill_dir}]"]

    if user_instruction.strip():
        parts += ["", f"用户补充指令：{user_instruction.strip()}"]

    return "\n".join(parts)


def build_skill_activation(
    cmd_key: str,
    user_instruction: str = "",
    session_id: str | None = None,
) -> SkillActivation | None:
    """Build the external-runtime snapshot from the same resolved Skill truth."""
    info = get_skills().get(cmd_key)
    instruction = build_skill_message(cmd_key, user_instruction, session_id)
    return _activation_from(cmd_key, info, instruction)


async def abuild_skill_activation(
    cmd_key: str,
    user_instruction: str = "",
    session_id: str | None = None,
) -> SkillActivation | None:
    """build_skill_activation 的 async 变体。"""
    info = (await aget_skills()).get(cmd_key)
    instruction = await abuild_skill_message(cmd_key, user_instruction, session_id)
    return _activation_from(cmd_key, info, instruction)


def _activation_from(
    cmd_key: str,
    info: dict | None,
    instruction: str | None,
) -> SkillActivation | None:
    if info is None or instruction is None:
        return None
    entrypoints = tuple(
        SkillEntrypoint.from_dict(item)
        for item in (info.get("entrypoints") or [])
        if isinstance(item, dict)
    )
    return SkillActivation(
        skill_id=str(info.get("slug") or cmd_key.lstrip("/")).strip(),
        name=str(info.get("display_name") or info.get("name") or cmd_key).strip(),
        instruction=instruction,
        skill_root=str(info.get("skill_dir") or "").strip(),
        required_tools=tuple(
            str(item).strip()
            for item in (info.get("required_tools") or [])
            if str(item).strip()
        ),
        required_env=tuple(
            str(item).strip()
            for item in (info.get("required_env") or [])
            if str(item).strip()
        ),
        entrypoints=entrypoints,
    )


def skill_activations_from_params(params: dict[str, Any] | None) -> tuple[SkillActivation, ...]:
    """Safely restore the current-turn activation snapshot from Envelope params."""
    activations: list[SkillActivation] = []
    for raw in (dict(params or {}).get("active_skills") or []):
        if not isinstance(raw, dict):
            continue
        activation = SkillActivation.from_dict(raw)
        if activation.skill_id and activation.skill_root and activation.instruction:
            activations.append(activation)
    return tuple(activations)


def trusted_skill_roots_from_params(params: dict[str, Any] | None) -> tuple[Path, ...]:
    """Revalidate explicitly activated Skill roots before sandbox exposure."""
    roots: list[Path] = []
    for activation in skill_activations_from_params(params):
        info = resolve_skill_any(activation.skill_id)
        if info is None:
            raise ValueError(f"当前 Skill 已不存在：{activation.skill_id}")
        root = _registered_skill_dir(Path(str(info.get("skill_dir") or "")))
        if root != Path(activation.skill_root).expanduser().resolve():
            raise ValueError(f"Skill 在当前执行期间发生变化：{activation.skill_id}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def resolve_skill_activation_entrypoint(
    activation: SkillActivation,
    entrypoint_id: str,
) -> tuple[Path, SkillEntrypoint]:
    """Revalidate a frozen activation against the current authoritative Skill."""
    info = resolve_skill_any(activation.skill_id)
    if info is None:
        raise ValueError(f"当前 Skill 已不存在：{activation.skill_id}")
    skill_root = _registered_skill_dir(Path(str(info.get("skill_dir") or "")))
    if skill_root != Path(activation.skill_root).expanduser().resolve():
        raise ValueError(f"Skill 在当前执行期间发生变化：{activation.skill_id}")

    declared = {
        str(item.get("id") or ""): SkillEntrypoint.from_dict(item)
        for item in (info.get("entrypoints") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    entrypoint = declared.get(str(entrypoint_id or "").strip())
    if entrypoint is None:
        raise ValueError(f"Skill 未声明执行入口：{entrypoint_id or '<empty>'}")
    target = resolve_skill_path(skill_root / entrypoint.path, skill_root)
    if not target.is_file():
        raise ValueError(f"Skill 执行入口不存在：{entrypoint.path}")
    return target, entrypoint


# ── 用于 system prompt 的 skills 索引 ───────────────────────────────────


def build_skills_index_prompt(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> str:
    """构建 compact skills/package 索引，注入到 system prompt context 层。

    采用 progressive disclosure：
    - 默认只暴露 package 层与独立 skills；
    - 通过 skill_package_open 展开 package 后，才暴露其内部 skills；
    - 完整 SKILL.md 必须通过 skill_view(name) 按需加载。
    """
    return _skills_index_prompt(get_skills(), enabled, disabled)


async def abuild_skills_index_prompt(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> str:
    """build_skills_index_prompt 的 async 变体：metadata 经 aget_skills 获取。"""
    return _skills_index_prompt(await aget_skills(), enabled, disabled)


def _skills_index_prompt(
    skills: dict[str, dict],
    enabled: list[str] | None,
    disabled: list[str] | None,
) -> str:
    if not skills and not _current_packages():
        return ""

    skill_filter = _configured_skill_filter()
    enabled = enabled if enabled is not None else skill_filter["enabled"]
    disabled = disabled if disabled is not None else skill_filter["disabled"]

    # 读取当前已激活的 packages（统一规范化为带前导 / 的 slug）
    active_packages: set[str] = set()
    try:
        from crew.core.runctx import current_active_skill_packages

        raw = current_active_skill_packages.get()
        active_packages = {f"/{s.lstrip('/')}" for s in (raw or set())}
    except Exception:
        pass

    cache_key = (
        _catalog_key(),
        tuple(enabled or ()),
        tuple(disabled or ()),
        frozenset(active_packages),
    )
    prompt_cache = _index_prompt_cache()
    cached = prompt_cache.get(cache_key)
    if cached is not None:
        return cached

    # 过滤允许的 skills
    allowed_skills: dict[str, dict] = {}
    for key, info in skills.items():
        if not _skill_allowed(
            info["slug"], enabled, disabled, info.get("aliases")
        ):
            continue
        allowed_skills[key] = info
    # 按 package 聚合
    standalone_entries: list[tuple[str, str]] = []
    packages_to_show: dict[str, dict] = {}
    expanded_package_entries: dict[str, list[tuple[str, str]]] = {}

    for key, info in sorted(allowed_skills.items()):
        pkg = info.get("package") or ""
        if pkg:
            pkg_key = f"/{pkg.lstrip('/')}"
            packages_to_show[pkg_key] = _current_packages().get(pkg_key)
            if pkg_key in active_packages:
                desc = _compact_text(
                    str(info.get("description_zh") or info.get("description") or ""), 220
                )
                expanded_package_entries.setdefault(pkg_key, []).append((key, desc))
        else:
            desc = _compact_text(
                str(info.get("description_zh") or info.get("description") or ""), 220
            )
            standalone_entries.append((key, desc))

    lines: list[str] = []

    # Package 部分
    if packages_to_show:
        lines.append("# 可用 Skill Packages")
        lines.append("")
        if not active_packages:
            lines.append(
                "这是 compact package index。需要使用某个 package 中的 skill 时，"
                "先调用 skill_package_open(name) 展开；也可以通过 /package-name 或 /package-name/skill-name 激活。"
            )
        for pkg_slug in sorted(packages_to_show):
            pkg_info = packages_to_show[pkg_slug]
            if pkg_info is None:
                continue
            desc = _compact_text(
                str(pkg_info.get("description_zh") or pkg_info.get("description") or ""), 220
            )
            lines.append(f"- {pkg_slug}: {desc}")
            if pkg_slug in active_packages:
                members = expanded_package_entries.get(pkg_slug, [])
                for member_key, member_desc in members:
                    lines.append(f"  - {member_key}: {member_desc}")
        lines.append("")

    # 独立 skills
    if standalone_entries:
        lines.append("# 其他可用 Skills")
        lines.append("")
        lines.append(
            "这是 compact skill index。需要使用某个 skill 时，先用 skill_view(name) 加载完整说明；"
            "同一 name/file_path 已成功读取且结果仍在上下文时，直接继续使用，不要重复读取；"
            "只有需要其他文件或有明确证据表明内容已变化时才再次调用。也可以通过 "
            "/skill-name [补充指令] 激活。"
        )
        for key, desc in standalone_entries:
            lines.append(f"- {key}: {desc}")

    if not lines:
        return ""

    result = "\n".join(lines)
    with _SCAN_LOCK:
        prompt_cache[cache_key] = result
        if len(prompt_cache) > 16:
            prompt_cache.pop(next(iter(prompt_cache)))
    return result


def build_optional_skills_index_prompt(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> str:
    """扫描 optional-skills/ 构建 compact index，供按需技能补充使用。

    与主索引分离，避免影响默认 agent 的 skill 列表、安装/卸载等主逻辑。
    已存在于 builtin/user skills 中的同名 skill 不再重复加入。
    """
    installed_slugs = set(get_skills().keys())
    entries: list[tuple[str, str]] = []

    optional_root = patched("get_optional_skills_dir")()
    for skill_md in _iter_skill_files(optional_root):
        try:
            safe_skill_dir = resolve_skill_path(skill_md.parent, optional_root)
            content = read_skill_text(skill_md, safe_skill_dir)
            fm, body = _parse_frontmatter(content)
            name = str(fm.get("name") or skill_md.parent.name).strip()
            slug = _slugify(name) or _slugify(skill_md.parent.name)
            if not slug or f"/{slug}" in installed_slugs:
                continue
            if not _skill_allowed(slug, enabled, disabled):
                continue
            description = str(fm.get("description") or "").strip()
            if not description:
                for line in body.strip().splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        description = line[:80]
                        break
            desc = _compact_text(
                str(_zh_description_from_frontmatter(fm, description) or description or ""), 220
            )
            if desc:
                entries.append((f"/{slug}", desc))
        except Exception as exc:
            logger.debug("跳过 optional skill %s: %s", skill_md, exc)

    if not entries:
        return ""

    lines = ["# 可选 Skills", ""]
    for key, desc in sorted(entries):
        lines.append(f"- {key}: {desc}")
    return "\n".join(lines)


async def abuild_optional_skills_index_prompt(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> str:
    """build_optional_skills_index_prompt 的 async 变体：listing 走磁盘，进线程池。"""
    return await asyncio.to_thread(build_optional_skills_index_prompt, enabled, disabled)


def get_skill_packages() -> dict[str, dict]:
    """返回所有已扫描的 skill packages 映射：package slug -> package info。"""
    get_skills()  # 确保已扫描
    return dict(_current_packages())


async def aget_skill_packages() -> dict[str, dict]:
    """get_skill_packages 的 async 变体。"""
    await aget_skills()
    return dict(_current_packages())


def get_package_info(package_slug: str) -> dict | None:
    """根据 slug 返回 package info；不存在返回 None。"""
    get_skills()
    return _current_packages().get(package_slug)


def get_package_members(package_slug: str) -> list[dict]:
    """返回指定 package 内所有 skill info 列表；package 不存在返回空列表。"""
    return _package_members_from(get_skills(), package_slug)


async def aget_package_members(package_slug: str) -> list[dict]:
    """get_package_members 的 async 变体。"""
    return _package_members_from(await aget_skills(), package_slug)


def _package_members_from(skills: dict[str, dict], package_slug: str) -> list[dict]:
    key = f"/{package_slug.lstrip('/')}"
    members: list[dict] = []
    for member_key in _current_package_members().get(key, []):
        info = skills.get(member_key)
        if info is not None:
            members.append(info)
    return members


def resolve_package(name: str) -> dict | None:
    """根据 slug 或 frontmatter name 解析 package。"""
    return _resolve_package_from(get_skill_packages(), name)


async def aresolve_package(name: str) -> dict | None:
    """resolve_package 的 async 变体。"""
    return _resolve_package_from(await aget_skill_packages(), name)


def _resolve_package_from(packages: dict[str, dict], name: str) -> dict | None:
    if not name:
        return None
    bare = name.lstrip("/")
    key = f"/{bare}"
    if key in packages:
        return packages[key]
    for pkg in packages.values():
        if pkg["slug"] == bare or pkg["name"] == name:
            return pkg
    return None


# ── 列表展示（给 API 用） ──────────────────────────────────────────────────


def list_skills(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> list[dict]:
    """返回可展示的 skills 列表（不含完整 content）。"""
    return _list_skills_from(get_skills(), enabled, disabled)


async def alist_skills(
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> list[dict]:
    """list_skills 的 async 变体。"""
    return _list_skills_from(await aget_skills(), enabled, disabled)


def _list_skills_from(
    skills: dict[str, dict],
    enabled: list[str] | None,
    disabled: list[str] | None,
) -> list[dict]:
    skill_filter = _configured_skill_filter()
    enabled = enabled if enabled is not None else skill_filter["enabled"]
    disabled = disabled if disabled is not None else skill_filter["disabled"]
    user_dir = patched("get_user_skills_dir")()
    out: list[dict] = []
    for info in skills.values():
        if not _skill_allowed(
            info["slug"], enabled, disabled, info.get("aliases")
        ):
            continue
        skill_dir = str(info["skill_dir"])
        try:
            resolve_skill_path(Path(skill_dir), user_dir)
            is_user = True
        except SkillPathError:
            is_user = False
        item: dict = {
            "name": info["name"],
            "display_name": info.get("display_name") or info["name"],
            "slug": info["slug"],
            "base_slug": info.get("base_slug") or info["slug"],
            "aliases": list(info.get("aliases") or []),
            "description": info["description"],
            "description_zh": info.get("description_zh") or "",
            "query_examples": list(info.get("query_examples") or []),
            "category": str(info.get("category") or "通用办公").strip(),
            "featured": bool(info.get("featured", False)),
            "source": "user" if is_user else "builtin",
            # 本地共享 Skill 以链接接入 Crew；前端卸载时需明确只移除 Crew 入口，
            # 不会删除 ~/.agents/skills 中的原始 Skill。
            "is_local_shared": is_user and _is_trusted_local_link(Path(skill_dir)),
            "version": info.get("version") or "",
            "state": "installed" if is_user else "builtin",
            "path": skill_dir,
            "package": info.get("package") or "",
            "package_path": info.get("package_path") or "",
        }
        out.append(item)
    return out
