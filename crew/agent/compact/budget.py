"""Output reservation and final request admission for a known context window."""

from __future__ import annotations

from dataclasses import dataclass

from crew.core.errors import CrewErrorKind, ProviderError

_DEFAULT_OUTPUT_RESERVE = 4096
_MAX_OUTPUT_RESERVE = 21_000


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


@dataclass(frozen=True)
class RequestBudget:
    prompt_tokens: int
    context_window: int | None
    output_reserve: int
    max_output_tokens: int | None

    @property
    def excess_tokens(self) -> int:
        if self.context_window is None:
            return 0
        return max(0, self.prompt_tokens + self.output_reserve - self.context_window)

    @property
    def over_budget(self) -> bool:
        return self.excess_tokens > 0


def assess_request_budget(
    prompt_tokens: int,
    *,
    context_window: int | None,
    requested_output_tokens: int | None,
    provider_default_output_tokens: int | None,
    accepts_output_override: bool = True,
) -> RequestBudget:
    window = _positive_int(context_window)
    wire_limit = _positive_int(requested_output_tokens) if accepts_output_override else None
    output_limit = wire_limit or _positive_int(provider_default_output_tokens)
    if window is None:
        return RequestBudget(prompt_tokens, None, 0, wire_limit)

    reserve = min(output_limit or _DEFAULT_OUTPUT_RESERVE, _MAX_OUTPUT_RESERVE, max(1, window // 4))
    if not accepts_output_override and output_limit is not None:
        # A fixed provider default has to fit in its entirety.
        reserve = output_limit
    remaining = window - prompt_tokens
    if accepts_output_override and output_limit is not None and reserve <= remaining < output_limit:
        wire_limit = remaining
    return RequestBudget(prompt_tokens, window, reserve, wire_limit)


class RequestBudgetExceeded(ProviderError):
    """A local estimate rejected a provider request before its transport opened."""

    def __init__(self, budget: RequestBudget) -> None:
        self.budget = budget
        super().__init__(
            f"当前模型上下文不足（输入估算 {budget.prompt_tokens}，预留输出 {budget.output_reserve}，"
            f"上下文上限 {budget.context_window}）。请缩短输入或选择上下文窗口更大的模型。",
            kind=CrewErrorKind.CONTEXT_WINDOW_EXCEEDED,
            retryable=False,
        )
