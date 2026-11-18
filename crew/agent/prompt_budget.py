"""Budgets for model-visible instruction, catalog, and memory sources."""

from __future__ import annotations

from crew.agent.compact.tokens import estimate_text_tokens


def source_token_budgets(context_window: int | None) -> dict[str, int]:
    """Reserve independent space for rules so an earlier source cannot crowd them out."""
    total = 8_192 if context_window is None else min(12_000, max(0, context_window // 8))
    # Leave room for separators between the independently rendered sources.
    total = max(0, total - 24)
    shares = {
        "owner_rules": 14,
        "workspace_rules": 9,
        "project_rules": 24,
        "skills": 23,
        "optional_skills": 5,
        "memory": 10,
        "user": 5,
        "recall": 10,
    }
    return {source: total * share // 100 for source, share in shares.items()}


def render_prompt_source(
    heading: str,
    content: str,
    max_tokens: int,
    notice: str,
    *,
    truncated: bool = False,
) -> str:
    """Retain provenance and a truncation notice inside the source's budget."""
    char_cap = max_tokens * 3 + 12
    if len(content) > char_cap:
        content = content[:char_cap]
        truncated = True
    content = content.strip()
    if not content and not truncated:
        return ""
    prefix = f"{heading}\n\n" if heading else ""
    if not truncated and estimate_text_tokens(prefix + content) <= max_tokens:
        return prefix + content
    suffix = f"\n\n（{notice}）"
    # Explanatory text must leave room for actual rules on a small window.
    if estimate_text_tokens(prefix + content[:12] + suffix) > max_tokens:
        suffix = "\n\n（已截断。）"
        if estimate_text_tokens(prefix + suffix) > max_tokens:
            return ""
    low, high = 0, len(content)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_text_tokens(prefix + content[:mid] + suffix) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return (prefix + content[:low].rstrip() + suffix).strip()
