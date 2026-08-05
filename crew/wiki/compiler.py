"""Wiki 编译器：把原始信息源转换为结构化 Wiki 页面。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.state.logging import get_logger

from .schemas import (
    CompileResult,
    Confidence,
    IngestResult,
    LintIssue,
    PageType,
    PlannedPage,
    PlanResult,
    RawSource,
    WikiClaim,
    WikiEvidence,
    WikiOrientation,
    WikiPage,
    WikiRelation,
)
from .sources import SOURCE_DIRS
from .store import WikiStore
from .store._ids import filename_from_title, page_id, source_page_id, unique_file_path
from ._llm import _is_capacity_error, chat_text
from ._utils import normalize_page_key
from .summary import WikiSummarizer

log = get_logger("wiki.compiler")

ProgressFn = Callable[[str, int, dict[str, Any]], Awaitable[None]]


def _check_cancelled(cancel_event: asyncio.Event | None) -> None:
    """如果取消事件已设置，抛出 CancelledError。"""
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("Wiki ingest 已取消")


def _load_analysis_json(text: str) -> dict[str, Any] | None:
    """容错解析 LLM 返回的分析 JSON。

    依次尝试：剥 markdown 围栏 -> 首尾大括号切片 -> json.loads -> 去尾随逗号重试。
    全部失败返回 None，由调用方决定重试/降级。通过围栏剥离和首尾大括号切片
    处理常见格式偏差，不引入 json_repair 等额外依赖。
    """
    if not text:
        return None
    candidate = _strip_md_fences(text).strip()
    if not candidate:
        return None
    original_candidate = candidate
    # LLM 可能在 JSON 前后掺杂解释文字，切到最外层 {} 之间
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = candidate[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 修复常见格式瑕疵：对象/数组尾随逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return _salvage_complete_typed_units(original_candidate)


def _salvage_array_objects(text: str, array_name: str) -> list[dict[str, Any]]:
    """从可能截断的指定数组中读取已经闭合的顶层对象。"""
    match = re.search(rf'"{re.escape(array_name)}"\s*:\s*\[', text)
    if match is None:
        return []

    units: list[dict[str, Any]] = []
    object_start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index in range(match.end(), len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "]" and depth == 0:
            break
        if char == "{":
            if depth == 0:
                object_start = index
            depth += 1
            continue
        if char != "}" or depth == 0:
            continue
        depth -= 1
        if depth != 0 or object_start is None:
            continue
        try:
            unit = json.loads(text[object_start : index + 1])
        except json.JSONDecodeError:
            object_start = None
            continue
        if isinstance(unit, dict):
            units.append(unit)
        object_start = None
    return units


def _salvage_complete_typed_units(text: str) -> dict[str, Any] | None:
    """保留知识数组中已闭合的单元，避免因尾部截断整块重跑。"""
    salvaged = {
        bucket: _salvage_array_objects(text, bucket)
        for bucket in ("entities", "topics")
    }
    total = sum(len(units) for units in salvaged.values())
    if total == 0:
        return None
    return {
        "format": _ANALYSIS_PROTOCOL_VERSION,
        **salvaged,
        "_truncated": True,
        "_analysis_warnings": [
            f"LLM 输出被截断，已保留 {total} 个完整知识单元"
        ],
    }


def _strip_md_fences(text: str) -> str:
    """剥离 LLM 返回 JSON 时偶尔包裹的 markdown 代码块标记。"""
    t = text.strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()

# 阶段 → (percent, label)
# 前端只能明显感知到 LLM 分析这一个耗时阶段，其他本地保存/建索引步骤
# 非常快、用户根本看不到。因此只暴露三个节点：读取文档 → LLM 分析内容
# （10%→99% 平滑推进）→ 编译完成（100%），视觉上最连续自然。
_PROGRESS_STAGES: dict[str, tuple[int, str]] = {
    "load": (5, "读取文档"),
    "analyze": (10, "LLM 分析内容"),
    "done": (100, "编译完成"),
}

# analyze 阶段内部平滑推进的上限（含），完成后直接进入 100%。
_ANALYZE_SMOOTH_MAX = 99
# analyze 阶段每次进度递增的步长与间隔。
_ANALYZE_SMOOTH_STEP = 1
_ANALYZE_SMOOTH_INTERVAL = 0.4

# 轻量知识单元协议版本。修改 Prompt 或单元语义时必须升级，以使旧缓存失效。
_ANALYSIS_PROTOCOL_VERSION = "knowledge-units-v7"

# 长文档分块分析阈值。每块输出已被限制为紧凑知识单元，因此可以让模型读取
# 更完整的章节上下文，减少请求数量和跨块重复。
_CHUNK_SIZE_CHARS = 48_000
# 低于此长度直接单轮分析，避免无意义分块
_SINGLE_PASS_THRESHOLD = 48_000
# 维持保守的两路并发，兼容低并发模型端点；速度主要来自更少的请求和更短的
# 有界输出。普通错误只重试一次，429/503 容量错误不重试。
_ANALYZE_CHUNK_CONCURRENCY = 2
_ANALYZE_CHUNK_MAX_RETRIES = 1
_SHORT_SOURCE_THRESHOLD = 1_000
_LONG_SOURCE_ENTITY_LIMIT = 5
_LONG_SOURCE_TOPIC_LIMIT = 3
_SHORT_SOURCE_ENTITY_LIMIT = 3
# 推理型模型（如 deepseek-v4 系列）会先烧掉一笔不可见的推理 token，过小的
# 上限会让正文一个字都吐不出来（实测空返回）；这里只是上限而非目标，
# 非推理模型不受影响。
_ANALYSIS_MAX_TOKENS = 20_000


def _split_into_semantic_chunks(content: str, max_size: int = _CHUNK_SIZE_CHARS) -> list[str]:
    """按 Markdown 标题/段落把长文档切成语义块，保持原文顺序。

    切分策略：
    1. 先按 Markdown 标题（# / ## 等）切分，尽量保证章节语义完整；
    2. 单章仍超过 max_size 时，按段落（空行分隔）继续切；
    3. 单个段落超长时，直接作为一个块（极少出现）。
    """
    if len(content) <= max_size:
        return [content]

    header_pattern = re.compile(r"(?m)^(?=#+\s)")
    sections = header_pattern.split(content)

    chunks: list[str] = []
    current = ""

    def _flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for section in sections:
        section = section.strip()
        if not section:
            continue

        if len(current) + len(section) + 2 <= max_size:
            current = f"{current}\n\n{section}".strip() if current else section
            continue

        _flush()

        if len(section) <= max_size:
            current = section
            continue

        # 章节超长，按段落切
        for para in section.split("\n\n"):
            if len(para) > max_size:
                _flush()
                for start in range(0, len(para), max_size):
                    piece = para[start : start + max_size].strip()
                    if piece:
                        chunks.append(piece)
                continue
            if len(current) + len(para) + 2 <= max_size:
                current = f"{current}\n\n{para}".strip() if current else para
            else:
                _flush()
                current = para

    _flush()
    return chunks


def _compact_description(existing: str, incoming: str, limit: int = 1_200) -> str:
    """合并少量页面导语，避免多个 chunk 把正文重复塞进计划。"""
    merged = _merge_content(existing, incoming)
    if len(merged) <= limit:
        return merged
    return merged[:limit].rsplit("\n", 1)[0].rstrip()


def _knowledge_units_to_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """把分类型轻量知识单元转换为页面规划结构。

    LLM 只负责提供原子知识与来源定位；页面聚合、规范匹配和变更动作继续由
    Python 决定。概念、方法、原则和机制统一作为 entity 的子类型，不生成
    独立 concept 页面，也不接受旧 concepts 数组。
    payload 带协议标记或数组元素包含 subject 时执行转换。
    """
    typed_buckets = ("entities", "topics")
    is_typed_units = payload.get("format") == _ANALYSIS_PROTOCOL_VERSION or any(
        isinstance(raw_unit, dict) and "subject" in raw_unit
        for bucket in typed_buckets
        for raw_unit in (
            payload.get(bucket)
            if isinstance(payload.get(bucket), list)
            else []
        )
    )
    if not is_typed_units:
        return payload

    raw_summary = payload.get("source_summary")
    analysis: dict[str, Any] = {
        "entities": [],
        "topics": [],
        "relationships": [],
        "source_summary": raw_summary if isinstance(raw_summary, dict) else {},
        "_analysis_warnings": list(payload.get("_analysis_warnings") or []),
    }
    for bucket in typed_buckets:
        raw_units = payload.get(bucket)
        if not isinstance(raw_units, list):
            continue
        for raw_unit in raw_units:
            if not isinstance(raw_unit, dict):
                continue
            subject = str(raw_unit.get("subject", "")).strip()
            statement = str(raw_unit.get("statement", "")).strip()
            if not subject or not statement:
                continue

            contradictions = [
                str(value).strip()
                for value in (raw_unit.get("contradictions") or [])
                if str(value).strip()
            ]
            item: dict[str, Any] = {
                "name": subject,
                "description": (
                    str(raw_unit.get("summary", "")).strip() or statement
                ),
                "aliases": [
                    str(value).strip()
                    for value in (raw_unit.get("aliases") or [])
                    if str(value).strip()
                ],
                "importance": (
                    "core"
                    if str(raw_unit.get("importance", "")).lower() == "core"
                    else "supporting"
                ),
                "confidence": _valid_confidence(raw_unit.get("confidence")),
                "contested": bool(raw_unit.get("contested", False)),
                "contradictions": contradictions,
                "claims": [
                    {
                        "statement": statement,
                        "locator": str(raw_unit.get("locator", "")).strip(),
                        "excerpt": str(raw_unit.get("excerpt", "")).strip()[:200],
                        "confidence": _valid_confidence(
                            raw_unit.get("confidence")
                        ),
                        "contested": bool(raw_unit.get("contested", False)),
                        "contradictions": contradictions,
                    }
                ],
            }
            item["entity_kind"] = str(
                raw_unit.get("entity_kind", "")
            ).strip()
            analysis[bucket].append(item)

            for raw_relation in raw_unit.get("relations") or []:
                if not isinstance(raw_relation, dict):
                    continue
                target = str(raw_relation.get("target", "")).strip()
                if (
                    not target
                    or normalize_page_key(target) == normalize_page_key(subject)
                ):
                    continue
                analysis["relationships"].append(
                    {
                        "source": subject,
                        "target": target,
                        "relation": str(
                            raw_relation.get("relation", "related")
                        ).strip() or "related",
                    }
                )
    return analysis


def _resolve_cross_type_collisions(analysis: dict[str, Any]) -> None:
    """同名候选跨类型冲突时只保留一个规范类型。

    优先保留拥有更多不同主张的候选；数量相同时 entity > topic。
    """
    priorities = {"entities": 1, "topics": 0}
    candidates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for bucket in ("entities", "topics"):
        for item in analysis.get(bucket, []):
            key = normalize_page_key(str(item.get("name", "")))
            if key:
                candidates.setdefault(key, []).append((bucket, item))

    for matches in candidates.values():
        buckets = {bucket for bucket, _ in matches}
        if len(buckets) < 2:
            continue

        def _rank(candidate: tuple[str, dict[str, Any]]) -> tuple[int, int]:
            bucket, item = candidate
            statements = {
                normalize_page_key(str(claim.get("statement", "")))
                for claim in (item.get("claims") or [])
                if isinstance(claim, dict)
            }
            return len(statements - {""}), priorities[bucket]

        winner_bucket, winner = max(matches, key=_rank)
        for bucket, item in matches:
            if bucket == winner_bucket and item is winner:
                continue
            analysis[bucket].remove(item)


def _merge_analysis_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个分块的 LLM 分析结果合并成一个，按标题去重并合并描述。"""
    merged: dict[str, Any] = {
        "entities": [],
        "topics": [],
        "relationships": [],
        "source_summary": {"one_sentence": "", "core_points": []},
        "_analysis_warnings": [],
    }

    seen_entities: dict[str, dict[str, Any]] = {}
    seen_topics: dict[str, dict[str, Any]] = {}
    seen_relationships: set[tuple[str, str, str]] = set()

    def _normalize(name: str) -> str:
        return str(name or "").strip().lower()

    for raw_result in results:
        result = _knowledge_units_to_analysis(raw_result)
        merged["_analysis_warnings"].extend(result.get("_analysis_warnings") or [])
        source_summary = result.get("source_summary")
        if isinstance(source_summary, dict):
            one_sentence = str(source_summary.get("one_sentence") or "").strip()
            if one_sentence and not merged["source_summary"]["one_sentence"]:
                merged["source_summary"]["one_sentence"] = one_sentence
            for point in source_summary.get("core_points") or []:
                point_text = str(point).strip()
                if (
                    point_text
                    and point_text not in merged["source_summary"]["core_points"]
                    and len(merged["source_summary"]["core_points"]) < 5
                ):
                    merged["source_summary"]["core_points"].append(point_text)
        entity_items = result.get("entities") or []
        for ent in entity_items:
            name = _normalize(ent.get("name"))
            if not name:
                continue
            if name in seen_entities:
                existing = seen_entities[name]
                aliases = list(existing.get("aliases") or [])
                aliases.extend(ent.get("aliases") or [])
                existing["aliases"] = list(dict.fromkeys(aliases))
                desc = str(ent.get("description") or "").strip()
                if desc:
                    existing["description"] = _compact_description(
                        existing.get("description", ""),
                        desc,
                    )
                existing.setdefault("claims", []).extend(ent.get("claims") or [])
                existing["contested"] = bool(existing.get("contested")) or bool(ent.get("contested"))
                existing.setdefault("contradictions", []).extend(ent.get("contradictions") or [])
                if ent.get("importance") == "core":
                    existing["importance"] = "core"
            else:
                seen_entities[name] = dict(ent)

        for top in result.get("topics", []):
            name = _normalize(top.get("name"))
            if not name:
                continue
            if name in seen_topics:
                existing = seen_topics[name]
                for key in ("description", "summary"):
                    new_val = str(top.get(key) or "").strip()
                    if new_val:
                        existing[key] = _compact_description(
                            str(existing.get(key) or ""),
                            new_val,
                        )
                for key in ("decisions", "pitfalls"):
                    existing.setdefault(key, []).extend(top.get(key) or [])
                existing.setdefault("claims", []).extend(top.get("claims") or [])
                existing["contested"] = bool(existing.get("contested")) or bool(top.get("contested"))
                existing.setdefault("contradictions", []).extend(top.get("contradictions") or [])
                if top.get("importance") == "core":
                    existing["importance"] = "core"
            else:
                seen_topics[name] = dict(top)

        for rel in result.get("relationships", []):
            src = _normalize(rel.get("source"))
            tgt = _normalize(rel.get("target"))
            relation = str(rel.get("relation", "mentions")).strip().lower()
            if not src or not tgt or src == tgt:
                continue
            key = (src, tgt, relation)
            if key not in seen_relationships:
                seen_relationships.add(key)
                merged["relationships"].append(rel)

    merged["entities"] = list(seen_entities.values())
    merged["topics"] = list(seen_topics.values())
    _resolve_cross_type_collisions(merged)
    return merged


