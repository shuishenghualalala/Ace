"""Evolution 模块数据模型。

定义轨迹日志、技能使用统计、优化建议和新技能提案的数据结构。
所有模型均支持 to_dict / from_dict / to_json / from_json 序列化。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TrajectoryEntry:
    """轨迹日志中的单条记录，对应一条 Message 的结构化摘要。"""

    role: str = ""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    timestamp: str | None = None
    turn_duration: float | None = None
    is_error: bool = False
    thinking: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TrajectoryEntry:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TrajectoryLog:
    """从会话中提取的完整轨迹日志。"""

    log_id: str = ""
    session_id: str = ""
    title: str = ""
    workspace_id: str | None = None
    owner_account_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    entries: list[TrajectoryEntry] = field(default_factory=list)
    skills_activated: list[str] = field(default_factory=list)
    tool_usage: dict[str, int] = field(default_factory=dict)
    error_count: int = 0
    error_tools: list[str] = field(default_factory=list)
    summary: str = ""
    structured_summary: dict[str, Any] = field(default_factory=dict)
    extracted_at: str = ""

    def __post_init__(self):
        if not self.log_id:
            # 基于 session_id + owner_account_id 确定性生成 log_id，
            # 使同一会话重复提取时 log_id 保持不变，_update_index() 的 upsert 才能正确去重。
            seed = f"{self.session_id}:{self.owner_account_id}"
            self.log_id = hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]
        if not self.extracted_at:
            self.extracted_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entries"] = [
            e.to_dict() if isinstance(e, TrajectoryEntry) else e
            for e in self.entries
        ]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TrajectoryLog:
        # 创建副本避免修改原始 dict
        d_copy = dict(d)
        entries_data = d_copy.pop("entries", [])
        entries = [TrajectoryEntry.from_dict(e) for e in entries_data]
        return cls(
            entries=entries,
            **{k: v for k, v in d_copy.items() if k in cls.__dataclass_fields__},
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> TrajectoryLog:
        return cls.from_dict(json.loads(json_str))


@dataclass
class SkillUsageStat:
    """技能使用统计，由历史日志聚合而来。

    当 current_session_id 被传入 get_skill_stats() 时，
    current_session_* 字段记录当前会话的独立统计，用于以当前交互为主、
    历史为辅的判断逻辑。
    """

    skill_slug: str = ""
    skill_name: str = ""
    activation_count: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    avg_message_count: float = 0.0
    user_queries: list[str] = field(default_factory=list)
    tools_used: dict[str, int] = field(default_factory=dict)
    source_log_ids: list[str] = field(default_factory=list)
    # 当前会话独立统计（以当前交互为主）
    current_session_queries: list[str] = field(default_factory=list)
    current_session_error_count: int = 0
    current_session_activation_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SkillUsageStat:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class OptimizationSuggestion:
    """针对单个 skill 的一条优化建议。

    suggestion_type 取值：
      - description: 优化 frontmatter description
      - query_examples: 添加 metadata.query_examples
      - metadata: 补充 zh_name / zh_description
      - body: 在正文中追加注意事项
      - evolve: 基于跨轮次关联扩展已有 skill 的能力（同时添加 query_examples 和 body 扩展）
    """

    skill_slug: str = ""
    skill_name: str = ""
    skill_path: str = ""
    suggestion_type: str = ""
    current_value: str = ""
    suggested_value: str = ""
    reason: str = ""
    confidence: float = 0.0
    applied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> OptimizationSuggestion:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SkillProposal:
    """新技能生成提案。

    structured_content 字段存储按 SKILL.md 骨架拆分的结构化 JSON，
    包含 metadata / overview / trigger_conditions / workflow_steps /
    script_template / notes / output_format / common_keywords 等模块，
    每个工作流步骤独立存储，方便后续优化和进化时深入到步骤级别。

    body 字段保留用于向后兼容（由 structured_content 拼接生成或回退时直接填充）。
    """

    proposal_id: str = ""
    proposed_name: str = ""
    proposed_slug: str = ""
    description: str = ""
    zh_name: str = ""
    zh_description: str = ""
    query_examples: list[str] = field(default_factory=list)
    category: str = "通用办公"
    body: str = ""
    structured_content: dict[str, Any] = field(default_factory=dict)
    source_trajectories: list[str] = field(default_factory=list)
    source_queries: list[str] = field(default_factory=list)
    reason: str = ""
    created_at: str = ""
    created: bool = False
    created_path: str = ""

    def __post_init__(self):
        if not self.proposal_id:
            self.proposal_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SkillProposal:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CrossRoundAssociation:
    """跨轮次关联检测结果。

    记录本轮任务与上一轮交互/skill 的关联性分析结果，
    用于决定是进化已有 skill 还是创建新 skill。

    核心字段：
      - independence_score: 任务独立性评分（0-1，1=完全独立）
      - action: "evolve"（进化已有 skill）或 "create"（创建新 skill）
      - 独立性 >= 0.7 时创建新 skill，< 0.7 时进化已有 skill
    """

    current_query: str = ""
    related_skill_slug: str = ""
    association_score: float = 0.0       # 0-1, 关联度（越高越关联）
    independence_score: float = 1.0      # 0-1, 独立性（越高越独立）
    association_type: str = ""           # continuation/expansion/same_domain/tool_chain/none
    action: str = "create"               # "evolve" or "create"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CrossRoundAssociation:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
