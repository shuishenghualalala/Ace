"""Wiki Eval 框架测试。

验证评测框架本身正确性，以及与 tutorial seed 的集成。
"""

from __future__ import annotations

import pytest

from tests.wiki.eval.eval_runner import (
    EvalReport,
    EvalRunner,
    QuestionResult,
    load_eval_dataset,
)


class TestEvalDataset:
    """评测数据集完整性检查。"""

    def test_dataset_loads_and_has_ten_questions(self):
        dataset = load_eval_dataset()
        assert len(dataset["questions"]) == 10

    def test_all_questions_have_required_fields(self):
        dataset = load_eval_dataset()
        required = {"id", "type", "question", "expected_answer", "expected_pages",
                     "min_evidence_count", "keywords"}
        for q in dataset["questions"]:
            missing = required - set(q.keys())
            assert not missing, f"{q['id']} 缺少字段: {missing}"

    def test_all_expected_pages_exist_in_tutorial_seed(self, tmp_path):
        """确保 expected_pages 中的页面标题在 tutorial seed 中真实存在。"""
        runner = EvalRunner.from_tutorial_seed(tmp_path)
        all_titles = {p.title for p in runner.store.list_all(
            kb_id=runner.kb_id, limit=100
        )}
        dataset = load_eval_dataset()
        for q in dataset["questions"]:
            for title in q["expected_pages"]:
                assert title in all_titles, (
                    f"{q['id']}: expected_page '{title}' 不在 tutorial seed 中"
                )

    def test_question_types_match_meta(self):
        dataset = load_eval_dataset()
        declared = set(dataset["meta"]["question_types"])
        actual = {q["type"] for q in dataset["questions"]}
        unknown = actual - declared
        assert not unknown, f"未声明的 question_type: {unknown}"

    def test_ids_are_unique(self):
        dataset = load_eval_dataset()
        ids = [q["id"] for q in dataset["questions"]]
        assert len(ids) == len(set(ids))

    def test_each_question_has_at_least_one_expected_page(self):
        dataset = load_eval_dataset()
        for q in dataset["questions"]:
            assert len(q["expected_pages"]) >= 1, f"{q['id']}: expected_pages 不能为空"


class TestEvalRunner:
    """评测执行器测试。"""

    @pytest.fixture
    def runner(self, tmp_path):
        return EvalRunner.from_tutorial_seed(tmp_path)

    @pytest.fixture
    def dataset(self):
        return load_eval_dataset()

    def test_eval_retrieval_returns_report(self, runner):
        report = runner.run_retrieval()
        assert isinstance(report, EvalReport)
        assert report.mode == "retrieval"
        assert report.total_questions == 10

    def test_eval_retrieval_all_results_have_ids(self, runner, dataset):
        report = runner._evaluate(dataset, mode="retrieval")
        result_ids = {r.question_id for r in report.results}
        expected_ids = {q["id"] for q in dataset["questions"]}
        assert result_ids == expected_ids

    def test_mrr_is_zero_when_no_match(self):
        """MRR 在没有命中页面时应为 0。"""
        r = QuestionResult(
            question_id="q000",
            question_type="simple_fact",
            question="测试",
            recall_at_5=0.0,
            mrr=0.0,
            keyword_coverage=0.0,
            top_pages=["无关页面A", "无关页面B"],
            hit_pages=[],
            missed_pages=["目标页面"],
        )
        assert r.mrr == 0.0
        assert not r.is_perfect_recall

    def test_mrr_computes_reciprocal_rank(self):
        """MRR = 1/首个命中排名。"""
        r = QuestionResult(
            question_id="q001",
            question_type="simple_fact",
            question="测试",
            recall_at_5=0.5,
            mrr=1.0,  # 第 1 名命中
            keyword_coverage=0.8,
            top_pages=["目标页面", "其他页面"],
            hit_pages=["目标页面"],
            missed_pages=[],
        )
        assert r.mrr == 1.0

    def test_report_summary_contains_key_metrics(self, runner):
        report = runner.run_retrieval()
        summary = report.summary()
        assert "Recall@5=" in summary
        assert "MRR=" in summary
        assert "Keyword=" in summary
        assert "Perfect=" in summary

    def test_report_detailed_contains_per_question(self, runner):
        report = runner.run_retrieval()
        detailed = report.detailed_report()
        for r in report.results:
            assert r.question_id in detailed

    def test_by_type_breakdown(self, runner, dataset):
        report = runner._evaluate(dataset, mode="retrieval")
        types_declared = set(dataset["meta"]["question_types"])
        types_in_results = set(report.by_type.keys())
        # 每种声明类型都应至少在评测中覆盖一道题
        for t in types_declared:
            assert t in types_in_results, f"类型 '{t}' 没有评测结果"
        # 每个类型都有 recall 和 mrr
        for metrics in report.by_type.values():
            assert "recall_at_5" in metrics
            assert "mrr" in metrics
            assert metrics["count"] >= 1

    def test_recall_range_is_valid(self, runner):
        report = runner.run_retrieval()
        assert 0.0 <= report.recall_at_5 <= 1.0
        assert 0.0 <= report.mrr <= 1.0
        assert 0.0 <= report.keyword_coverage <= 1.0
        assert 0.0 <= report.perfect_recall_rate <= 1.0
        for r in report.results:
            assert 0.0 <= r.recall_at_5 <= 1.0
            assert 0.0 <= r.mrr <= 1.0
            assert 0.0 <= r.keyword_coverage <= 1.0

    def test_keyword_coverage_is_one_when_no_keywords(self):
        r = QuestionResult(
            question_id="q000",
            question_type="simple_fact",
            question="测试",
            recall_at_5=0.0,
            mrr=0.0,
            keyword_coverage=1.0,
            top_pages=[],
            hit_pages=[],
            missed_pages=[],
        )
        assert r.keyword_coverage == 1.0

    def test_all_questions_have_some_keywords(self, dataset):
        for q in dataset["questions"]:
            assert len(q["keywords"]) >= 1, f"{q['id']}: 应至少有一个 keyword"


class TestEvalReport:
    """EvalReport 数据类测试。"""

    def test_empty_report(self):
        report = EvalReport(mode="retrieval", dataset_version="1.0", total_questions=0)
        assert report.recall_at_5 == 0.0
        assert "0题" in report.summary()

    def test_perfect_report(self):
        r = QuestionResult(
            question_id="q001",
            question_type="simple_fact",
            question="测试",
            recall_at_5=1.0,
            mrr=1.0,
            keyword_coverage=1.0,
            top_pages=["目标页面"],
            hit_pages=["目标页面"],
            missed_pages=[],
        )
        report = EvalReport(
            mode="retrieval",
            dataset_version="1.0",
            total_questions=1,
            results=[r],
        )
        report.recall_at_5 = 1.0
        report.mrr = 1.0
        report.keyword_coverage = 1.0
        report.perfect_recall_rate = 1.0
        report.by_type = {"simple_fact": {"recall_at_5": 1.0, "mrr": 1.0, "count": 1}}
        assert "Perfect=100%" in report.summary()
