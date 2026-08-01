"""历史日志存储：将轨迹日志持久化到 Crew home 目录。

存储路径：get_crew_home()/evolution/logs/
每条日志为一个 JSON 文件，文件名为 {log_id}.json。
索引文件：get_crew_home()/evolution/index.json

支持按 skill、tool、session、错误状态等维度过滤查询。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from crew.evolution.models import TrajectoryLog, SkillUsageStat

logger = logging.getLogger(__name__)


class EvolutionLogStore:
    """轨迹历史日志存储。"""

    def __init__(self, base_dir: Path | None = None):
        if base_dir is None:
            from crew.state.home import get_crew_home
            base_dir = get_crew_home() / "evolution"
        self._base_dir = base_dir
        self._logs_dir = base_dir / "logs"
        self._index_path = base_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)

    # ── 保存 / 加载 ───────────────────────────────────────────────────────

    def save(self, log: TrajectoryLog) -> str:
        """保存一条轨迹日志，返回 log_id。"""
        path = self._logs_dir / f"{log.log_id}.json"
        path.write_text(log.to_json(), encoding="utf-8")
        self._update_index(log)
        logger.debug("保存轨迹日志 %s -> %s", log.log_id, path)
        return log.log_id

    def save_batch(self, logs: list[TrajectoryLog]) -> list[str]:
        """批量保存轨迹日志。只读写一次 index.json，避免 O(n²) 索引更新。"""
        ids: list[str] = []
        for log in logs:
            path = self._logs_dir / f"{log.log_id}.json"
            path.write_text(log.to_json(), encoding="utf-8")
            ids.append(log.log_id)
            logger.debug("保存轨迹日志 %s -> %s", log.log_id, path)
        # 批量更新索引，只读写一次
        if logs:
            self._update_index_batch(logs)
        return ids

    def load(self, log_id: str) -> TrajectoryLog | None:
        """加载单条轨迹日志。"""
        path = self._logs_dir / f"{log_id}.json"
        if not path.exists():
            return None
        try:
            return TrajectoryLog.from_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("加载轨迹日志 %s 失败: %s", log_id, exc)
            return None

    # ── 查询 ──────────────────────────────────────────────────────────────

    def list_logs(
        self,
        *,
        skill_slug: str | None = None,
        tool_name: str | None = None,
        session_id: str | None = None,
        has_errors: bool | None = None,
        limit: int = 0,
    ) -> list[TrajectoryLog]:
        """列出轨迹日志，支持多维度过滤。

        Args:
            skill_slug: 仅返回激活了该 skill 的日志
            tool_name: 仅返回使用了该工具的日志
            session_id: 仅返回该会话的日志
            has_errors: True 仅返回有错误的，False 仅返回无错误的
            limit: 最多返回 N 条，0 表示不限
        """
        index = self._read_index()
        results: list[TrajectoryLog] = []

        for entry in index:
            if session_id and entry.get("session_id") != session_id:
                continue
            if has_errors is True and entry.get("error_count", 0) == 0:
                continue
            if has_errors is False and entry.get("error_count", 0) > 0:
                continue
            if skill_slug and skill_slug not in entry.get("skills_activated", []):
                continue
            if tool_name and tool_name not in entry.get("tool_usage", {}):
                continue

            log = self.load(entry["log_id"])
            if log:
                results.append(log)
            if limit and len(results) >= limit:
                break

        return results

    def list_user_queries(
        self,
        *,
        session_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[tuple[str, str, str]]:
        """从索引中直接提取用户查询，返回 [(query_text, log_id, session_id), ...]。

        直接从 index.json 读取 user_queries 冗余字段，无需逐条加载 JSON 正文。
        对于索引中缺少 user_queries 字段的旧条目，回退加载 JSON。

        Args:
            session_id: 仅返回该会话的查询
            exclude_session_id: 排除该会话的查询
        """
        index = self._read_index()
        results: list[tuple[str, str, str]] = []

        for entry in index:
            entry_session = entry.get("session_id", "")
            if session_id and entry_session != session_id:
                continue
            if exclude_session_id and entry_session == exclude_session_id:
                continue

            log_id = entry.get("log_id", "")
            user_queries = entry.get("user_queries")

            if user_queries is None:
                # 旧索引条目无 user_queries 字段，回退加载 JSON
                log = self.load(log_id)
                if not log:
                    continue
                for e in log.entries:
                    if e.role == "user" and e.content and not e.is_error:
                        if "用户激活了" in e.content:
                            continue
                        results.append(
                            (e.content.strip()[:200], log_id, entry_session)
                        )
            else:
                for q in user_queries:
                    if "用户激活了" in q:
                        continue
                    results.append((q.strip()[:200], log_id, entry_session))

        return results

    def get_previous_session_logs(
        self,
        current_session_id: str,
        limit: int = 5,
    ) -> list[TrajectoryLog]:
        """获取当前会话之前的轨迹日志（按 updated_at 倒序）。

        用于跨轮次关联检测：获取上一轮交互的轨迹，
        分析本轮任务与上轮任务/skill 的关联性。

        Args:
            current_session_id: 当前会话 ID
            limit: 最多返回 N 条日志
        """
        index = self._read_index()
        # 过滤掉当前会话的日志
        previous_entries = [
            e for e in index
            if e.get("session_id") != current_session_id
        ]
        # 按 updated_at 倒序排序（回退到 extracted_at）
        # 统一转为 str，避免 float 与 str 混合比较报错
        previous_entries.sort(
            key=lambda e: str(e.get("updated_at", "") or e.get("extracted_at", "")),
            reverse=True,
        )

        results: list[TrajectoryLog] = []
        for entry in previous_entries[:limit]:
            log = self.load(entry["log_id"])
            if log:
                results.append(log)
        return results

    def add_skill_to_logs(self, log_ids: list[str], skill_slug: str) -> int:
        """将 skill slug 添加到指定轨迹日志的 skills_activated 中。

        用于进化周期创建新 skill 后，将其回写到源轨迹日志，
        使下一轮跨轮次关联检测能直接从 skills_activated 获取到。

        Returns:
            成功更新的日志数量
        """
        count = 0
        for log_id in log_ids:
            log = self.load(log_id)
            if not log:
                continue
            if skill_slug not in log.skills_activated:
                log.skills_activated.append(skill_slug)
                self.save(log)
                count += 1
        return count

    def delete(self, log_id: str) -> bool:
        """删除一条轨迹日志。"""
        path = self._logs_dir / f"{log_id}.json"
        deleted = path.exists()
        if deleted:
            path.unlink()
        self._remove_from_index(log_id)
        return deleted

    def delete_all(self) -> int:
        """清空所有轨迹日志。"""
        count = 0
        for f in self._logs_dir.glob("*.json"):
            f.unlink()
            count += 1
        self._write_index([])
        # 同时清除持久化的聚类摘要
        self.clear_clusters()
        return count

    # ── 聚类摘要持久化 ──────────────────────────────────────────────────

    def load_clusters(self) -> list[dict]:
        """加载持久化的聚类摘要。

        返回 list[dict]，每个 dict 可通过 QueryCluster.from_dict 反序列化。
        文件不存在或损坏时返回空列表。
        """
        path = self._base_dir / "clusters.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)]
            return []
        except Exception as exc:
            logger.warning("加载聚类摘要失败: %s", exc)
            return []

    def save_clusters(self, clusters: list[dict]) -> None:
        """持久化聚类摘要。"""
        path = self._base_dir / "clusters.json"
        path.write_text(
            json.dumps(clusters, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug("持久化 %d 个聚类摘要 -> %s", len(clusters), path)

    def clear_clusters(self) -> None:
        """清除持久化的聚类摘要。"""
        path = self._base_dir / "clusters.json"
        if path.exists():
            path.unlink()
            logger.info("已清除持久化聚类摘要")

    # ── 聚合统计 ──────────────────────────────────────────────────────────

    def get_skill_stats(
        self, current_session_id: str | None = None
    ) -> dict[str, SkillUsageStat]:
        """聚合所有日志，按 skill 统计使用情况。

        直接从 index 聚合，不加载 JSON 正文，避免 O(n) 全量文件读取。
        index 条目中冗余存储了 user_queries 字段，无需反序列化完整 TrajectoryLog。

        当 current_session_id 被传入时，额外统计当前会话的独立数据
        （current_session_* 字段），用于以当前交互为主、历史为辅的判断逻辑。
        """
        index = self._read_index()
        stats: dict[str, SkillUsageStat] = {}

        for entry in index:
            session_id = entry.get("session_id", "")
            is_current = bool(current_session_id) and session_id == current_session_id
            log_id = entry.get("log_id", "")
            skills_activated = entry.get("skills_activated", [])
            error_count = entry.get("error_count", 0)
            tool_usage = entry.get("tool_usage", {})
            message_count = entry.get("message_count", 0)
            # 从 index 冗余字段获取 user_queries，旧条目无此字段时回退为空
            entry_user_queries = entry.get("user_queries", [])

            for skill_name in skills_activated:
                if skill_name not in stats:
                    stats[skill_name] = SkillUsageStat(
                        skill_slug=skill_name, skill_name=skill_name
                    )
                stat = stats[skill_name]
                stat.activation_count += 1
                stat.error_count += error_count
                stat.source_log_ids.append(log_id)
                stat.avg_message_count = (
                    (stat.avg_message_count * (stat.activation_count - 1) + message_count)
                    / stat.activation_count
                )
                for tool, cnt in tool_usage.items():
                    stat.tools_used[tool] = stat.tools_used.get(tool, 0) + cnt
                # 收集 user queries（从 index 冗余字段）
                for q in entry_user_queries:
                    if q not in stat.user_queries:
                        stat.user_queries.append(q)
                # 当前会话独立统计
                if is_current:
                    stat.current_session_activation_count += 1
                    stat.current_session_error_count += error_count
                    for q in entry_user_queries:
                        if q not in stat.current_session_queries:
                            stat.current_session_queries.append(q)

        # 计算错误率（失败工具调用占比），限制 user_queries 数量
        for stat in stats.values():
            total_tool_calls = sum(stat.tools_used.values())
            if total_tool_calls > 0:
                stat.error_rate = stat.error_count / total_tool_calls
            stat.user_queries = stat.user_queries[:20]
            stat.current_session_queries = stat.current_session_queries[:20]

        return stats

    def get_tool_stats(self) -> dict[str, dict[str, Any]]:
        """聚合所有日志，按 tool 统计使用情况。直接从 index 聚合，不加载 JSON。"""
        index = self._read_index()
        stats: dict[str, dict[str, Any]] = {}
        for entry in index:
            tool_usage = entry.get("tool_usage", {})
            error_tools = entry.get("error_tools", [])
            for tool, cnt in tool_usage.items():
                if tool not in stats:
                    stats[tool] = {
                        "tool_name": tool,
                        "total_calls": 0,
                        "session_count": 0,
                        "error_sessions": 0,
                    }
                stats[tool]["total_calls"] += cnt
                stats[tool]["session_count"] += 1
                if tool in error_tools:
                    stats[tool]["error_sessions"] += 1
        return stats

    # ── 索引管理 ──────────────────────────────────────────────────────────

    def _read_index(self) -> list[dict]:
        """读取索引为 list[dict]，用于遍历场景。"""
        if not self._index_path.exists():
            return []
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        # 兼容 {"logs": [...]} 和 [...] 两种格式
        if isinstance(data, dict):
            data = data.get("logs", [])
        if not isinstance(data, list):
            return []
        # 确保每个条目都是 dict
        return [e for e in data if isinstance(e, dict)]

    def _read_index_dict(self) -> dict[str, dict]:
        """读取索引为 dict[log_id → entry]，用于 O(1) 查找/upsert 场景。"""
        index_list = self._read_index()
        return {
            e["log_id"]: e
            for e in index_list
            if "log_id" in e
        }

    def _write_index(self, index: list[dict]) -> None:
        self._index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _build_index_entry(log: TrajectoryLog) -> dict:
        """从 TrajectoryLog 构建索引条目。

        冗余存储 user_queries 字段，使 get_skill_stats() 无需加载 JSON 正文。
        """
        user_queries = [
            e.content[:200]
            for e in log.entries
            if e.role == "user" and e.content and not e.is_error
        ]
        return {
            "log_id": log.log_id,
            "session_id": log.session_id,
            "title": log.title,
            "skills_activated": log.skills_activated,
            "tool_usage": log.tool_usage,
            "error_count": log.error_count,
            "error_tools": log.error_tools,
            "message_count": log.message_count,
            "updated_at": log.updated_at,
            "extracted_at": log.extracted_at,
            "summary": log.summary,
            "user_queries": user_queries,
        }

    def _update_index(self, log: TrajectoryLog) -> None:
        """单条 upsert：dict O(1) 查找替换，避免线性扫描。"""
        index_dict = self._read_index_dict()
        index_dict[log.log_id] = self._build_index_entry(log)
        self._write_index(list(index_dict.values()))

    def _update_index_batch(self, logs: list[TrajectoryLog]) -> None:
        """批量 upsert：只读写一次 index.json，避免 save_batch() 的 O(n²) 索引更新。"""
        index_dict = self._read_index_dict()
        for log in logs:
            index_dict[log.log_id] = self._build_index_entry(log)
        self._write_index(list(index_dict.values()))

    def _remove_from_index(self, log_id: str) -> None:
        """dict O(1) 删除，避免线性扫描。"""
        index_dict = self._read_index_dict()
        index_dict.pop(log_id, None)
        self._write_index(list(index_dict.values()))
