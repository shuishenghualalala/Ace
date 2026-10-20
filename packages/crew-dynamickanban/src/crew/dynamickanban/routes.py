"""Dynamic Kanban 看板 HTTP 入口。

- GET  /api/dynamic-kanban/{session_id}/board   获取某会话的最新看板状态
- GET  /api/dynamic-kanban/{session_id}/status  获取 workflow + runtime state 快照
- POST /api/dynamic-kanban/{session_id}/pause   暂停 workflow
- POST /api/dynamic-kanban/{session_id}/resume  恢复 workflow（SSE 流）
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from crew.core.envelope import Envelope, ResponseChunk


def _owner(request: Request) -> str:
    """Read the host's minimal authenticated-owner request contract."""
    account = getattr(request.state, "account", None)
    owner = str(getattr(account, "owner_account_id", "") or "").strip()
    if not owner:
        raise RuntimeError("authenticated owner is missing from request state")
    return owner


def create_dynamic_kanban_router(crew, *, service: Any) -> APIRouter:
    router = APIRouter()
    dk_manager = service.manager

    @router.get("/api/dynamic-kanban/{session_id}/board")
    async def dynamic_kanban_board(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        store = dk_manager.store.for_owner(owner)
        workflow = store.get_latest_workflow_by_session(session_id, exclude_source="team")
        if workflow is None:
            return JSONResponse({"error": "该会话暂无 Dynamic Kanban 工作流"}, status_code=404)
        board = store.get_board_state(workflow.id)
        board["workflow"] = workflow.to_dict()
        return JSONResponse(board)

    @router.get("/api/dynamic-kanban/{session_id}/status")
    async def dynamic_kanban_status(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        status = dk_manager.status(session_id, owner_account_id=owner)
        if status is None:
            return JSONResponse({"error": "该会话暂无 workflow"}, status_code=404)
        return JSONResponse(status)

    @router.post("/api/dynamic-kanban/{session_id}/pause")
    async def dynamic_kanban_pause(
        session_id: str,
        request: Request,
        reason: str = "用户请求暂停",
    ) -> JSONResponse:
        owner = _owner(request)
        ok = dk_manager.pause(session_id, reason=reason, owner_account_id=owner)
        if not ok:
            return JSONResponse({"ok": False, "error": "没有运行中的 workflow"}, status_code=404)
        return JSONResponse({"ok": True, "session_id": session_id, "reason": reason})

    @router.post("/api/dynamic-kanban/{session_id}/resume")
    async def dynamic_kanban_resume(
        session_id: str,
        request: Request,
    ) -> StreamingResponse:
        envelope = Envelope(
            session_id=session_id,
            params={"query": "继续执行"},
            request_id=f"resume_{session_id}_{asyncio.get_event_loop().time()}",
            channel="gateway",
            user_id=_owner(request),
            workspace_id="default",
            mode="dynamic_kanban",
        )

        async def event_stream():
            async with aclosing(
                dk_manager.resume_stream(
                    session_id,
                    envelope.request_id,
                    envelope,
                )
            ) as stream:
                async for chunk in stream:
                    yield f"data: {json.dumps(_chunk_to_dict(chunk), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return router


def _chunk_to_dict(chunk: ResponseChunk) -> dict[str, Any]:
    return {
        "request_id": chunk.request_id,
        "kind": chunk.kind,
        "body": chunk.body,
        "sequence": chunk.sequence,
        "is_final": chunk.is_final,
        "status": chunk.status,
        "ts": chunk.ts,
    }


async def _sse_error(message: str):
    yield f"data: {json.dumps({'kind': 'error', 'body': {'message': message}}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
