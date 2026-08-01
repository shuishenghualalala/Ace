"""Dynamic Kanban Worker 的系统提示与 handoff 上下文构造。"""

from __future__ import annotations

from typing import Any


def build_handoff_context(
    parent_results: list[dict[str, Any]],
    *,
    max_summary_chars: int = 400,
    max_artifacts: int = 5,
) -> str:
    """生成上游任务结果交接上下文，默认只保留关键结论与产物路径，避免大量 token。"""
    if not parent_results:
        return ""
    lines = ["# 上游任务结果摘要"]
    for r in parent_results:
        summary = str(r.get("result_summary", "") or "")
        if len(summary) > max_summary_chars:
            summary = summary[: max_summary_chars - 1].rstrip() + "…"
        artifacts = r.get("artifact_paths") or []
        if len(artifacts) > max_artifacts:
            artifacts = artifacts[:max_artifacts] + [f"…等 {len(artifacts) - max_artifacts} 个"]
        lines.append(f"\n- [{r.get('assignee', 'unknown')}] {r.get('title', '')}")
        if summary:
            lines.append(f"  结论：{summary}")
        if artifacts:
            lines.append(f"  产物：{', '.join(str(a) for a in artifacts)}")
    return "\n".join(lines)


def runtime_worker_system_prompt(
    role: str,
    task_prompt: str,
    *,
    handoff: str = "",
    valid_roles: list[str] | None = None,
    workflow_workdir: str | None = None,
    task_id: str | None = None,
    is_planning_role: bool = False,
) -> str:
    """WorkflowRuntime 使用的 worker 系统提示：稳定约定 + 动态任务上下文分离。

    稳定约定部分（团队身份、通用要求、工作目录）适合被 LLM provider 缓存；
    动态部分（当前任务、上游摘要、当前任务 ID）每轮变化。
    """
    role_hint = ""
    if valid_roles:
        joined = ", ".join(valid_roles)
        role_hint = (
            f"\n团队成员角色仅限：{joined}。"
            "新增任务时 assignee 必须从中选择，无效时自动归一化为第一个角色。"
        )

    planning_hint = ""
    if is_planning_role:
        planning_hint = (
            "\n你是规划型角色：产出为下一阶段执行计划。"
            "完成后请使用看板工具 `kanban_plan_next` 提交 JSON 计划，"
            "add_tasks 中 assignee 必须是团队成员之一，parent_task_ids 可用当前任务 ID。"
            "没有新增任务时返回空 add_tasks。"
        )

    stable = (
        f"你是 Dynamic Kanban 团队的成员「{role}」。{role_hint}\n"
        "要求：\n"
        "1. 专注完成当前任务，必要时调用工具。\n"
        "2. 完成后给出结构化结果摘要，并说明产出了哪些文件/工件。\n"
        "3. 多步骤任务请逐步调用工具；不要在没有调用工具时直接声称已完成。\n"
        "4. 如需新增下游任务或报告阻塞，请使用看板工具（kanban_add_task / kanban_report_blocker）。\n"
        "5. 不要执行与当前任务无关的操作。"
        f"{planning_hint}"
    )

    dynamic_parts: list[str] = ["\n# 当前任务", task_prompt]
    if workflow_workdir:
        dynamic_parts.append(f"\n本 workflow 工作目录：{workflow_workdir}\n所有中间文件、最终产出必须放在该目录下。")
    if task_id:
        dynamic_parts.append(f"当前任务 ID：`{task_id}`，使用看板工具时请传入此 ID。")
    if handoff:
        dynamic_parts.append(handoff)

    return stable + "\n" + "\n".join(dynamic_parts)
