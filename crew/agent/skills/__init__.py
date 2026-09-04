"""Skills 模块：三层 skill 目录的发现、加载与消息构建。

扫描层（scan_skills 扫描，决定对话中可调用）：
  1. 内置 skills：<repo>/crew/skills/      — 随仓库发布，始终激活
  2. 用户 skills：get_crew_home()/skills/  — 用户安装/自定义，可覆盖同名内置
  3. Optional：  <repo>/optional-skills/       — 可安装，安装后进入用户目录

可安装源（仅技能页展示，未安装不可调用）：
  - Optional：<repo>/optional-skills/        - 仓库随附
  - 本地：    ~/.agents/skills/              - 跨 agent 共享（如 npx skills 安装）；
                                             安装时以软链发布到用户目录，源更新自动同步

每个 skill 是一个目录，包含 SKILL.md（YAML frontmatter + Markdown 正文）：

  ---
  name: my-skill
  description: 一句话描述
  ---
  主体指令内容...

支持正文模板变量：
  ${CREW_SKILL_DIR}   → skill 所在目录的绝对路径
  ${CREW_SESSION_ID}  → 当前会话 ID（可选）

子包结构：
  core.py     路径/安全 containment/frontmatter 等基础原语（无缓存状态）
  scanner.py  同步扫描 roots，一层 + package 感知，只产 metadata
  catalog.py  metadata 缓存与 revision 失效计数器
  watcher.py  watchdog 文件监听，失败时由 SkillIndex 回退 stat TTL
  index.py    SkillIndex 单例：缓存键、调度、消息构建、body 缓存
  manage.py   安装/卸载/更新/校验/审计/修复
"""

from __future__ import annotations

# 测试经 skills_mod.shutil 打桩（patch 的是共享 shutil 模块本体）
import shutil  # noqa: F401

# ── 兼容缓存全局量（catalog/索引的存储本体，测试可直接读/重置） ─────────────

_cache: dict[str, dict] = {}
_cache_key: tuple = ()
_skills_index_cache: dict[tuple, str] = {}

# stat 回退模式的 mtime key 短 TTL；_mtime_key_cache 仅作兼容观察面
_MTIME_KEY_TTL_S = 2.0
_mtime_key_cache: tuple | None = None

# Package 缓存：package slug → package info；members：package slug → [member full_slug]
_packages: dict[str, dict] = {}
_package_members: dict[str, list[str]] = {}

# 全局 skill 过滤器（由 app.py 在启动时根据 access_control 配置）
_skill_filter: dict[str, list[str] | None] = {"enabled": None, "disabled": None}

# 已加载插件声明的 skill roots 提供方（由 app.py 注入 PluginManager.plugin_skill_roots）
_plugin_skill_roots_provider = None

from .core import (  # noqa: E402
    SKILL_CATEGORY_NAMES,
    SkillActivation,
    SkillEntrypoint,
    SkillPathError,
    _EXCLUDED_DIRS,
    _REPAIRABLE_TEXT_SUFFIXES,
    _REPO_ROOT,
    _compact_text,
    _configured_skill_filter,
    _containment_finding,
    _contains_cjk,
    _display_name_from_frontmatter,
    _extract_query_examples,
    _featured_from_frontmatter,
    _first_text,
    _format_skill_markdown,
    _is_link_or_reparse,
    _is_trusted_local_link,
    _is_within_trusted_link_target,
    _lexical_path_key,
    _metadata_dict,
    _normalize_skill_category,
    _package_description_from_frontmatter,
    _parse_frontmatter,
    _preprocess_content,
    _registered_skill_dir,
    _resolved_path_key,
    _skill_allowed,
    _skill_category_from_frontmatter,
    _skill_runtime_metadata,
    _slugify,
    _trusted_link_target_roots,
    _validate_skill_tree,
    _walk_contained,
    _zh_description_from_frontmatter,
    configure_plugin_skill_roots,
    configure_skill_filter,
    get_builtin_skills_dir,
    get_local_skills_dir,
    get_optional_skills_dir,
    get_plugin_skill_roots,
    get_user_skills_dir,
    patched,
    read_skill_text,
    resolve_skill_path,
)
from .scanner import (  # noqa: E402
    _is_excluded_dir,
    _iter_package_skills,
    _iter_skill_files,
    _parse_package_md,
    scan_all,
    scan_dir,
)
from .scanner import stat_scan_key as _scan_mtime_key  # noqa: E402,F401
from .catalog import SkillCatalog  # noqa: E402
from .watcher import SkillWatcher  # noqa: E402
from .index import (  # noqa: E402
    _SCAN_LOCK,
    SkillIndex,
    _invalidate_cache,
    abuild_optional_skills_index_prompt,
    abuild_skill_activation,
    abuild_skill_message,
    abuild_skills_index_prompt,
    aget_package_members,
    aget_skill_body,
    aget_skill_packages,
    aget_skills,
    alist_skills,
    aresolve_package,
    aresolve_skill,
    aresolve_skill_any,
    build_optional_skills_index_prompt,
    build_skill_activation,
    build_skill_message,
    build_skills_index_prompt,
    get_package_info,
    get_package_members,
    get_skill_body,
    get_skill_packages,
    get_skills,
    list_skills,
    resolve_package,
    resolve_skill,
    resolve_skill_activation_entrypoint,
    resolve_skill_any,
    scan_skills,
    skill_activations_from_params,
    skill_index,
    trusted_skill_roots_from_params,
)
from .manage import (  # noqa: E402
    _GENERATED_SKILL_MD_MAX_BYTES,
    _SKILL_AUDIT_LOCK,
    _SKILL_MUTATION_LOCK,
    _append_failed_global_skill_audit,
    _append_global_skill_audit,
    _audit_skill_metadata,
    _audit_skill_paths,
    _declared_skill_version,
    _existing_chinese_metadata,
    _hide_published_skill,
    _install_local_skill_link,
    _install_skill_tree,
    _is_metadata_finding,
    _is_record_replay_skill,
    _iter_skill_text_files,
    _iter_skill_text_files_with_findings,
    _looks_like_real_secret,
    _make_unified_patch,
    _parse_listing_skill,
    _parse_metadata_json_response,
    _rename_directory_noreplace,
    _replace_skill_tree,
    _retire_published_skill_if_same,
    _safe_audit_value,
    _set_metadata,
    _skill_generation_context,
    _skill_operator,
    _skill_tree_fingerprint,
    _validate_generated_metadata,
    _validate_record_replay_markdown,
    _write_text_via_patch,
    audit_skills,
    generate_skill_metadata_with_model,
    install_skill,
    install_skill_from_dir,
    list_local_skills,
    list_optional_skills,
    repair_skills,
    uninstall_skill,
    update_skill_markdown,
    validate_generated_skill,
)


