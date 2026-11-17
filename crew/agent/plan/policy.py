"""Model-facing and execution-time tool scope for Plan mode."""

from collections.abc import Iterable


_PLAN_TOOLS = {
    "file_read": "Read a workspace file or the existing plan when revising it.",
    "glob": "Find workspace paths by glob pattern.",
    "grep": "Find matching content in workspace files.",
    "file_write": "Write only the current session's plan file.",
    "exit_plan_mode": "Submit the saved plan for the user's approval.",
    "todo": "Update the task checklist.",
    "ask_followup_question": "Clarify requirements or constraints with the user.",
}

PLAN_MODE_TOOLS = tuple(_PLAN_TOOLS)


def plan_tool_reminder(visible_tools: Iterable[str]) -> str:
    """Document this request's registered and authorized Plan tools."""
    visible = set(visible_tools)
    lines = ["## Plan tools available in this turn", "Use only the available tools listed here."]
    lines.extend(f"- `{name}` — {description}" for name, description in _PLAN_TOOLS.items() if name in visible)
    if not visible:
        lines.append("No tools are available. Reply in text within these limits.")
    elif not {"file_write", "exit_plan_mode"} <= visible:
        lines.append("If the task requires a plan for approval, explain that the current tool scope cannot submit it.")
    return "\n".join(lines)
