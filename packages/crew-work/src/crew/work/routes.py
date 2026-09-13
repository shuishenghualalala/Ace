"""Work REST API route contribution owned by the Work feature."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from functools import wraps
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.features.runtime import FeatureLeaseUnavailableError
from crew.state.logging import get_logger
from crew.work.items import WorkItemConflictError
from crew.work.preferences import WorkPreferenceConflictError
from crew.work.service import WORK_SERVICE_KEY

log = get_logger("work.routes")


def create_work_router(crew) -> APIRouter:
    """Create the Work router without accepting owner identity from payloads."""
    router = APIRouter(prefix="/api/work", tags=["work"])

    active_service: ContextVar[Any | None] = ContextVar(
        "work_gateway_service", default=None
    )

    @asynccontextmanager
    async def _service_context(request: Request | None = None):
        plugins = getattr(crew, "plugins", None)
        acquire = getattr(plugins, "acquire_service_lease", None)
        binding = getattr(getattr(request, "state", None), "route_binding", None)
        generation = getattr(binding, "generation", None)
        if binding is not None and generation is None:
            yield None
            return
        if callable(acquire):
            try:
                acquired = acquire(
                    WORK_SERVICE_KEY,
                    label="gateway:work",
                    generation=generation,
                )
            except (FeatureLeaseUnavailableError, TypeError):
                acquired = None
            if acquired is None:
                if generation is not None:
                    yield None
                    return
                fallback = getattr(crew, "work_service", None)
                if fallback is None:
                    yield None
                    return
                yield fallback
                return
            service, lease = acquired
            async with lease:
                yield service
            return
        if generation is not None:
            yield None
            return
        yield getattr(crew, "work_service", None)

    def _service() -> Any | None:
        current = active_service.get()
        if current is not None:
            return current
        plugins = getattr(crew, "plugins", None)
        resolver = getattr(plugins, "resolve_service", None)
        if callable(resolver):
            resolved = resolver(WORK_SERVICE_KEY, default=None)
            if resolved is not None:
                return resolved
        return getattr(crew, "work_service", None)

    def _unavailable() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "Work 未启用"}, status_code=503)

    def _owner(request: Request) -> str:
        account = getattr(getattr(request, "state", None), "account", None)
        owner = str(getattr(account, "owner_account_id", "") or "").strip()
        if not owner:
            raise RuntimeError("authenticated owner is missing from request state")
        return owner

    def _with_work(handler):
        @wraps(handler)
        async def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next(
                    (arg for arg in args if isinstance(arg, Request)),
                    None,
                )
            async with _service_context(request) as service:
                if service is None:
                    return _unavailable()
                token = active_service.set(service)
                try:
                    return await handler(*args, **kwargs)
                finally:
                    active_service.reset(token)

        return wrapped

    def _error(exc: Exception) -> JSONResponse:
        if isinstance(exc, KeyError):
            status_code = 404
        elif isinstance(exc, (WorkItemConflictError, WorkPreferenceConflictError)):
            status_code = 409
        elif isinstance(exc, PermissionError):
            status_code = 403
        elif isinstance(exc, RuntimeError):
            status_code = 503
        else:
            status_code = 422
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status_code)

    async def _notify_item(
        owner_account_id: str, action: str, item_id: str, version: int
    ) -> None:
        """Broadcast a lightweight invalidation event; never roll back a successful write."""
        try:
            await crew.connections.notify_owner(
                owner_account_id,
                {
                    "kind": "work_event",
                    "body": {
                        "entity": "work_item",
                        "action": action,
                        "item_id": item_id,
                        "version": version,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - persistence already succeeded
            log.warning("WorkItem 事件推送失败: error_type=%s", type(exc).__name__)

    @router.post("/sessions", status_code=201)
    @_with_work
    async def create_work_session(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            created = _service().create_session(
                owner_account_id=owner,
                workspace_id=str(payload.get("workspace_id") or "default"),
                title=str(payload.get("title") or "新对话"),
            )
        except KeyError:
            return JSONResponse(
                {"ok": False, "error": "工作空间不存在"},
                status_code=404,
            )
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=422,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=503,
            )
        return JSONResponse(created, status_code=201)

    @router.get("/history")
    @_with_work
    async def work_history(
        request: Request,
        include_archived: bool = False,
    ) -> JSONResponse:
        owner = _owner(request)
        entries = _service().history(owner, include_archived=include_archived)
        return JSONResponse({"entries": entries, "count": len(entries)})

    @router.post("/items", status_code=201)
    @_with_work
    async def create_item(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            item = _service().create_item(owner_account_id=owner, values=payload)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        await _notify_item(owner, "created", item.item_id, item.version)
        return JSONResponse(asdict(item), status_code=201)

    @router.get("/items")
    @_with_work
    async def list_items(
        request: Request,
        workspace_id: str | None = None,
        business_status: str | None = None,
        disposition: str | None = None,
    ) -> JSONResponse:
        owner = _owner(request)
        filters = {
            key: value
            for key, value in {
                "workspace_id": workspace_id,
                "business_status": business_status,
                "disposition": disposition,
            }.items()
            if value is not None
        }
        try:
            items = _service().list_items(owner, **filters)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(item) for item in items], "count": len(items)}
        )

    @router.get("/items/{item_id}")
    @_with_work
    async def get_item(request: Request, item_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            item = _service().get_item(owner, item_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(item))

    @router.patch("/items/{item_id}")
    @_with_work
    async def update_item(request: Request, item_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        changes = dict(payload)
        try:
            expected_version = int(changes.pop("expected_version"))
            item = _service().update_item(
                owner_account_id=owner,
                item_id=item_id,
                expected_version=expected_version,
                changes=changes,
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkItemConflictError) as exc:
            return _error(exc)
        await _notify_item(owner, "updated", item.item_id, item.version)
        return JSONResponse(asdict(item))

    @router.post("/items/{item_id}/actions")
    @_with_work
    async def act_on_item(request: Request, item_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            item = _service().act_on_item(
                owner_account_id=owner,
                item_id=item_id,
                expected_version=int(payload.get("expected_version")),
                action=str(payload.get("action") or ""),
                due_at=payload.get("due_at"),
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkItemConflictError) as exc:
            return _error(exc)
        await _notify_item(owner, str(payload.get("action")), item.item_id, item.version)
        return JSONResponse(asdict(item))

    @router.post("/items/{item_id}/processing-session")
    @_with_work
    async def start_item_processing_session(
        request: Request,
        item_id: str,
        payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            item = _service().start_item_processing_session(
                owner_account_id=owner,
                item_id=item_id,
                expected_version=int(payload.get("expected_version")),
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkItemConflictError) as exc:
            return _error(exc)
        await _notify_item(owner, "processing_session_started", item.item_id, item.version)
        return JSONResponse(asdict(item))

    @router.get("/items/{item_id}/activity")
    @_with_work
    async def item_activity(request: Request, item_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            events = _service().list_item_activity(owner, item_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"events": [asdict(event) for event in events], "count": len(events)}
        )

    @router.post("/items/{item_id}/knowledge", status_code=201)
    @_with_work
    async def save_item_knowledge(
        request: Request,
        item_id: str,
        payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            page = _service().save_item_knowledge(
                owner,
                item_id,
                full=bool(payload.get("full", True)),
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        page_dict = page.to_dict() if hasattr(page, "to_dict") else page
        return JSONResponse({"page": page_dict}, status_code=201)

    @router.delete("/items/{item_id}")
    @_with_work
    async def delete_item(request: Request, item_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            if payload.get("confirm") != "delete_work_item":
                raise ValueError("confirm must be delete_work_item")
            expected_version = int(payload.get("expected_version"))
            _service().delete_item(
                owner_account_id=owner,
                item_id=item_id,
                expected_version=expected_version,
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkItemConflictError) as exc:
            return _error(exc)
        await _notify_item(owner, "deleted", item_id, expected_version)
        return JSONResponse(
            {"ok": True, "deleted_scope": ["work_item", "work_item_activity"]}
        )

    @router.get("/mentions")
    @_with_work
    async def search_mentions(
        request: Request,
        q: str = "",
        workspace_id: str | None = None,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            items = _service().search_mentions(owner, q, workspace_id=workspace_id)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"items": items, "count": len(items)})

    @router.post("/references", status_code=201)
    @_with_work
    async def create_reference(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            ref = _service().create_reference(
                owner_account_id=owner,
                target_session_id=str(payload.get("target_session_id") or ""),
                reference_type=str(payload.get("reference_type") or ""),
                source_id=str(payload.get("source_id") or ""),
                target_item_id=payload.get("target_item_id"),
                source_link=str(payload.get("source_link") or ""),
                snapshot_summary=str(payload.get("snapshot_summary") or ""),
            )
        except (KeyError, RuntimeError, TypeError, ValueError, PermissionError) as exc:
            return _error(exc)
        return JSONResponse(asdict(ref), status_code=201)

    @router.post("/references/agent-session", status_code=201)
    @_with_work
    async def create_agent_session_reference(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            ref = _service().create_agent_session_reference(
                owner_account_id=owner,
                target_session_id=str(payload.get("target_session_id") or ""),
                source_session_id=str(payload.get("source_session_id") or ""),
            )
        except (KeyError, RuntimeError, TypeError, ValueError, PermissionError) as exc:
            return _error(exc)
        return JSONResponse(asdict(ref), status_code=201)

    @router.post("/references/{reference_id}/refresh")
    @_with_work
    async def refresh_reference(request: Request, reference_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            ref = _service().refresh_reference(owner, reference_id)
        except (KeyError, RuntimeError, ValueError, PermissionError) as exc:
            return _error(exc)
        return JSONResponse(asdict(ref))

    @router.get("/references")
    @_with_work
    async def list_references(
        request: Request,
        target_session_id: str = "",
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            refs = _service().list_references(owner, target_session_id)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(ref) for ref in refs], "count": len(refs)}
        )

    @router.delete("/references/{reference_id}")
    @_with_work
    async def delete_reference(request: Request, reference_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            _service().delete_reference(owner, reference_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"ok": True})

    @router.get("/preferences/settings")
    @_with_work
    async def get_preference_settings(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().get_preference_settings(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.put("/preferences/settings")
    @_with_work
    async def set_preference_settings(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().set_preference_settings(
                owner, bool(payload.get("auto_learning_enabled"))
            )
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.get("/preferences")
    @_with_work
    async def list_preferences(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            prefs = _service().list_preferences(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(pref) for pref in prefs], "count": len(prefs)}
        )

    @router.post("/preferences", status_code=201)
    @_with_work
    async def create_preference(request: Request, payload: dict) -> JSONResponse:
        """Create one owner-scoped manual Work preference."""
        owner = _owner(request)
        try:
            pref = _service().create_preference(
                owner_account_id=owner,
                category=str(payload.get("category") or ""),
                content=str(payload.get("content") or ""),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(pref), status_code=201)

    @router.patch("/preferences/{preference_id}")
    @_with_work
    async def update_preference(
        request: Request, preference_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        changes = dict(payload)
        try:
            expected_version = int(changes.pop("expected_version"))
            pref = _service().update_preference(
                owner_account_id=owner,
                preference_id=preference_id,
                expected_version=expected_version,
                **changes,
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkPreferenceConflictError) as exc:
            return _error(exc)
        return JSONResponse(asdict(pref))

    @router.delete("/preferences/{preference_id}")
    @_with_work
    async def delete_preference(
        request: Request, preference_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            _service().delete_preference(
                owner_account_id=owner,
                preference_id=preference_id,
                expected_version=int(payload.get("expected_version")),
            )
        except (KeyError, RuntimeError, TypeError, ValueError, WorkPreferenceConflictError) as exc:
            return _error(exc)
        return JSONResponse({"ok": True})

    @router.get("/sources")
    @_with_work
    async def list_sources(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            states = _service().list_sources(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(s) for s in states], "count": len(states)}
        )

    @router.put("/sources/{connector_key}")
    @_with_work
    async def toggle_source(request: Request, connector_key: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            state = _service().toggle_source(
                owner, connector_key, bool(payload.get("enabled"))
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(state))

    @router.post("/sources/{connector_key}/refresh")
    @_with_work
    async def refresh_source(request: Request, connector_key: str) -> JSONResponse:
        owner = _owner(request)
        try:
            state = _service().refresh_source(owner, connector_key)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(state))

    @router.delete("/sources/{connector_key}/data")
    @_with_work
    async def delete_source_local_data(
        request: Request,
        connector_key: str,
        payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            if payload.get("confirm") != "delete_work_source_local_data":
                raise ValueError("confirm must be delete_work_source_local_data")
            deleted = _service().delete_source_local_data(owner, connector_key)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"ok": True, "deleted_records": deleted})

    @router.get("/sources/records")
    @_with_work
    async def list_source_records(
        request: Request,
        connector_key: str | None = None,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            records = _service().list_source_records(owner, connector_key=connector_key)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(r) for r in records], "count": len(records)}
        )

    @router.post("/sources/records/{record_id}/resolve")
    @_with_work
    async def resolve_source_conflict(
        request: Request, record_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            record = _service().resolve_source_conflict(
                owner, record_id, str(payload.get("resolution") or "")
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(record))

    @router.get("/dashboard")
    @_with_work
    async def get_dashboard(
        request: Request,
        workspace_id: str | None = None,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            brief = _service().get_dashboard(owner, workspace_id=workspace_id)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"brief": asdict(brief) if brief else None})

    @router.post("/dashboard/refresh")
    @_with_work
    async def refresh_dashboard(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            brief = _service().refresh_dashboard(
                owner,
                content=payload.get("content") if "content" in payload else None,
                input_version=payload.get("input_version"),
                workspace_id=payload.get("workspace_id"),
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"brief": asdict(brief)})

    @router.post("/dashboard/archive")
    @_with_work
    async def archive_dashboard(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            brief = _service().archive_dashboard(
                owner, workspace_id=payload.get("workspace_id"),
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"brief": asdict(brief)})

    @router.get("/reports")
    @_with_work
    async def get_period_report(
        request: Request,
        period: str,
        anchor: str,
        workspace_id: str | None = None,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            report = _service().get_period_report(
                owner,
                period=period,
                anchor=anchor,
                workspace_id=workspace_id,
            )
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"report": asdict(report)})

    @router.post("/reports/archive")
    @_with_work
    async def archive_period_report(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            report = _service().archive_period_report(
                owner,
                period=str(payload.get("period") or ""),
                anchor=str(payload.get("anchor") or ""),
                workspace_id=payload.get("workspace_id"),
            )
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"report": asdict(report)})

    @router.get("/settings")
    @_with_work
    async def get_account_settings(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().get_account_settings(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.put("/settings")
    @_with_work
    async def update_account_settings(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().update_account_settings(owner, **payload)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.get("/settings/workspaces/{workspace_id}")
    @_with_work
    async def get_workspace_settings(request: Request, workspace_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().get_workspace_settings(owner, workspace_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.put("/settings/workspaces/{workspace_id}")
    @_with_work
    async def update_workspace_settings(
        request: Request, workspace_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            settings = _service().update_workspace_settings(owner, workspace_id, **payload)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(settings)

    @router.get("/templates")
    @_with_work
    async def list_templates(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            templates = _service().list_templates(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(
            {"items": [asdict(t) for t in templates], "count": len(templates)}
        )

    @router.post("/templates", status_code=201)
    @_with_work
    async def create_template(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            template = _service().create_template(
                owner,
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                category=str(payload.get("category") or ""),
                blueprint=payload.get("blueprint") or {},
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(template), status_code=201)

    @router.get("/templates/{template_id}")
    @_with_work
    async def get_template(request: Request, template_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            template = _service().get_template(owner, template_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(template))

    @router.patch("/templates/{template_id}")
    @_with_work
    async def update_template(
        request: Request, template_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            template = _service().update_template(owner, template_id, **payload)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(template))

    @router.delete("/templates/{template_id}")
    @_with_work
    async def delete_template(
        request: Request, template_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            _service().delete_template(owner, template_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"ok": True})

    @router.post("/templates/{template_id}/instantiate", status_code=201)
    @_with_work
    async def instantiate_template(
        request: Request, template_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            item = _service().instantiate_template(
                owner,
                template_id,
                workspace_id=str(payload.get("workspace_id") or "default"),
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(asdict(item), status_code=201)

    @router.get("/knowledge/personal")
    @_with_work
    async def list_personal_knowledge(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            pages = _service().list_personal_knowledge(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        serialized = [p.to_dict(brief=True) if hasattr(p, "to_dict") else p for p in pages]
        return JSONResponse({"items": serialized, "count": len(pages)})
    @router.post("/knowledge/personal", status_code=201)
    @_with_work
    async def save_personal_knowledge(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            page = _service().save_personal_knowledge(
                owner,
                title=str(payload.get("title") or ""),
                content=str(payload.get("content") or ""),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        page_dict = page.to_dict() if hasattr(page, "to_dict") else page
        return JSONResponse({"page": page_dict}, status_code=201)
    @router.get("/knowledge/organization")
    @_with_work
    async def list_organization_knowledge(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            items = _service().list_organization_knowledge(owner)
            available = _service().organization_knowledge_available()
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"items": items, "count": len(items), "available": available})

    @router.post("/knowledge/publish", status_code=201)
    @_with_work
    async def request_publish(request: Request, payload: dict) -> JSONResponse:
        owner = _owner(request)
        try:
            req = _service().request_publish(
                owner,
                page_id=str(payload.get("page_id") or ""),
                target=str(payload.get("target") or ""),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(req, status_code=201)

    @router.get("/knowledge/publish")
    @_with_work
    async def list_publish_requests(request: Request) -> JSONResponse:
        owner = _owner(request)
        try:
            reqs = _service().list_publish_requests(owner)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"items": reqs, "count": len(reqs)})

    @router.get("/workspaces/{workspace_id}/index")
    @_with_work
    async def get_index_status(request: Request, workspace_id: str) -> JSONResponse:
        owner = _owner(request)
        try:
            status = _service().get_index_status(owner, workspace_id)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(status)

    @router.put("/workspaces/{workspace_id}/index")
    @_with_work
    async def set_index_status(
        request: Request, workspace_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            status = _service().set_index_status(
                owner, workspace_id,
                enabled=payload.get("enabled"),
                state=payload.get("state"),
            )
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse(status)

    @router.delete("/workspaces/{workspace_id}/index")
    @_with_work
    async def delete_index_status(
        request: Request, workspace_id: str, payload: dict,
    ) -> JSONResponse:
        owner = _owner(request)
        try:
            _service().delete_index_status(owner, workspace_id)
        except (RuntimeError, ValueError) as exc:
            return _error(exc)
        return JSONResponse({"ok": True})

    return router
