"""场景推荐（scenarios）HTTP 入口。

- /api/scenarios       随机推荐 N 个场景（首页 / 换一换；count 默认 4）
- /api/scenarios/all   全量场景（前端可缓存后本地洗牌）
- /api/scenarios/intro-lines   随机 Crew 功能介绍话术（任务运行中 loading 轮播）
- /api/scenarios/loading-status   随机任务运行状态语
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crew.scenarios import get_scenarios, recommend, recommend_intro_lines, recommend_loading_statuses


def create_scenarios_router(crew) -> APIRouter:
    router = APIRouter()

    @router.get("/api/scenarios")
    async def scenarios(count: int = 4) -> JSONResponse:
        return JSONResponse(recommend(count))

    @router.get("/api/scenarios/all")
    async def scenarios_all() -> JSONResponse:
        return JSONResponse(get_scenarios())

    @router.get("/api/scenarios/intro-lines")
    async def intro_lines(count: int = 8) -> JSONResponse:
        return JSONResponse(recommend_intro_lines(count))

    @router.get("/api/scenarios/loading-status")
    async def loading_status(count: int = 8) -> JSONResponse:
        return JSONResponse(recommend_loading_statuses(count))

    return router
