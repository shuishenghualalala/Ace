"""Wiki LLM Prompt 调优评估框架。

分层评估体系：

Level 1 — 检索质量：IDF Coverage + Recall@k
Level 2 — 端到端问答：Answer Accuracy + Citation Accuracy
Level 3 — Prompt A/B 对比：实体提取质量 + 解析成功率 + Token 消耗

使用方式::

    # 快速跑完整 eval（基于 tutorial seed KB）
    python -m pytest tests/wiki/eval/ -v

    # 编程方式
    from tests.wiki.eval import EvalDataset, EvalRunner

    dataset = EvalDataset.load()
    runner = EvalRunner.from_tutorial_seed(tmp_path)
    report = await runner.run(dataset)
    print(report.summary())

要求：
- eval_dataset.json 新增/修改问题时务必同步更新 ``last_updated`` 时间戳。
- 增加覆盖类型（如 hop_2、risk_identification）时同步更新 ``question_types`` 列表。
"""

__version__ = "0.1.0"
__all__ = ["EvalDataset", "EvalRunner", "EvalReport"]
