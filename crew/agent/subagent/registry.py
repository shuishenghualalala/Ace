"""Subagent 注册表：两层加载通用预设 agent 定义来源。

对照 crew.agent.skills 的两层目录模式：
  1. 内置：<repo>/crew/agent/subagent/presets/*.md   随仓库发布
  2. 用户：get_crew_home()/agents/*.md                同名覆盖内置

产品 Feature 的专属预设由其 Bundle 从本地资产注册到 Feature Runtime；
本类只维护内置与用户两级通用来源。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from crew.agent.subagent.definition import SubagentDefinition, parse_definition
from crew.state.logging import get_logger

log = get_logger("subagent")

_PRESETS_DIR = Path(__file__).resolve().parent / "presets"


def get_user_agents_dir() -> Path:
    """用户 agent 定义目录：get_crew_home()/agents/。"""
    from crew.state.home import get_crew_home

    return get_crew_home() / "agents"


def _scan_dir(agents_dir: Path, source: str) -> dict[str, SubagentDefinition]:
    result: dict[str, SubagentDefinition] = {}
    if not agents_dir.is_dir():
        return result
    for md in sorted(agents_dir.glob("*.md")):
        definition = parse_definition(md, source=source)
        if definition is None:
            log.debug("跳过无效 agent 定义: %s", md)
            continue
        result[definition.name] = definition
    return result


def _resolve_inheritance(
    definitions: dict[str, SubagentDefinition],
) -> dict[str, SubagentDefinition]:
    """Resolve ``extends`` once at reload time, with cycle-safe fallback."""

    resolved: dict[str, SubagentDefinition] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> SubagentDefinition:
        if name in resolved:
            return resolved[name]
        definition = definitions[name]
        parent_name = definition.extends
        if not parent_name:
            resolved[name] = definition
            return definition
        if name in visiting or parent_name not in definitions or parent_name == name:
            log.warning("预设子智能体继承关系无效，忽略父级: %s -> %s", name, parent_name)
            fallback = replace(definition, extends=None)
            resolved[name] = fallback
            return fallback
        visiting.add(name)
        parent = resolve(parent_name)
        visiting.discard(name)
        merged = SubagentDefinition(
            name=definition.name,
            description=definition.description or parent.description,
            system_prompt=definition.system_prompt or parent.system_prompt,
            toolsets=definition.toolsets if definition.toolsets is not None else parent.toolsets,
            tools=definition.tools if definition.tools is not None else parent.tools,
            skills=definition.skills if definition.skills is not None else parent.skills,
            model=definition.model if definition.model != "inherit" else parent.model,
            max_iterations=(
                definition.max_iterations
                if definition.max_iterations is not None
                else parent.max_iterations
            ),
            background=definition.background or parent.background,
            source=definition.source,
            agent_id=definition.agent_id or f"subagent:{definition.name}",
            toolset_additions=(
                definition.toolset_additions
                if definition.toolset_additions is not None
                else parent.toolset_additions
            ),
            context_tags=(
                definition.context_tags
                if definition.context_tags is not None
                else parent.context_tags
            ),
            disclosure_mode=(
                definition.disclosure_mode
                if definition.disclosure_mode != "progressive"
                else parent.disclosure_mode
            ),
            reserved_toolsets=(
                definition.reserved_toolsets
                if definition.reserved_toolsets is not None
                else parent.reserved_toolsets
            ),
            reserved_skills=(
                definition.reserved_skills
                if definition.reserved_skills is not None
                else parent.reserved_skills
            ),
            provider=(
                definition.provider
                if definition.provider != "inprocess-spawn"
                else parent.provider
            ),
            extends=None,
            read_only=definition.read_only or parent.read_only,
            delegatable_presets=(
                definition.delegatable_presets
                if definition.delegatable_presets is not None
                else parent.delegatable_presets
            ),
            summary_min_chars=(
                definition.summary_min_chars
                if definition.summary_min_chars is not None
                else parent.summary_min_chars
            ),
            summary_retries=(
                definition.summary_retries
                if definition.summary_retries is not None
                else parent.summary_retries
            ),
        )
        resolved[name] = merged
        return merged

    for name in definitions:
        resolve(name)
    return resolved


class SubagentRegistry:
    """加载并提供预设子智能体定义。"""

    def __init__(self) -> None:
        self._defs: dict[str, SubagentDefinition] = {}
        self._claimed: dict[str, SubagentDefinition] = {}
        self.reload()

    def reload(self) -> dict[str, SubagentDefinition]:
        builtin = _scan_dir(_PRESETS_DIR, "builtin")
        user = _scan_dir(get_user_agents_dir(), "user")
        accepted_user: dict[str, SubagentDefinition] = {}
        for name, definition in user.items():
            if name in builtin and not definition.override:
                log.warning("用户 preset %s 未声明 override: true，保留内置定义", name)
                continue
            accepted_user[name] = definition
        self._defs = _resolve_inheritance({**builtin, **accepted_user})
        # Feature-owned definitions stay out of the generic catalog across a
        # reload. If a newer user override exists, it becomes the next claimed
        # value; otherwise the previous claimed value remains stable.
        for name in tuple(self._claimed):
            replacement = self._defs.pop(name, None)
            if replacement is not None:
                self._claimed[name] = replacement
        log.info("已加载预设子智能体: %s", list(self._defs))
        return self._defs

    def get(self, name: str) -> SubagentDefinition | None:
        return self._defs.get(name)

    def names(self) -> list[str]:
        return list(self._defs.keys())

    def list(self) -> list[SubagentDefinition]:
        return list(self._defs.values())

    def claim(self, name: str) -> SubagentDefinition | None:
        """Remove one definition from the generic catalog for a Feature owner."""
        normalized = str(name or "").strip()
        if normalized in self._claimed:
            return self._claimed[normalized]
        definition = self._defs.pop(normalized, None)
        if definition is not None:
            self._claimed[normalized] = definition
        return definition
