"""Wiki 查询器：检索 Wiki 页面并格式化上下文供 Agent 使用。

注意：不单独调用 LLM，答案合成由 Crew 主 agent 在对话中完成。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from crew.state.logging import get_logger

from ._utils import normalize_page_key, query_terms
from .rerank import RerankingProvider, validate_scores
from .schemas import WikiPage
from .store import WikiStore
from .vector import page_embedding_text

log = get_logger("wiki.query")


class WikiQuerier:
    """从 Wiki 检索相关页面，供主 agent 合成答案。"""

    def __init__(
        self, store: WikiStore, *, fuse_weight: float = 0.9,
        candidate_limit: int = 50, index_weight: float = 0.0,
        reranker: RerankingProvider | None = None,
        reranker_for_owner: Callable[[str], RerankingProvider | None] | None = None,
        rerank_limit: int = 50,
    ) -> None:
        if candidate_limit <= 0 or rerank_limit <= 0:
            raise ValueError("Candidate and rerank limits must be positive")
        if any(not math.isfinite(weight) or weight < 0 for weight in (index_weight, fuse_weight)):
            raise ValueError("Channel weights must be finite and nonnegative")
        self.store = store
        self._fuse_weight = float(fuse_weight)
        self._candidate_limit = int(candidate_limit)
        self._index_weight = float(index_weight)
        self._reranker = reranker
        self._reranker_for_owner = reranker_for_owner
        self._rerank_limit = int(rerank_limit)

    def query(
        self,
        question: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
    ) -> dict[str, Any]:
        """兼容旧 REST 查询接口；Wiki Agent 统一使用 ``search``。"""
        result = self.search(
            question,
            owner_account_id=owner_account_id,
            top_k=top_k,
            kb_id=kb_id,
            expand_neighbors=True,
            include_context=True,
        )
        return {"answer": "", **result}

    def search(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
        *,
        expand_neighbors: bool = True,
        include_context: bool = True,
    ) -> dict[str, Any]:
        """统一检索入口：融合召回，并按需扩展邻居与生成回答上下文。"""
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        seeds, retrieval = self._retrieve_seed_pages(
            query,
            owner_account_id=owner_account_id,
            top_k=top_k,
            kb_id=kb_id,
        )
        if not seeds:
            return {
                **(
                    {"context": "Wiki 中暂未找到与该问题相关的页面。"}
                    if include_context
                    else {}
                ),
                "pages": [],
                "retrieval": {
                    **retrieval,
                    "expanded_page_ids": [],
                },
            }

        candidates = list(seeds)
        seen = {page.id for page in candidates}
        expanded_ids: list[str] = []
        if expand_neighbors:
            for seed in seeds[: min(3, len(seeds))]:
                for neighbor in self.store.get_neighbors(
                    seed.id,
                    owner_account_id=owner_account_id,
                    kb_id=kb_id,
                )[:3]:
                    if neighbor.id in seen:
                        continue
                    candidates.append(neighbor)
                    seen.add(neighbor.id)
                    expanded_ids.append(neighbor.id)

        seed_rank = {page.id: index for index, page in enumerate(seeds)}
        terms = query_terms(query)
        query_key = normalize_page_key(query)

        def _rank(page: WikiPage) -> tuple[int, float, float]:
            score = (
                100.0 - seed_rank[page.id] * 5
                if page.id in seed_rank
                else 45.0
            )
            # Direct candidates already carry lexical/vector relevance through
            # RRF. Only newly expanded neighbors need a query relevance signal.
            if page.id not in seed_rank:
                claim_text = " ".join(claim.statement for claim in page.claims)
                searchable = (
                    f"{page.title} {' '.join(page.aliases)} "
                    f"{' '.join(page.tags)} {page.content} {claim_text}"
                ).casefold()
                score += sum(6 for term in terms if term and term in searchable)
            if page.confidence == "high":
                score += 3
            elif page.confidence == "low":
                score -= 2
            if page.contested:
                score -= 4
            exact = 0
            if query_key and query_key == normalize_page_key(page.title):
                exact = 2
            elif query_key and any(query_key == normalize_page_key(a) for a in page.aliases):
                exact = 1
            return exact, score, page.updated_at

        ordered = sorted(candidates, key=_rank, reverse=True)
        rerank_trace: dict[str, Any] = {"status": "disabled"}
        try:
            reranker = (
                self._reranker_for_owner(owner_account_id)
                if self._reranker_for_owner is not None else self._reranker
            )
            if reranker is not None:
                pool = ordered[:max(self._rerank_limit, top_k)]
                rerank_trace = {
                    "status": "failed", "model": reranker.model,
                    "candidate_page_ids": [page.id for page in pool],
                }
                scores = validate_scores(
                    reranker.score(query, [page_embedding_text(page) for page in pool]), len(pool),
                )
                # Preserve exact title/alias navigation and use the original
                # fused order to break model-score ties deterministically.
                ranked = sorted(enumerate(pool), key=lambda item: (
                    -_rank(item[1])[0], -scores[item[0]], item[0],
                ))
                ordered = [page for _, page in ranked]
                rerank_trace.update(status="ok", scores=scores)
        except Exception as exc:  # noqa: BLE001 - optional model failure must preserve retrieval
            rerank_trace.update(status="failed", error_type=type(exc).__name__)
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                rerank_trace["http_status"] = status
            log.warning("Wiki 精排失败，保留召回顺序: %s status=%s", type(exc).__name__, status)
        results = ordered[:top_k]
        return {
            **({"context": _build_context(results, query)} if include_context else {}),
            "pages": [p.to_dict() for p in results],
            "retrieval": {
                **retrieval,
                "expanded_page_ids": expanded_ids,
                "rerank": rerank_trace,
            },
        }

    def _retrieve_seed_pages(
        self,
        query: str,
        *,
        owner_account_id: str,
        top_k: int,
        kb_id: str,
    ) -> tuple[list[WikiPage], dict[str, Any]]:
        candidate_limit = max(self._candidate_limit, top_k)
        search_seeds = self.store.search(
            query,
            top_k=candidate_limit,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        index_seeds = self.store.search_index(
            query,
            top_k=candidate_limit,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        vector_seeds = self.store.search_vectors(
            query,
            top_k=candidate_limit,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        seeds = _fuse_seed_channels(
            search_seeds,
            index_seeds,
            vector_seeds,
            candidate_limit,
            fuse_weight=self._fuse_weight,
            index_weight=self._index_weight,
        )
        # Navigation summaries are a weaker signal than full-text/vector
        # relevance. By default they fill an undersized pool without changing
        # the primary channels' order. Positive weights opt into joint RRF.
        fallback_ids: list[str] = []
        if self._index_weight == 0:
            seen = {page.id for page in seeds}
            for page in index_seeds:
                if len(seeds) >= candidate_limit:
                    break
                if page.id not in seen:
                    seeds.append(page)
                    seen.add(page.id)
                    fallback_ids.append(page.id)
        return seeds, {
            "search_seed_page_ids": [page.id for page in search_seeds],
            "index_seed_page_ids": [page.id for page in index_seeds],
            "vector_seed_page_ids": [page.id for page in vector_seeds],
            "seed_page_ids": [page.id for page in seeds],
            "candidate_limit": candidate_limit,
            "channel_weights": {"search": 1.0, "index": self._index_weight, "vector": self._fuse_weight},
            "index_fallback_page_ids": fallback_ids,
        }


def _fuse_seed_channels(
    search_pages: list[WikiPage],
    index_pages: list[WikiPage],
    vector_pages: list[WikiPage],
    limit: int,
    *,
    fuse_weight: float = 0.9,
    index_weight: float = 0.0,
) -> list[WikiPage]:
    """用加权 RRF 合并正文搜索、index 导航与语义向量候选。"""
    pages: dict[str, WikiPage] = {}
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    seen_order = 0
    for weight, channel in (
        (1.0, search_pages),
        (index_weight, index_pages),
        (fuse_weight, vector_pages),
    ):
        if weight <= 0:
            continue
        for rank, page in enumerate(channel):
            pages[page.id] = page
            scores[page.id] = scores.get(page.id, 0.0) + weight / (60 + rank)
            if page.id not in first_seen:
                first_seen[page.id] = seen_order
                seen_order += 1
    ordered = sorted(
        pages.values(),
        key=lambda page: (
            -scores[page.id],
            first_seen[page.id],
            -page.updated_at,
        ),
    )
    return ordered[: max(0, limit)]


def _relevant_excerpt(content: str, question: str, limit: int = 2000) -> str:
    """优先选择命中问题的段落；无命中时回退正文开头。"""
    content = str(content or "")
    if len(content) <= limit:
        return content
    terms = query_terms(question)
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", content)
        if paragraph.strip()
    ]
    scored: list[tuple[int, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        folded = paragraph.casefold()
        score = sum(1 for term in terms if term in folded)
        if score:
            scored.append((score, -index, paragraph))
    if not scored:
        return content[:limit]
    selected: list[str] = []
    used = 0
    for _, _, paragraph in sorted(scored, reverse=True):
        extra = len(paragraph) + (2 if selected else 0)
        if used + extra > limit:
            remaining = limit - used
            if remaining > 80:
                selected.append(paragraph[:remaining])
            break
        selected.append(paragraph)
        used += extra
    return "\n\n".join(selected) or content[:limit]


def _build_context(pages: list[WikiPage], question: str = "") -> str:
    parts: list[str] = []
    for i, page in enumerate(pages, 1):
        parts.append(f"--- 页面 {i}: [[{page.title}]] ({page.page_type}) ---")
        quality = [
            f"来源: {', '.join(page.sources) if page.sources else '无'}",
            f"置信度: {page.confidence or '未标注'}",
            f"争议: {'是' if page.contested else '否'}",
        ]
        parts.append(" | ".join(quality))
        relevant_claims = [
            claim
            for claim in page.claims
            if not question
            or any(term in claim.statement.casefold() for term in query_terms(question))
        ][:3]
        if relevant_claims:
            parts.append("关键主张:")
            for claim in relevant_claims:
                evidence_ids = ", ".join(
                    item.source_id for item in claim.evidence if item.source_id
                )
                parts.append(
                    f"- {claim.statement} "
                    f"[confidence={claim.confidence}; evidence={evidence_ids or '无'}]"
                )
        parts.append(_relevant_excerpt(page.content, question))
        parts.append("")
    return "\n".join(parts)
