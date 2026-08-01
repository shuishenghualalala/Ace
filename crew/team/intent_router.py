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
        reason = spec.reasons[0] if spec.reasons else spec.task_kind
        return TeamIntentDecision(spec.route, reason, spec.to_dict())