def _candidate_rank(item: dict[str, Any]) -> tuple[int, int, int]:
    """稳定排序知识候选：核心优先，其次证据数量和置信度。"""
    importance = 1 if item.get("importance") == "core" else 0
    claim_count = len({
        normalize_page_key(str(claim.get("statement", "")))
        for claim in (item.get("claims") or [])
        if isinstance(claim, dict) and normalize_page_key(str(claim.get("statement", "")))
    })
    confidence = _CONFIDENCE_ORDER.get(str(item.get("confidence") or "medium"), 1)
    return importance, claim_count, confidence


def _apply_document_limits(analysis: dict[str, Any], content_length: int) -> None:
    """按整篇素材限制页面候选，而不是让配额随分块数量增长。"""
    short_source = content_length <= _SHORT_SOURCE_THRESHOLD
    entity_limit = (
        _SHORT_SOURCE_ENTITY_LIMIT if short_source else _LONG_SOURCE_ENTITY_LIMIT
    )
    topic_limit = 0 if short_source else _LONG_SOURCE_TOPIC_LIMIT
    analysis["entities"] = sorted(
        analysis.get("entities") or [],
        key=_candidate_rank,
        reverse=True,
    )[:entity_limit]
    analysis["topics"] = sorted(
        analysis.get("topics") or [],
        key=_candidate_rank,
        reverse=True,
    )[:topic_limit]
    selected = {
        normalize_page_key(str(item.get("name", "")))
        for bucket in ("entities", "topics")
        for item in analysis.get(bucket) or []
    }
    analysis["relationships"] = [
        relation
        for relation in analysis.get("relationships") or []
        if normalize_page_key(str(relation.get("source", ""))) in selected
        and normalize_page_key(str(relation.get("target", ""))) in selected
    ]


def _pop_analysis_issues(analysis: dict[str, Any]) -> list[str]:
    """提取并移除分块失败标记，避免把失败误报成空分析成功。"""
    warnings = [
        str(value)
        for value in (analysis.pop("_analysis_warnings", None) or [])
        if str(value).strip()
    ]
    meta = analysis.pop("_analysis_meta", None)
    if meta is not None and meta.get("failed_chunks", 0) > 0:
        failed = int(meta["failed_chunks"])
        total = int(meta.get("total_chunks", 1))
        if failed >= total:
            return [
                *warnings,
                f"LLM 分析失败: {failed}/{total} 个分块全部失败；已解析内容仍保留，可稍后重试",
            ]
        return [
            *warnings,
            f"LLM 分析部分失败: {failed}/{total} 个分块未提取到内容；已解析内容仍保留",
        ]
    if analysis.pop("_chunk_failed", False):
        return [
            *warnings,
            "LLM 分析失败: 1/1 个分块全部失败；已解析内容仍保留，可稍后重试",
        ]
    return warnings


_CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _valid_confidence(value: object, default: Confidence = "medium") -> Confidence:
    text = str(value or "").strip().lower()
    if text in _CONFIDENCE_ORDER:
        return text  # type: ignore[return-value]
    return default


def _analysis_claims(
    item: dict[str, Any],
    source_id: str,
    *,
    fallback_statement: str = "",
) -> list[WikiClaim]:
    """把 LLM 候选项转换为带当前 source 证据的知识主张。"""
    raw_claims = item.get("claims") or []
    if not raw_claims and fallback_statement.strip():
        raw_claims = [{"statement": fallback_statement}]

    claims: list[WikiClaim] = []
    for raw in raw_claims:
        if isinstance(raw, str):
            raw = {"statement": raw}
        if not isinstance(raw, dict):
            continue
        statement = str(raw.get("statement", "")).strip()
        if not statement:
            continue
        locator = str(raw.get("locator", "")).strip()
        excerpt = str(raw.get("excerpt", "")).strip()[:500]
        contradictions = [
            str(value).strip()
            for value in (raw.get("contradictions") or [])
            if str(value).strip()
        ]
        claims.append(
            WikiClaim(
                statement=statement,
                evidence=[
                    WikiEvidence(
                        source_id=source_id,
                        locator=locator,
                        excerpt=excerpt,
                    )
                ],
                confidence=_valid_confidence(
                    raw.get("confidence"),
                    _valid_confidence(item.get("confidence")),
                ),
                contested=bool(raw.get("contested", item.get("contested", False))),
                contradictions=contradictions,
            )
        )
    return claims


def _merge_claims(existing: list[WikiClaim], incoming: list[WikiClaim]) -> list[WikiClaim]:
    """按规范化主张文本合并证据，保守保留置信度与争议状态。"""
    merged = [
        WikiClaim.from_dict(claim.to_dict())
        for claim in existing
        if claim.statement.strip()
    ]
    by_statement = {
        normalize_page_key(claim.statement): claim
        for claim in merged
        if normalize_page_key(claim.statement)
    }
    for claim in incoming:
        key = normalize_page_key(claim.statement)
        current = by_statement.get(key)
        if current is None:
            copied = WikiClaim.from_dict(claim.to_dict())
            merged.append(copied)
            if key:
                by_statement[key] = copied
            continue

        evidence_keys = {
            (item.source_id, item.locator, item.excerpt)
            for item in current.evidence
        }
        for evidence in claim.evidence:
            evidence_key = (evidence.source_id, evidence.locator, evidence.excerpt)
            if evidence_key not in evidence_keys:
                current.evidence.append(WikiEvidence.from_dict(evidence.to_dict()))
                evidence_keys.add(evidence_key)
        current.contested = current.contested or claim.contested
        current.contradictions = list(
            dict.fromkeys([*current.contradictions, *claim.contradictions])
        )
        if _CONFIDENCE_ORDER[claim.confidence] < _CONFIDENCE_ORDER[current.confidence]:
            current.confidence = claim.confidence
    return merged


def _apply_page_quality(
    page: WikiPage,
    claims: list[WikiClaim],
    *,
    confidence: Confidence | None = None,
    contested: bool = False,
    contradictions: list[str] | None = None,
) -> None:
    """合并主张并刷新页面级质量信号。"""
    page.claims = _merge_claims(page.claims, claims)
    claim_confidences = [claim.confidence for claim in page.claims]
    if confidence is not None:
        claim_confidences.append(confidence)
    if claim_confidences:
        page.confidence = min(
            claim_confidences,
            key=lambda value: _CONFIDENCE_ORDER[value],
        )
    page.contested = (
        page.contested
        or contested
        or any(claim.contested for claim in page.claims)
    )
    page.contradictions = list(
        dict.fromkeys(
            [
                *page.contradictions,
                *(contradictions or []),
                *[
                    value
                    for claim in page.claims
                    for value in claim.contradictions
                ],
            ]
        )
    )
    if page.contradictions:
        page.contested = True


def _planned_page_fingerprint_entry(page: PlannedPage) -> dict[str, Any]:
    """抽取参与计划指纹的稳定字段，剥离会随时间变化的元数据。"""
    return {
        "title": page.title,
        "page_type": page.page_type,
        "action": page.action,
        "is_new": page.is_new,
        "content_sha256": hashlib.sha256(page.content.encode("utf-8")).hexdigest(),
        "target_page_id": page.target_page_id,
        "target_content_sha256": page.target_content_sha256,
    }


