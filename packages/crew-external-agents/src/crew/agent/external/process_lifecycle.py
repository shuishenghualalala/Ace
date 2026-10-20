"""Shared lifecycle helpers for locally spawned external-agent runtimes."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Any


def isolated_process_kwargs(*, platform_name: str | None = None) -> dict[str, Any]:
    """Start an external runtime in a killable process group on every OS."""
    if (platform_name or os.name) == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        }
    return {"start_new_session": True}


async def terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float = 2.0,
) -> None:
    """Best-effort termination for a runtime and every process it spawned."""
    try:
        if os.name == "nt":
            if proc.returncode is not None:
                return
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=max(1.0, timeout))
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, asyncio.TimeoutError):
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        await proc.wait()


async def finish_process_after_terminal(
    proc: asyncio.subprocess.Process,
    *,
    stdin: Any = None,
    grace_timeout: float = 1.0,
) -> bool:
    """Close a completed protocol stream and reap its process.

    Runtime adapters call this only after receiving their authoritative turn
    terminal event.  A cooperative process exits on stdin EOF; a runtime that
    remains resident is reclaimed through the same cross-platform process-tree
    path used for cancellation.  Returns ``True`` when forced cleanup was
    required.
    """

    if stdin is not None and not stdin.is_closing():
        try:
            stdin.close()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
    if proc.returncode is not None:
        return False

    grace = max(0.05, float(grace_timeout))
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return False
    except asyncio.TimeoutError:
        await terminate_process_tree(proc, timeout=max(1.0, grace))
        return True
