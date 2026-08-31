"""Wiki 搜索索引抽象测试。"""

import tempfile
from pathlib import Path

import pytest

from crew.wiki.schemas import WikiPage
from crew.wiki.search import SQLiteFTS5SearchIndex


@pytest.fixture
def index():
    with tempfile.TemporaryDirectory() as tmp:
        value = SQLiteFTS5SearchIndex(Path(tmp) / "fts.db")
        try:
            yield value
        finally:
            value.close()


def _page(pid: str, title: str, content: str = "") -> WikiPage:
    return WikiPage(id=pid, page_type="topic", title=title, content=content, file_path="")


def test_index_and_search_chinese(index: SQLiteFTS5SearchIndex):
    """中文单字分词：搜"负责"命中"租户负责人"。"""
    index.sync_page(_page("p1", "租户负责人", "负责特定租户下 UCX 产品运营的人员。"))
    index.sync_page(_page("p2", "订购租户", "使用 UCX 产品的业务部门或合作方。"))

    assert index.search("负责", top_k=5) == ["p1"]
    assert set(index.search("租户", top_k=5)) == {"p1", "p2"}


def test_search_rank_prefers_title(index: SQLiteFTS5SearchIndex):
    """标题命中应比正文命中更靠前。"""
    index.sync_page(_page("p1", "周报", "项目 A 已经交付上线。"))
    index.sync_page(_page("p2", "项目总结", "周报内容整理。"))

    results = index.search("周报", top_k=5)
    assert results[0] == "p1"


def test_delete_removes_from_index(index: SQLiteFTS5SearchIndex):
    """删除页面后索引中不应再搜到。"""
    index.sync_page(_page("p1", "待删除", "这个页面即将被删除。"))
    assert index.search("删除", top_k=5) == ["p1"]

    index.delete_pages(["p1"])
    assert index.search("删除", top_k=5) == []


def test_delete_empty_list_is_noop(index: SQLiteFTS5SearchIndex):
    """删除空列表不应报错。"""
    index.delete_pages([])


def test_update_page_overwrites_index(index: SQLiteFTS5SearchIndex):
    """同步同一 page_id 应覆盖旧内容。"""
    index.sync_page(_page("p1", "旧标题", "旧关键词"))
    assert index.search("旧关键词", top_k=5) == ["p1"]

    index.sync_page(_page("p1", "新标题", "新关键词"))
    assert index.search("旧关键词", top_k=5) == []
    assert index.search("新关键词", top_k=5) == ["p1"]


def test_search_respects_top_k(index: SQLiteFTS5SearchIndex):
    """top_k 限制返回数量。"""
    for i in range(5):
        index.sync_page(_page(f"p{i}", f"通用标题 {i}", "通用内容"))

    assert len(index.search("通用", top_k=3)) == 3


def test_batch_sync_commits_pages_together(index: SQLiteFTS5SearchIndex):
    """批量同步应复用连接并在一个批次结束时一次提交。"""
    statements: list[str] = []
    index._conn.set_trace_callback(statements.append)
    with index.batch():
        index.sync_page(_page("p1", "批量一", "批量内容"))
        index.sync_page(_page("p2", "批量二", "批量内容"))

    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    assert set(index.search("批量内容", top_k=5)) == {"p1", "p2"}


def test_sync_pages_updates_all_pages(index: SQLiteFTS5SearchIndex):
    """显式批量 API 应将多个页面写入同一索引。"""
    index.sync_pages([
        _page("p1", "第一篇", "统一内容"),
        _page("p2", "第二篇", "统一内容"),
    ])

    assert set(index.search("统一内容", top_k=5)) == {"p1", "p2"}
