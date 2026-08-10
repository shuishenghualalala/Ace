"""Desktop 本地站点 REST API。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from crew.gateway.auth import account_from_request


def create_sites_router(crew) -> APIRouter:
    router = APIRouter(prefix="/api/sites", tags=["sites"])

    def owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    def manager():
        value = getattr(crew, "sites", None)
        if value is None:
            raise RuntimeError("站点模块未初始化")
        return value

    @router.get("")
    async def list_sites(request: Request):
        workspace_id = request.query_params.get("workspace_id") or None
        return {"ok": True, "sites": manager().store.list_sites(owner(request), workspace_id)}

    @router.get("/inspirations")
    async def list_inspirations(request: Request):
        return {"ok": True, "inspirations": manager().list_inspirations(owner(request))}

    @router.get("/inspirations/{inspiration_id}")
    async def get_inspiration(inspiration_id: str, request: Request):
        try:
            return {"ok": True, "inspiration": manager().get_inspiration(owner(request), inspiration_id)}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.delete("/inspirations/{inspiration_id}")
    async def delete_inspiration(inspiration_id: str, request: Request):
        try:
            manager().delete_inspiration(owner(request), inspiration_id)
            return {"ok": True}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.post("/inspirations/{inspiration_id}/export")
    async def prepare_inspiration_export(inspiration_id: str, request: Request):
        try:
            archive = manager().export_inspiration(owner(request), inspiration_id)
            return {"ok": True, "archive_path": str(archive), "filename": archive.name}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.post("/inspirations/{inspiration_id}/annotations")
    async def create_inspiration_annotation(inspiration_id: str, request: Request):
        try:
            data = await request.json()
            if inspiration_id.startswith("widget_"):
                widget = manager().blueprint.store.get_widget(owner(request), inspiration_id)
                inspiration = {
                    "kind": "widget", "updatedAt": widget["updatedAt"],
                    "resourceRevision": widget["resourceRevision"],
                }
            else:
                inspiration = manager().get_inspiration(owner(request), inspiration_id)
            revision_id = (
                inspiration.get("site", {}).get("active_release_id", "")
                if inspiration["kind"] == "site"
                else str(data.get("revisionId") or inspiration.get("resourceRevision")
                         or inspiration.get("updatedAt") or "")
            )
            annotation = manager().store.create_inspiration_annotation(
                owner(request), inspiration_id, inspiration["kind"], revision_id, data,
            )
            return {"ok": True, "annotation": annotation}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.patch("/inspirations/{inspiration_id}/annotations/{annotation_id}")
    async def update_inspiration_annotation(
        inspiration_id: str, annotation_id: str, request: Request,
    ):
        try:
            data = await request.json()
            annotation = manager().store.get_inspiration_annotation(owner(request), annotation_id)
            if annotation["inspirationId"] != inspiration_id:
                raise KeyError("灵感注释不存在")
            updated = manager().store.update_inspiration_annotation_status(
                owner(request), annotation_id, str(data.get("status") or ""),
            )
            return {"ok": True, "annotation": updated}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/canvases")
    async def list_canvases(request: Request):
        return {"ok": True, "canvases": manager().blueprint.store.list_canvases(owner(request))}

    @router.get("/canvases/{canvas_id}")
    async def get_canvas(canvas_id: str, request: Request):
        try:
            canvas = manager().blueprint.store.get_canvas(owner(request), canvas_id)
            widgets = {
                placement["widgetId"]: manager().blueprint.store.get_widget(
                    owner(request), placement["widgetId"]
                )
                for placement in canvas["placements"]
            }
            for widget in widgets.values():
                widget["validation"] = manager().blueprint.validate_widget_file(widget)
            return {"ok": True, "canvas": canvas, "widgets": widgets}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.get("/canvases/{canvas_id}/render")
    async def render_canvas(canvas_id: str, request: Request):
        try:
            return HTMLResponse(
                manager().canvas_html(owner(request), canvas_id),
                headers={"Cache-Control": "no-store"},
            )
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.patch("/canvases/{canvas_id}/placements/{mount_id}")
    async def update_canvas_placement(canvas_id: str, mount_id: str, request: Request):
        try:
            data = await request.json()
            placement = manager().blueprint.store.get_placement(owner(request), mount_id)
            if placement["canvasId"] != canvas_id:
                raise KeyError("看板组件位置不存在")
            layout = None
            if "layout" in data:
                layout = manager().blueprint.normalize_layout(data.get("layout"))
            updated = manager().blueprint.store.update_placement(
                owner(request), mount_id, layout=layout,
                z_order=data.get("zOrder") if isinstance(data.get("zOrder"), int) else None,
                view_state=data.get("viewState") if isinstance(data.get("viewState"), dict) else None,
            )
            return {"ok": True, "placement": updated}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str, request: Request):
        try:
            widget = manager().blueprint.store.get_widget(owner(request), widget_id)
            widget["validation"] = manager().blueprint.validate_widget_file(widget)
            return {"ok": True, "widget": widget}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.get("/widgets/{widget_id}/render")
    async def render_widget(widget_id: str, request: Request):
        return await render_widget_asset(widget_id, "index.html", request)

    @router.get("/widgets/{widget_id}/render/{asset_path:path}")
    async def render_widget_asset(widget_id: str, asset_path: str, request: Request):
        try:
            widget = manager().blueprint.store.get_widget(owner(request), widget_id)
            root = Path(widget["workspacePath"]).resolve()
            requested = (root / (asset_path or "index.html")).resolve()
            try:
                requested.relative_to(root)
            except ValueError as exc:
                raise ValueError("无效的 Widget 资源路径") from exc
            if not requested.is_file():
                raise KeyError("Widget 资源不存在")
            if requested.name.lower() != "index.html":
                return FileResponse(requested, headers={"Cache-Control": "no-store"})
            placement = None
            mount_id = request.query_params.get("mount_id") or ""
            if mount_id:
                placement = manager().blueprint.store.get_placement(owner(request), mount_id)
                if placement["widgetId"] != widget_id:
                    raise KeyError("Widget placement 不匹配")
            html = manager().blueprint.runtime_html(widget, placement)
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.post("/widgets/{widget_id}/emit")
    async def emit_widget_event(widget_id: str, request: Request):
        try:
            data = await request.json()
            binding = manager().blueprint.store.active_binding_for_widget(owner(request), widget_id)
            if not binding:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            f"Widget {widget_id} 未连接数据源。请在看板绑定对话中创建或修复 "
                            "Automation → Binding → Widget 连接后再刷新。"
                        ),
                        "code": "widget_binding_missing",
                        "widgetId": widget_id,
                    },
                    status_code=409,
                )
            run = await manager().blueprint.run_automation(
                owner(request), binding["automationId"], run_input=data.get("value"),
                trigger_kind="widget_event",
            )
            widget = manager().blueprint.store.get_widget(owner(request), widget_id)
            return {"ok": run["status"] == "succeeded", "run": run, "widget": widget}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.post("/automations/{automation_id}/run")
    async def run_automation(automation_id: str, request: Request):
        try:
            data = await request.json()
            run = await manager().blueprint.run_automation(
                owner(request), automation_id, run_input=data.get("runInput"), trigger_kind="manual"
            )
            return {"ok": run["status"] == "succeeded", "run": run}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/{site_id}")
    async def get_site(site_id: str, request: Request):
        try:
            site = manager().store.get_site(owner(request), site_id)
            releases = manager().store.list_releases(owner(request), site_id)
            annotations = manager().store.list_annotations(owner(request), site_id)
            return {"ok": True, "site": site, "releases": releases, "annotations": annotations}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.delete("/{site_id}")
    async def delete_site(site_id: str, request: Request):
        try:
            manager().store.get_site(owner(request), site_id)
            manager().delete(owner(request), site_id)
            return {"ok": True}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.post("/{site_id}/publish")
    async def publish_site(site_id: str, request: Request):
        """Desktop 手动重发；Agent 常规发布走 publish_site 工具。"""
        try:
            data = await request.json()
            current = manager().store.get_site(owner(request), site_id)
            workspace = crew.workspace_store.get(current["workspace_id"], owner_account_id=owner(request))
            workspace_root = workspace.get("root_path") or current["source_path"]
            from crew.security.context import build_gateway_security_context
            from crew.security.launch import compile_process_launch, use_process_launch

            context = build_gateway_security_context(
                crew.workspace_store,
                owner_account_id=owner(request),
                workspace_id=current["workspace_id"],
                session_id=current["session_id"] or f"site-{site_id}",
                request_id=uuid4().hex,
                cwd=workspace_root,
            )
            launch = compile_process_launch(
                context,
                crew.security_service.mode_for(context),
                db_path=crew.security_service.db_path,
                audit=crew.security_service.audit,
            )
            with use_process_launch(launch):
                result = await manager().publish(
                    owner=owner(request), workspace_id=current["workspace_id"],
                    session_id=current["session_id"], workspace_root=workspace_root,
                    source_path=current["source_path"], name=str(data.get("name") or current["name"]),
                    description=str(data.get("description") or current.get("description") or ""),
                    build_command=str(data.get("build_command") or current["build_command"]),
                    output_directory=str(data.get("output_directory") or current["output_directory"]),
                    site_id=site_id,
                )
            return {"ok": True, **result}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/{site_id}/preview/{asset_path:path}")
    async def preview(site_id: str, asset_path: str, request: Request):
        try:
            path = manager().release_file(owner(request), site_id, asset_path)
            if path.name.lower() == "index.html":
                html = path.read_text(encoding="utf-8")
                bridge = r"""
