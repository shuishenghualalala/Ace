"""对话入口：交互式 REPL（chat）与一次性非交互运行（run）。"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from crew.cli.app import CliContext, CliError, CliResult
from crew.cli.followup import conversation_followups
from crew.cli.security import CliApprovalRequired, consume_conversation_turn
from crew.core.envelope import Envelope, ResponseChunk

console = Console()
_MAX_RUN_PROMPT_CHARS = 1_000_000

BANNER = """[bold cyan]Crew[/bold cyan] 多智能体调用平台 · CLI
输入消息开始对话；/help 查看命令，/quit 退出。
"""

HELP = """[bold]命令[/bold]
  /help    显示帮助
  /new     新建会话（清空上下文）
  /team    切换到 Team 多智能体模式
  /agent   切换到单 Agent 模式
  /plan    进入 Plan 模式（只读探索→写计划→审批后执行）
  /compact 立即压缩当前会话上下文（idle 维护窗口；历史原文保留在会话库）
  /todo    查看当前任务清单
  /quit    退出（也可用 /exit）
"""


def register(subparsers, handlers: dict[str, Any]) -> None:
    chat = subparsers.add_parser("chat", help="启动交互式对话")
    chat.add_argument("--session-id", metavar="ID", help="续接当前账号与工作空间中的已有会话")
    chat.add_argument(
        "-c", "--continue", dest="continue_last", action="store_true",
        help="续接当前账号与工作空间中最近一次有内容的会话",
    )
    chat.set_defaults(handler=_chat)
    run = subparsers.add_parser("run", help="一次性非交互对话")
    run.add_argument("prompt", nargs="?", default="", help="要发送的消息（也可用 --prompt 或 stdin）")
    run.add_argument(
        "-p", "--prompt", dest="prompt_option", default="",
        help="要发送的消息；与位置参数二选一，适合以 - 开头的文本",
    )
    run.add_argument("--mode", choices=("agent", "team", "dynamic_kanban"), default="agent")
    run.add_argument("--session-id", default="", help="复用已有会话，缺省新建")
    run.add_argument(
        "-c", "--continue", dest="continue_last", action="store_true",
        help="向当前账号与工作空间中最近一次有内容的会话续发一条消息",
    )
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


async def _continue_session_id(app: Any, ctx: CliContext) -> str:
    """Resolve the most recently updated non-empty session in this owner and workspace."""
    rows = await asyncio.to_thread(
        app.session_store.list_sessions,
        ctx.workspace_id,
        owner_account_id=ctx.owner,
    )
    candidates = [row for row in rows if int(row.get("message_count") or 0) > 0]
    if not candidates:
        raise CliError("当前工作空间还没有可续接的会话；请先用 crew chat 或 crew run 开始对话")
    chosen = max(candidates, key=lambda row: (
        float(row.get("updated_at") or 0), str(row.get("session_id") or ""),
    ))
    return str(chosen["session_id"])


async def _chat(args: Any, ctx: CliContext) -> CliResult:
    session_id = args.session_id.strip() if args.session_id is not None else None
    if session_id == "":
        raise CliError("--session-id 不能为空")
    if args.continue_last and args.session_id is not None:
        raise CliError("--continue 与 --session-id 不能同时使用")
    async with ctx.running_app() as app:
        if args.continue_last:
            session_id = await _continue_session_id(app, ctx)
        elif session_id is not None:
            rows = await asyncio.to_thread(
                app.session_store.list_sessions,
                owner_account_id=ctx.owner,
                include_archived=True,
                session_id=session_id,
                limit=1,
            )
            if not rows:
                raise CliError("当前账号下未找到可续接的会话；请用 crew session list 查询")
            workspace_id = rows[0]["workspace_id"]
            if workspace_id != ctx.workspace_id:
                raise CliError(f"会话属于其他工作空间；请用 --workspace-id {workspace_id} 续接")
        await _repl(app, ctx, session_id=session_id)
    return CliResult(data=None)


async def _repl(app: Any, ctx: CliContext, *, session_id: str | None = None) -> None:
    session = PromptSession()
    session_id = session_id or f"cli_{uuid.uuid4().hex[:8]}"
    mode = "agent"

    console.print(BANNER)
    console.print("当前会话 ID：", session_id, markup=False)
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
                    try:
                        app.plan_manager.reset(session_id, owner_account_id=ctx.owner)
                    except Exception as exc:  # noqa: BLE001 - a stale Plan must not block a new session
                        console.print(f"[yellow]旧会话 Plan 状态清理失败: {exc}[/yellow]")
                session_id = f"cli_{uuid.uuid4().hex[:8]}"
                console.print("[green]已新建会话。[/green]")
                console.print("当前会话 ID：", session_id, markup=False)
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
                    try:
                        app.plan_manager.enter(session_id, owner_account_id=ctx.owner)
                    except Exception as exc:  # noqa: BLE001 - keep the REPL usable on Plan setup failure
                        console.print(f"[red]进入 Plan 模式失败: {exc}[/red]")
                    else:
                        console.print(
                            "[bold yellow]已进入 Plan 模式（只读）。[/bold yellow] "
                            "描述你的需求，我会探索代码、写出计划，再请你确认后执行。"
                        )
            elif cmd == "/todo":
                try:
                    _print_todos(app, session_id, ctx.owner)
                except Exception as exc:  # noqa: BLE001 - task store failure must not end the REPL
                    console.print(f"[red]读取任务清单失败: {exc}[/red]")
            elif cmd == "/compact":
                try:
                    await _compact_context(app, session_id, ctx.owner)
                except Exception as exc:  # noqa: BLE001 - compaction failure must not end the REPL
                    console.print(f"[red]压缩上下文失败: {exc}[/red]")
            else:
                try:
                    result = await app.plugins.run_plugin_command(
                        text,
                        session_id=session_id,
                        owner_account_id=ctx.owner,
                        channel="cli",
                    )
                except Exception as exc:  # noqa: BLE001 - plugin failure must not end the REPL
                    console.print(f"[red]插件命令失败: {exc}[/red]")
                    continue
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
        await _render(app, envelope, session)
        if (
            app.plan_manager is not None
            and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=ctx.owner)
        ):
            await _handle_plan_approval(app, session, envelope)


async def _compact_context(app: Any, session_id: str, owner_account_id: str) -> None:
    """手动 /compact：idle 维护窗口内立即压缩（REPL 只在回合间接受输入，天然空闲）。"""
    agent = app.agents.peek(session_id, owner_account_id)
    compactor = getattr(agent, "compactor", None)
    if compactor is None:
        console.print("[yellow]当前会话尚无压缩器：先开始一段对话再 /compact。[/yellow]")
        return
    from crew.agent.compact.tokens import estimate_tokens

    history = await app.session_store.load_async(session_id, owner_account_id=owner_account_id)
    before = estimate_tokens(history)
    _view, changed = await compactor.compact_now(history, session_id, owner_account_id=owner_account_id)
    if changed:
        console.print(
            f"[green]已压缩上下文：{before} → {estimate_tokens(_view)} tokens；"
            "历史原文保留在会话库中，可回溯查询。[/green]"
        )
    else:
        console.print("[dim]暂无可压缩的历史区间（历史太短或已在低位）。[/dim]")


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


def _plan_execution_envelope(envelope: Envelope) -> Envelope:
    return Envelope.of(
        "计划已批准，请按上述计划开始执行。",
        session_id=envelope.session_id,
        channel=envelope.channel,
        mode=envelope.mode,
        user_id=envelope.user_id,
        workspace_id=envelope.workspace_id,
    )


async def _prompt(session: PromptSession, message: str) -> str:
    with patch_stdout():
        return await session.prompt_async(message)


async def _confirm(session: PromptSession, message: str, accepted: tuple[str, ...]) -> bool:
    try:
        answer = await _prompt(session, message)
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in accepted


async def _handle_plan_approval(app: Any, session: PromptSession, envelope: Envelope) -> None:
    from crew.agent.plan import read_plan

    session_id = envelope.session_id
    owner_account_id = envelope.user_id
    plan_text = read_plan(session_id, owner_account_id=owner_account_id) or "(计划文件为空)"
    console.print("\n[bold cyan]──── 待审批的计划 ────[/bold cyan]")
    console.print(plan_text)
    console.print("[bold cyan]─────────────────────[/bold cyan]")
    if await _confirm(session, "批准计划并开始执行？[y/N] ", ("y", "yes", "是", "ok")):
        app.plan_manager.approve(session_id, owner_account_id=owner_account_id)
        console.print("[green]计划已批准，开始执行……[/green]\n")
        await _render(app, _plan_execution_envelope(envelope), session)
    else:
        app.plan_manager.reject(session_id, owner_account_id=owner_account_id)
        console.print("[yellow]已保留 Plan 模式，请继续告诉我如何完善计划。[/yellow]")


async def _render(app: Any, envelope: Envelope, session: PromptSession) -> None:
    input_lock = asyncio.Lock()

    async def prompt_tool_approval(message: str) -> bool:
        async with input_lock:
            return await _confirm(session, message, ("once",))

    async def ask(message: str) -> str:
        return await _prompt(session, message)

    envelope.params["_allow_mcp_install"] = True
    async with conversation_followups(envelope, ask, input_lock):
        await consume_conversation_turn(app, envelope, _render_chunk, prompt=prompt_tool_approval)


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
    positional = str(getattr(args, "prompt", "") or "").strip()
    option = str(getattr(args, "prompt_option", "") or "").strip()
    if positional and option:
        raise CliError("位置参数与 --prompt 只能提供一个")
    if len(positional) > _MAX_RUN_PROMPT_CHARS or len(option) > _MAX_RUN_PROMPT_CHARS:
        raise CliError(f"prompt 超过上限 {_MAX_RUN_PROMPT_CHARS} 字符")
    prompt = option or positional
    if not prompt and not sys.stdin.isatty():
        try:
            prompt = sys.stdin.read(_MAX_RUN_PROMPT_CHARS + 1).strip()
        except (OSError, ValueError):
            prompt = ""
    if len(prompt) > _MAX_RUN_PROMPT_CHARS:
        raise CliError(f"prompt 超过上限 {_MAX_RUN_PROMPT_CHARS} 字符")
    if not prompt:
        raise CliError("prompt 不能为空；可通过位置参数、--prompt 或 stdin 提供")
    output_format = args.output_format or ("json" if ctx.json_output else "text")
    explicit_session = args.session_id.strip() or None
    if args.continue_last and explicit_session is not None:
        raise CliError("--continue 与 --session-id 不能同时使用")
    session_id = explicit_session or f"cli_{uuid.uuid4().hex[:8]}"

    async with ctx.running_app() as app:
        if args.continue_last:
            session_id = await _continue_session_id(app, ctx)
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
        chunks: list[ResponseChunk] = []

        def collect(chunk: ResponseChunk) -> None:
            chunks.append(chunk)
            if output_format == "stream-json":
                print(json.dumps(_chunk_dict(chunk), ensure_ascii=False, default=str))
            elif output_format == "text":
                _render_chunk(chunk)

        async def collect_turn(turn_envelope: Envelope) -> None:
            try:
                await consume_conversation_turn(app, turn_envelope, collect)
            except CliApprovalRequired as error:
                if output_format == "json":
                    data = _summarize(session_id, chunks)
                    data.update(status="stopped", error=str(error), error_code="approval_required")
                    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
                raise

        await collect_turn(envelope)

        if (
            args.yes
            and app.plan_manager is not None
            and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=ctx.owner)
        ):
            app.plan_manager.approve(session_id, owner_account_id=ctx.owner)
            await collect_turn(_plan_execution_envelope(envelope))
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
