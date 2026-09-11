"""LLM Wiki 数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

PageType = Literal["entity", "topic", "source", "comparison", "synthesis"]
_PAGE_TYPES = {"entity", "topic", "source", "comparison", "synthesis"}
SourceType = Literal["upload", "url", "session", "paste", "image", "video"]
ParseStatus = Literal["pending", "parsed", "failed"]
Confidence = Literal["high", "medium", "low"]
PlanAction = Literal["create", "update", "skip", "contest"]
SourceKind = Literal[
    "article", "pdf", "word", "excel", "ppt", "note",
    "session", "image", "video", "asset",
]
ExtractionState = Literal[
    "available", "not_installed", "env_unavailable",
    "runtime_failed", "unsupported", "empty_result",
]

_SOURCE_KINDS = {
    "article", "pdf", "word", "excel", "ppt", "note",
    "session", "image", "video", "asset",
}
_EXTRACTION_STATES = {
    "available", "not_installed", "env_unavailable",
    "runtime_failed", "unsupported", "empty_result",
}


def _source_kind_from_legacy(data: dict[str, Any]) -> str:
    value = str(data.get("source_kind") or "").strip()
    if value in _SOURCE_KINDS:
        return value
    source_type = str(data.get("source_type") or "")
    file_type = str(data.get("file_type") or "").lower()
    title = str(data.get("title") or "").lower()
    if source_type == "url":
        return "video" if "youtube" in str(data.get("source_url") or "") else "article"
    if source_type == "session":
        return "session"
    if source_type == "image":
        return "image"
    if source_type == "video":
        return "video"
    if "pdf" in file_type or title.endswith(".pdf"):
        return "pdf"
    if any(token in file_type or title.endswith(token) for token in ("word", ".doc", ".docx", ".odt", ".rtf")):
        return "word"
    if any(token in file_type or title.endswith(token) for token in ("excel", "spreadsheet", ".xls", ".xlsx", ".csv", ".tsv")):
        return "excel"
    if any(token in file_type or title.endswith(token) for token in ("powerpoint", "presentation", ".ppt", ".pptx")):
        return "ppt"
    return "note" if source_type == "paste" or file_type.startswith("text/") else "asset"


def _valid_extraction_state(value: Any) -> str:
    state = str(value or "available").strip()
    return state if state in _EXTRACTION_STATES else "available"


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _serializable_repr(self) -> dict[str, Any]:
    """默认 to_dict：递归 asdict（适合无嵌套 dataclass 的纯数据类）。"""
    return asdict(self)


def _deserialize_from_dict(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """从 dict 中提取与 dataclass 字段同名的键，仅此而已。
    调用方在返回的 kwargs 上做类型转换后再 cls(**kwargs)。
    """
    field_names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in field_names}


@dataclass
class RawSource:
    """原始信息源（上传文件 / URL / 会话 / 粘贴文本）。"""

    id: str
    title: str
    source_type: SourceType
    parsed_path: str
    original_path: str | None = None
    file_type: str | None = None
    size: int = 0
    created_at: float = 0.0
    session_id: str | None = None
    parse_status: ParseStatus = "pending"
    parse_error: str | None = None
    original_sha256: str | None = None
    content_sha256: str | None = None
    drift_from: str | None = None
    is_duplicate: bool = False
    source_url: str | None = None
    source_kind: SourceKind = "note"
    source_platform: str = ""
    adapter_name: str = "builtin"
    original_ref: str | None = None
    extraction_state: ExtractionState = "available"
    # 来源版本链与刷新状态。RawSource 不可变：刷新失败只记 last_refresh_*，
    # 旧版本状态保持 parsed/available；刷新出内容变化的新版本时，旧版本
    # superseded_by 指向新版本，默认检索与入库只认当前版本（is_current）。
    superseded_by: str | None = None
    last_refresh_at: float = 0.0
    last_refresh_error: str | None = None
    # 第二层轻量摘要生成的来源级元数据，仅用于 inbox 筛选与整理推荐，
    # 不等同于 WikiPage.tags，也不进入页面标签体系。
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    doc_type: str = ""
    ingest_recommend: bool = False
    ingest_reason: str = ""
    ingest_status: str = "pending"  # pending | recommended | ignored | ingested | failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source_type": self.source_type,
            "original_path": self.original_path,
            "parsed_path": self.parsed_path,
            "file_type": self.file_type,
            "size": self.size,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "parse_status": self.parse_status,
            "parse_error": self.parse_error,
            "original_sha256": self.original_sha256,
            "content_sha256": self.content_sha256,
            "drift_from": self.drift_from,
            "is_duplicate": self.is_duplicate,
            "source_url": self.source_url,
            "source_kind": self.source_kind,
            "source_platform": self.source_platform,
            "adapter_name": self.adapter_name,
            "original_ref": self.original_ref,
            "extraction_state": self.extraction_state,
            "superseded_by": self.superseded_by,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_error": self.last_refresh_error,
            "summary": self.summary,
            "tags": self.tags,
            "doc_type": self.doc_type,
            "ingest_recommend": self.ingest_recommend,
            "ingest_reason": self.ingest_reason,
            "ingest_status": self.ingest_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawSource":
        parse_status = data.get("parse_status", "")
        # 兼容旧数据：没有 parse_status 且 parsed_path 非空时视为已解析
        if not parse_status:
            parse_status = "parsed" if data.get("parsed_path") else "pending"
        if parse_status not in ("pending", "parsed", "failed"):
            parse_status = "pending"
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            source_type=data.get("source_type", "paste"),  # type: ignore[arg-type]
            parsed_path=str(data.get("parsed_path", "")),
            original_path=data.get("original_path"),
            file_type=data.get("file_type"),
            size=int(data.get("size", 0)),
            created_at=float(data.get("created_at", 0.0)),
            session_id=data.get("session_id"),
            parse_status=parse_status,  # type: ignore[arg-type]
            parse_error=data.get("parse_error"),
            original_sha256=data.get("original_sha256"),
            content_sha256=data.get("content_sha256"),
            drift_from=data.get("drift_from"),
            is_duplicate=bool(data.get("is_duplicate", False)),
            source_url=data.get("source_url"),
            source_kind=_source_kind_from_legacy(data),  # type: ignore[arg-type]
            source_platform=str(data.get("source_platform", "")),
            adapter_name=str(data.get("adapter_name", "builtin") or "builtin"),
            original_ref=data.get("original_ref"),
            extraction_state=_valid_extraction_state(data.get("extraction_state")),  # type: ignore[arg-type]
            superseded_by=data.get("superseded_by"),
            last_refresh_at=float(data.get("last_refresh_at", 0.0) or 0.0),
            last_refresh_error=data.get("last_refresh_error"),
            summary=str(data.get("summary", "")),
            tags=[str(t) for t in (data.get("tags") or []) if str(t).strip()],
            doc_type=str(data.get("doc_type", "")),
            ingest_recommend=bool(data.get("ingest_recommend", False)),
            ingest_reason=str(data.get("ingest_reason", "")),
            ingest_status=str(data.get("ingest_status", "pending") or "pending"),
        )

    @property
    def is_current(self) -> bool:
        """是否为来源版本链的当前版本；被新版本取代后为 False。"""
        return self.superseded_by is None


@dataclass
class WikiEvidence:
    """支撑一条知识主张的来源定位。"""

    source_id: str
    locator: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        result = {"source_id": self.source_id}
        if self.locator:
            result["locator"] = self.locator
        if self.excerpt:
            result["excerpt"] = self.excerpt
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiEvidence":
        return cls(
            source_id=str(data.get("source_id", "")),
            locator=str(data.get("locator", "")),
            excerpt=str(data.get("excerpt", "")),
        )


@dataclass
class WikiClaim:
    """可追溯的最小知识主张。"""

    statement: str
    evidence: list[WikiEvidence] = field(default_factory=list)
    confidence: Confidence = "medium"
    contested: bool = False
    contradictions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "evidence": [item.to_dict() for item in self.evidence],
            "confidence": self.confidence,
            "contested": self.contested,
            "contradictions": list(self.contradictions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiClaim":
        confidence = str(data.get("confidence", "medium"))
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        raw_evidence = data.get("evidence") or []
        evidence = [
            WikiEvidence.from_dict(item)
            for item in raw_evidence
            if isinstance(item, dict) and item.get("source_id")
        ]
        return cls(
            statement=str(data.get("statement", "")),
            evidence=evidence,
            confidence=confidence,  # type: ignore[arg-type]
            contested=bool(data.get("contested", False)),
            contradictions=[
                str(item) for item in (data.get("contradictions") or []) if str(item).strip()
            ],
        )


@dataclass
class WikiRelation:
    """从当前页面指向另一规范页面的有类型关系。"""

    target_page_id: str
    relation: str = "related"

    def to_dict(self) -> dict[str, str]:
        return {"target_page_id": self.target_page_id, "relation": self.relation}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiRelation":
        return cls(
            # ``target`` 只在 KB 初始化迁移前用于读取旧 frontmatter。
            target_page_id=str(data.get("target_page_id") or data.get("target", "")),
            relation=str(data.get("relation", "related")) or "related",
        )


@dataclass
class WikiPage:
    """AI 编译后的 Wiki 页面。"""

    id: str
    page_type: PageType
    title: str
    content: str
    file_path: str
    sources: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    aliases: list[str] = field(default_factory=list)
    summary: str | None = None
    claims: list[WikiClaim] = field(default_factory=list)
    confidence: Confidence | None = None
    contested: bool = False
    contradictions: list[str] = field(default_factory=list)
    relations: list[WikiRelation] = field(default_factory=list)
    # 证据底座变化（如支撑来源被删除）后，页面正文可能与剩余证据脱节。
    # stale=True 表示需要重新整理；lint 会把它送入复核队列。
    stale: bool = False

    def to_dict(self, brief: bool = False) -> dict[str, Any]:
        """序列化为字典。

        brief=True 时用于列表接口：不返回完整 content，仅返回 summary（若有），
        以减少网络传输和前端渲染压力。
        """
        result: dict[str, Any] = {
            "id": self.id,
            "page_type": self.page_type,
            "title": self.title,
            "file_path": self.file_path,
            "sources": self.sources,
            "related": self.related,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "aliases": self.aliases,
            "confidence": self.confidence,
            "contested": self.contested,
            "contradictions": self.contradictions,
            "relations": [relation.to_dict() for relation in self.relations],
            "stale": self.stale,
        }
        if brief:
            result["claim_count"] = len(self.claims)
            if self.summary:
                result["summary"] = self.summary
        else:
            result["content"] = self.content
            result["claims"] = [claim.to_dict() for claim in self.claims]
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiPage":
        # 旧磁盘数据（.md frontmatter / .kb.json）中的 status 等多余键直接忽略。
        summary = data.get("summary")
        page_type = str(data.get("page_type", "topic"))
        if page_type not in _PAGE_TYPES:
            raise ValueError(f"不支持的 Wiki 页面类型: {page_type}")
        confidence = data.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = None
        raw_claims = data.get("claims") or []
        raw_relations = data.get("relations") or []
        return cls(
            id=str(data.get("id", "")),
            page_type=page_type,  # type: ignore[arg-type]
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            file_path=str(data.get("file_path", "")),
            sources=list(data.get("sources") or []),
            related=list(data.get("related") or []),
            tags=list(data.get("tags") or []),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            aliases=list(data.get("aliases") or []),
            summary=str(summary) if summary is not None else None,
            claims=[
                WikiClaim.from_dict(item)
                for item in raw_claims
                if isinstance(item, dict) and str(item.get("statement", "")).strip()
            ],
            confidence=confidence,  # type: ignore[arg-type]
            contested=bool(data.get("contested", False)),
            contradictions=[
                str(item) for item in (data.get("contradictions") or []) if str(item).strip()
            ],
            relations=[
                WikiRelation.from_dict(item)
                for item in raw_relations
                if isinstance(item, dict)
                and str(item.get("target_page_id") or item.get("target", "")).strip()
            ],
            stale=bool(data.get("stale", False)),
        )

    def content_from_claims(self) -> str:
        """依据剩余 claims 确定性重编正文骨架。

        支撑来源被删除后，原 LLM 叙述可能引用已不存在的证据。本方法用剩余
        claims 生成可追溯的最小正文；调用方应同时置 stale=True 提示重新整理，
        避免正文残留已无证据支撑的结论（"幽灵知识"）。
        """
        lines = [f"# {self.title}", ""]
        if self.claims:
            lines.extend(["## 关键主张", ""])
            for claim in self.claims:
                evidence_ids = ", ".join(
                    sorted({item.source_id for item in claim.evidence if item.source_id})
                )
                meta = [f"confidence={claim.confidence}"]
                if evidence_ids:
                    meta.append(f"evidence={evidence_ids}")
                if claim.contested:
                    meta.append("contested")
                lines.append(f"- {claim.statement} [{'; '.join(meta)}]")
            lines.extend(["", "> 本页正文因支撑来源删除已依据剩余主张重编，标记为待整理。"])
        else:
            lines.extend(["> 本页已无支撑主张（相关来源被删除），标记为待整理。", ""])
        return "\n".join(lines)


@dataclass
class WikiGraph:
    """Wiki 页面关系图（MVP 简单实现）。"""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

    to_dict = _serializable_repr


@dataclass
class HomeIntro:
    """Home.md「内容导读」的缓存元数据。

    导读专为 Home.md 首页撰写，只在页面/来源内容 hash 变化时重新生成。
    """

    text: str = ""
    # 首页推荐问题（随导读一起由 LLM 生成，见 summary._HOME_INTRO_PROMPT）。
    questions: list[str] = field(default_factory=list)
    content_hash: str = ""
    generated_at: float = 0.0
    status: Literal["ready", "generating", "empty", "stale"] = "empty"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "questions": list(self.questions),
            "content_hash": self.content_hash,
            "generated_at": self.generated_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HomeIntro":
        status = data.get("status", "empty")
        if status not in ("ready", "generating", "empty", "stale"):
            status = "empty"
        questions = data.get("questions")
        return cls(
            text=str(data.get("text", "")),
            questions=[str(q) for q in questions if str(q).strip()]
            if isinstance(questions, list)
            else [],
            content_hash=str(data.get("content_hash", "")),
            generated_at=float(data.get("generated_at", 0.0)),
            status=status,  # type: ignore[arg-type]
        )


@dataclass
class KnowledgeBase:
    """知识库元数据。"""

    id: str
    name: str
    created_at: float = 0.0
    updated_at: float = 0.0
    vault_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "vault_path": self.vault_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeBase":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            vault_path=str(data.get("vault_path", "")),
        )


@dataclass
class IngestResult:
    """单次 ingest 的结果。"""

    source_id: str
    pages: list[WikiPage] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "pages": [p.to_dict() for p in self.pages],
            "issues": self.issues,
        }


@dataclass
class PlannedPage:
    """计划中的页面变更。"""

    title: str
    page_type: PageType
    action: PlanAction
    content: str = ""
    is_new: bool = False
    existing_title: str = ""
    aliases: list[str] = field(default_factory=list)
    reason: str = ""
    claims: list[WikiClaim] = field(default_factory=list)
    confidence: Confidence | None = None
    contested: bool = False
    contradictions: list[str] = field(default_factory=list)
    # 目标页快照：update/contest 时记录命中页面的 id 与正文 hash，
    # apply 阶段据此检测目标页是否在计划生成后被外部修改。
    target_page_id: str = ""
    target_content_sha256: str = ""

    def to_dict(self, brief: bool = False) -> dict[str, Any]:
        """序列化为字典。

        brief=True 时用于计划预览接口：不返回完整 content，仅返回前 500 字符摘要，
        避免大文档的 source 全文把 Agent 上下文撑满。
        """
        content = self.content
        if brief and len(content) > 500:
            content = content[:500] + "\n\n...(内容已省略，apply 时会使用完整内容)..."
        return {
            "title": self.title,
            "page_type": self.page_type,
            "action": self.action,
            "content": content,
            "is_new": self.is_new,
            "existing_title": self.existing_title,
            "aliases": list(self.aliases),
            "reason": self.reason,
            "claims": [claim.to_dict() for claim in self.claims],
            "confidence": self.confidence,
            "contested": self.contested,
            "contradictions": list(self.contradictions),
            "target_page_id": self.target_page_id,
            "target_content_sha256": self.target_content_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlannedPage":
        action = str(data.get("action", "create"))
        if action not in ("create", "update", "skip", "contest"):
            action = "create"
        confidence = data.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = None
        page_type = str(data.get("page_type", "topic"))
        if page_type not in _PAGE_TYPES:
            raise ValueError(f"不支持的 Wiki 页面类型: {page_type}")
        return cls(
            title=str(data.get("title", "")),
            page_type=page_type,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            content=str(data.get("content", "")),
            is_new=bool(data.get("is_new", True)),
            existing_title=str(data.get("existing_title", "")),
            aliases=list(data.get("aliases") or []),
            reason=str(data.get("reason", "")),
            claims=[
                WikiClaim.from_dict(item)
                for item in (data.get("claims") or [])
                if isinstance(item, dict) and str(item.get("statement", "")).strip()
            ],
            confidence=confidence,  # type: ignore[arg-type]
            contested=bool(data.get("contested", False)),
            contradictions=[
                str(item) for item in (data.get("contradictions") or []) if str(item).strip()
            ],
            target_page_id=str(data.get("target_page_id", "") or ""),
            target_content_sha256=str(data.get("target_content_sha256", "") or ""),
        )


@dataclass
class PlanResult:
    """ingest plan 的结果。"""

    source_id: str
    source_title: str = ""
    source_content_sha256: str = ""
    planned_pages: list[PlannedPage] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    total_new: int = 0
    total_update: int = 0
    total_contested: int = 0
    analysis_stats: dict[str, int] = field(default_factory=dict)
    # 计划指纹：source_id + source 内容 hash + 规划页面与关系的规范化摘要。
    # apply 前必须与磁盘 plan 一致，防止用户确认旧计划后被新计划覆盖。
    plan_fingerprint: str = ""

    def to_dict(self, brief: bool = False) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_title": self.source_title,
            "source_content_sha256": self.source_content_sha256,
            "planned_pages": [p.to_dict(brief=brief) for p in self.planned_pages],
            "relationships": self.relationships,
            "issues": self.issues,
            "total_new": self.total_new,
            "total_update": self.total_update,
            "total_contested": self.total_contested,
            "analysis_stats": dict(self.analysis_stats),
            "plan_fingerprint": self.plan_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanResult":
        planned_pages = data.get("planned_pages") or []
        return cls(
            source_id=str(data.get("source_id", "")),
            source_title=str(data.get("source_title", "")),
            source_content_sha256=str(data.get("source_content_sha256", "")),
            planned_pages=[PlannedPage.from_dict(p) for p in planned_pages if isinstance(p, dict)],
            relationships=list(data.get("relationships") or []),
            issues=list(data.get("issues") or []),
            total_new=int(data.get("total_new", 0)),
            total_update=int(data.get("total_update", 0)),
            total_contested=int(data.get("total_contested", 0)),
            analysis_stats={
                str(key): int(value)
                for key, value in (data.get("analysis_stats") or {}).items()
                if isinstance(value, (int, float))
            },
            plan_fingerprint=str(data.get("plan_fingerprint", "") or ""),
        )


@dataclass
class CompileResult:
    """全量编译结果。"""

    ingested: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    to_dict = _serializable_repr


@dataclass
class LintIssue:
    """Lint 发现的问题。

    kind 取值：
    - broken_link: [[链接]] 指向不存在的页面
    - orphan: 没有任何入链/出链的页面
    - format_violation: 页面格式违规（如缺少标题、文件名不规范）
    - outdated_marker: 包含「最新」「目前」等时效性用语
    - entity_gap: 多个页面引用某个重要知识对象，但没有对应 Entity 页面
    - contradiction: 不同页面对同一概念陈述矛盾
    - stale: 页面内容可能过时
    - duplicate: 同一实体被拆成多个页面
    - conflict: 通用冲突
    """

    kind: Literal[
        "broken_link",
        "orphan",
        "format_violation",
        "outdated_marker",
        "entity_gap",
        "contradiction",
        "stale",
        "duplicate",
        "conflict",
        "missing_source",
        "alias_conflict",
        "low_confidence",
        "contested",
        "index_drift",
    ]
    page_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class WikiOrientation:
    """Wiki 知识库全景信息，供 Agent 在操作前 orientation。"""

    kb_id: str
    kb_name: str
    schema: dict[str, Any] = field(default_factory=dict)
    index: dict[str, Any] = field(default_factory=dict)
    recent_log: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    candidate_index: dict[str, Any] = field(default_factory=dict)
    vault_path: str = ""
    generated_at: float = 0.0

    to_dict = _serializable_repr
