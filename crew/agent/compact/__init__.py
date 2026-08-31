"""上下文压缩子包：三层渐进式压缩体系。

- L1 MicroCompact（microcompact.py）：纯规则清理旧工具结果，无 LLM。
- L2 增量摘要（pipeline.py）：复用进程内缓存的旧摘要，只摘要新增轮次。
- L3 全量摘要（summary.py）：调 LLM 生成结构化摘要。
- Post-Compact 文件恢复（post_compact.py）：压缩后把最近文件内容附加回上下文。
"""

from __future__ import annotations

from crew.agent.compact.microcompact import (
    CLEARED_PLACEHOLDER,
    FILE_UNCHANGED_STUB,
    micro_compact,
)
from crew.agent.compact.pipeline import ContextCompactor
from crew.agent.compact.store import SummaryState, SummaryStore
from crew.agent.compact.summary import SUMMARY_MARKER
from crew.agent.compact.tokens import estimate_prompt_tokens, estimate_tokens

__all__ = [
    "ContextCompactor",
    "SummaryStore",
    "SummaryState",
    "estimate_tokens",
    "estimate_prompt_tokens",
    "micro_compact",
    "CLEARED_PLACEHOLDER",
    "FILE_UNCHANGED_STUB",
    "SUMMARY_MARKER",
]