__all__ = (
    "abuild_optional_skills_index_prompt",
    "abuild_skill_activation",
    "abuild_skill_message",
    "abuild_skills_index_prompt",
    "aget_package_members",
    "aget_skill_body",
    "aget_skill_packages",
    "aget_skills",
    "alist_skills",
    "aresolve_package",
    "aresolve_skill",
    "aresolve_skill_any",
    "SKILL_CATEGORY_NAMES",
    "SkillActivation",
    "SkillCatalog",
    "SkillEntrypoint",
    "SkillIndex",
    "SkillPathError",
    "SkillWatcher",
    "_EXCLUDED_DIRS",
    "_GENERATED_SKILL_MD_MAX_BYTES",
    "_REPAIRABLE_TEXT_SUFFIXES",
    "_REPO_ROOT",
    "_SCAN_LOCK",
    "_SKILL_AUDIT_LOCK",
    "_SKILL_MUTATION_LOCK",
    "_append_failed_global_skill_audit",
    "_append_global_skill_audit",
    "_audit_skill_metadata",
    "_audit_skill_paths",
    "_compact_text",
    "_configured_skill_filter",
    "_containment_finding",
    "_contains_cjk",
    "_declared_skill_version",
    "_display_name_from_frontmatter",
    "_existing_chinese_metadata",
    "_extract_query_examples",
    "_featured_from_frontmatter",
    "_first_text",
    "_format_skill_markdown",
    "_hide_published_skill",
    "_install_local_skill_link",
    "_install_skill_tree",
    "_invalidate_cache",
    "_is_excluded_dir",
    "_is_link_or_reparse",
    "_is_metadata_finding",
    "_is_record_replay_skill",
    "_is_trusted_local_link",
    "_is_within_trusted_link_target",
    "_iter_package_skills",
    "_iter_skill_files",
    "_iter_skill_text_files",
    "_iter_skill_text_files_with_findings",
    "_lexical_path_key",
    "_looks_like_real_secret",
    "_make_unified_patch",
    "_metadata_dict",
    "_normalize_skill_category",
    "_package_description_from_frontmatter",
    "_parse_frontmatter",
    "_parse_listing_skill",
    "_parse_metadata_json_response",
    "_parse_package_md",
    "_preprocess_content",
    "_registered_skill_dir",
    "_rename_directory_noreplace",
    "_replace_skill_tree",
    "_resolved_path_key",
    "_retire_published_skill_if_same",
    "_safe_audit_value",
    "_scan_mtime_key",
    "_set_metadata",
    "_skill_allowed",
    "_skill_category_from_frontmatter",
    "_skill_generation_context",
    "_skill_operator",
    "_skill_runtime_metadata",
    "_skill_tree_fingerprint",
    "_slugify",
    "_trusted_link_target_roots",
    "_validate_generated_metadata",
    "_validate_record_replay_markdown",
    "_validate_skill_tree",
    "_walk_contained",
    "_write_text_via_patch",
    "_zh_description_from_frontmatter",
    "audit_skills",
    "build_optional_skills_index_prompt",
    "build_skill_activation",
    "build_skill_message",
    "build_skills_index_prompt",
    "configure_plugin_skill_roots",
    "configure_skill_filter",
    "generate_skill_metadata_with_model",
    "get_builtin_skills_dir",
    "get_local_skills_dir",
    "get_optional_skills_dir",
    "get_package_info",
    "get_package_members",
    "get_plugin_skill_roots",
    "get_skill_body",
    "get_skill_packages",
    "get_skills",
    "get_user_skills_dir",
    "install_skill",
    "install_skill_from_dir",
    "list_local_skills",
    "list_optional_skills",
    "list_skills",
    "patched",
    "read_skill_text",
    "repair_skills",
    "resolve_package",
    "resolve_skill",
    "resolve_skill_activation_entrypoint",
    "resolve_skill_any",
    "resolve_skill_path",
    "scan_all",
    "scan_dir",
    "scan_skills",
    "skill_activations_from_params",
    "skill_index",
    "trusted_skill_roots_from_params",
    "uninstall_skill",
    "update_skill_markdown",
    "validate_generated_skill",
)
