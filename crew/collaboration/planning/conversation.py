"""Bridge a team conversation to the canonical persisted planning service.

The caller owns dispatch. This module only creates and resolves planning
records, then projects the committed plan; it never starts an agent.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from crew.collaboration.planning.handoff import workflow_from_preview
from crew.collaboration.planning.plan import PlanningError
from crew.collaboration.planning.service import PlanningService

DecisionHandler = Callable[[dict[str, Any]], Awaitable[str]]


async def plan_conversation(
    service: PlanningService,
    *,
    owner: str,
    request_id: str,
    session_id: str,
    goal: str,
    team: dict[str, Any],
    provider: Any,
    inputs: list[dict[str, str]],
    ask: DecisionHandler,
    current_team: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Create one task and resolve persisted decisions before handing it off."""
    preview = await service.create(owner=owner, request_id=request_id, session_id=session_id,
                                   goal=goal, team=team, provider=provider, inputs=inputs)
    for _ in range(12):
        if preview["state"] == "failed":
            raise PlanningError(preview.get("error") or "规划失败")
        if preview["state"] in {"planned", "handoff_committed"}:
            live_team = current_team()
            return workflow_from_preview({**preview, "state": "planned"}, team_id=str(team.get("id") or ""), session_id=session_id,
                                         goal=goal, team_updated_at=live_team.get("updated_at"))
        if preview["state"] != "waiting_user" or not preview.get("plan"):
            raise PlanningError("规划正在处理中，请稍后重试")
        decision = next((d for d in preview["plan"].get("decisions", []) if d.get("state") == "open"), None)
        if decision is None:
            raise PlanningError("规划处于等待状态，但没有可回答的问题")
        answer = str(await ask(decision) or "").strip()
        if not answer:
            raise PlanningError("未收到规划问题的回答，本次任务已暂停")
        task_id = preview["task"]["task_id"]
        if decision["kind"] == "clarification":
            preview = await service.answer_clarification(owner=owner, task_id=task_id,
                                                         decision_id=decision["decision_id"],
                                                         answer=answer, provider=provider)
        elif decision["kind"] == "assignment":
            preview = service.store.answer(owner, task_id, decision["decision_id"], answer)
        else:
            raise PlanningError("未知的规划问题类型")
    raise PlanningError("规划问题过多，本次任务已暂停")
