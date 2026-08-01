"""Subagent 定义：frontmatter + 正文 → SubagentDefinition。

预设 agent 使用带 YAML frontmatter 的 Markdown 文件描述：

  ---
  name: explore
  description: 只读探索，定位代码与信息，不做任何修改
  toolsets: [default]          # 允许的工具集（不填=继承主 agent 默认）
  tools: [file_read, terminal] # 可选：精确白名单，与 toolsets 取交集
  skills: [code-review]        # 可选：该专业 Agent 固定使用的 Skill
  model: inherit               # inherit=沿用主 agent；或填 config.yaml 里的 model profile id
  max_iterations: 15           # 可选：子 agent 单轮最多工具迭代次数
  ---
  你是一个只读探索子智能体……（正文即 system prompt）

解析复用 crew.agent.skills._parse_frontmatter（同一套 YAML 头规则）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.agent.skills import _parse_frontmatter


@dataclass
class SubagentDefinition:
    """一个预设子智能体的定义。"""

    name: str
    description: str
    system_prompt: str
    toolsets: list[str] | None = None   # None=继承主 agent 默认 toolsets
    tools: list[str] | None = None      # None=不额外限制；否则精确白名单
    skills: list[str] | None = None     # None=不固定 Skill；否则按 slug 收窄并注入索引
    model: str = "inherit"              # "inherit" 或 model profile id
    max_iterations: int | None = None   # None=继承全局 max_iterations
    background: bool = False             # True=默认后台异步执行
    source: str = "builtin"             # "builtin" | "user"


def build_preset_spec(
    definition: SubagentDefinition,
    *,
    model_override: str = "",
) -> dict[str, Any]:
    """把预设定义转换为统一的运行规格。

    ``run_agent`` 与持久化的预设 Agent 会话都必须走这里，避免分别解释
    frontmatter 后出现 prompt、toolsets、tools、skills 或 model 漂移。
    """
    model = str(model_override or "").strip() or definition.model
    return {
        "preset_name": definition.name,
        "system_prompt": definition.system_prompt,
        "toolsets": definition.toolsets,
        "tools": definition.tools,
        "model": model,
        "max_iterations": definition.max_iterations,
        "preset_skills": definition.skills,
        "skills": definition.skills,
    }


def _as_list(value: Any) -> list[str] | None:
    """把 frontmatter 值归一化为字符串列表或 None。

    None/缺失 → None；逗号分隔字符串或 YAML 列表 → list[str]；空 → None。
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = [s.strip() for s in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(s).strip() for s in value]
    else:
        return None
    items = [s for s in items if s]
    return items or None


def parse_definition(path: str | Path, *, source: str = "builtin") -> SubagentDefinition | None:
    """解析单个 agent 定义文件，失败返回 None。"""
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, body = _parse_frontmatter(content)
    name = str(fm.get("name") or p.stem).strip()
    if not name:
        return None

    description = str(fm.get("description") or "").strip()
    if not description:
        # 用正文首个非空行兜底，确保 schema 描述非空
        for line in body.strip().splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                description = line[:80]
                break

    max_iter = fm.get("max_iterations")
    try:
        max_iter = int(max_iter) if max_iter not in (None, "") else None
    except (TypeError, ValueError):
        max_iter = None

    return SubagentDefinition(
        name=name,
        description=description or f"预设子智能体 {name}",
        system_prompt=body.strip(),
        toolsets=_as_list(fm.get("toolsets")),
        tools=_as_list(fm.get("tools")),
        skills=_as_list(fm.get("skills")),
        model=str(fm.get("model") or "inherit").strip() or "inherit",
        max_iterations=max_iter,
        background=bool(fm.get("background", False)),
        source=source,
    )
