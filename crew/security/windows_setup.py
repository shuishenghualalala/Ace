"""Bound Windows Desktop setup waits before spawning managed commands."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

_SETUP_POLL_SECONDS = 0.05
_SETUP_MAX_WAIT_SECONDS = 30.0


def _state(directory: Path) -> Literal["idle", "pending", "ready", "failed"]:
    if not directory.is_dir():
        return "idle"
    if (directory / "ready").is_file():
        return "ready"
    if (directory / "failed").is_file():
        return "failed"
    return "pending"


async def wait_for_windows_sandbox_setup(
    helper_argv: tuple[str, ...], *, timeout: float | None, allow_wait: bool = True,
) -> float | None:
    """Wait only for an observed automatic attempt for this bundled helper."""
    if sys.platform != "win32" or not helper_argv:
        return timeout
    directory_value = os.environ.get("ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR")
    runtime = os.environ.get("ACE_SECURITY_RUNTIME")
    if not directory_value or not runtime:
        return timeout
    directory = Path(directory_value)
    runtime_path = Path(runtime)
    if not directory.is_absolute() or not runtime_path.is_absolute():
        return timeout

    loop = asyncio.get_running_loop()
    started = loop.time()
    wait_limit = _SETUP_MAX_WAIT_SECONDS if timeout is None else min(timeout, _SETUP_MAX_WAIT_SECONDS)
    try:
        async with asyncio.timeout(wait_limit):
            state = await asyncio.to_thread(_state, directory)
            if state != "pending":
                return timeout
            expected_helper = await asyncio.to_thread(runtime_path.resolve)
            if expected_helper != Path(helper_argv[0]):
                return timeout
            if not allow_wait:
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_UNAVAILABLE, "Windows 沙箱正在准备，命令尚未启动。",
                )
            while state == "pending":
                await asyncio.sleep(_SETUP_POLL_SECONDS)
                state = await asyncio.to_thread(_state, directory)
            remaining = None if timeout is None else timeout - (loop.time() - started)
            if remaining is not None and remaining <= 0:
                raise TimeoutError
    except TimeoutError as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE, "等待 Windows 沙箱准备超时，命令尚未启动。",
        ) from exc
    except OSError as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE, "无法读取 Windows 沙箱准备状态，命令尚未启动。",
        ) from exc
    if state != "ready":
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE, "Windows 沙箱未完成准备，命令尚未启动。",
        )
    return remaining
