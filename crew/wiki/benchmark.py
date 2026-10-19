"""Offline Wiki retrieval benchmarks over public corpora and immutable page snapshots.

Only corpus documents are indexed. Questions, answers and qrels stay on the
evaluation side. Production search/ranking code runs against a cached snapshot;
timings therefore exclude Markdown loading and are not gateway latency figures.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.query import WikiQuerier
from crew.wiki.rerank import RerankingProvider
from crew.wiki.schemas import WikiPage
from crew.wiki.search import SQLiteFTS5SearchIndex
from crew.wiki.store import FileSystemWikiStore


@dataclass(frozen=True)
class BenchmarkDocument:
    id: str
    title: str
    text: str


@dataclass(frozen=True)
class BenchmarkQuery:
    id: str
    text: str
    relevance: dict[str, float]
    answers: tuple[str, ...] = ()


@dataclass
class BenchmarkDataset:
    name: str
    documents: list[BenchmarkDocument]
    queries: list[BenchmarkQuery]
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        doc_ids = [doc.id for doc in self.documents]
        query_ids = [query.id for query in self.queries]
        if not doc_ids or not query_ids:
            raise ValueError("Benchmark corpus and queries must be nonempty")
        if len(set(doc_ids)) != len(doc_ids) or len(set(query_ids)) != len(query_ids):
            raise ValueError("Duplicate document or query ID")
        known = set(doc_ids)
        for query in self.queries:
            if not query.text.strip():
                raise ValueError(f"Empty query: {query.id}")
            if not query.relevance or not any(v > 0 for v in query.relevance.values()):
                raise ValueError(f"Query has no positive relevance labels: {query.id}")
            if not set(query.relevance).issubset(known):
                raise ValueError(f"Qrels reference missing documents: {query.id}")
            if any(not math.isfinite(v) or v < 0 for v in query.relevance.values()):
                raise ValueError(f"Invalid relevance grade: {query.id}")

    def sample(self, limit: int | None, seed: int = 42) -> BenchmarkDataset:
        self.validate()
        if limit is not None and limit <= 0:
            raise ValueError("Query limit must be positive")
        queries = sorted(self.queries, key=lambda query: query.id)
        if limit is not None and limit < len(queries):
            queries = sorted(random.Random(seed).sample(queries, limit), key=lambda q: q.id)
        return BenchmarkDataset(
            self.name,
            self.documents,
            queries,
            {**self.metadata, "seed": seed, "total_queries": len(self.queries)},
        )


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def load_beir(path: Path, *, name: str = "scifact", split: str = "test") -> BenchmarkDataset:
    documents = [
        BenchmarkDocument(str(row["_id"]), str(row.get("title", "")), str(row["text"]))
        for row in _jsonl(path / "corpus.jsonl")
    ]
    relevance: dict[str, dict[str, float]] = {}
    with (path / "qrels" / f"{split}.tsv").open(encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            query_id, doc_id = row["query-id"], row["corpus-id"]
            labels = relevance.setdefault(query_id, {})
            if doc_id in labels:
                raise ValueError(f"Duplicate qrel: {query_id}/{doc_id}")
            labels[doc_id] = float(row["score"])
    queries = [
        BenchmarkQuery(str(row["_id"]), str(row["text"]), relevance[str(row["_id"])])
        for row in _jsonl(path / "queries.jsonl")
        if str(row["_id"]) in relevance
    ]
    if set(relevance) != {query.id for query in queries}:
        raise ValueError("Qrels reference missing queries")
    dataset = BenchmarkDataset(name, documents, queries, {"split": split, "format": "BEIR"})
    dataset.validate()
    return dataset


def load_cmrc2018(path: Path) -> BenchmarkDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    documents: dict[str, BenchmarkDocument] = {}
    queries: list[BenchmarkQuery] = []
    for article in payload["data"]:
        for paragraph in article["paragraphs"]:
            text = paragraph["context"]
            doc_id = "cmrc_" + hashlib.sha256(text.encode()).hexdigest()
            documents.setdefault(doc_id, BenchmarkDocument(doc_id, article.get("title", ""), text))
            for question in paragraph["qas"]:
                answers = tuple(dict.fromkeys(answer["text"] for answer in question["answers"]))
                if not answers:
                    raise ValueError(f"Missing gold answer: {question['id']}")
                queries.append(BenchmarkQuery(
                    str(question["id"]), question["question"], {doc_id: 1.0}, answers,
                ))
    dataset = BenchmarkDataset(
        "cmrc2018", list(documents.values()), queries,
        {"split": "dev", "format": "CMRC2018 adapted to document retrieval"},
    )
    dataset.validate()
    return dataset


def retrieval_metrics(
    ranked_ids: Iterable[str], relevance: dict[str, float], cutoffs: Iterable[int] = (5, 10),
) -> dict[str, float]:
    """Macro-average these per-query metrics; nDCG uses linear relevance gains."""
    ranked = list(dict.fromkeys(ranked_ids))
    positive = {doc_id: grade for doc_id, grade in relevance.items() if grade > 0}
    if not positive:
        raise ValueError("Retrieval metrics require at least one relevant document")
    result: dict[str, float] = {}
    for k in cutoffs:
        if k <= 0:
            raise ValueError("Metric cutoffs must be positive")
        hits = [i + 1 for i, doc_id in enumerate(ranked[:k]) if doc_id in positive]
        dcg = sum(positive.get(doc_id, 0.0) / math.log2(i + 2)
                  for i, doc_id in enumerate(ranked[:k]))
        ideal = sum(grade / math.log2(i + 2)
                    for i, grade in enumerate(sorted(positive.values(), reverse=True)[:k]))
        result.update({
            f"Recall@{k}": len(hits) / len(positive),
            f"Hit@{k}": float(bool(hits)),
            f"MRR@{k}": 1.0 / hits[0] if hits else 0.0,
            f"nDCG@{k}": dcg / ideal,
        })
    return result


def project_sources(pages: Iterable[WikiPage], source_map: dict[str, str]) -> list[str]:
    """Project page results to unique original documents without consulting qrels."""
    result: list[str] = []
    seen: set[str] = set()
    for page in pages:
        source_ids = list(page.sources)
        source_ids.extend(e.source_id for claim in page.claims for e in claim.evidence)
        for source_id in sorted(set(source_ids)):
            if source_id not in source_map:
                raise ValueError(f"Unmapped source {source_id!r} on page {page.id!r}")
            doc_id = source_map[source_id]
            if doc_id not in seen:
                seen.add(doc_id)
                result.append(doc_id)
    return result


def source_pages(dataset: BenchmarkDataset) -> list[WikiPage]:
    """One source page per corpus document; gold QA data never enters the pages."""
    pages = []
    for doc in sorted(dataset.documents, key=lambda doc: doc.id):
        # Unique titles prevent navigation ambiguity for repeated document titles.
        title = f"{doc.title or 'Document'} [{doc.id}]"
        pages.append(WikiPage(
            id=doc.id, page_type="source", title=title, content=f"# {title}\n\n{doc.text}",
            file_path="", sources=[doc.id],
        ))
    return pages


class _SnapshotWikiStore(FileSystemWikiStore):
    """Cache only document reads; production retrieval/scoring stays inherited."""

    def __init__(self, root: Path, pages: list[WikiPage], embedding_provider: EmbeddingProvider | None = None) -> None:
        super().__init__(base_dir=root, embedding_provider=embedding_provider)
        self.pages = tuple(sorted(pages, key=lambda page: page.id))
        self.by_id = {page.id: page for page in pages}
        if len(self.by_id) != len(pages):
            raise ValueError("Duplicate Wiki page ID")
        index = self._search_index("", "default")
        index.sync_pages(self.pages)
        from crew.wiki.compiler import _page_index_summary

        entries = ["# 知识导航", "", "## 来源摘要", ""]
        entries.extend(f"- [[{page.title}]] — {page.summary or _page_index_summary(page.content)}"
                       for page in self.pages)
        (self._dir() / "index.md").write_text("\n".join(entries), encoding="utf-8")

    def _iter_pages(self, owner_account_id: str = "", kb_id: str = "default", brief: bool = False):
        if owner_account_id or kb_id != "default":
            raise ValueError("Benchmark snapshot only exposes its isolated default KB")
        return iter(self.pages)

    def get(self, page_id_str: str, owner_account_id: str = "", kb_id: str = "default"):
        if owner_account_id or kb_id != "default":
            raise ValueError("Benchmark snapshot only exposes its isolated default KB")
        return self.by_id.get(page_id_str)

    def count_pages(self, owner_account_id: str = "", kb_id: str = "default") -> int:
        # Snapshot pages live in memory, so filesystem counting would report
        # zero and suppress lazy vector rebuilding and vector top_k entirely.
        if owner_account_id or kb_id != "default":
            raise ValueError("Benchmark snapshot only exposes its isolated default KB")
        return len(self.pages)


VARIANT_DESCRIPTIONS = {
    "fts-bm25-and": "Legacy strict FTS5 AND reference; all literal query tokens must match",
    "fts-bm25-or": "Benchmark-only FTS5 OR query; same tokenizer, fields and BM25 weights",
    "wiki-lexical": "Production WikiQuerier without embeddings or neighbor expansion",
    "wiki-lexical-neighbors": "Production WikiQuerier without embeddings, with neighbor expansion",
}
DEFAULT_VARIANTS = tuple(VARIANT_DESCRIPTIONS)
VARIANT_DESCRIPTIONS.update({
    "wiki-vector": "Production vector channel only; diagnostic dense retrieval",
    "wiki-hybrid": "Production WikiQuerier with lexical/vector RRF, without neighbors",
    "wiki-lexical-rerank": "Lexical candidates with an independent query/document reranker",
    "wiki-hybrid-rerank": "Lexical/vector RRF candidates with an independent reranker",
})
VARIANTS = tuple(VARIANT_DESCRIPTIONS)


def _fts_bm25_reference(
    index: SQLiteFTS5SearchIndex, query: str, top_k: int, *, match_all: bool = False,
) -> list[str]:
    """Independent literal AND/OR references, unaffected by production query policy."""
    tokens = index._fts_tokenize(query).split()
    if not tokens:
        return []
    # Quote tokens so words such as AND/OR/NOT remain literal search terms.
    connector = " AND " if match_all else " OR "
    fts_query = connector.join('"' + token.replace('"', '""') + '"' for token in tokens)
    with index._lock:
        rows = index._conn.execute(
            "SELECT page_id, bm25(pages_fts, 1, 10, 1, 1, 1) AS score "
            "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY score, rowid LIMIT ?",
            (fts_query, top_k),
        ).fetchall()
    return [str(row[0]) for row in rows]


def run_benchmark(
    dataset: BenchmarkDataset,
    *,
    variants: Iterable[str] = DEFAULT_VARIANTS,
    cutoffs: Iterable[int] = (5, 10),
    pages: list[WikiPage] | None = None,
    source_map: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    candidate_limit: int = 50,
    index_weight: float = 0.0,
    fuse_weight: float = 0.9,
    embedding_provider: EmbeddingProvider | None = None,
    reranker: RerankingProvider | None = None,
    rerank_limit: int = 50,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    dataset.validate()
    variants, cutoffs = tuple(variants), tuple(sorted(set(cutoffs)))
    if not variants or not cutoffs or min(cutoffs) <= 0:
        raise ValueError("Variants and positive cutoffs are required")
    if len(set(variants)) != len(variants) or set(variants) - set(VARIANTS):
        raise ValueError("Invalid or duplicate retrieval variant")
    needs_embedding = any(variant.startswith("wiki-hybrid") or variant == "wiki-vector" for variant in variants)
    needs_rerank = any(variant.endswith("-rerank") for variant in variants)
    if needs_embedding and embedding_provider is None:
        raise ValueError("Vector/hybrid variants require an embedding provider")
    if needs_rerank and reranker is None:
        raise ValueError("Rerank variants require an independent reranking provider")
    snapshot = source_pages(dataset) if pages is None else pages
    if not snapshot:
        raise ValueError("Page snapshot must be nonempty")
    mapping = source_map if source_map is not None else {doc.id: doc.id for doc in dataset.documents}
    if set(mapping.values()) - {doc.id for doc in dataset.documents}:
        raise ValueError("Source map references unknown corpus documents")
    project_sources(snapshot, mapping)  # Fail on broken provenance before running any queries.
    result_rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="ace-wiki-benchmark-") as tmp:
        store = _SnapshotWikiStore(Path(tmp), snapshot)
        hybrid_store = None
        try:
            setup_started = time.perf_counter()
            if needs_embedding:
                hybrid_store = _SnapshotWikiStore(Path(tmp) / "hybrid", snapshot, embedding_provider)
                vector_index = hybrid_store._ensure_vectors("", "default")
                indexed = vector_index._conn.execute("SELECT COUNT(*) FROM pages_vectors").fetchone()[0]
                if indexed != len(snapshot):
                    raise ValueError(f"Embedding index incomplete: {indexed}/{len(snapshot)} pages")
            setup_ms = (time.perf_counter() - setup_started) * 1000
            index = store._search_index("", "default")
            for variant in variants:
                uses_embedding = variant.startswith("wiki-hybrid") or variant == "wiki-vector"
                uses_rerank = variant.endswith("-rerank")
                selected_store = hybrid_store if uses_embedding else store
                querier = WikiQuerier(
                    selected_store, candidate_limit=candidate_limit, index_weight=index_weight,
                    fuse_weight=fuse_weight, reranker=reranker if uses_rerank else None,
                    rerank_limit=rerank_limit,
                )
                rows: list[dict[str, Any]] = []
                for number, query in enumerate(dataset.queries, 1):
                    started = time.perf_counter()
                    trace: dict[str, Any] = {}
                    if variant == "fts-bm25-and":
                        found = [store.by_id[pid] for pid in _fts_bm25_reference(
                            index, query.text, max(cutoffs), match_all=True,
                        )]
                    elif variant == "fts-bm25-or":
                        found = [store.by_id[pid] for pid in _fts_bm25_reference(index, query.text, max(cutoffs))]
                    elif variant == "wiki-vector":
                        found = selected_store.search_vectors(query.text, top_k=max(cutoffs))
                        trace = {"vector_seed_page_ids": [page.id for page in found]}
                    else:
                        result = querier.search(
                            query.text, top_k=max(cutoffs),
                            expand_neighbors=variant.endswith("-neighbors"), include_context=False,
                        )
                        found = [store.by_id[page["id"]] for page in result["pages"]]
                        trace = result["retrieval"]
                        if uses_rerank and found and trace.get("rerank", {}).get("status") != "ok":
                            status = trace.get("rerank", {}).get("http_status")
                            raise ValueError(f"Reranking failed on query {query.id} (HTTP {status}); refusing mislabeled results")
                    if uses_embedding and len(selected_store._query_similarities(query.text, "", "default")) != len(snapshot):
                        raise ValueError(f"Vector search failed on query {query.id}; refusing partial results")
                    duration_ms = (time.perf_counter() - started) * 1000
                    ranked = project_sources(found, mapping)
                    row = {
                        "variant": variant, "query_id": query.id, "query": query.text,
                        "page_ids": [page.id for page in found], "document_ids": ranked,
                        "relevance": query.relevance,
                        "metrics": retrieval_metrics(ranked, query.relevance, cutoffs),
                        "duration_ms": duration_ms, "retrieval": trace,
                    }
                    rows.append(row)
                    if progress and (number % 25 == 0 or number == len(dataset.queries)):
                        progress(f"{dataset.name}/{variant}: {number}/{len(dataset.queries)}")
                summaries[variant] = {
                    "metrics": {key: statistics.mean(row["metrics"][key] for row in rows)
                                for key in rows[0]["metrics"]},
                    "mean_query_ms": statistics.mean(row["duration_ms"] for row in rows),
                    "empty_result_count": sum(not row["document_ids"] for row in rows),
                    "embedding_enabled": uses_embedding,
                    "rerank_enabled": uses_rerank,
                    "rerank_failure_count": sum(row["retrieval"].get("rerank", {}).get("status") == "failed" for row in rows),
                }
                result_rows.extend(rows)
        finally:
            store.close()
            if hybrid_store is not None:
                hybrid_store.close()
    report = {
        "dataset": dataset.name, "documents": len(dataset.documents), "pages": len(snapshot),
        "queries": len(dataset.queries), "cutoffs": cutoffs, "metadata": dataset.metadata,
        "query_ids": [query.id for query in dataset.queries],
        "dataset_sha256": dataset_fingerprint(dataset), "snapshot_sha256": _fingerprint([
            page.to_dict() for page in sorted(snapshot, key=lambda page: page.id)
        ]),
        "source_map_sha256": _fingerprint(mapping),
        "embedding_enabled": needs_embedding, "rerank_enabled": needs_rerank,
        "models": {"embedding": embedding_provider.model if needs_embedding else None,
                   "rerank": reranker.model if needs_rerank else None},
        "embedding_index_setup_ms": setup_ms,
        "answer_generation_evaluated": False,
        "snapshot_kind": "corpus source pages" if pages is None else "exported Wiki pages",
        "search_top_k": max(cutoffs),
        "retrieval_settings": {"candidate_limit": candidate_limit, "index_weight": index_weight,
                               "fuse_weight": fuse_weight, "rerank_limit": rerank_limit},
        "relation_count": sum(len(page.relations) for page in snapshot),
        "variant_descriptions": {variant: VARIANT_DESCRIPTIONS[variant] for variant in variants},
        "timing_scope": "cached immutable pages; includes query model/cache calls; excludes corpus embedding/index setup and disk loading",
        "ranking_unit": "unique original documents, projected from page provenance",
        "variants": summaries,
    }
    return report, result_rows


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def dataset_fingerprint(dataset: BenchmarkDataset) -> str:
    return _fingerprint({
        "documents": [vars(doc) for doc in sorted(dataset.documents, key=lambda doc: doc.id)],
        "queries": [vars(query) for query in sorted(dataset.queries, key=lambda query: query.id)],
    })


def write_report(path: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (path / "queries.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    metrics = list(next(iter(report["variants"].values()))["metrics"])
    lines = [
        f"# Ace Wiki 检索评测：{report['dataset']}", "",
        f"语料 {report['documents']} 篇；Wiki 页面 {report['pages']} 个；查询 {report['queries']} 条。",
        "", f"本次为公开数据检索评测：embedding={report['embedding_enabled']}，rerank={report['rerank_enabled']}；未评测答案质量。",
        "缓存固定页面后运行检索；耗时不包含磁盘读取，不能视为 Gateway 响应时间。",
        "", "| 方案 | " + " | ".join(metrics) + " | 平均查询 ms | 空结果 |",
        "| --- | " + " | ".join("---:" for _ in metrics) + " | ---: | ---: |",
    ]
    for variant, summary in report["variants"].items():
        values = " | ".join(f"{summary['metrics'][key]:.4f}" for key in metrics)
        lines.append(f"| {variant} | {values} | {summary['mean_query_ms']:.2f} | {summary['empty_result_count']} |")
    lines.extend([
        "", "fts-bm25-and/fts-bm25-or 为同一索引上的独立 AND/OR 对照，采用相同分词和字段权重。",
        "生产查询对长问题使用 OR、对中文短关键词保留词组匹配；对照不是 BEIR 官方基线。",
        f"检索调用 top_k={report['search_top_k']}，各 @K 指标取该次排名前 K 篇原文。",
        f"Wiki 候选设置：{report['retrieval_settings']}。",
        f"模型：{report['models']}。查询耗时包括模型/缓存读取；语料向量建库单独记录，未调用答案生成模型。",
        f"页面关系数：{report['relation_count']}。原文 Source 页面没有生成关系，不能据此判断图检索效果。",
        "", f"数据 SHA-256：`{report['dataset_sha256']}`", "",
        f"快照 SHA-256：`{report['snapshot_sha256']}`", "",
        "逐题结果、来源映射后的排名和召回通道见 queries.jsonl。",
        "合成页面的来源投影只能衡量来源覆盖，不能替代句子级证据正确性评测。", "",
    ])
    if report["dataset"] == "cmrc2018":
        lines.append("CMRC2018 是改造后的共享语料检索任务；其分数不对应官方阅读理解榜单。\n")
    (path / "report.md").write_text("\n".join(lines), encoding="utf-8")
