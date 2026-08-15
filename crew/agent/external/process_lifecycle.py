"""Shared lifecycle helpers for locally spawned external-agent runtimes."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.security.process_lifecycle import windows_system_executable

MAX_EXTERNAL_ENV_BYTES = 256 * 1024
MAX_EXTERNAL_ARGV_BYTES = 1024 * 1024
MAX_EXTERNAL_PROBE_OUTPUT_BYTES = 2 * 1024 * 1024
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_UNSAFE_EXTERNAL_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "BASH_ENV",
        "CDPATH",
        "ELECTRON_RUN_AS_NODE",
        "ENV",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_EXEC_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "JAVA_TOOL_OPTIONS",
        "JWT",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NO_PROXY",
        "PERL5OPT",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SSLKEYLOGFILE",
        "_JAVA_OPTIONS",
    }
)
_UNSAFE_EXTERNAL_ENV_PREFIXES = (
    "ACE_",
    "CREW_",
    "DYLD_",
    "GIT_CONFIG_",
    "LD_",
)


class ExternalProcessBoundaryError(RuntimeError):
    """Raised before or during an external-process security boundary."""


class ExternalProcessOutputLimitError(ExternalProcessBoundaryError):
    """Raised when a trusted discovery probe exceeds its output budget."""


@dataclass(frozen=True)
class ExternalProbeResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def validate_external_env_overrides(
    custom_env: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate explicit runtime environment without accepting process-control hooks."""

    from crew.security.launch import INHERITED_ENV_NAMES

    result: dict[str, str] = {}
    seen: set[str] = set()
    encoded_size = 0
    for raw_name, raw_value in dict(custom_env or {}).items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise ExternalProcessBoundaryError("external runtime environment must contain strings")
        name = raw_name.strip()
        normalized = name.upper()
        if (
            not _ENV_NAME.fullmatch(name)
            or normalized in seen
            or normalized in INHERITED_ENV_NAMES
            or normalized in _UNSAFE_EXTERNAL_ENV_NAMES
            or normalized.startswith(_UNSAFE_EXTERNAL_ENV_PREFIXES)
            or "\x00" in raw_value
        ):
            raise ExternalProcessBoundaryError(
                f"external runtime environment contains a disallowed entry: {name or '<empty>'}"
            )
        encoded_size += len(name.encode("utf-8")) + len(raw_value.encode("utf-8"))
        if encoded_size > MAX_EXTERNAL_ENV_BYTES:
            raise ExternalProcessBoundaryError(
                "external runtime environment exceeds the size limit"
            )
        seen.add(normalized)
        result[name] = raw_value
    return result


def external_runtime_environment(
    custom_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an allowlisted host environment plus explicit validated overrides."""

    from crew.security.launch import minimal_inherited_environment

    environment = minimal_inherited_environment()
    environment.update(validate_external_env_overrides(custom_env))
    return environment


def resolve_external_executable(
    executable_path: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve one executable to a canonical file before snapshot authorization."""

    raw = str(executable_path or "").strip()
    if not raw or "\x00" in raw:
        raise FileNotFoundError("external runtime executable is missing")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ExternalProcessBoundaryError(
            "external runtime executable must be an absolute discovered path"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(raw) from exc
    if not resolved.is_file():
        raise FileNotFoundError(raw)
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise PermissionError(f"external runtime is not executable: {resolved}")
    return str(resolved)


def _validated_argv(executable: str, args: Sequence[str]) -> tuple[str, ...]:
    if any(not isinstance(arg, str) for arg in args):
        raise ExternalProcessBoundaryError("external runtime argv must contain strings")
    argv = (executable, *args)
    encoded_size = 0
    for part in argv:
        if "\x00" in part:
            raise ExternalProcessBoundaryError("external runtime argv contains an invalid token")
        encoded_size += len(part.encode("utf-8"))
        if encoded_size > MAX_EXTERNAL_ARGV_BYTES:
            raise ExternalProcessBoundaryError("external runtime argv exceeds the size limit")
    return argv


def _trusted_probe_launch(argv: tuple[str, ...], cwd: Path):
    """Issue exact, host-control-plane authority for one fixed discovery probe."""

    from crew.security.actions import normalize_exec_action
    from crew.security.context import SecurityContext
    from crew.security.launch import issue_process_launch
    from crew.security.models import (
        PermissionProfile,
        PermissionProfileKind,
        SandboxablePreference,
    )

    return issue_process_launch(
        SecurityContext(
            os_user="host-control-plane",
            owner_account_id="system:external-runtime-discovery",
            workspace_id="system:external-runtime-discovery",
            workspace_root=cwd,
            session_id="external-runtime-discovery",
            request_id="",
            task_id=f"probe-{secrets.token_hex(8)}",
            cwd=cwd,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
        sandbox_preference=SandboxablePreference.FORBID,
        sandbox_system_surface="external-runtime-discovery",
        approved_action=normalize_exec_action(argv, cwd),
    )


async def _spawn_with_authority(
    executable_path: str,
    args: Sequence[str],
    *,
    cwd: str | Path,
    custom_env: Mapping[str, str] | None,
    trusted_probe: bool,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    limit: int,
) -> asyncio.subprocess.Process:
    from crew.security.launch import (
        current_process_launch,
        finalize_process_launch,
        validate_process_launch,
    )
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
    from crew.security.snapshot import AuthorizationSnapshotError, consume_authorization_snapshot

    if not isinstance(limit, int) or not 1024 <= limit <= 64 * 1024 * 1024:
        raise ExternalProcessBoundaryError("external runtime stream limit is invalid")
    environment = external_runtime_environment(custom_env)
    executable = resolve_external_executable(
        executable_path,
        environment=environment,
    )
    argv = _validated_argv(executable, args)
    try:
        resolved_cwd = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExternalProcessBoundaryError(
            "external runtime working directory is unavailable"
        ) from exc
    if not resolved_cwd.is_dir():
        raise ExternalProcessBoundaryError("external runtime working directory is not a directory")

    launch = current_process_launch.get()
    if launch is not None:
        validate_process_launch(launch)
    elif trusted_probe:
        launch = _trusted_probe_launch(argv, resolved_cwd)
    else:
        validate_process_launch(None)
        raise AssertionError("unreachable")
    if launch.managed:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "managed external stdio requires native bidirectional transport",
        )

    authorization = finalize_process_launch(
        launch,
        argv=argv,
        cwd=resolved_cwd,
        environment=environment,
    )
    try:
        snapshot = consume_authorization_snapshot(
            authorization,
            environment=environment,
            expected_owner_account_id=launch.owner_account_id,
            expected_workspace_id=launch.workspace_id,
            expected_session_id=launch.session_id,
            expected_task_id=launch.task_id,
        )
    except AuthorizationSnapshotError as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"external process authorization snapshot rejected: {exc}",
        ) from exc

    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *snapshot.argv,
            cwd=snapshot.cwd,
            env=environment,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            limit=limit,
            **isolated_process_kwargs(),
        )
    )
    try:
        return await asyncio.shield(spawn_task)
    except asyncio.CancelledError:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.shield(spawn_task)
        except Exception:  # noqa: BLE001, S110 - preserve caller cancellation
            pass
        if process is not None:
            await terminate_process_tree(process)
        raise


