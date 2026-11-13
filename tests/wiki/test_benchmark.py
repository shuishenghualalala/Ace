"""Public dataset adaptation, metric correctness, provenance and search integration."""

from __future__ import annotations

import json
import math

import pytest

from crew.wiki.benchmark import (
    BenchmarkDataset,
    BenchmarkDocument,
    BenchmarkQuery,
    dataset_fingerprint,
    load_beir,
    load_cmrc2018,
    project_sources,
    retrieval_metrics,
    run_benchmark,
    source_pages,
    write_report,
)
from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.rerank import RerankingProvider
from crew.wiki.schemas import WikiClaim, WikiEvidence, WikiPage, WikiRelation


def tiny_dataset() -> BenchmarkDataset:
    return BenchmarkDataset("tiny", [
        BenchmarkDocument("d1", "Neural research", "Neural therapy treats brain tumors."),
        BenchmarkDocument("d2", "Trial outcomes", "Survival improved in the clinical trial."),
        BenchmarkDocument("d3", "Ocean research", "Coral reefs support marine biodiversity."),
    ], [BenchmarkQuery("q1", "neural", {"d1": 1.0})])


def test_metrics_deduplicate_and_use_all_gold_documents():
    scores = retrieval_metrics(["d1", "d1", "d2"], {"d1": 1, "d2": 3}, (1, 2, 5))
    assert scores["Recall@1"] == 0.5
    assert scores["Recall@2"] == 1
    assert scores["MRR@2"] == 1
    assert scores["nDCG@1"] == pytest.approx(1 / 3)
    expected = (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3))
    assert scores["nDCG@2"] == pytest.approx(expected)
    assert scores["nDCG@5"] == pytest.approx(expected)


def test_metrics_empty_results_are_zero_not_excluded():
    assert set(retrieval_metrics([], {"d1": 1}).values()) == {0.0}
    assert retrieval_metrics(["wrong", "d1"], {"d1": 1}, (5,))["MRR@5"] == 0.5


@pytest.mark.parametrize("labels,cutoffs", [({}, (5,)), ({"d1": 0}, (5,)), ({"d1": 1}, (0,))])
def test_metrics_reject_invalid_inputs(labels, cutoffs):
    with pytest.raises(ValueError):
        retrieval_metrics([], labels, cutoffs)


def test_sampling_is_fixed_and_keeps_distractor_corpus():
    dataset = tiny_dataset()
    dataset.queries = [BenchmarkQuery(f"q{i:03}", "neural", {"d1": 1}) for i in range(30)]
    sampled = dataset.sample(7, seed=42)
    reversed_dataset = BenchmarkDataset(dataset.name, dataset.documents, list(reversed(dataset.queries)))
    assert [q.id for q in sampled.queries] == [q.id for q in reversed_dataset.sample(7, seed=42).queries]
    assert len(sampled.documents) == 3
    assert sampled.metadata["total_queries"] == 30
    assert dataset_fingerprint(sampled) == dataset_fingerprint(reversed_dataset.sample(7, seed=42))


@pytest.mark.parametrize("kind", ["duplicate_doc", "duplicate_query", "missing_doc", "nan", "negative"])
def test_dataset_rejects_broken_labels(kind):
    dataset = tiny_dataset()
    if kind == "duplicate_doc":
        dataset.documents.append(dataset.documents[0])
    elif kind == "duplicate_query":
        dataset.queries.append(dataset.queries[0])
    else:
        labels = {"missing": 1} if kind == "missing_doc" else {"d1": float("nan") if kind == "nan" else -1}
        dataset.queries = [BenchmarkQuery("q1", "neural", labels)]
    with pytest.raises(ValueError):
        dataset.validate()


