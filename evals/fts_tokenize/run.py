"""对比三种中文分词策略在 FTS5 全文检索下的召回/精度差距。

只比较分词层，不涉及 LLM：对同一份页面语料分别用三种策略分词后写入
结构完全一致的 FTS5 索引（porter unicode61），跑同一组带标注的查询，
输出 recall@k / MRR / 误召回数。

三种策略：
- unigram：生产现状（crew.wiki.search.SQLiteFTS5SearchIndex._fts_tokenize），
  中文拆单字。
- bigram：无依赖的 unigram + 相邻双字 bigram，兼顾召回与相邻约束。
- jieba：词级分词（可选，未安装 jieba 时跳过该列）。

用法：
    python evals/fts_tokenize/run.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

TOP_K = 5


# ── 分词策略 ──────────────────────────────────────────────────────────────

def unigram_tokenize(text: str) -> str:
    """生产现状：中文单字分词。

    这是 crew/wiki/search.py 中 SQLiteFTS5SearchIndex._fts_tokenize 的无依赖拷贝，
    用于让评测脱离项目依赖独立运行。改动生产实现时需同步此处。
    """
    result: list[str] = []
    prev_cjk = False
    for ch in text:
        is_cjk = "一" <= ch <= "鿿"
        is_alnum = ch.isalnum()
        if is_cjk:
            if result and not prev_cjk:
                result.append(" ")
            result.append(ch)
            result.append(" ")
            prev_cjk = True
        elif is_alnum:
            if result and prev_cjk:
                result.append(" ")
            result.append(ch)
            prev_cjk = False
        else:
            result.append(" ")
            prev_cjk = False
    return " ".join("".join(result).split())


def bigram_tokenize(text: str) -> str:
    """unigram + 相邻双字 bigram，零依赖。"""
    out: list[str] = []
    cjk: list[str] = []
    word: list[str] = []

    def flush_word() -> None:
        if word:
            out.append("".join(word))
            word.clear()

    def flush_cjk() -> None:
        if cjk:
            out.extend(cjk)
            out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
            cjk.clear()

    for ch in text:
        if "一" <= ch <= "鿿":
            flush_word()
            cjk.append(ch)
        elif ch.isalnum():
            flush_cjk()
            word.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    return " ".join(out)


def _jieba_tokenize(text: str) -> str:
    import jieba

    return " ".join(jieba.cut(text))


def make_jieba_tokenize() -> tuple[callable | None, str | None]:
    """返回 (分词函数, 错误信息)。未安装 jieba 时返回 (None, 原因)。"""
    try:
        import jieba  # noqa: F401
    except ImportError as exc:
        return None, f"未安装 jieba（{exc}）"
    return _jieba_tokenize, None


# ── FTS5 检索骨架（三种策略共用，仅分词函数不同）──────────────────────────

def build_index(pages: list[dict], tokenize) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE pages_fts USING fts5("
        "page_id UNINDEXED, title, content, tokenize='porter unicode61')"
    )
    for page in pages:
        conn.execute(
            "INSERT INTO pages_fts (page_id, title, content) VALUES (?, ?, ?)",
            (page["id"], tokenize(page["title"]), tokenize(page["content"])),
        )
    return conn


def search(conn: sqlite3.Connection, tokenize, query: str) -> list[str]:
    fts_query = tokenize(query)
    if not fts_query.strip():
        return []
    rows = conn.execute(
        "SELECT page_id FROM pages_fts WHERE pages_fts MATCH ? "
        "ORDER BY bm25(pages_fts, 1, 10, 1) LIMIT ?",
        (fts_query, TOP_K),
    ).fetchall()
    return [row[0] for row in rows]


# ── 指标 ──────────────────────────────────────────────────────────────────

def score_case(relevant: set[str], retrieved: list[str]) -> dict:
    first_rank = next(
        (i + 1 for i, pid in enumerate(retrieved) if pid in relevant), None
    )
    return {
        "retrieved": retrieved,
        "recall@k": len(set(retrieved) & relevant) / len(relevant),
        "mrr": 1.0 / first_rank if first_rank else 0.0,
        "false_positives": [pid for pid in retrieved if pid not in relevant],
    }


def evaluate(strategy_name: str, tokenize, corpus: dict) -> dict:
    conn = build_index(corpus["pages"], tokenize)
    cases = []
    for case in corpus["queries"]:
        relevant = set(case["relevant"])
        result = score_case(relevant, search(conn, tokenize, case["query"]))
        cases.append(
            {
                "query": case["query"],
                "relevant": sorted(relevant),
                "note": case["note"],
                **result,
            }
        )
    conn.close()
    n = len(cases)
    return {
        "strategy": strategy_name,
        "mean_recall": sum(c["recall@k"] for c in cases) / n,
        "mean_mrr": sum(c["mrr"] for c in cases) / n,
        "total_false_positives": sum(len(c["false_positives"]) for c in cases),
        "cases": cases,
    }


# ── 输出 ──────────────────────────────────────────────────────────────────

def _mark(pid: str, relevant: set[str]) -> str:
    return pid if pid in relevant else f"{pid}✗"


def render(results: list[dict], jieba_err: str | None) -> None:
    names = [r["strategy"] for r in results]
    width = max(len(n) for n in names)

    print("\n== 聚合指标 ==")
    print(f"{'策略':<{width}}  recall@k   MRR    误召回总数")
    for r in results:
        print(
            f"{r['strategy']:<{width}}  {r['mean_recall']:.3f}    "
            f"{r['mean_mrr']:.3f}   {r['total_false_positives']}"
        )
    if jieba_err:
        print(f"  (jieba 列不可用：{jieba_err})")

    print("\n== 逐用例命中（✗ = 误召回，不在标注内）==")
    for case in results[0]["cases"]:
        print(f"\n查询「{case['query']}」  标注相关={case['relevant']}")
        print(f"    说明：{case['note']}")
        for r in results:
            c = next(x for x in r["cases"] if x["query"] == case["query"])
            got = " ".join(_mark(p, set(case["relevant"])) for p in c["retrieved"]) or "∅"
            print(
                f"    {r['strategy']:<{width}} recall={c['recall@k']:.2f} "
                f"mrr={c['mrr']:.2f} → {got}"
            )


def main() -> None:
    base = Path(__file__).parent
    corpus = json.loads((base / "corpus.json").read_text())
    jieba_tokenize, jieba_err = make_jieba_tokenize()

    strategies: list[tuple[str, callable]] = [
        ("unigram", unigram_tokenize),
        ("bigram", bigram_tokenize),
    ]
    if jieba_tokenize:
        strategies.append(("jieba", jieba_tokenize))

    results = [evaluate(name, fn, corpus) for name, fn in strategies]
    render(results, jieba_err)


if __name__ == "__main__":
    main()
