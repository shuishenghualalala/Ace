"""FileSystemWikiStore per-owner embedding 解析与失效（多租户向量索引隔离）。"""

from __future__ import annotations

from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.store import FileSystemWikiStore


class _StubEmbedding(EmbeddingProvider):
    """按 model 名区分、返回固定向量的假 provider（不发网络）。"""

    def __init__(self, model: str, dim: int = 4) -> None:
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


def test_store_resolves_per_owner_provider(tmp_path):
    def resolver(owner: str):
        return _StubEmbedding(f"{owner}-model")

    store = FileSystemWikiStore(base_dir=tmp_path, provider_for_owner=resolver)
    try:
        idx_a = store._vector_index("owner-a", "default")
        idx_b = store._vector_index("owner-b", "default")
        assert idx_a is not None and idx_b is not None
        assert idx_a.model == "owner-a-model"
        assert idx_b.model == "owner-b-model"
    finally:
        store.close()


def test_store_falls_back_to_global_provider_without_resolver(tmp_path):
    store = FileSystemWikiStore(
        base_dir=tmp_path,
        embedding_provider=_StubEmbedding("global-model"),
    )
    try:
        idx = store._vector_index("any-owner", "default")
        assert idx is not None
        assert idx.model == "global-model"
    finally:
        store.close()


def test_store_provider_for_owner_none_disables_semantic(tmp_path):
    store = FileSystemWikiStore(base_dir=tmp_path, provider_for_owner=lambda owner: None)
    try:
        assert store._vector_index("owner-x", "default") is None
    finally:
        store.close()


def test_store_invalidate_owner_embedding_isolates_owners(tmp_path):
    owner_a_calls = {"count": 0}

    def resolver(owner: str):
        if owner == "owner-a":
            owner_a_calls["count"] += 1
            return (
                _StubEmbedding("owner-a-v1")
                if owner_a_calls["count"] == 1
                else _StubEmbedding("owner-a-v2")
            )
        return _StubEmbedding("owner-b")

    store = FileSystemWikiStore(base_dir=tmp_path, provider_for_owner=resolver)
    try:
        idx_a = store._vector_index("owner-a", "default")
        idx_b = store._vector_index("owner-b", "default")
        assert idx_a.model == "owner-a-v1"
        assert idx_b.model == "owner-b"

        store.invalidate_owner_embedding("owner-a")

        new_a = store._vector_index("owner-a", "default")
        assert new_a is not idx_a
        assert new_a.model == "owner-a-v2"
        # owner-b 的向量索引对象保持不变（未被误失效）
        assert store._vector_index("owner-b", "default") is idx_b
    finally:
        store.close()


def test_store_invalidate_embedding_drops_all(tmp_path):
    store = FileSystemWikiStore(
        base_dir=tmp_path,
        provider_for_owner=lambda owner: _StubEmbedding(owner),
    )
    try:
        idx_a = store._vector_index("owner-a", "default")
        idx_b = store._vector_index("owner-b", "default")

        store.invalidate_embedding()

        assert store._vector_index("owner-a", "default") is not idx_a
        assert store._vector_index("owner-b", "default") is not idx_b
    finally:
        store.close()
