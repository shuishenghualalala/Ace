"""HotpotQA → Wiki Eval 适配器。

将内嵌的 HotpotQA 数据集中的 Wikipedia 文档段落实例化为 Wiki 页面，
然后用现有的 ``EvalRunner`` 执行多跳检索评测。

使用方式::

    from tests.wiki.eval.hotpotqa_adapter import HotpotQAAdapter

    adapter = HotpotQAAdapter.from_embedded(tmp_path)
    report = adapter.run_eval()
    print(report.summary())

数据集：
- 15 道精选 bridge/comparison 类型多跳推理题
- 所有文档段落内嵌在 ``eval_dataset_hotpotqa.json`` 中（无需网络）
- 每道题需跨 2 个页面检索才能得出答案
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crew.wiki.schemas import WikiPage
from crew.wiki.store import FileSystemWikiStore

# 独立的 kb_id 避免与 tutorial seed 冲突
HOTPOTQA_KB_ID = "hotpotqa_eval"
HOTPOTQA_KB_NAME = "HotpotQA Multi-hop Eval"

_EVAL_DATASET_PATH = Path(__file__).resolve().parent / "eval_dataset_hotpotqa.json"


def _load_dataset() -> dict[str, Any]:
    with open(_EVAL_DATASET_PATH, encoding="utf-8") as f:
        return json.load(f)


class HotpotQAAdapter:
    """将 HotpotQA 数据适配为 Wiki 评测。"""

    def __init__(self, store: FileSystemWikiStore) -> None:
        self.store = store
        self.dataset = _load_dataset()
        self._setup_kb()

    @classmethod
    def from_embedded(cls, base_dir: Path | str) -> "HotpotQAAdapter":
        """从内嵌数据创建适配器（无需网络）。"""
        store = FileSystemWikiStore(base_dir=Path(base_dir))
        return cls(store)

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def ingest_all_documents(self) -> int:
        """将所有 HotpotQA 上下文文档实例化为 Wiki 页面。返回创建的页面数。"""
        docs = self.dataset.get("_wiki_documents", {})
        created = 0
        for title, doc in docs.items():
            if not isinstance(doc, dict):
                continue  # 跳过元数据字段（如 _description）
            page = WikiPage(
                id="",
                page_type=doc.get("page_type", "entity"),
                title=title,
                content=doc.get("content", ""),
                file_path="",
                aliases=doc.get("aliases", []),
                tags=doc.get("tags", []),
            )
            self.store.save_page(page, kb_id=HOTPOTQA_KB_ID)
            created += 1
        return created

    def run_eval(self) -> "EvalReport":
        """在 HotpotQA 数据集上执行多跳检索评测。"""
        from tests.wiki.eval.eval_runner import EvalRunner

        self.ingest_all_documents()
        runner = EvalRunner(self.store, kb_id=HOTPOTQA_KB_ID)
        report = runner._evaluate(self.dataset, mode="retrieval")
        return report

    def get_question_breakdown(self) -> dict[str, int]:
        """按类型统计题目数量。"""
        from collections import Counter
        return dict(Counter(q["type"] for q in self.dataset["questions"]))

    def _setup_kb(self) -> None:
        """确保 HotpotQA 专用 KB 存在。"""
        existing = {kb.id for kb in self.store.list_kbs()}
        if HOTPOTQA_KB_ID not in existing:
            self.store.create_kb(HOTPOTQA_KB_ID, HOTPOTQA_KB_NAME)
