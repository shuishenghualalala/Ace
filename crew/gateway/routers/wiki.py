"""Wiki REST API 路由。"""

from __future__ import annotations

import asyncio
import getpass
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from crew.gateway.auth import account_from_request
from crew.state.logging import get_logger
from crew.wiki._utils import is_wiki_agent_session
from crew.wiki.capture import CaptureError, CaptureValidationError, capture_text_source
from crew.wiki.parser import (
    MissingDependencyError,
    guess_mime_type,
    parse_document_from_bytes,
)
from crew.wiki.schemas import RawSource, WikiRelation
from crew.wiki.sources import classify_file
from crew.wiki.store import normalize_kb_id
from crew.wiki.store._ids import filename_from_title, source_page_id

log = get_logger("gateway.routers.wiki")


def create_wiki_router(crew) -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

    def _finish_page_write(owner: str, kb_id: str, message: str) -> None:
        compiler = getattr(crew, "_wiki_compiler", None)
        if compiler is not None:
            compiler.finalize_write(message, owner_account_id=owner, kb_id=kb_id)
    ingest_tasks: dict[tuple[str, str], asyncio.Task] = {}

    def _owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    def _kb_id(request: Request) -> str:
        return request.query_params.get("kb_id") or "default"

    def _find_title_conflict(
        store,
        title: str,
        page_type: str,
        owner: str,
        kb_id: str,
        exclude_page_id: str = "",
    ):
        """查找同知识库内同类型、同标题的页面。

        Source Page 的身份由 source_id 决定，不按标题去重；不同来源可以有相同
        的展示标题。其他页面类型沿用创建工具的唯一标题约束。
        """
        if page_type == "source":
            return None
        normalized_title = str(title or "").strip()
        if not normalized_title:
            return None
        for candidate in store.list_all(
            owner_account_id=owner,
            kb_id=kb_id,
            limit=10000,
            brief=True,
        ):
            if (
                candidate.id != exclude_page_id
                and candidate.page_type == page_type
                and str(candidate.title or "").strip() == normalized_title
            ):
                return candidate
        return None

    def _task_key(owner: str, source_id: str) -> tuple[str, str]:
        return (owner, source_id)

    def _source_titles_for_page(page, owner: str, kb_id: str) -> dict[str, str]:
        """获取页面数据源 source_id -> title 的映射。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None or not page.sources:
            return {}
        return store.get_source_titles(page.sources, owner, kb_id)

    def _source_pages_for_page(page, owner: str, kb_id: str) -> list[dict[str, Any]]:
        """按稳定页面 ID 返回可跳转的来源摘要页，并去除重复来源。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_id in page.sources:
            source_page = store.get_source_page(source_id, owner, kb_id)
            if source_page is None or source_page.id in seen:
                continue
            seen.add(source_page.id)
            result.append({
                "id": source_page.id,
                "title": source_page.title,
                "page_type": source_page.page_type,
            })
        return result

    def _relation_pages_for_page(page, owner: str, kb_id: str) -> list[dict[str, Any]]:
        """返回页面的正向与反向结构化关系，供详情页直接展示和跳转。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return []
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def _append(target, relation: str, direction: str) -> None:
            key = (target.id, relation.casefold(), direction)
            if target.id == page.id or key in seen:
                return
            seen.add(key)
            result.append({
                "id": target.id,
                "title": target.title,
                "page_type": target.page_type,
                "relation": relation,
                "direction": direction,
            })

        for relation in page.relations:
            target = store.get(relation.target_page_id, owner, kb_id)
            if target is not None:
                _append(target, relation.relation, "outgoing")
        for candidate in store.list_all(
            owner_account_id=owner,
            kb_id=kb_id,
            limit=10000,
        ):
            for relation in candidate.relations:
                if relation.target_page_id == page.id:
                    _append(candidate, relation.relation, "incoming")
        return result

    def _resolve_original_path(raw, owner: str, kb_id: str) -> Path | None:
        """根据 RawSource 元数据定位真实原始文件路径。

        优先使用记录的绝对路径；对旧数据的相对路径，依次尝试 raw_dir 下的同名路径、
        {source_id}.original{ext} 约定路径，以及任意 {source_id}.original* 文件。
        """
        if not raw or not raw.original_path:
            return None
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return None
        raw_dir = store._raw_dir(owner, kb_id)
        recorded = Path(raw.original_path)

        candidates: list[Path] = []
        if recorded.is_absolute():
            candidates.append(recorded)
        else:
            candidates.append(raw_dir / recorded)
            ext = Path(str(raw.title or "")).suffix.lower() or Path(recorded.name).suffix.lower()
            candidates.append(raw_dir / f"{raw.id}.original{ext}")
            # 兜底：raw_dir 下任意 {source_id}.original* 文件
            candidates.extend(sorted(raw_dir.glob(f"{raw.id}.original*")))

        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _source_files_for_page(page, owner: str, kb_id: str) -> dict[str, dict[str, Any]]:
        """获取页面数据源 source_id -> 原始文件元信息的映射。

        仅当原始文件真实存在时才返回，避免前端对丢失文件显示跳转链接。
        """
        store = getattr(crew, "_wiki_store", None)
        if store is None or not page.sources:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for sid in page.sources:
            raw = store.load_raw(sid, owner, kb_id)
            original_path = _resolve_original_path(raw, owner, kb_id)
            if original_path is not None:
                result[sid] = {
                    "original_path": str(original_path),
                    "file_type": raw.file_type if raw else None,
                    "title": (raw.title or sid) if raw else sid,
                }
        return result

    @router.post("/init")
    async def wiki_init(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        kb_id = _kb_id(request)
        store.init_kb(owner, kb_id)
        # 旧版种子/历史页面可能落在无 wiki/ 前缀的目录（如 entities/xxx.md），
        # 前端文件树按 wiki/ 前缀过滤会看不到；init 幂等，顺手做一次性布局迁移。
        try:
            if store.layout_migration_preview(owner, kb_id).get("required"):
                store.migrate_layout(owner, kb_id)
        except Exception:
            pass
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
    async def wiki_cancel_confirmation(confirmation_id: str, request: Request):
        data = await request.json()
        session_id = str(data.get("session_id") or "").strip()
        manager = getattr(crew, "wiki_manager", None)
        if manager is None or not session_id:
            return JSONResponse({"ok": False, "error": "缺少 Wiki 会话"}, status_code=400)
        cancelled = manager.cancel_confirmation(
            session_id,
            confirmation_id,
            owner_account_id=_owner(request),
        )
        if not cancelled:
            return JSONResponse({"ok": False, "error": "确认已失效或不属于当前会话"}, status_code=404)
        return {"ok": True, "cancelled": True}

    @router.get("/kbs")
    async def wiki_list_kbs(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        from crew.wiki.seed import ensure_tutorial_kb

        ensure_tutorial_kb(store, _owner(request))
        kbs = store.list_kbs(_owner(request))
        return {"ok": True, "kbs": [kb.to_dict() for kb in kbs]}

    @router.post("/kbs")
    async def wiki_create_kb(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        data = await request.json()
        kb_id = str(data.get("kb_id", "")).strip()
        if not kb_id:
            return JSONResponse({"ok": False, "error": "缺少 kb_id"}, status_code=400)
        name = str(data.get("name", "") or kb_id).strip()
        try:
            kb = store.create_kb(kb_id, name=name, owner_account_id=_owner(request))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "kb": kb.to_dict()}

    @router.delete("/kbs/{kb_id}")
    async def wiki_delete_kb(kb_id: str, request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        session_ids = [
            str(session.get("session_id") or "")
            for session in _wiki_agent_sessions(owner, kb_id)
            if str(session.get("session_id") or "")
        ]
        try:
            ok = store.delete_kb(kb_id, owner)
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
    async def wiki_vault_document(document_name: str, request: Request):
        """读取文件树根部的公开文档；不接受任意 Vault 路径。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        if document_name not in {"Home.md", "index.md"}:
            return JSONResponse(
                {"ok": False, "error": "只允许读取 Home.md 或 index.md"},
                status_code=400,
            )
        path = store._dir(_owner(request), _kb_id(request)) / document_name
        if not path.is_file():
            return JSONResponse({"ok": False, "error": "文档不存在"}, status_code=404)
        return {
            "ok": True,
            "document": {
                "name": document_name,
                "path": document_name,
                "content": path.read_text(encoding="utf-8", errors="replace"),
                "updated_at": path.stat().st_mtime,
            },
        }

    @router.get("/pages")
    async def wiki_pages(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        limit = int(request.query_params.get("limit", 100))
        offset = int(request.query_params.get("offset", 0))
        brief = request.query_params.get("brief", "").lower() in ("1", "true", "yes")
        owner = _owner(request)
        kb_id = _kb_id(request)
        pages = store.list_all(
            owner_account_id=owner,
            kb_id=kb_id,
            limit=limit,
            offset=offset,
            brief=brief,
        )
        source_titles: dict[str, str] = {}
        for page in pages:
            source_titles.update(_source_titles_for_page(page, owner, kb_id))
        source_files: dict[str, dict[str, Any]] = {}
        for page in pages:
            source_files.update(_source_files_for_page(page, owner, kb_id))
        return {
            "ok": True,
            "pages": [p.to_dict(brief=brief) for p in pages],
            "source_titles": source_titles,
            "source_files": source_files,
        }

    @router.post("/pages")
    async def wiki_create_page(request: Request):
        from crew.wiki.schemas import WikiPage

        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        data = await request.json()
        kb_id = _kb_id(request)
        page_type = str(data.get("page_type", "topic"))
        if page_type not in {"entity", "topic", "source", "comparison", "synthesis"}:
            return JSONResponse(
                {"ok": False, "error": f"不支持的 Wiki 页面类型: {page_type}"},
                status_code=400,
            )
        owner = _owner(request)
        title = str(data.get("title", "")).strip()
        conflict = _find_title_conflict(store, title, page_type, owner, kb_id)
        if conflict is not None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"页面标题已存在: {conflict.id}",
                    "existing_page_id": conflict.id,
                },
                status_code=409,
            )
        sources = [str(source_id).strip() for source_id in data.get("sources") or [] if str(source_id).strip()]
        page_id = ""
        if page_type == "source" and len(sources) == 1:
            page_id = source_page_id(sources[0])
            if store.get(page_id, owner, kb_id) is not None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"该来源页面已存在: {page_id}",
                        "existing_page_id": page_id,
                    },
                    status_code=409,
                )
        page = WikiPage(
            id=page_id,
            page_type=page_type,
            title=title,
            content=data.get("content", ""),
            file_path="",
            sources=sources,
            tags=list(data.get("tags") or []),
            relations=[
                WikiRelation.from_dict(item)
                for item in data.get("relations", [])
                if isinstance(item, dict)
            ],
        )
        saved = store.save_page(page, owner, kb_id)
        _finish_page_write(owner, kb_id, f"创建页面 {saved.id} ({saved.title})")
        return {
            "ok": True,
            "page": saved.to_dict(),
            "source_titles": _source_titles_for_page(saved, owner, kb_id),
            "source_files": _source_files_for_page(saved, owner, kb_id),
        }

    @router.get("/pages/{page_id}")
    async def wiki_get_page(page_id: str, request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        kb_id = _kb_id(request)
        page = store.get(page_id, owner, kb_id)
        if page is None:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        return {
            "ok": True,
            "page": page.to_dict(),
            "source_titles": _source_titles_for_page(page, owner, kb_id),
            "source_files": _source_files_for_page(page, owner, kb_id),
            "source_pages": _source_pages_for_page(page, owner, kb_id),
            "relation_pages": _relation_pages_for_page(page, owner, kb_id),
        }

    @router.put("/pages/{page_id}")
    async def wiki_update_page(page_id: str, request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        kb_id = _kb_id(request)
        existing = store.get(page_id, _owner(request), kb_id)
        if existing is None:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        data = await request.json()
        owner = _owner(request)
        new_title = str(data.get("title", existing.title)).strip()
        conflict = _find_title_conflict(
            store,
            new_title,
            existing.page_type,
            owner,
            kb_id,
            exclude_page_id=existing.id,
        )
        if conflict is not None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"目标标题已存在: {conflict.id}",
                    "existing_page_id": conflict.id,
                },
                status_code=409,
            )
        existing.title = new_title
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
        updated = store.update(existing, owner, kb_id)
        result_page = updated or existing
        _finish_page_write(owner, kb_id, f"更新页面 {result_page.id} ({result_page.title})")
        return {
            "ok": True,
            "page": result_page.to_dict(),
            "source_titles": _source_titles_for_page(result_page, owner, kb_id),
            "source_files": _source_files_for_page(result_page, owner, kb_id),
            "source_pages": _source_pages_for_page(result_page, owner, kb_id),
            "relation_pages": _relation_pages_for_page(result_page, owner, kb_id),
        }

    @router.delete("/pages/{page_id}")
    async def wiki_delete_page(page_id: str, request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        ok = store.delete(page_id, _owner(request), _kb_id(request))
        if not ok:
            return JSONResponse({"ok": False, "error": "页面不存在"}, status_code=404)
        return {"ok": True}

    @router.delete("/pages")
    async def wiki_bulk_delete(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        data = await request.json()
        page_ids = list(data.get("page_ids") or [])
        kb_id = _kb_id(request)
        deleted = []
        failed = []
        for page_id in page_ids:
            ok = store.delete(page_id, _owner(request), kb_id)
            if ok:
                deleted.append(page_id)
            else:
                failed.append({"id": page_id, "error": "页面不存在"})
        return {"ok": True, "deleted": deleted, "failed": failed}

    @router.get("/search")
    async def wiki_search(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        query = request.query_params.get("q", "")
        top_k = int(request.query_params.get("top_k", 5))
        owner = _owner(request)
        kb_id = _kb_id(request)
        pages = store.search(
            query,
            top_k=top_k,
            owner_account_id=owner,
            kb_id=kb_id,
        )
        source_titles: dict[str, str] = {}
        for page in pages:
            source_titles.update(_source_titles_for_page(page, owner, kb_id))
        source_files: dict[str, dict[str, Any]] = {}
        for page in pages:
            source_files.update(_source_files_for_page(page, owner, kb_id))
        return {"ok": True, "pages": [p.to_dict() for p in pages], "source_titles": source_titles, "source_files": source_files}

    @router.get("/sources")
    async def wiki_list_sources(request: Request):
        """列出当前知识库的所有 raw sources。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        kb_id = _kb_id(request)
        status_filter = request.query_params.get("status", "all").strip().lower()
        limit = max(1, int(request.query_params.get("limit", 200)))
        offset = max(0, int(request.query_params.get("offset", 0)))
        raws = store.list_raws(owner_account_id=owner, kb_id=kb_id)
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
    async def wiki_delete_source(source_id: str, request: Request):
        """删除指定的 raw source 及其关联页面。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        kb_id = _kb_id(request)

        # 先收集关联页面，用于前端展示影响范围
        related_pages = []
        for page in store.list_all(owner_account_id=owner, kb_id=kb_id, limit=10000):
            if source_id in page.sources:
                related_pages.append({"id": page.id, "title": page.title})

        ok = store.delete_raw(source_id, owner_account_id=owner, kb_id=kb_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "source 不存在"}, status_code=404)
        return {"ok": True, "deleted_source_id": source_id, "related_pages": related_pages}

    @router.get("/sources/{source_id}/file")
    async def wiki_source_file(source_id: str, request: Request):
        """返回原始数据源文件，供浏览器/本地程序打开。"""
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        owner = _owner(request)
        kb_id = _kb_id(request)
        raw = store.load_raw(source_id, owner, kb_id)
        if raw is None or not raw.original_path:
            return JSONResponse({"ok": False, "error": "源文件不存在"}, status_code=404)
        original_path = _resolve_original_path(raw, owner, kb_id)
        if original_path is None:
            return JSONResponse({"ok": False, "error": "源文件已丢失"}, status_code=404)
        media_type = raw.file_type or guess_mime_type(str(original_path))
        return FileResponse(
            str(original_path),
            media_type=media_type,
            filename=raw.title or original_path.name,
        )

    @router.post("/ingest")
    async def wiki_ingest(request: Request):
        compiler = getattr(crew, "_wiki_compiler", None)
        if compiler is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
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
                        "kind": "wiki_ingest_progress",
                        "body": {
                            "stage": stage,
                            "percent": percent,
                            "label": label,
                            "source_id": source_id,
                            "detail": detail,
                        },
                        "is_final": stage == "done",
                        "sequence": 0,
                        "session_id": session_id,
                    },
                )
        else:
            _progress = None

        cancel_event = asyncio.Event()
        task_key = _task_key(owner, source_id)

        async def _ingest_with_cancel() -> Any:
            ingest_tasks[task_key] = asyncio.current_task()  # type: ignore[assignment]
            try:
                return await compiler.ingest(
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
                    "kind": "wiki_ingest_progress",
                    "body": {
                        "stage": "done",
                        "percent": 100,
                        "label": "编译完成",
                        "source_id": source_id,
                        "error": result.issues[0],
                    },
                    "is_final": True,
                    "sequence": 0,
                    "session_id": session_id,
                },
            )
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        return {"ok": True, **result.to_dict()}

    @router.post("/ingest/cancel")
    async def wiki_cancel_ingest(request: Request):
        data = await request.json()
        source_id = str(data.get("source_id", ""))
        if not source_id:
            return JSONResponse({"ok": False, "error": "缺少 source_id"}, status_code=400)
        owner = _owner(request)
        key = _task_key(owner, source_id)
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
    async def wiki_compile(request: Request):
        compiler = getattr(crew, "_wiki_compiler", None)
        if compiler is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        result = await compiler.compile_all(
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
        )
        return {"ok": True, "ingested": result.ingested, "errors": result.errors}

    @router.get("/graph")
    async def wiki_graph(request: Request):
        store = getattr(crew, "_wiki_store", None)
        if store is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        graph = await asyncio.to_thread(
            store.get_graph,
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
        )
        return {"ok": True, "graph": graph.to_dict()}

    @router.get("/query")
    async def wiki_query(request: Request):
        querier = getattr(crew, "_wiki_querier", None)
        if querier is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        q = request.query_params.get("q", "")
        if not q:
            return JSONResponse({"ok": False, "error": "缺少 q"}, status_code=400)
        result = querier.query(
            q,
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
        )
        return {"ok": True, **result}

    @router.post("/lint")
    async def wiki_lint(request: Request):
        compiler = getattr(crew, "_wiki_compiler", None)
        if compiler is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
        deep = request.query_params.get("deep", "").lower() in ("1", "true", "yes")
        issues = await compiler.lint(
            owner_account_id=_owner(request),
            kb_id=_kb_id(request),
            deep=deep,
        )
        return {"ok": True, "issues": issues}

    @router.post("/upload")
    async def wiki_upload(request: Request):
        from pathlib import Path

        from crew.wiki.config import WikiConfig
        from crew.wiki.multimodal import MediaUnderstandingError, is_image_mime, is_video_mime

        store = getattr(crew, "_wiki_store", None)
        compiler = getattr(crew, "_wiki_compiler", None)
        if store is None or compiler is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)

        wiki_config: WikiConfig = getattr(crew, "config", None)
        wiki_config = wiki_config.wiki if wiki_config is not None else WikiConfig()

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

        import time
        import uuid

        source_id = f"upload_{uuid.uuid4().hex[:12]}"
        file_type = guess_mime_type(filename)
        source_kind = classify_file(filename, file_type)
        source_dir = store._source_dir(source_kind, _owner(request), kb_id)

        # 媒体文件：保存原文件，按配置决定是否自动理解
        if is_image_mime(file_type) or is_video_mime(file_type):
            if not wiki_config.multimodal.enabled:
                return JSONResponse({"ok": False, "error": "Wiki 多模态功能未启用"}, status_code=400)

            source_type = "image" if is_image_mime(file_type) else "video"
            ext = Path(filename).suffix.lower() or ".bin"
            original_path = source_dir / f"{source_id}-{filename_from_title(Path(filename).stem)}{ext}"
            original_path.write_bytes(content)

            raw = RawSource(
                id=source_id,
                title=filename,
                source_type=source_type,  # type: ignore[arg-type]
                parsed_path="",
                original_path=str(original_path),
                file_type=file_type,
                size=len(content),
                created_at=time.time(),
                source_kind=source_kind,
                source_platform="local",
                adapter_name="builtin-file",
                original_ref=filename,
            )
            store.save_raw(raw, _owner(request), kb_id)

            # 图片默认自动理解；视频需 auto_video + video_upload_confirmed 同时满足
            auto_process = False
            if source_type == "image" and wiki_config.multimodal.auto_image:
                auto_process = True
            if source_type == "video" and wiki_config.multimodal.auto_video and wiki_config.multimodal.video_upload_confirmed:
                auto_process = True

            if auto_process:
                try:
                    from crew.wiki.multimodal import describe_media

                    prompt = (
                        wiki_config.multimodal.prompt_image
                        if source_type == "image"
                        else wiki_config.multimodal.prompt_video
                    )
                    description = describe_media(
                        str(original_path),
                        file_type,
                        prompt,
                        confirm_upload=(source_type == "video"),
                    )
                except MediaUnderstandingError as exc:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": str(exc),
                            "source_id": source_id,
                            "source_type": source_type,
                            "needs_confirmation": exc.needs_confirmation,
                        },
                        status_code=400,
                    )
                raw.parsed_path = store.save_parsed_markdown(
                    source_id,
                    description,
                    owner_account_id=_owner(request),
                    kb_id=kb_id,
                )
                store.save_raw(raw, _owner(request), kb_id)
                result = await compiler.ingest(
                    source_id,
                    owner_account_id=_owner(request),
                    kb_id=kb_id,
                )
                return {
                    "ok": True,
                    "source_id": source_id,
                    "title": filename,
                    "source_type": source_type,
                    "ingested": True,
                    "pages": [p.to_dict() for p in result.pages],
                    "issues": result.issues,
                }

            return {
                "ok": True,
                "source_id": source_id,
                "title": filename,
                "source_type": source_type,
                "ingested": False,
                "needs_confirmation": source_type == "video",
            }

        # 文本/文档：先保存原文件，再尝试解析；解析失败保留 raw source 供 Agent 挽救
        ext = Path(filename).suffix.lower() or ".bin"
        original_path = source_dir / f"{source_id}-{filename_from_title(Path(filename).stem)}{ext}"
        original_path.write_bytes(content)

        raw = RawSource(
            id=source_id,
            title=filename,
            source_type="upload",
            parsed_path="",
            original_path=str(original_path),
            file_type=file_type,
            size=len(content),
            created_at=time.time(),
            source_kind=source_kind,
            source_platform="local",
            adapter_name="builtin-file",
            original_ref=filename,
        )
        store.save_raw(raw, _owner(request), kb_id)

        try:
            # 解析是 CPU 密集型同步调用，丢线程池避免阻塞事件循环拖垮整个网关。
            from crew.security.context import SecurityContext
            from crew.security.launch import compile_process_launch, use_process_launch

            security_context = SecurityContext(
                os_user=getpass.getuser(),
                owner_account_id=_owner(request),
                workspace_id="wiki",
                workspace_root=source_dir.resolve(),
                session_id="wiki-upload",
                request_id=uuid.uuid4().hex,
                task_id="",
                cwd=source_dir.resolve(),
            )
            launch = compile_process_launch(
                security_context,
                crew.security_service.mode_for(security_context),
                db_path=crew.security_service.db_path,
                audit=crew.security_service.audit,
            )
            with use_process_launch(launch):
                text = await asyncio.to_thread(parse_document_from_bytes, content, filename)
        except asyncio.CancelledError:
            # 请求中断/取消时 raw 不能留在 pending（永远不会被重试，前端也看不到），
            # 标记为 failed，让 Agent / 用户能发现并按失败处理。
            raw.parse_status = "failed"
            raw.parse_error = "解析被中断，请重新上传或让 Agent 重新解析"
            store.save_raw(raw, _owner(request), kb_id)
            raise
        except MissingDependencyError as exc:
            error_msg = str(exc)
            raw.parse_status = "failed"
            raw.parse_error = f"缺少依赖: {error_msg}"
            store.save_raw(raw, _owner(request), kb_id)
            return JSONResponse(
                {
                    "ok": False,
                    "error": error_msg,
                    "error_code": "MISSING_DEPENDENCY",
                    "dependency": exc.dependency,
                    "install_command": exc.install_command,
                    "source_id": source_id,
                },
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"解析失败: {exc}"
            log.warning("Wiki 上传解析失败 source=%s: %s", source_id, error_msg)
            raw.parse_status = "failed"
            raw.parse_error = error_msg
            store.save_raw(raw, _owner(request), kb_id)
            return {
                "ok": True,
                "source_id": source_id,
                "title": filename,
                "needs_agent_review": True,
                "error": error_msg,
                "message": "文件已保存，但自动解析失败，已交给 Wiki Agent 处理。",
            }

        raw.parsed_path = store.save_parsed_markdown(
            source_id,
            text,
            owner_account_id=_owner(request),
            kb_id=kb_id,
        )
        raw.parse_status = "parsed"
        store.save_raw(raw, _owner(request), kb_id)

        return {"ok": True, "source_id": source_id, "title": filename}

    @router.post("/capture")
    async def wiki_capture(request: Request):
        """把一段文本（如浏览器标签页正文）存为不可变 RawSource 并发布 Source 页面。

        与 wiki_capture_text 工具共用 crew.wiki.capture 的入库流水线，
        这里只做 HTTP 参数解析与响应包装，供面板「存入 Wiki」使用。
        """
        store = getattr(crew, "_wiki_store", None)
        compiler = getattr(crew, "_wiki_compiler", None)
        if store is None or compiler is None:
            return JSONResponse({"ok": False, "error": "Wiki 未启用"}, status_code=503)
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
            outcome = capture_text_source(
                store,
                compiler,
                title=title,
                content=content,
                owner_account_id=owner,
                kb_id=kb_id,
                source_type="paste",
                source_platform="web",
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
