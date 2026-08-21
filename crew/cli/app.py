"""CLI 公共上下文与输出助手。

所有子命令共享的进程内 App 装配、全局参数解析结果、错误与结果渲染。
CLI 默认不依赖 Gateway，直接复用 ``build_app()`` 装配的 service/store。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from crew.app import build_app
from crew.state.config import load_config

DEFAULT_CLI_OWNER = os.getenv("CREW_CLI_ACCOUNT", "local")


class CliError(RuntimeError):
    """CLI 业务错误，携带进程退出码。"""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def parse_json(value: str | None, *, name: str = "JSON") -> Any:
    """解析 ``--json`` 这类透传 payload；非 JSON 字符串直接报错。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CliError(f"{name} 不是合法 JSON: {exc}") from exc


@dataclass
class CliResult:
    """命令结果：``data`` 用于 --json，``text`` 用于人类可读输出。"""

    data: Any = None
    text: str | None = None


@dataclass
class CliContext:
    """一次 CLI 进程的全局参数与懒加载 App。"""

    owner: str = DEFAULT_CLI_OWNER
    workspace_id: str = "default"
    config_path: str | None = None
    json_output: bool = False
    quiet: bool = False
    _app: Any = None

    @property
    def app(self) -> Any:
        if self._app is None:
            self._app = build_app(load_config(self.config_path))
        return self._app

    @asynccontextmanager
    async def running_app(self) -> AsyncIterator[Any]:
        """装配 App 并拉起后台能力，命令结束后统一关闭。"""
        app = self.app
        await app.startup()
        try:
            yield app
        finally:
            await app.shutdown()
            self._app = None


def emit(result: CliResult, *, json_output: bool, quiet: bool = False) -> None:
    """按全局输出模式打印结果。"""
    if json_output:
        print(json.dumps(result.data, ensure_ascii=False, indent=2, default=str))
        return
    if quiet:
        return
    if result.text is not None:
        print(result.text)
    elif result.data is not None:
        print(json.dumps(result.data, ensure_ascii=False, indent=2, default=str))


def require_callable(target: Any, name: str) -> Any:
    """从 App/Store 取方法，缺失时给出明确 CLI 错误。"""
    if not callable(target):
        raise CliError(f"{name} 当前不可用（未装配或依赖缺失）")
    return target


def write_stderr(message: str) -> None:
    print(message, file=sys.stderr)


__all__ = [
    "DEFAULT_CLI_OWNER",
    "CliContext",
    "CliError",
    "CliResult",
    "emit",
    "parse_json",
    "require_callable",
    "write_stderr",
]
