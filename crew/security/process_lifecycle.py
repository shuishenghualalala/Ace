"""Cross-platform lifecycle helpers for host processes at the security boundary."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Any


def isolated_process_kwargs() -> dict[str, Any]:
    """Start a host process in a group that can be terminated as one tree."""
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        }
    return {"start_new_session": True}


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    timeout: float = 2.0,
) -> None:
    """Best-effort termination for a process and every descendant it spawned."""
    try:
        if os.name == "nt":
            if process.returncode is not None:
                return
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=max(1.0, timeout))
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, asyncio.TimeoutError):
        pass

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        await process.wait()
