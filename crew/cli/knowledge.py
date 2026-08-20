"""知识管理命令：Wiki（知识库/页面/来源/入库）与 Skill（安装/卸载/自进化）。"""

from __future__ import annotations

import asyncio
import getpass
import time
import uuid
from pathlib import Path
from typing import Any

from crew.cli.app import CliContext, CliError, CliResult, parse_json
from crew.state.logging import get_logger

log = get_logger("cli.knowledge")


def register(subparsers, handlers: dict[str, Any]) -> None:
    _register_wiki(subparsers)
    _register_skill(subparsers)


def _wiki_store(app: Any):
    store = getattr(app, "_wiki_store", None)
    if store is None:
        raise CliError("Wiki 未启用")
    return store


def _wiki_compiler(app: Any):
    compiler = getattr(app, "_wiki_compiler", None)
    if compiler is None:
        raise CliError("Wiki 未启用（缺少 compiler）")
    return compiler


def _kb_id(value: str) -> str:
    from crew.wiki.store import normalize_kb_id

    try:
        return normalize_kb_id(value)
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _register_wiki(subparsers) -> None:
    parser = subparsers.add_parser("wiki", help="Wiki 知识库管理")
    cmds = parser.add_subparsers(dest="wiki_cmd")

    init = cmds.add_parser("init", help="初始化知识库")
    init.add_argument("--kb-id", default="default")
    init.set_defaults(handler=_wiki_init)

    kbs = cmds.add_parser("kbs", help="知识库列表/创建/删除")
    kbs_cmds = kbs.add_subparsers(dest="wiki_kbs_cmd")
    kbs_cmds.add_parser("list").set_defaults(handler=_wiki_kbs_list)
    create = kbs_cmds.add_parser("create")
    create.add_argument("--kb-id", required=True)
    create.add_argument("--name", default="")
    create.set_defaults(handler=_wiki_kbs_create)
    delete = kbs_cmds.add_parser("delete")
    delete.add_argument("--kb-id", required=True)
    delete.set_defaults(handler=_wiki_kbs_delete)

    pages = cmds.add_parser("pages", help="Wiki 页面管理")
    pages_cmds = pages.add_subparsers(dest="wiki_pages_cmd")
    lst = pages_cmds.add_parser("list")
    lst.add_argument("--kb-id", default="default")
    lst.add_argument("--limit", type=int, default=100)
    lst.add_argument("--offset", type=int, default=0)
    lst.add_argument("--brief", action="store_true")
    lst.set_defaults(handler=_wiki_pages_list)
    show = pages_cmds.add_parser("show")
    show.add_argument("--id", dest="page_id", required=True)
    show.add_argument("--kb-id", default="default")
    show.set_defaults(handler=_wiki_pages_show)
    create_page = pages_cmds.add_parser("create")
    create_page.add_argument("--kb-id", default="default")
    create_page.add_argument("--title", required=True)
    create_page.add_argument("--page-type", default="topic")
    create_page.add_argument("--content", default="")
    create_page.add_argument("--sources", default="", help="逗号分隔的 source_id")
    create_page.add_argument("--tags", default="", help="逗号分隔的标签")
    create_page.add_argument("--relations", default="", help="relations JSON 数组")
    create_page.set_defaults(handler=_wiki_pages_create)
    update_page = pages_cmds.add_parser("update")
    update_page.add_argument("--id", dest="page_id", required=True)
    update_page.add_argument("--kb-id", default="default")
    update_page.add_argument("--title")
    update_page.add_argument("--content")
    update_page.add_argument("--sources", help="逗号分隔的 source_id")
    update_page.add_argument("--tags", help="逗号分隔的标签")
    update_page.add_argument("--relations", help="relations JSON 数组")
    update_page.set_defaults(handler=_wiki_pages_update)
    delete_page = pages_cmds.add_parser("delete")
    delete_page.add_argument("--id", dest="page_id", required=True)
    delete_page.add_argument("--kb-id", default="default")
    delete_page.set_defaults(handler=_wiki_pages_delete)

    search = cmds.add_parser("search", help="搜索 Wiki 页面")
    search.add_argument("--q", required=True)
    search.add_argument("--kb-id", default="default")
    search.add_argument("--top-k", type=int, default=5)
    search.set_defaults(handler=_wiki_search)

    sources = cmds.add_parser("sources", help="Wiki 数据源管理")
    sources_cmds = sources.add_subparsers(dest="wiki_sources_cmd")
    source_list = sources_cmds.add_parser("list")
    source_list.add_argument("--kb-id", default="default")
    source_list.add_argument("--status", default="all")
    source_list.add_argument("--limit", type=int, default=200)
    source_list.add_argument("--offset", type=int, default=0)
    source_list.set_defaults(handler=_wiki_sources_list)
    source_delete = sources_cmds.add_parser("delete")
    source_delete.add_argument("--id", dest="source_id", required=True)
    source_delete.add_argument("--kb-id", default="default")
    source_delete.set_defaults(handler=_wiki_sources_delete)

    upload = cmds.add_parser("upload", help="上传文件到 Wiki")
    upload.add_argument("--file", required=True)
    upload.add_argument("--kb-id", default="default")
    upload.set_defaults(handler=_wiki_upload)

    ingest = cmds.add_parser("ingest", help="编译入库一个数据源")
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--kb-id", default="default")
    ingest.add_argument("--session-id", default="")
    ingest.set_defaults(handler=_wiki_ingest)

    compile_all = cmds.add_parser("compile", help="全库重新编译")
    compile_all.add_argument("--kb-id", default="default")
    compile_all.set_defaults(handler=_wiki_compile)

    graph = cmds.add_parser("graph", help="查看 Wiki 图谱")
    graph.add_argument("--kb-id", default="default")
    graph.set_defaults(handler=_wiki_graph)

    query = cmds.add_parser("query", help="Wiki 检索问答")
    query.add_argument("--q", required=True)
    query.add_argument("--kb-id", default="default")
    query.set_defaults(handler=_wiki_query)

    lint = cmds.add_parser("lint", help="检查知识库页面质量")
    lint.add_argument("--kb-id", default="default")
    lint.add_argument("--deep", action="store_true")
    lint.set_defaults(handler=_wiki_lint)

    sessions = cmds.add_parser("agent-sessions", help="列出 Wiki Agent 会话")
    sessions.add_argument("--kb-id", default="default")
    sessions.set_defaults(handler=_wiki_agent_sessions)

    session = cmds.add_parser("agent-session", help="获取/创建 Wiki Agent 会话")
    session.add_argument("--kb-id", default="default")
    session.add_argument("--force-new", action="store_true")
    session.set_defaults(handler=_wiki_agent_session)

    cancel = cmds.add_parser("confirmation-cancel", help="取消 Wiki 确认")
    cancel.add_argument("--confirmation-id", required=True)
    cancel.add_argument("--session-id", required=True)
    cancel.set_defaults(handler=_wiki_confirmation_cancel)


