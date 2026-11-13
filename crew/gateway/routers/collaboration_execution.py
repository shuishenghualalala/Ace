"""Owner-scoped authoritative execution projection and explicit controls."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.collaboration.runtime.store import ExecutionStore
from crew.gateway.auth import account_from_request


def create_collaboration_execution_router(crew: Any) -> APIRouter:
    router = APIRouter()
    store = None

    def repository():
        nonlocal store
        if store is None:
            store = ExecutionStore(crew.config.db_path, wal_enabled=crew.config.sqlite_wal)
        return store

    @router.get('/api/collaboration/sessions/{session_id}/executions')
    async def list_executions(request: Request, session_id: str):
        owner = account_from_request(request).owner_account_id
        return {'ok': True, 'executions': repository().for_session(owner, session_id)}

    @router.get('/api/collaboration/executions/{task_id}')
    async def get_execution(request: Request, task_id: str):
        owner = account_from_request(request).owner_account_id
        task = repository().get(owner, task_id)
        return JSONResponse({'ok': bool(task), 'execution': task}, status_code=200 if task else 404)

    @router.post('/api/collaboration/executions/{task_id}/control')
    async def control(request: Request, task_id: str, payload: dict):
        owner = account_from_request(request).owner_account_id
        task = repository().get(owner, task_id)
        if task is None:
            return JSONResponse({'ok': False, 'error': '任务不存在'}, status_code=404)
        action = str(payload.get('action') or '')
        try:
            from crew.team.collaboration_runtime import control_execution
            control_execution(crew.team, owner, task_id, action, unit_id=str(payload.get('unit_id') or ''),
                              confirm_unknown=payload.get('confirm_unknown') is True)
            return {'ok': True, 'execution': repository().get(owner, task_id),
                    'notice': '停止启动新工作；已发出的调用可能继续，结果会如实记录。'}
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)

    return router
