"""配置、会话、工作空间、任务、定时任务与系统监控命令。"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any

from crew.cli.app import CliContext, CliError, CliResult, parse_json
from crew.gateway.helpers import config_body, with_session_agent_labels
from crew.gateway.hooks import hook_registry
from crew.gateway.routers.sessions import _session_has_blocking_cron, _teardown_session_resources
from crew.state.logging import query_logs
from crew.state.session_store import SessionOwnershipError

_MODEL_FIELDS = (
    "name",
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "api_key",
    "temperature",
    "max_tokens",
    "context_window",
    "timeout",
    "loaded",
    "capabilities",
)


def register(subparsers, handlers: dict[str, Any]) -> None:
    _register_config(subparsers)
    _register_session(subparsers)
    _register_workspace(subparsers)
    _register_task(subparsers)
    _register_cron(subparsers)
    _register_kanban(subparsers)
    _register_system(subparsers)
    _register_scenario(subparsers)
    _register_migrate(subparsers)


def _model_payload(args: Any, *, require_id: bool) -> dict[str, Any]:
    payload = parse_json(getattr(args, "json_payload", None), name="模型配置")
    if require_id:
        model_id = str(getattr(args, "model_id", "") or "").strip()
        if not model_id:
            raise CliError("--id 不能为空")
        payload["id"] = model_id
    for field in _MODEL_FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, str):
        payload["capabilities"] = [item.strip() for item in capabilities.split(",") if item.strip()]
    return payload


def _register_config(subparsers) -> None:
    parser = subparsers.add_parser("config", help="查看配置与管理模型")
    cmds = parser.add_subparsers(dest="config_cmd")

    show = cmds.add_parser("show", help="查看当前配置")
    show.set_defaults(handler=_config_show)

    models = cmds.add_parser("models", help="模型 profile 管理")
    models_cmds = models.add_subparsers(dest="models_cmd")

    lst = models_cmds.add_parser("list", help="列出模型")
    lst.set_defaults(handler=_models_list)

    add = models_cmds.add_parser("add", help="新增模型")
    _add_model_args(add, require_id=True)
    add.set_defaults(handler=_models_add)

    update = models_cmds.add_parser("update", help="更新模型")
    update.add_argument("--id", dest="model_id", required=True)
    _add_model_args(update, require_id=False)
    update.set_defaults(handler=_models_update)

    delete = models_cmds.add_parser("delete", help="删除模型")
    delete.add_argument("--id", dest="model_id", required=True)
    delete.add_argument("--force", action="store_true", help="强制删除并停止占用会话")
    delete.set_defaults(handler=_models_delete)

    use = models_cmds.add_parser("use", help="切换默认模型")
    use.add_argument("--id", dest="model_id", required=True)
    use.set_defaults(handler=_models_use)


def _add_model_args(parser, *, require_id: bool) -> None:
    if require_id:
        parser.add_argument("--id", dest="model_id", required=True)
    parser.add_argument("--name")
    parser.add_argument("--provider")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env")
    parser.add_argument("--api-key")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--loaded", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    parser.add_argument("--capabilities")
    parser.add_argument("--json", dest="json_payload", help="完整模型配置 JSON（与显式参数合并）")


def _config_show(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    body = config_body(
        app,
        owner_account_id=ctx.owner,
        include_builtin_profiles=True,
        is_gateway_admin=True,
    )
    active = next(
        (item for item in body["model_profiles"] if item["id"] == body["active_model_id"]),
        {},
    )
    text = (
        f"激活模型: {body['active_model_id']} ({body['model']}) "
        f"has_key={body['has_key']} models={len(body['model_profiles'])}"
    )
    if active.get("base_url"):
        text += f" base_url={active['base_url']}"
    return CliResult(data=body, text=text)


def _models_list(args: Any, ctx: CliContext) -> CliResult:
    profiles = ctx.app.owner_visible_model_profiles(ctx.owner, include_builtin_profiles=True)
    items = [profile.public_dict() for profile in profiles]
    text = "\n".join(
        f"{item['id']}  {item.get('model', '')}  provider={item.get('provider', '')} "
        f"loaded={item.get('loaded')} has_key={item.get('has_key')}"
        for item in items
    )
    return CliResult(data=items, text=text or "(无模型配置)")


def _models_add(args: Any, ctx: CliContext) -> CliResult:
    payload = _model_payload(args, require_id=True)
    profile = ctx.app.add_model(payload, owner_account_id=ctx.owner)
    data = profile.public_dict()
    return CliResult(data=data, text=f"已新增模型 {data['id']}")


def _models_update(args: Any, ctx: CliContext) -> CliResult:
    payload = _model_payload(args, require_id=False)
    if not payload:
        raise CliError("至少需要提供一个要更新的字段")
    profile = ctx.app.update_model(args.model_id, payload, owner_account_id=ctx.owner)
    data = profile.public_dict()
    return CliResult(data=data, text=f"已更新模型 {data['id']}")


async def _models_delete(args: Any, ctx: CliContext) -> CliResult:
    from crew.state.session_model import rebind_sessions_from_model, sessions_using_model

    app = ctx.app
    model_id = args.model_id
    owner = ctx.owner
    hits = sessions_using_model(app.session_store, model_id, owner_account_id=owner)
    busy_sessions = [
        hit["session_id"]
        for hit in hits
        if app.dispatcher.status(hit["session_id"], owner_account_id=owner).get("live")
        in ("running", "queued")
    ]
    if busy_sessions and not args.force:
        raise CliError(f"有会话正在使用该模型，请先停止或使用 --force: {busy_sessions}")
    if args.force:
        for sid in busy_sessions:
            await app.dispatcher.stop(sid, owner_account_id=owner)

    visible_profiles = app.owner_model_profiles(owner)
    fallback_model_id = app.config.owner_default_model_id(owner)
    if fallback_model_id == model_id:
        fallback_model_id = next(
            (
                candidate_id
                for candidate_id, candidate in sorted(visible_profiles.items())
                if candidate_id != model_id and candidate.loaded
            ),
            fallback_model_id,
        )
    rebound = rebind_sessions_from_model(
        app.session_store,
        app.config,
        visible_profiles,
        model_id,
        owner_account_id=owner,
        to_model_id=fallback_model_id,
        fallback_model_id=fallback_model_id,
    )
    for sid in rebound:
        app.agents.drop(sid, owner_account_id=owner)
    result = app.remove_model(model_id, owner_account_id=owner)
    data = {
        "ok": True,
        "removed": result["removed"].public_dict(),
        "rebound_sessions": rebound,
        "switched_to": result.get("switched_to"),
    }
    return CliResult(data=data, text=f"已删除模型 {model_id}")


def _models_use(args: Any, ctx: CliContext) -> CliResult:
    try:
        profile = ctx.app.use_model(args.model_id, owner_account_id=ctx.owner)
    except KeyError as exc:
        raise CliError(f"未知模型配置: {args.model_id}") from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    data = profile.public_dict()
    return CliResult(data=data, text=f"默认模型已切换为 {data['id']}")


def _require_session(app: Any, session_id: str, owner: str) -> None:
    if not app.session_store.session_belongs_to(session_id, owner):
        raise CliError(f"会话不存在: {session_id}", exit_code=404)


def _history_items(msgs: list[Any]) -> list[dict[str, Any]]:
    items = []
    for message in msgs:
        if message.is_meta:
            continue
        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp,
        }
        if message.thinking is not None:
            item["thinking"] = message.thinking
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "status": tc.status,
                }
                for tc in message.tool_calls
            ]
        items.append(item)
    return items


def _register_session(subparsers) -> None:
    parser = subparsers.add_parser("session", help="会话管理")
    cmds = parser.add_subparsers(dest="session_cmd")

    def _id_arg(p, required: bool = True):
        p.add_argument("--id", dest="session_id", required=required)

    lst = cmds.add_parser("list", help="列出会话")
    lst.add_argument("--workspace-id")
    lst.add_argument("--include-archived", action="store_true")
    lst.set_defaults(handler=_session_list)

    show = cmds.add_parser("show", help="查看会话历史")
    _id_arg(show)
    show.set_defaults(handler=_session_show)

    ensure = cmds.add_parser("ensure", help="创建/确认会话")
    _id_arg(ensure)
    ensure.add_argument("--title", default="CLI 会话")
    ensure.set_defaults(handler=_session_ensure)

    title = cmds.add_parser("title", help="修改会话标题")
    _id_arg(title)
    title.add_argument("--title", required=True)
    title.set_defaults(handler=_session_title)

    archive = cmds.add_parser("archive", help="归档会话")
    _id_arg(archive)
    archive.add_argument("--unarchive", action="store_true")
    archive.set_defaults(handler=_session_archive)

    pin = cmds.add_parser("pin", help="置顶会话")
    _id_arg(pin)
    pin.add_argument("--unpin", action="store_true")
    pin.set_defaults(handler=_session_pin)

    todos = cmds.add_parser("todos", help="查看会话任务清单")
    _id_arg(todos)
    todos.set_defaults(handler=_session_todos)

    plan = cmds.add_parser("plan", help="查看会话计划")
    _id_arg(plan)
    plan.set_defaults(handler=_session_plan)

    model = cmds.add_parser("model", help="查看/切换会话模型")
    _id_arg(model)
    model.add_argument("--set", dest="model_profile_id")
    model.set_defaults(handler=_session_model)

    agent_config = cmds.add_parser("agent-config", help="查看/设置会话 Agent 配置")
    _id_arg(agent_config)
    agent_config.add_argument("--set", dest="config_json", help="Agent 配置 JSON")
    agent_config.set_defaults(handler=_session_agent_config)

    status = cmds.add_parser("status", help="查看会话运行状态")
    _id_arg(status)
    status.set_defaults(handler=_session_status)

    statuses = cmds.add_parser("statuses", help="批量查看会话状态")
    statuses.set_defaults(handler=_session_statuses)

    context = cmds.add_parser("context", help="查看会话上下文用量")
    _id_arg(context)
    context.set_defaults(handler=_session_context)

    usage = cmds.add_parser("usage", help="查看 owner 用量")
    usage.set_defaults(handler=_session_usage)

    delete = cmds.add_parser("delete", help="删除会话")
    _id_arg(delete)
    delete.set_defaults(handler=_session_delete)


def _session_list(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    rows = app.session_store.list_sessions(
        args.workspace_id or None,
        owner_account_id=ctx.owner,
        include_archived=args.include_archived,
    )
    items = with_session_agent_labels(app, rows, owner_account_id=ctx.owner)
    text = "\n".join(
        f"{row['session_id']}  {row.get('title', '')}  ws={row.get('workspace_id', '')}"
        for row in items
    )
    return CliResult(data=items, text=text or "(无会话)")


def _session_show(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    msgs = app.session_store.load(args.session_id, owner_account_id=ctx.owner)
    items = _history_items(msgs)
    return CliResult(data=items, text=f"{len(items)} 条消息")


def _session_ensure(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    try:
        app.session_store.ensure_session(
            args.session_id,
            workspace_id=ctx.workspace_id,
            title=args.title,
            owner_account_id=ctx.owner,
        )
    except SessionOwnershipError as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data={"ok": True, "session_id": args.session_id}, text="会话已就绪")


def _session_title(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    app.session_store.set_title(args.session_id, args.title, owner_account_id=ctx.owner)
    return CliResult(data={"ok": True}, text="标题已更新")


def _session_archive(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    archived = not args.unarchive
    app.session_store.set_archived(args.session_id, archived, owner_account_id=ctx.owner)
    return CliResult(data={"ok": True, "archived": archived})


def _session_pin(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    pinned = not args.unpin
    app.session_store.set_pinned(args.session_id, pinned, owner_account_id=ctx.owner)
    return CliResult(data={"ok": True, "pinned": pinned})


def _session_todos(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    if app.plan_manager is None:
        return CliResult(data={"todos": []}, text="(无任务清单)")
    todos = app.plan_manager.todo_store(args.session_id, owner_account_id=ctx.owner).read()
    text = "\n".join(f"{it['id']}. {it['content']} ({it['status']})" for it in todos)
    return CliResult(data={"todos": todos}, text=text or "(任务清单为空)")


def _session_plan(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.plan import plan_display_path, read_plan

    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    pm = app.plan_manager
    plan = read_plan(args.session_id, owner_account_id=ctx.owner)
    data = {
        "session_id": args.session_id,
        "active": bool(pm is not None and pm.is_active(args.session_id, owner_account_id=ctx.owner)),
        "awaiting_approval": bool(
            pm is not None and pm.is_awaiting_approval(args.session_id, owner_account_id=ctx.owner)
        ),
        "phase": pm.phase(args.session_id, owner_account_id=ctx.owner) if pm is not None else "inactive",
        "plan": plan or "",
        "plan_file": plan_display_path(args.session_id, owner_account_id=ctx.owner),
        "has_plan": bool(plan),
    }
    return CliResult(data=data, text=data["plan"] or "(无计划)")


def _session_model(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    if not args.model_profile_id:
        _require_session(app, args.session_id, ctx.owner)
        return CliResult(data=app.read_session_model_binding(args.session_id, owner_account_id=ctx.owner))
    ensure = getattr(app.session_store, "ensure_session", None)
    if callable(ensure):
        try:
            ensure(
                args.session_id,
                workspace_id=ctx.workspace_id,
                title="CLI 会话",
                owner_account_id=ctx.owner,
            )
        except SessionOwnershipError as exc:
            raise CliError(str(exc)) from exc
    busy = app.dispatcher.status(args.session_id, owner_account_id=ctx.owner).get("live") in (
        "running",
        "queued",
    )
    try:
        body = app.set_session_model_binding(
            args.session_id,
            args.model_profile_id,
            owner_account_id=ctx.owner,
            busy=busy,
        )
    except KeyError as exc:
        raise CliError(str(exc)) from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data=body)


def _session_agent_config(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    if args.config_json is None:
        config = app.session_store.get_agent_config(args.session_id, owner_account_id=ctx.owner) or {}
        return CliResult(data=config)
    config = parse_json(args.config_json, name="Agent 配置")
    if not isinstance(config, dict):
        raise CliError("Agent 配置必须是 JSON 对象")
    executor = str(config.get("executor") or "").strip().lower()
    if executor not in {"builtin", "client", "external", "acp", "team"}:
        raise CliError("executor 必须是 builtin | client | external | acp | team")
    stored = app.session_store.set_agent_config(
        args.session_id,
        config,
        owner_account_id=ctx.owner,
    )
    app.agents.drop(args.session_id, owner_account_id=ctx.owner)
    return CliResult(data=stored)


def _session_status(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    return CliResult(data=app.dispatcher.status(args.session_id, owner_account_id=ctx.owner))


def _session_statuses(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    result = {}
    for row in app.session_store.list_sessions(owner_account_id=ctx.owner):
        sid = row["session_id"]
        state = app.dispatcher.status(sid, owner_account_id=ctx.owner)
        if state["live"] in ("running", "queued"):
            result[sid] = state["live"]
        elif state["last_status"] == "failed":
            result[sid] = "error"
        else:
            result[sid] = "idle"
    return CliResult(data=result)


def _session_context(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    data = app.session_store.context_usage(
        args.session_id,
        app.resolve_session_context_window(args.session_id, ctx.owner),
        owner_account_id=ctx.owner,
    )
    return CliResult(data=data)


def _session_usage(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data=ctx.app.session_store.total_usage(owner_account_id=ctx.owner))


async def _session_delete(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    _require_session(app, args.session_id, ctx.owner)
    block = _session_has_blocking_cron(app, args.session_id, ctx.owner)
    if block:
        raise CliError(block, exit_code=409)
    await hook_registry.emit("session:end", {"session_id": args.session_id, "owner_account_id": ctx.owner})
    await _teardown_session_resources(app, args.session_id, ctx.owner)
    if app.dynamic_kanban is not None:
        app.dynamic_kanban.interrupt(args.session_id, owner_account_id=ctx.owner)
        app.dynamic_kanban.clear_session_workspaces(args.session_id, owner_account_id=ctx.owner)
    app.session_store.clear(args.session_id, owner_account_id=ctx.owner)
    return CliResult(data={"ok": True}, text="会话已删除")


def _register_workspace(subparsers) -> None:
    parser = subparsers.add_parser("workspace", help="工作空间管理")
    cmds = parser.add_subparsers(dest="workspace_cmd")

    lst = cmds.add_parser("list", help="列出工作空间")
    lst.set_defaults(handler=_workspace_list)

    create = cmds.add_parser("create", help="创建工作空间")
    create.add_argument("--name", default="新工作空间")
    create.add_argument("--description", default="")
    create.add_argument("--instructions", default="")
    create.add_argument("--root-path", default="")
    create.set_defaults(handler=_workspace_create)

    update = cmds.add_parser("update", help="更新工作空间")
    update.add_argument("--id", dest="workspace_id", required=True)
    update.add_argument("--name")
    update.add_argument("--description")
    update.add_argument("--instructions")
    update.add_argument("--root-path")
    update.set_defaults(handler=_workspace_update)

    delete = cmds.add_parser("delete", help="删除工作空间")
    delete.add_argument("--id", dest="workspace_id", required=True)
    delete.set_defaults(handler=_workspace_delete)


def _workspace_list(args: Any, ctx: CliContext) -> CliResult:
    items = ctx.app.workspace_store.list(owner_account_id=ctx.owner)
    text = "\n".join(
        f"{row['id']}  {row.get('name', '')}  root={row.get('root_path', '')}" for row in items
    )
    return CliResult(data=items, text=text or "(无工作空间)")


def _workspace_create(args: Any, ctx: CliContext) -> CliResult:
    from crew.state.workspace_store import _normalize_root_path

    if args.root_path and not _normalize_root_path(args.root_path):
        raise CliError("root_path 不是有效目录")
    row = ctx.app.workspace_store.create(
        name=args.name,
        description=args.description,
        instructions=args.instructions,
        root_path=args.root_path,
        owner_account_id=ctx.owner,
    )
    return CliResult(data=row, text=f"已创建工作空间 {row['id']}")


def _workspace_update(args: Any, ctx: CliContext) -> CliResult:
    payload = {}
    for field in ("name", "description", "instructions", "root_path"):
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    if not payload:
        raise CliError("至少需要提供一个要更新的字段")
    try:
        row = ctx.app.workspace_store.update(
            args.workspace_id,
            owner_account_id=ctx.owner,
            **payload,
        )
    except KeyError as exc:
        raise CliError(str(exc)) from exc
    return CliResult(data=row, text="工作空间已更新")


async def _workspace_delete(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    owner = ctx.owner
    workspace_id = args.workspace_id
    sessions = app.session_store.list_sessions(workspace_id, owner_account_id=owner)
    session_ids = [str(row.get("session_id") or "") for row in sessions if row.get("session_id")]
    for sid in session_ids:
        block = _session_has_blocking_cron(app, sid, owner)
        if block:
            raise CliError(block, exit_code=409)
    snapshots = {
        sid: list(app.session_store.load(sid, owner_account_id=owner) or []) for sid in session_ids
    }

    def _delete_workspace(conn):
        deleted = app.session_store.delete_sessions_for_workspace(
            workspace_id,
            owner_account_id=owner,
            writer=conn,
        )
        app.workspace_store.delete(workspace_id, owner_account_id=owner, writer=conn)
        return deleted

    tx = getattr(app.session_store, "transaction", None)
    deleted_sessions = tx(_delete_workspace) if callable(tx) else None
    if deleted_sessions is None:
        raise CliError("会话存储不支持事务删除工作空间")
    for sid in session_ids:
        await hook_registry.emit("session:end", {"session_id": sid, "owner_account_id": owner})
        await _teardown_session_resources(
            app,
            sid,
            owner,
            messages_snapshot=snapshots.get(sid),
        )
        if app.dynamic_kanban is not None:
            app.dynamic_kanban.interrupt(sid, owner_account_id=owner)
            app.dynamic_kanban.clear_session_workspaces(sid, owner_account_id=owner)
    return CliResult(
        data={"ok": True, "deleted_sessions": deleted_sessions},
        text=f"已删除工作空间，级联 {len(deleted_sessions)} 个会话",
    )


def _register_task(subparsers) -> None:
    parser = subparsers.add_parser("task", help="后台任务管理")
    cmds = parser.add_subparsers(dest="task_cmd")

    lst = cmds.add_parser("list", help="列出任务")
    lst.add_argument("--session-id")
    lst.add_argument("--status")
    lst.add_argument("--limit", type=int, default=200)
    lst.set_defaults(handler=_task_list)

    show = cmds.add_parser("show", help="查看任务")
    show.add_argument("--id", dest="task_id", required=True)
    show.set_defaults(handler=_task_show)

    cancel = cmds.add_parser("cancel", help="取消任务")
    cancel.add_argument("--id", dest="task_id", required=True)
    cancel.add_argument("--reason", default="用户取消")
    cancel.set_defaults(handler=_task_cancel)

    wait = cmds.add_parser("wait", help="等待任务完成")
    wait.add_argument("--id", dest="task_id", required=True)
    wait.add_argument("--timeout", type=float)
    wait.set_defaults(handler=_task_wait)

    concurrency = cmds.add_parser("concurrency", help="查看运行时并发")
    concurrency.set_defaults(handler=_task_concurrency)


def _task_list(args: Any, ctx: CliContext) -> CliResult:
    if args.session_id and not ctx.app.session_store.session_belongs_to(
        args.session_id, ctx.owner
    ):
        raise CliError(f"会话不存在: {args.session_id}", exit_code=404)
    items = ctx.app.tasks.list_tasks(
        session_id=args.session_id,
        status=args.status,
        limit=args.limit,
        owner_account_id=ctx.owner,
    )
    text = "\n".join(
        f"{row.get('task_id')}  {row.get('status')}  {row.get('kind', '')}  "
        f"{str(row.get('prompt') or row.get('description') or '')[:60]}"
        for row in items
    )
    return CliResult(data=items, text=text or "(无任务)")


def _task_show(args: Any, ctx: CliContext) -> CliResult:
    try:
        return CliResult(data=ctx.app.tasks.get(args.task_id, owner_account_id=ctx.owner))
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc


async def _task_cancel(args: Any, ctx: CliContext) -> CliResult:
    try:
        task = await ctx.app.tasks.cancel(
            args.task_id,
            reason=args.reason,
            owner_account_id=ctx.owner,
        )
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data=task, text=f"任务已取消 {args.task_id}")


async def _task_wait(args: Any, ctx: CliContext) -> CliResult:
    try:
        task = await ctx.app.tasks.wait(
            args.task_id,
            timeout=args.timeout,
            owner_account_id=ctx.owner,
        )
    except KeyError as exc:
        raise CliError(str(exc), exit_code=404) from exc
    return CliResult(data=task)


def _task_concurrency(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data=ctx.app.dispatcher.runtime_status())


def _cron_job_view(job: dict[str, Any]) -> dict[str, Any]:
    from crew.cron.jobs import format_bj_timestamp

    return {
        "id": job.get("id", ""),
        "name": job.get("name", ""),
        "kind": job.get("kind", "once"),
        "schedule": job.get("schedule", ""),
        "trigger_type": job.get("trigger_type", ""),
        "trigger_payload": job.get("trigger_payload") or {},
        "query": job.get("query", ""),
        "session_id": job.get("session_id", ""),
        "workspace_id": job.get("workspace_id", "default"),
        "deliver": job.get("deliver", ""),
        "enabled": bool(job.get("enabled")),
        "last_status": job.get("last_status", ""),
        "next_run_at": float(job.get("next_run_at") or 0),
        "next_run_at_bj": format_bj_timestamp(job.get("next_run_at")),
        "last_run_at_bj": format_bj_timestamp(job.get("last_run_at")),
        "created_at_bj": format_bj_timestamp(job.get("created_at")),
    }


def _register_cron(subparsers) -> None:
    parser = subparsers.add_parser("cron", help="定时任务管理")
    cmds = parser.add_subparsers(dest="cron_cmd")

    lst = cmds.add_parser("list", help="列出定时任务")
    lst.add_argument("--session-id")
    lst.set_defaults(handler=_cron_list)

    stats = cmds.add_parser("stats", help="定时任务统计")
    stats.set_defaults(handler=_cron_stats)

    show = cmds.add_parser("show", help="查看定时任务")
    show.add_argument("--id", dest="job_id", required=True)
    show.add_argument("--limit", type=int, default=20)
    show.set_defaults(handler=_cron_show)

    create = cmds.add_parser("create", help="创建定时任务")
    create.add_argument("--name", required=True)
    create.add_argument("--schedule", required=True, help="如 every 30m / 每天9点 / in 1h")
    create.add_argument("--query", required=True)
    create.add_argument("--session-id", required=True)
    create.add_argument("--workspace-id")
    create.add_argument("--deliver", default="", help="空=新建本地会话；local=回写原会话")
    create.set_defaults(handler=_cron_create)

    pause = cmds.add_parser("pause", help="暂停定时任务")
    pause.add_argument("--id", dest="job_id", required=True)
    pause.set_defaults(handler=_cron_pause)

    resume = cmds.add_parser("resume", help="恢复定时任务")
    resume.add_argument("--id", dest="job_id", required=True)
    resume.set_defaults(handler=_cron_resume)

    run = cmds.add_parser("run", help="立即执行定时任务")
    run.add_argument("--id", dest="job_id", required=True)
    run.set_defaults(handler=_cron_run)

    delete = cmds.add_parser("delete", help="删除定时任务")
    delete.add_argument("--id", dest="job_id", required=True)
    delete.set_defaults(handler=_cron_delete)

    retry = cmds.add_parser("retry", help="重试失败的 Fire")
    retry.add_argument("--fire-id", dest="fire_id", type=int, required=True)
    retry.set_defaults(handler=_cron_retry)


def _cron_store(app: Any):
    store = getattr(app, "cron_store", None)
    if store is None:
        raise CliError("cron store 未初始化")
    return store


def _cron_list(args: Any, ctx: CliContext) -> CliResult:
    jobs = _cron_store(ctx.app).list(session_id=args.session_id, owner_account_id=ctx.owner)
    items = [_cron_job_view(job) for job in jobs]
    text = "\n".join(
        f"{item['id']}  {item['name']}  enabled={item['enabled']}  {item['schedule']}"
        for item in items
    )
    return CliResult(data=items, text=text or "(无定时任务)")


def _cron_stats(args: Any, ctx: CliContext) -> CliResult:
    jobs = _cron_store(ctx.app).list(owner_account_id=ctx.owner)
    now = time.time()
    total = len(jobs)
    enabled = sum(1 for job in jobs if job.get("enabled"))
    interval = sum(1 for job in jobs if job.get("kind") in ("interval", "cron"))
    once = sum(1 for job in jobs if job.get("kind") == "once")
    failed_recent = sum(
        1
        for job in jobs
        if str(job.get("last_status", "")).startswith("failed")
        and job.get("last_run_at", 0) > 0
        and (now - float(job["last_run_at"])) < 86400
    )
    upcoming = sum(
        1
        for job in jobs
        if job.get("enabled") and 0 < float(job.get("next_run_at") or 0) - now < 60
    )
    data = {
        "ok": True,
        "total": total,
        "enabled": enabled,
        "disabled": total - enabled,
        "interval": interval,
        "once": once,
        "failed_recent": failed_recent,
        "upcoming_60s": upcoming,
    }
    return CliResult(data=data, text=f"total={total} enabled={enabled}")


def _cron_show(args: Any, ctx: CliContext) -> CliResult:
    store = _cron_store(ctx.app)
    job = store.get(args.job_id, owner_account_id=ctx.owner)
    if job is None:
        raise CliError(f"任务不存在: {args.job_id}", exit_code=404)
    runs = store.get_job_runs(args.job_id, limit=max(1, args.limit))
    summary = store.get_job_run_summary(args.job_id)
    return CliResult(
        data={"job": _cron_job_view(job), "runs": runs, "run_summary": summary},
        text=_cron_job_view(job)["schedule"],
    )


def _cron_create(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    store = _cron_store(app)
    if not (args.name and args.schedule and args.query):
        raise CliError("name / schedule / query 均不能为空")
    if not app.session_store.session_belongs_to(args.session_id, ctx.owner):
        ensure = getattr(app.session_store, "ensure_session", None)
        if callable(ensure):
            ensure(
                args.session_id,
                workspace_id=args.workspace_id or ctx.workspace_id,
                owner_account_id=ctx.owner,
            )
    if not app.session_store.session_belongs_to(args.session_id, ctx.owner):
        raise CliError(f"会话不存在: {args.session_id}", exit_code=404)
    workspace_id = args.workspace_id or ctx.workspace_id
    if not args.workspace_id:
        for row in app.session_store.list_sessions(owner_account_id=ctx.owner):
            if row.get("session_id") == args.session_id:
                workspace_id = str(row.get("workspace_id") or ctx.workspace_id)
                break
    try:
        job = store.create(
            name=args.name,
            schedule=args.schedule,
            query=args.query,
            session_id=args.session_id,
            workspace_id=workspace_id,
            deliver=args.deliver,
            owner_account_id=ctx.owner,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    service = getattr(app, "cron_service", None)
    if service is not None and service.is_running:
        service.sync_job(str(job["id"]), owner_account_id=ctx.owner)
    view = _cron_job_view(job)
    return CliResult(data=view, text=f"已创建定时任务 {view['id']}")


def _cron_set_enabled(args: Any, ctx: CliContext, enabled: bool) -> CliResult:
    app = ctx.app
    store = _cron_store(app)
    job = store.get(args.job_id, owner_account_id=ctx.owner)
    if job is None:
        raise CliError(f"任务不存在: {args.job_id}", exit_code=404)
    store.set_enabled(args.job_id, enabled, owner_account_id=ctx.owner)
    service = getattr(app, "cron_service", None)
    if service is not None and service.is_running:
        service.sync_job(args.job_id, owner_account_id=ctx.owner)
    refreshed = store.get(args.job_id, owner_account_id=ctx.owner) or job
    view = _cron_job_view(refreshed)
    return CliResult(data=view, text=("已恢复" if enabled else "已暂停") + f" {view['id']}")


def _cron_pause(args: Any, ctx: CliContext) -> CliResult:
    return _cron_set_enabled(args, ctx, False)


def _cron_resume(args: Any, ctx: CliContext) -> CliResult:
    return _cron_set_enabled(args, ctx, True)


async def _cron_run(args: Any, ctx: CliContext) -> CliResult:
    async with ctx.running_app() as app:
        store = _cron_store(app)
        job = store.get(args.job_id, owner_account_id=ctx.owner)
        if job is None:
            raise CliError(f"任务不存在: {args.job_id}", exit_code=404)
        service = getattr(app, "cron_service", None)
        if service is None or not service.is_running:
            raise CliError("CronService 未启用")
        _mount_cron_owner(service, ctx.owner)
        result = await service.run_now(args.job_id, owner_account_id=ctx.owner)
        return CliResult(data={"ok": True, "job": _cron_job_view(job), "result": result})


async def _cron_retry(args: Any, ctx: CliContext) -> CliResult:
    async with ctx.running_app() as app:
        store = _cron_store(app)
        source = store.get_fire(args.fire_id, owner_account_id=ctx.owner)
        if source is None:
            raise CliError(f"Fire 不存在: {args.fire_id}", exit_code=404)
        if str(source.get("status") or "") not in {"failed", "abandoned", "cancelled_by_logout"}:
            raise CliError("只有失败、遗弃或因退出取消的 Fire 可人工重试", exit_code=409)
        service = getattr(app, "cron_service", None)
        if service is None or not service.is_running:
            raise CliError("CronService 未启用")
        _mount_cron_owner(service, ctx.owner)
        await service.retry_fire(args.fire_id, owner_account_id=ctx.owner)
        return CliResult(data={"ok": True, "source_fire_id": args.fire_id})


def _mount_cron_owner(service: Any, owner: str) -> None:
    if owner in service.mounted_owners:
        return
    try:
        service.mount_owner(owner)
    except RuntimeError as exc:
        raise CliError(str(exc)) from exc


def _cron_delete(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    store = _cron_store(app)
    job = store.get(args.job_id, owner_account_id=ctx.owner)
    if job is None:
        raise CliError(f"任务不存在: {args.job_id}", exit_code=404)
    store.delete(args.job_id, owner_account_id=ctx.owner)
    service = getattr(app, "cron_service", None)
    if service is not None and service.is_running:
        service.sync_job(args.job_id, owner_account_id=ctx.owner)
    return CliResult(data={"ok": True, "id": args.job_id}, text="定时任务已删除")


def _register_system(subparsers) -> None:
    parser = subparsers.add_parser("system", help="系统健康与监控")
    cmds = parser.add_subparsers(dest="system_cmd")

    health = cmds.add_parser("health", help="检查健康状态")
    health.set_defaults(handler=_system_health)

    metrics = cmds.add_parser("metrics", help="查看资源指标")
    metrics.set_defaults(handler=_system_metrics)

    logs = cmds.add_parser("logs", help="查询进程内日志")
    logs.add_argument("--level")
    logs.add_argument("--q")
    logs.add_argument("--limit", type=int, default=500)
    logs.add_argument("--offset", type=int, default=0)
    logs.set_defaults(handler=_system_logs)

    tools = cmds.add_parser("tools", help="列出已注册工具")
    tools.set_defaults(handler=_system_tools)

    toolsets = cmds.add_parser("toolsets", help="列出工具集目录")
    toolsets.set_defaults(handler=_system_toolsets)


def _system_health(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(
        data={"ok": True, "service": "crew-cli", "owner": ctx.owner},
        text="ok",
    )


def _gb(n: float) -> float:
    return round(n / (1024**3), 1)


def _system_metrics(args: Any, ctx: CliContext) -> CliResult:
    payload: dict[str, Any] = {"uptime_s": round(time.monotonic(), 0), "cpu_count": os.cpu_count() or 1}
    try:
        du = shutil.disk_usage(os.getcwd())
        payload["disk"] = {
            "total_gb": _gb(du.total),
            "used_gb": _gb(du.used),
            "free_gb": _gb(du.free),
            "percent": round(du.used / du.total * 100, 1) if du.total else 0,
        }
    except OSError:
        pass
    try:
        import psutil

        vm = psutil.virtual_memory()
        payload["cpu_percent"] = psutil.cpu_percent(interval=None)
        payload["memory"] = {
            "total_gb": _gb(vm.total),
            "used_gb": _gb(vm.used),
            "percent": round(vm.percent, 1),
        }
        net = psutil.net_io_counters()
        payload["network"] = {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv}
        try:
            proc = psutil.Process()
            mem_info = proc.memory_info()
            payload["process"] = {"rss_mb": round(mem_info.rss / (1024**2), 1), "pid": proc.pid}
        except psutil.Error:
            pass
    except ImportError:
        payload["psutil_unavailable"] = True
    return CliResult(data=payload)


def _system_logs(args: Any, ctx: CliContext) -> CliResult:
    result = query_logs(
        level=args.level,
        keyword=args.q,
        owner_account_id=ctx.owner,
        limit=args.limit,
        offset=args.offset,
    )
    return CliResult(data=result)


def _system_tools(args: Any, ctx: CliContext) -> CliResult:
    registry = getattr(ctx.app, "registry", None)
    if registry is None:
        return CliResult(data=[])
    items = []
    for name in sorted(registry.names()):
        items.append(
            {
                "name": name,
                "toolset": registry.toolset_for(name) or "default",
                "display_name": registry.ui_meta(name).get("display_name", ""),
            }
        )
    text = "\n".join(f"{item['name']}  {item['toolset']}" for item in items)
    return CliResult(data=items, text=text or "(无工具)")


def _system_toolsets(args: Any, ctx: CliContext) -> CliResult:
    registry = getattr(ctx.app, "registry", None)
    if registry is None:
        return CliResult(data=[])
    return CliResult(data=registry.toolsets())


def _register_kanban(subparsers) -> None:
    parser = subparsers.add_parser("kanban", help="Dynamic Kanban 动态看板")
    cmds = parser.add_subparsers(dest="kanban_cmd")
    board = cmds.add_parser("board")
    board.add_argument("--session-id", required=True)
    board.set_defaults(handler=_kanban_board)
    status = cmds.add_parser("status")
    status.add_argument("--session-id", required=True)
    status.set_defaults(handler=_kanban_status)
    pause = cmds.add_parser("pause")
    pause.add_argument("--session-id", required=True)
    pause.add_argument("--reason", default="用户请求暂停")
    pause.set_defaults(handler=_kanban_pause)
    resume = cmds.add_parser("resume")
    resume.add_argument("--session-id", required=True)
    resume.set_defaults(handler=_kanban_resume)


def _kanban_manager(app: Any):
    manager = getattr(app, "dynamic_kanban", None)
    if manager is None:
        raise CliError("Dynamic Kanban 未启用", exit_code=503)
    return manager


def _kanban_board(args: Any, ctx: CliContext) -> CliResult:
    manager = _kanban_manager(ctx.app)
    store = manager.store.for_owner(ctx.owner)
    workflow = store.get_latest_workflow_by_session(args.session_id, exclude_source="team")
    if workflow is None:
        raise CliError("该会话暂无 Dynamic Kanban 工作流", exit_code=404)
    board = store.get_board_state(workflow.id)
    board["workflow"] = workflow.to_dict()
    return CliResult(data=board)


def _kanban_status(args: Any, ctx: CliContext) -> CliResult:
    status = _kanban_manager(ctx.app).status(args.session_id, owner_account_id=ctx.owner)
    if status is None:
        raise CliError("该会话暂无 workflow", exit_code=404)
    return CliResult(data=status)


def _kanban_pause(args: Any, ctx: CliContext) -> CliResult:
    manager = _kanban_manager(ctx.app)
    ok = manager.pause(args.session_id, reason=args.reason, owner_account_id=ctx.owner)
    if not ok:
        raise CliError("没有运行中的 workflow", exit_code=404)
    return CliResult(data={"ok": True, "session_id": args.session_id, "reason": args.reason})


async def _kanban_resume(args: Any, ctx: CliContext) -> CliResult:
    from crew.core.envelope import Envelope

    manager = _kanban_manager(ctx.app)
    envelope = Envelope(
        session_id=args.session_id,
        params={"query": "继续执行"},
        request_id=f"resume_{args.session_id}_{time.monotonic()}",
        channel="cli",
        user_id=ctx.owner,
        workspace_id=ctx.workspace_id,
        mode="dynamic_kanban",
    )
    chunks = []
    async for chunk in manager.resume_stream(args.session_id, envelope.request_id, envelope):
        chunks.append(chunk)
    final_text = ""
    for chunk in chunks:
        if chunk.kind == "final":
            final_text = str(chunk.body.get("text") or "")
    return CliResult(
        data={"session_id": args.session_id, "chunks": len(chunks), "text": final_text},
        text=final_text or f"已恢复执行（{len(chunks)} 个事件）",
    )


def _register_scenario(subparsers) -> None:
    parser = subparsers.add_parser("scenario", help="场景推荐")
    cmds = parser.add_subparsers(dest="scenario_cmd")
    recommend = cmds.add_parser("list")
    recommend.add_argument("--count", type=int, default=4)
    recommend.set_defaults(handler=_scenario_list)
    all_scenarios = cmds.add_parser("all")
    all_scenarios.set_defaults(handler=_scenario_all)
    intro = cmds.add_parser("intro-lines")
    intro.add_argument("--count", type=int, default=8)
    intro.set_defaults(handler=_scenario_intro)
    loading = cmds.add_parser("loading-status")
    loading.add_argument("--count", type=int, default=8)
    loading.set_defaults(handler=_scenario_loading)


def _scenario_list(args: Any, ctx: CliContext) -> CliResult:
    from crew.scenarios import recommend

    return CliResult(data=recommend(args.count))


def _scenario_all(args: Any, ctx: CliContext) -> CliResult:
    from crew.scenarios import get_scenarios

    return CliResult(data=get_scenarios())


def _scenario_intro(args: Any, ctx: CliContext) -> CliResult:
    from crew.scenarios import recommend_intro_lines

    return CliResult(data=recommend_intro_lines(args.count))


def _scenario_loading(args: Any, ctx: CliContext) -> CliResult:
    from crew.scenarios import recommend_loading_statuses

    return CliResult(data=recommend_loading_statuses(args.count))


def _register_migrate(subparsers) -> None:
    parser = subparsers.add_parser("migrate", help="数据迁移工具")
    cmds = parser.add_subparsers(dest="migrate_cmd")
    claim = cmds.add_parser("claim-legacy", help="认领未归属数据")
    claim.add_argument("--account", required=True, help="目标 owner_account_id，例如 owner:user-a")
    claim.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    claim.set_defaults(handler=_claim_legacy)


def _claim_legacy(args: Any, ctx: CliContext) -> CliResult:
    from crew.state._migration import OWNER_TABLE_LABELS, claim_legacy_owner_database

    app = ctx.app
    owner = args.account.strip()
    if not owner:
        raise CliError("--account 不能为空")
    changed, remaining = claim_legacy_owner_database(
        app.config.db_path,
        owner,
        dry_run=bool(args.dry_run),
        wal_enabled=app.config.sqlite_wal,
    )
    verb = "将认领" if args.dry_run else "已认领"
    text = "\n".join(
        f"{verb} {count} 条{OWNER_TABLE_LABELS.get(table, table)}"
        for table, count in changed.items()
        if count
    )
    if not any(changed.values()):
        text = "没有未归属数据。"
    remaining_view = {table: count for table, count in remaining.items() if count}
    return CliResult(data={"changed": changed, "remaining": remaining_view}, text=text)


__all__ = ["register"]
