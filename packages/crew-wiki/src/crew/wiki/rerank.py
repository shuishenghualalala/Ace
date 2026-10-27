"""Independent query/document reranking, separate from vector similarity."""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from crew.wiki.config import WikiRerankConfig


class RerankingProvider(ABC):
    model: str = ""

    @abstractmethod
    def score(self, query: str, documents: list[str]) -> list[float]:
        """Return one finite score per input document, in input order; higher is better."""

    def close(self) -> None:
        """Release provider resources."""


class RerankingHTTPError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Reranking HTTP status {status_code}")


def validate_scores(scores: list[float], count: int) -> list[float]:
    if len(scores) != count:
        raise ValueError("Reranker must return one score per candidate")
    values = [float(score) for score in scores]
    if not all(math.isfinite(score) for score in values):
        raise ValueError("Reranker scores must be finite")
    return values


class HTTPRerankingProvider(RerankingProvider):
    """SiliconFlow-compatible /rerank endpoint; response indices are authoritative."""

    def __init__(
        self, *, api_key: str, base_url: str, model: str = "BAAI/bge-reranker-v2-m3",
        timeout: float = 30.0, max_chunks_per_doc: int = 8,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not base_url or not model:
            raise ValueError("Reranking requires a key, endpoint and model")
        if not math.isfinite(timeout) or timeout <= 0 or max_chunks_per_doc <= 0 or max_retries < 0:
            raise ValueError("Reranking timeout and chunk limit must be positive")
        self.model = f"http:{model}"
        self._raw_model = model
        self._max_chunks_per_doc = max_chunks_per_doc
        self._timeout = timeout
        self._max_retries = max_retries
        self.last_input_tokens = 0
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/", timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"}, transport=transport,
        )

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        payload = {
            "model": self._raw_model, "query": query, "documents": documents,
            "top_n": len(documents), "return_documents": False,
            "max_chunks_per_doc": self._max_chunks_per_doc,
        }
        response = self._request(payload)
        # Keep response bodies (and possible credentials) out of error messages.
        if response.is_error:
            raise RerankingHTTPError(response.status_code)
        body = response.json()
        results = body["results"]
        token_meta = body.get("tokens")
        if not isinstance(token_meta, dict):
            meta = body.get("meta")
            token_meta = meta.get("tokens") if isinstance(meta, dict) else None
        tokens = token_meta.get("input_tokens") if isinstance(token_meta, dict) else None
        self.last_input_tokens = int(tokens) if isinstance(tokens, (int, float)) and math.isfinite(tokens) and tokens > 0 else 0
        scores: dict[int, float] = {}
        for item in results:
            index = item["index"]
            if type(index) is not int or not 0 <= index < len(documents) or index in scores:
                raise ValueError("Invalid or duplicate reranking response index")
            scores[index] = item["relevance_score"]
        if set(scores) != set(range(len(documents))):
            raise ValueError("Incomplete reranking response")
        return validate_scores([scores[index] for index in range(len(documents))], len(documents))

    def _request(self, payload: dict) -> httpx.Response:
        deadline = time.monotonic() + self._timeout
        for attempt in range(self._max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout("Reranking deadline exceeded")
            response = None
            try:
                response = self._client.post("rerank", json=payload, timeout=remaining)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
            else:
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == self._max_retries:
                    return response
            delay = min(0.5 * 2 ** attempt, 5.0)
            if response is not None:
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    if math.isfinite(retry_after):
                        delay = max(delay, min(max(0.0, retry_after), 10.0))
                except ValueError:
                    pass
                response.close()
            if time.monotonic() + delay >= deadline:
                if response is not None:
                    raise RerankingHTTPError(response.status_code)
                raise httpx.ReadTimeout("Reranking deadline exceeded")
            time.sleep(delay)
        raise RuntimeError("Unreachable reranking retry state")

    def close(self) -> None:
        self._client.close()


class OwnerRerankerResolver:
    """Cache clients by owner and effective settings, with no cross-owner key fallback."""

    def __init__(self, settings: Callable[[str], tuple[WikiRerankConfig, str, str]]) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple, HTTPRerankingProvider]] = {}
        self._providers: list[HTTPRerankingProvider] = []

    def __call__(self, owner: str) -> RerankingProvider | None:
        config, key, base_url = self._settings(owner)
        if not config.enabled:
            return None
        signature = (config.model, key, base_url, config.timeout, config.max_chunks_per_doc, config.max_retries)
        with self._lock:
            previous = self._cache.get(owner)
            if previous is not None and previous[0] == signature:
                return previous[1]
            provider = HTTPRerankingProvider(
                api_key=key, base_url=base_url, model=config.model,
                timeout=config.timeout, max_chunks_per_doc=config.max_chunks_per_doc,
                max_retries=config.max_retries,
            )
            self._cache[owner] = (signature, provider)
            # In-flight searches may still use a replaced client. Close all
            # clients together at App shutdown, after consumers have stopped.
            self._providers.append(provider)
            return provider

    def close(self) -> None:
        with self._lock:
            for provider in self._providers:
                provider.close()
            self._providers.clear()
            self._cache.clear()