def test_beir_uses_only_the_selected_split_and_rejects_missing_queries(tmp_path):
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"_id": "d1", "title": "T", "text": "body"}) + "\n")
    (tmp_path / "queries.jsonl").write_text(
        json.dumps({"_id": "test", "text": "question"}) + "\n" +
        json.dumps({"_id": "train", "text": "another question"}) + "\n",
    )
    (tmp_path / "qrels").mkdir()
    qrels = tmp_path / "qrels/test.tsv"
    qrels.write_text("query-id\tcorpus-id\tscore\ntest\td1\t1\n")
    assert [q.id for q in load_beir(tmp_path).queries] == ["test"]
    qrels.write_text("query-id\tcorpus-id\tscore\nmissing\td1\t1\n")
    with pytest.raises(ValueError, match="missing queries"):
        load_beir(tmp_path)


def test_cmrc_deduplicates_context_and_never_indexes_gold_qa(tmp_path):
    payload = {"data": [{"title": "Research", "paragraphs": [
        {"context": "Shared paper text", "qas": [
            {"id": "q1", "question": "SECRET_QUESTION", "answers": [{"text": "SECRET_ANSWER"}]},
        ]},
        {"context": "Shared paper text", "qas": [
            {"id": "q2", "question": "Other question", "answers": [{"text": "Other answer"}]},
        ]},
        {"context": "Distractor paper", "qas": []},
    ]}]}
    path = tmp_path / "cmrc.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    dataset = load_cmrc2018(path)
    assert len(dataset.documents) == 2
    assert dataset.queries[0].relevance == dataset.queries[1].relevance
    assert dataset.queries[0].answers == ("SECRET_ANSWER",)
    pages = source_pages(dataset)
    assert all("SECRET_QUESTION" not in page.content and "SECRET_ANSWER" not in page.content for page in pages)
    assert len(dataset.sample(1).documents) == 2


def test_source_projection_deduplicates_and_includes_claim_evidence():
    first = WikiPage("p1", "topic", "One", "text", "", sources=["s2", "s1"],
                     claims=[WikiClaim("claim", [WikiEvidence("s3")])])
    second = WikiPage("p2", "source", "Two", "text", "", sources=["s1"])
    assert project_sources([first, second], {"s1": "d1", "s2": "d1", "s3": "d2"}) == ["d1", "d2"]
    with pytest.raises(ValueError, match="Unmapped source"):
        project_sources([first], {"s1": "d1"})


def test_benchmark_runs_production_search_in_isolation(tmp_path, monkeypatch):
    real_home = tmp_path / "user-home"
    monkeypatch.setenv("CREW_HOME", str(real_home))
    report, rows = run_benchmark(tiny_dataset())
    assert not real_home.exists()
    assert len(rows) == 4
    assert report["embedding_enabled"] is False
    assert report["answer_generation_evaluated"] is False
    assert report["variants"]["fts-bm25-and"]["metrics"]["Recall@5"] == 1
    assert report["variants"]["fts-bm25-or"]["metrics"]["Recall@5"] == 1
    assert report["relation_count"] == 0
    assert report["search_top_k"] == 10
    assert all(row["document_ids"] == ["d1"] for row in rows)
    output = tmp_path / "report"
    write_report(output, report, rows)
    assert json.loads((output / "report.json").read_text())["queries"] == 1
    assert len((output / "queries.jsonl").read_text().splitlines()) == 4
    assert "未评测答案质量" in (output / "report.md").read_text()


def test_custom_compiled_snapshot_exercises_real_neighbor_expansion():
    dataset = tiny_dataset()
    dataset.queries = [BenchmarkQuery("q1", "neural", {"d2": 1})]
    pages = [
        WikiPage("p1", "entity", "Neural therapy", "# Neural therapy", "", sources=["s1"],
                 relations=[WikiRelation("p2", "uses")]),
        WikiPage("p2", "topic", "Trial results", "# Trial results\n\nSurvival improved.", "", sources=["s2"]),
    ]
    report, rows = run_benchmark(dataset, pages=pages, source_map={"s1": "d1", "s2": "d2"})
    assert report["variants"]["wiki-lexical"]["metrics"]["Recall@5"] == 0
    assert report["variants"]["wiki-lexical-neighbors"]["metrics"]["Recall@5"] == 1
    assert rows[-1]["retrieval"]["expanded_page_ids"] == ["p2"]


