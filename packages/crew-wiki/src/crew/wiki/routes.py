"""Wiki REST API route contribution owned by the Wiki feature."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from crew.core.envelope import feature_event_body
from crew.features.runtime import FeatureGeneration, FeatureLeaseUnavailableError
from crew.state.logging import get_logger
from crew.wiki._utils import is_wiki_agent_session
from crew.wiki.capture import CaptureError, CaptureValidationError
from crew.wiki.schemas import WikiPage, WikiRelation
from crew.wiki.service import KNOWLEDGE_SERVICE_KEY, KnowledgeService, normalize_kb_id

log = get_logger("wiki.routes")


def create_wiki_router(crew) -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

    ingest_tasks: dict[tuple[str, str, FeatureGeneration | None], asyncio.Task] = {}
    active_service: ContextVar[KnowledgeService | None] = ContextVar(
        "wiki_gateway_service", default=None
    )

    @asynccontextmanager
    async def _service_context(request: Request | None = None):
        plugins = getattr(crew, "plugins", None)
        acquire = getattr(plugins, "acquire_service_lease", None)
        if callable(acquire):
            route_binding = getattr(
                getattr(request, "state", None), "route_binding", None
            )
            generation = getattr(route_binding, "generation", None)
            try:
                if generation is None:
                    acquired = acquire(KNOWLEDGE_SERVICE_KEY, label="gateway:wiki")
                else:
                    # A route gate already selected and leased one exact
                    # Generation.  The Service lookup must use that same
                    # identity; falling back to the newest service here would
                    # let a restart mix an old router request with a new
                    # provider.  Older host adapters that do not understand
                    # generation binding fail closed for this gated request.
                    acquired = acquire(
                        KNOWLEDGE_SERVICE_KEY,
                        label="gateway:wiki",
                        generation=generation,
                    )
            except FeatureLeaseUnavailableError:
                acquired = None
            except TypeError:
                acquired = None
            if acquired is None:
                if generation is not None:
                    yield None
                    return
                yield _service()
                return
            service, lease = acquired
            async with lease:
                yield service
            return
        yield _service()

    def _with_knowledge(handler):
        @wraps(handler)
        async def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next((arg for arg in args if isinstance(arg, Request)), None)
            async with _service_context(request) as service:
                if service is None:
                    return _unavailable()
                token = active_service.set(service)
                try:
                    return await handler(*args, **kwargs)
                finally:
                    active_service.reset(token)
        return wrapped

    def _service() -> KnowledgeService | None:
        current = active_service.get()
        if current is not None:
            return current
        plugins = getattr(crew, "plugins", None)
        resolver = getattr(plugins, "resolve_service", None)
        if callable(resolver):
            resolved = resolver(KNOWLEDGE_SERVICE_KEY, default=None)
            if resolved is not None:
                return resolved
        return getattr(crew, "knowledge_service", None)

    def _unavailable() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)

    def _owner(request: Request) -> str:
        """Read the host's minimal authenticated-owner request contract."""
        account = getattr(getattr(request, "state", None), "account", None)
        owner = str(getattr(account, "owner_account_id", "") or "").strip()
        if not owner:
            raise RuntimeError("authenticated owner is missing from request state")
        return owner

    def _kb_id(request: Request) -> str:
        return request.query_params.get("kb_id") or "default"

    def _task_key(
        owner: str,
        source_id: str,
        generation: FeatureGeneration | None,
    ) -> tuple[str, str, FeatureGeneration | None]:
        # The router object is intentionally stable across replacement
        # generations. Keep cancellation state generation-scoped so a new
        # request cannot cancel an older provider's in-flight ingest task.
        return (owner, source_id, generation)

    def _route_generation(request: Request) -> FeatureGeneration | None:
        binding = getattr(getattr(request, "state", None), "route_binding", None)
        return getattr(binding, "generation", None)

    def _source_titles_for_page(service: KnowledgeService, page: WikiPage, owner: str, kb_id: str) -> dict[str, str]:
        """获取页面数据源 source_id -> title 的映射。"""
        if not page.sources:
            return {}
        return service.source_titles(page.sources, owner, kb_id)

    def _source_pages_for_page(service: KnowledgeService, page: WikiPage, owner: str, kb_id: str) -> list[dict[str, Any]]:
        """按稳定页面 ID 返回可跳转的来源摘要页，并去除重复来源。"""
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_id in page.sources:
            source_pages = service.source_pages(source_id, owner, kb_id)
            source_page = next((candidate for candidate in source_pages if candidate.page_type == "source"), None)
            if source_page is None or source_page.id in seen:
                continue
            seen.add(source_page.id)
            result.append({
                "id": source_page.id,
                "title": source_page.title,
                "page_type": source_page.page_type,
            })
        return result

    def _relation_pages_for_page(service: KnowledgeService, page: WikiPage, owner: str, kb_id: str) -> list[dict[str, Any]]:
        """返回页面的正向与反向结构化关系，供详情页直接展示和跳转。"""
        result: list[dict[str, Any]] = []
        for item in service.related_pages(page, owner, kb_id):
            result.append({
                "id": item.page.id,
                "title": item.page.title,
                "page_type": item.page.page_type,
                "relation": item.relation,
                "direction": item.direction,
            })
        return result

    def _source_files_for_page(service: KnowledgeService, page: WikiPage, owner: str, kb_id: str) -> dict[str, dict[str, Any]]:
        """获取页面数据源 source_id -> 原始文件元信息的映射。

        仅当原始文件真实存在时才返回，避免前端对丢失文件显示跳转链接。
        """
        if not page.sources:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for sid, source_file in service.source_files(page, owner, kb_id).items():
            if source_file.location:
                result[sid] = {
                    "original_path": source_file.location,
                    "file_type": source_file.file_type,
                    "title": source_file.title,
                }
        return result

    @router.post("/init")
    @_with_knowledge
    async def wiki_init(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        kb_id = _kb_id(request)
        service.initialize(owner, kb_id)
        return {"ok": True}

    def _wiki_agent_sessions(owner: str, kb_id: str) -> list[dict[str, Any]]:
        """返回指定知识库的 Wiki Agent 会话，保持 SessionStore 的最近优先顺序。"""
        session_store = getattr(crew, "session_store", None)
        if session_store is None:
            return []
        result: list[dict[str, Any]] = []
        for session in session_store.list_sessions(
            workspace_id="wiki",
            owner_account_id=owner,
        ):
            session_id = str(session.get("session_id") or "")
            if not is_wiki_agent_session(session_id):
                continue
            config = session_store.get_agent_config(session_id, owner_account_id=owner) or {}
            if not config.get("wiki_agent_session"):
                continue
            if str(config.get("wiki_kb_id") or "default") != kb_id:
                continue
            result.append(session)
        return result

    @router.get("/agent-sessions")
    @_with_knowledge
    async def wiki_agent_sessions(request: Request):
        """列出当前用户、当前知识库的 Wiki Agent 对话历史。"""
        if getattr(crew, "session_store", None) is None:
            return JSONResponse({"ok": False, "error": "会话存储未初始化"}, status_code=503)
        try:
            kb_id = normalize_kb_id(request.query_params.get("kb_id"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {
            "ok": True,
            "kb_id": kb_id,
            "sessions": _wiki_agent_sessions(_owner(request), kb_id),
        }

    @router.post("/agent-session")
    @_with_knowledge
    async def wiki_agent_session(request: Request):
        """获取或创建 Wiki Agent session；force_new=true 时始终新建。"""
        session_store = getattr(crew, "session_store", None)
        if session_store is None:
            return JSONResponse({"ok": False, "error": "会话存储未初始化"}, status_code=503)
        owner = _owner(request)
        try:
            kb_id = normalize_kb_id(request.query_params.get("kb_id"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        force_new = request.query_params.get("force_new", "").lower() in {"1", "true", "yes"}

        # 默认复用当前知识库最近一次会话；显式新建时保留旧会话供历史切换。
        for s in [] if force_new else _wiki_agent_sessions(owner, kb_id):
            sid = s.get("session_id", "")
            cfg = session_store.get_agent_config(sid, owner_account_id=owner) or {}
            # 向前兼容旧 Wiki session：补齐正式预设身份。
            if cfg.get("preset_agent_type") != "Wiki":
                cfg["preset_agent_type"] = "Wiki"
                cfg["wiki_kb_id"] = kb_id
                session_store.set_agent_config(sid, cfg, owner_account_id=owner)
            return {"ok": True, "session_id": sid, "kb_id": kb_id}

        # 没有则创建新 session
        session_id = f"wiki-{uuid.uuid4().hex[:12]}"
        session_store.ensure_session(
            session_id,
            workspace_id="wiki",
            # 使用占位标题，让首轮消息沿用现有会话自动命名能力，历史列表更易辨认。
            title="新对话",
            owner_account_id=owner,
        )
        session_store.set_agent_config(
            session_id,
            {
                "wiki_agent_session": True,
                "preset_agent_type": "Wiki",
                "wiki_kb_id": kb_id,
            },
            owner_account_id=owner,
        )
        return {"ok": True, "session_id": session_id, "kb_id": kb_id}

    @router.post("/confirmations/{confirmation_id}/cancel")
    @_with_knowledge
    async def wiki_cancel_confirmation(confirmation_id: str, request: Request):
        data = await request.json()
        session_id = str(data.get("session_id") or "").strip()
        service = _service()
        if service is None or not session_id:
            return JSONResponse({"ok": False, "error": "缺少 Wiki 会话"}, status_code=400)
        cancelled = service.cancel_confirmation(
            session_id,
            confirmation_id,
            owner_account_id=_owner(request),
        )
        if not cancelled:
            return JSONResponse({"ok": False, "error": "确认已失效或不属于当前会话"}, status_code=404)
        return {"ok": True, "cancelled": True}

    @router.get("/kbs")
    @_with_knowledge
    async def wiki_list_kbs(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        kbs = service.list_knowledge_bases(_owner(request))
        return {"ok": True, "kbs": [kb.to_dict() for kb in kbs]}

    @router.post("/kbs")
    @_with_knowledge
    async def wiki_create_kb(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        data = await request.json()
        kb_id = str(data.get("kb_id", "")).strip()
        if not kb_id:
            return JSONResponse({"ok": False, "error": "缺少 kb_id"}, status_code=400)
        name = str(data.get("name", "") or kb_id).strip()
        try:
            kb = service.create_knowledge_base(kb_id, name, _owner(request))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "kb": kb.to_dict()}

    @router.delete("/kbs/{kb_id}")
    @_with_knowledge
    async def wiki_delete_kb(kb_id: str, request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        session_ids = [
            str(session.get("session_id") or "")
            for session in _wiki_agent_sessions(owner, kb_id)
            if str(session.get("session_id") or "")
        ]
        try:
            ok = service.delete_knowledge_base(kb_id, owner)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not ok:
            return JSONResponse({"ok": False, "error": "知识库不存在"}, status_code=404)
        session_store = getattr(crew, "session_store", None)
        if session_store is not None:
            for session_id in session_ids:
                session_store.clear(session_id, owner_account_id=owner)
        return {
            "ok": True,
            "deleted_session_ids": session_ids,
        }

    @router.get("/vault-documents/{document_name}")
    @_with_knowledge
    async def wiki_vault_document(document_name: str, request: Request):
        """读取文件树根部的公开文档；不接受任意 Vault 路径。"""
        service = _service()
        if service is None:
            return _unavailable()
        if document_name not in {"Home.md", "index.md"}:
            return JSONResponse(
                {"ok": False, "error": "只允许读取 Home.md 或 index.md"},
                status_code=400,
            )
        document = service.read_vault_document(document_name, _owner(request), _kb_id(request))
        if document is None:
            return JSONResponse({"ok": False, "error": "文档不存在"}, status_code=404)
        return {
            "ok": True,
            "document": {
                "name": document_name,
                "path": document_name,
                "content": document.content,
                "updated_at": document.updated_at,
            },
        }

    @router.get("/pages")
    @_with_knowledge
    async def wiki_pages(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        limit = int(request.query_params.get("limit", 100))
        offset = int(request.query_params.get("offset", 0))
        brief = request.query_params.get("brief", "").lower() in ("1", "true", "yes")
        owner = _owner(request)
        kb_id = _kb_id(request)
        pages = service.list_pages(owner, kb_id, limit=limit, offset=offset, brief=brief)
        source_titles: dict[str, str] = {}
        for page in pages:
            source_titles.update(_source_titles_for_page(service, page, owner, kb_id))
        source_files: dict[str, dict[str, Any]] = {}
        for page in pages:
            source_files.update(_source_files_for_page(service, page, owner, kb_id))
        return {
            "ok": True,
            "pages": [p.to_dict(brief=brief) for p in pages],
            "source_titles": source_titles,
            "source_files": source_files,
        }

    @router.post("/pages")
    @_with_knowledge
    async def wiki_create_page(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        data = await request.json()
        kb_id = _kb_id(request)
        page_type = str(data.get("page_type", "topic"))
        if page_type not in {"entity", "topic", "source", "comparison", "synthesis"}:
            return JSONResponse(
                {"ok": False, "error": f"不支持的 Wiki 页面类型: {page_type}"},
                status_code=400,
            )
        page = WikiPage(
            id="",
            page_type=page_type,
            title=data.get("title", ""),
            content=data.get("content", ""),
            file_path="",
            sources=list(data.get("sources") or []),
            tags=list(data.get("tags") or []),
            relations=[
                WikiRelation.from_dict(item)
                for item in data.get("relations", [])
                if isinstance(item, dict)
            ],
        )
        owner = _owner(request)
        saved = service.save_page(page, owner, kb_id)
        return {
            "ok": True,
            "page": saved.to_dict(),
            "source_titles": _source_titles_for_page(service, saved, owner, kb_id),
            "source_files": _source_files_for_page(service, saved, owner, kb_id),
        }

    @router.get("/pages/{page_id}")
    @_with_knowledge
    async def wiki_get_page(page_id: str, request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        kb_id = _kb_id(request)
        page = service.read_document(page_id, owner, kb_id)
        if page is None:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        return {
            "ok": True,
            "page": page.to_dict(),
            "source_titles": _source_titles_for_page(service, page, owner, kb_id),
            "source_files": _source_files_for_page(service, page, owner, kb_id),
            "source_pages": _source_pages_for_page(service, page, owner, kb_id),
            "relation_pages": _relation_pages_for_page(service, page, owner, kb_id),
        }

    @router.put("/pages/{page_id}")
    @_with_knowledge
    async def wiki_update_page(page_id: str, request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        kb_id = _kb_id(request)
        existing = service.read_document(page_id, _owner(request), kb_id)
        if existing is None:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        data = await request.json()
        existing.title = str(data.get("title", existing.title))
        existing.content = str(data.get("content", existing.content))
        existing.tags = list(data.get("tags", existing.tags))
        existing.sources = list(data.get("sources", existing.sources))
        if "relations" in data:
            existing.relations = [
                WikiRelation.from_dict(item)
                for item in data["relations"]
                if isinstance(item, dict)
            ]
        existing.related = []
        owner = _owner(request)
        updated = service.update_page(existing, owner, kb_id)
        result_page = updated or existing
        return {
            "ok": True,
            "page": result_page.to_dict(),
            "source_titles": _source_titles_for_page(service, result_page, owner, kb_id),
            "source_files": _source_files_for_page(service, result_page, owner, kb_id),
            "source_pages": _source_pages_for_page(service, result_page, owner, kb_id),
            "relation_pages": _relation_pages_for_page(service, result_page, owner, kb_id),
        }

    @router.delete("/pages/{page_id}")
    @_with_knowledge
    async def wiki_delete_page(page_id: str, request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        ok = service.delete_page(page_id, _owner(request), _kb_id(request))
        if not ok:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        return {"ok": True}

    @router.delete("/pages")
    @_with_knowledge
    async def wiki_bulk_delete(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        data = await request.json()
        page_ids = list(data.get("page_ids") or [])
        kb_id = _kb_id(request)
        deleted = []
        failed = []
        for page_id in page_ids:
            ok = service.delete_page(page_id, _owner(request), kb_id)
            if ok:
                deleted.append(page_id)
            else:
                failed.append({"id": page_id, "error": "页面不存在"})
        return {"ok": True, "deleted": deleted, "failed": failed}

    @router.get("/search")
    @_with_knowledge
    async def wiki_search(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        query = request.query_params.get("q", "")
        top_k = int(request.query_params.get("top_k", 5))
        owner = _owner(request)
        kb_id = _kb_id(request)
        pages = service.search_pages(query, owner, top_k, kb_id)
        source_titles: dict[str, str] = {}
        for page in pages:
            source_titles.update(_source_titles_for_page(service, page, owner, kb_id))
        source_files: dict[str, dict[str, Any]] = {}
        for page in pages:
            source_files.update(_source_files_for_page(service, page, owner, kb_id))
        return {"ok": True, "pages": [p.to_dict() for p in pages], "source_titles": source_titles, "source_files": source_files}

    @router.get("/sources")
    @_with_knowledge
    async def wiki_list_sources(request: Request):
        """列出当前知识库的所有 raw sources。"""
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        kb_id = _kb_id(request)
        status_filter = request.query_params.get("status", "all").strip().lower()
        limit = max(1, int(request.query_params.get("limit", 200)))
        offset = max(0, int(request.query_params.get("offset", 0)))
        raws = service.list_sources(owner, kb_id)
        if status_filter != "all":
            raws = [r for r in raws if (r.parse_status or "pending") == status_filter]
        total = len(raws)
        raws.sort(key=lambda r: r.created_at, reverse=True)
        page = raws[offset : offset + limit]
        return {
            "ok": True,
            "sources": [r.to_dict() for r in page],
            "total": total,
            "kb_id": kb_id,
        }

    @router.delete("/sources/{source_id}")
    @_with_knowledge
    async def wiki_delete_source(source_id: str, request: Request):
        """删除指定的 raw source 及其关联页面。"""
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        kb_id = _kb_id(request)

        # 先收集关联页面，用于前端展示影响范围
        related_pages = []
        for page in service.list_pages(owner, kb_id, limit=10000):
            if source_id in page.sources:
                related_pages.append({"id": page.id, "title": page.title})

        ok = service.delete_source(source_id, owner_account_id=owner, kb_id=kb_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "source 不存在"}, status_code=404)
        return {"ok": True, "deleted_source_id": source_id, "related_pages": related_pages}

    @router.get("/sources/{source_id}/file")
    @_with_knowledge
    async def wiki_source_file(source_id: str, request: Request):
        """返回原始数据源文件，供浏览器/本地程序打开。"""
        service = _service()
        if service is None:
            return _unavailable()
        owner = _owner(request)
        kb_id = _kb_id(request)
        source_file = service.source_file(source_id, owner, kb_id)
        if source_file is None:
            return JSONResponse({"ok": False, "error": "源文件不存在"}, status_code=404)
        if source_file.content is None:
            return JSONResponse({"ok": False, "error": "源文件已丢失"}, status_code=404)
        return Response(
            content=source_file.content,
            media_type=source_file.file_type,
            headers={
                "content-disposition": (
                    "attachment; filename*=UTF-8''" + quote(source_file.title)
                )
            },
        )

    @router.post("/ingest")
    @_with_knowledge
    async def wiki_ingest(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        data = await request.json()
        source_id = str(data.get("source_id", ""))
        session_id = str(data.get("session_id", ""))
        if not source_id:
            return JSONResponse({"ok": False, "error": "缺少 source_id"}, status_code=400)

        owner = _owner(request)
        kb_id = _kb_id(request)
        progress_tasks: list[asyncio.Task] = []

        def _push_payload(session: str, payload: dict) -> None:
            fn = getattr(crew, "_push_payload_fn", None)
            if fn is None or not session:
                return
            log.info("Wiki router push payload session=%s kind=%s", session, payload.get("kind"))
            progress_tasks.append(asyncio.create_task(fn(session, payload, owner_account_id=owner)))

        if session_id:
            async def _progress(stage: str, percent: int, detail: dict) -> None:
                label = detail.get("label", stage)
                _push_payload(
                    session_id,
                    {
                        "kind": "feature_event",
                        "body": feature_event_body(
                            "wiki",
                            "ingest_progress",
                            {
                                "stage": stage,
                                "percent": percent,
                                "label": label,
                                "source_id": source_id,
                                "detail": detail,
                            },
                        ),
                        "is_final": stage == "done",
                        "sequence": 0,
                        "session_id": session_id,
                    },
                )
        else:
            _progress = None

        cancel_event = asyncio.Event()
        task_key = _task_key(owner, source_id, _route_generation(request))

        async def _ingest_with_cancel():
            ingest_tasks[task_key] = asyncio.current_task()  # type: ignore[assignment]
            try:
                return await service.ingest(
                    source_id,
                    owner_account_id=owner,
                    kb_id=kb_id,
                    progress=_progress,
                    cancel_event=cancel_event,
                )
            finally:
                ingest_tasks.pop(task_key, None)

        result = await _ingest_with_cancel()
        if result.issues and session_id:
            _push_payload(
                session_id,
                {
                    "kind": "feature_event",
                    "body": feature_event_body(
                        "wiki",
                        "ingest_progress",
                        {
                            "stage": "done",
                            "percent": 100,
                            "label": "编译完成",
                            "source_id": source_id,
                            "error": result.issues[0],
                        },
                    ),
                    "is_final": True,
                    "sequence": 0,
                    "session_id": session_id,
                },
            )
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        return {"ok": True, **result.to_dict()}

    @router.post("/ingest/cancel")
    @_with_knowledge
    async def wiki_cancel_ingest(request: Request):
        data = await request.json()
        source_id = str(data.get("source_id", ""))
        if not source_id:
            return JSONResponse({"ok": False, "error": "缺少 source_id"}, status_code=400)
        owner = _owner(request)
        key = _task_key(owner, source_id, _route_generation(request))
        task = ingest_tasks.get(key)
        if task is None or task.done():
            return JSONResponse({"ok": False, "error": "没有正在进行的 ingest 任务"}, status_code=404)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return {"ok": True, "cancelled": True}

    @router.post("/compile")
    @_with_knowledge
    async def wiki_compile(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        result = await service.compile_all(
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
        )
        return {"ok": True, "ingested": result.ingested, "errors": result.errors}

    @router.get("/graph")
    @_with_knowledge
    async def wiki_graph(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        graph = await service.graph(_owner(request), _kb_id(request))
        return {"ok": True, "graph": graph.to_dict()}

    @router.get("/query")
    @_with_knowledge
    async def wiki_query(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        q = request.query_params.get("q", "")
        if not q:
            return JSONResponse({"ok": False, "error": "缺少 q"}, status_code=400)
        result = service.query(
            q,
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
        )
        return {"ok": True, **result}

    @router.post("/lint")
    @_with_knowledge
    async def wiki_lint(request: Request):
        service = _service()
        if service is None:
            return _unavailable()
        deep = request.query_params.get("deep", "").lower() in ("1", "true", "yes")
        issues = await service.lint(
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
            deep=deep,
        )
        return {"ok": True, "issues": issues}

    @router.post("/upload")
    @_with_knowledge
    async def wiki_upload(request: Request):
        service = _service()
        if service is None:
            return _unavailable()

        kb_id = _kb_id(request)
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"表单解析失败: {exc}"}, status_code=400)
        uploaded = form.get("file")
        if uploaded is None:
            return JSONResponse({"ok": False, "error": "缺少 file 字段"}, status_code=400)
        content = await uploaded.read()
        filename = str(getattr(uploaded, "filename", "upload") or "upload")
        if not content:
            return JSONResponse({"ok": False, "error": "上传文件为空"}, status_code=400)

        result = await service.upload_file(filename, content, _owner(request), kb_id)
        if result.error_code:
            return JSONResponse({
                "ok": False, "error": result.error, "error_code": result.error_code,
                "dependency": result.dependency, "source_id": result.source_id,
                "install_command": result.install_command,
                "source_type": result.source_type,
                "needs_confirmation": result.needs_confirmation,
            }, status_code=400)
        payload: dict[str, Any] = {
            "ok": True, "source_id": result.source_id, "title": result.title,
            "source_type": result.source_type, "ingested": result.ingested,
            "needs_confirmation": result.needs_confirmation,
        }
        if result.pages:
            payload["pages"] = [page.to_dict() for page in result.pages]
            payload["issues"] = list(result.issues)
        if result.needs_agent_review:
            payload.update({"error": result.error, "needs_agent_review": True,
                            "message": "文件已保存，但自动解析失败，已交给 Wiki Agent 处理。"})
        return payload

    @router.post("/capture")
    @_with_knowledge
    async def wiki_capture(request: Request):
        """把一段文本（如浏览器标签页正文）存为不可变 RawSource 并发布 Source 页面。

        与 wiki_capture_text 工具共用 crew.wiki.capture 的入库流水线，
        这里只做 HTTP 参数解析与响应包装，供面板「存入 Wiki」使用。
        """
        service = _service()
        if service is None:
            return _unavailable()
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "请求体必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"}, status_code=400)
        owner = _owner(request)
        try:
            kb_id = normalize_kb_id(payload.get("kb_id"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        title = str(payload.get("title") or "")
        content = str(payload.get("content") or "")
        source_url = str(payload.get("source_url") or "").strip()

        # 面板文本一律按 web 来源归类（material_kind=article），source_url 供溯源。
        try:
            outcome = service.capture_text(
                title=title,
                content=content,
                owner_account_id=owner,
                kb_id=kb_id,
                source_url=source_url,
            )
        except CaptureValidationError as exc:
            body: dict[str, Any] = {"ok": False, "error": str(exc)}
            if exc.source_id:
                body["source_id"] = exc.source_id
            return JSONResponse(body, status_code=400)
        except CaptureError as exc:  # raw 已落库，可让 Wiki Agent 挽救
            log.warning("Wiki 文本捕获失败 source=%s: %s", exc.source_id, exc)
            return JSONResponse(
                {"ok": False, "error": f"捕获失败: {exc}", "source_id": exc.source_id},
                status_code=500,
            )
        if outcome.duplicate is not None:
            return {
                "ok": True,
                "source_id": outcome.raw.id,
                "pages": [],
                "duplicate": True,
                "duplicate_of": outcome.duplicate.id,
            }
        return {"ok": True, "source_id": outcome.raw.id, "pages": [outcome.page.to_dict(brief=True)]}

    return router
