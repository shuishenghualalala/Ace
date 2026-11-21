"""Authenticated, versioned controls for a conversation's goal and explicit step."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from crew.gateway.auth import account_from_request
from crew.gateway.goal_steps import GoalSteps
from crew.state.session_goals import GoalAction, GoalSnapshot, GoalStepConflict, GoalVersionConflict, SessionGoalStore


class GoalChange(BaseModel):
    action: GoalAction | Literal["advance"]
    version: Annotated[str, Field(min_length=1, max_length=128)]
    objective: Annotated[str, Field(max_length=4000)] = ""


def _response(snapshot: GoalSnapshot | None, *, status: int = 200) -> JSONResponse:
    if snapshot is None:
        return JSONResponse({"error": "当前账号下没有可设置目标的会话"}, status_code=404)
    body = {**asdict(snapshot), "supports_advance": True}
    if snapshot.run is None:
        body.pop("run")
    return JSONResponse(body, status_code=status)


def create_session_goals_router(goals: SessionGoalStore, steps: GoalSteps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session/{session_id}/goal")
    def read_goal(request: Request, session_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        return _response(goals.read(owner, session_id))

    @router.post("/api/session/{session_id}/goal")
    async def change_goal(request: Request, session_id: str, change: GoalChange) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        try:
            if change.action == "advance":
                return _response(await steps.advance(owner, session_id, change.version), status=202)
            if change.action == "pause":
                return _response(await steps.pause(owner, session_id, change.version))
            snapshot = await asyncio.to_thread(
                goals.apply, owner, session_id, change.action, change.version, objective=change.objective,
            )
        except GoalVersionConflict as exc:
            return JSONResponse({"error": str(exc), "code": "goal_version_conflict"}, status_code=409)
        except GoalStepConflict as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "code": "invalid_goal"}, status_code=400)
        return _response(snapshot)

    return router
