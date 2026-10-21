"""Wiki 向量索引测试。"""

import math

import pytest

from crew.wiki.schemas import WikiPage
from crew.wiki.vector import SQLiteVectorIndex, page_embedding_text


def _embed(texts: list[str]) -> list[list[float]]:
    """确定性：按字符哈希生成 8 维向量，字符重叠越多 cosine 越接近 1。"""
    result = []
    for text in texts:
        vector = [0.0] * 8
        for ch in text:
            vector[ord(ch) % 8] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        result.append([x / norm for x in vector])
    return result


@pytest.fixture
def index(tmp_path):
    value = SQLiteVectorIndex(
        tmp_path / "vectors.db",
        embed_fn=_embed,
        model="test:model",
        dim=8,
    )
    try:
        yield value
    finally:
        value.close()


def _page(pid: str, title: str, content: str = "") -> WikiPage:
    return WikiPage(id=pid, page_type="topic", title=title, content=content, file_path="")


def test_sync_and_search_by_vector(index):
    index.sync_page(_page("p1", "负责人", "负责"))
    index.sync_page(_page("p2", "周报", "周报内容"))

    scored = index.search_by_vector(_embed(["负责"])[0], top_k=5)
    assert scored[0][0] == "p1"


def test_delete_removes_from_index(index):
    index.sync_page(_page("p1", "待删除", "内容"))
    assert len(index.search_by_vector(_embed(["待删除"])[0], top_k=5)) == 1

    index.delete_pages(["p1"])
    assert index.search_by_vector(_embed(["待删除"])[0], top_k=5) == []


def test_batch_commits_pages_together(index):
    with index.batch():
        index.sync_page(_page("p1", "批量一", "批量"))
        index.sync_page(_page("p2", "批量二", "批量"))

    ids = {pid for pid, _ in index.search_by_vector(_embed(["批量"])[0], top_k=5)}
    assert ids == {"p1", "p2"}


def test_rebuild_replaces_all(index):
    index.sync_page(_page("p1", "旧页", "旧"))
    index.rebuild([_page("p2", "新页", "新")])

    ids = {pid for pid, _ in index.search_by_vector(_embed(["旧"])[0], top_k=5)}
    assert "p1" not in ids
    assert index.search_by_vector(_embed(["新"])[0], top_k=5)[0][0] == "p2"


def test_meta_reflects_model_after_sync(index):
    assert index.stored_model is None
    index.sync_page(_page("p1", "标题", "正文"))
    assert index.stored_model == "test:model"


def test_page_embedding_text_title_first():
    text = page_embedding_text(_page("p", "标题", "正文"))
    assert text.split("\n")[0] == "标题"
    assert "正文" in text
