"""Cross-platform lifecycle helpers for host processes at the security boundary."""

from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
from pathlib import Path
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


def windows_system_directory() -> Path | None:
    """Return the kernel-reported Windows system directory."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if not length or length >= len(buffer):
            return None
        return Path(buffer.value).resolve(strict=True)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None


def windows_system_executable(name: str) -> str | None:
    """Resolve a Windows system executable from the kernel directory, never PATH."""
    if not name or Path(name).name != name:
        return None
    try:
        system_directory = windows_system_directory()
        if system_directory is None:
            return None
        candidate = (system_directory / name).resolve(strict=True)
        if candidate.parent != system_directory or not candidate.is_file():
            return None
        return str(candidate)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    timeout: float = 2.0,
) -> None:
    """Best-effort termination for a process and every descendant it spawned."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise TypeError("process termination timeout is invalid")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("process termination timeout is invalid")
    if process.returncode is not None:
        return
    process_group_id: int | None = None
    try:
        if os.name == "nt":
            taskkill = windows_system_executable("taskkill.exe")
            if taskkill is None:
                raise OSError("trusted taskkill executable is unavailable")
            from crew.security.launch import minimal_native_helper_environment

            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                env=minimal_native_helper_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=max(1.0, timeout))
        else:
            process_group_id = os.getpgid(process.pid)
            os.killpg(process_group_id, signal.SIGTERM)
    except (TimeoutError, ProcessLookupError, PermissionError, OSError):
        pass

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        try:
            if os.name != "nt":
                if process_group_id is None:
                    process_group_id = os.getpgid(process.pid)
                os.killpg(process_group_id, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=max(1.0, timeout))
        except (TimeoutError, ProcessLookupError, OSError):
            pass
