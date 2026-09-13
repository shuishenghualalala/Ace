"""Lightweight Team turn routing.

The runtime authority is ``TeamTurnDecision``. The current user message is
always the turn goal; a persisted TeamSpec only supplies defaults for a
substantive task and never overrides a direct-chat fast path.
"""

from __future__ import annotations

from crew.team.team_spec import TeamSpec, TeamSpecInput, build_team_spec
from crew.team.turn_decision import TeamTurnDecision, direct_chat_decision, new_workflow_decision


_SIMPLE_CHAT_PHRASES = {
    "你好", "您好", "hello", "hi", "hey", "谢谢", "感谢", "辛苦", "在吗",
    "收到", "好的", "好", "ok", "确认", "继续", "谢谢你", "辛苦了",
}


def _is_simple_chat(goal: str) -> bool:
    normalized = " ".join(str(goal or "").strip().split()).strip(" ，,。.!！?？;；:：~～").lower()
    return normalized in _SIMPLE_CHAT_PHRASES


class TeamTurnRouter:
    """Fast local router backed by TeamSpec."""

    def route(self, goal: str, *, team_spec: TeamSpecInput = None) -> TeamTurnDecision:
        if _is_simple_chat(goal):
            decision = direct_chat_decision("simple_chat")
            return TeamTurnDecision(
                turn_kind=decision.turn_kind,
                execution_mode=decision.execution_mode,
                reason=decision.reason,
                diagnostics={
                    **decision.diagnostics,
                    "turn_source": "simple_chat",
                    "team_spec_default_ignored": team_spec is not None,
                },
            )
        if isinstance(team_spec, TeamSpec):
            spec_source: TeamSpecInput = {
                **team_spec.to_dict(),
                "goal": str(goal or "").strip(),
            }
        elif isinstance(team_spec, dict):
            spec_source = {
                **team_spec,
                "goal": str(goal or "").strip(),
            }
        else:
            spec_source = {"goal": goal}
        spec = build_team_spec(spec_source)
        reason = (
            spec.planner_notes[0]
            if spec.planner_notes
            else str(spec.task_profile.get("intent") or "mixed")
        )
        profile = spec.task_profile if isinstance(spec.task_profile, dict) else {}
        planning = spec.planning if isinstance(spec.planning, dict) else {}
        workflow_lanes = set(spec.team_requirements.get("workflow_lanes") or [])
        intent = str(profile.get("intent") or "mixed").strip().lower()
        complexity = str(profile.get("complexity") or "focused").strip().lower()
        missing_info = [item for item in list(planning.get("missing_info") or []) if str(item or "").strip()]
        diagnostics = {
            "source": "team_spec_profile",
            "team_spec": spec.to_dict(),
        }
        if missing_info and not str(spec.goal or "").strip():
            decision = direct_chat_decision(reason)
            return TeamTurnDecision(
                turn_kind=decision.turn_kind,
                execution_mode=decision.execution_mode,
                reason=decision.reason,
                diagnostics={**diagnostics, "turn_source": "missing_info_empty_goal"},
            )
        if intent == "chat" and complexity == "simple":
            decision = direct_chat_decision(reason)
            return TeamTurnDecision(
                turn_kind=decision.turn_kind,
                execution_mode=decision.execution_mode,
                reason=decision.reason,
                diagnostics={**diagnostics, "turn_source": "simple_chat"},
            )
        mode = "fast" if intent == "question" and not workflow_lanes else "standard"
        decision = new_workflow_decision(mode, reason, source="team_spec_profile")
        return TeamTurnDecision(
            turn_kind=decision.turn_kind,
            execution_mode=decision.execution_mode,
            reason=decision.reason,
            diagnostics={**diagnostics, "turn_source": "task_profile"},
        )
