"""进化管理器：统一编排轨迹提取、技能优化与自动生成。

EvolutionManager 是 crew.evolution 模块的入口，将三个子组件串联：

    SessionStore
        │
        ▼
    TrajectoryExtractor ──► EvolutionLogStore (持久化)
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              SkillOptimizer   SkillGenerator   查询/统计

典型用法：
    from crew.evolution import EvolutionManager
    from crew.state.session_store import SQLiteSessionStore

    store = SQLiteSessionStore(...)
    manager = EvolutionManager(session_store=store)

    # 1. 提取所有会话轨迹
    manager.extract_trajectories()

    # 2. 优化现有 skill
    report = manager.optimize_all(dry_run=True)

    # 3. 生成新 skill 提案
    proposals = manager.generate_proposals()

    # 一键全流程
    report = manager.run_full_cycle()
"""
from __future__ import annotations

import logging
from typing import Any

from crew.core.interfaces import SessionStore
from crew.evolution.log_store import EvolutionLogStore
from crew.evolution.models import (
    OptimizationSuggestion,
    SkillProposal,
    SkillUsageStat,
    TrajectoryLog,
)
from crew.evolution.skill_generator import SkillGenerator
from crew.evolution.skill_optimizer import SkillOptimizer
from crew.evolution.trajectory_extractor import TrajectoryExtractor

logger = logging.getLogger(__name__)


