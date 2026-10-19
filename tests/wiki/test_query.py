"""WikiQuerier 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from crew.wiki.query import WikiQuerier, _fuse_seed_channels
from crew.wiki.rerank import RerankingHTTPError, RerankingProvider
from crew.wiki.schemas import WikiPage, WikiRelation
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


class _TestReranker(RerankingProvider):
    model = "test:cross-encoder"

    def __init__(self, scoring):
        self.scoring = scoring
        self.documents = []

    def score(self, query, documents):
        self.documents = documents
        return self.scoring(documents)


def _fixed_candidates(store, count=25):
    pages = [WikiPage(f"p{i}", "topic", f"Candidate {i}", f"body {i}", "") for i in range(count)]
    store.search = lambda *args, **kwargs: pages[:kwargs["top_k"]]
    store.search_index = lambda *args, **kwargs: []
    store.search_vectors = lambda *args, **kwargs: []
    return pages


def test_rerank_scores_candidates_before_top_k_and_preserves_exact_alias(store):
    pages = _fixed_candidates(store)
    pages[3].aliases = ["lookup"]
    reranker = _TestReranker(lambda documents: list(range(len(documents))))
    result = WikiQuerier(store, reranker=reranker).search("lookup", top_k=2, expand_neighbors=False)
    assert [page["id"] for page in result["pages"]] == ["p3", "p24"]
    assert len(reranker.documents) == 25
    assert result["retrieval"]["rerank"]["status"] == "ok"
    assert len(result["retrieval"]["rerank"]["scores"]) == 25


@pytest.mark.parametrize("scores", [[1], [float("nan")] * 25, [float("inf")] * 25])
def test_bad_rerank_outputs_preserve_original_ranking(store, scores):
    _fixed_candidates(store)
    expected = WikiQuerier(store).search("question", top_k=3, expand_neighbors=False)
    reranker = _TestReranker(lambda documents: scores)
    result = WikiQuerier(store, reranker=reranker).search("question", top_k=3, expand_neighbors=False)
    assert result["pages"] == expected["pages"]
    assert result["retrieval"]["rerank"]["status"] == "failed"


def test_rerank_timeout_preserves_ranking_and_equal_scores_keep_fused_order(store):
    import httpx

    _fixed_candidates(store)
    expected = WikiQuerier(store).search("question", top_k=3, expand_neighbors=False)
    ties = _TestReranker(lambda documents: [1] * len(documents))
    assert WikiQuerier(store, reranker=ties).search("question", top_k=3, expand_neighbors=False)["pages"] == expected["pages"]

    def timeout(documents):
        raise httpx.ReadTimeout("private diagnostic")

    result = WikiQuerier(store, reranker=_TestReranker(timeout)).search("question", top_k=3, expand_neighbors=False)
    assert result["pages"] == expected["pages"]
    assert result["retrieval"]["rerank"]["error_type"] == "ReadTimeout"
    assert "private diagnostic" not in str(result["retrieval"])


def test_rerank_includes_expanded_neighbors_and_resolves_requested_owner(store):
    pages = _fixed_candidates(store, count=1)
    neighbor = WikiPage("neighbor", "topic", "Relevant evidence", "evidence body", "")
    store.get_neighbors = lambda *args, **kwargs: [neighbor]
    owners = []
    reranker = _TestReranker(lambda documents: [0, 10])

    def resolve(owner):
        owners.append(owner)
        return reranker

    result = WikiQuerier(store, reranker_for_owner=resolve).search("question", owner_account_id="owner-a", top_k=1)
    assert result["pages"][0]["id"] == neighbor.id
    assert result["retrieval"]["expanded_page_ids"] == [neighbor.id]
    assert owners == ["owner-a"]
    assert len(reranker.documents) == len(pages) + 1


def test_rerank_failure_trace_includes_safe_http_status(store):
    _fixed_candidates(store)

    def failure(documents):
        raise RerankingHTTPError(429)

    result = WikiQuerier(store, reranker=_TestReranker(failure)).search("query", expand_neighbors=False)
    assert result["retrieval"]["rerank"]["http_status"] == 429
    assert result["pages"][0]["id"] == "p0"


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
    neighbor = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="知识编译",
            content="把来源整理成长期知识。",
            file_path="",
        )
    )
    seed = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="Crew Wiki",
            content="Crew Wiki 使用知识编译流程。",
            file_path="",
            relations=[WikiRelation(neighbor.id, "related")],
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
    neighbor = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="证据链",
            content="# 证据链\n\n连接结论与来源。",
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
            relations=[WikiRelation(neighbor.id, "related")],
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


def test_broad_candidates_survive_fusion_before_exact_alias_selection(store):
    pages = [WikiPage(f"p{i}", "topic", f"Page {i}", "ordinary text", "") for i in range(25)]
    pages[-1].aliases = ["specific lookup"]
    # A candidate beyond the old 2*top_k budget must remain available to
    # final ranking. Index-only candidates are mixed into the same pool.
    store.search = lambda *args, **kwargs: pages[:kwargs["top_k"]]
    store.search_index = lambda *args, **kwargs: []
    store.search_vectors = lambda *args, **kwargs: []
    result = WikiQuerier(store).search("specific lookup", top_k=5, expand_neighbors=False)
    assert result["pages"][0]["id"] == "p24"
    assert len(result["retrieval"]["seed_page_ids"]) == 25
    assert result["retrieval"]["candidate_limit"] == 50


def test_direct_candidates_keep_fused_order_instead_of_raw_keyword_count(store):
    rare = WikiPage("rare", "topic", "Rare match", "neural", "")
    common = WikiPage("common", "topic", "Common words", "what is the evidence for neural therapy", "")
    store.search = lambda *args, **kwargs: [rare, common]
    store.search_index = lambda *args, **kwargs: []
    store.search_vectors = lambda *args, **kwargs: []
    result = WikiQuerier(store).search(
        "what is the evidence for neural therapy", top_k=2, expand_neighbors=False,
    )
    assert [page["id"] for page in result["pages"]] == ["rare", "common"]


def test_zero_weight_channel_does_not_add_candidates():
    page = WikiPage("nav", "topic", "Navigation", "text", "")
    assert _fuse_seed_channels([], [page], [], 50, index_weight=0) == []


def test_navigation_fills_pool_without_demoting_primary_search(store):
    primary = WikiPage("primary", "topic", "Paper", "neural", "")
    navigation = WikiPage("nav", "topic", "Overview", "ordinary", "")
    store.search = lambda *args, **kwargs: [primary]
    store.search_index = lambda *args, **kwargs: [navigation, primary]
    store.search_vectors = lambda *args, **kwargs: []
    result = WikiQuerier(store).search("neural question", top_k=2, expand_neighbors=False)
    assert [page["id"] for page in result["pages"]] == ["primary", "nav"]
    assert result["retrieval"]["index_fallback_page_ids"] == ["nav"]


@pytest.mark.parametrize("kwargs", [
    {"candidate_limit": 0}, {"index_weight": -1},
    {"index_weight": float("nan")}, {"fuse_weight": float("inf")},
])
def test_invalid_retrieval_settings_are_rejected(store, kwargs):
    with pytest.raises(ValueError):
        WikiQuerier(store, **kwargs)


def test_search_rejects_nonpositive_top_k(querier):
    with pytest.raises(ValueError):
        querier.search("query", top_k=0)
