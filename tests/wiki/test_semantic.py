"""Wiki 语义检索集成测试：FileSystemWikiStore 接线 + WikiQuerier 融合。"""

import pytest

from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.query import WikiQuerier
from crew.wiki.schemas import WikiPage
from crew.wiki.store import FileSystemWikiStore


class _TopicEmbedding(EmbeddingProvider):
    """把文本映射到预定义语义桶的 one-hot 向量，模拟同义召回（确定性）。"""

    model = "test:topic"
    dim = 4

    @staticmethod
    def _topic(text: str) -> int:
        if any(word in text for word in ("故障", "bug", "报错", "排查")):
            return 0
        if any(word in text for word in ("周报", "总结", "汇报")):
            return 1
        return 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(self._topic(text)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(self._topic(text))

    @staticmethod
    def _vec(topic: int) -> list[float]:
        vector = [0.0] * 4
        vector[topic] = 1.0
        return vector


def _page(pid: str, title: str, content: str = "") -> WikiPage:
    return WikiPage(id=pid, page_type="topic", title=title, content=content, file_path="")


@pytest.fixture
def store(tmp_path):
    value = FileSystemWikiStore(
        base_dir=tmp_path,
        embedding_provider=_TopicEmbedding(),
    )
    try:
        yield value
    finally:
        value.close()


def test_semantic_recall_surfaces_synonym_page(store):
    """同义不同词：「怎么修 bug」应经向量通道召回「故障排查」。"""
    store.save_page(_page("fault", "故障排查", "故障排查流程"))
    store.save_page(_page("weekly", "周报", "每周汇报"))

    result = WikiQuerier(store).search("怎么修 bug", top_k=5)

    assert "fault" in [page["id"] for page in result["pages"]]
    assert result["retrieval"]["vector_seed_page_ids"] == ["fault"]


def test_rerank_boosts_semantic_match(store):
    """语义相似但零关键词的页面应越过 relevance>0 过滤被 store.search 召回。"""
    store.save_page(_page("fault", "故障排查", "故障排查流程"))
    store.save_page(_page("weekly", "周报", "每周汇报"))

    assert [page.id for page in store.search("怎么修 bug", top_k=5)] == ["fault"]


def test_no_provider_search_vectors_empty(tmp_path):
    """未注入 embedding provider 时语义通道返回空，不影响词法检索。"""
    store = FileSystemWikiStore(base_dir=tmp_path)
    try:
        store.save_page(_page("p1", "故障排查", "故障排查流程"))
        assert store.search_vectors("故障排查", top_k=5) == []
        assert [page.id for page in store.search("故障排查", top_k=5)] == ["p1"]
    finally:
        store.close()
