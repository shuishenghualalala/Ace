"""Cron-owned model context contributions."""

from __future__ import annotations

from crew.core.envelope import Envelope
from crew.features.context import ContextContribution


async def contribute_cron_trigger_reminder(
    envelope: Envelope,
) -> ContextContribution | None:
    """Describe an active Cron fire without coupling Agent Runtime to Cron."""
    if envelope.channel != "cron":
        return None
    job_name = str(envelope.params.get("cron_job_name") or "").strip()
    name_part = f"「{job_name}」" if job_name else ""
    return ContextContribution(
        prompt_parts=(
            f"【定时任务触发】你之前创建的定时任务{name_part}现在已到点执行。"
            "请直接完成下面这条任务内容、并输出要发送给用户的话——"
            "不要询问是否到时间、不要反问是不是用户提前来问、也不要解释这是定时任务。",
        )
    )
