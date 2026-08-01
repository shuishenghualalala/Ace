"""异步进化队列：按 session 串行处理 evolution 任务，结果在下一轮交互中体现。

设计要点：
  - 每个 session 一个 worker，FIFO 依次执行 evolution 任务
  - evolution 完成后，结果存入 _pending_results[session_id]
  - 下一轮交互调用 drain_results(session_id) 取走并清空
  - worker 通过 asyncio.to_thread 调用同步的 EvolutionManager 方法，不阻塞事件循环
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class EvolutionQueue:
    """Per-session 串行进化队列。

    用法::

        queue = EvolutionQueue()

        # Turn N 结束后：入队本轮 evolution 任务
        await queue.enqueue(sid, owner, conv_id, mgr, full_cycle=True)

        # Turn N+1 开始时：取出上一轮的结果（可能为空）
        results = queue.drain_results(sid)
        for r in results:
            yield ResponseChunk.evolution_footer(..., r["text"], ...)

        # 关闭
        await queue.shutdown()
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._pending_results: dict[str, list[dict]] = {}

    # ── 公开接口 ──────────────────────────────────────────────────────

    async def enqueue(
        self,
        session_id: str,
        owner_account_id: str,
        conversation_id: str,
        manager: Any,
        full_cycle: bool,
    ) -> None:
        """入队一个 evolution 任务，自动启动 session 级 worker。"""
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue()
            self._workers[session_id] = asyncio.create_task(
                self._worker(session_id)
            )
        await self._queues[session_id].put(
            (owner_account_id, conversation_id, manager, full_cycle)
        )

    def drain_results(self, session_id: str) -> list[dict]:
        """取出并清空该 session 的 pending evolution 结果。"""
        return self._pending_results.pop(session_id, [])

    def has_pending(self, session_id: str) -> bool:
        """是否有未取走的 evolution 结果。"""
        return bool(self._pending_results.get(session_id))

    async def shutdown(self) -> None:
        """优雅关闭：向所有队列发送哨兵，等待 worker 退出。"""
        for queue in self._queues.values():
            await queue.put(None)
        for task in list(self._workers.values()):
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._queues.clear()
        self._workers.clear()

    # ── 内部实现 ──────────────────────────────────────────────────────

    async def _worker(self, session_id: str) -> None:
        """单 session 串行处理：取任务 → 执行 → 存结果。"""
        queue = self._queues[session_id]
        while True:
            item = await queue.get()
            if item is None:
                break  # 哨兵：shutdown
            owner_account_id, conversation_id, manager, full_cycle = item
            try:
                result = await self._run_evolution(
                    manager, session_id, owner_account_id,
                    conversation_id, full_cycle,
                )
                if result is not None:
                    self._pending_results.setdefault(session_id, []).append(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("[EVOLUTION] 队列任务失败: session=%s err=%s", session_id, exc)
            finally:
                queue.task_done()

        # worker 退出，清理自身引用
        self._queues.pop(session_id, None)
        self._workers.pop(session_id, None)
        log.debug("[EVOLUTION] worker 退出: session=%s", session_id)

    async def _run_evolution(
        self,
        manager: Any,
        session_id: str,
        owner_account_id: str,
        conversation_id: str,
        full_cycle: bool,
    ) -> dict | None:
        """执行一次 evolution 周期，返回结果 dict（或 None 表示无结果）。

        复用 _run_evolution_visible 中的三阶段逻辑，但不输出状态帧，
        仅在完成后构建摘要结果。
        """
        if not full_cycle:
            # 仅提取轨迹，不产生 footer 结果
            log_id = await asyncio.to_thread(
                manager.extract_session, session_id, owner_account_id,
            )
            if log_id:
                log.info("[EVOLUTION] 轨迹已提取(async): session=%s log_id=%s", session_id, log_id)
            return None

        # ── 阶段 1：轨迹提取 ──────────────────────────────────────────
        try:
            log_id = await asyncio.to_thread(
                manager.extract_session, session_id, owner_account_id,
            )
            log.info("[EVOLUTION] 阶段1-轨迹提取(async): session=%s log_id=%s", session_id, log_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[EVOLUTION] 阶段1-轨迹提取失败(async): session=%s err=%s", session_id, exc)

        # ── 阶段 2：技能优化 ──────────────────────────────────────────
        optimized_skills: list[dict] = []  # 阶段2优化结果，不计入最终 footer 的"进化"计数
        evolved_skills: list[dict] = []    # 阶段3真正的进化结果
        try:
            optimization_patches = await asyncio.to_thread(
                manager.optimize_all,
                dry_run=False,
                current_session_id=session_id,
            )
            if isinstance(optimization_patches, dict):
                for slug, patches in optimization_patches.items():
                    if patches:
                        optimized_skills.append({
                            "slug": slug,
                            "name": slug.replace("-", " ").title(),
                            "patches": patches[:3] if isinstance(patches, list) else [str(patches)],
                        })
            log.info(
                "[EVOLUTION] 阶段2-技能优化(async): session=%s optimized=%d",
                session_id, len(optimized_skills),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[EVOLUTION] 阶段2-技能优化失败(async): session=%s err=%s", session_id, exc)

        # ── 阶段 3：技能生成（创建新技能 / 进化已有技能）──────────────
        created_skills: list[dict] = []
        # 命名变量：propose() 内部会将进化建议追加到此列表
        # _build_evolve_suggestion 已实际执行 evolve_skill 更新 SKILL.md
        evolution_suggestions: list = []
        try:
            proposals = await asyncio.to_thread(
                manager.generate_proposals,
                min_queries=2,
                max_proposals=5,
                current_session_id=session_id,
                evolution_suggestions=evolution_suggestions,
                conversation_id=conversation_id,
                session_id=session_id or "",
            )
            if proposals:
                for proposal in proposals:
                    path = await asyncio.to_thread(
                        manager.create_skill,
                        proposal,
                        conversation_id=conversation_id,
                        session_id=session_id or "",
                    )
                    if path:
                        meta: dict = {}
                        if proposal.structured_content:
                            meta = proposal.structured_content.get("metadata", {})
                        actual_slug = ""
                        try:
                            actual_slug = Path(path).parent.name
                        except Exception:
                            pass
                        created_skills.append({
                            "name": meta.get("name") or proposal.proposed_name or proposal.proposed_slug,
                            "slug": actual_slug or proposal.proposed_slug,
                            "path": path,
                            "description": meta.get("zh_description") or meta.get("description") or proposal.description or proposal.zh_description or "",
                            "zh_name": meta.get("zh_name") or proposal.zh_name or proposal.proposed_name or "",
                        })
            # 从 evolution_suggestions 中提取已执行的进化结果
            # _build_evolve_suggestion 在 propose() 内部已调用 evolve_skill
            # 完成了 SKILL.md 更新，这里只需收集结果用于 footer 展示
            for sug in evolution_suggestions:
                if getattr(sug, "suggestion_type", "") == "evolve":
                    evolved_skills.append({
                        "slug": sug.skill_slug,
                        "name": sug.skill_name,
                        "patches": [sug.suggested_value[:200]] if sug.suggested_value else [sug.reason[:200]],
                    })
            log.info(
                "[EVOLUTION] 阶段3-技能生成(async): session=%s proposals=%d created=%d evolved=%d",
                session_id, len(proposals) if proposals else 0, len(created_skills), len(evolution_suggestions),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[EVOLUTION] 阶段3-技能生成失败(async): session=%s err=%s", session_id, exc)

        # ── 构建摘要 ──────────────────────────────────────────────────
        # 进化成功或技能创建成功即产生 footer，
        # 两者都无（无进化）或失败时不显示，避免给用户输出无意义信息
        if not created_skills and not evolved_skills:
            log.info("[EVOLUTION] 本轮无需新增或进化技能(async): session=%s", session_id)
            return None

        parts = []
        if created_skills:
            parts.append(f"新增 {len(created_skills)} 项技能")
        if evolved_skills:
            parts.append(f"进化 {len(evolved_skills)} 项技能")

        return {
            "text": f"Skill 自进化完成 — {' · '.join(parts)}",
            "created_skills": created_skills if created_skills else None,
            "evolved_skills": evolved_skills if evolved_skills else None,
        }
