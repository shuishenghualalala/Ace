"""Development-only planning preview API; it never dispatches work."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.collaboration.planning.plan import PlanningError
from crew.collaboration.planning.service import PlanningService
from crew.collaboration.planning.store import PlanningStore
from crew.gateway.auth import account_from_request


def create_collaboration_planning_router(crew: Any) -> APIRouter:
    router = APIRouter()
    store: PlanningStore | None = None

    def get_store() -> PlanningStore:
        nonlocal store
        if store is None:
            store = PlanningStore(crew.config.db_path, wal_enabled=crew.config.sqlite_wal)
        return store

    @router.post("/api/collaboration/planning-previews")
    async def create_preview(request: Request, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        try:
            team_id = str(payload.get("team_id") or "").strip()
            request_id = str(payload.get("request_id") or "").strip()
            goal = str(payload.get("goal") or "").strip()
            inputs = payload.get("inputs", [])
            if not team_id or not request_id or not goal or not isinstance(inputs, list) or any(not isinstance(i, str) for i in inputs):
                raise ValueError("请提供团队、请求标识、任务目标和有效的输入列表")
            if len(goal) > 16000 or len(request_id) > 200 or len(inputs) > 50 or any(len(i) > 4000 for i in inputs):
                raise ValueError("任务输入过长")
            if crew.external_agents is None:
                raise ValueError("团队存储不可用")
            team = crew.external_agents.get_team(team_id, owner_account_id=owner)
            result = await PlanningService(get_store()).create(
                owner=owner, request_id=request_id, session_id=str(payload.get("session_id") or ""),
                goal=goal, team=team, provider=crew.owner_team_provider(owner), inputs=inputs)
            return JSONResponse({"ok": True, "preview": result})
        except KeyError:
            return JSONResponse({"ok": False, "error": "团队不存在"}, status_code=404)
        except (ValueError, PlanningError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/api/collaboration/planning-previews/{task_id}")
    async def get_preview(request: Request, task_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        result = get_store().get(owner, task_id)
        return JSONResponse({"ok": bool(result), "preview": result}, status_code=200 if result else 404)

    @router.post("/api/collaboration/planning-previews/{task_id}/decisions/{decision_id}")
    async def answer_preview(request: Request, task_id: str, decision_id: str, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        try:
            result = get_store().answer(owner, task_id, decision_id, str(payload.get("member_id") or ""))
            return JSONResponse({"ok": True, "preview": result})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return router
