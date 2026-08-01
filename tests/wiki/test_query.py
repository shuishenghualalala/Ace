"""WikiQuerier 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from crew.wiki.schemas import WikiPage
from crew.wiki.query import WikiQuerier
from crew.wiki.store import FileSystemWikiStore


@pytest.fixture
def store(tmp_path: Path) -> FileSystemWikiStore:
    return FileSystemWikiStore(base_dir=tmp_path)


@pytest.fixture
def querier(store: FileSystemWikiStore) -> WikiQuerier:
    return WikiQuerier(store)


def test_query_no_results(querier):
    result = querier.query("不存在的主题")
    assert result["pages"] == []
    assert "暂未找到" in result["context"]
    assert result["answer"] == ""


def test_query_finds_all_pages(querier, store):
    store.save_page(
        WikiPage(
            id="p1",
            page_type="topic",
            title="页面一",
            content="这是关于 Crew 的内容。",
            file_path="topics/页面一.md",
        )
    )
    store.save_page(
        WikiPage(
            id="p2",
            page_type="topic",
            title="页面二",
            content="这是关于 Crew 的另一部分内容。",
            file_path="topics/页面二.md",
        )
    )

    result = querier.query("Crew")

    assert len(result["pages"]) == 2
    assert {p["title"] for p in result["pages"]} == {"页面一", "页面二"}
    assert "页面一" in result["context"]
    assert "页面二" in result["context"]


def test_query_kb_isolation(querier, store):
    store.save_page(
        WikiPage(
            id="p1",
            page_type="topic",
            title="共享标题",
            content="default KB 内容",
            file_path="topics/共享标题.md",
        ),
        kb_id="default",
    )
    store.save_page(
        WikiPage(
            id="p2",
            page_type="topic",
            title="共享标题",
            content="project KB 内容",
            file_path="topics/共享标题.md",
        ),
        kb_id="project_a",
    )

    default_result = querier.query("共享标题", kb_id="default")
    project_result = querier.query("共享标题", kb_id="project_a")

    assert len(default_result["pages"]) == 1
    assert "default KB 内容" in default_result["context"]
    assert len(project_result["pages"]) == 1
    assert "project KB 内容" in project_result["context"]


def test_query_respects_top_k(querier, store):
    for i in range(5):
        store.save_page(
            WikiPage(
                id=f"p{i}",
                page_type="topic",
                title=f"页面 {i}",
                content=f"Crew 相关内容 {i}",
                file_path=f"topics/页面_{i}.md",
            )
        )

    result = querier.query("Crew", top_k=2)
    assert len(result["pages"]) == 2


def test_query_context_format(querier, store):
    store.save_page(
        WikiPage(
            id="p1",
            page_type="topic",
            title="格式化测试",
            content="A" * 2500,
            file_path="topics/格式化测试.md",
        )
    )

    result = querier.query("格式化测试")

    assert "--- 页面 1: [[格式化测试]] (topic) ---" in result["context"]
    # 内容截断到 2000 字符
    assert len(result["context"]) < 2600


def test_query_expands_one_hop_graph_neighbors(querier, store):
    seed = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="Crew Wiki",
            content="Crew Wiki 使用知识编译流程。",
            file_path="",
            related=["知识编译"],
        )
    )
    neighbor = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="知识编译",
            content="把来源整理成长期知识。",
            file_path="",
            related=["Crew Wiki"],
        )
    )

    result = querier.query("Crew Wiki", top_k=3)

    assert {page["id"] for page in result["pages"]} == {seed.id, neighbor.id}
    assert neighbor.id in result["retrieval"]["expanded_page_ids"]


def test_query_fuses_index_and_full_text_before_graph_expansion(querier, store):
    direct = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="检索入口",
            content="# 检索入口\n\n协同检索从正文召回这个页面。",
            file_path="",
        )
    )
    indexed = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="架构导航",
            content="# 架构导航\n\n正文没有查询词。",
            file_path="",
            related=["证据链"],
        )
    )
    neighbor = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="证据链",
            content="# 证据链\n\n连接结论与来源。",
            file_path="",
            related=["架构导航"],
        )
    )
    store.init_kb()
    (store._dir() / "index.md").write_text(
        "# Crew Wiki\n\n"
        "## 主题\n\n"
        "- [[检索入口]] — 协同检索的正文入口\n"
        "- [[架构导航]] — 协同检索的全局分类与关系入口\n",
        encoding="utf-8",
    )

    result = querier.query("协同检索", top_k=3)

    result_ids = {page["id"] for page in result["pages"]}
    assert result_ids == {direct.id, indexed.id, neighbor.id}
    assert direct.id in result["retrieval"]["search_seed_page_ids"]
    assert indexed.id in result["retrieval"]["index_seed_page_ids"]
    assert len(result["retrieval"]["seed_page_ids"]) == len(
        set(result["retrieval"]["seed_page_ids"])
    )
    assert neighbor.id in result["retrieval"]["expanded_page_ids"]


def test_search_uses_same_index_and_full_text_fusion(querier, store):
    page = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="导航候选",
            content="# 导航候选\n\n正文没有检索词。",
            file_path="",
        )
    )
    store.init_kb()
    (store._dir() / "index.md").write_text(
        "# Crew Wiki\n\n## 主题\n\n"
        "- [[导航候选]] — 跨来源关联检索入口\n",
        encoding="utf-8",
    )

    result = querier.search("跨来源")

    assert [item["id"] for item in result["pages"]] == [page.id]
    assert result["retrieval"]["index_seed_page_ids"] == [page.id]
    assert result["retrieval"]["search_seed_page_ids"] == []
    assert "context" in result


def test_search_can_skip_neighbor_expansion_and_context(querier, store):
    seed = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="入口",
            content="统一检索入口。",
            file_path="",
            related=["邻居"],
        )
    )
    store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="邻居",
            content="关联内容。",
            file_path="",
            related=["入口"],
        )
    )

    result = querier.search(
        "统一检索",
        expand_neighbors=False,
        include_context=False,
    )

    assert [page["id"] for page in result["pages"]] == [seed.id]
    assert result["retrieval"]["expanded_page_ids"] == []
    assert "context" not in result


def test_query_extracts_matching_paragraph_instead_of_page_prefix(querier, store):
    store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="大型说明",
            content=(
                "# 大型说明\n\n"
                + "背景信息。" * 1200
                + "\n\n关键结论：Crew Wiki 的资料入库需要经过知识对账。"
            ),
            file_path="",
        )
    )

    result = querier.query("知识对账")

    assert "资料入库需要经过知识对账" in result["context"]
    assert len(result["context"]) < 2800
