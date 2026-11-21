"""Authenticated, versioned controls for the goal attached to a conversation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from crew.gateway.auth import account_from_request
from crew.state.session_goals import GoalAction, GoalSnapshot, GoalVersionConflict, SessionGoalStore


class GoalChange(BaseModel):
    action: GoalAction
    version: Annotated[str, Field(min_length=1, max_length=128)]
    objective: Annotated[str, Field(max_length=4000)] = ""


def _response(snapshot: GoalSnapshot | None) -> JSONResponse:
    if snapshot is None:
        return JSONResponse({"error": "当前账号下没有可设置目标的会话"}, status_code=404)
    return JSONResponse(asdict(snapshot))


def create_session_goals_router(goals: SessionGoalStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session/{session_id}/goal")
    def read_goal(request: Request, session_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        return _response(goals.read(owner, session_id))

    @router.post("/api/session/{session_id}/goal")
    def change_goal(request: Request, session_id: str, change: GoalChange) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        try:
            snapshot = goals.apply(owner, session_id, change.action, change.version, objective=change.objective)
        except GoalVersionConflict as exc:
            return JSONResponse({"error": str(exc), "code": "goal_version_conflict"}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "code": "invalid_goal"}, status_code=400)
        return _response(snapshot)

    return router