def _wiki_init(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    store.init_kb(ctx.owner, kb_id)
    try:
        if store.layout_migration_preview(ctx.owner, kb_id).get("required"):
            store.migrate_layout(ctx.owner, kb_id)
    except Exception as exc:  # noqa: BLE001 - 布局迁移失败不阻断 init
        log.warning("Wiki 布局迁移失败 kb=%s: %s", kb_id, exc)
    return CliResult(data={"ok": True, "kb_id": kb_id}, text=f"知识库 {kb_id} 已初始化")


def _wiki_kbs_list(args: Any, ctx: CliContext) -> CliResult:
    from crew.wiki.seed import ensure_tutorial_kb

    store = _wiki_store(ctx.app)
    ensure_tutorial_kb(store, ctx.owner)
    items = [kb.to_dict() for kb in store.list_kbs(ctx.owner)]
    text = "\n".join(f"{item.get('id')}  {item.get('name', '')}" for item in items)
    return CliResult(data={"ok": True, "kbs": items}, text=text or "(无知识库)")


def _wiki_kbs_create(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    try:
        kb = store.create_kb(kb_id, name=args.name or kb_id, owner_account_id=ctx.owner)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    data = kb.to_dict()
    return CliResult(data=data, text=f"已创建知识库 {data.get('id')}")


def _wiki_kbs_delete(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    session_ids = [
        str(s.get("session_id") or "")
        for s in _wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id)
        if str(s.get("session_id") or "")
    ]
    try:
        ok = store.delete_kb(kb_id, ctx.owner)
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
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    pages = store.list_all(
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
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    page = store.get(args.page_id, ctx.owner, kb_id)
    if page is None:
        raise CliError("页面不存在", exit_code=404)
    return CliResult(data={"page": page.to_dict()}, text=page.title)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def _wiki_pages_create(args: Any, ctx: CliContext) -> CliResult:
    from crew.wiki.schemas import WikiPage, WikiRelation

    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    if args.page_type not in {"entity", "topic", "source", "comparison", "synthesis"}:
        raise CliError(f"不支持的 Wiki 页面类型: {args.page_type}")
    relations = [
        WikiRelation.from_dict(item)
        for item in parse_json(args.relations, name="relations")
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
    saved = store.save_page(page, ctx.owner, kb_id)
    _finish_page_write(ctx.app, ctx.owner, kb_id, f"创建页面 {saved.id} ({saved.title})")
    data = saved.to_dict()
    return CliResult(data=data, text=f"已创建页面 {data.get('id')}")


def _wiki_pages_update(args: Any, ctx: CliContext) -> CliResult:
    from crew.wiki.schemas import WikiRelation

    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    page = store.get(args.page_id, ctx.owner, kb_id)
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
    updated = store.update(page, ctx.owner, kb_id)
    result_page = updated or page
    _finish_page_write(ctx.app, ctx.owner, kb_id, f"更新页面 {result_page.id} ({result_page.title})")
    data = result_page.to_dict()
    return CliResult(data=data, text=f"已更新页面 {data.get('id')}")


def _wiki_pages_delete(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    ok = store.delete(args.page_id, ctx.owner, kb_id)
    if not ok:
        raise CliError("页面不存在", exit_code=404)
    return CliResult(data={"ok": True}, text="页面已删除")


def _wiki_search(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    pages = store.search(
        args.q,
        top_k=args.top_k,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    items = [page.to_dict() for page in pages]
    text = "\n".join(f"{item.get('id')}  {item.get('title', '')}" for item in items)
    return CliResult(data=items, text=text or "(无结果)")


def _wiki_sources_list(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    raws = store.list_raws(owner_account_id=ctx.owner, kb_id=kb_id)
    if args.status != "all":
        raws = [r for r in raws if (r.parse_status or "pending") == args.status]
    total = len(raws)
    raws.sort(key=lambda r: r.created_at, reverse=True)
    page = raws[args.offset : args.offset + args.limit]
    items = [r.to_dict() for r in page]
    text = "\n".join(f"{item.get('id')}  {item.get('title', '')}  {item.get('parse_status', '')}" for item in items)
    return CliResult(data={"sources": items, "total": total, "kb_id": kb_id}, text=text or "(无数据源)")


def _wiki_sources_delete(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    related_pages = [
        {"id": page.id, "title": page.title}
        for page in store.list_all(owner_account_id=ctx.owner, kb_id=kb_id, limit=10000)
        if args.source_id in page.sources
    ]
    ok = store.delete_raw(args.source_id, owner_account_id=ctx.owner, kb_id=kb_id)
    if not ok:
        raise CliError("source 不存在", exit_code=404)
    return CliResult(
        data={"ok": True, "deleted_source_id": args.source_id, "related_pages": related_pages},
        text="数据源已删除",
    )


def _wiki_upload(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.context import SecurityContext
    from crew.security.launch import compile_process_launch, use_process_launch
    from crew.wiki.multimodal import MediaUnderstandingError, is_image_mime, is_video_mime
    from crew.wiki.parser import MissingDependencyError, guess_mime_type, parse_document_from_bytes
    from crew.wiki.schemas import RawSource
    from crew.wiki.sources import classify_file
    from crew.wiki.store._ids import filename_from_title

    app = ctx.app
    store = _wiki_store(app)
    compiler = _wiki_compiler(app)
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise CliError(f"文件不存在: {path}")
    filename = path.name
    content = path.read_bytes()
    if not content:
        raise CliError("上传文件为空")

    kb_id = _kb_id(args.kb_id)
    source_id = f"upload_{uuid.uuid4().hex[:12]}"
    file_type = guess_mime_type(filename)
    source_kind = classify_file(filename, file_type)
    source_dir = store._source_dir(source_kind, ctx.owner, kb_id)
    wiki_config = getattr(app.config, "wiki", None)

    if is_image_mime(file_type) or is_video_mime(file_type):
        if wiki_config is None or not wiki_config.multimodal.enabled:
            raise CliError("Wiki 多模态功能未启用")
        source_type = "image" if is_image_mime(file_type) else "video"
        ext = Path(filename).suffix.lower() or ".bin"
        original_path = source_dir / f"{source_id}-{filename_from_title(Path(filename).stem)}{ext}"
        original_path.write_bytes(content)
        raw = RawSource(
            id=source_id,
            title=filename,
            source_type=source_type,
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
        store.save_raw(raw, ctx.owner, kb_id)
        auto_process = False
        if source_type == "image" and wiki_config.multimodal.auto_image:
            auto_process = True
        if (
            source_type == "video"
            and wiki_config.multimodal.auto_video
            and wiki_config.multimodal.video_upload_confirmed
        ):
            auto_process = True
        if not auto_process:
            return CliResult(
                data={
                    "ok": True,
                    "source_id": source_id,
                    "title": filename,
                    "source_type": source_type,
                    "ingested": False,
                },
                text=f"已保存媒体数据源 {source_id}",
            )
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
            return CliResult(
                data={
                    "ok": False,
                    "error": str(exc),
                    "source_id": source_id,
                    "needs_confirmation": exc.needs_confirmation,
                },
                text=str(exc),
            )
        raw.parsed_path = store.save_parsed_markdown(
            source_id,
            description,
            owner_account_id=ctx.owner,
            kb_id=kb_id,
        )
        store.save_raw(raw, ctx.owner, kb_id)
        result = asyncio.run(compiler.ingest(source_id, owner_account_id=ctx.owner, kb_id=kb_id))
        return CliResult(
            data={
                "ok": True,
                "source_id": source_id,
                "title": filename,
                "source_type": source_type,
                "ingested": True,
                "pages": [p.to_dict() for p in result.pages],
                "issues": result.issues,
            },
            text=f"已入库媒体数据源 {source_id}",
        )

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
    store.save_raw(raw, ctx.owner, kb_id)
    try:
        security_context = SecurityContext(
            os_user=getpass.getuser(),
            owner_account_id=ctx.owner,
            workspace_id="wiki",
            workspace_root=source_dir.resolve(),
            session_id="wiki-upload",
            request_id=uuid.uuid4().hex,
            task_id="",
            cwd=source_dir.resolve(),
        )
        launch = compile_process_launch(
            security_context,
            app.security_service.mode_for(security_context),
            db_path=app.security_service.db_path,
            audit=app.security_service.audit,
        )
        with use_process_launch(launch):
            text = asyncio.run(asyncio.to_thread(parse_document_from_bytes, content, filename))
    except MissingDependencyError as exc:
        raw.parse_status = "failed"
        raw.parse_error = f"缺少依赖: {exc}"
        store.save_raw(raw, ctx.owner, kb_id)
        raise CliError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raw.parse_status = "failed"
        raw.parse_error = f"解析失败: {exc}"
        store.save_raw(raw, ctx.owner, kb_id)
        return CliResult(
            data={
                "ok": True,
                "source_id": source_id,
                "title": filename,
                "needs_agent_review": True,
                "error": str(exc),
            },
            text=f"文件已保存但解析失败: {exc}",
        )
    raw.parsed_path = store.save_parsed_markdown(
        source_id,
        text,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    raw.parse_status = "parsed"
    store.save_raw(raw, ctx.owner, kb_id)
    return CliResult(
        data={"ok": True, "source_id": source_id, "title": filename},
        text=f"已保存数据源 {source_id}",
    )


async def _wiki_ingest(args: Any, ctx: CliContext) -> CliResult:
    compiler = _wiki_compiler(ctx.app)
    kb_id = _kb_id(args.kb_id)
    result = await compiler.ingest(
        args.source_id,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    data = result.to_dict()
    return CliResult(data={"ok": True, **data}, text=f"入库完成，生成 {len(result.pages)} 个页面")


async def _wiki_compile(args: Any, ctx: CliContext) -> CliResult:
    compiler = _wiki_compiler(ctx.app)
    kb_id = _kb_id(args.kb_id)
    result = await compiler.compile_all(owner_account_id=ctx.owner, kb_id=kb_id)
    return CliResult(
        data={"ok": True, "ingested": result.ingested, "errors": result.errors},
        text=f"编译完成 ingested={result.ingested} errors={len(result.errors)}",
    )


async def _wiki_graph(args: Any, ctx: CliContext) -> CliResult:
    store = _wiki_store(ctx.app)
    kb_id = _kb_id(args.kb_id)
    graph = await asyncio.to_thread(
        store.get_graph,
        owner_account_id=ctx.owner,
        kb_id=kb_id,
    )
    return CliResult(data=graph.to_dict())


def _wiki_query(args: Any, ctx: CliContext) -> CliResult:
    querier = getattr(ctx.app, "_wiki_querier", None)
    if querier is None:
        raise CliError("Wiki 未启用（缺少 querier）")
    kb_id = _kb_id(args.kb_id)
    result = querier.query(args.q, owner_account_id=ctx.owner, kb_id=kb_id)
    return CliResult(data={"ok": True, **result}, text=result.get("answer", ""))


async def _wiki_lint(args: Any, ctx: CliContext) -> CliResult:
    compiler = _wiki_compiler(ctx.app)
    kb_id = _kb_id(args.kb_id)
    issues = await compiler.lint(
        owner_account_id=ctx.owner,
        kb_id=kb_id,
        deep=args.deep,
    )
    return CliResult(data={"ok": True, "issues": issues}, text=f"{len(issues)} 个问题")


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
    return CliResult(
        data={"ok": True, "kb_id": kb_id, "sessions": _wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id)},
        text=f"{len(_wiki_agent_sessions_rows(ctx.app, ctx.owner, kb_id))} 个会话",
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
    return CliResult(data={"ok": True, "session_id": session_id, "kb_id": kb_id}, text=f"已创建 {session_id}")


def _wiki_confirmation_cancel(args: Any, ctx: CliContext) -> CliResult:
    manager = getattr(ctx.app, "wiki_manager", None)
    if manager is None or not args.session_id:
        raise CliError("缺少 Wiki 会话")
    cancelled = manager.cancel_confirmation(
        args.session_id,
        args.confirmation_id,
        owner_account_id=ctx.owner,
    )
    if not cancelled:
        raise CliError("确认已失效或不属于当前会话", exit_code=404)
    return CliResult(data={"ok": True, "cancelled": True})


def _finish_page_write(app: Any, owner: str, kb_id: str, message: str) -> None:
    compiler = getattr(app, "_wiki_compiler", None)
    if compiler is not None:
        compiler.finalize_write(message, owner_account_id=owner, kb_id=kb_id)


def _register_skill(subparsers) -> None:
    parser = subparsers.add_parser("skill", help="技能管理")
    cmds = parser.add_subparsers(dest="skill_cmd")

    lst = cmds.add_parser("list", help="列出技能")
    lst.add_argument("--store", action="store_true", help="同时显示可安装/本地/自进化配置")
    lst.set_defaults(handler=_skill_list)

    install = cmds.add_parser("install", help="安装可选/本地技能")
    install.add_argument("--slug", required=True)
    install.set_defaults(handler=_skill_install)

    uninstall = cmds.add_parser("uninstall", help="卸载用户技能")
    uninstall.add_argument("--slug", required=True)
    uninstall.set_defaults(handler=_skill_uninstall)

    evolution = cmds.add_parser("evolution", help="查看/修改 Skill 自进化配置")
    evolution.add_argument("--auto-trigger", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    evolution.add_argument("--auto-full-cycle", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
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
    text = "\n".join(f"{item['slug']}  {item['display_name']}  {item.get('source', '')}" for item in items)
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
    changed = False
    if args.auto_trigger is not None:
        cfg.evolution_auto_trigger = args.auto_trigger
        changed = True
    if args.auto_full_cycle is not None:
        cfg.evolution_auto_full_cycle = args.auto_full_cycle
        changed = True
    if args.visible is not None:
        cfg.evolution_visible = args.visible
        changed = True
    if changed:
        try:
            cfg.persist_evolution_config()
        except RuntimeError as exc:
            raise CliError(str(exc)) from exc
    data = {
        "auto_trigger": cfg.evolution_auto_trigger,
        "auto_full_cycle": cfg.evolution_auto_full_cycle,
        "visible": cfg.evolution_visible,
    }
    return CliResult(data=data, text=str(data))


__all__ = ["register"]
