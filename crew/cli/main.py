"""CLI 入口：全局参数提取、子命令树与统一分发。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import Any

from crew.cli import chat, content, integration, knowledge, management, security
from crew.cli.app import DEFAULT_CLI_OWNER, CliContext, CliError, emit

CREW_VERSION = "0.1.0"

_VALUE_FLAGS = {
    "--owner": "owner",
    "-o": "owner",
    "--workspace-id": "workspace_id",
    "--config": "config_path",
}
_BOOL_FLAGS = {
    "--json": "json_output",
    "--quiet": "quiet",
}


def extract_global_flags(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """从任意位置提取全局参数，剩余参数交给子命令解析。"""
    flags: dict[str, Any] = {}
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        consumed = False
        for name, dest in _VALUE_FLAGS.items():
            if token == name:
                if index + 1 >= len(argv):
                    raise CliError(f"{name} 缺少参数值")
                flags[dest] = argv[index + 1]
                index += 2
                consumed = True
                break
            if token.startswith(f"{name}="):
                flags[dest] = token.split("=", 1)[1]
                index += 1
                consumed = True
                break
        if consumed:
            continue
        if token in _BOOL_FLAGS:
            flags[_BOOL_FLAGS[token]] = True
            index += 1
            continue
        rest.append(token)
        index += 1
    return flags, rest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crew",
        description="Crew 多智能体工作台 CLI（进程内直调，不依赖 Gateway）",
    )
    parser.add_argument("--version", action="version", version=f"crew {CREW_VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in (chat, management, knowledge, integration, security, content):
        module.register(subparsers, {})
    return parser


def _invoke(handler: Callable, args: argparse.Namespace, ctx: CliContext) -> Any:
    result = handler(args, ctx)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        flags, rest = extract_global_flags(raw)
        if not rest:
            rest = ["chat"]
        parser = build_parser()
        args = parser.parse_args(rest)
        handler = getattr(args, "handler", None)
        if handler is None:
            parser.print_help()
            return 0
        ctx = CliContext(
            owner=str(flags.get("owner") or DEFAULT_CLI_OWNER),
            workspace_id=str(flags.get("workspace_id") or "default"),
            config_path=flags.get("config_path"),
            json_output=bool(flags.get("json_output")),
            quiet=bool(flags.get("quiet")),
        )
        result = _invoke(handler, args, ctx)
        emit(result, json_output=ctx.json_output, quiet=ctx.quiet)
        return 0
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["extract_global_flags", "main"]