<script data-ace-site-annotation-bridge>
(() => {
  let enabled = false;
  let hovered = null;
  parent.postMessage({ type: 'ace-site-preview-ready' }, '*');
  const outline = (el, on) => { if (el) el.style.outline = on ? '2px solid #5b7cff' : ''; };
  window.addEventListener('message', (event) => {
    if (event.data?.type !== 'ace-site-annotation-mode') return;
    enabled = Boolean(event.data.enabled);
    if (!enabled) { outline(hovered, false); hovered = null; }
  });
  document.addEventListener('mouseover', (event) => {
    if (!enabled || !(event.target instanceof Element)) return;
    outline(hovered, false); hovered = event.target; outline(hovered, true);
  }, true);
  document.addEventListener('click', (event) => {
    if (!enabled || !(event.target instanceof Element)) return;
    event.preventDefault(); event.stopPropagation();
    const el = event.target;
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && parts.length < 6; node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      if (node.id) { part += '#' + CSS.escape(node.id); parts.unshift(part); break; }
      const siblings = node.parentElement ? [...node.parentElement.children].filter(x => x.tagName === node.tagName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    const box = el.getBoundingClientRect();
    parent.postMessage({ type: 'ace-site-element-selected', payload: {
      route: location.pathname + location.search,
      selector: parts.join(' > '), element_tag: el.tagName.toLowerCase(),
      element_text: (el.textContent || '').trim().slice(0, 2000),
      context: { bounding_box: { x: box.x, y: box.y, width: box.width, height: box.height } }
    }}, '*');
  }, true);
})();
</script>
"""
                marker = "</body>"
                html = html.replace(marker, bridge + marker) if marker in html else html + bridge
                return HTMLResponse(html, headers={"Cache-Control": "no-store"})
            return FileResponse(path)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/{site_id}/preview")
    async def preview_index(site_id: str, request: Request):
        return await preview(site_id, "index.html", request)

    @router.post("/{site_id}/annotations")
    async def create_annotation(site_id: str, request: Request):
        try:
            data = await request.json()
            site = manager().store.get_site(owner(request), site_id)
            annotation = manager().store.create_annotation(
                owner(request), site_id, site["active_release_id"], data,
            )
            return {"ok": True, "annotation": annotation}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.patch("/{site_id}/annotations/{annotation_id}")
    async def update_annotation(site_id: str, annotation_id: str, request: Request):
        try:
            data = await request.json()
            item = manager().store.get_annotation(owner(request), annotation_id)
            if item["site_id"] != site_id:
                raise KeyError("注释不存在")
            annotation = manager().store.update_annotation_status(
                owner(request), annotation_id, str(data.get("status") or ""),
            )
            return {"ok": True, "annotation": annotation}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/{site_id}/export")
    async def export_site(site_id: str, request: Request):
        try:
            archive = manager().export(owner(request), site_id)
            return FileResponse(archive, filename=archive.name, media_type="application/zip")
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.post("/{site_id}/export")
    async def prepare_export_site(site_id: str, request: Request):
        try:
            archive = manager().export(owner(request), site_id)
            return {"ok": True, "archive_path": str(archive), "filename": archive.name}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    return router