def test_disjunctive_bm25_reference_does_not_require_every_query_token():
    dataset = tiny_dataset()
    dataset.queries = [BenchmarkQuery("q1", "neural treatment", {"d1": 1})]
    report, rows = run_benchmark(dataset, variants=("fts-bm25-and", "fts-bm25-or"))
    assert rows[0]["document_ids"] == []
    assert rows[1]["document_ids"] == ["d1"]
    assert report["variants"]["fts-bm25-and"]["empty_result_count"] == 1
    assert report["variants"]["fts-bm25-or"]["metrics"]["Recall@5"] == 1


def test_disjunctive_reference_quotes_operator_words_and_ignores_empty_tokens():
    dataset = tiny_dataset()
    dataset.queries = [
        BenchmarkQuery("q1", "OR neural", {"d1": 1}),
        BenchmarkQuery("q2", "?!", {"d1": 1}),
    ]
    _, rows = run_benchmark(dataset, variants=("fts-bm25-or",))
    assert rows[0]["document_ids"] == ["d1"]
    assert rows[1]["document_ids"] == []


def test_benchmark_fails_before_queries_when_provenance_is_broken():
    pages = [WikiPage("p", "topic", "P", "text", "", sources=["unknown"])]
    with pytest.raises(ValueError, match="Unmapped source"):
        run_benchmark(tiny_dataset(), pages=pages)
    with pytest.raises(ValueError, match="unknown corpus"):
        run_benchmark(tiny_dataset(), source_map={"d1": "missing"})


def test_repeated_runs_have_identical_ranks_and_fingerprints():
    first, first_rows = run_benchmark(tiny_dataset())
    second, second_rows = run_benchmark(tiny_dataset())
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert [row["document_ids"] for row in first_rows] == [row["document_ids"] for row in second_rows]
    assert {key: value["metrics"] for key, value in first["variants"].items()} == {
        key: value["metrics"] for key, value in second["variants"].items()
    }


class _ModelEmbedding(EmbeddingProvider):
    model = "test:dense"
    dim = 2

    def embed(self, texts):
        return [[1, 0] if "Survival" in text else [0, 1] for text in texts]

    def embed_query(self, text):
        return [1, 0]


class _ModelReranker(RerankingProvider):
    model = "test:rerank"

    def score(self, query, documents):
        return [10 if "Survival" in doc else 0 for doc in documents]


def test_model_variants_use_real_channels_without_contaminating_lexical_control():
    dataset = tiny_dataset()
    dataset.queries = [BenchmarkQuery("q1", "neural", {"d2": 1})]
    report, rows = run_benchmark(
        dataset, variants=("wiki-lexical", "wiki-vector", "wiki-hybrid", "wiki-hybrid-rerank"),
        embedding_provider=_ModelEmbedding(), reranker=_ModelReranker(),
    )
    assert rows[0]["document_ids"] == ["d1"]
    assert rows[0]["retrieval"]["vector_seed_page_ids"] == []
    assert rows[1]["document_ids"] == ["d2"]
    assert rows[-1]["document_ids"][0] == "d2"
    assert rows[-1]["retrieval"]["rerank"]["status"] == "ok"
    assert report["embedding_enabled"] and report["rerank_enabled"]
    assert report["variants"]["wiki-hybrid-rerank"]["rerank_failure_count"] == 0


def test_benchmark_refuses_failed_rerank_instead_of_reporting_fallback_as_model_success():
    reranker = _ModelReranker()
    reranker.score = lambda query, documents: [float("nan")] * len(documents)
    with pytest.raises(ValueError, match="Reranking failed"):
        run_benchmark(tiny_dataset(), variants=("wiki-lexical-rerank",), reranker=reranker)


@pytest.mark.parametrize("variant", ["wiki-vector", "wiki-hybrid", "wiki-lexical-rerank"])
def test_model_variants_require_explicit_providers(variant):
    with pytest.raises(ValueError, match="require"):
        run_benchmark(tiny_dataset(), variants=(variant,))