class EvolutionManager:
    """进化管理器：统一编排轨迹提取、技能优化与自动生成。

    通过依赖注入接收 SessionStore，内部自动创建 LogStore / Optimizer / Generator，
    也可由调用方显式传入以实现自定义存储路径或 LLM 增强。
    """

    def __init__(
        self,
        session_store: SessionStore,
        log_store: EvolutionLogStore | None = None,
        llm_provider: Any | None = None,
    ):
        self._session_store = session_store
        self._log_store = log_store or EvolutionLogStore()
        self._extractor = TrajectoryExtractor(session_store, llm_provider)
        self._optimizer = SkillOptimizer(self._log_store)
        self._generator = SkillGenerator(self._log_store, llm_provider)

    # ── 属性 ──────────────────────────────────────────────────────────────

    @property
    def log_store(self) -> EvolutionLogStore:
        return self._log_store

    @property
    def extractor(self) -> TrajectoryExtractor:
        return self._extractor

    @property
    def optimizer(self) -> SkillOptimizer:
        return self._optimizer

    @property
    def generator(self) -> SkillGenerator:
        return self._generator

    # ── 轨迹提取 ──────────────────────────────────────────────────────────

    def extract_trajectories(
        self,
        owner_account_id: str = "",
        workspace_id: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """提取所有会话轨迹并保存到历史日志，返回保存数量。"""
        logs = self._extractor.extract_all(
            owner_account_id=owner_account_id,
            workspace_id=workspace_id,
            include_archived=include_archived,
        )
        if not logs:
            logger.info("没有可提取的会话轨迹")
            return 0

        ids = self._log_store.save_batch(logs)
        logger.info("提取并保存 %d 条轨迹日志", len(ids))
        return len(ids)

    def extract_session(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> str | None:
        """提取单个会话的轨迹并保存，返回 log_id。"""
        log = self._extractor.extract(session_id, owner_account_id)
        if not log:
            return None
        return self._log_store.save(log)

    # ── 技能优化 ──────────────────────────────────────────────────────────

    def get_optimization_suggestions(
        self,
        skill_slug: str | None = None,
        current_session_id: str | None = None,
    ) -> dict[str, list[OptimizationSuggestion]]:
        """获取优化建议。

        skill_slug 为空时分析所有有使用记录的 skill。
        current_session_id 传入时以当前会话数据为主、历史为辅。
        """
        if skill_slug:
            suggestions = self._optimizer.analyze(skill_slug, current_session_id=current_session_id)
            return {skill_slug: suggestions} if suggestions else {}
        return self._optimizer.analyze_all(current_session_id=current_session_id)

    def optimize_skill(
        self,
        skill_slug: str,
        dry_run: bool = False,
        current_session_id: str | None = None,
    ) -> list[str]:
        """优化单个 skill，返回 patch 列表。"""
        suggestions = self._optimizer.analyze(skill_slug, current_session_id=current_session_id)
        if not suggestions:
            return []
        patches: list[str] = []
        for sug in suggestions:
            patch = self._optimizer.apply(sug, dry_run=dry_run)
            if patch:
                patches.append(patch)
        return patches

    def optimize_all(
        self,
        dry_run: bool = False,
        current_session_id: str | None = None,
    ) -> dict[str, list[str]]:
        """优化所有有使用记录的 skill，返回 {slug: [patch, ...]}。

        current_session_id 传入时以当前会话数据为主、历史为辅。
        """
        all_suggestions = self._optimizer.analyze_all(current_session_id=current_session_id)
        if not all_suggestions:
            logger.info("没有需要优化的 skill")
            return {}
        return self._optimizer.apply_all(all_suggestions, dry_run=dry_run)

    # ── 技能生成 ──────────────────────────────────────────────────────────

    def generate_proposals(
        self,
        min_queries: int = 2,
        max_proposals: int = 5,
        current_session_id: str | None = None,
        evolution_suggestions: list[OptimizationSuggestion] | None = None,
        conversation_id: str = "",
        session_id: str = "",
    ) -> list[SkillProposal]:
        """从历史轨迹中生成新 skill 提案。

        current_session_id 传入时以当前会话查询为主、历史为辅。
        evolution_suggestions 传入时，与上一轮关联的聚类（独立性 < 0.7）
        会生成进化建议追加到此列表，而非创建新提案。
        conversation_id 和 session_id 用于 evolution_log 文件命名。
        """
        return self._generator.propose(
            min_queries=min_queries,
            max_proposals=max_proposals,
            current_session_id=current_session_id,
            evolution_suggestions=evolution_suggestions,
            conversation_id=conversation_id,
            session_id=session_id,
        )

    def generate_from_session(self, session_id: str) -> SkillProposal | None:
        """从单个会话轨迹生成 skill 提案。"""
        return self._generator.propose_from_session(session_id)

    def create_skill(
        self,
        proposal: SkillProposal,
        conversation_id: str = "",
        session_id: str = "",
    ) -> str | None:
        """根据提案创建新 SKILL.md，返回创建路径。

        conversation_id 和 session_id 用于 evolution_log 文件命名。
        """
        return self._generator.create(
            proposal,
            conversation_id=conversation_id,
            session_id=session_id,
        )

    def create_skill_from_session(self, session_id: str) -> str | None:
        """从单个会话直接生成并创建 skill。"""
        return self._generator.create_from_session(session_id)

    # ── 全流程 ────────────────────────────────────────────────────────────

    def run_full_cycle(
        self,
        owner_account_id: str = "",
        workspace_id: str | None = None,
        session_id: str | None = None,
        dry_run_optimize: bool = True,
        auto_create: bool = False,
        min_queries: int = 2,
        max_proposals: int = 5,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """执行完整的进化周期：提取 → 优化 → 生成。

        Args:
            owner_account_id: 会话归属账号
            workspace_id: 工作空间过滤
            session_id: 当前会话 ID，传入时只提取当前会话轨迹，
                        优化和生成以当前交互为主、历史为辅
            dry_run_optimize: 优化阶段是否仅预览不写入
            auto_create: 是否自动创建生成的新 skill 文件
            min_queries: 生成提案的最小查询次数
            max_proposals: 最多生成提案数
            conversation_id: 主会话 ID，用于 evolution_log 文件命名

        Returns:
            包含各阶段结果的报告字典
        """
        report: dict[str, Any] = {
            "trajectories_extracted": 0,
            "optimization_patches": {},
            "proposals": [],
            "created_skills": [],
            "evolution_patches": [],
            "errors": [],
        }

        # 跨轮次进化建议收集列表
        evolution_suggestions: list[OptimizationSuggestion] = []

        # 1. 提取轨迹 — 只提取当前会话（如果 session_id 提供）
        try:
            if session_id:
                log_id = self.extract_session(session_id, owner_account_id)
                report["trajectories_extracted"] = 1 if log_id else 0
            else:
                count = self.extract_trajectories(
                    owner_account_id=owner_account_id,
                    workspace_id=workspace_id,
                )
                report["trajectories_extracted"] = count
        except Exception as exc:
            logger.exception("轨迹提取失败")
            report["errors"].append(f"extract: {exc}")

        # 2. 优化现有 skill — 以当前会话为主
        try:
            patches = self.optimize_all(
                dry_run=dry_run_optimize,
                current_session_id=session_id,
            )
            report["optimization_patches"] = patches
        except Exception as exc:
            logger.exception("技能优化失败")
            report["errors"].append(f"optimize: {exc}")

        # 3. 生成新 skill 提案 — 以当前会话为主
        #    同时收集跨轮次进化建议（独立性 < 0.7 的聚类不创建新 skill 而是进化已有 skill）
        try:
            proposals = self.generate_proposals(
                min_queries=min_queries,
                max_proposals=max_proposals,
                current_session_id=session_id,
                evolution_suggestions=evolution_suggestions,
                conversation_id=conversation_id,
                session_id=session_id or "",
            )
            report["proposals"] = [p.to_dict() for p in proposals]

            if auto_create:
                for proposal in proposals:
                    path = self.create_skill(
                        proposal,
                        conversation_id=conversation_id,
                        session_id=session_id or "",
                    )
                    if path:
                        report["created_skills"].append(path)

            # 应用跨轮次进化建议
            if evolution_suggestions:
                for sug in evolution_suggestions:
                    try:
                        patch = self._optimizer.apply(sug, dry_run=dry_run_optimize)
                        if patch:
                            report["evolution_patches"].append(patch)
                            logger.info(
                                "进化 skill %s: %s",
                                sug.skill_slug,
                                sug.reason[:80],
                            )
                    except Exception as exc:
                        logger.warning("应用进化建议失败 (%s): %s", sug.skill_slug, exc)
                        report["errors"].append(f"evolve {sug.skill_slug}: {exc}")

        except Exception as exc:
            logger.exception("技能生成失败")
            report["errors"].append(f"generate: {exc}")

        logger.info(
            "进化周期完成: 提取 %d 条轨迹, %d 个优化 patch, %d 个提案, "
            "%d 个已创建, %d 个进化 patch",
            report["trajectories_extracted"],
            sum(len(v) for v in report["optimization_patches"].values()),
            len(report["proposals"]),
            len(report["created_skills"]),
            len(report["evolution_patches"]),
        )
        return report

    # ── 查询接口 ──────────────────────────────────────────────────────────

    def list_logs(
        self,
        *,
        skill_slug: str | None = None,
        tool_name: str | None = None,
        session_id: str | None = None,
        has_errors: bool | None = None,
        limit: int = 0,
    ) -> list[TrajectoryLog]:
        """列出轨迹日志，支持多维度过滤。"""
        return self._log_store.list_logs(
            skill_slug=skill_slug,
            tool_name=tool_name,
            session_id=session_id,
            has_errors=has_errors,
            limit=limit,
        )

    def get_skill_stats(
        self, current_session_id: str | None = None
    ) -> dict[str, SkillUsageStat]:
        """获取所有 skill 的使用统计。

        current_session_id 传入时额外统计当前会话独立数据。
        """
        return self._log_store.get_skill_stats(current_session_id=current_session_id)

    def get_tool_stats(self) -> dict[str, dict[str, Any]]:
        """获取所有工具的使用统计。"""
        return self._log_store.get_tool_stats()

    def delete_log(self, log_id: str) -> bool:
        """删除单条轨迹日志。"""
        return self._log_store.delete(log_id)

    def clear_all_logs(self) -> int:
        """清空所有轨迹日志，返回删除数量。"""
        return self._log_store.delete_all()
