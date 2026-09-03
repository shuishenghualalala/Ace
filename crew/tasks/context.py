"""任务运行时的模型上下文贡献：后台任务恢复回合的完成通知 reminder。

恢复回合由 CrewApp._resume_completed_task 构造（envelope.params 携带
task_notifications），本 contributor 只负责把结果拼成模型可见的 reminder，
不持有任何队列。
"""

from __future__ import annotations

from crew.core.envelope import Envelope
from crew.features.context import ContextContribution


def format_task_notifications(pending: object) -> str:
    """把已结束的后台任务格式化为可注入上下文的 system-reminder 块。"""
    if not isinstance(pending, list) or not pending:
        return ""
    lines = [
        "# 后台任务完成通知",
        "以下后台任务已结束。请读取结果，判断是否继续原任务、修复失败或向用户汇报。",
    ]
    for task in pending:
        if not isinstance(task, dict):
            continue
        lines.append(
            "\n## "
            f"{task.get('task_id', '')} kind={task.get('kind', '')} status={task.get('status', '')}\n"
            f"{task.get('result') or task.get('error') or '(无结果)'}"
        )
    return "\n".join(lines)


async def contribute_task_notifications(
    envelope: Envelope,
) -> ContextContribution | None:
    """恢复回合携带 task_notifications 时，产出对应的完成通知 reminder。"""
    block = format_task_notifications(envelope.params.get("task_notifications"))
    if not block:
        return None
    return ContextContribution(prompt_parts=(block,))
