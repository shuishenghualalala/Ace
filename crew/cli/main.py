"""CLI 交互入口（REPL）。用于 CLI。

用法：
    python -m crew.cli          # 启动交互
slash 命令：
    /help  /new  /team  /agent  /quit
"""

from __future__ import annotations

import asyncio
import os
import uuid

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from crew.app import build_app
from crew.core.envelope import Envelope

console = Console()
DEFAULT_CLI_OWNER = os.getenv("CREW_CLI_ACCOUNT", "local")


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


async def _run() -> None:
    app = build_app()
    await app.startup()  # 连接 MCP server、启动 cron（失败静默降级）
    session = PromptSession()
    session_id = f"cli_{uuid.uuid4().hex[:8]}"
    mode = "agent"

    console.print(BANNER)
    if not app.config.has_llm_key:
        console.print(
            "[yellow]提示：未配置模型 API Key，当前为 FakeProvider 演示模式，"
            "不会调用真实模型。请在 .env 和 config/config.yaml 中完成模型配置。[/yellow]\n"
        )

    try:
        await _repl(app, session, session_id, mode, DEFAULT_CLI_OWNER)
    finally:
        await app.shutdown()


async def _repl(app, session, session_id: str, mode: str, owner_account_id: str) -> None:
    while True:
        try:
            with patch_stdout():
                text = await session.prompt_async(f"[{mode}] › ")
        except (EOFError, KeyboardInterrupt):
            break

        text = text.strip()
        if not text:
            continue

        # slash 命令
        if text.startswith("/"):
            cmd = text.lower()
            if cmd in ("/quit", "/exit"):
                break
            if cmd == "/help":
                console.print(HELP)
            elif cmd == "/new":
                if app.plan_manager is not None:
                    app.plan_manager.reset(session_id, owner_account_id=owner_account_id)
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
                    app.plan_manager.enter(session_id, owner_account_id=owner_account_id)
                    console.print(
                        "[bold yellow]已进入 Plan 模式（只读）。[/bold yellow] "
                        "描述你的需求，我会探索代码、写出计划，再请你确认后执行。"
                    )
            elif cmd == "/todo":
                _print_todos(app, session_id, owner_account_id)
            else:
                result = await app.plugins.run_plugin_command(
                    text,
                    session_id=session_id,
                    owner_account_id=owner_account_id,
                    channel="cli",
                )
                if result is None:
                    console.print(f"[red]未知命令: {text}[/red]")
                elif result:
                    console.print(f"{result}\n")
            continue

        envelope = Envelope.of(text, session_id=session_id, channel="cli", mode=mode, user_id=owner_account_id)
        await _render(app, envelope)
        # Plan 模式：本轮若模型调用了 exit_plan_mode，则在此请用户审批
        if app.plan_manager is not None and app.plan_manager.is_awaiting_approval(session_id, owner_account_id=owner_account_id):
            await _handle_plan_approval(app, session, session_id, mode, owner_account_id)


def _print_todos(app, session_id: str, owner_account_id: str) -> None:
    """打印当前会话的任务清单。"""
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


async def _handle_plan_approval(app, session, session_id: str, mode: str, owner_account_id: str) -> None:
    """展示计划并请用户审批：y 批准并执行 / n 留在 plan 模式继续完善。"""
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


async def _render(app, envelope: Envelope) -> None:
    async for chunk in app.handle(envelope):
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


def main() -> None:
    import sys

    # 子命令：crew mcp serve → 启动 MCP Server（stdio）
    if sys.argv[1:2] == ["mcp"] and sys.argv[2:3] == ["serve"]:
        from crew.gateway.mcp_server import serve
        serve()
        return
    if sys.argv[1:2] == ["mcp"] and sys.argv[2:3] == ["interaction-proxy"]:
        from crew.gateway.mcp_server import serve_interaction_proxy
        serve_interaction_proxy()
        return
    if sys.argv[1:3] == ["migrate", "claim-legacy"]:
        _claim_legacy(sys.argv[3:])
        return

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]再见。[/dim]")


def _claim_legacy(argv: list[str]) -> None:
    """CLI wrapper for claiming pre-account rows."""

    import argparse

    from crew.state._migration import OWNER_TABLE_LABELS, claim_legacy_owner_database
    from crew.state.config import load_config

    parser = argparse.ArgumentParser(prog="python -m crew.cli migrate claim-legacy")
    parser.add_argument("--account", required=True, help="目标 owner_account_id，例如 owner:user-a")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args(argv)

    owner = args.account.strip()
    if not owner:
        parser.error("--account 不能为空")

    cfg = load_config()
    _prepare_state_schema(cfg)
    changed, remaining = claim_legacy_owner_database(
        cfg.db_path,
        owner,
        dry_run=bool(args.dry_run),
        wal_enabled=cfg.sqlite_wal,
    )
    verb = "将认领" if args.dry_run else "已认领"
    for table, count in changed.items():
        if count:
            console.print(f"{verb} {count} 条{OWNER_TABLE_LABELS.get(table, table)}")
    if not any(changed.values()):
        console.print("[dim]没有未归属数据。[/dim]")
    if not args.dry_run:
        left = {table: count for table, count in remaining.items() if count}
        if left:
            console.print(f"[yellow]仍有未归属数据: {left}[/yellow]")


def _prepare_state_schema(cfg) -> None:
    """Initialize state schemas before running raw migration SQL."""

    from crew.cron import CronJobStore
    from crew.state.session_store import SQLiteSessionStore
    from crew.state.workspace_store import SQLiteWorkspaceStore
    from crew.tasks import TaskRuntime

    SQLiteSessionStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    SQLiteWorkspaceStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    CronJobStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    tasks = TaskRuntime(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    tasks.close()


if __name__ == "__main__":
    main()
