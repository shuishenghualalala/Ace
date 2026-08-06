"""技能优化器：基于历史日志分析并优化现有 skill。

优化维度：
  1. query_examples — 从历史用户查询中提取示例，补充到 metadata.query_examples
  2. description — 当 description 为空或过短时，从用户查询中生成
  3. metadata — 补充缺失的 zh_name / zh_description
  4. body — 根据错误率在正文中追加注意事项

优化流程：
  analyze(skill_slug) → 生成 OptimizationSuggestion 列表
  apply(suggestion) → 修改 SKILL.md 文件（支持 dry_run 预览 patch）
"""
from __future__ import annotations

import difflib
import logging
from pathlib import Path

from crew.evolution.log_store import EvolutionLogStore
from crew.evolution.models import OptimizationSuggestion, SkillUsageStat

logger = logging.getLogger(__name__)


def _current_owner_for_audit() -> str | None:
    """取当前 owner 记入技能审计；取不到由审计层记成 system。"""
    try:
        from crew.core.runctx import current_owner_account_id

        return str(current_owner_account_id.get() or "") or None
    except Exception:
        return None


class SkillOptimizer:
    """基于历史日志分析 skill 使用情况，生成并应用优化建议。"""

    def __init__(self, log_store: EvolutionLogStore | None = None):
        self._log_store = log_store or EvolutionLogStore()

    # ── 分析 ──────────────────────────────────────────────────────────────

    def analyze(
        self,
        skill_slug: str,
        current_session_id: str | None = None,
        stats: dict[str, SkillUsageStat] | None = None,
    ) -> list[OptimizationSuggestion]:
        """分析单个 skill 的使用情况，返回优化建议列表。

        当 current_session_id 被传入时，以当前会话数据为主、历史数据为辅。
        stats 可传入预计算的统计结果，避免 analyze_all() 重复调用 get_skill_stats()。
        """
        if stats is None:
            stats = self._log_store.get_skill_stats(current_session_id=current_session_id)
        stat = stats.get(skill_slug)
        if not stat:
            logger.info("skill %s 无使用记录", skill_slug)
            return []

        skill_info = self._get_skill_info(skill_slug)
        if not skill_info:
            logger.info("skill %s 不存在或未安装", skill_slug)
            return []

        suggestions: list[OptimizationSuggestion] = []
        suggestions.extend(self._suggest_query_examples(skill_slug, stat, skill_info))
        suggestions.extend(self._suggest_description(skill_slug, stat, skill_info))
        suggestions.extend(self._suggest_body_additions(skill_slug, stat, skill_info))
        suggestions.extend(self._suggest_metadata(skill_slug, stat, skill_info))

        return suggestions

    def analyze_all(
        self, current_session_id: str | None = None
    ) -> dict[str, list[OptimizationSuggestion]]:
        """分析所有有使用记录的 skill。

        当 current_session_id 被传入时，以当前会话数据为主、历史数据为辅。
        """
        stats = self._log_store.get_skill_stats(current_session_id=current_session_id)
        results: dict[str, list[OptimizationSuggestion]] = {}
        for slug in stats:
            suggestions = self.analyze(
                slug, current_session_id=current_session_id, stats=stats
            )
            if suggestions:
                results[slug] = suggestions
        return results

    # ── 应用 ──────────────────────────────────────────────────────────────

    def apply(
        self,
        suggestion: OptimizationSuggestion,
        dry_run: bool = False,
    ) -> str | None:
        """应用单条优化建议到 SKILL.md，返回 unified diff patch 文本。

        dry_run=True 时只返回 patch 不实际写入。
        """
        skill_path = Path(suggestion.skill_path)
        if not skill_path.exists():
            logger.warning("SKILL.md 不存在: %s", skill_path)
            return None

        old_text = skill_path.read_text(encoding="utf-8")

        from crew.agent.skills import _parse_frontmatter, _format_skill_markdown

        frontmatter, body = _parse_frontmatter(old_text)
        changed = False

        if suggestion.suggestion_type == "query_examples":
            meta = frontmatter.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                frontmatter["metadata"] = meta
            existing = meta.get("query_examples", [])
            if isinstance(existing, str):
                existing = [existing]
            if not isinstance(existing, list):
                existing = list(existing) if existing else []
            for q in suggestion.suggested_value.split("\n"):
                q = q.strip()
                if q and q not in existing:
                    existing.append(q)
            meta["query_examples"] = existing[:10]
            changed = True

        elif suggestion.suggestion_type == "description":
            frontmatter["description"] = suggestion.suggested_value
            changed = True

        elif suggestion.suggestion_type == "metadata":
            meta = frontmatter.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                frontmatter["metadata"] = meta
            for line in suggestion.suggested_value.split("\n"):
                line = line.strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    if k in ("zh_name", "zh_description") and v:
                        meta[k] = v
            changed = True

        elif suggestion.suggestion_type == "body":
            addition = (
                f"\n\n## 注意事项（基于历史轨迹自动生成）\n\n"
                f"{suggestion.suggested_value}\n"
            )
            body = body + addition
            changed = True

        elif suggestion.suggestion_type == "evolve":
            # 跨轮次关联触发的进化建议
            # suggested_value 格式: query_examples（每行一个）\n---\nbody_addition
            parts = suggestion.suggested_value.split("\n---\n", 1)
            query_part = parts[0] if len(parts) > 0 else ""
            body_part = parts[1] if len(parts) > 1 else ""

            # 1. 添加 query_examples 到 metadata
            meta = frontmatter.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                frontmatter["metadata"] = meta
            existing_examples = meta.get("query_examples", [])
            if isinstance(existing_examples, str):
                existing_examples = [existing_examples]
            if not isinstance(existing_examples, list):
                existing_examples = list(existing_examples) if existing_examples else []
            for q in query_part.split("\n"):
                q = q.strip()
                if q and q not in existing_examples:
                    existing_examples.append(q)
            meta["query_examples"] = existing_examples[:10]

            # 2. 添加 body 扩展段落
            if body_part.strip():
                addition = f"\n\n{body_part.strip()}\n"
                body = body + addition

            changed = True

        if not changed:
            return None

        new_text = _format_skill_markdown(frontmatter, body)

        patch = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{skill_path.name}",
            tofile=f"b/{skill_path.name}",
            n=3,
        ))

        if not dry_run:
            # 走受治理的原地更新：互斥锁 + 路径 containment（内置技能不可改）+
            # 审计日志 + 原子替换 + 失败回滚。此前是裸 write_text，四样全绕过。
            from crew.agent.skills import update_skill_markdown

            if not update_skill_markdown(
                suggestion.skill_slug,
                new_text,
                operator_account_id=_current_owner_for_audit(),
                source="crew.evolution.optimizer",
            ):
                logger.warning("skill %s 优化写入被拒绝", suggestion.skill_slug)
                return ""
            suggestion.applied = True

        return patch

    def apply_all(
        self,
        suggestions: dict[str, list[OptimizationSuggestion]],
        dry_run: bool = False,
    ) -> dict[str, list[str]]:
        """批量应用优化建议，返回 {slug: [patch, ...]}。"""
        results: dict[str, list[str]] = {}
        for slug, sug_list in suggestions.items():
            patches: list[str] = []
            for sug in sug_list:
                patch = self.apply(sug, dry_run=dry_run)
                if patch:
                    patches.append(patch)
            if patches:
                results[slug] = patches
        return results

    # ── 内部：建议生成 ────────────────────────────────────────────────────

    def _get_skill_info(self, skill_slug: str) -> dict | None:
        """从 skills 系统获取 skill 信息。"""
        from crew.agent.skills import get_skills, resolve_skill_any

        info = resolve_skill_any(skill_slug)
        if info:
            return info

        skills = get_skills()
        key = f"/{skill_slug.lstrip('/')}"
        return skills.get(key)

    def _suggest_query_examples(
        self,
        skill_slug: str,
        stat: SkillUsageStat,
        skill_info: dict,
    ) -> list[OptimizationSuggestion]:
        """从历史用户查询中提取 query_examples。"""
        suggestions: list[OptimizationSuggestion] = []

        from crew.agent.skills import _parse_frontmatter

        skill_path = Path(skill_info.get("skill_md_path", ""))
        if not skill_path.exists():
            return suggestions

        content = skill_path.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        meta = fm.get("metadata") or {}
        existing_examples = meta.get("query_examples", [])
        if isinstance(existing_examples, str):
            existing_examples = [existing_examples]
        if not isinstance(existing_examples, list):
            existing_examples = []
        existing_set = {str(e).strip().lower() for e in existing_examples}

        # 优先使用当前会话的查询，历史查询作为补充
        prioritized_queries = list(stat.current_session_queries)
        for q in stat.user_queries:
            if q not in prioritized_queries:
                prioritized_queries.append(q)

        new_examples: list[str] = []
        for query in prioritized_queries:
            q = query.strip()
            if q and q.lower() not in existing_set and len(q) <= 100:
                new_examples.append(q)
            if len(new_examples) >= 5:
                break

        if new_examples:
            session_note = ""
            if stat.current_session_activation_count > 0:
                session_note = f"（当前会话 {stat.current_session_activation_count} 次）"
            suggestions.append(OptimizationSuggestion(
                skill_slug=skill_slug,
                skill_name=stat.skill_name,
                skill_path=str(skill_path),
                suggestion_type="query_examples",
                current_value="\n".join(str(e) for e in existing_examples),
                suggested_value="\n".join(new_examples),
                reason=f"从 {stat.activation_count} 次使用记录中提取的高频用户查询{session_note}",
                confidence=0.7,
            ))

        return suggestions

    def _suggest_description(
        self,
        skill_slug: str,
        stat: SkillUsageStat,
        skill_info: dict,
    ) -> list[OptimizationSuggestion]:
        """根据用户查询优化 description。"""
        suggestions: list[OptimizationSuggestion] = []
        current_desc = skill_info.get("description", "")

        if not current_desc or len(current_desc) < 10:
            # 优先使用当前会话的查询
            queries = stat.current_session_queries or stat.user_queries
            if queries:
                best_query = min(queries, key=len)
                if best_query:
                    new_desc = f"帮助用户完成：{best_query[:80]}"
                    suggestions.append(OptimizationSuggestion(
                        skill_slug=skill_slug,
                        skill_name=stat.skill_name,
                        skill_path=skill_info.get("skill_md_path", ""),
                        suggestion_type="description",
                        current_value=current_desc,
                        suggested_value=new_desc,
                        reason="当前 description 为空或过短，从用户查询中生成",
                        confidence=0.6,
                    ))

        return suggestions

    def _suggest_body_additions(
        self,
        skill_slug: str,
        stat: SkillUsageStat,
        skill_info: dict,
    ) -> list[OptimizationSuggestion]:
        """根据错误模式生成 body 注意事项。"""
        suggestions: list[OptimizationSuggestion] = []

        # 优先使用当前会话的错误率
        if stat.current_session_activation_count > 0:
            # 使用当前会话的实际错误率计算
            error_rate = stat.current_session_error_count / stat.current_session_activation_count
            error_count = stat.current_session_error_count
        else:
            error_rate = stat.error_rate
            error_count = stat.error_count

        if error_rate > 0.3 and error_count > 0:
            error_tools_str = (
                ", ".join(stat.tools_used.keys()) if stat.tools_used else "未知工具"
            )
            addition = (
                f"- 该技能在历史使用中错误率为 {error_rate:.0%}，"
                f"请特别注意参数格式\n"
                f"- 常见出错工具：{error_tools_str}\n"
                f"- 建议在调用前确认参数完整性"
            )
            suggestions.append(OptimizationSuggestion(
                skill_slug=skill_slug,
                skill_name=stat.skill_name,
                skill_path=skill_info.get("skill_md_path", ""),
                suggestion_type="body",
                current_value="",
                suggested_value=addition,
                reason=f"错误率 {error_rate:.0%}，需要补充注意事项",
                confidence=0.8,
            ))

        return suggestions

    def _suggest_metadata(
        self,
        skill_slug: str,
        stat: SkillUsageStat,
        skill_info: dict,
    ) -> list[OptimizationSuggestion]:
        """补充缺失的中文 metadata。"""
        suggestions: list[OptimizationSuggestion] = []

        from crew.agent.skills import _parse_frontmatter, _contains_cjk

        skill_path = Path(skill_info.get("skill_md_path", ""))
        if not skill_path.exists():
            return suggestions

        content = skill_path.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        meta = fm.get("metadata") or {}

        missing: list[str] = []
        zh_name = meta.get("zh_name", "")
        if not zh_name or not _contains_cjk(zh_name):
            display = skill_info.get("display_name") or stat.skill_name
            if _contains_cjk(display):
                missing.append(f"zh_name: {display}")

        zh_desc = meta.get("zh_description", "")
        if not zh_desc or not _contains_cjk(zh_desc):
            desc_zh = skill_info.get("description_zh", "")
            if _contains_cjk(desc_zh):
                missing.append(f"zh_description: {desc_zh}")
            elif stat.current_session_queries or stat.user_queries:
                # 优先使用当前会话的查询
                ref_queries = stat.current_session_queries or stat.user_queries
                missing.append(
                    f"zh_description: 帮助用户完成{ref_queries[0][:50]}"
                )

        if missing:
            suggestions.append(OptimizationSuggestion(
                skill_slug=skill_slug,
                skill_name=stat.skill_name,
                skill_path=str(skill_path),
                suggestion_type="metadata",
                current_value="",
                suggested_value="\n".join(missing),
                reason="缺少中文展示元数据",
                confidence=0.5,
            ))

        return suggestions
