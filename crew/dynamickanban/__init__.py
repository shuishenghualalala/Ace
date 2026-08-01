"""Dynamic Kanban 多智能体协同后端。

把 Crew Kanban 的 SQLite 持久化 + DAG 状态机 + CAS 认领作为执行骨架，
以 LLM 动态规划/重构、并行 Worker 和结构化交接作为决策大脑。
"""

from __future__ import annotations

from crew.dynamickanban.manager import DynamicKanbanManager

__all__ = ["DynamicKanbanManager"]
