"""Canvas、Widget、Automation 与 Binding 的 Agent 工具契约。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_workspace_id,
)
from crew.tools.registry import Registry, tool_result


def _response(data: Any = None, **extra: Any) -> str:
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return tool_result(payload)


def _context() -> tuple[str, str, str, str]:
    owner = current_owner_account_id.get().strip()
    workspace_id = current_workspace_id.get().strip() or "default"
    session_id = current_session_id.get().strip()
    workdir = current_agent_workdir.get().strip()
    if not owner:
        raise ValueError("当前会话缺少用户身份")
    return owner, workspace_id, session_id, workdir


def _schema(name: str, description: str, actions: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": actions},
                "canvasId": {"type": "string"}, "widgetId": {"type": "string"},
                "mountId": {"type": "string"}, "automationId": {"type": "string"},
                "bindingId": {"type": "string"}, "title": {"type": "string"},
                "description": {"type": "string"}, "purpose": {"type": "string"},
                "layout": {"type": "object"}, "viewState": {"type": "object"},
                "zOrder": {"type": "integer"}, "slots": {"type": "object"},
                "events": {"type": "object"}, "trigger": {"type": "object"},
                "input": {"type": "object"}, "execution": {"type": "object"},
                "result": {"type": "object"}, "enabled": {"type": "boolean"},
                "runInput": {}, "kind": {"type": "string"}, "limit": {"type": "integer"},
                "offset": {"type": "integer"}, "runId": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    }


CANVAS_SCHEMA = _schema(
    "Canvas", "创建和管理灵感 App 的 Canvas 与 Widget 布局；完成后使用 show 展示结果。",
    ["list", "create", "read", "update", "placeWidget", "updatePlacement",
     "removePlacement", "show", "delete"],
)
WIDGET_SCHEMA = _schema(
    "Widget", "创建、读取、更新、验证和展示灵感 App 的 HTML Widget。",
    ["list", "create", "read", "update", "validate", "show", "delete"],
)
AUTOMATION_SCHEMA = _schema(
    "Automation", "创建、运行、调度和检查可向 Widget 产出 JSON Artifact 的任务。",
    ["list", "create", "read", "update", "run", "readRun", "readRunArtifact",
     "readRunLogs", "listRuns", "delete"],
)
BINDING_SCHEMA = _schema(
    "Binding", "建立并验证 Automation Artifact 与 Widget main Slot 的连接。",
    ["list", "create", "read", "validate", "delete"],
)


def register_blueprint_tools(
    registry: Registry,
    sites,
    *,
    workspace_store=None,
    security_service=None,
) -> None:
    manager = sites.blueprint
    store = manager.store

    def page(items: list[Any], args: dict[str, Any]) -> dict[str, Any]:
        offset = max(0, int(args.get("offset") or 0))
        limit = max(1, min(int(args.get("limit") or 50), 100))
        sliced = items[offset:offset + limit]
        next_offset = offset + len(sliced) if offset + len(sliced) < len(items) else None
        return {"items": sliced, "page": {"offset": offset, "limit": limit,
                                           "nextOffset": next_offset, "total": len(items)}}

    def canvas_tool(args: dict[str, Any]):
        owner, workspace_id, session_id, _ = _context()
        action = args["action"]
        if action == "list":
            return _response(page(store.list_canvases(owner), args))
        if action == "create":
            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("Canvas title 必填")
            return _response(store.create_canvas(owner, workspace_id, session_id, title,
                                                 str(args.get("purpose") or "")))
        canvas_id = str(args.get("canvasId") or "")
        if action == "read":
            return _response(store.get_canvas(owner, canvas_id))
        if action == "update":
            return _response(store.update_canvas(owner, canvas_id,
                                                  title=args.get("title"), purpose=args.get("purpose")))
        if action == "show":
            canvas = store.get_canvas(owner, canvas_id)
            return _response(canvas, surface={
                "kind": "inspiration", "mode": "canvas", "sessionId": session_id,
                "inspirationId": canvas_id, "canvasId": canvas_id, "title": canvas["title"],
                "status": "ready", "revisionId": str(canvas["updatedAt"]),
            })
        if action == "placeWidget":
            layout = manager.normalize_layout(args.get("layout"))
            return _response(store.place_widget(owner, canvas_id, str(args.get("widgetId") or ""), layout))
        if action == "updatePlacement":
            layout = manager.normalize_layout(args["layout"]) if "layout" in args else None
            view_state = args.get("viewState") if isinstance(args.get("viewState"), dict) else None
            return _response(store.update_placement(owner, str(args.get("mountId") or ""),
                                                     layout=layout, z_order=args.get("zOrder"),
                                                     view_state=view_state))
        if action == "removePlacement":
            store.remove_placement(owner, str(args.get("mountId") or ""))
            return _response({"removed": True})
        if action == "delete":
            store.delete_canvas(owner, canvas_id)
            return _response({"deleted": True})
        raise ValueError("未知 Canvas action")

    def widget_tool(args: dict[str, Any]):
        owner, workspace_id, session_id, workdir = _context()
        action = args["action"]
        if action == "list":
            return _response(page(store.list_widgets(owner), args))
        if action == "create":
            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("Widget title 必填")
            widget_id = f"widget_{uuid.uuid4().hex[:12]}"
            base = Path(workdir).resolve() if workdir else manager.widget_root(owner, widget_id).parent.parent
            root = base / ".ace" / "blueprint" / "widgets" / widget_id if workdir else manager.widget_root(owner, widget_id)
            root.mkdir(parents=True, exist_ok=True)
            widget = store.create_widget(owner, workspace_id, str(root), title,
                                         str(args.get("description") or ""), widget_id=widget_id)
            return _response(widget)
        widget_id = str(args.get("widgetId") or "")
        if action == "read":
            return _response(store.get_widget(owner, widget_id))
        if action == "update":
            changes = {key: args[key] for key in ("title", "description", "slots", "events") if key in args}
            widget = store.update_widget(owner, widget_id, **changes)
            revalidated = []
            for binding in store.list_bindings(owner, widget_id=widget_id):
                if binding["active"]:
                    revalidated.append(manager.validate_binding(owner, binding["id"]))
            return _response(widget, revalidatedBindings=revalidated)
        if action == "validate":
            widget = store.get_widget(owner, widget_id)
            validation = manager.validate_widget_file(widget)
            if validation["status"] == "valid":
                widget = store.bump_widget_revision(owner, widget_id)
            return _response({"widgetId": widget_id, "widget": widget, "validation": validation},
                             validation=validation)
        if action == "show":
            widget = store.get_widget(owner, widget_id)
            validation = manager.validate_widget_file(widget)
            return _response(widget, surface={
                "kind": "inspiration", "mode": "widget", "sessionId": session_id,
                "widgetId": widget_id, "title": widget["title"],
                "resourceRevision": widget["resourceRevision"],
                "status": "ready" if validation["status"] == "valid" else "preparing",
            })
        if action == "delete":
            widget = store.get_widget(owner, widget_id)
            store.delete_widget(owner, widget_id)
            root = Path(widget["workspacePath"]).resolve()
            safe_owned_root = (
                root.name == widget_id
                and root.parent.name == "widgets"
                and root.parent.parent.name == "blueprint"
                and root.parent.parent.parent.name == ".ace"
            )
            if root.is_dir() and safe_owned_root:
                import shutil
                shutil.rmtree(root)
            return _response({"deleted": True})
        raise ValueError("未知 Widget action")

    async def automation_tool(args: dict[str, Any]):
        owner, workspace_id, _, _ = _context()
        action = args["action"]
        if action == "list":
            return _response(page(store.list_automations(owner), args))
        if action == "create":
            title = str(args.get("title") or "").strip()
            description = str(args.get("description") or "").strip()
            if not title or not description:
                raise ValueError("Automation title 和 description 必填")
            trigger = args.get("trigger") if isinstance(args.get("trigger"), dict) else {"kind": "manual"}
            execution = args.get("execution") if isinstance(args.get("execution"), dict) else {}
            result = args.get("result") if isinstance(args.get("result"), dict) else {}
            manager.validate_automation_contract(trigger, execution, result)
            automation = store.create_automation(
                owner, workspace_id, title, description,
                trigger,
                args.get("input") if isinstance(args.get("input"), dict) else {"kind": "none"},
                execution, result,
                bool(args.get("enabled", False)),
            )
            manager.sync_schedule(owner, automation["id"])
            return _response(automation)
        automation_id = str(args.get("automationId") or "")
        if action == "read":
            return _response(store.get_automation(owner, automation_id))
        if action == "update":
            if "description" not in args or not str(args.get("description") or "").strip():
                raise ValueError("Automation.update 必须提供非空 description")
            changes = {key: args[key] for key in
                       ("title", "description", "trigger", "input", "execution", "result", "enabled") if key in args}
            current = store.get_automation(owner, automation_id)
            manager.validate_automation_contract(
                changes.get("trigger", current["trigger"]),
                changes.get("execution", current["execution"]),
                changes.get("result", current["result"]),
            )
            automation = store.update_automation(owner, automation_id, **changes)
            manager.sync_schedule(owner, automation_id)
            revalidated = [manager.validate_binding(owner, item["id"])
                           for item in store.list_bindings(owner, automation_id=automation_id) if item["active"]]
            return _response(automation, revalidatedBindings=revalidated)
        if action == "run":
            async def authorize_network(url: str) -> None:
                from crew.tools.security_guard import authorize_network_tool

                await authorize_network_tool(
                    url,
                    tool_name="Automation.run",
                    workspace_store=workspace_store,
                    security_service=security_service,
                )

            return _response(await manager.run_automation(
                owner,
                automation_id,
                run_input=args.get("runInput"),
                authorize_network=authorize_network,
            ))
        if action == "readRun":
            return _response(store.get_run(owner, str(args.get("runId") or "")))
        if action == "readRunArtifact":
            run = store.get_run(owner, str(args.get("runId") or ""))
            return _response({"runId": run["id"], "artifact": run["artifact"]})
        if action == "readRunLogs":
            run = store.get_run(owner, str(args.get("runId") or ""))
            return _response({"runId": run["id"], "logs": run["logs"], "error": run["error"]})
        if action == "listRuns":
            return _response({"items": store.list_runs(owner, automation_id, int(args.get("limit") or 20))})
        if action == "delete":
            manager.remove_schedule(owner, automation_id)
            store.delete_automation(owner, automation_id)
            return _response({"deleted": True})
        raise ValueError("未知 Automation action")

    def binding_tool(args: dict[str, Any]):
        owner, _, _, _ = _context()
        action = args["action"]
        if action == "list":
            return _response(page(store.list_bindings(
                owner, automation_id=str(args.get("automationId") or ""),
                widget_id=str(args.get("widgetId") or "")), args))
        if action == "create":
            binding = store.create_binding(owner, str(args.get("automationId") or ""),
                                           str(args.get("widgetId") or ""), "pending_run", [])
            return _response(manager.validate_binding(owner, binding["id"]))
        binding_id = str(args.get("bindingId") or "")
        if action == "read":
            return _response(store.get_binding(owner, binding_id))
        if action == "validate":
            return _response(manager.validate_binding(owner, binding_id))
        if action == "delete":
            store.delete_binding(owner, binding_id)
            return _response({"deleted": True})
        raise ValueError("未知 Binding action")

    registry.register(name="Canvas", toolset="blueprint", schema=CANVAS_SCHEMA, handler=canvas_tool,
                      display_name="灵感 App", ui_label_template="管理 App {title}", always_load=True)
    registry.register(name="Widget", toolset="blueprint", schema=WIDGET_SCHEMA, handler=widget_tool,
                      display_name="App 组件", ui_label_template="管理组件 {title}", always_load=True)
    registry.register(name="Automation", toolset="blueprint", schema=AUTOMATION_SCHEMA,
                      handler=automation_tool, is_async=True, display_name="App 自动化",
                      ui_label_template="运行自动化 {title}", always_load=True)
    registry.register(name="Binding", toolset="blueprint", schema=BINDING_SCHEMA, handler=binding_tool,
                      display_name="数据绑定", ui_label_template="绑定组件数据", always_load=True)
