"""Subagent 注册表：两层加载预设 agent 定义。

对照 crew.agent.skills 的两层目录模式：
  1. 内置：<repo>/crew/agent/subagent/presets/*.md   随仓库发布
  2. 用户：get_crew_home()/agents/*.md                同名覆盖内置

当前只维护内置与用户两级定义。
"""

from __future__ import annotations

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


class SubagentRegistry:
    """加载并提供预设子智能体定义。"""

    def __init__(self) -> None:
        self._defs: dict[str, SubagentDefinition] = {}
        self.reload()

    def reload(self) -> dict[str, SubagentDefinition]:
        builtin = _scan_dir(_PRESETS_DIR, "builtin")
        user = _scan_dir(get_user_agents_dir(), "user")
        self._defs = {**builtin, **user}  # user 覆盖同名 builtin
        log.info("已加载预设子智能体: %s", list(self._defs))
        return self._defs

    def get(self, name: str) -> SubagentDefinition | None:
        return self._defs.get(name)

    def names(self) -> list[str]:
        return list(self._defs.keys())

    def list(self) -> list[SubagentDefinition]:
        return list(self._defs.values())
