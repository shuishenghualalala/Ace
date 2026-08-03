"""HotpotQA 多跳推理评测测试。"""

from __future__ import annotations

import pytest

from tests.wiki.eval.eval_runner import EvalReport
from tests.wiki.eval.hotpotqa_adapter import HOTPOTQA_KB_ID, HotpotQAAdapter


class TestHotpotQADataset:
    """HotpotQA 内嵌数据集完整性测试。"""

    @pytest.fixture
    def adapter(self, tmp_path):
        return HotpotQAAdapter.from_embedded(tmp_path)

    def test_dataset_has_15_questions(self, adapter):
        assert len(adapter.dataset["questions"]) == 15

    def test_all_questions_are_bridge_or_comparison(self, adapter):
        for q in adapter.dataset["questions"]:
            assert q["type"] in ("bridge", "comparison"), f"{q['id']}: type={q['type']}"

    def test_all_questions_have_two_expected_pages(self, adapter):
        """多跳推理题至少需要 cross 两个页面。"""
        for q in adapter.dataset["questions"]:
            assert len(q["expected_pages"]) >= 2, (
                f"{q['id']}: expected_pages={q['expected_pages']}, 需要 >= 2"
            )

    def test_all_expected_pages_have_wiki_docs(self, adapter):
        docs = adapter.dataset["_wiki_documents"]
        for q in adapter.dataset["questions"]:
            for title in q["expected_pages"]:
                assert title in docs, (
                    f"{q['id']}: expected_page '{title}' 没有对应的 _wiki_documents"
                )

    def test_all_wiki_docs_have_required_fields(self, adapter):
        docs = adapter.dataset["_wiki_documents"]
        for title, doc in docs.items():
            assert "content" in doc, f"'{title}': 缺少 content"
            assert "page_type" in doc, f"'{title}': 缺少 page_type"
            assert len(doc["content"]) > 50, (
                f"'{title}': content 太短 ({len(doc['content'])} chars)"
            )

    def test_ids_are_unique(self, adapter):
        ids = [q["id"] for q in adapter.dataset["questions"]]
        assert len(ids) == len(set(ids))

    def test_question_breakdown(self, adapter):
        breakdown = adapter.get_question_breakdown()
        assert "bridge" in breakdown
        assert breakdown["bridge"] >= 8  # 至少 8 道 bridge
        assert breakdown["bridge"] + breakdown.get("comparison", 0) == 15


class TestHotpotQAIngestion:
    """文档 → Wiki 页面实例化测试。"""

    @pytest.fixture
    def adapter(self, tmp_path):
        return HotpotQAAdapter.from_embedded(tmp_path)

    def test_ingest_creates_all_pages(self, adapter):
        doc_count = len(adapter.dataset["_wiki_documents"])
        created = adapter.ingest_all_documents()
        assert created == doc_count

        pages = adapter.store.list_all(kb_id=HOTPOTQA_KB_ID, limit=200)
        assert len(pages) == doc_count

    def test_ingest_pages_are_searchable(self, adapter):
        adapter.ingest_all_documents()
        # 检索应该能跨页面找到关联信息
        results = adapter.store.search("Mozart", top_k=5, kb_id=HOTPOTQA_KB_ID)
        assert len(results) > 0
        titles = [p.title for p in results]
        assert "Wolfgang Amadeus Mozart" in titles

    def test_ingest_is_idempotent(self, adapter):
        first = adapter.ingest_all_documents()
        second = adapter.ingest_all_documents()
        assert first == second
        pages = adapter.store.list_all(kb_id=HOTPOTQA_KB_ID, limit=200)
        assert len(pages) == first  # 不重复创建

    def test_kb_is_created(self, adapter):
        adapter.ingest_all_documents()
        kbs = {kb.id: kb for kb in adapter.store.list_kbs()}
        assert HOTPOTQA_KB_ID in kbs


class TestHotpotQAEval:
    """端到端多跳推理评测。"""

    @pytest.fixture
    def adapter(self, tmp_path):
        return HotpotQAAdapter.from_embedded(tmp_path)

    def test_run_eval_returns_report(self, adapter):
        report = adapter.run_eval()
        assert isinstance(report, EvalReport)
        assert report.mode == "retrieval"
        assert report.total_questions == 15

    def test_run_eval_has_by_type_breakdown(self, adapter):
        report = adapter.run_eval()
        assert "bridge" in report.by_type
        assert "comparison" in report.by_type

    def test_every_question_has_result(self, adapter):
        report = adapter.run_eval()
        result_ids = {r.question_id for r in report.results}
        expected_ids = {q["id"] for q in adapter.dataset["questions"]}
        assert result_ids == expected_ids

    def test_recall_range_valid(self, adapter):
        report = adapter.run_eval()
        assert 0.0 <= report.recall_at_5 <= 1.0
        assert 0.0 <= report.mrr <= 1.0

    def test_summary_contains_hotpotqa(self, adapter):
        report = adapter.run_eval()
        detailed = report.detailed_report()
        # 应包含多跳题目的特征信息
        assert "Recall@5" in detailed
        assert "[h0" in detailed  # HotpotQA question IDs
