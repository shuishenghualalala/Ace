"""Per-agent iteration budget — thread-safe consume/refund counter.

``BuiltinExecutor`` 每轮 ``consume()`` 一次预算，跑满后返回「最大迭代」final；
``execute_code`` 一类「编程式工具调用」的迭代用 ``refund()`` 退还，不计入预算。
"""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent.

    Each agent (parent or subagent) gets its own ``IterationBudget``.
    Parent and child agents each receive an independent budget. A non-positive
    limit means unlimited iterations; positive limits cap that agent only.

    ``execute_code`` (programmatic tool calling) iterations are refunded via
    :meth:`refund` so they don't eat into the budget.
    """

    def __init__(self, max_total: int):
        # max_total == 0 表示无限：
        # 靠 auto-compact 管上下文增长 + guardrail 防失控 + 用户停止，不用迭代数硬卡。
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    @property
    def _unlimited(self) -> bool:
        return self.max_total <= 0

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._unlimited:
                self._used += 1
                return True
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            if self._unlimited:
                return 1 << 30  # 充分大，使 grace 逻辑永不误触
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