async def spawn_authorized_external_process(
    executable_path: str,
    *args: str,
    cwd: str | Path,
    custom_env: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    limit: int = 64 * 1024,
) -> asyncio.subprocess.Process:
    """Spawn bidirectional host stdio only under a consumed disabled launch snapshot."""

    return await _spawn_with_authority(
        executable_path,
        args,
        cwd=cwd,
        custom_env=custom_env,
        trusted_probe=False,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        limit=limit,
    )


async def spawn_trusted_probe_process(
    executable_path: str,
    *args: str,
    cwd: str | Path,
    custom_env: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    limit: int = 64 * 1024,
) -> asyncio.subprocess.Process:
    """Spawn one fixed control-plane probe without ambient credentials.

    A bound launch is still validated and managed mode remains fail-closed. Only
    a context-free discovery call receives a fresh exact disabled authority.
    """

    return await _spawn_with_authority(
        executable_path,
        args,
        cwd=cwd,
        custom_env=custom_env,
        trusted_probe=True,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        limit=limit,
    )


async def run_trusted_external_probe(
    executable_path: str,
    *args: str,
    custom_env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    max_output_bytes: int = MAX_EXTERNAL_PROBE_OUTPUT_BYTES,
) -> ExternalProbeResult:
    """Run a fixed discovery command with timeout, output, snapshot, and tree bounds."""

    if timeout <= 0:
        raise ValueError("external runtime probe timeout must be positive")
    if max_output_bytes <= 0:
        raise ValueError("external runtime probe output limit must be positive")
    with tempfile.TemporaryDirectory(prefix="crew-runtime-probe-") as probe_cwd:
        process = await spawn_trusted_probe_process(
            executable_path,
            *args,
            cwd=probe_cwd,
            custom_env=custom_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        total_output = 0

        async def _read(
            stream: asyncio.StreamReader | None,
            target: bytearray,
        ) -> None:
            nonlocal total_output
            if stream is None:
                return
            while chunk := await stream.read(64 * 1024):
                if total_output + len(chunk) > max_output_bytes:
                    raise ExternalProcessOutputLimitError(
                        "external runtime probe output exceeds the configured limit"
                    )
                total_output += len(chunk)
                target.extend(chunk)

        readers = [
            asyncio.create_task(_read(process.stdout, stdout_buffer)),
            asyncio.create_task(_read(process.stderr, stderr_buffer)),
        ]
        waiter = asyncio.create_task(process.wait())
        tasks = [*readers, waiter]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        except asyncio.CancelledError:
            await terminate_process_tree(process)
            raise
        except Exception:
            await terminate_process_tree(process)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return ExternalProbeResult(
            returncode=int(process.returncode or 0),
            stdout=bytes(stdout_buffer),
            stderr=bytes(stderr_buffer),
        )


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
            taskkill = windows_system_executable("taskkill.exe")
            if taskkill:
                from crew.security.launch import minimal_inherited_environment

                killer = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/PID",
                    str(int(proc.pid)),
                    "/T",
                    "/F",
                    env=minimal_inherited_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    await asyncio.wait_for(killer.wait(), timeout=max(1.0, timeout))
                except TimeoutError:
                    killer.kill()
                    await asyncio.wait_for(killer.wait(), timeout=max(1.0, timeout))
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (TimeoutError, ProcessLookupError, PermissionError, OSError):
        pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(1.0, timeout))
        except TimeoutError:
            pass


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
    except TimeoutError:
        await terminate_process_tree(proc, timeout=max(1.0, grace))
        return True
