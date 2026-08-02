"""Wiki 评测执行器。

提供两种模式：

- **retrieval**（快速，确定性）：仅评估检索质量，不调用 LLM。
  适用场景：CI 回归、Prompt 变更快速验证。
- **full**（需真实 LLM）：额外评估答案正确性（LLM-as-judge）。
  适用场景：发版前完整评测。

使用方式::

    import pytest
    from tests.wiki.eval.eval_runner import EvalRunner, EvalMode

    @pytest.fixture
    def runner(tmp_path):
        return EvalRunner.from_tutorial_seed(tmp_path)

    def test_eval_retrieval(runner):
        report = runner.run_retrieval()
        assert report.recall_at_5 >= 0.50
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.wiki.query import WikiQuerier
from crew.wiki.seed import TUTORIAL_KB_ID, ensure_tutorial_kb
from crew.wiki.store import FileSystemWikiStore


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class QuestionResult:
    """单题评测结果。"""

    question_id: str
    question_type: str
    question: str
    recall_at_5: float  # top-5 召回率（命中 expected_pages 的比例）
    mrr: float  # Mean Reciprocal Rank（首个命中页面的排名倒数）
    keyword_coverage: float  # 期望关键词的覆盖率
    top_pages: list[str] = field(default_factory=list)
    hit_pages: list[str] = field(default_factory=list)
    missed_pages: list[str] = field(default_factory=list)

    @property
    def is_perfect_recall(self) -> bool:
        return self.recall_at_5 >= 1.0


@dataclass
class EvalReport:
    """完整评测报告。"""

    mode: str  # "retrieval" | "full"
    dataset_version: str
    total_questions: int
    results: list[QuestionResult] = field(default_factory=list)

    # 聚合指标
    recall_at_5: float = 0.0
    mrr: float = 0.0
    keyword_coverage: float = 0.0
    perfect_recall_rate: float = 0.0

    # 按类型拆分
    by_type: dict[str, dict[str, float]] = field(default_factory=dict)

    def summary(self) -> str:
        """单行摘要。"""
        return (
            f"[{self.mode}] {self.total_questions}题 | "
            f"Recall@5={self.recall_at_5:.0%} | "
            f"MRR={self.mrr:.2f} | "
            f"Keyword={self.keyword_coverage:.0%} | "
            f"Perfect={self.perfect_recall_rate:.0%}"
        )

    def detailed_report(self) -> str:
        """多行详细报告。"""
        lines = [
            f"========== Wiki Eval 评测报告 ==========",
            f"模式: {self.mode}",
            f"数据集版本: {self.dataset_version}",
            f"题目总数: {self.total_questions}",
            f"",
            f"--- 聚合指标 ---",
            f"Recall@5:        {self.recall_at_5:.1%}",
            f"MRR:             {self.mrr:.2f}",
            f"Keyword Coverage:{self.keyword_coverage:.1%}",
            f"Perfect Recall:  {self.perfect_recall_rate:.1%}",
            f"",
            f"--- 按类型拆分 ---",
        ]
        for qtype, metrics in sorted(self.by_type.items()):
            lines.append(
                f"  {qtype:20s}  Recall@5={metrics['recall_at_5']:.0%}  "
                f"MRR={metrics['mrr']:.2f}  n={int(metrics['count'])}"
            )
        lines.append("")
        lines.append("--- 逐题详情 ---")
        for r in self.results:
            status = "✓" if r.is_perfect_recall else "✗"
            lines.append(
                f"  {status} [{r.question_id}] ({r.question_type}) {r.question[:50]}..."
            )
            lines.append(f"     Recall@5={r.recall_at_5:.0%}  MRR={r.mrr:.2f}")
            if r.hit_pages:
                lines.append(f"     命中: {', '.join(r.hit_pages)}")
            if r.missed_pages:
                lines.append(f"     遗漏: {', '.join(r.missed_pages)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 评测执行器
# --------------------------------------------------------------------------- #


class EvalRunner:
    """Wiki 检索与问答评测器。

    初始化需要 WikiStore（已加载评测数据）和可选的 WikiQuerier。
    """

    def __init__(self, store: FileSystemWikiStore, kb_id: str = TUTORIAL_KB_ID) -> None:
        self.store = store
        self.kb_id = kb_id
        self.querier = WikiQuerier(store)

    @classmethod
    def from_tutorial_seed(cls, base_dir: Path | str) -> "EvalRunner":
        """从 tutorial seed 创建评测器（最常用入口）。"""
        store = FileSystemWikiStore(base_dir=Path(base_dir))
        ensure_tutorial_kb(store)
        return cls(store, kb_id=TUTORIAL_KB_ID)

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def run_retrieval(self, dataset: dict | None = None) -> EvalReport:
        """执行检索质量评测（不调用 LLM，确定性）。"""
        if dataset is None:
            dataset = load_eval_dataset()
        return self._evaluate(dataset, mode="retrieval")

    # ------------------------------------------------------------------ #
    # 核心评测逻辑
    # ------------------------------------------------------------------ #

    def _evaluate(self, dataset: dict, *, mode: str) -> EvalReport:
        questions = dataset["questions"]
        results: list[QuestionResult] = []
        for q in questions:
            result = self._eval_single_retrieval(q)
            results.append(result)

        report = self._build_report(dataset, results, mode)
        return report

    def _eval_single_retrieval(self, q: dict) -> QuestionResult:
        """单题检索评测：计算 Recall@k, MRR, Keyword Coverage。"""
        search_result = self.querier.search(
            q["question"],
            kb_id=self.kb_id,
            top_k=5,
            expand_neighbors=True,
            include_context=True,
        )
        retrieved_pages = search_result.get("pages", [])
        retrieved_titles = [p["title"] for p in retrieved_pages]
        expected = q.get("expected_pages", [])

        # Recall@5
        hit = [title for title in expected if title in retrieved_titles]
        recall_at_5 = len(hit) / len(expected) if expected else 1.0

        # MRR
        mrr = 0.0
        for title in expected:
            try:
                rank = retrieved_titles.index(title) + 1
                mrr = max(mrr, 1.0 / rank)
            except ValueError:
                pass

        # Keyword Coverage
        keywords = q.get("keywords", [])
        if keywords:
            context = search_result.get("context", "")
            # 同时检查 context 和检索到的页面正文
            searchable = context + " " + " ".join(
                p.get("content", "") for p in retrieved_pages
            )
            searchable_folded = searchable.casefold()
            matched_keywords = sum(
                1 for kw in keywords if kw.casefold() in searchable_folded
            )
            keyword_coverage = matched_keywords / len(keywords)
        else:
            keyword_coverage = 1.0

        return QuestionResult(
            question_id=q["id"],
            question_type=q["type"],
            question=q["question"],
            recall_at_5=recall_at_5,
            mrr=mrr,
            keyword_coverage=keyword_coverage,
            top_pages=retrieved_titles,
            hit_pages=hit,
            missed_pages=[t for t in expected if t not in retrieved_titles],
        )

    def _build_report(
        self,
        dataset: dict,
        results: list[QuestionResult],
        mode: str,
    ) -> EvalReport:
        """聚合结果并生成报告。"""
        n = len(results)
        meta = dataset.get("meta", {})
        report = EvalReport(
            mode=mode,
            dataset_version=meta.get("version", "unknown"),
            total_questions=n,
            results=results,
        )
        if n == 0:
            return report

        report.recall_at_5 = sum(r.recall_at_5 for r in results) / n
        report.mrr = sum(r.mrr for r in results) / n
        report.keyword_coverage = sum(r.keyword_coverage for r in results) / n
        report.perfect_recall_rate = sum(1 for r in results if r.is_perfect_recall) / n

        # 按类型拆分
        by_type: dict[str, dict[str, Any]] = {}
        for r in results:
            t = r.question_type
            if t not in by_type:
                by_type[t] = {"recall_at_5": 0.0, "mrr": 0.0, "count": 0}
            by_type[t]["recall_at_5"] += r.recall_at_5
            by_type[t]["mrr"] += r.mrr
            by_type[t]["count"] += 1
        for metrics in by_type.values():
            c = metrics["count"]
            metrics["recall_at_5"] /= c
            metrics["mrr"] /= c
        report.by_type = by_type

        return report


# --------------------------------------------------------------------------- #
# 数据集加载
# --------------------------------------------------------------------------- #

_EVAL_DATASET_PATH = Path(__file__).resolve().parent / "eval_dataset.json"


def load_eval_dataset(path: Path | None = None) -> dict:
    """加载评测数据集。"""
    target = path or _EVAL_DATASET_PATH
    if not target.is_file():
        raise FileNotFoundError(f"评测数据集不存在: {target}")
    with open(target, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# LLM-as-Judge（full 模式，需要真实 LLM Provider）
# --------------------------------------------------------------------------- #

JUDGE_SYSTEM_PROMPT = """你是一个 Wiki 问答质量评委。根据标准答案和检索到的 Wiki 页面内容，评判 AI 回答的正确性。

评分标准（0-3）：
- 0: 完全不相关或严重错误
- 1: 部分相关但有关键事实错误
- 2: 基本正确但有遗漏或不够精确
- 3: 完全正确且覆盖全面，证据充分

返回 JSON: {"score": <int>, "reason": "<一句话原因>"}"""


def judge_answer(
    question: str,
    answer: str,
    expected_answer: str,
    wiki_context: str,
) -> dict:
    """用 LLM-as-judge 评判答案正确性。

    注意：此函数需要真实 LLM Provider，仅供 full 模式使用，不在 CI 中运行。
    """
    judge_prompt = (
        f"## 问题\n{question}\n\n"
        f"## 标准答案\n{expected_answer}\n\n"
        f"## Wiki 检索到的上下文\n{wiki_context}\n\n"
        f"## 待评判的回答\n{answer}\n\n"
        f"请根据评分标准评判这个回答的分数（0-3），返回 JSON。"
    )
    # 实际调用留给集成层 —— 使用者注入 provider
    return {"score": -1, "reason": "judge_answer 需要注入 LLM provider，请参见 EvalRunner.run_full()"}
