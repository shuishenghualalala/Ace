"""内容命令：本地站点（Sites）与工作台（Work）。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import uuid4

from crew.cli.app import CliContext, CliError, CliResult, parse_json


def register(subparsers, handlers: dict[str, Any]) -> None:
    _register_sites(subparsers)
    _register_work(subparsers)


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

def _sites_manager(app: Any):
    manager = getattr(app, "sites", None)
    if manager is None:
        raise CliError("站点模块未初始化")
    return manager


def _register_sites(subparsers) -> None:
    parser = subparsers.add_parser("site", help="本地站点管理")
    cmds = parser.add_subparsers(dest="site_cmd")

    lst = cmds.add_parser("list")
    lst.add_argument("--workspace-id")
    lst.set_defaults(handler=_site_list)
    show = cmds.add_parser("show")
    show.add_argument("--id", dest="site_id", required=True)
    show.set_defaults(handler=_site_show)
    delete = cmds.add_parser("delete")
    delete.add_argument("--id", dest="site_id", required=True)
    delete.set_defaults(handler=_site_delete)
    publish = cmds.add_parser("publish")
    publish.add_argument("--id", dest="site_id", required=True)
    publish.add_argument("--name")
    publish.add_argument("--description")
    publish.add_argument("--build-command")
    publish.add_argument("--output-directory")
    publish.set_defaults(handler=_site_publish)
    export = cmds.add_parser("export")
    export.add_argument("--id", dest="site_id", required=True)
    export.set_defaults(handler=_site_export)

    inspirations = cmds.add_parser("inspirations", help="灵感列表")
    inspirations_cmds = inspirations.add_subparsers(dest="site_inspirations_cmd")
    inspirations_cmds.add_parser("list").set_defaults(handler=_site_inspirations_list)
    get = inspirations_cmds.add_parser("show")
    get.add_argument("--id", dest="inspiration_id", required=True)
    get.set_defaults(handler=_site_inspirations_show)
    delete_insp = inspirations_cmds.add_parser("delete")
    delete_insp.add_argument("--id", dest="inspiration_id", required=True)
    delete_insp.set_defaults(handler=_site_inspirations_delete)
    export_insp = inspirations_cmds.add_parser("export")
    export_insp.add_argument("--id", dest="inspiration_id", required=True)
    export_insp.set_defaults(handler=_site_inspirations_export)

    canvases = cmds.add_parser("canvases", help="看板")
    canvases_cmds = canvases.add_subparsers(dest="site_canvases_cmd")
    canvases_cmds.add_parser("list").set_defaults(handler=_site_canvases_list)
    canvas_show = canvases_cmds.add_parser("show")
    canvas_show.add_argument("--id", dest="canvas_id", required=True)
    canvas_show.set_defaults(handler=_site_canvases_show)

    widgets = cmds.add_parser("widgets", help="组件")
    widgets_cmds = widgets.add_subparsers(dest="site_widgets_cmd")
    widget_show = widgets_cmds.add_parser("show")
    widget_show.add_argument("--id", dest="widget_id", required=True)
    widget_show.set_defaults(handler=_site_widgets_show)

    automations = cmds.add_parser("automations", help="自动化")
    automations_cmds = automations.add_subparsers(dest="site_automations_cmd")
    run = automations_cmds.add_parser("run")
    run.add_argument("--id", dest="automation_id", required=True)
    run.add_argument("--run-input", default="")
    run.set_defaults(handler=_site_automations_run)


def _site_list(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    items = manager.store.list_sites(ctx.owner, args.workspace_id)
    text = "\n".join(
        f"{item.get('id')}  {item.get('name', '')}  ws={item.get('workspace_id', '')}"
        for item in items
    )
    return CliResult(data={"sites": items}, text=text or "(无站点)")


def _site_show(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        site = manager.store.get_site(ctx.owner, args.site_id)
        releases = manager.store.list_releases(ctx.owner, args.site_id)
        annotations = manager.store.list_annotations(ctx.owner, args.site_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(
        data={"site": site, "releases": releases, "annotations": annotations},
        text=site.get("name", ""),
    )


def _site_delete(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        manager.store.get_site(ctx.owner, args.site_id)
        manager.delete(ctx.owner, args.site_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"ok": True}, text="站点已删除")


async def _site_publish(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.context import build_gateway_security_context
    from crew.security.launch import compile_process_launch, use_process_launch
    from crew.tools.security_guard import authorize_user_initiated_exec

    app = ctx.app
    manager = _sites_manager(app)
    try:
        current = manager.store.get_site(ctx.owner, args.site_id)
        workspace = app.workspace_store.get(current["workspace_id"], owner_account_id=ctx.owner)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    workspace_root = workspace.get("root_path") or current["source_path"]
    context = build_gateway_security_context(
        app.workspace_store,
        owner_account_id=ctx.owner,
        workspace_id=current["workspace_id"],
        session_id=current["session_id"] or f"site-{args.site_id}",
        request_id=uuid4().hex,
        cwd=workspace_root,
    )
    launch = compile_process_launch(
        context,
        app.security_service.mode_for(context),
        db_path=app.security_service.db_path,
        audit=app.security_service.audit,
        approval_service=app.security_service,
    )

    async def authorize_build(argv, cwd, _preview):
        authorize_user_initiated_exec(
            argv,
            cwd=cwd,
            tool_name="publish_site",
            security_service=app.security_service,
            security_context=context,
        )

    try:
        with use_process_launch(launch):
            result = await manager.publish(
                owner=ctx.owner,
                workspace_id=current["workspace_id"],
                session_id=current["session_id"],
                workspace_root=workspace_root,
                source_path=current["source_path"],
                name=args.name or current["name"],
                description=args.description or current.get("description") or "",
                build_command=args.build_command or current["build_command"],
                output_directory=args.output_directory or current["output_directory"],
                site_id=args.site_id,
                build_authorizer=authorize_build,
            )
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    except (ValueError, RuntimeError) as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data={"ok": True, **result}, text="站点已发布")


def _site_export(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        archive = manager.export(ctx.owner, args.site_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"archive_path": str(archive), "filename": archive.name}, text=str(archive))


def _site_inspirations_list(args: Any, ctx: CliContext) -> CliResult:
    items = _sites_manager(ctx.app).list_inspirations(ctx.owner)
    text = "\n".join(
        f"{item.get('id')}  {item.get('kind')}  {item.get('title', '')}"
        for item in items
    )
    return CliResult(data={"inspirations": items}, text=text or "(无灵感)")


def _site_inspirations_show(args: Any, ctx: CliContext) -> CliResult:
    try:
        inspiration = _sites_manager(ctx.app).get_inspiration(ctx.owner, args.inspiration_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"inspiration": inspiration})


def _site_inspirations_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _sites_manager(ctx.app).delete_inspiration(ctx.owner, args.inspiration_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"ok": True}, text="灵感已删除")


def _site_inspirations_export(args: Any, ctx: CliContext) -> CliResult:
    try:
        archive = _sites_manager(ctx.app).export_inspiration(ctx.owner, args.inspiration_id)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"archive_path": str(archive), "filename": archive.name}, text=str(archive))


def _site_canvases_list(args: Any, ctx: CliContext) -> CliResult:
    items = _sites_manager(ctx.app).blueprint.store.list_canvases(ctx.owner)
    text = "\n".join(f"{item.get('id')}  {item.get('name', '')}" for item in items)
    return CliResult(data={"canvases": items}, text=text or "(无看板)")


def _site_canvases_show(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        canvas = manager.blueprint.store.get_canvas(ctx.owner, args.canvas_id)
        widgets = {
            placement["widgetId"]: manager.blueprint.store.get_widget(
                ctx.owner, placement["widgetId"]
            )
            for placement in canvas["placements"]
        }
        for widget in widgets.values():
            widget["validation"] = manager.blueprint.validate_widget_file(widget)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"canvas": canvas, "widgets": widgets}, text=canvas.get("name", ""))


def _site_widgets_show(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        widget = manager.blueprint.store.get_widget(ctx.owner, args.widget_id)
        widget["validation"] = manager.blueprint.validate_widget_file(widget)
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data={"widget": widget}, text=widget.get("name", ""))


async def _site_automations_run(args: Any, ctx: CliContext) -> CliResult:
    manager = _sites_manager(ctx.app)
    try:
        run = await manager.blueprint.run_automation(
            ctx.owner,
            args.automation_id,
            run_input=args.run_input,
            trigger_kind="manual",
        )
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    except (ValueError, RuntimeError) as exc:
        raise CliError(str(exc)) from exc
    return CliResult(
        data={"ok": run["status"] == "succeeded", "run": run},
        text=f"自动化运行完成 status={run.get('status')}",
    )


# ---------------------------------------------------------------------------
# Work
# ---------------------------------------------------------------------------

def _work_service(app: Any):
    service = getattr(app, "work_service", None)
    if service is None:
        raise CliError("Work service 未初始化")
    return service


def _register_work(subparsers) -> None:
    parser = subparsers.add_parser("work", help="工作台管理")
    cmds = parser.add_subparsers(dest="work_cmd")

    sessions = cmds.add_parser("sessions", help="工作会话")
    sessions_cmds = sessions.add_subparsers(dest="work_sessions_cmd")
    create = sessions_cmds.add_parser("create")
    create.add_argument("--workspace-id", default="default")
    create.add_argument("--title", default="新对话")
    create.set_defaults(handler=_work_session_create)
    history = cmds.add_parser("history")
    history.add_argument("--include-archived", action="store_true")
    history.set_defaults(handler=_work_history)

    items = cmds.add_parser("items", help="工作项")
    items_cmds = items.add_subparsers(dest="work_items_cmd")
    item_list = items_cmds.add_parser("list")
    item_list.add_argument("--workspace-id")
    item_list.add_argument("--business-status")
    item_list.add_argument("--disposition")
    item_list.set_defaults(handler=_work_items_list)
    item_show = items_cmds.add_parser("show")
    item_show.add_argument("--id", dest="item_id", required=True)
    item_show.set_defaults(handler=_work_items_show)
    item_create = items_cmds.add_parser("create")
    item_create.add_argument("--json", dest="json_payload", required=True)
    item_create.set_defaults(handler=_work_items_create)
    item_update = items_cmds.add_parser("update")
    item_update.add_argument("--id", dest="item_id", required=True)
    item_update.add_argument("--json", dest="json_payload", required=True)
    item_update.add_argument("--expected-version", type=int, required=True)
    item_update.set_defaults(handler=_work_items_update)
    item_act = items_cmds.add_parser("act")
    item_act.add_argument("--id", dest="item_id", required=True)
    item_act.add_argument("--action", required=True)
    item_act.add_argument("--expected-version", type=int, required=True)
    item_act.add_argument("--due-at")
    item_act.set_defaults(handler=_work_items_act)
    item_activity = items_cmds.add_parser("activity")
    item_activity.add_argument("--id", dest="item_id", required=True)
    item_activity.set_defaults(handler=_work_items_activity)
    item_delete = items_cmds.add_parser("delete")
    item_delete.add_argument("--id", dest="item_id", required=True)
    item_delete.add_argument("--expected-version", type=int, required=True)
    item_delete.set_defaults(handler=_work_items_delete)

    references = cmds.add_parser("references", help="工作引用")
    ref_cmds = references.add_subparsers(dest="work_references_cmd")
    ref_list = ref_cmds.add_parser("list")
    ref_list.add_argument("--target-session-id", default="")
    ref_list.set_defaults(handler=_work_references_list)
    ref_create = ref_cmds.add_parser("create")
    ref_create.add_argument("--target-session-id", required=True)
    ref_create.add_argument("--reference-type", default="")
    ref_create.add_argument("--source-id", default="")
    ref_create.add_argument("--source-link", default="")
    ref_create.set_defaults(handler=_work_references_create)
    ref_delete = ref_cmds.add_parser("delete")
    ref_delete.add_argument("--id", dest="reference_id", required=True)
    ref_delete.set_defaults(handler=_work_references_delete)

    preferences = cmds.add_parser("preferences", help="偏好")
    pref_cmds = preferences.add_subparsers(dest="work_preferences_cmd")
    pref_cmds.add_parser("list").set_defaults(handler=_work_preferences_list)
    settings_get = pref_cmds.add_parser("settings")
    settings_get.set_defaults(handler=_work_preferences_settings)
    settings_set = pref_cmds.add_parser("settings-set")
    settings_set.add_argument("--enabled", type=lambda v: v.lower() in ("1", "true", "yes", "on"), required=True)
    settings_set.set_defaults(handler=_work_preferences_settings_set)
    pref_create = pref_cmds.add_parser("create")
    pref_create.add_argument("--category", required=True)
    pref_create.add_argument("--content", required=True)
    pref_create.set_defaults(handler=_work_preferences_create)
    pref_delete = pref_cmds.add_parser("delete")
    pref_delete.add_argument("--id", dest="preference_id", required=True)
    pref_delete.add_argument("--expected-version", type=int, required=True)
    pref_delete.set_defaults(handler=_work_preferences_delete)

    sources = cmds.add_parser("sources", help="数据源")
    source_cmds = sources.add_subparsers(dest="work_sources_cmd")
    source_cmds.add_parser("list").set_defaults(handler=_work_sources_list)
    source_toggle = source_cmds.add_parser("toggle")
    source_toggle.add_argument("--connector-key", required=True)
    source_toggle.add_argument("--enabled", type=lambda v: v.lower() in ("1", "true", "yes", "on"), required=True)
    source_toggle.set_defaults(handler=_work_sources_toggle)
    source_refresh = source_cmds.add_parser("refresh")
    source_refresh.add_argument("--connector-key", required=True)
    source_refresh.set_defaults(handler=_work_sources_refresh)

    dashboard = cmds.add_parser("dashboard", help="看板/报表")
    dashboard_cmds = dashboard.add_subparsers(dest="work_dashboard_cmd")
    dash_get = dashboard_cmds.add_parser("get")
    dash_get.add_argument("--workspace-id")
    dash_get.set_defaults(handler=_work_dashboard_get)
    dash_refresh = dashboard_cmds.add_parser("refresh")
    dash_refresh.add_argument("--workspace-id")
    dash_refresh.set_defaults(handler=_work_dashboard_refresh)
    report_get = dashboard_cmds.add_parser("report")
    report_get.add_argument("--period", required=True)
    report_get.add_argument("--anchor", required=True)
    report_get.add_argument("--workspace-id")
    report_get.set_defaults(handler=_work_report_get)
    report_archive = dashboard_cmds.add_parser("report-archive")
    report_archive.add_argument("--period", required=True)
    report_archive.add_argument("--anchor", required=True)
    report_archive.add_argument("--workspace-id")
    report_archive.set_defaults(handler=_work_report_archive)

    settings = cmds.add_parser("settings", help="工作台设置")
    settings_cmds = settings.add_subparsers(dest="work_settings_cmd")
    settings_cmds.add_parser("get").set_defaults(handler=_work_settings_get)
    settings_update = settings_cmds.add_parser("update")
    settings_update.add_argument("--json", dest="json_payload", required=True)
    settings_update.set_defaults(handler=_work_settings_update)
    ws_get = settings_cmds.add_parser("workspace-get")
    ws_get.add_argument("--workspace-id", required=True)
    ws_get.set_defaults(handler=_work_settings_workspace_get)
    ws_update = settings_cmds.add_parser("workspace-update")
    ws_update.add_argument("--workspace-id", required=True)
    ws_update.add_argument("--json", dest="json_payload", required=True)
    ws_update.set_defaults(handler=_work_settings_workspace_update)

    templates = cmds.add_parser("templates", help="模板")
    template_cmds = templates.add_subparsers(dest="work_templates_cmd")
    template_cmds.add_parser("list").set_defaults(handler=_work_templates_list)
    template_create = template_cmds.add_parser("create")
    template_create.add_argument("--name", required=True)
    template_create.add_argument("--description", default="")
    template_create.add_argument("--category", default="")
    template_create.add_argument("--blueprint", default="{}")
    template_create.set_defaults(handler=_work_templates_create)
    template_show = template_cmds.add_parser("show")
    template_show.add_argument("--id", dest="template_id", required=True)
    template_show.set_defaults(handler=_work_templates_show)
    template_delete = template_cmds.add_parser("delete")
    template_delete.add_argument("--id", dest="template_id", required=True)
    template_delete.set_defaults(handler=_work_templates_delete)
    template_instantiate = template_cmds.add_parser("instantiate")
    template_instantiate.add_argument("--id", dest="template_id", required=True)
    template_instantiate.add_argument("--workspace-id", default="default")
    template_instantiate.set_defaults(handler=_work_templates_instantiate)

    knowledge = cmds.add_parser("knowledge", help="工作知识")
    knowledge_cmds = knowledge.add_subparsers(dest="work_knowledge_cmd")
    knowledge_cmds.add_parser("personal-list").set_defaults(handler=_work_knowledge_personal_list)
    knowledge_save = knowledge_cmds.add_parser("personal-save")
    knowledge_save.add_argument("--title", required=True)
    knowledge_save.add_argument("--content", required=True)
    knowledge_save.set_defaults(handler=_work_knowledge_personal_save)

    index = cmds.add_parser("index", help="工作空间索引状态")
    index_cmds = index.add_subparsers(dest="work_index_cmd")
    index_get = index_cmds.add_parser("get")
    index_get.add_argument("--workspace-id", required=True)
    index_get.set_defaults(handler=_work_index_get)
    index_set = index_cmds.add_parser("set")
    index_set.add_argument("--workspace-id", required=True)
    index_set.add_argument("--enabled", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    index_set.add_argument("--state")
    index_set.set_defaults(handler=_work_index_set)
    index_delete = index_cmds.add_parser("delete")
    index_delete.add_argument("--workspace-id", required=True)
    index_delete.set_defaults(handler=_work_index_delete)


def _work_error(exc: BaseException) -> CliError:
    from crew.work.items import WorkItemConflictError
    from crew.work.preferences import WorkPreferenceConflictError

    if isinstance(exc, KeyError):
        return CliError(str(exc), exit_code=404)
    if isinstance(exc, (WorkItemConflictError, WorkPreferenceConflictError)):
        return CliError(str(exc), exit_code=409)
    if isinstance(exc, PermissionError):
        return CliError(str(exc), exit_code=403)
    return CliError(str(exc))


def _work_session_create(args: Any, ctx: CliContext) -> CliResult:
    try:
        created = _work_service(ctx.app).create_session(
            owner_account_id=ctx.owner,
            workspace_id=args.workspace_id,
            title=args.title,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=created, text=f"已创建工作会话 {created.get('session_id')}")


def _work_history(args: Any, ctx: CliContext) -> CliResult:
    entries = _work_service(ctx.app).history(ctx.owner, include_archived=args.include_archived)
    return CliResult(data={"entries": entries, "count": len(entries)})


def _work_items_list(args: Any, ctx: CliContext) -> CliResult:
    filters = {
        key: value
        for key, value in {
            "workspace_id": args.workspace_id,
            "business_status": args.business_status,
            "disposition": args.disposition,
        }.items()
        if value is not None
    }
    try:
        items = _work_service(ctx.app).list_items(ctx.owner, **filters)
    except Exception as exc:
        raise _work_error(exc) from exc
    rows = [asdict(item) for item in items]
    text = "\n".join(
        f"{row.get('item_id')}  {row.get('title', '')}  {row.get('business_status', '')}"
        for row in rows
    )
    return CliResult(data={"items": rows, "count": len(rows)}, text=text or "(无工作项)")


def _work_items_show(args: Any, ctx: CliContext) -> CliResult:
    try:
        item = _work_service(ctx.app).get_item(ctx.owner, args.item_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(item))


def _work_items_create(args: Any, ctx: CliContext) -> CliResult:
    payload = parse_json(args.json_payload, name="工作项")
    if not isinstance(payload, dict):
        raise CliError("工作项必须是 JSON 对象")
    try:
        item = _work_service(ctx.app).create_item(owner_account_id=ctx.owner, values=payload)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(item), text=f"已创建工作项 {item.item_id}")


def _work_items_update(args: Any, ctx: CliContext) -> CliResult:
    changes = parse_json(args.json_payload, name="变更")
    if not isinstance(changes, dict):
        raise CliError("变更必须是 JSON 对象")
    try:
        item = _work_service(ctx.app).update_item(
            owner_account_id=ctx.owner,
            item_id=args.item_id,
            expected_version=args.expected_version,
            changes=changes,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(item), text="工作项已更新")


def _work_items_act(args: Any, ctx: CliContext) -> CliResult:
    try:
        item = _work_service(ctx.app).act_on_item(
            owner_account_id=ctx.owner,
            item_id=args.item_id,
            expected_version=args.expected_version,
            action=args.action,
            due_at=args.due_at,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(item), text=f"工作项已执行 {args.action}")


def _work_items_activity(args: Any, ctx: CliContext) -> CliResult:
    try:
        events = _work_service(ctx.app).list_item_activity(ctx.owner, args.item_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    rows = [asdict(event) for event in events]
    return CliResult(data={"events": rows, "count": len(rows)})


def _work_items_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _work_service(ctx.app).delete_item(
            owner_account_id=ctx.owner,
            item_id=args.item_id,
            expected_version=args.expected_version,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"ok": True}, text="工作项已删除")


def _work_references_list(args: Any, ctx: CliContext) -> CliResult:
    try:
        refs = _work_service(ctx.app).list_references(ctx.owner, args.target_session_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    rows = [asdict(ref) for ref in refs]
    return CliResult(data={"items": rows, "count": len(rows)})


def _work_references_create(args: Any, ctx: CliContext) -> CliResult:
    try:
        ref = _work_service(ctx.app).create_reference(
            owner_account_id=ctx.owner,
            target_session_id=args.target_session_id,
            reference_type=args.reference_type,
            source_id=args.source_id,
            source_link=args.source_link,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(ref), text="引用已创建")


def _work_references_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _work_service(ctx.app).delete_reference(ctx.owner, args.reference_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"ok": True}, text="引用已删除")


def _work_preferences_list(args: Any, ctx: CliContext) -> CliResult:
    prefs = _work_service(ctx.app).list_preferences(ctx.owner)
    rows = [asdict(pref) for pref in prefs]
    return CliResult(data={"items": rows, "count": len(rows)})


def _work_preferences_settings(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data=_work_service(ctx.app).get_preference_settings(ctx.owner))


def _work_preferences_settings_set(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(
        data=_work_service(ctx.app).set_preference_settings(
            ctx.owner, args.enabled
        )
    )


def _work_preferences_create(args: Any, ctx: CliContext) -> CliResult:
    try:
        pref = _work_service(ctx.app).create_preference(
            owner_account_id=ctx.owner,
            category=args.category,
            content=args.content,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(pref), text="偏好已创建")


def _work_preferences_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _work_service(ctx.app).delete_preference(
            owner_account_id=ctx.owner,
            preference_id=args.preference_id,
            expected_version=args.expected_version,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"ok": True}, text="偏好已删除")


def _work_sources_list(args: Any, ctx: CliContext) -> CliResult:
    states = _work_service(ctx.app).list_sources(ctx.owner)
    rows = [asdict(state) for state in states]
    return CliResult(data={"items": rows, "count": len(rows)})


def _work_sources_toggle(args: Any, ctx: CliContext) -> CliResult:
    try:
        state = _work_service(ctx.app).toggle_source(ctx.owner, args.connector_key, args.enabled)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(state))


def _work_sources_refresh(args: Any, ctx: CliContext) -> CliResult:
    try:
        state = _work_service(ctx.app).refresh_source(ctx.owner, args.connector_key)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(state))


def _work_dashboard_get(args: Any, ctx: CliContext) -> CliResult:
    brief = _work_service(ctx.app).get_dashboard(ctx.owner, workspace_id=args.workspace_id)
    return CliResult(data={"brief": asdict(brief) if brief else None})


def _work_dashboard_refresh(args: Any, ctx: CliContext) -> CliResult:
    try:
        brief = _work_service(ctx.app).refresh_dashboard(
            ctx.owner,
            workspace_id=args.workspace_id,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"brief": asdict(brief)})


def _work_report_get(args: Any, ctx: CliContext) -> CliResult:
    try:
        report = _work_service(ctx.app).get_period_report(
            ctx.owner,
            period=args.period,
            anchor=args.anchor,
            workspace_id=args.workspace_id,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"report": asdict(report)})


def _work_report_archive(args: Any, ctx: CliContext) -> CliResult:
    try:
        report = _work_service(ctx.app).archive_period_report(
            ctx.owner,
            period=args.period,
            anchor=args.anchor,
            workspace_id=args.workspace_id,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"report": asdict(report)})


def _work_settings_get(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data=_work_service(ctx.app).get_account_settings(ctx.owner))


def _work_settings_update(args: Any, ctx: CliContext) -> CliResult:
    payload = parse_json(args.json_payload, name="设置")
    if not isinstance(payload, dict):
        raise CliError("设置必须是 JSON 对象")
    return CliResult(data=_work_service(ctx.app).update_account_settings(ctx.owner, **payload))


def _work_settings_workspace_get(args: Any, ctx: CliContext) -> CliResult:
    try:
        settings = _work_service(ctx.app).get_workspace_settings(ctx.owner, args.workspace_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=settings)


def _work_settings_workspace_update(args: Any, ctx: CliContext) -> CliResult:
    payload = parse_json(args.json_payload, name="设置")
    if not isinstance(payload, dict):
        raise CliError("设置必须是 JSON 对象")
    try:
        settings = _work_service(ctx.app).update_workspace_settings(
            ctx.owner, args.workspace_id, **payload
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=settings)


def _work_templates_list(args: Any, ctx: CliContext) -> CliResult:
    templates = _work_service(ctx.app).list_templates(ctx.owner)
    rows = [asdict(t) for t in templates]
    text = "\n".join(f"{row.get('template_id')}  {row.get('name', '')}" for row in rows)
    return CliResult(data={"items": rows, "count": len(rows)}, text=text or "(无模板)")


def _work_templates_create(args: Any, ctx: CliContext) -> CliResult:
    blueprint = parse_json(args.blueprint, name="blueprint")
    if not isinstance(blueprint, dict):
        raise CliError("blueprint 必须是 JSON 对象")
    try:
        template = _work_service(ctx.app).create_template(
            ctx.owner,
            name=args.name,
            description=args.description,
            category=args.category,
            blueprint=blueprint,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(template), text="模板已创建")


def _work_templates_show(args: Any, ctx: CliContext) -> CliResult:
    try:
        template = _work_service(ctx.app).get_template(ctx.owner, args.template_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(template))


def _work_templates_delete(args: Any, ctx: CliContext) -> CliResult:
    try:
        _work_service(ctx.app).delete_template(ctx.owner, args.template_id)
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data={"ok": True}, text="模板已删除")


def _work_templates_instantiate(args: Any, ctx: CliContext) -> CliResult:
    try:
        item = _work_service(ctx.app).instantiate_template(
            ctx.owner,
            args.template_id,
            workspace_id=args.workspace_id,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    return CliResult(data=asdict(item), text="模板已实例化")


def _work_knowledge_personal_list(args: Any, ctx: CliContext) -> CliResult:
    pages = _work_service(ctx.app).list_personal_knowledge(ctx.owner)
    rows = [p.to_dict(brief=True) if hasattr(p, "to_dict") else p for p in pages]
    return CliResult(data={"items": rows, "count": len(pages)})


def _work_knowledge_personal_save(args: Any, ctx: CliContext) -> CliResult:
    try:
        page = _work_service(ctx.app).save_personal_knowledge(
            ctx.owner,
            title=args.title,
            content=args.content,
        )
    except Exception as exc:
        raise _work_error(exc) from exc
    page_dict = page.to_dict() if hasattr(page, "to_dict") else page
    return CliResult(data={"page": page_dict}, text="个人知识已保存")


def _work_index_get(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data=_work_service(ctx.app).get_index_status(ctx.owner, args.workspace_id))


def _work_index_set(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(
        data=_work_service(ctx.app).set_index_status(
            ctx.owner,
            args.workspace_id,
            enabled=args.enabled,
            state=args.state,
        )
    )


def _work_index_delete(args: Any, ctx: CliContext) -> CliResult:
    _work_service(ctx.app).delete_index_status(ctx.owner, args.workspace_id)
    return CliResult(data={"ok": True}, text="索引状态已删除")


__all__ = ["register"]
