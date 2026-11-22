"""上下文压缩子包：三层渐进式压缩体系。

- L1 MicroCompact（microcompact.py）：纯规则清理旧工具结果，无 LLM。
- L2 增量摘要（pipeline.py）：复用进程内缓存的旧摘要，只摘要新增轮次。
- L3 全量摘要（summary.py）：调 LLM 生成结构化摘要。
- Post-Compact 文件恢复（post_compact.py）：压缩后把最近文件内容附加回上下文。

导出按需加载，避免仅使用 token 估算的启动路径拉入完整压缩流水线。
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "ContextCompactor",
    "SummaryStore",
    "SummaryState",
    "TokenMeasurement",
    "TokenMeter",
    "estimate_tokens",
    "estimate_prompt_tokens",
    "micro_compact",
    "CLEARED_PLACEHOLDER",
    "FILE_UNCHANGED_STUB",
    "SUMMARY_MARKER",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "TokenMeasurement": (".meter", "TokenMeasurement"),
    "TokenMeter": (".meter", "TokenMeter"),
    "CLEARED_PLACEHOLDER": (".microcompact", "CLEARED_PLACEHOLDER"),
    "FILE_UNCHANGED_STUB": (".microcompact", "FILE_UNCHANGED_STUB"),
    "micro_compact": (".microcompact", "micro_compact"),
    "ContextCompactor": (".pipeline", "ContextCompactor"),
    "SummaryState": (".store", "SummaryState"),
    "SummaryStore": (".store", "SummaryStore"),
    "SUMMARY_MARKER": (".summary", "SUMMARY_MARKER"),
    "estimate_prompt_tokens": (".tokens", "estimate_prompt_tokens"),
    "estimate_tokens": (".tokens", "estimate_tokens"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
