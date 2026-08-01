"""crew.evolution 模块：轨迹提取、技能优化与自动生成。

子模块：
  - trajectory_extractor: 从 SessionStore 提取会话轨迹到历史日志
  - log_store: 历史日志持久化存储
  - skill_optimizer: 基于历史日志优化现有 skill
  - skill_generator: 根据轨迹信息自动生成新 skill
  - evolution_manager: 统一编排管理器

基本用法：
    from crew.evolution import EvolutionManager
    from crew.state.session_store import SQLiteSessionStore

    store = SQLiteSessionStore(...)
    manager = EvolutionManager(session_store=store)
    report = manager.run_full_cycle()
"""
from __future__ import annotations

from crew.evolution.models import (
    TrajectoryEntry,
    TrajectoryLog,
    SkillUsageStat,
    OptimizationSuggestion,
    SkillProposal,
)
from crew.evolution.trajectory_extractor import TrajectoryExtractor
from crew.evolution.log_store import EvolutionLogStore
from crew.evolution.skill_optimizer import SkillOptimizer
from crew.evolution.skill_generator import SkillGenerator
from crew.evolution.evolution_manager import EvolutionManager
from crew.evolution.queue import EvolutionQueue

__all__ = [
    "TrajectoryEntry",
    "TrajectoryLog",
    "SkillUsageStat",
    "OptimizationSuggestion",
    "SkillProposal",
    "TrajectoryExtractor",
    "EvolutionLogStore",
    "SkillOptimizer",
    "SkillGenerator",
    "EvolutionManager",
    "EvolutionQueue",
]
