"""知识管理命令：Wiki（知识库/页面/来源/入库）与 Skill。

Skill 支持安装、卸载与自进化配置。
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, cast

from crew.cli.app import CliContext, CliError, CliResult, parse_json
from crew.features.runtime import FeatureLease, FeatureLeaseUnavailableError
from crew.wiki.schemas import WikiPage, WikiRelation
from crew.wiki.service import (
    KNOWLEDGE_SERVICE_KEY,
    KnowledgeService,
    normalize_kb_id,
)

_active_knowledge_service: ContextVar[KnowledgeService | None] = ContextVar(
    "cli_active_knowledge_service", default=None
)


def register(subparsers, handlers: dict[str, Any]) -> None:
    _register_wiki(subparsers)
    _register_skill(subparsers)


def _kb_id(value: str) -> str:
    try:
        return normalize_kb_id(value)
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _service(ctx: CliContext) -> KnowledgeService:
    service = _active_knowledge_service.get()
    if service is None:
        raise CliError("Wiki 未启用")
    return service


def _acquire_service(
    ctx: CliContext, command: str
) -> tuple[KnowledgeService, FeatureLease | None]:
    """Resolve one current Wiki generation and keep its lease for one command."""
    plugins = getattr(ctx.app, "plugins", None)
    acquire = getattr(plugins, "acquire_service_lease", None)
    if callable(acquire):
        try:
            acquired = acquire(KNOWLEDGE_SERVICE_KEY, label=f"cli:wiki:{command}")
        except FeatureLeaseUnavailableError as exc:
            raise CliError("Wiki 未启用") from exc
        if acquired is not None:
            service, lease = acquired
            return cast(KnowledgeService, service), lease

    # Embedded hosts may provide an externally-owned service without a
    # product.wiki Feature record. It remains valid, but is never cached here.
    runtime = getattr(plugins, "feature_runtime", None)
    record = (
        runtime.get("product.wiki")
        if callable(getattr(runtime, "get", None))
        else None
    )
    if record is None:
        external = getattr(ctx.app, "knowledge_service", None)
        if external is not None:
            return cast(KnowledgeService, external), None
    raise CliError("Wiki 未启用")


def _lease_wiki_handler(handler: Callable[..., Any], command: str) -> Callable[..., Any]:
    """Gate each Wiki command at execution time, preserving sync/async behavior."""
    if inspect.iscoroutinefunction(handler):

        @wraps(handler)
        async def async_handler(args: Any, ctx: CliContext) -> Any:
            service, lease = _acquire_service(ctx, command)
            token = _active_knowledge_service.set(service)
            try:
                if lease is None:
                    return await handler(args, ctx)
                async with lease:
                    return await handler(args, ctx)
            finally:
                _active_knowledge_service.reset(token)

        return async_handler

    @wraps(handler)
    def sync_handler(args: Any, ctx: CliContext) -> Any:
        service, lease = _acquire_service(ctx, command)
        token = _active_knowledge_service.set(service)
        try:
            return handler(args, ctx)
        finally:
            _active_knowledge_service.reset(token)
            if lease is not None:
                lease.release()

    return sync_handler


def _set_wiki_handler(parser: Any, handler: Callable[..., Any], command: str) -> None:
    parser.set_defaults(handler=_lease_wiki_handler(handler, command))


def _register_wiki(subparsers) -> None:
    parser = subparsers.add_parser("wiki", help="Wiki 知识库管理")
    cmds = parser.add_subparsers(dest="wiki_cmd")

    init = cmds.add_parser("init", help="初始化知识库")
    init.add_argument("--kb-id", default="default")
    _set_wiki_handler(init, _wiki_init, "init")

    kbs = cmds.add_parser("kbs", help="知识库列表/创建/删除")
    kbs_cmds = kbs.add_subparsers(dest="wiki_kbs_cmd")
    _set_wiki_handler(kbs_cmds.add_parser("list"), _wiki_kbs_list, "kbs-list")
    create = kbs_cmds.add_parser("create")
    create.add_argument("--kb-id", required=True)
    create.add_argument("--name", default="")
    _set_wiki_handler(create, _wiki_kbs_create, "kbs-create")
    delete = kbs_cmds.add_parser("delete")
    delete.add_argument("--kb-id", required=True)
    _set_wiki_handler(delete, _wiki_kbs_delete, "kbs-delete")

    pages = cmds.add_parser("pages", help="Wiki 页面管理")
    pages_cmds = pages.add_subparsers(dest="wiki_pages_cmd")
    lst = pages_cmds.add_parser("list")
    lst.add_argument("--kb-id", default="default")
    lst.add_argument("--limit", type=int, default=100)
    lst.add_argument("--offset", type=int, default=0)
    lst.add_argument("--brief", action="store_true")
    _set_wiki_handler(lst, _wiki_pages_list, "pages-list")
    show = pages_cmds.add_parser("show")
    show.add_argument("--id", dest="page_id", required=True)
    show.add_argument("--kb-id", default="default")
    _set_wiki_handler(show, _wiki_pages_show, "pages-show")
    create_page = pages_cmds.add_parser("create")
    create_page.add_argument("--kb-id", default="default")
    create_page.add_argument("--title", required=True)
    create_page.add_argument("--page-type", default="topic")
    create_page.add_argument("--content", default="")
    create_page.add_argument("--sources", default="", help="逗号分隔的 source_id")
    create_page.add_argument("--tags", default="", help="逗号分隔的标签")
    create_page.add_argument("--relations", default="", help="relations JSON 数组")
    _set_wiki_handler(create_page, _wiki_pages_create, "pages-create")
    update_page = pages_cmds.add_parser("update")
    update_page.add_argument("--id", dest="page_id", required=True)
    update_page.add_argument("--kb-id", default="default")
    update_page.add_argument("--title")
    update_page.add_argument("--content")
    update_page.add_argument("--sources", help="逗号分隔的 source_id")
    update_page.add_argument("--tags", help="逗号分隔的标签")
    update_page.add_argument("--relations", help="relations JSON 数组")
    _set_wiki_handler(update_page, _wiki_pages_update, "pages-update")
    delete_page = pages_cmds.add_parser("delete")
    delete_page.add_argument("--id", dest="page_id", required=True)
    delete_page.add_argument("--kb-id", default="default")
    _set_wiki_handler(delete_page, _wiki_pages_delete, "pages-delete")

    search = cmds.add_parser("search", help="搜索 Wiki 页面")
    search.add_argument("--q", required=True)
    search.add_argument("--kb-id", default="default")
    search.add_argument("--top-k", type=int, default=5)
    _set_wiki_handler(search, _wiki_search, "search")

    sources = cmds.add_parser("sources", help="Wiki 数据源管理")
    sources_cmds = sources.add_subparsers(dest="wiki_sources_cmd")
    source_list = sources_cmds.add_parser("list")
    source_list.add_argument("--kb-id", default="default")
    source_list.add_argument("--status", default="all")
    source_list.add_argument("--limit", type=int, default=200)
    source_list.add_argument("--offset", type=int, default=0)
    _set_wiki_handler(source_list, _wiki_sources_list, "sources-list")
    source_delete = sources_cmds.add_parser("delete")
    source_delete.add_argument("--id", dest="source_id", required=True)
    source_delete.add_argument("--kb-id", default="default")
    _set_wiki_handler(source_delete, _wiki_sources_delete, "sources-delete")

    upload = cmds.add_parser("upload", help="上传文件到 Wiki")
    upload.add_argument("--file", required=True)
    upload.add_argument("--kb-id", default="default")
    _set_wiki_handler(upload, _wiki_upload, "upload")

    ingest = cmds.add_parser("ingest", help="编译入库一个数据源")
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--kb-id", default="default")
    ingest.add_argument("--session-id", default="")
    _set_wiki_handler(ingest, _wiki_ingest, "ingest")

    compile_all = cmds.add_parser("compile", help="全库重新编译")
    compile_all.add_argument("--kb-id", default="default")
    _set_wiki_handler(compile_all, _wiki_compile, "compile")

    graph = cmds.add_parser("graph", help="查看 Wiki 图谱")
    graph.add_argument("--kb-id", default="default")
    _set_wiki_handler(graph, _wiki_graph, "graph")

    query = cmds.add_parser("query", help="Wiki 检索问答")
    query.add_argument("--q", required=True)
    query.add_argument("--kb-id", default="default")
    _set_wiki_handler(query, _wiki_query, "query")

    lint = cmds.add_parser("lint", help="检查知识库页面质量")
    lint.add_argument("--kb-id", default="default")
    lint.add_argument("--deep", action="store_true")
    _set_wiki_handler(lint, _wiki_lint, "lint")

    sessions = cmds.add_parser("agent-sessions", help="列出 Wiki Agent 会话")
    sessions.add_argument("--kb-id", default="default")
    _set_wiki_handler(sessions, _wiki_agent_sessions, "agent-sessions")

    session = cmds.add_parser("agent-session", help="获取/创建 Wiki Agent 会话")
    session.add_argument("--kb-id", default="default")
    session.add_argument("--force-new", action="store_true")
    _set_wiki_handler(session, _wiki_agent_session, "agent-session")

    cancel = cmds.add_parser("confirmation-cancel", help="取消 Wiki 确认")
    cancel.add_argument("--confirmation-id", required=True)
    cancel.add_argument("--session-id", required=True)
    _set_wiki_handler(cancel, _wiki_confirmation_cancel, "confirmation-cancel")


def _wiki_init(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    service.initialize(ctx.owner, kb_id)
    return CliResult(data={"ok": True, "kb_id": kb_id}, text=f"知识库 {kb_id} 已初始化")


def _wiki_kbs_list(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    items = [kb.to_dict() for kb in service.list_knowledge_bases(ctx.owner)]
    text = "\n".join(f"{item.get('id')}  {item.get('name', '')}" for item in items)
    return CliResult(data={"ok": True, "kbs": items}, text=text or "(无知识库)")


def _wiki_kbs_create(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    try:
        kb = service.create_knowledge_base(
            kb_id,
            name=args.name or kb_id,
            owner_account_id=ctx.owner,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    data = kb.to_dict()
    return CliResult(data=data, text=f"已创建知识库 {data.get('id')}")


def _wiki_kbs_delete(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    session_ids = [
        str(s.get("session_id") or "")
        for s in _wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id)
        if str(s.get("session_id") or "")
    ]
    try:
        ok = service.delete_knowledge_base(kb_id, ctx.owner)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    if not ok:
        raise CliError("知识库不存在", exit_code=404)
    for session_id in session_ids:
        ctx.app.session_store.clear(session_id, owner_account_id=ctx.owner)
    return CliResult(
        data={"ok": True, "deleted_session_ids": session_ids},
        text=f"已删除知识库 {kb_id}",
    )


def _wiki_pages_list(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    pages = service.list_pages(
        owner_account_id=ctx.owner,
        kb_id=kb_id,
        limit=args.limit,
        offset=args.offset,
        brief=args.brief,
    )
    items = [page.to_dict(brief=args.brief) for page in pages]
    text = "\n".join(f"{item.get('id')}  {item.get('title', '')}" for item in items)
    return CliResult(data=items, text=text or "(无页面)")


def _wiki_pages_show(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    page = service.read_document(args.page_id, ctx.owner, kb_id)
    if page is None:
        raise CliError("页面不存在", exit_code=404)
    return CliResult(data={"page": page.to_dict()}, text=page.title)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def _wiki_pages_create(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    if args.page_type not in {"entity", "topic", "source", "comparison", "synthesis"}:
        raise CliError(f"不支持的 Wiki 页面类型: {args.page_type}")
    relation_payload = parse_json(args.relations, name="relations") if args.relations else []
    relations = [
        WikiRelation.from_dict(item)
        for item in relation_payload
        if isinstance(item, dict)
    ]
    page = WikiPage(
        id="",
        page_type=args.page_type,
        title=args.title,
        content=args.content,
        file_path="",
        sources=_split_csv(args.sources),
        tags=_split_csv(args.tags),
        relations=relations,
    )
    saved = service.save_page(page, ctx.owner, kb_id)
    data = saved.to_dict()
    return CliResult(data=data, text=f"已创建页面 {data.get('id')}")


def _wiki_pages_update(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    page = service.read_document(args.page_id, ctx.owner, kb_id)
    if page is None:
        raise CliError("页面不存在", exit_code=404)
    if args.title is not None:
        page.title = args.title
    if args.content is not None:
        page.content = args.content
    if args.tags is not None:
        page.tags = _split_csv(args.tags)
    if args.sources is not None:
        page.sources = _split_csv(args.sources)
    if args.relations:
        page.relations = [
            WikiRelation.from_dict(item)
            for item in parse_json(args.relations, name="relations")
            if isinstance(item, dict)
        ]
    page.related = []
    updated = service.update_page(page, ctx.owner, kb_id)
    result_page = updated or page
    data = result_page.to_dict()
    return CliResult(data=data, text=f"已更新页面 {data.get('id')}")


def _wiki_pages_delete(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    ok = service.delete_page(args.page_id, ctx.owner, kb_id)
    if not ok:
        raise CliError("页面不存在", exit_code=404)
    return CliResult(data={"ok": True}, text="页面已删除")


def _wiki_search(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    pages = service.search_pages(
        args.q,
        top_k=args.top_k,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    items = [page.to_dict() for page in pages]
    text = "\n".join(f"{item.get('id')}  {item.get('title', '')}" for item in items)
    return CliResult(data=items, text=text or "(无结果)")


def _wiki_sources_list(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    raws = service.list_sources(owner_account_id=ctx.owner, kb_id=kb_id)
    if args.status != "all":
        raws = [r for r in raws if (r.parse_status or "pending") == args.status]
    total = len(raws)
    raws.sort(key=lambda r: r.created_at, reverse=True)
    page = raws[args.offset : args.offset + args.limit]
    items = [r.to_dict() for r in page]
    text = "\n".join(
        f"{item.get('id')}  {item.get('title', '')}  {item.get('parse_status', '')}"
        for item in items
    )
    return CliResult(
        data={"sources": items, "total": total, "kb_id": kb_id},
        text=text or "(无数据源)",
    )


def _wiki_sources_delete(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    related_pages = [
        {"id": page.id, "title": page.title}
        for page in service.list_pages(owner_account_id=ctx.owner, kb_id=kb_id, limit=10000)
        if args.source_id in page.sources
    ]
    ok = service.delete_source(args.source_id, owner_account_id=ctx.owner, kb_id=kb_id)
    if not ok:
        raise CliError("source 不存在", exit_code=404)
    return CliResult(
        data={"ok": True, "deleted_source_id": args.source_id, "related_pages": related_pages},
        text="数据源已删除",
    )


async def _wiki_upload(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise CliError(f"文件不存在: {path}")
    filename = path.name
    content = path.read_bytes()
    if not content:
        raise CliError("上传文件为空")

    kb_id = _kb_id(args.kb_id)
    result = await service.upload_file(
        filename,
        content,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    if result.error_code == "MULTIMODAL_DISABLED":
        raise CliError(result.error or "Wiki 多模态功能未启用")
    if result.error_code == "MISSING_DEPENDENCY":
        raise CliError(result.error)
    if result.error_code == "MEDIA_UNDERSTANDING" or (
        result.source_type in {"image", "video"} and result.error
    ):
        return CliResult(
            data={
                "ok": False,
                "error": result.error,
                "source_id": result.source_id,
                "needs_confirmation": result.needs_confirmation,
            },
            text=result.error,
        )
    if result.needs_agent_review:
        return CliResult(
            data={
                "ok": True,
                "source_id": result.source_id,
                "title": result.title,
                "needs_agent_review": True,
                "error": result.error,
            },
            text=f"文件已保存但解析失败: {result.error}",
        )
    if result.source_type in {"image", "video"} and not result.ingested:
        return CliResult(
            data={
                "ok": True,
                "source_id": result.source_id,
                "title": result.title,
                "source_type": result.source_type,
                "ingested": False,
            },
            text=f"已保存媒体数据源 {result.source_id}",
        )
    if result.ingested:
        return CliResult(
            data={
                "ok": True,
                "source_id": result.source_id,
                "title": result.title,
                "source_type": result.source_type,
                "ingested": True,
                "pages": [page.to_dict() for page in result.pages],
                "issues": list(result.issues),
            },
            text=f"已入库媒体数据源 {result.source_id}",
        )
    return CliResult(
        data={"ok": True, "source_id": result.source_id, "title": result.title},
        text=f"已保存数据源 {result.source_id}",
    )


async def _wiki_ingest(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    result = await service.ingest(
        args.source_id,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    data = result.to_dict()
    return CliResult(
        data={"ok": True, **data},
        text=f"入库完成，生成 {len(result.pages)} 个页面",
    )


async def _wiki_compile(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    result = await service.compile_all(owner_account_id=ctx.owner, kb_id=kb_id)
    return CliResult(
        data={"ok": True, "ingested": result.ingested, "errors": result.errors},
        text=f"编译完成 ingested={result.ingested} errors={len(result.errors)}",
    )


async def _wiki_graph(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    graph = await service.graph(owner_account_id=ctx.owner, kb_id=kb_id)
    return CliResult(data=graph.to_dict())


def _wiki_query(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    result = service.query(args.q, owner_account_id=ctx.owner, kb_id=kb_id)
    return CliResult(
        data={"ok": True, **result},
        text=str(result.get("answer") or ""),
    )


async def _wiki_lint(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    kb_id = _kb_id(args.kb_id)
    issues = await service.lint(
        owner_account_id=ctx.owner,
        kb_id=kb_id,
        deep=args.deep,
    )
    return CliResult(
        data={"ok": True, "issues": issues},
        text=f"{len(issues)} 个问题",
    )


def _wiki_agent_sessions_rows(app: Any, owner: str, kb_id: str) -> list[dict[str, Any]]:
    from crew.wiki._utils import is_wiki_agent_session

    rows = []
    for session in app.session_store.list_sessions(workspace_id="wiki", owner_account_id=owner):
        session_id = str(session.get("session_id") or "")
        if not is_wiki_agent_session(session_id):
            continue
        config = app.session_store.get_agent_config(session_id, owner_account_id=owner) or {}
        if not config.get("wiki_agent_session"):
            continue
        if str(config.get("wiki_kb_id") or "default") != kb_id:
            continue
        rows.append(session)
    return rows


def _wiki_agent_sessions(args: Any, ctx: CliContext) -> CliResult:
    kb_id = _kb_id(args.kb_id)
    sessions = _wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id)
    return CliResult(
        data={"ok": True, "kb_id": kb_id, "sessions": sessions},
        text=f"{len(sessions)} 个会话",
    )


def _wiki_agent_session(args: Any, ctx: CliContext) -> CliResult:
    kb_id = _kb_id(args.kb_id)
    if not args.force_new:
        for row in _wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id):
            sid = str(row.get("session_id") or "")
            config = ctx.app.session_store.get_agent_config(sid, owner_account_id=ctx.owner) or {}
            if config.get("preset_agent_type") != "Wiki":
                config["preset_agent_type"] = "Wiki"
                config["wiki_kb_id"] = kb_id
                ctx.app.session_store.set_agent_config(sid, config, owner_account_id=ctx.owner)
            return CliResult(data={"ok": True, "session_id": sid, "kb_id": kb_id})
    session_id = f"wiki-{uuid.uuid4().hex[:12]}"
    ctx.app.session_store.ensure_session(
        session_id,
        workspace_id="wiki",
        title="新对话",
        owner_account_id=ctx.owner,
    )
    ctx.app.session_store.set_agent_config(
        session_id,
        {
            "wiki_agent_session": True,
            "preset_agent_type": "Wiki",
            "wiki_kb_id": kb_id,
        },
        owner_account_id=ctx.owner,
    )
    return CliResult(
        data={"ok": True, "session_id": session_id, "kb_id": kb_id},
        text=f"已创建 {session_id}",
    )


def _wiki_confirmation_cancel(args: Any, ctx: CliContext) -> CliResult:
    service = _service(ctx)
    if not args.session_id:
        raise CliError("缺少 Wiki 会话")
    cancelled = service.cancel_confirmation(
        args.session_id,
        args.confirmation_id,
        owner_account_id=ctx.owner,
    )
    if not cancelled:
        raise CliError("确认已失效或不属于当前会话", exit_code=404)
    return CliResult(data={"ok": True, "cancelled": True})


def _register_skill(subparsers) -> None:
    parser = subparsers.add_parser("skill", help="技能管理")
    cmds = parser.add_subparsers(dest="skill_cmd")

    lst = cmds.add_parser("list", help="列出技能")
    lst.add_argument(
        "--store", action="store_true", help="同时显示可安装/本地/自进化配置"
    )
    lst.set_defaults(handler=_skill_list)

    install = cmds.add_parser("install", help="安装可选/本地技能")
    install.add_argument("--slug", required=True)
    install.set_defaults(handler=_skill_install)

    uninstall = cmds.add_parser("uninstall", help="卸载用户技能")
    uninstall.add_argument("--slug", required=True)
    uninstall.set_defaults(handler=_skill_uninstall)

    evolution = cmds.add_parser("evolution", help="查看/修改 Skill 自进化配置")
    evolution.add_argument("--auto-trigger", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    evolution.add_argument(
        "--auto-full-cycle",
        type=lambda v: v.lower() in ("1", "true", "yes", "on"),
    )
    evolution.add_argument("--visible", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    evolution.set_defaults(handler=_skill_evolution)


def _skill_list(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.skills import list_local_skills, list_optional_skills, list_skills

    items = list_skills()
    if args.store:
        data = {
            "installed": items,
            "optional": list_optional_skills(),
            "local": list_local_skills(),
            "evolution": {
                "auto_trigger": ctx.app.config.evolution_auto_trigger,
                "auto_full_cycle": ctx.app.config.evolution_auto_full_cycle,
                "visible": ctx.app.config.evolution_visible,
            },
        }
    else:
        data = items
    text = "\n".join(
        f"{item['slug']}  {item['display_name']}  {item.get('source', '')}"
        for item in items
    )
    return CliResult(data=data, text=text or "(无技能)")


def _skill_install(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.skills import install_skill

    ok = install_skill(args.slug, operator_account_id=ctx.owner, source="cli")
    if not ok:
        raise CliError("技能不存在或已安装")
    return CliResult(data={"ok": True, "slug": args.slug}, text=f"已安装技能 {args.slug}")


def _skill_uninstall(args: Any, ctx: CliContext) -> CliResult:
    from crew.agent.skills import uninstall_skill

    ok = uninstall_skill(args.slug, operator_account_id=ctx.owner, source="cli")
    if not ok:
        raise CliError("技能不存在或为内置（不可卸载）")
    return CliResult(data={"ok": True, "slug": args.slug}, text=f"已卸载技能 {args.slug}")


def _skill_evolution(args: Any, ctx: CliContext) -> CliResult:
    cfg = ctx.app.config
    changes: dict[str, bool] = {}
    if args.auto_trigger is not None:
        changes["auto_trigger"] = bool(args.auto_trigger)
    if args.auto_full_cycle is not None:
        changes["auto_full_cycle"] = bool(args.auto_full_cycle)
    if args.visible is not None:
        changes["visible"] = bool(args.visible)
    if changes:
        # 统一配置事务（Config.set_evolution_config）：候选值 → 持久化 → 发布。
        # 持久化失败时内存与磁盘一致保留旧值，先报错返回，不做其他操作。
        try:
            cfg.set_evolution_config(**changes)
        except Exception as exc:
            raise CliError(f"持久化失败: {exc}") from exc
    data = {
        "auto_trigger": cfg.evolution_auto_trigger,
        "auto_full_cycle": cfg.evolution_auto_full_cycle,
        "visible": cfg.evolution_visible,
    }
    return CliResult(data=data, text=str(data))


__all__ = ["register"]
