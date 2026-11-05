"""Persistent, content-addressed model outputs for repeatable public-data experiments.

Caches are scoped to an explicit endpoint/model identity. They never store keys,
answers or relevance labels. Failed or malformed model outputs are not cached.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.rerank import RerankingHTTPError, RerankingProvider, validate_scores


class ModelOutputCache:
    def __init__(self, path: Path, identity: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS outputs (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self.hits = 0
        self.misses = 0

    def key(self, kind: str, value: object) -> str:
        payload = json.dumps([self.identity, kind, value], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> bytes | None:
        row = self.connection.execute("SELECT value FROM outputs WHERE key=?", (key,)).fetchone()
        if row:
            self.hits += 1
            return row[0]
        self.misses += 1
        return None

    def put(self, values: list[tuple[str, bytes]]) -> None:
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO outputs VALUES (?, ?)", values)

    def close(self) -> None:
        self.connection.close()

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for key, value in self.connection.execute("SELECT key, value FROM outputs ORDER BY key"):
            digest.update(key.encode())
            digest.update(value)
        return digest.hexdigest()


class CachedEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self, provider: EmbeddingProvider, cache: ModelOutputCache,
        progress: Callable[[str], None] | None = None,
        batch_size: int = 32,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Embedding batch size must be positive")
        self.provider = provider
        self.cache = cache
        self.model, self.dim = provider.model, provider.dim
        self.progress = progress
        self.live_inputs = 0
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, query=False)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], query=True)[0]

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        keys = [self.cache.key("query" if query else "document", text) for text in texts]
        blobs = [self.cache.get(key) for key in keys]
        missing = [index for index, blob in enumerate(blobs) if blob is None]
        if missing:
            inputs = [texts[index] for index in missing]
            vectors = []
            for offset in range(0, len(inputs), self.batch_size):
                batch = inputs[offset:offset + self.batch_size]
                vectors.extend([self.provider.embed_query(text) for text in batch]
                               if query else self.provider.embed(batch))
            if len(vectors) != len(inputs) or not vectors:
                raise ValueError("Incomplete embedding output")
            dim = self.dim or len(vectors[0])
            if dim <= 0 or any(
                len(vector) != dim or not all(math.isfinite(x) for x in vector)
                or not any(vector) for vector in vectors
            ):
                raise ValueError("Invalid embedding dimensions or values")
            self.dim = dim
            writes = []
            for index, vector in zip(missing, vectors):
                blob = struct.pack(f"<{len(vector)}f", *vector)
                blobs[index] = blob
                writes.append((keys[index], blob))
            self.cache.put(writes)
            self.live_inputs += len(inputs)
            if self.progress and (not query or self.live_inputs % 25 == 0):
                self.progress(f"Embedding live inputs: {self.live_inputs}; cached hits: {self.cache.hits}")
        vectors = [list(struct.unpack(f"<{len(blob) // 4}f", blob)) for blob in blobs]
        if vectors:
            self.dim = self.dim or len(vectors[0])
        return vectors


class CachedRerankingProvider(RerankingProvider):
    def __init__(self, provider: RerankingProvider, cache: ModelOutputCache) -> None:
        self.provider, self.cache, self.model = provider, cache, provider.model
        self.live_pairs = 0

    def score(self, query: str, documents: list[str]) -> list[float]:
        keys = [self.cache.key("pair", [query, document]) for document in documents]
        blobs = [self.cache.get(key) for key in keys]
        missing = [index for index, blob in enumerate(blobs) if blob is None]
        if missing:
            scores = validate_scores(self.provider.score(query, [documents[i] for i in missing]), len(missing))
            writes = []
            for index, score in zip(missing, scores):
                blob = struct.pack("<d", score)
                blobs[index] = blob
                writes.append((keys[index], blob))
            self.cache.put(writes)
            self.live_pairs += len(missing)
        return [struct.unpack("<d", blob)[0] for blob in blobs]


class PacedRerankingProvider(RerankingProvider):
    """Offline experiment pacing; production searches retain their short timeout.

    Account quotas include tokens, so request count alone is insufficient. Use
    a conservative estimate before the call and actual token usage when exposed.
    """

    def __init__(
        self, provider: RerankingProvider, *, tokens_per_minute: int = 300_000,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if tokens_per_minute < 0:
            raise ValueError("Token budget must be nonnegative")
        self.provider, self.model = provider, provider.model
        self.budget = tokens_per_minute
        self.progress = progress
        self._events: deque[tuple[float, int]] = deque()
        self.rate_limit_waits = 0
        self.wait_seconds = 0.0
        self.input_tokens = 0

    @staticmethod
    def _estimate(query: str, documents: list[str]) -> int:
        def tokens(text):
            non_ascii = sum(ord(char) > 127 for char in text)
            return (len(text) - non_ascii) / 3 + non_ascii * 1.5 + 16
        return max(1, int(sum(tokens(document) + tokens(query) for document in documents)))

    def _wait(self, seconds: float, reason: str) -> None:
        if self.progress:
            self.progress(f"Rerank pacing: wait {seconds:.1f}s ({reason})")
        self.wait_seconds += seconds
        # The benchmark runs in a background process; short sleeps also make
        # cancellation responsive and keep the progress stream active.
        while seconds > 0:
            interval = min(seconds, 10.0)
            time.sleep(interval)
            seconds -= interval

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        estimated = self._estimate(query, documents)
        for attempt in range(3):
            if self.budget:
                while True:
                    now = time.monotonic()
                    while self._events and self._events[0][0] <= now - 60:
                        self._events.popleft()
                    if sum(tokens for _, tokens in self._events) + min(estimated, self.budget) <= self.budget:
                        break
                    self._wait(max(0.1, self._events[0][0] + 60.1 - now), "token budget")
            try:
                scores = self.provider.score(query, documents)
            except RerankingHTTPError as exc:
                if exc.status_code != 429 or attempt == 2:
                    raise
                self.rate_limit_waits += 1
                self._wait(60.0, "HTTP 429 cooldown")
                continue
            actual = getattr(self.provider, "last_input_tokens", 0) or estimated
            self.input_tokens += actual
            self._events.append((time.monotonic(), actual))
            return scores
        raise RuntimeError("Unreachable benchmark retry state")