def compute_plan_fingerprint(plan: PlanResult) -> str:
    """计算计划指纹：source_id + source 内容 hash + 规划页面与关系的规范化摘要。

    apply 前必须与磁盘 plan 一致，防止用户确认旧计划后系统又生成新计划、
    旧确认被应用到新计划上。source 内容 hash 相同也拦不住这种情况，
    因此指纹额外纳入规划页面与关系结构。
    """
    payload = {
        "source_id": plan.source_id,
        "source_content_sha256": plan.source_content_sha256,
        "planned_pages": [
            _planned_page_fingerprint_entry(page) for page in plan.planned_pages
        ],
        "relationships": sorted(
            sorted(str(item).split(":", 1)) for item in (
                f"{rel.get('source', '')}:{rel.get('target', '')}:{rel.get('relation', '')}"
                for rel in plan.relationships
                if isinstance(rel, dict)
            )
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()




_LLM_LINT_PROMPT = """你负责审核知识库质量。请阅读下面列出的 Wiki 页面摘要，检查以下两类问题：

1. **contradiction（矛盾）**：不同页面对同一概念/实体/事实给出相互冲突的陈述。
2. **entity_gap（实体缺口）**：多个页面反复提到某个重要知识对象，但该对象没有 Entity 页面（只列出页面标题和类型，不判断正文是否已有独立页面）。

页面列表：
---
{pages}
---

请输出一个 JSON 数组（不要包含 markdown 代码块标记）。每个元素格式如下：
{{
  "kind": "contradiction" | "entity_gap",
  "page_id": "相关页面 ID（矛盾时填其中一个，概念缺口填提出该概念的页面）",
  "message": "简短描述问题",
  "details": {{ "target": "概念/实体名称", "other_page_id": "矛盾的另一页面 ID（仅 contradiction）" }}
}}

如果没有发现问题，输出空数组 []。
只输出 JSON，不要任何解释文字。
"""

_ANALYSIS_PROMPT = """你负责整理知识库。请阅读下面的信息源片段，提取可增量合并的原子知识单元。

原始信息源：
---
{content}
---

请输出一个紧凑 JSON 对象（不要包含 markdown 代码块标记）：
{{
  "format": "knowledge-units-v7",
  "source_summary": {{
    "one_sentence": "这一片段的核心意思，最多80字",
    "core_points": ["核心观点1", "核心观点2"]
  }},
  "entities": [
    {{
      "subject": "规范、稳定的知识主题名称",
      "entity_kind": "person|organization|tool|product|project|system|concept|method|principle|mechanism|other",
      "importance": "core|supporting",
      "statement": "片段明确支持的一条独立知识主张",
      "summary": "subject 的简短解释，最多80字",
      "aliases": ["别名1"],
      "locator": "章节、页码、标题或段落线索，没有则为空",
      "excerpt": "直接支持 statement 的短原文，最多200字",
      "confidence": "high|medium|low",
      "contested": false,
      "contradictions": [],
      "relations": [
        {{"target": "另一个核心 subject", "relation": "uses|depends_on|part_of|contrasts_with|related"}}
      ]
    }}
  ],
  "topics": [
    {{
      "subject": "规范、稳定的知识主题名称",
      "importance": "core|supporting",
      "statement": "片段明确支持的一条独立知识主张",
      "summary": "subject 的简短解释，最多80字",
      "aliases": [],
      "locator": "章节、页码、标题或段落线索，没有则为空",
      "excerpt": "直接支持 statement 的短原文，最多200字",
      "confidence": "high|medium|low",
      "contested": false,
      "contradictions": [],
      "relations": []
    }}
  ]
}}

规则：
- 每个 unit 只表达一个主张；同一 subject 有多条主张时输出多个 unit。
- entities 包含人物、组织、工具、产品、项目、系统，也包含概念、方法、原则和机制；用 entity_kind 标明子类型。topics 只放需要跨多个知识点组织的事件、问题、工作主题或材料主线。
- 同一个规范 subject 只能出现在一个数组中，禁止跨类型重复。稳定知识对象归 entities，综合导航归 topics。
- core 表示该 subject 是片段的中心对象，或即使只出现一次也值得成为规范页面；supporting 表示它只是支撑性知识。路过式名称、作者列表、参考文献条目不要输出。
- source_summary 只概括当前片段；core_points 最多 2 条。
- 按重要性从高到低排列；每个片段 entities 最多 3 个 unit，topics 最多 2 个 unit。Compiler 会在整篇素材合并后再次硬限制为最多 5 个 Entity 和 3 个 Topic。
- 宁缺毋滥，禁止为了凑数拆出近义、重复或无独立复用价值的主张。
- 不要撰写完整 Wiki 页面，不要生成长篇摘要，不要重复原文背景。
- confidence：多处直接证据为 high；单处直接证据为 medium；间接、模糊或时效存疑为 low。
- 发现来源内部存在互相不兼容的说法时设置 contested=true，并在 contradictions 中简述冲突。
- locator/excerpt 用于把主张定位回原文；无法可靠定位时留空，禁止编造。
- relations 只记录本片段明确支持且两端都有长期复用价值的关系，最多 2 条。
- 使用紧凑 JSON；只输出 JSON，不要任何解释文字。
"""


def _analysis_chunk_key(content: str) -> str:
    payload = f"{_ANALYSIS_PROTOCOL_VERSION}\0{content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_analysis_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != _ANALYSIS_PROTOCOL_VERSION:
        return {}
    chunks = payload.get("chunks")
    if not isinstance(chunks, dict):
        return {}
    return {
        str(key): value
        for key, value in chunks.items()
        if isinstance(value, dict) and not value.get("_chunk_failed")
    }


def _save_analysis_cache(
    path: Path | None,
    chunks: dict[str, dict[str, Any]],
) -> None:
    """原子保存已成功的分块分析；失败不会破坏上一次可用缓存。"""
    if path is None:
        return
    payload = {
        "version": _ANALYSIS_PROTOCOL_VERSION,
        "chunks": chunks,
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError as exc:
        log.warning("保存 Wiki 分块分析缓存失败 %s: %s", path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


class WikiCompiler:
    """把 raw source / session 编译为 Wiki 页面。"""

    def __init__(
        self,
        store: WikiStore,
        provider: LLMProvider,
        summarizer: WikiSummarizer | None = None,
        provider_for_owner: Callable[[str], LLMProvider] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.summarizer = summarizer
        self.provider_for_owner = provider_for_owner
        self._analysis_owner: ContextVar[str] = ContextVar(
            f"wiki_analysis_owner_{id(self)}",
            default="",
        )

    def _provider_for_owner(self, owner_account_id: str = "") -> LLMProvider:
        resolver = self.provider_for_owner
        if callable(resolver):
            resolved = resolver(str(owner_account_id or ""))
            if resolved is not None:
                return resolved
        return self.provider

    def init_kb(self, owner_account_id: str = "", kb_id: str = "default") -> None:
        """初始化 Wiki 目录结构。"""
        self.store.init_kb(owner_account_id, kb_id)

    async def ingest(
        self,
        source_id: str,
        owner_account_id: str = "",
        source_content: str | None = None,
        kb_id: str = "default",
        progress: ProgressFn | None = None,
        cancel_event: asyncio.Event | None = None,
        *,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        skip_index: bool = False,
    ) -> IngestResult:
        """编译单个 source 为 Wiki 页面。"""

        self.init_kb(owner_account_id, kb_id)
        _check_cancelled(cancel_event)
        await _notify_progress(progress, "load", {"source_id": source_id})
        raw = self.store.load_raw(source_id, owner_account_id, kb_id)
        if raw is None and source_content is None:
            await _notify_progress(progress, "done", {"source_id": source_id, "error": f"source {source_id} 不存在"})
            return IngestResult(source_id=source_id, issues=[f"source {source_id} 不存在"])

        # 检查重复与漂移（一次遍历 raws，两个检查共享数据）
        issues: list[str] = []
        if raw is not None:
            all_raws = self.store.list_raws(owner_account_id, kb_id)
            dup = self.store.check_source_duplicate(raw, owner_account_id, kb_id, _raws=all_raws)
            if dup is not None:
                message = f"检测到重复 source: 与 {dup.id} ({dup.title}) 内容相同，已跳过知识编译"
                await _notify_progress(
                    progress,
                    "done",
                    {"source_id": source_id, "duplicate_of": dup.id, "skipped": True},
                )
                return IngestResult(source_id=source_id, issues=[message])
            drifted = self.store.check_source_drift(raw, owner_account_id, kb_id, _raws=all_raws)
            if drifted:
                issues.append(f"检测到内容漂移: 同 URL 历史版本 {', '.join(d.id for d in drifted)}")

        if source_content is None:
            try:
                parsed_path = raw.parsed_path  # type: ignore[union-attr]
                source_content = _read_parsed(parsed_path)
            except Exception as exc:  # noqa: BLE001
                await _notify_progress(progress, "done", {"source_id": source_id, "error": f"读取 source 失败: {exc}"})
                return IngestResult(source_id=source_id, issues=[f"读取 source 失败: {exc}"])

        _check_cancelled(cancel_event)
        smooth_stop = asyncio.Event()
        smooth_task: asyncio.Task | None = None

        async def _smooth_analyze_progress() -> None:
            """在 LLM 分析期间定期推送递增进度，让进度条持续移动。"""
            current = _PROGRESS_STAGES["analyze"][0]
            while not smooth_stop.is_set():
                try:
                    await asyncio.wait_for(smooth_stop.wait(), timeout=_ANALYZE_SMOOTH_INTERVAL)
                    break
                except asyncio.TimeoutError:
                    pass
                if smooth_stop.is_set():
                    break
                current = min(current + _ANALYZE_SMOOTH_STEP, _ANALYZE_SMOOTH_MAX)
                await _notify_progress(
                    progress,
                    "analyze",
                    {"source_id": source_id, "percent": current, "smoothing": True},
                )

        try:
            await _notify_progress(progress, "analyze", {"source_id": source_id})
            smooth_task = asyncio.create_task(_smooth_analyze_progress())
            analysis = await self._analyze(
                source_content,
                cancel_event,
                owner_account_id=owner_account_id,
                chunk_size=chunk_size,
                use_chunking=use_chunking,
            )
        except asyncio.CancelledError:
            await _notify_progress(progress, "done", {"source_id": source_id, "error": "已取消"})
            return IngestResult(source_id=source_id, issues=["已取消"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Wiki 分析失败 source=%s", source_id)
            await _notify_progress(progress, "done", {"source_id": source_id, "error": f"LLM 分析失败: {exc}"})
            return IngestResult(source_id=source_id, issues=[f"LLM 分析失败: {exc}"])
        finally:
            smooth_stop.set()
            if smooth_task is not None:
                try:
                    await asyncio.wait_for(smooth_task, timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass

        _check_cancelled(cancel_event)
        issues.extend(_pop_analysis_issues(analysis))

        pages: list[WikiPage] = []
        created_titles: set[str] = set()

        # entities
        _check_cancelled(cancel_event)
        for ent in analysis.get("entities", []):
            _check_cancelled(cancel_event)
            claims = _analysis_claims(
                ent,
                source_id,
                fallback_statement=str(ent.get("description", "")),
            )
            page = self._ensure_page(
                "entity",
                ent.get("name", ""),
                ent.get("description", ""),
                source_id,
                aliases=ent.get("aliases") or [],
                claims=claims,
                confidence=_valid_confidence(ent.get("confidence")),
                contested=bool(ent.get("contested", False)),
                contradictions=list(ent.get("contradictions") or []),
                owner_account_id=owner_account_id,
                kb_id=kb_id,
            )
            if page and page.title not in created_titles:
                pages.append(page)
                created_titles.add(page.title)

        # topics
        _check_cancelled(cancel_event)
        for top in analysis.get("topics", []):
            _check_cancelled(cancel_event)
            page = self._ensure_topic_page(top, source_id, owner_account_id, kb_id)
            if page and page.title not in created_titles:
                pages.append(page)
                created_titles.add(page.title)

        # source 摘要只引用本轮实际生成或更新成功的知识页面，避免展示无法跳转的链接。
        entity_pages = [page for page in pages if page.page_type == "entity"]
        topic_pages = [page for page in pages if page.page_type == "topic"]
        source_page = self._ensure_source_page(
            source_id,
            raw,
            source_content,
            owner_account_id,
            kb_id,
            source_summary=analysis.get("source_summary"),
            entities=[{"name": page.title} for page in entity_pages],
            topics=[{"name": page.title} for page in topic_pages],
        )
        pages.insert(0, source_page)
        created_titles.add(source_page.title)

        # 将分析结果编译为基于页面 ID 的有类型关系。
        _check_cancelled(cancel_event)
        relationships = analysis.get("relationships", [])
        self._apply_relationships(pages, relationships, owner_account_id, kb_id)

        # 更新 index.md
        _check_cancelled(cancel_event)
        if not skip_index:
            self._update_index(owner_account_id, kb_id)

        await _notify_progress(progress, "done", {"source_id": source_id, "page_count": len(pages)})

        # 页面变化后台刷新知识库摘要（内容 hash 未变时不触发 LLM）
        self._schedule_kb_summary_refresh(owner_account_id, kb_id)

        # 追加操作日志
        try:
            titles = [p.title for p in pages]
            self.store.append_log(
                [f"编译 source {source_id}，生成/更新 {len(pages)} 个页面: {', '.join(titles)}"],
                owner_account_id=owner_account_id,
                kb_id=kb_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("追加 Wiki 日志失败 source=%s", source_id)

        return IngestResult(source_id=source_id, pages=pages, issues=issues)

    async def plan_ingest(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
        cancel_event: asyncio.Event | None = None,
        *,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        skip_index: bool = False,
    ) -> PlanResult:
        """对 source 做只读分析，返回变更计划，不写入任何页面。"""
        self.init_kb(owner_account_id, kb_id)

        raw = self.store.load_raw(source_id, owner_account_id, kb_id)
        if raw is None:
            return PlanResult(source_id=source_id, issues=["source 不存在"])

        # 检查重复/漂移
        issues: list[str] = []
        dup = self.store.check_source_duplicate(raw, owner_account_id, kb_id)
        if dup:
            issues.append(f"检测到重复 source: {dup.id} ({dup.title})，已跳过知识编译")
            plan = PlanResult(
                source_id=source_id,
                source_title=raw.title,
                source_content_sha256=raw.content_sha256 or "",
                planned_pages=[],
                issues=issues,
            )
            plan.plan_fingerprint = compute_plan_fingerprint(plan)
            self._save_plan(plan, owner_account_id, kb_id)
            return plan
        drifted = self.store.check_source_drift(raw, owner_account_id, kb_id)
        if drifted:
            issues.append(f"发现 {len(drifted)} 个历史漂移版本")

        # 读取 source content
        source_content = _read_parsed(raw.parsed_path) if raw.parsed_path else ""
        if not source_content.strip():
            return PlanResult(
                source_id=source_id,
                source_title=raw.title,
                source_content_sha256=raw.content_sha256 or "",
                issues=issues + ["source content 为空"],
            )

        # LLM 分析
        if cancel_event and cancel_event.is_set():
            return PlanResult(
                source_id=source_id,
                source_title=raw.title,
                source_content_sha256=raw.content_sha256 or "",
                issues=issues + ["已取消"],
            )
        try:
            cache_path = (
                self.store._dir(owner_account_id, kb_id)
                / ".crew"
                / "cache"
                / f"{source_id}.analysis-cache.json"
            )
            analysis = await self._analyze(
                source_content,
                cancel_event,
                owner_account_id=owner_account_id,
                chunk_size=chunk_size,
                use_chunking=use_chunking,
                cache_path=cache_path,
            )
        except Exception as exc:  # noqa: BLE001
            return PlanResult(
                source_id=source_id,
                source_title=raw.title,
                source_content_sha256=raw.content_sha256 or "",
                issues=issues + [f"LLM 分析失败: {exc}"],
            )
        analysis_stats = {
            str(key): int(value)
            for key, value in (analysis.get("_analysis_meta") or {}).items()
            if isinstance(value, (int, float))
        }
        issues.extend(_pop_analysis_issues(analysis))

        # 干跑：计算计划而不写入
        planned: list[PlannedPage] = []

        # entities
        for e in analysis.get("entities", []):
            pp = self._plan_page(
                "entity",
                e,
                source_id,
                owner_account_id,
                kb_id,
            )
            if pp:
                planned.append(pp)

        # topics
        for t in analysis.get("topics", []):
            pp = self._plan_topic(t, source_id, owner_account_id, kb_id)
            if pp:
                planned.append(pp)

        # source page 只引用通过规划门槛、确实会被写入的知识页面。
        source_title = raw.title or source_id
        planned_entities = [
            {"name": page.title}
            for page in planned
            if page.page_type == "entity"
        ]
        planned_topics = [
            {"name": page.title}
            for page in planned
            if page.page_type == "topic"
        ]
        source_page_content = _build_source_page_content(
            source_title,
            raw,
            source_content,
            analysis.get("source_summary"),
            planned_entities,
            planned_topics,
        )
        existing_source = self.store.get_source_page(source_id, owner_account_id, kb_id)
        if existing_source is not None:
            source_plan = PlannedPage(
                title=source_title, page_type="source", action="update",
                content=source_page_content,
                is_new=False,
                target_page_id=existing_source.id,
                target_content_sha256=hashlib.sha256(
                    existing_source.content.encode("utf-8")
                ).hexdigest(),
            )
        else:
            source_plan = PlannedPage(
                title=source_title, page_type="source", action="create",
                content=source_page_content,
                is_new=True,
            )
        planned.insert(0, source_plan)

        total_new = sum(1 for p in planned if p.action == "create")
        total_update = sum(1 for p in planned if p.action == "update")
        total_contested = sum(1 for p in planned if p.action == "contest")

        plan = PlanResult(
            source_id=source_id,
            source_title=source_title,
            source_content_sha256=raw.content_sha256 or "",
            planned_pages=planned,
            relationships=analysis.get("relationships", []),
            issues=issues,
            total_new=total_new,
            total_update=total_update,
            total_contested=total_contested,
            analysis_stats=analysis_stats,
        )
        plan.plan_fingerprint = compute_plan_fingerprint(plan)
        self._save_plan(plan, owner_account_id, kb_id)
        return plan

    async def apply_ingest(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
        approved_titles: list[str] | None = None,
        progress: ProgressFn | None = None,
        cancel_event: asyncio.Event | None = None,
        *,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        skip_index: bool = False,
    ) -> IngestResult:
        """按已生成的 plan 执行写入，并校验计划对应的 source 内容版本。"""
        self.init_kb(owner_account_id, kb_id)

        plan = self._load_plan(source_id, owner_account_id, kb_id)
        if plan is None:
            return IngestResult(
                source_id=source_id,
                issues=["未找到 ingest 计划；请先重新调用 wiki_plan_ingest"],
            )

        if cancel_event and cancel_event.is_set():
            return IngestResult(source_id=source_id, issues=["已取消"])

        await _notify_progress(progress, "load", {"source_id": source_id})

        # 读取 source content 用于创建 source 页面
        raw = self.store.load_raw(source_id, owner_account_id, kb_id)
        current_hash = raw.content_sha256 if raw is not None else ""
        if not plan.source_content_sha256 or current_hash != plan.source_content_sha256:
            return IngestResult(
                source_id=source_id,
                issues=["ingest 计划已过期：source 内容版本已变化；请重新调用 wiki_plan_ingest"],
            )
        if raw is not None and raw.superseded_by:
            return IngestResult(
                source_id=source_id,
                issues=[
                    f"该来源已被新版本 {raw.superseded_by} 取代，不再应用其历史计划；"
                    "请对新版本重新调用 wiki_plan_ingest"
                ],
            )
        source_content = ""
        if raw is not None and raw.parsed_path:
            source_content = _read_parsed(raw.parsed_path)

        # 过滤 approved_titles；source 页面始终写入，不受 approved_titles 限制
        pages_to_apply = plan.planned_pages
        if approved_titles is not None:
            approved_set = {t.strip() for t in approved_titles if t.strip()}
            pages_to_apply = [
                p for p in pages_to_apply
                if p.page_type == "source" or p.title in approved_set
            ]

        applied_pages: list[WikiPage] = []
        linked_pages: list[WikiPage] = []
        skipped_titles: list[str] = []

        await _notify_progress(progress, "analyze", {"source_id": source_id})

        source_plan = next(
            (page for page in pages_to_apply if page.page_type == "source"),
            None,
        )
        for planned in pages_to_apply:
            _check_cancelled(cancel_event)
            if planned.page_type == "source":
                continue
            if planned.action == "skip":
                existing = self.store.resolve_page(
                    planned.title,
                    planned.page_type,
                    planned.aliases,
                    owner_account_id,
                    kb_id,
                )
                if existing is not None:
                    linked_pages.append(existing)
                continue
            else:
                page = self._apply_plan(planned, source_id, owner_account_id, kb_id)

            if page is not None:
                applied_pages.append(page)
                linked_pages.append(page)
            else:
                skipped_titles.append(planned.title)

        if source_plan is not None:
            entity_titles = [
                page.title for page in linked_pages if page.page_type == "entity"
            ]
            topic_titles = [
                page.title for page in linked_pages if page.page_type == "topic"
            ]
            source_content_filtered = _replace_source_page_links(
                source_plan.content,
                entity_titles,
                topic_titles,
            )
            source_page = self._ensure_source_page(
                source_id,
                raw,
                source_content,
                owner_account_id,
                kb_id,
                prepared_content=source_content_filtered,
            )
            applied_pages.insert(0, source_page)

        # 应用关系（只在实际写入的页面间）
        _check_cancelled(cancel_event)
        self._apply_relationships(applied_pages, plan.relationships, owner_account_id, kb_id)
        source_page = next((page for page in applied_pages if page.page_type == "source"), None)
        knowledge_pages = [page for page in linked_pages if page.page_type in {"entity", "topic"}]
        if source_page is not None:
            source_page.relations = [
                WikiRelation(
                    target_page_id=page.id,
                    relation="describes" if page.page_type == "entity" else "covers",
                )
                for page in knowledge_pages
            ]
            self.store.update(source_page, owner_account_id, kb_id)

        # 更新 index.md；批处理会在全部来源完成后统一更新一次。
        _check_cancelled(cancel_event)
        if not skip_index:
            self._update_index(owner_account_id, kb_id)

        await _notify_progress(progress, "done", {"source_id": source_id, "page_count": len(applied_pages)})

        # 页面变化后台刷新知识库摘要（内容 hash 未变时不触发 LLM）
        self._schedule_kb_summary_refresh(owner_account_id, kb_id)

        # 追加操作日志
        try:
            titles = [p.title for p in applied_pages]
            messages = [f"应用编译计划 source {source_id}，写入 {len(applied_pages)} 个页面: {', '.join(titles)}"]
            if skipped_titles:
                messages.append(f"跳过 {len(skipped_titles)} 个页面: {', '.join(skipped_titles)}")
            self.store.append_log(messages, owner_account_id=owner_account_id, kb_id=kb_id)
        except Exception:  # noqa: BLE001
            log.warning("追加 Wiki 日志失败 source=%s", source_id)

        return IngestResult(source_id=source_id, pages=applied_pages, issues=plan.issues)

    def _save_plan(
        self,
        plan: PlanResult,
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        """把 plan 保存到 .crew/plans，供 apply_ingest 使用。"""
        plan_dir = self.store._dir(owner_account_id, kb_id) / ".crew" / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / f"{plan.source_id}.json"
        try:
            plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("保存 plan 失败 source=%s: %s", plan.source_id, exc)

    def _load_plan(
        self,
        source_id: str,
        owner_account_id: str,
        kb_id: str,
    ) -> PlanResult | None:
        """从 .crew/plans 读取 plan，并兼容旧 raw/*.plan.json。"""
        plan_path = self.store._dir(owner_account_id, kb_id) / ".crew" / "plans" / f"{source_id}.json"
        if not plan_path.exists():
            plan_path = self.store._raw_dir(owner_account_id, kb_id) / f"{source_id}.plan.json"
        if not plan_path.exists():
            return None
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            return PlanResult.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 plan 失败 source=%s: %s", source_id, exc)
            return None

    def load_plan(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> PlanResult | None:
        """公开读取已保存的 ingest 计划，供工具层做指纹与版本校验。"""
        return self._load_plan(source_id, owner_account_id, kb_id)

    def _plan_page(
        self,
        page_type: PageType,
        item: dict[str, Any],
        source_id: str,
        owner_account_id: str,
        kb_id: str,
    ) -> PlannedPage | None:
        """计算单个页面的计划变更，不执行写入。"""
        title = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        aliases = list(item.get("aliases") or [])
        if not title.strip():
            return None
        claims = _analysis_claims(
            item,
            source_id,
            fallback_statement=description,
        )
        confidence = _valid_confidence(item.get("confidence"))
        contested = bool(item.get("contested", False)) or any(
            claim.contested for claim in claims
        )
        contradictions = list(
            dict.fromkeys(
                [
                    *[str(value) for value in (item.get("contradictions") or [])],
                    *[
                        value
                        for claim in claims
                        for value in claim.contradictions
                    ],
                ]
            )
        )
        existing = self.store.resolve_page(
            title,
            page_type,
            aliases,
            owner_account_id,
            kb_id,
        )
        if existing:
            target_content_sha256 = hashlib.sha256(
                existing.content.encode("utf-8")
            ).hexdigest()
            merged = _merge_content(existing.content, description)
            merged_claims = _merge_claims(existing.claims, claims)
            quality_changed = (
                [claim.to_dict() for claim in merged_claims]
                != [claim.to_dict() for claim in existing.claims]
                or source_id not in existing.sources
                or contested != existing.contested
                or any(value not in existing.contradictions for value in contradictions)
                or any(value not in existing.aliases for value in aliases)
            )
            if merged == existing.content and not quality_changed:
                return PlannedPage(
                    title=title, page_type=page_type, action="skip",
                    content=existing.content, is_new=False,
                    existing_title=existing.title,
                    aliases=list(dict.fromkeys([*existing.aliases, *aliases])),
                    reason="内容、证据与页面元数据均已存在",
                    claims=claims,
                    confidence=confidence,
                    contested=existing.contested,
                    contradictions=list(existing.contradictions),
                    target_page_id=existing.id,
                    target_content_sha256=target_content_sha256,
                )
            action = "contest" if contested or contradictions else "update"
            return PlannedPage(
                title=existing.title, page_type=page_type, action=action,
                content=merged, is_new=False,
                existing_title=existing.title,
                aliases=list(dict.fromkeys([*existing.aliases, *aliases, title])),
                reason=(
                    "新来源与已有结论存在争议，需要保留双方证据"
                    if action == "contest"
                    else "新来源为已有规范页面补充内容或证据"
                ),
                claims=claims,
                confidence=confidence,
                contested=contested,
                contradictions=contradictions,
                target_page_id=existing.id,
                target_content_sha256=target_content_sha256,
            )
        distinct_claims = {
            normalize_page_key(claim.statement)
            for claim in claims
            if normalize_page_key(claim.statement)
        }
        if item.get("importance") == "supporting" and len(distinct_claims) < 2:
            return None
        return PlannedPage(
            title=title, page_type=page_type, action="create",
            content=description, is_new=True,
            aliases=list(aliases or []),
            reason="未找到同标题、别名和页面类型的规范页面",
            claims=claims,
            confidence=confidence,
            contested=contested,
            contradictions=contradictions,
        )

    def _plan_topic(
        self,
        top: dict[str, Any],
        source_id: str,
        owner_account_id: str,
        kb_id: str,
    ) -> PlannedPage | None:
        """计算 topic 页面的计划变更。"""
        title = str(top.get("name", "")).strip()
        if not title:
            return None
        content = _build_topic_content(top)
        claims = _analysis_claims(
            top,
            source_id,
            fallback_statement=str(top.get("summary") or top.get("description") or ""),
        )
        confidence = _valid_confidence(top.get("confidence"))
        contested = bool(top.get("contested", False)) or any(
            claim.contested for claim in claims
        )
        contradictions = list(
            dict.fromkeys(
                [
                    *[str(value) for value in (top.get("contradictions") or [])],
                    *[
                        value
                        for claim in claims
                        for value in claim.contradictions
                    ],
                ]
            )
        )
        aliases = list(top.get("aliases") or [])
        existing = self.store.resolve_page(
            title,
            "topic",
            aliases,
            owner_account_id,
            kb_id,
        )
        if existing:
            target_content_sha256 = hashlib.sha256(
                existing.content.encode("utf-8")
            ).hexdigest()
            merged = _merge_content(existing.content, content)
            merged_claims = _merge_claims(existing.claims, claims)
            quality_changed = (
                [claim.to_dict() for claim in merged_claims]
                != [claim.to_dict() for claim in existing.claims]
                or source_id not in existing.sources
                or contested != existing.contested
                or any(value not in existing.contradictions for value in contradictions)
            )
            if merged == existing.content and not quality_changed:
                return PlannedPage(
                    title=title, page_type="topic", action="skip",
                    content=existing.content, is_new=False,
                    existing_title=existing.title,
                    reason="内容、证据与页面元数据均已存在",
                    claims=claims,
                    confidence=confidence,
                    contested=existing.contested,
                    contradictions=list(existing.contradictions),
                    target_page_id=existing.id,
                    target_content_sha256=target_content_sha256,
                )
            action = "contest" if contested or contradictions else "update"
            return PlannedPage(
                title=existing.title, page_type="topic", action=action,
                content=merged, is_new=False,
                existing_title=existing.title,
                aliases=list(dict.fromkeys([*existing.aliases, *aliases, title])),
                reason=(
                    "新来源与已有主题结论存在争议"
                    if action == "contest"
                    else "新来源补充已有主题的综合知识"
                ),
                claims=claims,
                confidence=confidence,
                contested=contested,
                contradictions=contradictions,
                target_page_id=existing.id,
                target_content_sha256=target_content_sha256,
            )
        distinct_claims = {
            normalize_page_key(claim.statement)
            for claim in claims
            if normalize_page_key(claim.statement)
        }
        if top.get("importance") == "supporting" and len(distinct_claims) < 2:
            return None
        return PlannedPage(
            title=title, page_type="topic", action="create",
            content=content, is_new=True,
            aliases=aliases,
            reason="未找到匹配的规范主题页面",
            claims=claims,
            confidence=confidence,
            contested=contested,
            contradictions=contradictions,
        )

    def _apply_plan(
        self,
        planned: PlannedPage,
        source_id: str,
        owner_account_id: str,
        kb_id: str,
    ) -> WikiPage | None:
        """根据 plan 创建或更新 entity/topic 页面。"""
        if planned.action == "skip":
            return None
        if planned.page_type == "source":
            return None  # source 页面在 apply_ingest 中单独处理

        existing = self.store.resolve_page(
            planned.title,
            planned.page_type,
            planned.aliases,
            owner_account_id,
            kb_id,
        )
        if planned.action in ("update", "contest") and existing is not None:
            # 目标页版本校验：计划生成后若目标页被外部修改（content hash 不符），
            # 停止应用该页并记 issue，要求重新规划，避免覆盖新内容。
            if planned.target_content_sha256:
                current_hash = hashlib.sha256(
                    existing.content.encode("utf-8")
                ).hexdigest()
                if current_hash != planned.target_content_sha256:
                    log.warning(
                        "Wiki apply 跳过页面 %s：目标页已被外部修改（计划指纹 %s != 当前 %s）",
                        planned.title,
                        planned.target_content_sha256,
                        current_hash,
                    )
                    return None
            existing.content = planned.content
            if source_id not in existing.sources:
                existing.sources.append(source_id)
            existing.aliases = list(
                dict.fromkeys([*existing.aliases, *planned.aliases])
            )
            _apply_page_quality(
                existing,
                planned.claims,
                confidence=planned.confidence,
                contested=planned.contested or planned.action == "contest",
                contradictions=planned.contradictions,
            )
            existing.updated_at = time.time()
            return self.store.update(existing, owner_account_id, kb_id) or existing

        # create：统一走 _ensure_page
        return self._ensure_page(
            planned.page_type,
            planned.title,
            planned.content,
            source_id,
            aliases=list(planned.aliases or []),
            claims=planned.claims,
            confidence=planned.confidence,
            contested=planned.contested,
            contradictions=planned.contradictions,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )

    def update_index(self, owner_account_id: str = "", kb_id: str = "default") -> None:
        """公开方法：重建 index.md。"""
        self._update_index(owner_account_id, kb_id)

    def publish_source_page(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage:
        """把已解析 RawSource 快速发布为全文 Source 页面，不执行 LLM 结构化分析。"""
        self.init_kb(owner_account_id, kb_id)
        raw = self.store.load_raw(source_id, owner_account_id, kb_id)
        if raw is None:
            raise ValueError(f"source 不存在: {source_id}")
        source_content = _read_parsed(raw.parsed_path) if raw.parsed_path else ""
        if not source_content.strip():
            raise ValueError(f"source content 为空: {source_id}")
        return self._ensure_source_page(
            source_id,
            raw,
            source_content,
            owner_account_id,
            kb_id,
        )

    def finalize_write(
        self,
        message: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """统一完成写入后的 index、log、全文索引与摘要状态维护。"""
        self._update_index(owner_account_id, kb_id)
        self.store.append_log([message], owner_account_id=owner_account_id, kb_id=kb_id)
        self.store.update_home(owner_account_id=owner_account_id, kb_id=kb_id)
        self._schedule_kb_summary_refresh(owner_account_id, kb_id)
        self._schedule_home_intro_refresh(owner_account_id, kb_id)

    def _schedule_home_intro_refresh(
        self,
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        """后台 fire-and-forget 刷新 Home 导读；无事件循环的环境直接跳过。

        导读只在内容 hash 变化时重新生成（见 generate_home_intro），
        生成成功后重建 Home.md，不阻塞当前写入流程。
        """
        if self.summarizer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._refresh_home_intro(owner_account_id, kb_id))

    async def _refresh_home_intro(
        self,
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        try:
            _intro, changed = await self.summarizer.generate_home_intro(
                owner_account_id,
                kb_id,
            )
            if changed:
                self.store.update_home(owner_account_id=owner_account_id, kb_id=kb_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Home 导读后台刷新失败 %s:%s: %s",
                owner_account_id,
                kb_id,
                exc,
            )

    def _schedule_kb_summary_refresh(
        self,
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        """后台 fire-and-forget 刷新知识库摘要；无事件循环的环境直接跳过。

        摘要只在内容 hash 变化时重新生成（见 generate_kb_summary），
        不阻塞当前写入流程。
        """
        if self.summarizer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._refresh_kb_summary(owner_account_id, kb_id))

    async def _refresh_kb_summary(
        self,
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        try:
            await self.summarizer.generate_kb_summary(
                owner_account_id,
                kb_id,
                force=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "知识库摘要后台刷新失败 %s:%s: %s",
                owner_account_id,
                kb_id,
                exc,
            )

    async def compile_all(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> CompileResult:
        """兼容入口：按五份一批重新编译所有已解析 RawSource。"""
        self.init_kb(owner_account_id, kb_id)
        source_ids = [
            raw.id
            for raw in self.store.list_raws(owner_account_id, kb_id)
            if raw.parse_status == "parsed" and raw.is_current
        ]
        ingested: list[str] = []
        errors: list[str] = []
        cursor = 0
        while cursor < len(source_ids):
            result = await self.batch_ingest(
                source_ids=source_ids,
                cursor=cursor,
                batch_size=5,
                apply=True,
                owner_account_id=owner_account_id,
                kb_id=kb_id,
            )
            ingested.extend(result["succeeded"])
            errors.extend(
                f"{item['source_id']}: {item['error']}"
                for item in result["failed"]
            )
            next_cursor = result.get("next_cursor")
            if next_cursor is None:
                break
            cursor = int(next_cursor)

        return CompileResult(ingested=ingested, errors=errors)

    async def batch_ingest(
        self,
        *,
        source_ids: list[str] | None = None,
        cursor: int = 0,
        batch_size: int = 5,
        apply: bool = True,
        use_existing_plans: bool = False,
        owner_account_id: str = "",
        kb_id: str = "default",
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """有界批处理 RawSource；一次最多五份并返回下一批游标。"""
        self.init_kb(owner_account_id, kb_id)
        size = max(1, min(int(batch_size), 5))
        start = max(0, int(cursor))
        if source_ids is None:
            candidates = [
                raw.id
                for raw in self.store.list_raws(owner_account_id, kb_id)
                if raw.parse_status == "parsed" and raw.is_current
            ]
        else:
            candidates = list(dict.fromkeys(
                str(source_id).strip()
                for source_id in source_ids
                if str(source_id).strip()
            ))
        selected = candidates[start : start + size]
        succeeded: list[str] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        plans: list[dict[str, Any]] = []
        page_ids: list[str] = []

        for source_id in selected:
            _check_cancelled(cancel_event)
            raw = self.store.load_raw(source_id, owner_account_id, kb_id)
            if raw is None:
                failed.append({"source_id": source_id, "error": "source 不存在"})
                continue
            if raw.parse_status != "parsed":
                skipped.append({
                    "source_id": source_id,
                    "reason": f"parse_status={raw.parse_status}",
                })
                continue
            try:
                if use_existing_plans:
                    plan = self._load_plan(source_id, owner_account_id, kb_id)
                    if plan is None:
                        failed.append({
                            "source_id": source_id,
                            "error": "批次确认对应的 ingest plan 不存在",
                        })
                        continue
                else:
                    plan = await self.plan_ingest(
                        source_id,
                        owner_account_id=owner_account_id,
                        kb_id=kb_id,
                        cancel_event=cancel_event,
                    )
                plans.append(plan.to_dict(brief=True))
                duplicate = not plan.planned_pages and any(
                    "重复 source" in issue for issue in plan.issues
                )
                if duplicate:
                    skipped.append({"source_id": source_id, "reason": "重复素材"})
                    continue
                analysis_failed = any(
                    issue.startswith("LLM 分析失败:")
                    or "source content 为空" in issue
                    for issue in plan.issues
                )
                if analysis_failed:
                    failed.append({
                        "source_id": source_id,
                        "error": "；".join(plan.issues),
                    })
                    continue
                if not apply:
                    succeeded.append(source_id)
                    continue
                applied = await self.apply_ingest(
                    source_id,
                    owner_account_id=owner_account_id,
                    kb_id=kb_id,
                    cancel_event=cancel_event,
                    skip_index=True,
                )
                page_ids.extend(page.id for page in applied.pages)
                succeeded.append(source_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failed.append({"source_id": source_id, "error": str(exc)})

        next_cursor = start + len(selected)
        if next_cursor >= len(candidates):
            next_cursor = None
        if apply and selected:
            self._update_index(owner_account_id, kb_id)
            self._schedule_kb_summary_refresh(owner_account_id, kb_id)
            self.store.append_log(
                [
                    "批量 ingest 完成："
                    f"成功 {len(succeeded)}，跳过 {len(skipped)}，失败 {len(failed)}"
                ],
                owner_account_id=owner_account_id,
                kb_id=kb_id,
            )
        return {
            "source_ids": selected,
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
            "plans": plans,
            "page_ids": list(dict.fromkeys(page_ids)),
            "cursor": start,
            "next_cursor": next_cursor,
            "remaining": max(0, len(candidates) - (start + len(selected))),
            "batch_size": size,
            "applied": apply,
        }

    async def lint(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        deep: bool = False,
    ) -> list[dict[str, Any]]:
        """Lint 入口。默认只做程序化检查；deep=True 时追加 LLM 语义检查。"""
        self.init_kb(owner_account_id, kb_id)
        issues = list(self.store.lint(owner_account_id, kb_id))

        if not deep:
            return [issue.__dict__ for issue in issues]

        # LLM 语义检查：矛盾 + 概念缺口
        pages = self.store.list_all(owner_account_id=owner_account_id, kb_id=kb_id, limit=30)
        if len(pages) >= 2:
            llm_issues = await self._llm_lint_pages(pages, owner_account_id=owner_account_id)
            issues.extend(llm_issues)

        return [issue.__dict__ for issue in issues]

    async def _llm_lint_pages(
        self,
        pages: list[WikiPage],
        *,
        owner_account_id: str = "",
    ) -> list[LintIssue]:
        """调用 LLM 检查页面间的矛盾和概念缺口。"""
        # 控制 token：每个页面只取前 1500 字符
        page_texts = []
        for p in pages:
            snippet = p.content[:1500]
            page_texts.append(
                f"ID: {p.id}\nType: {p.page_type}\nTitle: {p.title}\n---\n{snippet}\n"
            )
        prompt = _LLM_LINT_PROMPT.format(pages="\n".join(page_texts))
        messages = [Message.user(prompt)]
        try:
            text = (
                await chat_text(self._provider_for_owner(owner_account_id), messages)
            ).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM lint 调用失败: %s", exc)
            return []

        text = _strip_md_fences(text)

        try:
            raw_items = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("LLM lint 返回非法 JSON: %s", exc)
            return []

        if not isinstance(raw_items, list):
            return []

        result: list[LintIssue] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", ""))
            if kind not in ("contradiction", "entity_gap"):
                continue
            result.append(
                LintIssue(
                    kind=kind,  # type: ignore[arg-type]
                    page_id=str(item.get("page_id", "")),
                    message=str(item.get("message", "")),
                    details=dict(item.get("details") or {}),
                )
            )
        return result

    async def orient(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiOrientation:
        """返回当前 KB 的全景信息，供 Agent 在操作前 orientation。"""
        self.init_kb(owner_account_id, kb_id)
        return self.store.orient(owner_account_id, kb_id)

    async def digest(
        self,
        topic: str,
        *,
        mode: str = "auto",
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage:
        """跨至少两个独立来源生成可持久化的 comparison/synthesis 页面。"""
        topic = str(topic or "").strip()
        if not topic:
            raise ValueError("digest topic 不能为空")
        seeds = self.store.search(
            topic,
            top_k=20,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        source_ids = list(dict.fromkeys(
            source_id
            for page in seeds
            for source_id in page.sources
            if source_id
        ))
        if len(source_ids) < 2:
            raise ValueError("跨来源综合至少需要两个独立 RawSource")
        resolved_mode = str(mode or "auto").strip().lower()
        if resolved_mode == "auto":
            resolved_mode = "comparison" if re.search(r"对比|比较|\bvs\b|versus", topic, re.I) else "synthesis"
        if resolved_mode not in {"comparison", "synthesis"}:
            raise ValueError("mode 只能是 auto、comparison 或 synthesis")

        page_type: PageType = resolved_mode  # type: ignore[assignment]
        suffix = "对比" if resolved_mode == "comparison" else "深度综合"
        title = f"{topic}-{suffix}"
        context_parts: list[str] = []
        for page in seeds[:12]:
            context_parts.append(
                f"## [[{page.title}]]\n"
                f"来源: {', '.join(page.sources) or '-'}\n"
                f"{page.content[:3000]}"
            )
        prompt = (
            f"请基于以下知识库证据生成“{topic}”的{suffix}报告。\n"
            "只使用提供的证据；关键结论后用 [[页面名]] 标注依据；"
            "区分一致观点、差异、冲突和仍待解决的问题。返回完整 Markdown，"
            f"首行必须是“# {title}”。\n\n" + "\n\n".join(context_parts)
        )
        content = (
            await chat_text(
                self._provider_for_owner(owner_account_id),
                [Message.user(prompt)],
            )
        ).strip()
        if not content.startswith("# "):
            content = f"# {title}\n\n{content}"
        summary = _page_index_summary(content, max_len=180)
        existing = self.store.resolve_page(
            title,
            page_type,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        if existing is not None:
            existing.content = content
            existing.summary = summary
            existing.sources = source_ids
            existing.related = []
            existing.relations = [
                WikiRelation(target_page_id=seed.id, relation="references")
                for seed in seeds[:12]
            ]
            page = self.store.update(existing, owner_account_id, kb_id) or existing
        else:
            page = self.store.save_page(
                WikiPage(
                    id=page_id(page_type, title),
                    page_type=page_type,
                    title=title,
                    content=content,
                    file_path="",
                    sources=source_ids,
                    relations=[
                        WikiRelation(target_page_id=seed.id, relation="references")
                        for seed in seeds[:12]
                    ],
                    tags=[suffix],
                    summary=summary,
                ),
                owner_account_id,
                kb_id,
            )
        self.finalize_write(
            f"生成 {suffix}页面 [[{title}]]，综合 {len(source_ids)} 个来源",
            owner_account_id,
            kb_id,
        )
        return page

    # ---- internal ----

    async def _analyze(
        self,
        content: str,
        cancel_event: asyncio.Event | None = None,
        *,
        owner_account_id: str = "",
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        cache_path: Path | None = None,
    ) -> dict[str, Any]:
        """提取轻量知识单元，并确定性聚合为页面规划输入。

        Args:
            chunk_size: 分块大小（字符），None 使用模块默认。
            use_chunking: 是否对长文档启用分块分析，None 按长度自动判断。
            cache_path: 可选的 source 级分块缓存文件。每个成功块立即持久化，
                失败或取消后重试只处理未完成块。
        """
        started_at = time.monotonic()
        effective_chunk_size = max(
            1_000,
            chunk_size if chunk_size is not None else _CHUNK_SIZE_CHARS,
        )
        should_chunk = (
            use_chunking
            if use_chunking is not None
            else len(content) > _SINGLE_PASS_THRESHOLD
        )
        chunks = (
            _split_into_semantic_chunks(content, max_size=effective_chunk_size)
            if should_chunk
            else [content]
        )
        if len(chunks) > 1:
            log.info(
                "Wiki 轻量分块分析：%d 字符，chunk_size=%d，切分为 %d 个语义块",
                len(content),
                effective_chunk_size,
                len(chunks),
            )

        semaphore = asyncio.Semaphore(_ANALYZE_CHUNK_CONCURRENCY)
        cached_chunks = _load_analysis_cache(cache_path)
        current_keys = [_analysis_chunk_key(chunk) for chunk in chunks]
        # 丢弃同 source 旧版本不再使用的块，避免缓存文件无限增长。
        cache_state = {
            key: cached_chunks[key]
            for key in current_keys
            if key in cached_chunks
        }
        results_by_index: dict[int, dict[str, Any]] = {
            index: cache_state[key]
            for index, key in enumerate(current_keys)
            if key in cache_state
        }

        async def _analyze_one(
            idx: int,
            chunk: str,
        ) -> tuple[int, dict[str, Any]]:
            async with semaphore:
                _check_cancelled(cancel_event)
                result = await self._analyze_chunk(
                    chunk,
                    chunk_index=idx,
                    total_chunks=len(chunks),
                )
                _check_cancelled(cancel_event)
                return idx, result

        owner_token = self._analysis_owner.set(str(owner_account_id or ""))
        try:
            # asyncio.Task 在创建时复制 Context，因而并发 owner 不会互相串用 Provider。
            tasks = [
                asyncio.create_task(_analyze_one(index, chunk))
                for index, chunk in enumerate(chunks)
                if index not in results_by_index
            ]
        finally:
            self._analysis_owner.reset(owner_token)
        for coro in asyncio.as_completed(tasks):
            try:
                index, result = await coro
                results_by_index[index] = result
                if not result.get("_chunk_failed"):
                    cache_state[current_keys[index]] = result
                    _save_analysis_cache(cache_path, cache_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("Wiki 分块分析异常，跳过该块: %s", exc)

        results = [
            results_by_index.get(
                index,
                {
                    "_chunk_failed": True,
                    "format": _ANALYSIS_PROTOCOL_VERSION,
                    "entities": [],
                    "topics": [],
                    "source_summary": {},
                },
            )
            for index in range(len(chunks))
        ]
        failed = sum(1 for r in results if r.get("_chunk_failed"))
        truncated = sum(1 for r in results if r.get("_truncated"))
        merged = _merge_analysis_results(results)
        _apply_document_limits(merged, len(content))
        merged["_analysis_meta"] = {
            "total_chunks": len(chunks),
            "analyzed_chunks": len(tasks),
            "cache_hits": len(chunks) - len(tasks),
            "failed_chunks": failed,
            "truncated_chunks": truncated,
            "elapsed_ms": int((time.monotonic() - started_at) * 1_000),
        }
        return merged

    async def _analyze_chunk(
        self,
        content: str,
        *,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> dict[str, Any]:
        """对单个文本块做 LLM 提取分析；失败时重试，仍失败则返回空结果。"""
        prompt = _ANALYSIS_PROMPT.format(content=content)
        messages = [Message.user(prompt)]
        warnings: list[str] = []
        for attempt in range(_ANALYZE_CHUNK_MAX_RETRIES + 1):
            try:
                # 推理型模型的"思考" token 与正文共享预算：文档越大推理消耗越多，
                # 首轮预算可能全被推理烧掉导致正文为空；重试时预算翻倍兜底。
                text = await chat_text(
                    self._provider_for_owner(self._analysis_owner.get()),
                    messages,
                    max_tokens=_ANALYSIS_MAX_TOKENS * (attempt + 1),
                )
                if not text:
                    raise ValueError("LLM 返回为空")
                parsed = _load_analysis_json(text)
                if parsed is None:
                    raise ValueError(f"LLM 返回 JSON 解析失败: {text[:120]!r}")
                if warnings:
                    parsed["_analysis_warnings"] = list(warnings)
                return parsed
            except Exception as exc:  # noqa: BLE001
                if _is_capacity_error(exc):
                    log.warning(
                        "Wiki chunk %d/%d 遇到模型限流或容量不足，停止同端点重试: %s",
                        chunk_index + 1,
                        total_chunks,
                        exc,
                    )
                    break
                if attempt < _ANALYZE_CHUNK_MAX_RETRIES:
                    warnings.append(
                        f"LLM 分析失败后重试: chunk {chunk_index + 1}/{total_chunks}，{exc}"
                    )
                    log.warning(
                        "Wiki chunk %d/%d 分析失败，第 %d 次重试: %s",
                        chunk_index + 1,
                        total_chunks,
                        attempt + 1,
                        exc,
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    log.warning(
                        "Wiki chunk %d/%d 分析最终失败，跳过该块: %s",
                        chunk_index + 1,
                        total_chunks,
                        exc,
                    )
        return {
            "_chunk_failed": True,
            "_analysis_warnings": warnings,
            "format": _ANALYSIS_PROTOCOL_VERSION,
            "entities": [],
            "topics": [],
            "source_summary": {},
        }

    def _ensure_source_page(
        self,
        source_id: str,
        raw: RawSource | None,
        source_content: str,
        owner_account_id: str,
        kb_id: str,
        *,
        source_summary: dict[str, Any] | None = None,
        entities: list[dict[str, Any]] | None = None,
        topics: list[dict[str, Any]] | None = None,
        prepared_content: str | None = None,
    ) -> WikiPage:
        """生成面向 Obsidian 阅读的来源摘要页，并附完整解析内容。"""
        title = raw.title if raw else source_id
        page_content = prepared_content or _build_source_page_content(
            title,
            raw,
            source_content,
            source_summary,
            entities or [],
            topics or [],
        )
        summary = (
            str((source_summary or {}).get("one_sentence") or "").strip()
            or _page_index_summary(page_content, max_len=180)
        )
        source_dir = SOURCE_DIRS.get(
            str(raw.source_kind if raw else "asset"),
            "assets",
        )
        base = self.store._dir(owner_account_id, kb_id)
        target_dir = base / "wiki" / "sources" / source_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # Source Page 唯一身份基于 source_id，不再按标题查重：两份同名但内容
        # 不同的来源各自拥有独立 Source Page，互不覆盖。标题仅用于展示。
        existing = self.store.get_source_page(source_id, owner_account_id, kb_id)
        if existing is not None:
            current_path = Path(existing.file_path)
            expected_parent = Path("wiki") / "sources" / source_dir
            if current_path.parent != expected_parent:
                target_path = unique_file_path(
                    target_dir,
                    filename_from_title(existing.title),
                )
                existing.file_path = str(target_path.relative_to(base))
            existing.content = page_content
            existing.summary = summary
            existing.related = []
            existing.updated_at = time.time()
            # 自愈 legacy 合并页：旧版按标题合并多来源，现以 source_id 为身份，
            # 收敛为单源，避免误删其他来源时连带删除本页。
            if existing.sources != [source_id]:
                existing.sources = [source_id]
            return self.store.update(existing, owner_account_id, kb_id) or existing

        page = WikiPage(
            id=source_page_id(source_id),
            page_type="source",
            title=title,
            content=page_content,
            file_path=str(
                unique_file_path(target_dir, filename_from_title(title)).relative_to(base)
            ),
            sources=[source_id],
            tags=[],
            summary=summary,
            created_at=time.time(),
            updated_at=time.time(),
        )
        return self.store.save_page(page, owner_account_id, kb_id)

    def _ensure_page(
        self,
        page_type: PageType,
        title: str,
        description: str,
        source_id: str,
        aliases: list[str] | None = None,
        claims: list[WikiClaim] | None = None,
        confidence: Confidence | None = None,
        contested: bool = False,
        contradictions: list[str] | None = None,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        if not title.strip():
            return None
        existing = self.store.resolve_page(
            title,
            page_type,
            aliases,
            owner_account_id,
            kb_id,
        )
        if existing:
            existing.content = _merge_content(existing.content, description)
            if source_id not in existing.sources:
                existing.sources.append(source_id)
            existing.aliases = list(
                dict.fromkeys([*existing.aliases, *(aliases or []), title])
            )
            _apply_page_quality(
                existing,
                claims or [],
                confidence=confidence,
                contested=contested,
                contradictions=contradictions,
            )
            existing.updated_at = time.time()
            return self.store.update(existing, owner_account_id, kb_id) or existing

        page = WikiPage(
            id=page_id(page_type, title),
            page_type=page_type,
            title=title,
            content=description,
            file_path="",
            sources=[source_id],
            tags=[],
            created_at=time.time(),
            updated_at=time.time(),
            aliases=list(aliases or []),
        )
        _apply_page_quality(
            page,
            claims or [],
            confidence=confidence,
            contested=contested,
            contradictions=contradictions,
        )
        return self.store.save_page(page, owner_account_id, kb_id)

    def _ensure_topic_page(
        self,
        top: dict[str, Any],
        source_id: str,
        owner_account_id: str,
        kb_id: str,
    ) -> WikiPage | None:
        title = str(top.get("name", "")).strip()
        if not title:
            return None
        aliases = list(top.get("aliases") or [])
        claims = _analysis_claims(
            top,
            source_id,
            fallback_statement=str(top.get("summary") or top.get("description") or ""),
        )
        confidence = _valid_confidence(top.get("confidence"))
        contested = bool(top.get("contested", False))
        contradictions = [
            str(value)
            for value in (top.get("contradictions") or [])
            if str(value).strip()
        ]
        existing = self.store.resolve_page(
            title,
            "topic",
            aliases,
            owner_account_id,
            kb_id,
        )
        content = _build_topic_content(top)
        if existing:
            existing.content = _merge_content(existing.content, content)
            if source_id not in existing.sources:
                existing.sources.append(source_id)
            existing.aliases = list(
                dict.fromkeys([*existing.aliases, *aliases, title])
            )
            _apply_page_quality(
                existing,
                claims,
                confidence=confidence,
                contested=contested,
                contradictions=contradictions,
            )
            existing.updated_at = time.time()
            return self.store.update(existing, owner_account_id, kb_id) or existing

        page = WikiPage(
            id=page_id("topic", title),
            page_type="topic",
            title=title,
            content=content,
            file_path="",
            sources=[source_id],
            tags=[],
            created_at=time.time(),
            updated_at=time.time(),
            aliases=aliases,
        )
        _apply_page_quality(
            page,
            claims,
            confidence=confidence,
            contested=contested,
            contradictions=contradictions,
        )
        return self.store.save_page(page, owner_account_id, kb_id)

    def _apply_relationships(
        self,
        pages: list[WikiPage],
        relationships: list[dict[str, Any]],
        owner_account_id: str,
        kb_id: str,
    ) -> None:
        title_to_page = {p.title: p for p in pages}
        all_pages = {p.title: p for p in self.store.list_all(owner_account_id=owner_account_id, kb_id=kb_id)}
        all_pages.update(title_to_page)
        pages_by_key: dict[str, list[WikiPage]] = {}
        for page in all_pages.values():
            for value in [page.title, *page.aliases]:
                key = normalize_page_key(value)
                if key:
                    pages_by_key.setdefault(key, []).append(page)

        def _resolve(value: str) -> WikiPage | None:
            exact = all_pages.get(value)
            if exact is not None:
                return exact
            candidates = {
                page.id: page
                for page in pages_by_key.get(normalize_page_key(value), [])
            }
            if len(candidates) == 1:
                return next(iter(candidates.values()))
            return None

        touched: dict[str, WikiPage] = {page.id: page for page in pages if page.id}
        for rel in relationships:
            src_title = str(rel.get("source", "")).strip()
            tgt_title = str(rel.get("target", "")).strip()
            if not src_title or not tgt_title or src_title == tgt_title:
                continue
            src = _resolve(src_title)
            if src is None:
                continue
            target = _resolve(tgt_title)
            if target is None:
                continue
            relation_type = str(rel.get("relation", "related")).strip() or "related"
            relation_key = (target.id, relation_type.casefold())
            existing_relation_keys = {
                (item.target_page_id, item.relation.casefold())
                for item in src.relations
            }
            if relation_key not in existing_relation_keys:
                src.relations.append(
                    WikiRelation(
                        target_page_id=target.id,
                        relation=relation_type,
                    )
                )
                touched[src.id] = src

        # 写回
        for page in touched.values():
            self.store.update(page, owner_account_id, kb_id)

    def _update_index(self, owner_account_id: str, kb_id: str) -> None:
        base = self.store._dir(owner_account_id, kb_id)
        index_path = base / "index.md"
        pages = self.store.list_all(limit=10000, owner_account_id=owner_account_id, kb_id=kb_id)
        lines = [
            "# 知识导航",
            "",
            f"页面总数: {len(pages)}",
            f"更新时间: {time.strftime('%Y-%m-%d %H:%M')}",
            "",
            "> 本页是导航与 Agent 定向入口，不承载完整正文。",
            "",
        ]
        sections = (
            ("entity", "关键词"),
            ("topic", "话题"),
            ("source", "来源摘要"),
            ("comparison", "对比分析"),
            ("synthesis", "综合报告"),
        )
        for page_type, label in sections:
            typed_pages = sorted(
                (page for page in pages if page.page_type == page_type),
                key=lambda page: normalize_page_key(page.title),
            )
            lines.extend([f"## {label}", ""])
            if not typed_pages:
                lines.extend(["- _暂无页面_", ""])
                continue
            for page in typed_pages:
                summary = page.summary or _page_index_summary(page.content)
                quality: list[str] = [
                    f"来源 {len(page.sources)}",
                    f"关系 {len(page.relations)}",
                    f"更新 {time.strftime('%Y-%m-%d', time.localtime(page.updated_at)) if page.updated_at else '-'}",
                ]
                lines.append(
                    f"- [[{page.title}]] — {summary} "
                    f"（{'；'.join(quality)}）"
                )
            lines.append("")
        index_path.write_text("\n".join(lines), encoding="utf-8")
        self.store.update_home(owner_account_id=owner_account_id, kb_id=kb_id)


def _read_parsed(parsed_path: str) -> str:
    p = Path(parsed_path)
    if p.is_absolute() and p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return ""


async def _notify_progress(
    progress: ProgressFn | None,
    stage: str,
    detail: dict[str, Any],
) -> None:
    """通知 ingest 进度；无回调或未知 stage 时静默跳过。

    detail 中可传入 ``percent`` 覆盖映射表中的默认值，用于 analyze 等
    耗时阶段的内部平滑推进。
    """
    if progress is None:
        return
    base_percent, label = _PROGRESS_STAGES.get(stage, (0, stage))
    percent = int(detail.get("percent", base_percent))
    log.info("Wiki ingest progress stage=%s percent=%s source=%s", stage, percent, detail.get("source_id"))
    try:
        await progress(stage, percent, {"label": label, **detail})
    except Exception:  # noqa: BLE001
        log.exception("Wiki ingest progress 回调失败 stage=%s", stage)


def _build_topic_content(top: dict[str, Any]) -> str:
    lines = [f"# {top.get('name', '')}", ""]
    if top.get("description"):
        lines.append(str(top["description"]))
        lines.append("")
    if top.get("summary"):
        lines.append("## 摘要")
        lines.append(str(top["summary"]))
        lines.append("")
    if top.get("decisions"):
        lines.append("## 关键决策")
        for d in top["decisions"]:
            lines.append(f"- {d}")
        lines.append("")
    if top.get("pitfalls"):
        lines.append("## 踩坑记录")
        for p in top["pitfalls"]:
            lines.append(f"- {p}")
        lines.append("")
    return "\n".join(lines)


def _page_index_summary(content: str, max_len: int = 120) -> str:
    """从页面正文提取适合 index.md 的单行摘要。"""
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        plain = re.sub(r"\[\[([^\]]+)\]\]", r"\1", stripped)
        plain = re.sub(r"\s+", " ", plain).strip(" >-*")
        if plain:
            return plain if len(plain) <= max_len else f"{plain[: max_len - 3].rstrip()}..."
    return "暂无摘要"


def _replace_source_page_links(
    content: str,
    entity_titles: list[str],
    topic_titles: list[str],
) -> str:
    """按实际写入的页面重建来源摘要中的三个关联区块。"""
    sections = {
        "关键词": ([f"- [[{title}]]" for title in entity_titles] or ["- _暂无独立关键词_"]),
        "相关话题": ([f"- [[{title}]]" for title in topic_titles] or ["- _暂无独立话题_"]),
        "相关页面": (
            [
                f"- [[{title}]]"
                for title in dict.fromkeys(entity_titles + topic_titles)
            ]
            or ["- _暂无关联页面_"]
        ),
    }
    updated = content
    for heading, lines in sections.items():
        replacement = f"## {heading}\n\n" + "\n".join(lines) + "\n\n"
        updated = re.sub(
            rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |^---$)",
            replacement,
            updated,
            count=1,
        )
    return updated


def _build_source_page_content(
    title: str,
    raw: RawSource | None,
    source_content: str,
    source_summary: dict[str, Any] | None = None,
    entities: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
) -> str:
    summary_data = source_summary if isinstance(source_summary, dict) else {}
    summary = (
        str(summary_data.get("one_sentence") or "").strip()
        or _page_index_summary(source_content, max_len=180)
    )
    core_points = [
        str(point).strip()
        for point in (summary_data.get("core_points") or [])
        if str(point).strip()
    ][:5]
    entity_titles = [
        str(item.get("name") or "").strip()
        for item in (entities or [])
        if str(item.get("name") or "").strip()
    ]
    topic_titles = [
        str(item.get("name") or "").strip()
        for item in (topics or [])
        if str(item.get("name") or "").strip()
    ]
    source_kind = raw.source_kind if raw else "note"
    platform = raw.source_platform if raw and raw.source_platform else "-"
    original_ref: str | None = None
    if raw is not None:
        if raw.source_url:
            original_ref = raw.source_url
        elif raw.original_path:
            original_path = Path(raw.original_path)
            parts = original_path.parts
            if "raw" in parts:
                raw_index = parts.index("raw")
                relative = Path(*parts[raw_index:]).as_posix()
                # Source Summary 位于 wiki/sources/{source_kind}/，回到 Vault 根目录
                # 需要三级相对路径。
                original_ref = f"[打开原始文件](../../../{relative})"
            else:
                original_ref = raw.original_ref or original_path.name
        else:
            original_ref = raw.original_ref
    lines = [
        f"# {title}",
        "",
        f"> {summary}",
        "",
        "## 来源信息",
        "",
        f"- Source ID：`{raw.id if raw else title}`",
        f"- 素材类型：`{source_kind}`",
        f"- 来源平台：`{platform}`",
        f"- 原始位置：{original_ref or '-'}",
        "",
        "## 核心观点",
        "",
    ]
    if core_points:
        lines.extend(
            f"{index}. {point}"
            for index, point in enumerate(core_points, start=1)
        )
    else:
        lines.append(f"1. {summary}")
    lines.extend([
        "",
        "## 关键词",
        "",
    ])
    lines.extend(
        [f"- [[{name}]]" for name in entity_titles]
        or ["- _暂无独立关键词_"]
    )
    lines.extend([
        "",
        "## 相关话题",
        "",
    ])
    lines.extend(
        [f"- [[{name}]]" for name in topic_titles]
        or ["- _暂无独立话题_"]
    )

    # 相关页面：综合关键词和话题，优先展示
    all_related = list(dict.fromkeys(entity_titles + topic_titles))
    lines.extend([
        "",
        "## 相关页面",
        "",
    ])
    lines.extend(
        [f"- [[{name}]]" for name in all_related]
        or ["- _暂无关联页面_"]
    )

    # 分隔线：明确区分 wiki 整理部分与原始内容
    lines.extend([
        "",
        "---",
        "",
        "## 原始内容",
        "",
        source_content.strip(),
        "",
    ])
    return "\n".join(lines)


def _merge_content(existing: str, new: str) -> str:
    """合并内容：保留原有内容，追加新内容中的非重复段落。"""
    if not existing.strip():
        return new
    if not new.strip():
        return existing

    existing_norm = existing.strip()
    new_norm = new.strip()
    if new_norm in existing_norm:
        return existing

    # 段落级去重：把新内容拆成段落，跳过已存在的段落
    def _paragraphs(text: str) -> list[str]:
        return [p.strip() for p in text.split("\n\n") if p.strip()]

    existing_paragraphs = _paragraphs(existing)
    existing_set = {p.strip() for p in existing_paragraphs}
    new_paragraphs = [p for p in _paragraphs(new) if p.strip() not in existing_set]

    if not new_paragraphs:
        return existing

    return f"{existing}\n\n---\n\n" + "\n\n".join(new_paragraphs)
