"""Model cache keys, output validation and partial reuse without live services."""

from __future__ import annotations

import pytest

from crew.wiki.benchmark_models import (
    CachedEmbeddingProvider,
    CachedRerankingProvider,
    ModelOutputCache,
    PacedRerankingProvider,
)
from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.rerank import RerankingHTTPError, RerankingProvider


class _Embedding(EmbeddingProvider):
    model = "test:embedding"

    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        self.inputs.extend(texts)
        return [[len(text), 1] for text in texts]

    def embed_query(self, text):
        self.inputs.append("query:" + text)
        return [1, len(text)]


class _Reranker(RerankingProvider):
    model = "test:reranker"

    def __init__(self):
        self.inputs = []

    def score(self, query, documents):
        self.inputs.extend((query, doc) for doc in documents)
        return [float(len(doc)) for doc in documents]


def test_embedding_cache_keeps_query_and_document_modes_and_endpoint_identities_separate(tmp_path):
    provider = _Embedding()
    cache = ModelOutputCache(tmp_path / "embeddings.db", "endpoint-a:model-v1")
    try:
        cached = CachedEmbeddingProvider(provider, cache)
        assert cached.embed(["paper", "long paper"]) == [[5, 1], [10, 1]]
        assert cached.embed(["long paper", "paper"]) == [[10, 1], [5, 1]]
        assert cached.embed_query("paper") == [1, 5]
        assert cached.embed_query("paper") == [1, 5]
        assert provider.inputs == ["paper", "long paper", "query:paper"]
        assert cached.dim == 2
    finally:
        cache.close()
    cache = ModelOutputCache(tmp_path / "embeddings.db", "endpoint-b:model-v1")
    try:
        CachedEmbeddingProvider(provider, cache).embed(["paper"])
        assert provider.inputs[-1] == "paper"
        assert cache.misses == 1
    finally:
        cache.close()


def test_rerank_cache_reuses_pairs_across_candidate_sets_but_never_across_queries(tmp_path):
    provider = _Reranker()
    cache = ModelOutputCache(tmp_path / "scores.db", "endpoint:model:chunks=8")
    try:
        cached = CachedRerankingProvider(provider, cache)
        assert cached.score("q1", ["one", "two long"]) == [3, 8]
        assert cached.score("q1", ["two long", "new"]) == [8, 3]
        assert cached.score("q2", ["one"]) == [3]
        assert provider.inputs == [("q1", "one"), ("q1", "two long"), ("q1", "new"), ("q2", "one")]
        assert cached.live_pairs == 4
    finally:
        cache.close()


def test_malformed_embeddings_are_not_cached(tmp_path):
    provider = _Embedding()
    provider.embed = lambda texts: [[float("nan"), 1] for _ in texts]
    cache = ModelOutputCache(tmp_path / "invalid.db", "endpoint:model")
    try:
        with pytest.raises(ValueError, match="Invalid embedding"):
            CachedEmbeddingProvider(provider, cache).embed(["paper"])
        assert cache.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()[0] == 0
    finally:
        cache.close()


def test_offline_pacer_uses_actual_tokens_and_waits_for_window_expiry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("crew.wiki.benchmark_models.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("crew.wiki.benchmark_models.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    provider = _Reranker()
    provider.last_input_tokens = 100
    paced = PacedRerankingProvider(provider, tokens_per_minute=100)
    assert paced.score("q", ["document"]) == [8]
    assert paced.score("q", ["document"]) == [8]
    assert clock[0] >= 60
    assert paced.input_tokens == 200


def test_offline_rate_limit_cooldown_preserves_success_and_auth_errors_fail_immediately(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("crew.wiki.benchmark_models.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("crew.wiki.benchmark_models.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    provider = _Reranker()
    calls = []

    def limit_then_success(query, documents):
        calls.append(query)
        if len(calls) == 1:
            raise RerankingHTTPError(429)
        return [1] * len(documents)

    provider.score = limit_then_success
    paced = PacedRerankingProvider(provider)
    assert paced.score("q", ["doc"]) == [1]
    assert paced.rate_limit_waits == 1 and paced.wait_seconds == 60
    assert len(calls) == 2

    def unauthorized(query, documents):
        raise RerankingHTTPError(401)

    provider.score = unauthorized
    with pytest.raises(RerankingHTTPError):
        paced.score("q", ["doc"])
    assert paced.rate_limit_waits == 1
