"""技能生成器：根据轨迹信息自动生成新 skill。

生成策略：
  1. 从历史轨迹中收集所有用户查询
  2. 对相似查询进行聚类
  3. 过滤已被现有 skill 覆盖的意图
  4. 语义级判断：调用 LLM 评估是否值得固化为 skill
  5. 为剩余聚类生成 SKILL.md 提案
  6. （可选）创建 SKILL.md 文件到用户 skills 目录

也支持从单个会话轨迹直接生成 skill。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from crew.core.types import ChatResponse
from crew.evolution.log_store import EvolutionLogStore
from crew.evolution.models import (
    CrossRoundAssociation,
    OptimizationSuggestion,
    SkillProposal,
    TrajectoryEntry,
    TrajectoryLog,
)

logger = logging.getLogger(__name__)


class QueryCluster:
    """用户查询聚类。

    除了查询文本外，还聚合了轨迹的操作元数据（工具使用、错误数等），
    使聚类摘要自包含——增量聚类时无需重新加载轨迹日志即可展示操作信息。
    """

    def __init__(
        self,
        representative: str,
        queries: list[tuple[str, str]] | None = None,
        *,
        tool_usage: dict[str, int] | None = None,
        error_count: int = 0,
        total_tool_calls: int = 0,
        total_messages: int = 0,
        trajectory_count: int = 0,
        skills_activated: list[str] | None = None,
        operation_summary: str = "",
        trajectory_summaries: list[str] | None = None,
        has_new_queries: bool = False,
        cluster_label: str = "",
    ):
        self.representative = representative
        self.queries: list[tuple[str, str]] = queries or []
        self.tool_usage: dict[str, int] = tool_usage or {}
        self.error_count: int = error_count
        self.total_tool_calls: int = total_tool_calls
        self.total_messages: int = total_messages
        self.trajectory_count: int = trajectory_count
        self.skills_activated: list[str] = skills_activated or []
        self.operation_summary: str = operation_summary
        self.trajectory_summaries: list[str] = trajectory_summaries or []
        # LLM 生成的聚类摘要名称（如"电商用户行为分析"），为空时回退到 representative
        self.cluster_label: str = cluster_label
        # 瞬态标记：本轮增量聚类是否接收了新查询（不序列化，仅内存使用）
        self.has_new_queries: bool = has_new_queries
        # 记录已合并元数据的 log_id，防止同一 session 的多条消息导致元数据重复合并
        self._merged_log_ids: set[str] = set()

    @property
    def display_name(self) -> str:
        """用于日志和展示的聚类名称：优先 cluster_label，回退 representative。"""
        return self.cluster_label or self.representative

    @property
    def tool_success_rate(self) -> float:
        """工具调用成功率 = (总调用 - 错误数) / max(总调用, 1)。"""
        if self.total_tool_calls <= 0:
            return 1.0
        return max(0.0, 1.0 - self.error_count / self.total_tool_calls)

    def add(self, query: str, log_id: str, meta: dict | None = None) -> None:
        """添加一条查询到聚类，可选传入轨迹元数据并合并。

        同一 log_id（session）的元数据只合并一次，避免同一 session
        有多条用户消息时元数据被重复累加。
        """
        self.queries.append((query, log_id))
        if meta is not None and log_id not in self._merged_log_ids:
            self._merged_log_ids.add(log_id)
            self._merge_meta(meta)

    def _merge_meta(self, meta: dict) -> None:
        """将单条轨迹的元数据合并到聚类聚合统计中。"""
        for tool, cnt in meta.get("tool_usage", {}).items():
            self.tool_usage[tool] = self.tool_usage.get(tool, 0) + cnt
        self.error_count += meta.get("error_count", 0)
        self.total_tool_calls += meta.get("total_tool_calls", 0)
        self.total_messages += meta.get("total_messages", 0)
        self.trajectory_count += 1
        for skill in meta.get("skills_activated", []):
            if skill not in self.skills_activated:
                self.skills_activated.append(skill)
        summary = meta.get("summary", "")
        if summary and len(self.trajectory_summaries) < 10:
            self.trajectory_summaries.append(summary)
        self.operation_summary = self._build_operation_summary()

    def _build_operation_summary(self) -> str:
        """根据聚合的工具使用统计生成操作摘要文本。"""
        parts: list[str] = []
        if self.tool_usage:
            tools = ", ".join(
                f"{name}({cnt})" for name, cnt in
                sorted(self.tool_usage.items(), key=lambda x: -x[1])[:10]
            )
            parts.append(f"工具: {tools}")
        if self.error_count:
            parts.append(f"错误: {self.error_count}次")
        if self.skills_activated:
            parts.append(f"技能: {', '.join(self.skills_activated[:5])}")
        return "; ".join(parts) if parts else ""

    def to_dict(self) -> dict:
        """序列化为 dict，用于持久化。"""
        return {
            "representative": self.representative,
            "cluster_label": self.cluster_label,
            "queries": [[q, lid] for q, lid in self.queries],
            "tool_usage": self.tool_usage,
            "error_count": self.error_count,
            "total_tool_calls": self.total_tool_calls,
            "total_messages": self.total_messages,
            "trajectory_count": self.trajectory_count,
            "skills_activated": self.skills_activated,
            "operation_summary": self.operation_summary,
            "trajectory_summaries": self.trajectory_summaries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> QueryCluster:
        """从 dict 反序列化，兼容旧版缺少操作元数据字段的数据。"""
        queries = [
            (pair[0], pair[1]) for pair in d.get("queries", [])
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        ]
        obj = cls(
            representative=d.get("representative", ""),
            queries=queries,
            tool_usage=d.get("tool_usage", {}),
            error_count=d.get("error_count", 0),
            total_tool_calls=d.get("total_tool_calls", 0),
            total_messages=d.get("total_messages", 0),
            trajectory_count=d.get("trajectory_count", len(queries)),
            skills_activated=d.get("skills_activated", []),
            operation_summary=d.get("operation_summary", ""),
            trajectory_summaries=d.get("trajectory_summaries", []),
            cluster_label=d.get("cluster_label", ""),
        )
        # 从已有 queries 中推导已合并的 log_id，防止后续 add 重复合并
        obj._merged_log_ids = {pair[1] for pair in queries}
        return obj


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """从 LLM 输出文本中提取 JSON 对象。

    处理以下情况：
    - 纯 JSON 输出
    - ```json ... ``` 代码块包裹
    - 前后包含对话文本（如 "好的，我帮你生成了json\\n{...}"）
    - 嵌套 JSON 对象
    """
    text = (text or "").strip()
    if not text:
        return None

    # 1. 去除 markdown 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 2. 直接尝试解析
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. 平衡括号匹配，提取第一个完整 JSON 对象
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidate = text[start : end + 1]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass
            start = text.find("{", end + 1)
        else:
            break

    return None


def _extract_json_array_from_text(text: str) -> list[Any] | None:
    """从 LLM 输出文本中提取 JSON 数组。

    处理以下情况：
    - 纯 JSON 数组输出
    - ```json ... ``` 代码块包裹
    - 前后包含对话文本
    """
    text = (text or "").strip()
    if not text:
        return None

    # 1. 去除 markdown 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 2. 直接尝试解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. 平衡括号匹配，提取第一个完整 JSON 数组
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidate = text[start : end + 1]
            try:
                data = json.loads(candidate)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass
            start = text.find("[", end + 1)
        else:
            break

    return None


class _SemanticVerdict:
    """LLM 语义评估结果。"""

    __slots__ = ("should_create", "confidence", "reason")

    def __init__(self, should_create: bool, confidence: float = 0.5, reason: str = ""):
        self.should_create = should_create
        self.confidence = confidence
        self.reason = reason


_EVAL_SYSTEM_PROMPT = (
    "你负责技能评估。请判断一组相似的用户查询是否值得固化为一个可复用的 skill（技能）。\n\n"
    "评估维度：\n"
    "1. **任务复杂度**：涉及多步工具编排、需要固定流程的任务更适合做 skill；"
    "简单的一问一答、闲聊、一次性问题不需要。\n"
    "2. **可复用性**：是否为可标准化的重复模式，其他用户/场景也可能遇到。\n"
    "3. **轨迹质量**：错误率（失败工具调用占比）适中是正常的——智能体在执行中尝试、"
    "失败、自适应调整后成功完成任务属于正常行为。只有错误率极高（>80%）且工具调用数"
    "较多时才说明流程不稳定。错误率 <50% 且最终任务成功完成的轨迹完全可以作为 skill 模板。\n"
    "4. **工具使用**：有意义的工具调用链说明任务有编排价值；无工具调用可能只是简单问答。\n"
    "5. **工作流整体价值**：如果一个聚类包含同一工作流的多个子任务（如写作+编辑+格式调整），\n"
    "   应评估整体工作流的可复用性，而非仅看单个子任务。即使部分轨迹错误率高，\n"
    "   只要工作流本身有标准化价值，就可以考虑创建 skill 并在正文中标注注意事项。\n"
    "6. **样本量不作为拒绝理由**：即使只有 1 条轨迹，只要任务有编排价值和可复用性，\n"
    "   就应该创建 skill。不要因为轨迹数少而拒绝创建——skill 创建后可以随更多使用持续优化。\n"
    "7. **鼓励创建**：只要任务有编排价值、可复用、最终完成了用户目标，就应倾向于创建 skill。\n"
    "   宁可多创建一个可能不太完美的 skill，也不要错过一个有价值的自动化场景。\n"
    "   skill 创建后可以持续优化，不创建则永远无法复用。\n\n"
    "请返回 JSON 格式：\n"
    '{"should_create": true/false, "confidence": 0.0-1.0, "reason": "简短说明判断理由"}\n\n'
    "只返回 JSON，不要其他内容。"
)

_ASSIGN_SYSTEM_PROMPT = (
    "你负责查询分类。请判断一条新查询是否属于已有的某个查询簇。\n\n"
    "规则：\n"
    "1. 如果新查询的意图与某个已有簇高度相似或属于同一工作流，返回该簇的编号\n"
    "2. 如果新查询不属于任何已有簇，返回 -1（表示需要新建簇）\n"
    "3. 同一工作流/领域的相关操作应归为同一簇，即使具体动作不同\n"
    "   例如「帮我写新闻」和「调整篇幅到300字」属于同一工作流，应归为同一簇\n\n"
    "返回 JSON 格式：\n"
    '{"cluster": 簇编号或-1}\n\n'
    "只返回 JSON，不要其他内容。"
)

# 逐条增量聚类参数
_LLM_CLUSTER_MAX = 500       # 超过此数回退到关键词聚类
_MAX_SUMMARY_CLUSTERS = 30  # prompt 中最多展示的簇摘要数
_LLM_ASSIGN_BATCH_SIZE = 15  # 批量分配时每次处理的查询数

_BATCH_ASSIGN_SYSTEM_PROMPT = (
    "你负责查询分类。请判断多条新查询分别属于哪个已有的查询簇。\n\n"
    "规则：\n"
    "1. 如果新查询的意图与某个已有簇高度相似或属于同一工作流，返回该簇的编号\n"
    "2. 如果新查询不属于任何已有簇，返回 -1（表示需要新建簇）\n"
    "3. 同一工作流/领域的相关操作应归为同一簇，即使具体动作不同\n"
    "   例如「帮我写新闻」和「调整篇幅到300字」属于同一工作流，应归为同一簇\n"
    "4. 综合考虑用户意图、智能体操作和执行结果，而非仅看查询文本\n"
    "   工具使用模式和成功率相似的查询更可能属于同一工作流\n\n"
    "返回 JSON 数组格式：\n"
    '[{"id": 0, "cluster": 簇编号或-1}, {"id": 1, "cluster": 簇编号或-1}, ...]\n\n'
    "每条查询都必须出现在结果中，id 对应输入中的查询编号。只返回 JSON，不要其他内容。"
)

_CLUSTER_LABEL_SYSTEM_PROMPT = (
    "你负责聚类命名。请根据聚类中的用户查询、智能体操作和执行结果，"
    "生成一个简洁的中文标签（8-20字），概括该聚类的核心意图和操作特征。\n\n"
    "命名规则：\n"
    "1. 标签应概括用户意图 + 智能体操作，而非直接复制某条用户查询\n"
    "2. 使用名词性短语，如「电商用户行为分析」「新闻稿撰写与编辑」\n"
    "3. 如果聚类已有关联技能，标签可与技能名称呼应\n\n"
    "返回 JSON 格式：\n"
    '{"label": "标签文本"}\n\n'
    "只返回 JSON，不要其他内容。"
)

# ── 结构化技能内容 JSON schema ──────────────────────────────────────────
#
# 技能内容按 SKILL.md 骨架拆分为以下模块，每个模块独立存储，
# 工作流步骤（workflow_steps）以数组形式存储，每步为一个独立对象，
# 方便后续优化和进化时深入到步骤级别。
#
# 完整结构示例：
# {
#   "metadata": {
#     "name": "skill-slug",
#     "description": "技能描述（用于语义级触发）",
#     "zh_name": "中文名",
#     "zh_description": "中文描述",
#     "version": "0.1.0",
#     "emoji": "📊",
#     "category": "通用办公",
#     "query_examples": ["示例查询1", "示例查询2"]
#   },
#   "overview": {
#     "title": "技能标题",
#     "description": "概述描述",
#     "core_capabilities": ["能力1", "能力2"]
#   },
#   "trigger_conditions": [
#     "触发条件1",
#     "触发条件2"
#   ],
#   "workflow_steps": [
#     {
#       "step": 1,
#       "title": "步骤标题",
#       "description": "步骤描述",
#       "sub_steps": ["子步骤1", "子步骤2"],
#       "code_examples": [
#         {"language": "bash", "platform": "Windows", "code": "..."},
#         {"language": "bash", "platform": "macOS/Linux", "code": "..."}
#       ]
#     }
#   ],
#   "script_template": {
#     "language": "python",
#     "description": "脚本模板说明",
#     "code": "import pandas as pd\n..."
#   },
#   "notes": [
#     {"title": "注意事项分类", "items": ["注意1", "注意2"]}
#   ],
#   "output_format": {
#     "description": "输出格式说明",
#     "template": "## 📊 数据概览\n- 总记录数\n..."
#   },
#   "common_keywords": ["关键词1", "关键词2"]
# }

_STRUCTURED_SKILL_SYSTEM_PROMPT = (
    "你负责生成结构化技能。请分析用户的交互轨迹，"
    "重新构建优化后的工作流程步骤，并将内容映射到技能骨架的不同部分，"
    "最终输出结构化 JSON。\n\n"
    "## 技能骨架结构\n"
    "技能内容按以下模块组织，每个模块对应 SKILL.md 的一个章节：\n\n"
    "1. **metadata**（元数据）：name, description, zh_name, zh_description, "
    "version, emoji, category, query_examples\n"
    "2. **overview**（概述）：title, description, core_capabilities[]\n"
    "3. **trigger_conditions**（触发条件）：用户可能说的自然语言列表\n"
    "4. **workflow_steps**（工作流程）：数组，每步包含 step, title, description, "
    "sub_steps[], code_examples[{language, platform, code}]\n"
    "5. **script_template**（脚本模板）：language, description, code\n"
    "6. **notes**（注意事项）：数组，每项包含 title, items[]\n"
    "7. **output_format**（输出格式）：description, template\n"
    "8. **common_keywords**（常见关键词）：字符串数组\n\n"
    "## 生成要求\n"
    "1. 根据用户交互轨迹**重新构建优化后的步骤**，不要简单照搬轨迹中的操作顺序，"
    "而是提炼出最优工作流程\n"
    "2. 每个工作流步骤应独立完整，方便后续单独优化某一步\n"
    "3. 代码示例应包含跨平台方案（Windows PowerShell + macOS/Linux bash）\n"
    "4. 触发条件使用自然语言（用户实际会怎么说），不要用关键词\n"
    "5. 注意事项应基于轨迹中的错误和经验提炼\n"
    "6. 输出格式应给出具体的报告/结果模板\n"
    "7. 常见关键词用于关键词级触发召回\n"
    "8. **metadata.name 必须是简洁有意义的英文 slug**，用短横线连接单词，"
    "反映技能核心功能（如 pdf-merge, excel-data-clean, git-branch-cleanup），"
    "不要使用 auto-skill-xxx 这类无意义名称\n\n"
    "请返回完整的 JSON 对象，不要包裹在代码块中，不要输出其他内容。"
)


_ASSOCIATION_SYSTEM_PROMPT = (
    "你负责分析任务关联。请判断当前用户查询与上一轮交互的任务/skill 之间的关联性。\n\n"
    "关联类型：\n"
    "1. **延续（continuation）**：本轮任务是上轮任务的直接延续（如上轮「分析数据」，本轮「可视化结果」）\n"
    "2. **扩展（expansion）**：本轮任务扩展了上轮任务的能力（如上轮「写文章」，本轮「调整格式和排版」）\n"
    "3. **同领域（same_domain）**：属于同一领域但不同子任务（如上轮「翻译英文」，本轮「翻译日文」）\n"
    "4. **工具链（tool_chain）**：使用相同工具链完成不同但相关的任务\n"
    "5. **无关联（none）**：完全不同的任务领域\n\n"
    "独立性评分规则（0-1，1=完全独立）：\n"
    "- 延续：0.1-0.3（高度关联，应进化已有 skill）\n"
    "- 扩展：0.2-0.4（高度关联，应进化已有 skill）\n"
    "- 同领域：0.4-0.6（中度关联，倾向进化）\n"
    "- 工具链：0.3-0.5（中高度关联，倾向进化）\n"
    "- 无关联：0.8-1.0（高度独立，应创建新 skill）\n\n"
    "关键原则：**只有当独立性很高（>=0.7）时才创建新 skill，否则优先进化已有 skill。**\n"
    "例如：「分析销售数据」和「销售数据可视化HTML报告」的独立性应低于0.3，\n"
    "因为后者是前者的直接延续。\n\n"
    "related_skill_slug：如果上一轮激活了 skill，填写最相关的 skill slug；"
    "如果上一轮没有激活 skill 但本轮任务与上轮任务相关，填写空字符串。\n\n"
    "请返回 JSON 格式：\n"
    '{"association_type": "continuation/expansion/same_domain/tool_chain/none", '
    '"association_score": 0.0-1.0, "independence_score": 0.0-1.0, '
    '"related_skill_slug": "最相关的已有skill slug（无则空字符串", '
    '"reason": "简短说明"}\n\n'
    "只返回 JSON，不要其他内容。"
)

# ── 技能进化相关 system prompt ──────────────────────────────────────────

_EVOLVE_COMPARE_SYSTEM_PROMPT = (
    "你负责技能进化分析。请比较当前用户交互轨迹与已有技能的结构化内容，"
    "判断技能的哪些部分需要进化。\n\n"
    "## 技能结构化内容的模块\n"
    "1. metadata（元数据）\n"
    "2. overview（概述）\n"
    "3. trigger_conditions（触发条件）\n"
    "4. workflow_steps（工作流程）\n"
    "5. script_template（脚本模板）\n"
    "6. notes（注意事项）\n"
    "7. output_format（输出格式）\n"
    "8. common_keywords（常见关键词）\n\n"
    "## 判断规则\n"
    "- 如果当前轨迹展示了已有技能未覆盖的新能力、新步骤或新场景，"
    "则对应模块需要进化\n"
    "- 如果当前轨迹与已有技能内容高度一致，则不需要进化\n"
    "- 优先关注 workflow_steps、trigger_conditions、overview 的变化\n"
    "- metadata 中的 name 通常不需要改变，但 description 可能需要更新\n\n"
    "请返回 JSON 格式：\n"
    '{"need_evolve": true/false, '
    '"modules_to_evolve": ["模块名1", "模块名2", ...], '
    '"reason": "简述每个模块需要进化的原因"}\n\n'
    "modules_to_evolve 只包含需要进化的模块名，可选值为："
    "metadata, overview, trigger_conditions, workflow_steps, "
    "script_template, notes, output_format, common_keywords\n\n"
    "只返回 JSON，不要其他内容。"
)

_EVOLVE_BATCH_SYSTEM_PROMPT = (
    "你负责技能内容进化。请根据当前用户交互轨迹，"
    "对技能结构化内容中指定的模块进行批量进化更新。\n\n"
    "## 进化原则\n"
    "1. **保留原有有效内容**：进化是在原有基础上增强，不是重写\n"
    "2. **融合新信息**：将当前轨迹中的新能力、新步骤、新场景融合到原有内容中\n"
    "3. **保持一致性**：进化后的各模块之间应保持逻辑一致\n"
    "4. **不改变结构**：每个模块的数据结构保持不变（如 workflow_steps 仍是数组）\n"
    "5. **metadata.name 不变**：技能的 slug 名称不要改变\n"
    "6. **version 递增**：将 metadata.version 的小版本号 +1（如 0.1.0 → 0.2.0）\n\n"
    "请返回进化后的**完整**结构化 JSON 对象（包含所有 8 个模块，"
    "不仅是需要进化的模块），不要包裹在代码块中，不要输出其他内容。"
)

_EVOLVE_CONFLICT_SYSTEM_PROMPT = (
    "你负责技能内容审查。请检查进化后的技能结构化内容是否存在逻辑冲突。\n\n"
    "## 检查维度\n"
    "1. **步骤一致性**：workflow_steps 中的步骤顺序是否合理，是否有遗漏或重复\n"
    "2. **触发条件匹配**：trigger_conditions 是否与 workflow_steps 的能力匹配\n"
    "3. **概述准确**：overview 的描述是否与实际内容一致\n"
    "4. **脚本与步骤对应**：script_template 是否与 workflow_steps 中的代码示例一致\n"
    "5. **注意事项覆盖**：notes 是否覆盖了已知的错误场景和边界条件\n"
    "6. **输出格式合理**：output_format 是否与工作流的最终输出一致\n"
    "7. **关键词覆盖**：common_keywords 是否覆盖了触发条件和概述中的关键概念\n\n"
    "## 冲突类型\n"
    "- 步骤顺序错误（如先输出后处理）\n"
    "- 描述与内容矛盾（如概述说支持某功能但步骤中没有）\n"
    "- 代码示例与脚本模板不一致\n"
    "- 触发条件过于宽泛或过于狭窄\n\n"
    "请返回 JSON 格式：\n"
    '{"has_conflict": true/false, '
    '"conflicts": ["冲突描述1", "冲突描述2", ...], '
    '"fixed_content": "修复后的完整JSON字符串（无冲突时为空字符串）"}\n\n'
    "如果有冲突，请在 fixed_content 中返回修复后的**完整**结构化 JSON 对象。"
    "只返回 JSON，不要其他内容。"
)


# ── 工具类别映射（用于动态阶段划分）──
_ANALYSIS_TOOLS = frozenset({
    "search_file", "search_files", "search_content",
    "read_file", "file_read", "list_dir", "list_files",
    "codebase_search", "semantic_search", "grep",
    "web_search", "web_fetch", "fetch_url",
})
_EXECUTION_TOOLS = frozenset({
    "terminal", "execute_command", "run_command",
})
_WRITE_TOOLS = frozenset({
    "write_to_file", "file_write", "replace_in_file",
    "delete_file", "file_edit",
})

# 类别 → (中文标题, 描述)
_CATEGORY_TITLES: dict[str, tuple[str, str]] = {
    "分析": ("搜索与分析信息", "搜索和读取相关信息，分析用户需求与可用数据。"),
    "执行": ("执行命令或脚本", "执行终端命令或脚本完成核心任务。"),
    "写入": ("写入与修改文件", "创建或修改文件内容，保存工作成果。"),
}


class SkillGenerator:
    """根据轨迹信息生成新 skill 提案并创建 SKILL.md。"""

    def __init__(
        self,
        log_store: EvolutionLogStore | None = None,
        llm_provider: Any | None = None,
    ):
        self._log_store = log_store or EvolutionLogStore()
        self._llm = llm_provider

    # ── 提案生成 ──────────────────────────────────────────────────────────

    def propose(
        self,
        min_queries: int = 2,
        max_proposals: int = 5,
        current_session_id: str | None = None,
        evolution_suggestions: list[OptimizationSuggestion] | None = None,
        conversation_id: str = "",
        session_id: str = "",
    ) -> list[SkillProposal]:
        """从历史轨迹中生成新 skill 提案。

        Args:
            min_queries: 意图至少出现 N 次才考虑生成 skill。
                         当 current_session_id 非空时自动降为 1，
                         因为当前会话的单次复杂交互也值得固化为 skill，
                         语义评估会过滤掉简单问答。
            max_proposals: 最多生成 N 个提案
            current_session_id: 当前会话 ID，传入时以当前会话查询为主、历史为辅
            evolution_suggestions: 传入时，与上一轮关联的聚类（独立性 < 0.7）
                                   会生成进化建议追加到此列表，而非创建新提案。
                                   不传时仅生成新 skill 提案。
            conversation_id: 主会话 ID，用于 evolution_log 文件命名
            session_id: 当前会话/侧链 ID，用于 evolution_log 文件命名

        跨轮次关联感知机制：
          - 检测本轮每个聚类与上一轮交互的关联性
          - 独立性 >= 0.7：创建新 skill（正常流程）
          - 独立性 < 0.7：进化已有 skill（生成 evolve 建议而非新提案）
        """
        # 以当前会话为主时，允许单条查询即可生成提案（语义评估会把关质量）
        if current_session_id:
            min_queries = min(min_queries, 1)

        all_queries = self._collect_user_queries(current_session_id=current_session_id)
        clusters = self._cluster_queries(all_queries)

        # 当指定了当前会话时，只处理本轮接收了新查询的簇，
        # 跳过未变化的旧簇（上一轮已处理过，避免重复 LLM 调用）
        if current_session_id:
            filtered = [c for c in clusters if c.has_new_queries]
            skipped = len(clusters) - len(filtered)
            if skipped > 0:
                logger.info(
                    "技能生成: 跳过 %d 个未变化的聚类（本轮无新查询加入）",
                    skipped,
                )
            clusters = filtered

        logger.info(
            "技能生成: 收集到 %d 条查询, 聚类为 %d 个簇",
            len(all_queries),
            len(clusters),
        )

        existing_keywords = self._get_existing_skill_keywords()

        # 获取上一轮交互的轨迹日志（用于跨轮次关联检测）
        previous_logs: list[TrajectoryLog] = []
        if current_session_id:
            previous_logs = self._log_store.get_previous_session_logs(
                current_session_id, limit=5
            )
            if previous_logs:
                logger.info(
                    "跨轮次关联检测: 获取到 %d 条上一轮轨迹日志",
                    len(previous_logs),
                )

        proposals: list[SkillProposal] = []
        evolved_count = 0

        for cluster in clusters:
            if len(cluster.queries) < min_queries:
                logger.debug(
                    "跳过聚类「%s」: 查询数 %d < %d",
                    cluster.display_name[:40],
                    len(cluster.queries),
                    min_queries,
                )
                continue

            # ── 聚类级 skill 关联检测 ──
            # 如果聚类本身已有关联的 skill（从上一轮创建回写到聚类摘要），
            # 直接触发进化而非创建，无需依赖会话级轨迹日志。
            # 这解决了重启后会话状态丢失导致 prev_skills 为空的问题。
            # 新查询进入已有类时，始终触发进化，由 LLM 判断是否需要进化。
            if cluster.skills_activated:
                from crew.agent.skills import resolve_skill_any
                evolve_slug: str | None = None
                for slug in cluster.skills_activated:
                    if resolve_skill_any(slug):
                        evolve_slug = slug
                        break
                if evolve_slug:
                    cluster_assoc = CrossRoundAssociation(
                        current_query=cluster.representative[:200],
                        related_skill_slug=evolve_slug,
                        association_score=0.8,
                        independence_score=0.2,
                        association_type="same_domain",
                        action="evolve",
                        reason="聚类已关联已创建的 skill（从聚类摘要回写），直接触发进化",
                    )
                    logger.info(
                        "聚类「%s」已关联 skill %s（聚类摘要回写），触发进化",
                        cluster.display_name[:40],
                        evolve_slug,
                    )
                    if evolution_suggestions is not None:
                        evolve_sug = self._build_evolve_suggestion(
                            cluster, cluster_assoc,
                            conversation_id=conversation_id,
                            session_id=session_id,
                        )
                        if evolve_sug:
                            evolution_suggestions.append(evolve_sug)
                            evolved_count += 1
                        continue
                    # 进化建议生成失败，回退到创建流程
                    logger.info(
                        "聚类「%s」聚类级进化建议生成失败，回退到创建流程",
                        cluster.display_name[:40],
                    )

            # ── 跨轮次关联检测 ──
            # 如果本轮聚类与上一轮交互高度关联（独立性 < 0.7），
            # 则进化已有 skill 而非创建新 skill
            if previous_logs:
                # 使用当前会话的查询（而非聚类 representative）做关联检测，
                # 避免持久化簇中上一轮查询作为 current_query 与自身对比
                # 从上一轮轨迹日志中提取 log_ids 用于匹配
                previous_log_ids = {log.log_id for log in previous_logs if log.log_id}
                current_query_for_assoc: str | None = None
                if previous_log_ids:
                    for q, lid in cluster.queries:
                        if lid in previous_log_ids:
                            current_query_for_assoc = q
                            break
                association = self._detect_cross_round_association(
                    cluster, previous_logs, existing_keywords,
                    current_query=current_query_for_assoc,
                )
                if association and association.independence_score < 0.7:
                    logger.info(
                        "聚类「%s」与上一轮关联（独立性=%.2f, 类型=%s），触发进化而非创建",
                        cluster.display_name[:40],
                        association.independence_score,
                        association.association_type,
                    )
                    if evolution_suggestions is not None:
                        evolve_sug = self._build_evolve_suggestion(
                            cluster, association,
                            conversation_id=conversation_id,
                            session_id=session_id,
                        )
                        if evolve_sug:
                            evolution_suggestions.append(evolve_sug)
                            evolved_count += 1
                        continue  # 已尝试进化（成功或 skipped），跳过新 skill 创建
                    # 进化建议生成失败（如缺少 related_skill_slug），
                    # 回退到创建新 skill 流程而非直接跳过
                    logger.info(
                        "聚类「%s」进化建议生成失败，回退到创建流程",
                        cluster.display_name[:40],
                    )

            if self._is_covered_by_existing(cluster, existing_keywords):
                logger.info(
                    "跳过聚类「%s」: 已被现有 skill 覆盖",
                    cluster.display_name[:40],
                )
                continue

            # 语义级判断：让 LLM 评估该聚类是否值得固化为 skill
            verdict: _SemanticVerdict | None = None
            if self._llm is not None:
                verdict = self._semantic_evaluate(cluster)
                if not verdict.should_create:
                    logger.info(
                        "语义判断跳过聚类「%s」: %s (confidence=%.2f)",
                        cluster.display_name[:40],
                        verdict.reason,
                        verdict.confidence,
                    )
                    continue

            proposal = self._build_proposal(cluster)
            if proposal:
                if verdict and verdict.reason:
                    proposal.reason += f"；语义判断: {verdict.reason}"
                proposals.append(proposal)
            if len(proposals) >= max_proposals:
                break

        logger.info(
            "技能生成: 生成 %d 个提案, %d 个进化建议",
            len(proposals),
            evolved_count,
        )
        return proposals

    def propose_from_session(self, session_id: str) -> SkillProposal | None:
        """从单个会话轨迹生成 skill 提案（结构化 JSON 格式）。

        将会话数据映射到 SKILL.md 骨架的 8 个模块，生成 structured_content，
        并通过 _assemble_skill_markdown() 拼接为 body 文本。
        """
        logs = self._log_store.list_logs(session_id=session_id)
        if not logs:
            logger.info("会话 %s 无轨迹日志", session_id)
            return None

        log = logs[0]
        user_msgs = [e for e in log.entries if e.role == "user" and e.content]
        if not user_msgs:
            return None

        first_query = user_msgs[0].content[:100]
        name = self._generate_slug(first_query)
        description = f"帮助用户完成：{first_query[:80]}"

        # ── 构建 structured_content ──
        structured: dict[str, Any] = {}

        # 1. metadata
        structured["metadata"] = {
            "name": name,
            "description": description,
            "zh_name": first_query[:30],
            "zh_description": description,
            "query_examples": [first_query],
            "category": "通用办公",
            "version": "0.1.0",
        }

        # 2. overview
        core_caps: list[str] = []
        if log.tool_usage:
            for tool, cnt in sorted(
                log.tool_usage.items(), key=lambda x: -x[1]
            )[:5]:
                core_caps.append(f"使用 {tool} 完成 {cnt} 次操作")
        structured["overview"] = {
            "title": first_query[:50] or "自动生成技能",
            "description": f"该技能用于处理用户关于「{first_query[:60]}」的需求。",
            "core_capabilities": core_caps,
        }

        # 3. trigger_conditions
        structured["trigger_conditions"] = [first_query]

        # 4. workflow_steps（根据轨迹动态确定步骤）
        operations: list[dict[str, Any]] = []
        user_intents: list[str] = []
        if log.structured_summary:
            operations = log.structured_summary.get("operations", [])
            user_intents = log.structured_summary.get("user_intent", [])
        structured["workflow_steps"] = self._extract_workflow_steps_from_trajectory(
            operations=operations or None,
            tool_usage=log.tool_usage or None,
            operation_summary=log.summary or "",
            entries=log.entries,
            user_intents=user_intents or None,
        )

        # 5. script_template
        structured["script_template"] = {}

        # 6. notes
        structured["notes"] = [
            {
                "title": "使用建议",
                "items": [
                    "该技能由 crew.evolution 模块从单会话轨迹自动生成，"
                    "请根据实际使用情况调整",
                ],
            }
        ]

        # 7. output_format
        structured["output_format"] = {
            "description": "根据用户需求返回结构化的结果。",
            "template": "",
        }

        # 8. common_keywords
        structured["common_keywords"] = [first_query[:30]]

        # ── 拼接 body 文本（向后兼容）──
        body = self._assemble_skill_markdown(structured)

        return SkillProposal(
            proposed_name=name,
            proposed_slug=name,
            description=description,
            zh_name=first_query[:30],
            zh_description=description,
            query_examples=[first_query],
            category="通用办公",
            body=body,
            structured_content=structured,
            source_trajectories=[log.log_id],
            source_queries=[first_query],
            reason=f"从会话 {session_id} 的轨迹中提取",
        )

    # ── 创建 skill ────────────────────────────────────────────────────────

    def create(
        self,
        proposal: SkillProposal,
        conversation_id: str = "",
        session_id: str = "",
    ) -> str | None:
        """根据提案创建新 SKILL.md，返回创建路径。

        仅使用 LLM 生成结构化技能内容（步骤重建 + 骨架映射），
        LLM 不可用或生成失败时直接返回 None，不执行内容生成回退。

        Args:
            conversation_id: 主会话 ID，用于 evolution_log 文件命名
            session_id: 当前会话/侧链 ID，用于 evolution_log 文件命名
        """
        from crew.agent.skills import (
            get_user_skills_dir,
            _slugify,
        )

        # 过滤轨迹内容，只保留与目标 skill 相关的条目
        relevant_trajectory = self._filter_relevant_trajectory(proposal)

        # 优先使用 LLM 生成结构化技能内容（步骤重建 + 骨架映射）
        content: str | None = None
        if self._llm is not None:
            structured = self._generate_structured_skill_content(
                proposal, relevant_trajectory
            )
            if structured:
                content = self._assemble_skill_markdown(structured)
                proposal.structured_content = structured

        if not content:
            logger.warning("LLM 结构化生成失败，跳过 skill 创建")
            return None

        # 从 LLM 生成的结构化内容中提取 skill 名称作为 slug
        # 优先使用 LLM 生成的 metadata.name，其次回退到提案中的 slug
        llm_name = ""
        if proposal.structured_content:
            meta = proposal.structured_content.get("metadata", {})
            llm_name = meta.get("name", "")
        slug = _slugify(llm_name) if llm_name else (
            proposal.proposed_slug or _slugify(proposal.proposed_name)
        )
        if not slug:
            logger.warning("无法生成有效 slug")
            return None

        skill_dir = get_user_skills_dir() / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"

        if skill_md.exists():
            logger.warning("skill %s 已存在: %s", slug, skill_md)
            return None

        skill_md.write_text(content, encoding="utf-8")

        # 将结构化内容以 JSON 格式持久化到 skill 目录下的 evolution_log 文件夹
        # 使用会话ID组合命名（{conversation_id}_{session_id}_{timestamp}.json），
        # 并在 version_history.json 中按进化历史顺序记录文件名
        if proposal.structured_content:
            evo_log_dir = skill_dir / "evolution_log"
            filename = self._write_evolution_json(
                evo_log_dir,
                proposal.structured_content,
                conversation_id=conversation_id,
                session_id=session_id,
            )
            logger.info(
                "结构化内容已写入: %s/%s (会话=%s, session=%s)",
                evo_log_dir,
                filename,
                conversation_id,
                session_id,
            )

        proposal.created = True
        proposal.created_path = str(skill_md)

        # 将新创建的 skill slug 回写到源轨迹日志的 skills_activated，
        # 使下一轮跨轮次关联检测能直接获取到
        if proposal.source_trajectories:
            updated = self._log_store.add_skill_to_logs(
                proposal.source_trajectories, slug
            )
            if updated:
                logger.info(
                    "将 skill %s 回写到 %d 条源轨迹日志的 skills_activated",
                    slug, updated,
                )

        # 将 skill slug 回写到聚类摘要的 skills_activated，
        # 使下一轮增量聚类时新查询加入此聚类即可直接获取到 skill slug，
        # 无需依赖会话级轨迹日志（重启后会话状态丢失时仍可进化）
        self._add_skill_to_clusters(slug, proposal.source_trajectories)

        logger.info("创建新 skill: %s -> %s", slug, skill_md)
        return str(skill_md)

    def create_from_session(self, session_id: str) -> str | None:
        """从单个会话直接生成并创建 skill。"""
        proposal = self.propose_from_session(session_id)
        if not proposal:
            return None
        return self.create(
            proposal,
            conversation_id=session_id,
            session_id=session_id,
        )

    # ── 轨迹过滤 ──────────────────────────────────────────────────────────

    def _filter_relevant_trajectory(
        self, proposal: SkillProposal
    ) -> list[dict[str, Any]]:
        """过滤轨迹内容，只保留与目标 skill 相关的条目。

        根据 proposal.source_queries 提取关键词，只保留包含这些关键词的
        用户消息及其后续助手消息（含工具调用）。
        """
        relevant_entries: list[dict[str, Any]] = []

        # 从 source_queries 提取关键词
        keywords: set[str] = set()
        for q in proposal.source_queries:
            keywords.update(self._extract_keywords(q))
        if not keywords:
            return relevant_entries

        for log_id in proposal.source_trajectories:
            log = self._log_store.load(log_id)
            if not log:
                continue

            keep_next_assistant = False
            for entry in log.entries:
                if entry.role == "user" and entry.content:
                    entry_keywords = set(self._extract_keywords(entry.content))
                    if entry_keywords & keywords:
                        relevant_entries.append({
                            "role": "user",
                            "content": entry.content[:500],
                            "session_id": log.session_id,
                        })
                        keep_next_assistant = True
                    else:
                        keep_next_assistant = False
                elif entry.role == "assistant" and keep_next_assistant:
                    relevant_entries.append({
                        "role": "assistant",
                        "content": (entry.content or "")[:300],
                        "tool_calls": entry.tool_calls[:3] if entry.tool_calls else [],
                        "session_id": log.session_id,
                    })

        return relevant_entries

    # ── 结构化技能内容生成 & 拼接 ─────────────────────────────────────────

    def _generate_structured_skill_content(
        self,
        proposal: SkillProposal,
        relevant_trajectory: list[dict[str, Any]],
        cluster: QueryCluster | None = None,
    ) -> dict[str, Any] | None:
        """使用 LLM 从用户交互轨迹重建优化后的步骤，映射到技能骨架各部分。

        流程：
          1. 将提案信息 + 过滤后的轨迹内容组装为 prompt
          2. LLM 分析轨迹，重新构建最优工作流程步骤
          3. 将内容映射到 metadata / overview / trigger_conditions /
             workflow_steps / script_template / notes / output_format /
             common_keywords 等模块
          4. 返回结构化 JSON dict，每个工作流步骤独立存储

        LLM 不可用或调用失败时返回 None，调用方停止创建。
        """
        if self._llm is None:
            return None

        trajectory_text = json.dumps(
            relevant_trajectory, ensure_ascii=False, indent=2
        )
        query_examples_str = "\n".join(
            f"  - {q}" for q in proposal.query_examples[:5]
        )

        # 聚类操作统计（如有）
        cluster_info = ""
        if cluster:
            stats = self._collect_cluster_trajectory_stats(cluster)
            tools_str = ", ".join(
                f"{name}({cnt})" for name, cnt in
                sorted(stats["tool_usage"].items(), key=lambda x: -x[1])[:10]
            ) or "无"
            cluster_info = (
                f"\n## 聚类操作统计\n"
                f"- 关联轨迹数: {stats['trajectory_count']}\n"
                f"- 总消息数: {stats['total_messages']}\n"
                f"- 总工具调用数: {stats['total_tool_calls']}\n"
                f"- 错误率: {stats['error_rate']:.1%}\n"
                f"- 使用的工具: {tools_str}\n"
                f"- 已激活技能: {', '.join(stats['skills_activated']) or '无'}\n"
            )
            if cluster.trajectory_summaries:
                summaries = "\n".join(
                    f"  - {s[:200]}" for s in cluster.trajectory_summaries[:3]
                )
                cluster_info += f"- 轨迹摘要:\n{summaries}\n"

        prompt = (
            f"请根据以下信息生成结构化的技能内容 JSON。\n\n"
            f"## 技能提案\n"
            f"- 中文名: {proposal.zh_name}\n"
            f"- 描述: {proposal.description}\n"
            f"- 中文描述: {proposal.zh_description}\n"
            f"- 查询示例:\n{query_examples_str}\n"
            f"- 分类: {proposal.category}\n"
            f"- 生成原因: {proposal.reason}\n"
            f"{cluster_info}\n"
            f"## 相关轨迹内容（已过滤无关条目）\n"
            f"{trajectory_text}\n\n"
            f"请分析以上用户交互轨迹，重新构建优化后的工作流程步骤，"
            f"并将内容映射到技能骨架的各个部分。\n"
            f"**metadata.name 请根据技能功能生成简洁有意义的英文 slug**"
            f"（如 pdf-merge, excel-data-clean, git-branch-cleanup），"
            f"不要使用 auto-skill-xxx 或 pending-skill-xxx 这类无意义名称。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_STRUCTURED_SKILL_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)

            text = ""
            if hasattr(resp, "text"):
                text = resp.text or ""
            elif isinstance(resp, str):
                text = resp

            text = text.strip()
            if not text:
                logger.warning("LLM 结构化生成返回空文本")
                return None

            data = _extract_json_from_text(text)
            if not isinstance(data, dict):
                preview = text[:500] if len(text) > 500 else text
                logger.warning(
                    "LLM 返回的 JSON 不是 dict 对象，原始响应前500字符:\n%s", preview
                )
                return None

            # 合并提案中的元数据（LLM 输出优先，但补充缺失字段）
            self._merge_metadata(data, proposal)
            return data

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("结构化技能内容 JSON 解析失败: %s", exc)
            return None
        except Exception as exc:
            logger.warning("结构化技能内容生成失败: %s", exc)
            return None

    @staticmethod
    def _merge_metadata(
        data: dict[str, Any], proposal: SkillProposal
    ) -> None:
        """将提案中的元数据合并到 LLM 生成的结构化内容中（LLM 输出优先）。"""
        meta = data.setdefault("metadata", {})
        if not meta.get("name"):
            meta["name"] = proposal.proposed_name or proposal.proposed_slug
        if not meta.get("description"):
            meta["description"] = proposal.description
        if not meta.get("zh_name"):
            meta["zh_name"] = proposal.zh_name
        if not meta.get("zh_description"):
            meta["zh_description"] = proposal.zh_description
        if not meta.get("query_examples"):
            meta["query_examples"] = proposal.query_examples
        if not meta.get("category"):
            meta["category"] = proposal.category
        if not meta.get("version"):
            meta["version"] = "0.1.0"

    @staticmethod
    def _assemble_skill_markdown(
        structured: dict[str, Any],
    ) -> str:
        """将结构化 JSON 按 SKILL.md 逻辑顺序拼接为完整文本。

        拼接顺序：
          1. YAML frontmatter（从 metadata 生成）
          2. # 标题（从 overview.title）
          3. ## 概述（从 overview）
          4. ## 触发条件（从 trigger_conditions）
          5. ## 工作流程（从 workflow_steps，每步为 ### 第N步）
          6. ## 脚本模板（从 script_template）
          7. ## 注意事项（从 notes）
          8. ## 输出格式（从 output_format）
          9. ## 常见关键词（从 common_keywords）
        """
        parts: list[str] = []

        # ── 1. YAML frontmatter ──
        meta = structured.get("metadata", {})
        if meta:
            fm_lines = ["---"]
            if meta.get("name"):
                fm_lines.append(f"name: {meta['name']}")
            if meta.get("description"):
                # description 可能含特殊字符，用引号包裹
                desc = meta["description"]
                fm_lines.append(f'description: "{desc}"')
            fm_lines.append("homepage: ")
            # metadata 子字段
            sub_meta: dict[str, Any] = {}
            if meta.get("version"):
                sub_meta["version"] = str(meta["version"])
            if meta.get("emoji"):
                sub_meta["crew"] = {"emoji": str(meta["emoji"])}
            if meta.get("zh_name"):
                sub_meta["zh_name"] = meta["zh_name"]
            if meta.get("zh_description"):
                sub_meta["zh_description"] = meta["zh_description"]
            if meta.get("query_examples"):
                sub_meta["query_examples"] = meta["query_examples"]
            if meta.get("category"):
                sub_meta["skillCategoryName"] = meta["category"]
            sub_meta["generated_by"] = "crew.evolution"
            if sub_meta:
                fm_lines.append("metadata:")
                for k, v in sub_meta.items():
                    if isinstance(v, dict):
                        fm_lines.append(f"  {k}:")
                        for sk, sv in v.items():
                            fm_lines.append(f"    {sk}: {sv}")
                    elif isinstance(v, list):
                        fm_lines.append(f"  {k}:")
                        for item in v:
                            fm_lines.append(f"    - {item}")
                    else:
                        fm_lines.append(f"  {k}: {v}")
            fm_lines.append("---")
            parts.append("\n".join(fm_lines))
            parts.append("")

        # ── 2. 标题 ──
        overview = structured.get("overview", {})
        if not isinstance(overview, dict):
            overview = {"description": str(overview)} if overview else {}
        title = overview.get("title", "")
        if title:
            parts.append(f"# {title}")
            parts.append("")

        # ── 3. 概述 ──
        if overview:
            parts.append("## 概述")
            parts.append("")
            desc = overview.get("description", "")
            if desc:
                parts.append(desc)
                parts.append("")
            caps = overview.get("core_capabilities", [])
            if caps:
                parts.append("**核心能力：**")
                for cap in caps:
                    parts.append(f"- {cap}")
                parts.append("")

        # ── 4. 触发条件 ──
        triggers = structured.get("trigger_conditions", [])
        if triggers:
            parts.append("## 触发条件")
            parts.append("")
            parts.append("当用户请求以下任务时使用本技能：")
            parts.append("")
            for t in triggers:
                parts.append(f"- {t}")
            parts.append("")

        # ── 5. 工作流程 ──
        steps = structured.get("workflow_steps", [])
        if steps:
            parts.append("## 工作流程")
            parts.append("")
            for step_data in steps:
                # LLM 可能返回字符串而非 dict
                if isinstance(step_data, str):
                    parts.append(step_data)
                    parts.append("")
                    continue
                if not isinstance(step_data, dict):
                    continue
                step_num = step_data.get("step", 0)
                step_title = step_data.get("title", "")
                header = f"### 第{step_num}步：{step_title}" if step_title else f"### 第{step_num}步"
                parts.append(header)
                parts.append("")

                step_desc = step_data.get("description", "")
                if step_desc:
                    parts.append(step_desc)
                    parts.append("")

                sub_steps = step_data.get("sub_steps", [])
                if sub_steps:
                    for ss in sub_steps:
                        parts.append(f"- {ss}")
                    parts.append("")

                code_examples = step_data.get("code_examples", [])
                for ce in code_examples:
                    if isinstance(ce, str):
                        parts.append("```")
                        parts.append(ce)
                        parts.append("```")
                        parts.append("")
                        continue
                    if not isinstance(ce, dict):
                        continue
                    lang = ce.get("language", "")
                    platform = ce.get("platform", "")
                    code = ce.get("code", "")
                    if platform:
                        parts.append(f"**{platform} 示例：**")
                    parts.append(f"```{lang}")
                    parts.append(code)
                    parts.append("```")
                    parts.append("")

        # ── 6. 脚本模板 ──
        script = structured.get("script_template", {})
        if script:
            parts.append("## 脚本模板")
            parts.append("")
            # LLM 可能返回字符串而非 dict
            if isinstance(script, str):
                parts.append("```python")
                parts.append(script)
                parts.append("```")
                parts.append("")
            elif isinstance(script, dict):
                script_desc = script.get("description", "")
                if script_desc:
                    parts.append(script_desc)
                    parts.append("")
                script_lang = script.get("language", "python")
                script_code = script.get("code", "")
                if script_code:
                    parts.append(f"```{script_lang}")
                    parts.append(script_code)
                    parts.append("```")
                    parts.append("")

        # ── 7. 注意事项 ──
        notes = structured.get("notes", [])
        if notes:
            parts.append("## 注意事项")
            parts.append("")
            for note in notes:
                # LLM 可能返回字符串而非 dict
                if isinstance(note, str):
                    parts.append(f"- {note}")
                    continue
                if not isinstance(note, dict):
                    continue
                note_title = note.get("title", "")
                note_items = note.get("items", [])
                if note_title:
                    parts.append(f"### {note_title}")
                    parts.append("")
                for item in note_items:
                    parts.append(f"- {item}")
                parts.append("")

        # ── 8. 输出格式 ──
        output = structured.get("output_format", {})
        if output:
            parts.append("## 输出格式")
            parts.append("")
            # LLM 可能返回字符串而非 dict
            if isinstance(output, str):
                parts.append("```")
                parts.append(output)
                parts.append("```")
                parts.append("")
            elif isinstance(output, dict):
                out_desc = output.get("description", "")
                if out_desc:
                    parts.append(out_desc)
                    parts.append("")
                out_template = output.get("template", "")
                if out_template:
                    parts.append("```")
                    parts.append(out_template)
                    parts.append("```")
                    parts.append("")

        # ── 9. 常见关键词 ──
        keywords = structured.get("common_keywords", [])
        if keywords:
            parts.append("## 常见关键词")
            parts.append("")
            parts.append("当用户提到以下关键词时，优先使用本技能：")
            for kw in keywords:
                parts.append(f"- {kw}")
            parts.append("")

        return "\n".join(parts)

    # ── 动态步骤提取 ──────────────────────────────────────────────────────

    def _extract_workflow_steps_from_trajectory(
        self,
        *,
        operations: list[dict[str, Any]] | None = None,
        tool_usage: dict[str, int] | None = None,
        operation_summary: str = "",
        entries: list[TrajectoryEntry] | None = None,
        user_intents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """根据轨迹数据动态确定工作流步骤。

        步骤数量和内容由轨迹中的实际操作序列决定，不固定为 3 步。
        优先使用 operations 列表（来自 structured_summary）进行阶段划分，
        其次从 entries 中提取工具调用，最后回退到 tool_usage 启发式分组。

        数据源优先级：
          1. operations（来自 TrajectoryLog.structured_summary["operations"]）
          2. entries（TrajectoryLog.entries 中的 tool_calls）
          3. tool_usage（聚类聚合的工具使用统计）
          4. 兜底最小 2 步
        """
        steps: list[dict[str, Any]] = []

        # ── 初始步：理解需求（如有用户意图信息）──
        if user_intents:
            intent_text = " | ".join(user_intents[:3])
            steps.append({
                "step": 0,
                "title": "理解用户需求",
                "description": f"分析用户的具体意图：{intent_text[:150]}",
                "sub_steps": [operation_summary[:200]] if operation_summary else [],
                "code_examples": [],
            })

        # ── 中间步：从操作序列提取 ──
        if operations:
            phases = self._group_operations_into_phases(operations)
            for phase in phases:
                steps.append(self._build_step_from_phase(phase))

        if not steps and entries:
            ops_from_entries = self._extract_ops_from_entries(entries)
            if ops_from_entries:
                phases = self._group_operations_into_phases(ops_from_entries)
                for phase in phases:
                    steps.append(self._build_step_from_phase(phase))

        if not steps and tool_usage:
            steps = self._build_steps_from_tool_usage(tool_usage)

        # ── 兜底：最小 2 步 ──
        if not steps:
            steps = [
                {
                    "step": 0,
                    "title": "分析用户需求",
                    "description": "理解用户的具体意图，确定需要调用的工具和数据。",
                    "sub_steps": [operation_summary[:200]] if operation_summary else [],
                    "code_examples": [],
                },
                {
                    "step": 0,
                    "title": "执行操作并返回结果",
                    "description": "根据分析结果，调用相应工具完成任务并返回。",
                    "sub_steps": [],
                    "code_examples": [],
                },
            ]

        # ── 末尾步：整合结果（operations 只含工具调用，不含最终回复）──
        if steps and not self._is_result_step(steps[-1]):
            steps.append({
                "step": 0,
                "title": "整合结果并返回",
                "description": "将工具调用结果整合为用户可理解的格式并返回。",
                "sub_steps": [],
                "code_examples": [],
            })

        # ── 统一编号 ──
        for i, step in enumerate(steps):
            step["step"] = i + 1

        return steps

    @staticmethod
    def _categorize_tool(tool_name: str) -> str:
        """将工具名映射到操作类别。

        已知工具映射到 分析/执行/写入 三大类，
        未知工具以工具名本身作为类别（同工具归入同阶段）。
        """
        tool = (tool_name or "").lower()
        if tool in _ANALYSIS_TOOLS:
            return "分析"
        if tool in _EXECUTION_TOOLS:
            return "执行"
        if tool in _WRITE_TOOLS:
            return "写入"
        return tool

    @staticmethod
    def _group_operations_into_phases(
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将有序操作列表按类别分组为阶段。

        连续相同类别的操作归入同一阶段，类别变化时开启新阶段。
        例如：search → read → terminal → write → write
        分组为：[分析(search,read)], [执行(terminal)], [写入(write,write)]
        """
        phases: list[dict[str, Any]] = []
        for op in operations:
            category = SkillGenerator._categorize_tool(
                op.get("tool", "")
            )
            if not phases or phases[-1]["category"] != category:
                phases.append({
                    "category": category,
                    "operations": [op],
                })
            else:
                phases[-1]["operations"].append(op)
        return phases

    @staticmethod
    def _build_step_from_phase(
        phase: dict[str, Any],
    ) -> dict[str, Any]:
        """将一个操作阶段转换为工作流步骤 dict。"""
        category = phase["category"]
        ops = phase["operations"]
        title, desc = _CATEGORY_TITLES.get(
            category,
            (category, f"使用 {category} 完成相关操作。"),
        )
        sub_steps: list[str] = []
        for op in ops[:10]:
            tool = op.get("tool", "unknown")
            args = op.get("args_summary", "")
            tag = " [失败]" if op.get("is_error") else ""
            sub_steps.append(f"**{tool}**：{args}{tag}")
        return {
            "step": 0,
            "title": title,
            "description": desc,
            "sub_steps": sub_steps,
            "code_examples": [],
        }

    @staticmethod
    def _extract_ops_from_entries(
        entries: list[TrajectoryEntry],
    ) -> list[dict[str, Any]]:
        """从 TrajectoryEntry 列表中提取操作序列。

        遍历 entries 中的 tool_calls，构建与 structured_summary["operations"]
        结构一致的操作列表，供阶段划分使用。
        """
        ops: list[dict[str, Any]] = []
        for entry in entries:
            if not entry.tool_calls:
                continue
            for tc in entry.tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", "unknown")
                args = tc.get("arguments", {})
                args_str = (
                    json.dumps(args, ensure_ascii=False)[:120]
                    if args else ""
                )
                status = tc.get("status", "")
                is_err = (
                    entry.is_error
                    or status == "error"
                )
                ops.append({
                    "step": len(ops) + 1,
                    "tool": name,
                    "args_summary": args_str,
                    "status": "error" if is_err else "success",
                    "is_error": is_err,
                })
        return ops

    @staticmethod
    def _build_steps_from_tool_usage(
        tool_usage: dict[str, int],
    ) -> list[dict[str, Any]]:
        """从工具使用统计构建步骤（无操作序列时的回退方案）。

        按类别分组，每类工具成为一个步骤。
        """
        by_category: dict[str, list[tuple[str, int]]] = {}
        for tool, cnt in sorted(tool_usage.items(), key=lambda x: -x[1]):
            category = SkillGenerator._categorize_tool(tool)
            by_category.setdefault(category, []).append((tool, cnt))

        steps: list[dict[str, Any]] = []
        for category, tools in by_category.items():
            title, desc = _CATEGORY_TITLES.get(
                category,
                (category, f"使用 {category} 完成相关操作。"),
            )
            sub_steps = [f"**{t}**：{c} 次调用" for t, c in tools[:10]]
            steps.append({
                "step": 0,
                "title": title,
                "description": desc,
                "sub_steps": sub_steps,
                "code_examples": [],
            })
        return steps

    @staticmethod
    def _is_result_step(step: dict[str, Any]) -> bool:
        """判断步骤是否已经是"整合结果"类步骤。"""
        title = step.get("title", "")
        return any(kw in title for kw in ("整合", "返回", "结果", "总结"))

    # ── 语义级判断 ────────────────────────────────────────────────────────

    def _semantic_evaluate(self, cluster: QueryCluster) -> _SemanticVerdict:
        """调用 LLM 评估聚类是否值得固化为 skill。

        评估维度：
          - 任务复杂度：多步工具编排才值得做 skill，简单问答不值得
          - 可复用性：是否为可标准化的重复模式
          - 轨迹质量：成功轨迹占比高才适合做模板
          - 与已有 skill 的语义区分度

        LLM 不可用或调用失败时，回退到通过（不阻断生成）。
        """
        if self._llm is None:
            return _SemanticVerdict(should_create=True, reason="LLM 不可用，跳过语义判断", confidence=0.0)

        traj_stats = self._collect_cluster_trajectory_stats(cluster)
        prompt = self._build_eval_prompt(cluster, traj_stats)

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_EVAL_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)
            return self._parse_eval_response(resp)
        except Exception as exc:
            logger.warning("语义判断失败，回退为通过: %s", exc)
            return _SemanticVerdict(
                should_create=True,
                reason=f"语义判断异常，默认通过: {exc}",
                confidence=0.0,
            )

    def _extract_trajectory_meta(self, log_id: str) -> dict[str, Any]:
        """从单条轨迹日志中提取操作元数据。

        返回的 dict 可直接传给 QueryCluster.add() 的 meta 参数。
        日志不存在时返回空 dict（仍会计入 trajectory_count）。
        """
        log = self._log_store.load(log_id)
        if not log:
            return {}
        total_calls = sum(log.tool_usage.values())
        return {
            "tool_usage": dict(log.tool_usage),
            "error_count": log.error_count,
            "total_tool_calls": total_calls,
            "total_messages": log.message_count,
            "skills_activated": list(log.skills_activated),
            "summary": log.summary,
        }

    def _collect_cluster_trajectory_stats(self, cluster: QueryCluster) -> dict[str, Any]:
        """收集聚类关联轨迹的统计信息。

        优先使用聚类中已持久化的操作元数据（trajectory_count > 0 表示有元数据），
        避免重复加载轨迹日志。旧版持久化聚类无元数据时回退到逐条加载日志。
        """
        # 优先使用聚类中已聚合的元数据
        if cluster.trajectory_count > 0:
            return {
                "trajectory_count": cluster.trajectory_count,
                "total_messages": cluster.total_messages,
                "total_errors": cluster.error_count,
                "total_tool_calls": cluster.total_tool_calls,
                "tool_usage": dict(cluster.tool_usage),
                "skills_activated": list(cluster.skills_activated),
                "trajectory_summaries": list(cluster.trajectory_summaries),
                # 错误率 = 失败工具调用数 / 总工具调用数
                "error_rate": cluster.error_count / max(cluster.total_tool_calls, 1),
            }

        # 回退：旧版持久化聚类无元数据，逐条加载日志
        log_ids = {lid for _, lid in cluster.queries}
        total_messages = 0
        total_errors = 0
        total_tool_calls = 0
        tool_usage: dict[str, int] = {}
        skills_activated: set[str] = set()
        trajectory_count = 0

        for log_id in log_ids:
            log = self._log_store.load(log_id)
            if not log:
                continue
            trajectory_count += 1
            total_messages += log.message_count
            total_errors += log.error_count
            for tool, cnt in log.tool_usage.items():
                tool_usage[tool] = tool_usage.get(tool, 0) + cnt
                total_tool_calls += cnt
            skills_activated.update(log.skills_activated)

        return {
            "trajectory_count": trajectory_count,
            "total_messages": total_messages,
            "total_errors": total_errors,
            "total_tool_calls": total_tool_calls,
            "tool_usage": tool_usage,
            "skills_activated": sorted(skills_activated),
            "error_rate": total_errors / max(total_tool_calls, 1),
        }

    @staticmethod
    def _build_eval_prompt(cluster: QueryCluster, stats: dict[str, Any]) -> str:
        """构建语义评估 prompt。"""
        query_examples = [q for q, _ in cluster.queries[:8]]
        tools_str = ", ".join(
            f"{name}({cnt})" for name, cnt in
            sorted(stats["tool_usage"].items(), key=lambda x: -x[1])[:10]
        ) or "无"

        return (
            f"## 用户查询聚类\n"
            f"聚类名称: {cluster.display_name[:200]}\n"
            f"查询示例 ({len(query_examples)} 条):\n"
            + "\n".join(f"  - {q[:120]}" for q in query_examples)
            + f"\n\n## 轨迹统计\n"
            f"关联轨迹数: {stats['trajectory_count']}\n"
            f"总消息数: {stats['total_messages']}\n"
            f"总工具调用数: {stats['total_tool_calls']}\n"
            f"错误率: {stats['error_rate']:.1%}\n"
            f"使用的工具: {tools_str}\n"
            f"已激活技能: {', '.join(stats['skills_activated']) or '无'}\n\n"
            f"请评估这个查询聚类是否值得固化为一个新 skill。"
        )

    @staticmethod
    def _parse_eval_response(resp: Any) -> _SemanticVerdict:
        """解析 LLM 返回的评估结果。

        期望 LLM 返回 JSON:
        {"should_create": true/false, "confidence": 0.0-1.0, "reason": "..."}
        """
        text = ""
        if hasattr(resp, "text"):
            text = resp.text or ""
        elif isinstance(resp, str):
            text = resp

        text = text.strip()
        # 尝试提取 JSON（LLM 可能包裹在 ```json ... ``` 中或包含对话文本）
        data = _extract_json_from_text(text)
        if data:
            try:
                return _SemanticVerdict(
                    should_create=bool(data.get("should_create", True)),
                    confidence=float(data.get("confidence", 0.5)),
                    reason=str(data.get("reason", "")),
                )
            except (TypeError, ValueError):
                pass

        # JSON 解析失败，回退：如果文本包含"否"/"不建议"/"不需要"则判定为不创建
        text_lower = text.lower()
        negative_signals = ("不建议", "不需要", "不值得", "否", "不创建", "should not", "no", "false")
        if any(sig in text_lower for sig in negative_signals):
            return _SemanticVerdict(
                should_create=False,
                confidence=0.3,
                reason=f"LLM 判定不建议创建: {text[:100]}",
            )
        return _SemanticVerdict(
            should_create=True,
            confidence=0.5,
            reason=f"LLM 判定建议创建: {text[:100]}",
        )

    async def _stream_chat_full(self, messages: list[Any]) -> ChatResponse:
        """流式调用 LLM，收集完整文本后返回 ChatResponse。

        使用流式接口避免非流式长生成超时问题：
        非流式 chat() 受 60s read timeout 限制，而 skill 生成等复杂 prompt
        需要服务端长时间运算（2-5分钟），极易超时。流式逐 token 返回，
        每次 token 重置 read 超时计时器，且享有 stream_resilience 更长的
        read_timeout 兜底。
        """
        text_parts: list[str] = []
        tool_calls: list[Any] = []
        finish_reason: str | None = None
        reasoning_content = ""

        async for chunk in self._llm.stream_chat(messages):
            if chunk.delta_text:
                text_parts.append(chunk.delta_text)
            if chunk.reasoning_content:
                reasoning_content += chunk.reasoning_content
            if chunk.done and chunk.tool_calls:
                tool_calls = chunk.tool_calls
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        return ChatResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
        )

    def _run_llm_chat(self, messages: list[Any]) -> Any:
        """同步调用 async LLM stream_chat，兼容线程内和事件循环内调用。"""
        coro = self._stream_chat_full(messages)
        try:
            asyncio.get_running_loop()
            # 已在事件循环中（不应发生在 asyncio.to_thread 路径，但防御性处理）
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        except RuntimeError:
            # 无运行中的事件循环，直接 asyncio.run
            return asyncio.run(coro)

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _collect_user_queries(
        self, current_session_id: str | None = None
    ) -> list[tuple[str, str]]:
        """收集轨迹中的用户查询，返回 [(query, log_id), ...]。

        当 current_session_id 被传入时，只收集当前会话的查询，
        并排除已在之前会话轨迹中出现过的查询文本——因为当前会话的
        轨迹日志包含完整对话历史，会重复包含之前轮次的用户查询。
        无 current_session_id 时收集所有轨迹的查询（全量模式）。

        直接从 index.json 读取 user_queries 冗余字段，无需逐条加载 JSON 正文。
        """
        if current_session_id:
            # 收集之前会话中已出现过的查询文本，用于排除
            previous_queries = self._log_store.list_user_queries(
                exclude_session_id=current_session_id
            )
            previous_query_texts: set[str] = {q for q, _, _ in previous_queries}

            # 只收集当前会话的查询，排除已在之前会话出现过的
            current_queries = self._log_store.list_user_queries(
                session_id=current_session_id
            )
            return [
                (q, lid)
                for q, lid, _ in current_queries
                if q not in previous_query_texts
            ]
        else:
            # 全量模式：收集所有轨迹的查询
            all_queries = self._log_store.list_user_queries()
            return [(q, lid) for q, lid, _ in all_queries]

    def _cluster_queries(self, queries: list[tuple[str, str]]) -> list[QueryCluster]:
        """对用户查询进行聚类。

        聚类摘要持久化机制：
          1. 加载已持久化的聚类摘要（clusters.json）
          2. 找出不在任何持久化簇中的新查询
          3. 若无新查询 → 直接返回持久化簇（零 LLM 调用）
          4. 若有新查询 → 仅对新查询增量聚类（与已有簇摘要比较）
          5. 持久化更新后的聚类摘要

        优先使用 LLM 逐条增量式语义聚类（每条查询与已有簇摘要比较），
        LLM 不可用或失败时回退到 union-find bigram 关键词方法。
        """
        if not queries:
            return []

        # 加载持久化聚类摘要
        persisted_clusters = self._load_persisted_clusters()

        if persisted_clusters:
            # 找出不在任何持久化簇中的新查询
            existing_pairs: set[tuple[str, str]] = set()
            for c in persisted_clusters:
                for q, lid in c.queries:
                    existing_pairs.add((q, lid))
            new_queries = [
                (q, lid) for q, lid in queries if (q, lid) not in existing_pairs
            ]

            if not new_queries:
                # 全部已在持久化簇中，跳过聚类
                logger.info(
                    "聚类摘要持久化: %d 条查询全部已存在，跳过聚类",
                    len(queries),
                )
                persisted_clusters.sort(key=lambda c: -len(c.queries))
                return persisted_clusters

            logger.info(
                "聚类摘要持久化: %d 条总查询, %d 条新查询需增量聚类",
                len(queries),
                len(new_queries),
            )

            # 仅对新查询进行增量聚类
            if self._llm is not None:
                result = self._llm_cluster_queries(
                    new_queries, initial_clusters=persisted_clusters
                )
                if result is not None:
                    logger.info(
                        "LLM 增量聚类完成: %d 条新查询 -> %d 个簇",
                        len(new_queries),
                        len(result),
                    )
                    self._save_clusters(result)
                    return result
                logger.info("LLM 增量聚类不可用或失败，回退到关键词增量聚类")

            # 关键词回退：增量分配到已有簇
            self._keyword_assign_incremental(new_queries, persisted_clusters)
            # 对有新查询的簇生成/更新标签
            self._refresh_cluster_labels(persisted_clusters)
            self._save_clusters(persisted_clusters)
            persisted_clusters.sort(key=lambda c: -len(c.queries))
            return persisted_clusters

        # 无持久化聚类，从全量查询聚类
        if self._llm is not None:
            llm_clusters = self._llm_cluster_queries(queries)
            if llm_clusters is not None:
                logger.info(
                    "LLM 聚类完成: %d 条查询 -> %d 个簇",
                    len(queries),
                    len(llm_clusters),
                )
                self._save_clusters(llm_clusters)
                return llm_clusters
            logger.info("LLM 聚类不可用或失败，回退到关键词聚类")

        clusters = self._keyword_cluster_queries(queries)
        self._save_clusters(clusters)
        return clusters

    # ── 聚类摘要持久化辅助 ──────────────────────────────────────────────

    def _load_persisted_clusters(self) -> list[QueryCluster]:
        """从磁盘加载持久化的聚类摘要，反序列化为 QueryCluster 列表。"""
        raw = self._log_store.load_clusters()
        if not raw:
            return []
        clusters: list[QueryCluster] = []
        for d in raw:
            try:
                clusters.append(QueryCluster.from_dict(d))
            except Exception as exc:
                logger.warning("反序列化聚类摘要失败，跳过: %s", exc)
        logger.debug("加载 %d 个持久化聚类摘要", len(clusters))
        return clusters

    def _save_clusters(self, clusters: list[QueryCluster]) -> None:
        """将聚类摘要序列化并持久化到磁盘。"""
        try:
            self._log_store.save_clusters([c.to_dict() for c in clusters])
        except Exception as exc:
            logger.warning("持久化聚类摘要失败: %s", exc)

    def _add_skill_to_clusters(
        self, skill_slug: str, source_log_ids: list[str]
    ) -> None:
        """将新创建的 skill slug 回写到包含源轨迹的聚类摘要。

        使下一轮增量聚类时，新查询加入此聚类即可直接获取到 skill slug，
        无需依赖会话级轨迹日志（重启后会话状态丢失时仍可进化）。
        """
        if not source_log_ids:
            return
        try:
            clusters = self._load_persisted_clusters()
            if not clusters:
                return
            source_id_set = set(source_log_ids)
            updated = False
            for cluster in clusters:
                # 检查聚类的 queries 中是否包含源轨迹 log_id
                cluster_log_ids = {lid for _, lid in cluster.queries}
                if cluster_log_ids & source_id_set:
                    if skill_slug not in cluster.skills_activated:
                        cluster.skills_activated.append(skill_slug)
                        updated = True
            if updated:
                self._save_clusters(clusters)
                logger.info(
                    "将 skill %s 回写到聚类摘要的 skills_activated",
                    skill_slug,
                )
        except Exception as exc:
            logger.warning("回写 skill slug 到聚类摘要失败: %s", exc)

    def _keyword_assign_incremental(
        self,
        new_queries: list[tuple[str, str]],
        existing_clusters: list[QueryCluster],
    ) -> None:
        """关键词回退：将新查询增量分配到已有簇（就地修改）。

        对每条新查询，与已有簇的代表查询做 bigram 相似度比较：
          - 若与某簇代表相似度 >= 阈值 → 归入该簇
          - 否则 → 新建簇并追加到 existing_clusters

        这是 LLM 不可用时的增量聚类回退方案。
        """
        for q, log_id in new_queries:
            meta = self._extract_trajectory_meta(log_id)
            assigned = False
            for cluster in existing_clusters:
                if self._queries_similar(q, cluster.representative):
                    cluster.add(q, log_id, meta)
                    cluster.has_new_queries = True
                    assigned = True
                    break
            if not assigned:
                c = QueryCluster(q, has_new_queries=True)
                c.add(q, log_id, meta)
                existing_clusters.append(c)

    def _llm_cluster_queries(
        self,
        queries: list[tuple[str, str]],
        initial_clusters: list[QueryCluster] | None = None,
    ) -> list[QueryCluster] | None:
        """使用 LLM 对用户查询进行批量增量式语义聚类。

        策略（批量进入）：
        1. 第一条查询 → 形成第一个簇 c1，摘要 cs1 = 代表查询 + 示例
        2. 后续查询按 _LLM_ASSIGN_BATCH_SIZE 条为一组，一次性发送给 LLM
           判断每条查询属于哪个已有簇或需要新建簇
        3. 批量分配后，归入已有簇的查询直接添加；
           返回 -1 的查询再逐条与更新后的簇比较（处理同批内相似查询）

        每次 LLM 调用的 prompt = K 个簇摘要 + B 条查询（B ≤ _LLM_ASSIGN_BATCH_SIZE）。
        摘要随簇内容动态更新（代表查询 + 最多 3 条示例），无需额外 LLM 调用。

        Args:
            queries: 待聚类的查询列表
            initial_clusters: 已有的持久化聚类摘要，新查询将在此基础上增量分配。
                              传入时，这些簇作为起始簇，新查询批量与它们比较。
        """
        if self._llm is None or not queries:
            return None

        n = len(queries)
        if n > _LLM_CLUSTER_MAX:
            logger.info("查询数 %d 超过 %d，回退到关键词聚类", n, _LLM_CLUSTER_MAX)
            return None

        # 以持久化簇为起始（增量聚类），或从空开始
        clusters: list[QueryCluster] = list(initial_clusters) if initial_clusters else []

        i = 0
        while i < n:
            # 无簇时第一条查询 → 第一个簇
            if not clusters:
                q, log_id = queries[i]
                meta = self._extract_trajectory_meta(log_id)
                c = QueryCluster(q, has_new_queries=True)
                c.add(q, log_id, meta)
                clusters.append(c)
                i += 1
                continue

            # 取一批查询
            batch_end = min(i + _LLM_ASSIGN_BATCH_SIZE, n)
            batch = queries[i:batch_end]

            # 预提取轨迹元数据，供聚类判断使用
            batch_metas = [self._extract_trajectory_meta(log_id) for _, log_id in batch]

            # 批量分配（传入元数据帮助 LLM 基于操作特征判断归属）
            batch_queries = [q for q, _ in batch]
            assignments = self._llm_assign_batch(batch_queries, clusters, batch_metas)

            if assignments is None:
                # 批量调用失败，回退到逐条分配
                assignments = [None] * len(batch)

            # 先处理已分配的查询，收集未分配的（-1）
            unassigned: list[tuple[str, str, dict[str, Any]]] = []
            for j, (q, log_id) in enumerate(batch):
                meta = batch_metas[j]
                cluster_idx = assignments[j] if j < len(assignments) else None

                if cluster_idx is not None and 0 <= cluster_idx < len(clusters):
                    clusters[cluster_idx].add(q, log_id, meta)
                    clusters[cluster_idx].has_new_queries = True
                else:
                    unassigned.append((q, log_id, meta))

            # 未分配的查询逐条与更新后的簇比较（含本批新建的簇）
            for q, log_id, meta in unassigned:
                cluster_idx = self._llm_assign_single(q, clusters, meta)
                if cluster_idx is not None and 0 <= cluster_idx < len(clusters):
                    clusters[cluster_idx].add(q, log_id, meta)
                    clusters[cluster_idx].has_new_queries = True
                else:
                    c = QueryCluster(q, has_new_queries=True)
                    c.add(q, log_id, meta)
                    clusters.append(c)

            i = batch_end
            if i % 20 < _LLM_ASSIGN_BATCH_SIZE or i == n:
                logger.debug(
                    "增量聚类进度 %d/%d，当前 %d 个簇",
                    i, n, len(clusters),
                )

        clusters.sort(key=lambda c: -len(c.queries))
        # 对有新查询的簇生成/更新标签
        self._refresh_cluster_labels(clusters)
        return clusters

    def _generate_cluster_label(self, cluster: QueryCluster) -> str:
        """用 LLM 从聚类内容生成简洁标签。

        输入：用户查询 + 操作摘要 + 轨迹摘要 + 工具成功率。
        输出：8-20 字的中文标签（如「电商用户行为分析」）。
        LLM 不可用或失败时回退到 representative。
        """
        if self._llm is None:
            return cluster.representative

        # 构建聚类内容摘要
        query_examples = [q for q, _ in cluster.queries[:5]]
        parts: list[str] = []
        parts.append("## 用户查询示例")
        for i, q in enumerate(query_examples):
            parts.append(f"  {i+1}. {q[:120]}")

        if cluster.operation_summary:
            parts.append(f"\n## 智能体操作: {cluster.operation_summary}")

        if cluster.trajectory_summaries:
            parts.append("\n## 轨迹摘要")
            for s in cluster.trajectory_summaries[:3]:
                parts.append(f"  - {s[:150]}")

        success_rate = cluster.tool_success_rate
        parts.append(
            f"\n## 执行指标: 工具调用 {cluster.total_tool_calls} 次, "
            f"成功率 {success_rate:.0%}, 轨迹数 {cluster.trajectory_count}"
        )

        if cluster.skills_activated:
            parts.append(f"## 关联技能: {', '.join(cluster.skills_activated[:3])}")

        content = "\n".join(parts)
        prompt = (
            f"以下是聚类的内容摘要：\n\n{content}\n\n"
            f"请生成一个简洁的中文标签（8-20字），概括该聚类的核心意图和操作特征。\n"
            f"返回 JSON。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_CLUSTER_LABEL_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)
            text = ""
            if hasattr(resp, "text"):
                text = resp.text or ""
            elif isinstance(resp, str):
                text = resp
            text = text.strip()
            if not text:
                return cluster.representative
            data = _extract_json_from_text(text)
            if isinstance(data, dict):
                label = data.get("label", "")
                if isinstance(label, str) and label.strip():
                    return label.strip()[:50]
            logger.warning("聚类标签生成: LLM 返回格式异常: %s", text[:200])
        except Exception as exc:
            logger.warning("聚类标签生成失败: %s", exc)

        return cluster.representative

    def _refresh_cluster_labels(self, clusters: list[QueryCluster]) -> None:
        """对有新查询的聚类重新生成标签。

        仅处理 has_new_queries=True 的簇，避免不必要的 LLM 调用。
        """
        if self._llm is None:
            return
        for cluster in clusters:
            if cluster.has_new_queries or not cluster.cluster_label:
                old_label = cluster.cluster_label
                new_label = self._generate_cluster_label(cluster)
                cluster.cluster_label = new_label
                if new_label != old_label and new_label != cluster.representative:
                    logger.debug(
                        "聚类标签更新: 「%s」-> 「%s」",
                        old_label or cluster.representative[:40],
                        new_label,
                    )

    def _build_cluster_summary(
        self,
        clusters: list[QueryCluster],
    ) -> tuple[str, dict[int, int], int]:
        """构建簇摘要文本，返回 (摘要文本, 展示编号→实际索引映射, 展示簇数)。

        按簇大小降序，最多展示 _MAX_SUMMARY_CLUSTERS 个簇。
        """
        sorted_clusters = sorted(
            enumerate(clusters),
            key=lambda x: -len(x[1].queries),
        )
        shown = sorted_clusters[:_MAX_SUMMARY_CLUSTERS]

        idx_map: dict[int, int] = {}
        summary_lines = []
        for display_idx, (real_idx, c) in enumerate(shown):
            idx_map[display_idx] = real_idx
            summary_lines.append(f"[{display_idx}] {c.display_name[:100]}")
            examples = [q for q, _ in c.queries[:3]]
            if len(examples) > 1:
                summary_lines.append(f"    示例：{' | '.join(examples[1:])}")
            # 展示操作信息，帮助 LLM 基于操作特征判断归属
            if c.operation_summary:
                summary_lines.append(f"    操作：{c.operation_summary}")
            elif c.tool_usage:
                tools = ", ".join(
                    f"{name}({cnt})" for name, cnt in
                    sorted(c.tool_usage.items(), key=lambda x: -x[1])[:5]
                )
                summary_lines.append(f"    操作：{tools}")
            # 展示工具调用成功率，帮助 LLM 基于执行质量判断归属
            if c.total_tool_calls > 0:
                summary_lines.append(
                    f"    执行：{c.total_tool_calls} 次调用, 成功率 {c.tool_success_rate:.0%}"
                )
            # 展示轨迹摘要片段，帮助 LLM 理解簇的实际执行内容
            if c.trajectory_summaries:
                snippet = c.trajectory_summaries[0][:200]
                summary_lines.append(f"    轨迹摘要：{snippet}")

        return "\n".join(summary_lines), idx_map, len(shown)

    def _llm_assign_single(
        self,
        query: str,
        clusters: list[QueryCluster],
        meta: dict[str, Any] | None = None,
    ) -> int | None:
        """将单条新查询与已有簇摘要比较，判断归属。

        构建 prompt：已有簇摘要（标签 + 示例 + 操作 + 成功率）+ 新查询（含轨迹元数据）→ LLM 判断。
        返回簇编号（0-based），-1 表示新建簇，None 表示 LLM 调用失败。
        """
        cluster_summary, idx_map, num_shown = self._build_cluster_summary(clusters)

        # 构建新查询的上下文（用户意图 + 智能体操作 + 执行结果）
        query_context = f"新查询：{query[:150]}"
        if meta:
            meta_parts: list[str] = []
            tool_usage = meta.get("tool_usage", {})
            if tool_usage:
                tools = ", ".join(
                    f"{name}({cnt})" for name, cnt in
                    sorted(tool_usage.items(), key=lambda x: -x[1])[:5]
                )
                meta_parts.append(f"工具: {tools}")
            total_calls = meta.get("total_tool_calls", 0)
            error_count = meta.get("error_count", 0)
            if total_calls > 0:
                success_rate = 1.0 - error_count / total_calls
                meta_parts.append(f"调用{total_calls}次, 成功率{success_rate:.0%}")
            summary = meta.get("summary", "")
            if summary:
                meta_parts.append(f"结果: {summary[:150]}")
            if meta_parts:
                query_context += "\n" + "\n".join(f"  {p}" for p in meta_parts)

        prompt = (
            f"以下是已有的查询簇：\n\n{cluster_summary}\n\n"
            f"{query_context}\n\n"
            f"请判断这条新查询属于哪个已有簇（返回簇编号），或 -1 表示需要新建簇。\n"
            f"综合考虑用户意图、智能体操作和执行结果，同一工作流的查询应归为同一簇。\n"
            f"返回 JSON。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_ASSIGN_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)
            display_idx = self._parse_single_assignment(resp, num_shown)
            if display_idx is None:
                return None
            if display_idx == -1:
                return -1
            return idx_map.get(display_idx, -1)
        except Exception as exc:
            logger.warning("LLM 增量分配失败: %s", exc)
            return None

    def _llm_assign_batch(
        self,
        queries: list[str],
        clusters: list[QueryCluster],
        metas: list[dict[str, Any] | None] | None = None,
    ) -> list[int | None] | None:
        """将多条新查询批量与已有簇摘要比较，判断归属。

        一次 LLM 调用处理多条查询，返回每条查询的实际簇索引列表。
        -1 表示新建簇，None 表示该条解析失败或 LLM 调用整体失败。
        metas 为可选的轨迹元数据列表，帮助 LLM 基于操作特征判断归属。
        """
        cluster_summary, idx_map, num_shown = self._build_cluster_summary(clusters)

        query_lines = []
        for i, q in enumerate(queries):
            line = f"[{i}] {q[:150]}"
            # 附加轨迹元数据（用户意图 + 智能体操作 + 执行结果）
            if metas and i < len(metas) and metas[i]:
                meta = metas[i]
                meta_parts: list[str] = []
                tool_usage = meta.get("tool_usage", {})
                if tool_usage:
                    tools = ", ".join(
                        f"{name}({cnt})" for name, cnt in
                        sorted(tool_usage.items(), key=lambda x: -x[1])[:5]
                    )
                    meta_parts.append(f"工具: {tools}")
                total_calls = meta.get("total_tool_calls", 0)
                error_count = meta.get("error_count", 0)
                if total_calls > 0:
                    success_rate = 1.0 - error_count / total_calls
                    meta_parts.append(f"调用{total_calls}次, 成功率{success_rate:.0%}")
                summary = meta.get("summary", "")
                if summary:
                    meta_parts.append(f"结果: {summary[:150]}")
                if meta_parts:
                    line += "\n  " + "\n  ".join(meta_parts)
            query_lines.append(line)
        query_list = "\n".join(query_lines)

        prompt = (
            f"以下是已有的查询簇：\n\n{cluster_summary}\n\n"
            f"以下是待分配的新查询（含轨迹元数据）：\n\n{query_list}\n\n"
            f"请判断每条新查询属于哪个已有簇（返回簇编号），或 -1 表示需要新建簇。\n"
            f"综合考虑用户意图、智能体操作和执行结果，同一工作流的查询应归为同一簇。\n"
            f"返回 JSON 数组。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_BATCH_ASSIGN_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)
            return self._parse_batch_assignment(
                resp, num_shown, len(queries), idx_map
            )
        except Exception as exc:
            logger.warning("LLM 批量分配失败: %s", exc)
            return None

    @staticmethod
    def _parse_single_assignment(
        resp: Any,
        num_clusters: int,
    ) -> int | None:
        """解析 LLM 返回的单条查询分配结果。

        期望 JSON：{"cluster": 0} 或 {"cluster": -1}
        也兼容直接返回数字的情况。
        """
        text = ""
        if hasattr(resp, "text"):
            text = resp.text or ""
        elif isinstance(resp, str):
            text = resp

        text = text.strip()
        if not text:
            return None

        # 尝试提取 JSON 对象
        data = _extract_json_from_text(text)
        if isinstance(data, dict):
            cluster = data.get("cluster")
            if isinstance(cluster, int):
                if cluster == -1 or (0 <= cluster < num_clusters):
                    return cluster
            return None

        # 兼容：LLM 可能直接返回数字
        num_match = re.search(r"-?\d+", text)
        if num_match:
            idx = int(num_match.group())
            if idx == -1 or (0 <= idx < num_clusters):
                return idx

        logger.warning("LLM 单条分配响应解析失败: %s", text[:200])
        return None

    @staticmethod
    def _parse_batch_assignment(
        resp: Any,
        num_clusters: int,
        num_queries: int,
        idx_map: dict[int, int],
    ) -> list[int | None] | None:
        """解析 LLM 返回的批量查询分配结果。

        期望 JSON 数组：[{"id": 0, "cluster": 2}, {"id": 1, "cluster": -1}, ...]
        返回每条查询的实际簇索引列表（已映射为真实索引）。
        None 表示整体解析失败；列表中元素为 None 表示该条解析失败。
        """
        text = ""
        if hasattr(resp, "text"):
            text = resp.text or ""
        elif isinstance(resp, str):
            text = resp

        text = text.strip()
        if not text:
            return None

        data = _extract_json_array_from_text(text)
        if not isinstance(data, list):
            logger.warning("LLM 批量分配响应不是 JSON 数组: %s", text[:200])
            return None

        # 初始化为 None，表示未分配
        results: list[int | None] = [None] * num_queries

        for item in data:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            cluster = item.get("cluster")
            if not isinstance(item_id, int) or not isinstance(cluster, int):
                continue
            if not (0 <= item_id < num_queries):
                continue
            if cluster == -1:
                results[item_id] = -1
            elif 0 <= cluster < num_clusters:
                results[item_id] = idx_map.get(cluster, -1)
            else:
                results[item_id] = -1

        # 未出现在响应中的查询标记为 -1（当作新建簇处理）
        for i in range(num_queries):
            if results[i] is None:
                results[i] = -1

        return results

    def _keyword_cluster_queries(self, queries: list[tuple[str, str]]) -> list[QueryCluster]:
        """基于 bigram 关键词重叠度的 union-find 聚类（回退方案）。

        传统贪心 first-match 算法存在顺序依赖：第一个查询成为簇代表，
        后续查询仅与代表比较。当索引顺序变化时（新增会话排在前面），
        簇代表改变导致聚类结果剧变。

        union-find 算法保证：若 A~B 且 B~C，则 A/B/C 同簇，
        且结果与输入顺序无关。
        """
        n = len(queries)
        if n == 0:
            return []

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # 构建相似度图：O(n²)，对少量查询完全可行
        for i in range(n):
            for j in range(i + 1, n):
                if self._queries_similar(queries[i][0], queries[j][0]):
                    union(i, j)

        # 按根节点分组
        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(i)

        # 构建聚类：选择最短查询作为代表（更通用）
        clusters: list[QueryCluster] = []
        for indices in groups.values():
            best_idx = min(indices, key=lambda i: len(queries[i][0]))
            rep = queries[best_idx][0]
            cluster = QueryCluster(rep, has_new_queries=True)
            for i in indices:
                q, log_id = queries[i]
                meta = self._extract_trajectory_meta(log_id)
                cluster.add(q, log_id, meta)
            clusters.append(cluster)

        clusters.sort(key=lambda c: -len(c.queries))
        # 关键词聚类回退路径也生成标签
        self._refresh_cluster_labels(clusters)
        return clusters

    def _queries_similar(self, q1: str, q2: str) -> bool:
        """判断两个查询是否相似（基于 bigram 重叠度）。

        使用 overlap coefficient（交集 / min(两集合大小)），
        阈值 0.3 适配 bigram 粒度。
        """
        words1 = set(self._extract_keywords(q1))
        words2 = set(self._extract_keywords(q2))
        if not words1 or not words2:
            return False
        overlap = len(words1 & words2)
        return overlap / min(len(words1), len(words2)) >= 0.3

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """从文本中提取关键词。

        CJK 字符使用 bigram（字符二元组）提取，解决中文无自然词边界问题；
        非 CJK 部分保持按标点分词。
        """
        cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')
        keywords: list[str] = []

        # CJK 字符使用 bigram
        for match in cjk_pattern.finditer(text):
            segment = match.group()
            if len(segment) >= 2:
                for i in range(len(segment) - 1):
                    keywords.append(segment[i : i + 2])
            elif len(segment) == 1:
                keywords.append(segment)

        # 非 CJK 使用分词
        non_cjk = cjk_pattern.sub(' ', text)
        words = re.split(r'[\s,，。.!！?？;；:：、]+', non_cjk)
        keywords.extend(w.lower() for w in words if len(w) >= 2)

        return keywords

    def _get_existing_skill_keywords(self) -> set[str]:
        """获取现有 skill 的关键词集合。"""
        from crew.agent.skills import get_skills

        keywords: set[str] = set()
        for info in get_skills().values():
            keywords.add(info.get("name", "").lower())
            keywords.add(info.get("slug", "").lower())
            desc = info.get("description", "").lower()
            keywords.update(self._extract_keywords(desc))
        return keywords

    def _is_covered_by_existing(
        self,
        cluster: QueryCluster,
        existing_keywords: set[str],
    ) -> bool:
        """检查聚类是否已被现有 skill 覆盖。"""
        cluster_words = set(self._extract_keywords(cluster.representative))
        if not cluster_words:
            return False
        overlap = len(cluster_words & existing_keywords)
        return overlap / len(cluster_words) >= 0.6

    # ── 跨轮次关联检测 ──────────────────────────────────────────────────

    def _detect_cross_round_association(
        self,
        cluster: QueryCluster,
        previous_logs: list[TrajectoryLog],
        existing_keywords: set[str],
        current_query: str | None = None,
    ) -> CrossRoundAssociation | None:
        """检测本轮聚类与上一轮交互的关联性。

        优先使用 LLM 语义判断，回退到关键词重叠度。
        current_query 传入时使用当前会话的实际查询做对比，
        避免持久化簇中上一轮查询作为 representative 与自身对比。
        返回 CrossRoundAssociation 或 None（无上一轮数据时）。
        """
        # 收集上一轮的用户查询和激活的 skill
        prev_queries: list[str] = []
        prev_skills: list[str] = []
        for log in previous_logs:
            for entry in log.entries:
                if entry.role == "user" and entry.content:
                    prev_queries.append(entry.content.strip()[:200])
            prev_skills.extend(log.skills_activated)

        if not prev_queries and not prev_skills:
            return None

        # 优先使用传入的当前会话查询，否则回退到聚类代表
        query = current_query or cluster.representative
        query = query[:200]

        # 优先 LLM 语义判断
        if self._llm is not None:
            association = self._llm_detect_association(
                query, prev_queries, prev_skills
            )
            if association is not None:
                return association
            logger.info("LLM 关联检测失败，回退到关键词方法")

        # 回退：关键词重叠度
        return self._keyword_detect_association(
            query, prev_queries, prev_skills, existing_keywords
        )

    def _llm_detect_association(
        self,
        current_query: str,
        prev_queries: list[str],
        prev_skills: list[str],
    ) -> CrossRoundAssociation | None:
        """使用 LLM 语义判断本轮任务与上一轮的关联性。"""
        prev_queries_text = "\n".join(
            f"  - {q[:150]}" for q in prev_queries[:5]
        ) or "  （无）"
        prev_skills_text = ", ".join(prev_skills) if prev_skills else "（无）"

        prompt = (
            f"## 当前轮次用户查询\n{current_query}\n\n"
            f"## 上一轮用户查询\n{prev_queries_text}\n\n"
            f"## 上一轮激活的 skill\n{prev_skills_text}\n\n"
            f"请分析当前查询与上一轮交互的关联性。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_ASSOCIATION_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)
            return self._parse_association_response(resp, current_query, prev_skills)
        except Exception as exc:
            logger.warning("LLM 关联检测异常: %s", exc)
            return None

    def _keyword_detect_association(
        self,
        current_query: str,
        prev_queries: list[str],
        prev_skills: list[str],
        existing_keywords: set[str],
    ) -> CrossRoundAssociation:
        """关键词重叠度回退方案：计算 current_query 与上一轮查询/skill 的重叠度。"""
        current_words = set(self._extract_keywords(current_query))
        if not current_words:
            return CrossRoundAssociation(
                current_query=current_query,
                independence_score=1.0,
                association_type="none",
                action="create",
                reason="当前查询无有效关键词",
            )

        # 与上一轮查询的重叠度
        max_query_overlap = 0.0
        for prev_q in prev_queries:
            prev_words = set(self._extract_keywords(prev_q))
            if prev_words:
                overlap = len(current_words & prev_words) / min(
                    len(current_words), len(prev_words)
                )
                max_query_overlap = max(max_query_overlap, overlap)

        # 与上一轮 skill 关键词的重叠度
        skill_overlap = 0.0
        related_skill_slug = ""
        for skill_slug in prev_skills:
            skill_words = set(self._extract_keywords(skill_slug))
            # 也检查 existing_keywords 中与该 skill 相关的部分
            skill_words.update(
                w for w in existing_keywords if skill_slug.lower() in w.lower()
            )
            if skill_words:
                overlap = len(current_words & skill_words) / max(
                    len(current_words), 1
                )
                if overlap > skill_overlap:
                    skill_overlap = overlap
                    related_skill_slug = skill_slug

        # 关键词回退兜底：如果 query-to-query 重叠度高但 query-to-skill 重叠度为 0，
        # related_skill_slug 仍为空。此时若上一轮有激活 skill 且关联类型非 none，
        # 取第一个 prev_skill 作为进化目标（与 LLM 路径 _parse_association_response 一致）。
        if not related_skill_slug and prev_skills and max_query_overlap >= 0.2:
            related_skill_slug = prev_skills[0]

        # 综合关联度
        association_score = max(max_query_overlap, skill_overlap)

        # 关联度太低则判定为无关联
        if association_score < 0.2:
            return CrossRoundAssociation(
                current_query=current_query,
                independence_score=0.9,
                association_score=association_score,
                association_type="none",
                action="create",
                reason=f"关键词重叠度仅 {association_score:.2f}，判定为无关联",
            )

        # 根据重叠度估算关联类型和独立性
        if max_query_overlap >= 0.5:
            assoc_type = "continuation"
            independence = 0.2
        elif skill_overlap >= 0.3:
            assoc_type = "tool_chain"
            independence = 0.4
        else:
            assoc_type = "same_domain"
            independence = 0.5

        action = "evolve" if independence < 0.7 else "create"

        return CrossRoundAssociation(
            current_query=current_query,
            related_skill_slug=related_skill_slug,
            association_score=association_score,
            independence_score=independence,
            association_type=assoc_type,
            action=action,
            reason=(
                f"关键词回退: 查询重叠度={max_query_overlap:.2f}, "
                f"skill重叠度={skill_overlap:.2f}"
            ),
        )

    @staticmethod
    def _parse_association_response(
        resp: Any,
        current_query: str,
        prev_skills: list[str],
    ) -> CrossRoundAssociation | None:
        """解析 LLM 返回的关联检测 JSON。"""
        text = ""
        if hasattr(resp, "text"):
            text = resp.text or ""
        elif isinstance(resp, str):
            text = resp

        text = text.strip()
        data = _extract_json_from_text(text)
        if not data:
            logger.warning("关联检测响应中未找到 JSON: %s", text[:200])
            return None

        assoc_type = str(data.get("association_type", "none")).strip().lower()
        association_score = float(data.get("association_score", 0.0))
        independence_score = float(data.get("independence_score", 1.0))
        related_skill_slug = str(data.get("related_skill_slug", "")).strip()
        reason = str(data.get("reason", ""))

        # 如果 LLM 未给出 related_skill_slug 但有上一轮 skill，取第一个
        if not related_skill_slug and prev_skills and assoc_type != "none":
            related_skill_slug = prev_skills[0]

        action = "evolve" if independence_score < 0.7 else "create"

        return CrossRoundAssociation(
            current_query=current_query,
            related_skill_slug=related_skill_slug,
            association_score=association_score,
            independence_score=independence_score,
            association_type=assoc_type,
            action=action,
            reason=reason,
        )

    def _build_evolve_suggestion(
        self,
        cluster: QueryCluster,
        association: CrossRoundAssociation,
        conversation_id: str = "",
        session_id: str = "",
    ) -> OptimizationSuggestion | None:
        """构建 "evolve" 类型的 OptimizationSuggestion，并执行实际的技能进化。

        调用 evolve_skill() 完成完整的进化工作流：
        比较当前轨迹与已有技能的结构化内容 → 判断进化部分 → 批量进化 →
        逻辑冲突检查 → 更新 SKILL.md → 存储新版本结构化内容

        Args:
            conversation_id: 主会话 ID，用于 evolution_log 文件命名
            session_id: 当前会话/侧链 ID，用于 evolution_log 文件命名
        """
        from crew.agent.skills import resolve_skill_any

        skill_slug = association.related_skill_slug
        if not skill_slug:
            logger.warning(
                "进化建议缺少 related_skill_slug，跳过: %s",
                cluster.display_name[:40],
            )
            return None

        skill_info = resolve_skill_any(skill_slug)
        if not skill_info:
            logger.warning("skill %s 不存在，无法生成进化建议", skill_slug)
            return None

        skill_path = skill_info.get("skill_md_path", "")
        skill_name = skill_info.get("name", skill_slug)

        # 执行实际的技能进化工作流
        evolve_result = self.evolve_skill(
            skill_slug, cluster, association,
            conversation_id=conversation_id,
            session_id=session_id,
        )

        if evolve_result:
            evolved_modules = evolve_result.get("evolved_modules", [])
            new_version = evolve_result.get("new_version", "")
            skipped = evolve_result.get("skipped", False)
            cloned_from = evolve_result.get("cloned_from")
            # 如果发生了克隆，使用新副本的 skill 信息
            if cloned_from:
                skill_slug = evolve_result.get("skill_slug", skill_slug)
                skill_path = evolve_result.get(
                    "skill_md_path", skill_path,
                )
                skill_name = f"{skill_name}（进化副本）"
                # 将克隆后的新 skill slug 回写到源轨迹日志的 skills_activated，
                # 使下一轮跨轮次关联检测能直接获取到进化副本
                source_log_ids = list({lid for _, lid in cluster.queries})
                if source_log_ids:
                    updated = self._log_store.add_skill_to_logs(
                        source_log_ids, skill_slug,
                    )
                    if updated:
                        logger.info(
                            "将进化副本 skill %s 回写到 %d 条源轨迹日志的 skills_activated",
                            skill_slug, updated,
                        )
            if skipped:
                logger.info(
                    "技能进化评估: 无需进化（%s）；关联类型: %s, 独立性: %.2f%s",
                    evolve_result.get('reason', ''),
                    association.association_type,
                    association.independence_score,
                    f"；已创建进化副本 {skill_slug}" if cloned_from else "",
                )
                return None
            else:
                reason = (
                    f"技能进化完成（{new_version}），进化模块: "
                    f"{', '.join(evolved_modules)}；"
                    + (f"已从 {cloned_from} 克隆为 {skill_slug}；" if cloned_from else "")
                    + f"关联类型: {association.association_type}, "
                    f"独立性: {association.independence_score:.2f}: "
                    f"{association.reason}"
                )
                suggested_value = (
                    f"进化模块: {', '.join(evolved_modules)}\n"
                    f"新版本: {new_version}\n"
                    + (f"克隆自: {cloned_from}\n" if cloned_from else "")
                    + "SKILL.md 已更新"
                )
        else:
            reason = (
                f"技能进化失败，回退到简单建议（{association.association_type}, "
                f"独立性={association.independence_score:.2f}）: "
                f"{association.reason}"
            )
            suggested_value = (
                f"关联类型: {association.association_type}\n"
                f"聚类名称: {cluster.display_name[:80]}"
            )

        return OptimizationSuggestion(
            skill_slug=skill_slug,
            skill_name=skill_name,
            skill_path=skill_path,
            suggestion_type="evolve",
            current_value="",
            suggested_value=suggested_value,
            reason=reason,
            confidence=1.0 - association.independence_score,
        )

    # ── 技能进化工作流 ──────────────────────────────────────────────────

    @staticmethod
    def _sanitize_id(raw: str) -> str:
        """将会话/Session ID 清理为安全的文件名片段。"""
        if not raw:
            return "unknown"
        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(raw))
        return safe[:64] or "unknown"

    @staticmethod
    def _write_evolution_json(
        evo_log_dir: Path,
        data: dict[str, Any],
        conversation_id: str = "",
        session_id: str = "",
    ) -> str:
        """将结构化内容写入 evolution_log，使用会话ID组合命名，并更新 version_history.json。

        文件名格式: {conversation_id}_{session_id}_{timestamp}.json
        version_history.json 以进化历史顺序记录所有 JSON 文件名。
        """
        evo_log_dir.mkdir(parents=True, exist_ok=True)

        conv = SkillGenerator._sanitize_id(conversation_id)
        sess = SkillGenerator._sanitize_id(session_id)
        # 使用微秒确保同一秒内的多次调用不会覆盖文件
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{conv}_{sess}_{timestamp}.json"

        json_path = evo_log_dir / filename
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新 version_history.json — 追加文件名，保持进化历史顺序
        history_path = evo_log_dir / "version_history.json"
        history: dict[str, Any] = {"versions": []}
        if history_path.exists():
            try:
                existing = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and "versions" in existing:
                    history = existing
                elif isinstance(existing, list):
                    history = {"versions": existing}
            except (json.JSONDecodeError, ValueError):
                pass
        history["versions"].append(filename)
        history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return filename

    @staticmethod
    def _load_latest_structured_content(skill_dir: Path) -> dict[str, Any] | None:
        """从 skill 目录下的 evolution_log/ 加载最新版本的结构化内容 JSON。

        优先从 version_history.json 获取最新文件名；
        若 version_history.json 不存在（旧版数据），回退到扫描 v*.json 文件。
        """
        evo_log_dir = skill_dir / "evolution_log"
        if not evo_log_dir.exists():
            return None

        latest_path: Path | None = None

        # 优先从 version_history.json 获取最新版本
        history_path = evo_log_dir / "version_history.json"
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(history, dict) and history.get("versions"):
                    versions = history["versions"]
                    if isinstance(versions, list) and versions:
                        candidate = evo_log_dir / versions[-1]
                        if candidate.exists():
                            latest_path = candidate
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("加载 version_history.json 失败: %s", exc)

        # 回退：扫描 v*.json 文件（兼容旧版数据）
        if not latest_path:
            max_version = 0
            for f in evo_log_dir.iterdir():
                if f.is_file() and f.suffix == ".json":
                    match = re.match(r"^v(\d+)$", f.stem)
                    if match:
                        version = int(match.group(1))
                        if version > max_version:
                            max_version = version
                            latest_path = f

        # 兼容旧版 structured_content.json
        if not latest_path:
            legacy = evo_log_dir / "structured_content.json"
            if legacy.exists():
                latest_path = legacy

        if not latest_path:
            return None

        try:
            data = json.loads(latest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("加载结构化内容失败 %s: %s", latest_path, exc)
        return None

    # ── 克隆进化：为无 evolution_log 的 skill 创建可进化副本 ──────────────

    def _clone_skill_as_evol(
        self,
        skill_slug: str,
        skill_dir: Path,
    ) -> tuple[str, Path] | None:
        """将没有 evolution_log 的 skill 克隆为 ``xxx-evol`` 副本。

        创建新目录 ``{skill_slug}-evol``，复制原 skill 的所有文件和子目录，
        并创建 ``evolution_log`` 文件夹。

        Returns:
            (new_slug, new_skill_dir) 或 None（失败时）。
        """
        from crew.agent.skills import get_user_skills_dir

        new_slug = f"{skill_slug}-evol"
        new_dir = get_user_skills_dir() / new_slug

        if new_dir.exists():
            logger.info(
                "skill %s 的进化副本已存在: %s，直接复用",
                skill_slug, new_dir,
            )
        else:
            try:
                shutil.copytree(skill_dir, new_dir, dirs_exist_ok=True)
                logger.info("已克隆 skill %s -> %s", skill_slug, new_slug)
            except Exception as exc:
                logger.warning("克隆 skill %s 失败: %s", skill_slug, exc)
                return None

        # 确保 evolution_log 目录存在
        evo_log_dir = new_dir / "evolution_log"
        evo_log_dir.mkdir(parents=True, exist_ok=True)

        return new_slug, new_dir

    @staticmethod
    def _split_markdown_sections(body: str) -> dict[str, str]:
        """将 Markdown 正文按 ``## `` 标题分割为 ``{section_title: section_content}``。

        ``### `` 级别的子标题保留在所属 section 的内容中。
        """
        sections: dict[str, str] = {}
        current_title = ""
        current_lines: list[str] = []

        for line in body.splitlines():
            if line.startswith("## ") and not line.startswith("### "):
                if current_title:
                    sections[current_title] = "\n".join(current_lines).strip()
                current_title = line[3:].strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_title:
            sections[current_title] = "\n".join(current_lines).strip()

        return sections

    @staticmethod
    def _parse_workflow_steps(
        workflow_text: str,
    ) -> list[dict[str, Any]]:
        """解析 ``## 工作流程`` 文本为结构化步骤列表。

        每个步骤以 ``### 第N步：title`` 开头，包含 description、sub_steps、code_examples。
        """
        if not workflow_text:
            return []

        steps: list[dict[str, Any]] = []
        current_step: dict[str, Any] | None = None
        current_lines: list[str] = []

        for line in workflow_text.splitlines():
            step_match = re.match(
                r"^###\s+第\s*(\d+)\s*步\s*[：:]?\s*(.*)$", line,
            )
            if step_match:
                if current_step is not None:
                    SkillGenerator._finalize_workflow_step(
                        current_step, current_lines,
                    )
                    steps.append(current_step)
                current_step = {
                    "step": int(step_match.group(1)),
                    "title": step_match.group(2).strip(),
                }
                current_lines = []
            else:
                current_lines.append(line)

        if current_step is not None:
            SkillGenerator._finalize_workflow_step(
                current_step, current_lines,
            )
            steps.append(current_step)

        return steps

    @staticmethod
    def _finalize_workflow_step(
        step: dict[str, Any], lines: list[str],
    ) -> None:
        """将收集到的文本行解析为 step 的 description / sub_steps / code_examples。"""
        text = "\n".join(lines).strip()
        if not text:
            step["description"] = ""
            step["sub_steps"] = []
            step["code_examples"] = []
            return

        # 提取代码块
        code_blocks: list[dict[str, Any]] = []
        code_pattern = re.compile(
            r"```(\w*)\n(.*?)```", re.DOTALL,
        )
        for m in code_pattern.finditer(text):
            lang = m.group(1) or ""
            code = m.group(2).rstrip("\n")
            # 检查代码块前是否有平台示例标记
            before = text[:m.start()].rstrip()
            platform_match = re.search(
                r"\*\*(.+?)\s*示例[：:]\*\*\s*$", before,
            )
            code_entry: dict[str, Any] = {"code": code}
            if lang:
                code_entry["language"] = lang
            if platform_match:
                code_entry["platform"] = platform_match.group(1).strip()
            code_blocks.append(code_entry)

        # 移除代码块后的纯文本
        text_no_code = code_pattern.sub("", text).strip()

        # 提取 sub_steps（以 - 开头的行）
        sub_steps: list[str] = []
        desc_lines: list[str] = []
        for line in text_no_code.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                sub_steps.append(stripped[2:].strip())
            elif stripped:
                desc_lines.append(stripped)

        step["description"] = "\n".join(desc_lines).strip()
        step["sub_steps"] = sub_steps
        step["code_examples"] = code_blocks

    @staticmethod
    def _parse_script_template(
        script_text: str,
    ) -> dict[str, Any]:
        """解析 ``## 脚本模板`` 文本为结构化 dict。"""
        if not script_text:
            return {}

        # 提取代码块
        code_match = re.search(
            r"```(\w*)\n(.*?)```", script_text, re.DOTALL,
        )
        result: dict[str, Any] = {}
        if code_match:
            result["language"] = code_match.group(1) or "python"
            result["code"] = code_match.group(2).rstrip("\n")
            # 代码块前的文本作为 description
            desc = script_text[:code_match.start()].strip()
            if desc:
                result["description"] = desc
        else:
            result["description"] = script_text.strip()
            result["code"] = ""

        return result

    @staticmethod
    def _parse_notes(
        notes_text: str,
    ) -> list[dict[str, Any]]:
        """解析 ``## 注意事项`` 文本为结构化列表。"""
        if not notes_text:
            return []

        notes: list[dict[str, Any]] = []
        current_note: dict[str, Any] | None = None
        current_items: list[str] = []

        for line in notes_text.splitlines():
            sub_match = re.match(r"^###\s+(.+)$", line)
            if sub_match:
                if current_note is not None:
                    current_note["items"] = current_items
                    notes.append(current_note)
                current_note = {"title": sub_match.group(1).strip()}
                current_items = []
            else:
                stripped = line.strip()
                if stripped.startswith("- "):
                    current_items.append(stripped[2:].strip())

        if current_note is not None:
            current_note["items"] = current_items
            notes.append(current_note)

        # 如果没有 ### 子标题，但有 - 列表项，包装为单个 note
        if not notes and notes_text.strip():
            items = [
                line.strip()[2:].strip()
                for line in notes_text.splitlines()
                if line.strip().startswith("- ")
            ]
            if items:
                notes.append({"title": "注意事项", "items": items})

        return notes

    @staticmethod
    def _parse_output_format(
        output_text: str,
    ) -> dict[str, Any]:
        """解析 ``## 输出格式`` 文本为结构化 dict。"""
        if not output_text:
            return {}

        result: dict[str, Any] = {}
        code_match = re.search(
            r"```\n?(.*?)```", output_text, re.DOTALL,
        )
        if code_match:
            result["template"] = code_match.group(1).rstrip("\n")
            desc = output_text[:code_match.start()].strip()
            if desc:
                result["description"] = desc
        else:
            result["description"] = output_text.strip()
            result["template"] = ""

        return result

    @staticmethod
    def _map_skill_md_to_structured(
        skill_md_path: Path,
    ) -> dict[str, Any] | None:
        """将 SKILL.md 内容映射到结构化模板（8 模块 JSON）。

        解析 YAML frontmatter 和 Markdown 正文，反向映射到
        :meth:`_assemble_skill_markdown` 的输入格式，使手动创建的
        skill 也能进入进化工作流。
        """
        if not skill_md_path.exists():
            return None

        content = skill_md_path.read_text(encoding="utf-8")

        from crew.agent.skills import _parse_frontmatter, _metadata_dict

        frontmatter, body = _parse_frontmatter(content)
        meta = _metadata_dict(frontmatter)

        structured: dict[str, Any] = {}

        # 1. metadata
        md_meta: dict[str, Any] = {
            "name": frontmatter.get("name", "") or meta.get("name", ""),
            "description": (
                frontmatter.get("description", "")
                or meta.get("description", "")
            ),
            "zh_name": meta.get("zh_name", ""),
            "zh_description": meta.get("zh_description", ""),
            "query_examples": meta.get("query_examples", []),
            "category": meta.get(
                "skillCategoryName", meta.get("category", ""),
            ),
            "version": str(meta.get("version", "0.1.0")),
        }
        emoji = meta.get("crew", {})
        if isinstance(emoji, dict) and emoji.get("emoji"):
            md_meta["emoji"] = emoji["emoji"]
        structured["metadata"] = md_meta

        # 解析 body 各 section
        sections = SkillGenerator._split_markdown_sections(body)

        # 2. overview
        overview: dict[str, Any] = {}
        title_match = re.match(
            r"^#\s+(.+)$", body.strip(), re.MULTILINE,
        )
        overview["title"] = (
            title_match.group(1).strip()
            if title_match
            else md_meta["name"]
        )

        overview_section = sections.get("概述", "")
        if overview_section:
            parts = re.split(
                r"\*\*核心能力[：:]\*\*", overview_section, maxsplit=1,
            )
            overview["description"] = parts[0].strip()
            if len(parts) > 1:
                caps = [
                    line.lstrip("- ").strip()
                    for line in parts[1].strip().splitlines()
                    if line.strip().startswith("-")
                ]
                overview["core_capabilities"] = caps
            else:
                overview["core_capabilities"] = []
        else:
            overview["description"] = md_meta["description"]
            overview["core_capabilities"] = []
        structured["overview"] = overview

        # 3. trigger_conditions
        trigger_section = sections.get("触发条件", "")
        if trigger_section:
            triggers = [
                line.lstrip("- ").strip()
                for line in trigger_section.splitlines()
                if line.strip().startswith("-")
            ]
            structured["trigger_conditions"] = triggers
        else:
            structured["trigger_conditions"] = []

        # 4. workflow_steps
        workflow_section = sections.get("工作流程", "")
        structured["workflow_steps"] = (
            SkillGenerator._parse_workflow_steps(workflow_section)
        )

        # 5. script_template
        script_section = sections.get("脚本模板", "")
        structured["script_template"] = (
            SkillGenerator._parse_script_template(script_section)
        )

        # 6. notes
        notes_section = sections.get("注意事项", "")
        structured["notes"] = SkillGenerator._parse_notes(notes_section)

        # 7. output_format
        output_section = sections.get("输出格式", "")
        structured["output_format"] = (
            SkillGenerator._parse_output_format(output_section)
        )

        # 8. common_keywords
        keywords_section = sections.get("常见关键词", "")
        if keywords_section:
            keywords = [
                line.lstrip("- ").strip()
                for line in keywords_section.splitlines()
                if line.strip().startswith("-")
            ]
            structured["common_keywords"] = keywords
        else:
            structured["common_keywords"] = []

        return structured

    @staticmethod
    def _extract_skill_summary(
        structured: dict[str, Any],
    ) -> dict[str, str]:
        """从结构化内容中提取摘要字段（title, name, description）。"""
        meta = structured.get("metadata", {})
        overview = structured.get("overview", {})
        return {
            "name": meta.get("name", ""),
            "title": overview.get("title", ""),
            "description": meta.get("description", "")
            or overview.get("description", ""),
        }

    def _get_cluster_trajectory_text(
        self, cluster: QueryCluster
    ) -> str:
        """从聚类关联的轨迹日志中提取用户交互文本，用于 LLM 比较。"""
        log_ids = {lid for _, lid in cluster.queries}
        entries: list[dict[str, Any]] = []

        for log_id in log_ids:
            log = self._log_store.load(log_id)
            if not log:
                continue
            for entry in log.entries:
                if entry.role == "user" and entry.content:
                    entries.append({
                        "role": "user",
                        "content": entry.content[:500],
                    })
                elif entry.role == "assistant" and entry.content:
                    entries.append({
                        "role": "assistant",
                        "content": (entry.content or "")[:300],
                        "tool_calls": entry.tool_calls[:3] if entry.tool_calls else [],
                    })

        stats = self._collect_cluster_trajectory_stats(cluster)
        tools_str = (
            ", ".join(
                f"{name}({cnt})"
                for name, cnt in sorted(
                    stats["tool_usage"].items(), key=lambda x: -x[1]
                )[:10]
            )
            or "无"
        )

        result = {
            "cluster_label": cluster.display_name[:200],
            "representative_query": cluster.representative[:200],
            "query_examples": [q for q, _ in cluster.queries[:5]],
            "trajectory_entries": entries[:20],
            "tool_usage": tools_str,
            "trajectory_summaries": list(cluster.trajectory_summaries[:3]),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    def evolve_skill(
        self,
        skill_slug: str,
        cluster: QueryCluster,
        association: CrossRoundAssociation,
        conversation_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any] | None:
        """执行完整的技能进化工作流。

        流程：
        1. 加载最新版本的结构化内容，分三种场景：
           - 场景1：原 skill 有 evolution_log → 直接在其基础上进化
           - 场景2：原 skill 无 evolution_log 且无 xxx-evol →
             克隆为 ``xxx-evol`` 副本，将 SKILL.md 映射为结构化内容
             并写入 evolution_log，后续进化在副本上进行
           - 场景3：原 skill 无 evolution_log 但 xxx-evol 已存在 →
             不克隆不映射，直接在 xxx-evol 的 evolution_log 基础上继续进化
        2. 提取摘要（title, name, description）
        3. 获取当前轨迹数据
        4. LLM 比较当前轨迹与技能摘要，判断哪些模块需要进化
        5. LLM 批量进化需要更新的模块
        6. LLM 检查逻辑冲突，有则修复
        7. 更新 SKILL.md 并存储新版本结构化内容

        Args:
            conversation_id: 主会话 ID，用于 evolution_log 文件命名
            session_id: 当前会话/侧链 ID，用于 evolution_log 文件命名

        Returns:
            进化结果 dict，包含 evolved_modules, new_version 等；
            失败时返回 None。
        """
        from crew.agent.skills import resolve_skill_any

        if self._llm is None:
            logger.warning("LLM 不可用，无法执行技能进化")
            return None

        skill_info = resolve_skill_any(skill_slug)
        if not skill_info:
            logger.warning("skill %s 不存在，无法进化", skill_slug)
            return None

        skill_dir = Path(skill_info.get("skill_dir", ""))
        skill_md_path = Path(skill_info.get("skill_md_path", ""))
        if not skill_dir.exists():
            logger.warning("skill 目录不存在: %s", skill_dir)
            return None

        # ── 1. 加载最新版本的结构化内容 ──
        # 三种场景：
        #   1) 原 skill 有 evolution_log → 直接进化
        #   2) 原 skill 无 evolution_log 且无 xxx-evol → 克隆、映射后进化
        #   3) 原 skill 无 evolution_log 但 xxx-evol 已存在 → 直接在 xxx-evol 上进化
        cloned_from: str | None = None
        current_structured = self._load_latest_structured_content(skill_dir)
        if not current_structured:
            from crew.agent.skills import get_user_skills_dir

            evol_slug = f"{skill_slug}-evol"
            evol_dir = get_user_skills_dir() / evol_slug

            if evol_dir.exists():
                # 场景3：xxx-evol 已存在，检查是否有 evolution_log
                evol_structured = self._load_latest_structured_content(evol_dir)
                if evol_structured:
                    # xxx-evol 已有进化历史，直接在其基础上继续进化，不克隆不映射
                    logger.info(
                        "skill %s 无 evolution_log 但 %s 已存在且有进化历史，"
                        "直接在其基础上继续进化",
                        skill_slug, evol_slug,
                    )
                    current_structured = evol_structured
                    skill_slug = evol_slug
                    skill_dir = evol_dir
                    skill_md_path = evol_dir / "SKILL.md"
                else:
                    # xxx-evol 存在但无 evolution_log（边缘情况），
                    # 映射其 SKILL.md 为初始结构化内容
                    logger.info(
                        "skill %s 的进化副本 %s 已存在但无 evolution_log，"
                        "映射 SKILL.md 为初始结构化内容",
                        skill_slug, evol_slug,
                    )
                    new_skill_md = evol_dir / "SKILL.md"
                    current_structured = self._map_skill_md_to_structured(
                        new_skill_md,
                    )
                    if not current_structured:
                        logger.warning(
                            "skill %s SKILL.md 映射到结构化内容失败",
                            skill_slug,
                        )
                        return None
                    evo_log_dir = evol_dir / "evolution_log"
                    self._write_evolution_json(
                        evo_log_dir,
                        current_structured,
                        conversation_id=conversation_id,
                        session_id=session_id,
                    )
                    cloned_from = skill_slug
                    skill_slug = evol_slug
                    skill_dir = evol_dir
                    skill_md_path = new_skill_md
            else:
                # 场景2：没有 xxx-evol，需要克隆、映射后进化
                logger.info(
                    "skill %s 无 evolution_log 且无进化副本，启动克隆进化流程",
                    skill_slug,
                )
                clone_result = self._clone_skill_as_evol(
                    skill_slug, skill_dir,
                )
                if not clone_result:
                    logger.warning(
                        "skill %s 克隆失败，无法进化", skill_slug,
                    )
                    return None
                new_slug, new_dir = clone_result

                # 映射 SKILL.md 到结构化内容
                new_skill_md = new_dir / "SKILL.md"
                current_structured = self._map_skill_md_to_structured(
                    new_skill_md,
                )
                if not current_structured:
                    logger.warning(
                        "skill %s SKILL.md 映射到结构化内容失败",
                        skill_slug,
                    )
                    return None

                # 写入初始版本到 evolution_log
                evo_log_dir = new_dir / "evolution_log"
                self._write_evolution_json(
                    evo_log_dir,
                    current_structured,
                    conversation_id=conversation_id,
                    session_id=session_id,
                )
                logger.info(
                    "已创建进化副本 %s 并写入初始结构化内容",
                    new_slug,
                )

                # 后续操作在 xxx-evol 上进行
                cloned_from = skill_slug
                skill_slug = new_slug
                skill_dir = new_dir
                skill_md_path = new_skill_md

        # ── 2. 提取摘要 ──
        skill_summary = self._extract_skill_summary(current_structured)

        # ── 3. 获取当前轨迹数据 ──
        trajectory_text = self._get_cluster_trajectory_text(cluster)

        # ── 4. LLM 比较当前轨迹与技能摘要，判断哪些模块需要进化 ──
        compare_result = self._evolve_compare(
            skill_summary, current_structured, trajectory_text
        )
        if not compare_result:
            logger.warning("进化比较失败，跳过进化")
            return None

        if not compare_result.get("need_evolve", False):
            logger.info(
                "技能 %s 无需进化: %s",
                skill_slug,
                compare_result.get("reason", ""),
            )
            return {
                "evolved_modules": [],
                "new_version": current_structured.get("metadata", {}).get(
                    "version", "0.1.0"
                ),
                "skipped": True,
                "reason": compare_result.get("reason", "无需进化"),
                "cloned_from": cloned_from,
                "skill_slug": skill_slug,
                "skill_md_path": str(skill_md_path),
            }

        modules_to_evolve = compare_result.get("modules_to_evolve", [])
        if not modules_to_evolve:
            logger.info("技能 %s 无需进化的模块", skill_slug)
            return {
                "evolved_modules": [],
                "new_version": current_structured.get("metadata", {}).get(
                    "version", "0.1.0"
                ),
                "skipped": True,
                "reason": "无需进化的模块",
                "cloned_from": cloned_from,
                "skill_slug": skill_slug,
                "skill_md_path": str(skill_md_path),
            }

        logger.info(
            "技能 %s 需要进化的模块: %s",
            skill_slug,
            ", ".join(modules_to_evolve),
        )

        # ── 5. LLM 批量进化需要更新的模块 ──
        evolved_structured = self._evolve_batch(
            current_structured, trajectory_text, modules_to_evolve
        )
        if not evolved_structured:
            logger.warning("批量进化失败，跳过")
            return None

        # ── 6. LLM 检查逻辑冲突，有则修复 ──
        final_structured = self._evolve_conflict_check(evolved_structured)
        if not final_structured:
            final_structured = evolved_structured

        # ── 7. 更新 SKILL.md 并存储新版本结构化内容 ──
        new_content = self._assemble_skill_markdown(final_structured)
        if not new_content:
            logger.warning("拼接进化后的 SKILL.md 失败")
            return None

        skill_md_path.write_text(new_content, encoding="utf-8")
        logger.info("SKILL.md 已更新: %s", skill_md_path)

        evo_log_dir = skill_dir / "evolution_log"
        filename = self._write_evolution_json(
            evo_log_dir,
            final_structured,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        logger.info(
            "进化后的结构化内容已写入: %s/%s (会话=%s, session=%s)",
            evo_log_dir,
            filename,
            conversation_id,
            session_id,
        )

        new_version_str = final_structured.get("metadata", {}).get("version", "")
        return {
            "evolved_modules": modules_to_evolve,
            "new_version": new_version_str or filename,
            "skipped": False,
            "reason": compare_result.get("reason", ""),
            "cloned_from": cloned_from,
            "skill_slug": skill_slug,
            "skill_md_path": str(skill_md_path),
        }

    def _evolve_compare(
        self,
        skill_summary: dict[str, str],
        full_structured: dict[str, Any],
        trajectory_text: str,
    ) -> dict[str, Any] | None:
        """LLM 比较当前轨迹与技能摘要，判断哪些模块需要进化。"""
        summary_json = json.dumps(skill_summary, ensure_ascii=False, indent=2)
        full_json = json.dumps(full_structured, ensure_ascii=False, indent=2)

        prompt = (
            f"## 已有技能摘要\n{summary_json}\n\n"
            f"## 已有技能完整结构化内容（供参考）\n{full_json}\n\n"
            f"## 当前用户交互轨迹\n{trajectory_text}\n\n"
            f"请比较当前轨迹与已有技能，判断哪些模块需要进化。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_EVOLVE_COMPARE_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)

            text = ""
            if hasattr(resp, "text"):
                text = resp.text or ""
            elif isinstance(resp, str):
                text = resp

            text = text.strip()
            if not text:
                return None

            data = _extract_json_from_text(text)
            if not isinstance(data, dict):
                return None
            return data

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("进化比较 JSON 解析失败: %s", exc)
            return None
        except Exception as exc:
            logger.warning("进化比较失败: %s", exc)
            return None

    def _evolve_batch(
        self,
        current_structured: dict[str, Any],
        trajectory_text: str,
        modules_to_evolve: list[str],
    ) -> dict[str, Any] | None:
        """LLM 批量进化指定模块，返回完整的进化后结构化内容。"""
        current_json = json.dumps(
            current_structured, ensure_ascii=False, indent=2
        )
        modules_str = ", ".join(modules_to_evolve)

        prompt = (
            f"## 当前技能结构化内容\n{current_json}\n\n"
            f"## 当前用户交互轨迹\n{trajectory_text}\n\n"
            f"## 需要进化的模块\n{modules_str}\n\n"
            f"请对以上模块进行批量进化，返回进化后的**完整**结构化 JSON 对象。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_EVOLVE_BATCH_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)

            text = ""
            if hasattr(resp, "text"):
                text = resp.text or ""
            elif isinstance(resp, str):
                text = resp

            text = text.strip()
            if not text:
                return None

            data = _extract_json_from_text(text)
            if not isinstance(data, dict):
                return None
            return data

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("批量进化 JSON 解析失败: %s", exc)
            return None
        except Exception as exc:
            logger.warning("批量进化失败: %s", exc)
            return None

    def _evolve_conflict_check(
        self,
        evolved_structured: dict[str, Any],
    ) -> dict[str, Any] | None:
        """LLM 检查进化后的结构化内容是否存在逻辑冲突，有则修复。"""
        evolved_json = json.dumps(
            evolved_structured, ensure_ascii=False, indent=2
        )

        prompt = (
            f"## 进化后的技能结构化内容\n{evolved_json}\n\n"
            f"请检查以上内容是否存在逻辑冲突。"
            f"如果有冲突，请在 fixed_content 中返回修复后的完整 JSON。"
        )

        try:
            from crew.core.types import Message

            messages = [
                Message.system(_EVOLVE_CONFLICT_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)

            text = ""
            if hasattr(resp, "text"):
                text = resp.text or ""
            elif isinstance(resp, str):
                text = resp

            text = text.strip()
            if not text:
                return None

            data = _extract_json_from_text(text)
            if not isinstance(data, dict):
                return None

            has_conflict = data.get("has_conflict", False)
            if has_conflict:
                conflicts = data.get("conflicts", [])
                logger.info("检测到逻辑冲突: %s", "; ".join(conflicts))
                fixed = data.get("fixed_content", "")
                if fixed:
                    try:
                        fixed_data = (
                            _extract_json_from_text(fixed)
                            if isinstance(fixed, str)
                            else fixed
                        )
                        if isinstance(fixed_data, dict):
                            logger.info("已修复逻辑冲突")
                            return fixed_data
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(
                            "fixed_content JSON 解析失败，使用未修复版本"
                        )
                        return None
                else:
                    logger.warning(
                        "检测到冲突但未提供修复内容，使用未修复版本"
                    )
                    return None
            else:
                logger.info("未检测到逻辑冲突")
                return evolved_structured

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("冲突检查 JSON 解析失败: %s", exc)
            return None
        except Exception as exc:
            logger.warning("冲突检查失败: %s", exc)
            return None

    def _generate_slug(self, text: str) -> str:
        """从文本生成 slug（临时名称，最终由 LLM 在 create() 中覆盖）。"""
        from crew.agent.skills import _slugify, _contains_cjk

        if _contains_cjk(text):
            # CJK 文本无法直接做 slug，用时间戳做临时占位，
            # LLM 会在 create() 阶段根据内容生成有意义的英文名称
            return f"pending-skill-{int(time.time()) % 100000}"
        return _slugify(text[:50]) or f"pending-skill-{int(time.time()) % 100000}"

    def _build_proposal(self, cluster: QueryCluster) -> SkillProposal | None:
        """从聚类构建 skill 提案（结构化 JSON 格式）。

        将聚类数据映射到 SKILL.md 骨架的 8 个模块，生成 structured_content，
        并通过 _assemble_skill_markdown() 拼接为 body 文本。
        每个工作流步骤独立存储，方便后续优化和进化时深入到步骤级别。
        """
        rep = cluster.representative
        name = self._generate_slug(rep)
        description = f"帮助用户完成：{rep[:80]}"
        query_examples = [q for q, _ in cluster.queries[:5]]

        # ── 构建 structured_content ──
        structured: dict[str, Any] = {}

        # 1. metadata
        structured["metadata"] = {
            "name": name,
            "description": description,
            "zh_name": rep[:30] if rep else name,
            "zh_description": description,
            "query_examples": query_examples,
            "category": "通用办公",
            "version": "0.1.0",
        }

        # 2. overview
        core_caps: list[str] = []
        if cluster.tool_usage:
            for tool, cnt in sorted(
                cluster.tool_usage.items(), key=lambda x: -x[1]
            )[:5]:
                core_caps.append(f"使用 {tool} 完成 {cnt} 次操作")
        structured["overview"] = {
            "title": rep[:50] or "自动生成技能",
            "description": f"该技能用于处理用户关于「{rep[:60]}」的需求。",
            "core_capabilities": core_caps,
        }

        # 3. trigger_conditions
        triggers = list(query_examples[:5])
        if not triggers:
            triggers = [rep[:80]]
        structured["trigger_conditions"] = triggers

        # 4. workflow_steps（根据轨迹动态确定步骤）
        structured["workflow_steps"] = self._extract_workflow_steps_from_trajectory(
            tool_usage=cluster.tool_usage or None,
            operation_summary=cluster.operation_summary,
        )

        # 5. script_template（由 LLM 生成时填充，回退时留空）
        structured["script_template"] = {}

        # 6. notes
        note_items: list[str] = [
            "该技能由 crew.evolution 模块自动生成，请根据实际使用情况调整",
            "可根据历史轨迹中的错误信息补充参数说明",
        ]
        if cluster.error_count:
            error_rate = cluster.error_count / max(cluster.total_tool_calls, 1)
            note_items.append(
                f"历史轨迹中共出现 {cluster.error_count} 次错误"
                f"（错误率 {error_rate:.1%}），请重点关注容易出错的工具调用环节"
            )
        if cluster.skills_activated:
            note_items.append(
                f"历史轨迹中已激活技能：{', '.join(cluster.skills_activated[:5])}"
            )
        structured["notes"] = [
            {"title": "使用建议", "items": note_items}
        ]

        # 7. output_format
        structured["output_format"] = {
            "description": "根据用户需求返回结构化的结果。",
            "template": "",
        }

        # 8. common_keywords
        keywords: list[str] = []
        if cluster.trajectory_summaries:
            for s in cluster.trajectory_summaries[:3]:
                keywords.append(s[:50])
        if not keywords:
            keywords = [rep[:30]]
        structured["common_keywords"] = keywords

        # ── 拼接 body 文本（向后兼容）──
        body = self._assemble_skill_markdown(structured)

        return SkillProposal(
            proposed_name=name,
            proposed_slug=name,
            description=description,
            zh_name=rep[:30] if rep else name,
            zh_description=description,
            query_examples=query_examples,
            category="通用办公",
            body=body,
            structured_content=structured,
            source_trajectories=list({lid for _, lid in cluster.queries}),
            source_queries=query_examples,
            reason=f"从 {len(cluster.queries)} 条相似用户查询中提取",
        )
