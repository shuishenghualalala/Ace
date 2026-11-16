"""Reranking response alignment, malformed outputs and owner client isolation."""

from __future__ import annotations

import json

import httpx
import pytest

from crew.wiki.config import WikiRerankConfig
from crew.wiki.rerank import HTTPRerankingProvider, OwnerRerankerResolver, RerankingHTTPError


def test_http_scores_follow_input_indices_and_request_entire_pool():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"index": 2, "relevance_score": 9}, {"index": 0, "relevance_score": 3},
            {"index": 1, "relevance_score": -1},
        ]})

    provider = HTTPRerankingProvider(
        api_key="dummy", base_url="https://example.test/v1",
        transport=httpx.MockTransport(handle), max_chunks_per_doc=8,
    )
    try:
        assert provider.score("question", ["a", "b", "c"]) == [3, -1, 9]
        body = json.loads(requests[0].content)
        assert str(requests[0].url) == "https://example.test/v1/rerank"
        assert body["top_n"] == 3
        assert body["return_documents"] is False
        assert body["max_chunks_per_doc"] == 8
    finally:
        provider.close()


@pytest.mark.parametrize("results", [
    [{"index": 0, "relevance_score": 1}],
    [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 2}],
    [{"index": -1, "relevance_score": 1}, {"index": 1, "relevance_score": 2}],
    [{"index": 0.0, "relevance_score": 1}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "relevance_score": "nan"}, {"index": 1, "relevance_score": 2}],
])
def test_http_rejects_invalid_or_incomplete_results(results):
    provider = HTTPRerankingProvider(
        api_key="dummy", base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": results})),
    )
    try:
        with pytest.raises(ValueError):
            provider.score("question", ["a", "b"])
    finally:
        provider.close()


def test_http_empty_input_skips_network_and_errors_do_not_echo_response_body():
    provider = HTTPRerankingProvider(
        api_key="dummy", base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="DO_NOT_ECHO")),
    )
    try:
        assert provider.score("question", []) == []
        with pytest.raises(RuntimeError, match="HTTP status 401") as error:
            provider.score("question", ["a"])
        assert "DO_NOT_ECHO" not in str(error.value)
    finally:
        provider.close()


def test_owner_resolver_keeps_clients_separate_and_refreshes_changed_credentials():
    config = WikiRerankConfig(enabled=True)
    keys = {"a": "dummy-a", "b": "dummy-b"}
    resolver = OwnerRerankerResolver(lambda owner: (config, keys[owner], "https://example.test/v1"))
    try:
        a = resolver("a")
        assert resolver("a") is a
        b = resolver("b")
        assert a is not b
        keys["a"] = "rotated-dummy"
        assert resolver("a") is not a
        assert resolver("b") is b
    finally:
        resolver.close()
    assert a._client.is_closed and b._client.is_closed


def test_disabled_owner_needs_no_credentials():
    resolver = OwnerRerankerResolver(lambda owner: (WikiRerankConfig(), "", ""))
    assert resolver("a") is None
    resolver.close()


def test_retry_transient_statuses_then_align_successful_scores(monkeypatch):
    calls, sleeps = [], []

    def handle(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503 if len(calls) == 1 else 429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1}]})

    monkeypatch.setattr("crew.wiki.rerank.time.sleep", sleeps.append)
    provider = HTTPRerankingProvider(api_key="dummy", base_url="https://example.test/v1", transport=httpx.MockTransport(handle))
    try:
        assert provider.score("query", ["document"]) == [1]
        assert len(calls) == 3
        assert sleeps == [1, 1]
    finally:
        provider.close()


def test_retry_exhaustion_exposes_only_safe_status_and_respects_timeout_budget(monkeypatch):
    calls, sleeps = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, text="DO_NOT_ECHO")

    monkeypatch.setattr("crew.wiki.rerank.time.sleep", sleeps.append)
    provider = HTTPRerankingProvider(
        api_key="dummy", base_url="https://example.test/v1", timeout=0.2,
        transport=httpx.MockTransport(handle), max_retries=2,
    )
    try:
        with pytest.raises(RerankingHTTPError) as error:
            provider.score("query", ["document"])
        assert error.value.status_code == 429
        assert "DO_NOT_ECHO" not in str(error.value)
        assert len(calls) == 1 and sleeps == []
    finally:
        provider.close()
