"""Lightweight Team turn routing.

The runtime authority is ``TeamTurnDecision``.  TeamSpec only supplies the task
profile; execution mode is inferred here from that profile.
"""

from __future__ import annotations

from crew.team.team_spec import build_team_spec
from crew.team.turn_decision import TeamTurnDecision, direct_chat_decision, new_workflow_decision


class TeamTurnRouter:
    """Fast local router backed by TeamSpec."""

    def route(self, goal: str) -> TeamTurnDecision:
        spec = build_team_spec(goal)
        reason = (
            spec.planner_notes[0]
            if spec.planner_notes
            else str(spec.execution_profile.get("intent") or "mixed")
        )
        profile = spec.execution_profile if isinstance(spec.execution_profile, dict) else {}
        planning = spec.planning if isinstance(spec.planning, dict) else {}
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
        needs_build = bool(profile.get("needs_build"))
        needs_verification = bool(profile.get("needs_verification"))
        needs_docs = bool(profile.get("needs_docs"))
        mode = "fast" if intent == "question" and not needs_build and not needs_verification and not needs_docs else "standard"
        decision = new_workflow_decision(mode, reason, source="team_spec_profile")
        return TeamTurnDecision(
            turn_kind=decision.turn_kind,
            execution_mode=decision.execution_mode,
            reason=decision.reason,
            diagnostics={**diagnostics, "turn_source": "task_profile"},
        )
