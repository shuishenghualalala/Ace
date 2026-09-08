"""对比 FTS-only 与 FTS+语义 的中文语义召回评测。

语料刻意让查询与其相关页面零关键词重叠，词法检索（FTS/关键词）无法召回，
只有语义向量通道能命中。``--smoke`` 用预定义同义词桶验证接线（非质量评测）；
真实评测用 ``--provider-factory module:function``，函数返回一个已配置的
``EmbeddingProvider``（同步接口，如 OpenAI 或本地 fastembed）。

用法：
    python evals/semantic_recall/run.py --smoke
    python evals/semantic_recall/run.py --provider-factory my_eval_provider:create
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crew.wiki.embedding import EmbeddingProvider
from crew.wiki.query import WikiQuerier
from crew.wiki.schemas import WikiPage
from crew.wiki.store import FileSystemWikiStore

TOP_K = 5


class _SmokeEmbedding(EmbeddingProvider):
    """冒烟：按预定义同义词桶映射 one-hot（模拟语义相似，确定性）。"""

    model = "smoke:synonym"
    dim = 8
    _GROUPS = (
        ("修", "bug", "故障", "排查"),
        ("报错", "错误", "异常", "咋整"),
        ("回滚", "回退", "撤销", "改动"),
        ("内存", "资源", "磁盘", "不够"),
    )

    def _topic(self, text: str) -> int:
        for index, group in enumerate(self._GROUPS):
            if any(word in text for word in group):
                return index
        return len(self._GROUPS)

    def _vec(self, topic: int) -> list[float]:
        vector = [0.0] * self.dim
        vector[topic % self.dim] = 1.0
        return vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(self._topic(text)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(self._topic(text))


def _metrics(relevant: set[str], pages: list[dict]) -> tuple[float, float]:
    got = [page["id"] for page in pages]
    first = next((i + 1 for i, pid in enumerate(got) if pid in relevant), None)
    recall = len(set(got) & relevant) / len(relevant)
    mrr = 1.0 / first if first else 0.0
    return recall, mrr


def evaluate(provider: EmbeddingProvider | None, corpus: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="crew-semantic-eval-") as tmp:
        store = FileSystemWikiStore(base_dir=Path(tmp), embedding_provider=provider)
        try:
            for data in corpus["pages"]:
                store.save_page(
                    WikiPage(
                        id=data["id"],
                        page_type="topic",
                        title=data["title"],
                        content=data["content"],
                        file_path="",
                    )
                )
            querier = WikiQuerier(store)
            rows = []
            for case in corpus["queries"]:
                relevant = set(case["relevant"])
                result = querier.search(case["query"], top_k=TOP_K)
                recall, mrr = _metrics(relevant, result["pages"])
                rows.append(
                    {
                        "query": case["query"],
                        "relevant": sorted(relevant),
                        "recall": recall,
                        "mrr": mrr,
                    }
                )
        finally:
            store.close()
    return {
        "mean_recall": sum(row["recall"] for row in rows) / len(rows),
        "mean_mrr": sum(row["mrr"] for row in rows) / len(rows),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--provider-factory")
    args = parser.parse_args()

    corpus = json.loads((Path(__file__).parent / "corpus.json").read_text())

    if args.smoke:
        provider: EmbeddingProvider = _SmokeEmbedding()
    elif args.provider_factory:
        module, name = args.provider_factory.split(":", 1)
        provider = getattr(importlib.import_module(module), name)()
    else:
        parser.error("需提供 --smoke 或 --provider-factory")

    fts_only = evaluate(None, corpus)
    fts_vector = evaluate(provider, corpus)

    print("== 中文语义召回对比 ==")
    print(f"{'策略':<12} recall@k   MRR")
    print(f"{'fts-only':<12} {fts_only['mean_recall']:.3f}    {fts_only['mean_mrr']:.3f}")
    print(f"{'fts+vector':<12} {fts_vector['mean_recall']:.3f}    {fts_vector['mean_mrr']:.3f}")
    print("\n逐用例：")
    for fts_row, vec_row in zip(fts_only["rows"], fts_vector["rows"]):
        print(
            f"  {fts_row['query']!r} 相关={fts_row['relevant']}  "
            f"fts_recall={fts_row['recall']:.2f}  vec_recall={vec_row['recall']:.2f}"
        )


if __name__ == "__main__":
    main()
