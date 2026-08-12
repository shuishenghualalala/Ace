"""Lightweight Team intake routing.

TeamPlan is an execution mode, not the default chat mode. The router decides
whether the leader can answer directly, should ask for more information, or
should enter the TeamPlan runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from crew.team.team_spec import build_team_spec


TeamIntentAction = Literal["direct_leader", "ask_followup", "team_plan"]


@dataclass(frozen=True)
class TeamIntentDecision:
    action: TeamIntentAction
    reason: str = ""
    spec: dict | None = None

    @property
    def direct_leader(self) -> bool:
        return self.action == "direct_leader"

    @property
    def requires_team_plan(self) -> bool:
        return self.action == "team_plan"


class RuleBasedTeamIntentRouter:
    """Fast local router backed by TeamSpec.

    The class name stays stable for callers, but the decision now comes from a
    shared TeamSpec profile so team suggestion and task intake do not drift.
    """

    def route(self, goal: str) -> TeamIntentDecision:
        spec = build_team_spec(goal)
        profile = spec.execution_profile if isinstance(spec.execution_profile, dict) else {}
        intent = str(profile.get("intent") or "mixed").strip().lower()
        complexity = str(profile.get("complexity") or "focused").strip().lower()
        if not str(spec.goal or "").strip():
            action: TeamIntentAction = "ask_followup"
        elif intent == "chat" and complexity == "simple":
            action = "direct_leader"
        else:
            action = "team_plan"
        reason = (
            spec.planner_notes[0]
            if spec.planner_notes
            else "TeamSpec 已规范化显式任务字段，交由 Team 规划阶段理解目标。"
        )
        return TeamIntentDecision(action, reason, spec.to_dict())
