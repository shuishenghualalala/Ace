"""Subagent 子包：主 agent 调用子 agent 执行任务。

两种形态（与 crew/team 完全独立、互不影响）：
  - delegate_task：自定义临时委派（Crew）
  - run_agent：调用预设 frontmatter agent（Crew 风格）

初期禁止嵌套：子 agent 的工具集由 app 侧 tool_filter 剔除 subagent 工具集。
"""

from crew.agent.subagent.definition import SubagentDefinition, parse_definition
from crew.agent.subagent.registry import SubagentRegistry
from crew.agent.subagent.tools import (
    SUBAGENT_TOOLSET,
    ActiveSubagents,
    build_collect_subagent_schema,
    build_delegate_task_schema,
    build_run_agent_schema,
    register_subagent_tools,
)

__all__ = [
    "SubagentDefinition",
    "parse_definition",
    "SubagentRegistry",
    "SUBAGENT_TOOLSET",
    "ActiveSubagents",
    "register_subagent_tools",
    "build_delegate_task_schema",
    "build_run_agent_schema",
    "build_collect_subagent_schema",
]
