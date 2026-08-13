"""对话入口：交互式 REPL（chat）与一次性非交互运行（run）。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from crew.cli.app import CliContext, CliError, CliResult
from crew.core.envelope import Envelope, ResponseChunk

console = Console()

BANNER = """[bold cyan]Crew[/bold cyan] 多智能体调用平台 · CLI
输入消息开始对话；/help 查看命令，/quit 退出。
"""

HELP = """[bold]命令[/bold]
  /help    显示帮助
  /new     新建会话（清空上下文）
  /team    切换到 Team 多智能体模式
  /agent   切换到单 Agent 模式
  /plan    进入 Plan 模式（只读探索→写计划→审批后执行）
  /todo    查看当前任务清单
  /quit    退出
"""


def register(subparsers, handlers: dict[str, Any]) -> None:
    chat = subparsers.add_parser("chat", help="启动交互式对话")
    chat.set_defaults(handler=_chat)
    run = subparsers.add_parser("run", help="一次性非交互对话")
    run.add_argument("prompt", help="要发送的消息")
    run.add_argument("--mode", choices=("agent", "team", "dynamic_kanban"), default="agent")
    run.add_argument("--session-id", default="", help="复用已有会话，缺省新建")
    run.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default="",
        help="text=人类可读；json=单次结构化结果；stream-json=逐帧 NDJSON",
    )
    run.add_argument(
        "--permission-mode",
        choices=("request_approval", "auto_review", "full_access"),
        default="",
        help="覆盖会话安全审批模式",
    )
    run.add_argument("--yes", action="store_true", help="自动批准 Plan 模式的计划")
    run.set_defaults(handler=_run)


async def _chat(args: Any, ctx: CliContext) -> CliResult:
    async with ctx.running_app() as app:
        await _repl(app, ctx)
    return CliResult(data=None)


async def _repl(app: Any, ctx: CliContext) -> None:
    session = PromptSession()
    session_id = f"cli_{uuid.uuid4().hex[:8]}"
    mode = "agent"

    console.print(BANNER)
    if not app.config.has_llm_key:
        console.print(
            "[yellow]提示：未配置模型 API Key，当前为 FakeProvider 演示模式，"
            "不会调用真实模型。请在 .env 和 config/config.yaml 中完成模型配置。[/yellow]\n"
        )

    while True:
        try:
            with patch_stdout():
                text = await session.prompt_async(f"[{mode}] › ")
        except (EOFError, KeyboardInterrupt):
            break

        text = text.strip()
        if not text:
            continue

        if text.startswith("/"):
            cmd = text.lower()
            if cmd in ("/quit", "/exit"):
                break
            if cmd == "/help":
                console.print(HELP)
            elif cmd == "/new":
                if app.plan_manager is not None:
                    app.plan_manager.reset(session_id, owner_account_id=ctx.owner)
                session_id = f"cli_{uuid.uuid4().hex[:8]}"
                console.print("[green]已新建会话。[/green]")
            elif cmd == "/team":
                mode = "team"
                console.print("[magenta]已切换到 Team 模式。[/magenta]")
            elif cmd == "/agent":
                mode = "agent"
                console.print("[cyan]已切换到单 Agent 模式。[/cyan]")
            elif cmd == "/plan":
                if app.plan_manager is None:
                    console.print("[red]Plan 模式不可用（未装配 plan_manager）。[/red]")
                else:
                    app.plan_manager.enter(session_id, owner_account_id=ctx.owner)
                    console.print(
                        "[bold yellow]已进入 Plan 模式（只读）。[/bold yellow] "
                        "描述你的需求，我会探索代码、写出计划，再请你确认后执行。"
                    )
            elif cmd == "/todo":
                _print_todos(app, session_id, ctx.owner)
            else:
                result = await app.plugins.run_plugin_command(
                    text,
                    session_id=session_id,
                    owner_account_id=ctx.owner,
                    channel="cli",
                )
                if result is None:
                    console.print(f"[red]未知命令: {text}[/red]")
                elif result:
                    console.print(f"{result}\n")
            continue

        envelope = Envelope.of(
            text,
            session_id=session_id,
            channel="cli",
            mode=mode,
            user_id=ctx.owner,
            workspace_id=ctx.workspace_id,
        )
        await _render(app, envelope)
        if (
            app.plan_manager is not None
            and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=ctx.owner)
        ):
            await _handle_plan_approval(app, session, session_id, mode, ctx.owner)


def _print_todos(app: Any, session_id: str, owner_account_id: str) -> None:
    if app.plan_manager is None:
        console.print("[dim]无任务清单。[/dim]")
        return
    items = app.plan_manager.todo_store(session_id, owner_account_id=owner_account_id).read()
    if not items:
        console.print("[dim]任务清单为空。[/dim]")
        return
    markers = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}
    console.print("[bold]任务清单[/bold]")
    for it in items:
        console.print(f"  {markers.get(it['status'], '[?]')} {it['id']}. {it['content']} ([dim]{it['status']}[/dim])")


async def _handle_plan_approval(
    app: Any,
    session: PromptSession,
    session_id: str,
    mode: str,
    owner_account_id: str,
) -> None:
    from crew.agent.plan import read_plan

    plan_text = read_plan(session_id, owner_account_id=owner_account_id) or "(计划文件为空)"
    console.print("\n[bold cyan]──── 待审批的计划 ────[/bold cyan]")
    console.print(plan_text)
    console.print("[bold cyan]─────────────────────[/bold cyan]")
    try:
        with patch_stdout():
            ans = await session.prompt_async("批准计划并开始执行？[y/N] ")
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans.strip().lower() in ("y", "yes", "是", "ok"):
        app.plan_manager.approve(session_id, owner_account_id=owner_account_id)
        console.print("[green]计划已批准，开始执行……[/green]\n")
        kickoff = Envelope.of(
            "计划已批准，请按上述计划开始执行。",
            session_id=session_id,
            channel="cli",
            mode=mode,
            user_id=owner_account_id,
        )
        await _render(app, kickoff)
    else:
        app.plan_manager.reject(session_id, owner_account_id=owner_account_id)
        console.print("[yellow]已保留 Plan 模式，请继续告诉我如何完善计划。[/yellow]")


async def _render(app: Any, envelope: Envelope) -> None:
    async for chunk in app.handle(envelope):
        _render_chunk(chunk)


def _render_chunk(chunk: ResponseChunk) -> None:
    if chunk.kind == "tool":
        phase = chunk.body.get("phase")
        name = chunk.body.get("name")
        detail = chunk.body.get("detail", "")
        if phase == "generating":
            label = chunk.body.get("ui_label") or name
            console.print(f"  [dim]… {label}[/dim]")
        elif phase == "start":
            console.print(f"  [dim]→ 调用工具 {name}({detail})[/dim]")
        else:
            console.print(f"  [dim]← {name} 返回: {detail}[/dim]")
    elif chunk.kind == "status":
        console.print(f"  [blue]{chunk.body.get('message')}[/blue]")
    elif chunk.kind == "final":
        console.print(f"[bold green]{chunk.body.get('text')}[/bold green]\n")
    elif chunk.kind == "error":
        console.print(f"[bold red]错误: {chunk.body.get('message')}[/bold red]\n")


async def _run(args: Any, ctx: CliContext) -> CliResult:
    prompt = str(args.prompt or "").strip()
    if not prompt:
        raise CliError("prompt 不能为空")
    output_format = args.output_format or ("json" if ctx.json_output else "text")
    session_id = args.session_id.strip() or f"cli_{uuid.uuid4().hex[:8]}"

    async with ctx.running_app() as app:
        ensure = getattr(app.session_store, "ensure_session", None)
        if callable(ensure):
            ensure(
                session_id,
                workspace_id=ctx.workspace_id,
                title="CLI 运行",
                owner_account_id=ctx.owner,
            )
        if args.permission_mode:
            _set_permission_mode(app, ctx, session_id, args.permission_mode)

        envelope = Envelope.of(
            prompt,
            session_id=session_id,
            channel="cli",
            mode=args.mode,
            user_id=ctx.owner,
            workspace_id=ctx.workspace_id,
        )
        chunks = []
        async for chunk in app.handle(envelope):
            chunks.append(chunk)
            if output_format == "stream-json":
                print(
                    json.dumps(
                        {
                            "kind": chunk.kind,
                            "body": chunk.body,
                            "sequence": chunk.sequence,
                            "is_final": chunk.is_final,
                            "status": chunk.status,
                            "ts": chunk.ts,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            elif output_format == "text":
                _render_chunk(chunk)

        if (
            args.yes
            and app.plan_manager is not None
            and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=ctx.owner)
        ):
            app.plan_manager.approve(session_id, owner_account_id=ctx.owner)
            kickoff = Envelope.of(
                "计划已批准，请按上述计划开始执行。",
                session_id=session_id,
                channel="cli",
                mode=args.mode,
                user_id=ctx.owner,
                workspace_id=ctx.workspace_id,
            )
            async for chunk in app.handle(kickoff):
                chunks.append(chunk)
                if output_format == "stream-json":
                    print(json.dumps(_chunk_dict(chunk), ensure_ascii=False, default=str))
                elif output_format == "text":
                    _render_chunk(chunk)
        elif (
            app.plan_manager is not None
            and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=ctx.owner)
        ):
            raise CliError(
                "当前会话处于待审批计划状态；确认执行请加 --yes",
                exit_code=2,
            )

    if output_format == "json":
        return CliResult(data=_summarize(session_id, chunks))
    return CliResult(data=None)


def _chunk_dict(chunk: ResponseChunk) -> dict[str, Any]:
    return {
        "kind": chunk.kind,
        "body": chunk.body,
        "sequence": chunk.sequence,
        "is_final": chunk.is_final,
        "status": chunk.status,
        "ts": chunk.ts,
    }


def _summarize(session_id: str, chunks: list[ResponseChunk]) -> dict[str, Any]:
    final_text = ""
    error = ""
    tool_events = []
    last_status = "succeeded"
    for chunk in chunks:
        if chunk.kind == "final":
            final_text = str(chunk.body.get("text") or final_text)
            last_status = chunk.status or last_status
        elif chunk.kind == "error":
            error = str(chunk.body.get("message") or error)
            last_status = "failed"
        elif chunk.kind == "tool":
            tool_events.append(
                {
                    "name": chunk.body.get("name", ""),
                    "phase": chunk.body.get("phase", ""),
                    "detail": chunk.body.get("detail", ""),
                }
            )
    return {
        "session_id": session_id,
        "text": final_text,
        "status": last_status,
        "error": error,
        "tool_events": tool_events,
    }


def _set_permission_mode(app: Any, ctx: CliContext, session_id: str, mode: str) -> None:
    from crew.security.context import build_gateway_security_context
    from crew.security.models import ConversationPermissionMode

    try:
        value = ConversationPermissionMode(mode)
    except ValueError as exc:
        raise CliError(
            "permission-mode 必须是 request_approval | auto_review | full_access"
        ) from exc
    security_context = build_gateway_security_context(
        app.workspace_store,
        owner_account_id=ctx.owner,
        workspace_id=ctx.workspace_id,
        session_id=session_id,
    )
    app.security_service.set_mode(security_context, value)


__all__ = ["register"]
