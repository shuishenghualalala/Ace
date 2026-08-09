"""Versioned client for the per-task native security runtime."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import signal
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence


RUNTIME_PROTOCOL_VERSION = 2
_MAX_REQUEST_FRAME = 2 * 1024 * 1024
_MAX_PROTOCOL_FRAME = 128 * 1024
_MAX_OUTPUT_CHUNK = 64 * 1024
_MAX_STDIN_BYTES = 1024 * 1024
_MAX_ENV_BYTES = 256 * 1024
_MAX_HOME_FILE_BYTES = 1024 * 1024
_MAX_HOME_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_HOME_FILES = 64
_MAX_HELPER_STDERR = 64 * 1024
_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        # These values are established by the native sandbox backend. Allowing
        # callers to replace them would make the child observe host-controlled
        # paths or bypass the backend's private home/tmp layout.
        "PATH",
        "HOME",
        "TMPDIR",
        "PWD",
        "OLDPWD",
    }
)
_RESERVED_ENV_PREFIXES = ("ACE_SECURITY_", "ACE_BUNDLED_")
_REQUIRED_READY_CAPABILITIES = frozenset({"stdin_once", "stream_output"})
_INTERACTIVE_READY_CAPABILITIES = _REQUIRED_READY_CAPABILITIES | {"stdin_bidirectional"}
_LOGGER = logging.getLogger(__name__)


class RuntimeErrorCode(StrEnum):
    """Stable errors exposed by the native-runtime boundary."""

    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    RUNTIME_PROTOCOL_MISMATCH = "runtime_protocol_mismatch"
    RUNTIME_CRASHED = "runtime_crashed"
    SANDBOX_DENIED = "sandbox_denied"
    NETWORK_UNAVAILABLE = "network_unavailable"
    TIMEOUT = "timeout"
    OUTPUT_TRUNCATED = "output_truncated"


class NativeRuntimeError(RuntimeError):
    """A fail-closed native-runtime failure with a stable public code."""

    def __init__(self, code: RuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


# Backend names the host treats as the Windows sandbox family. The native Windows
# runtime reports ``windows_sandbox_account`` (it runs the command under a dedicated
# offline/online sandbox account, not the interactive user); legacy/test stubs may
# report the bare ``windows`` tag. Both must trigger the Windows-only capability
# gate below — a string compare against ``"windows"`` alone silently skipped the
# real helper and let an under-capable or swapped helper through.
WINDOWS_SANDBOX_BACKENDS: frozenset[str] = frozenset({"windows", "windows_sandbox_account"})


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Capabilities returned by one verified helper handshake."""

    backend: str
    filesystem_sandbox: bool
    process_tree_cleanup: bool
    managed_network: bool
    system_bwrap: bool = False
    bundled_bwrap: bool = False
    wsl_version: int | None = None
    local_binding_control: bool = False
    explicit_handle_inheritance: bool = False
    windows_restricted_token: bool = False
    windows_acl: bool = False
    windows_job: bool = False
    windows_wfp: bool = False

    @property
    def is_windows_backend(self) -> bool:
        """True when the helper reports any Windows sandbox backend variant."""
        return self.backend in WINDOWS_SANDBOX_BACKENDS

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> RuntimeCapabilities:
        return cls(
            backend=str(value.get("backend", "unavailable")),
            filesystem_sandbox=value.get("filesystem_sandbox") is True,
            process_tree_cleanup=value.get("process_tree_cleanup") is True,
            managed_network=value.get("managed_network") is True,
            system_bwrap=value.get("system_bwrap") is True,
            bundled_bwrap=value.get("bundled_bwrap") is True,
            wsl_version=_optional_int(value.get("wsl_version")),
            local_binding_control=value.get("local_binding_control") is True,
            explicit_handle_inheritance=value.get("explicit_handle_inheritance") is True,
            windows_restricted_token=value.get("windows_restricted_token") is True,
            windows_acl=value.get("windows_acl") is True,
            windows_job=value.get("windows_job") is True,
            windows_wfp=value.get("windows_wfp") is True,
        )


class ShellVerdict(StrEnum):
    """Host-consumed shell classification; unknown values never auto-allow."""

    ALLOW_READ_ONLY = "allow_read_only"
    ASK = "ask"


@dataclass(frozen=True)
class ShellClassification:
    shell_kind: str
    raw_command: str
    parsed_commands: tuple[tuple[str, ...], ...]
    canonical_digest: str
    verdict: ShellVerdict
    reason: str


@dataclass(frozen=True)
class RuntimeCommandResult:
    """Captured command result returned only by the sandbox helper."""

    exit_code: int
    stdout: str
    stderr: str
    capabilities: RuntimeCapabilities


class NativeInteractiveSession:
    """One authenticated, managed child process with bidirectional stdio."""

    def __init__(
        self,
        client: "NativeRuntimeClient",
        process: asyncio.subprocess.Process,
        *,
        open_nonce: str,
        stderr_task: asyncio.Task[bytes],
        timeout: float,
        max_output_bytes: int,
    ) -> None:
        self._client = client
        self.process = process
        self._open_nonce = open_nonce
        self._stderr_task = stderr_task
        self._timeout = max(0.1, float(timeout or 0.0))
        self._max_output_bytes = max_output_bytes
        self._next_seq = 0
        self._output_bytes = 0
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._completed = False
        self.stderr_lines: list[str] = []

    async def write(self, data: bytes) -> None:
        if self._closed or self._completed:
            raise NativeRuntimeError(RuntimeErrorCode.RUNTIME_CRASHED, "native interactive session is closed")
        if not isinstance(data, bytes) or len(data) > _MAX_STDIN_BYTES:
            raise ValueError("native interactive stdin exceeds the size limit")
        await self._send_request({
            "op": "interactive_write",
            "data_b64": base64.b64encode(data).decode("ascii"),
        })

    async def close_child_stdin(self) -> None:
        if self._closed or self._completed:
            return
        await self._send_request({"op": "interactive_close"})

    async def read_chunk(self) -> bytes | None:
        if self._completed:
            return None
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            frame = await self._client._read_frame(
                self.process,
                _remaining(deadline),
            )
            version = frame.get("version")
            nonce = str(frame.get("nonce") or "")
            seq = frame.get("seq")
            if version != RUNTIME_PROTOCOL_VERSION or seq != self._next_seq:
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "native interactive response mismatch",
                )
            self._next_seq += 1
            frame_type = frame.get("type")
            if frame_type == "started" and nonce == self._open_nonce:
                capabilities = RuntimeCapabilities.from_payload(
                    frame.get("capabilities") if isinstance(frame.get("capabilities"), dict) else {}
                )
                if not capabilities.filesystem_sandbox or not capabilities.process_tree_cleanup:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                        "native runtime lacks required managed capabilities",
                    )
                continue
            if frame_type == "stdout" and nonce == self._open_nonce:
                try:
                    chunk = base64.b64decode(frame.get("data_b64"), validate=True)
                except (binascii.Error, TypeError, ValueError) as exc:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "invalid native interactive stdout encoding",
                    ) from exc
                if (
                    len(chunk) > _MAX_OUTPUT_CHUNK
                    or self._output_bytes + len(chunk) > self._max_output_bytes
                ):
                    raise NativeRuntimeError(
                        RuntimeErrorCode.OUTPUT_TRUNCATED,
                        "native interactive output exceeds the configured limit",
                    )
                self._output_bytes += len(chunk)
                return chunk
            if frame_type == "stderr" and nonce == self._open_nonce:
                try:
                    chunk = base64.b64decode(frame.get("data_b64"), validate=True)
                except (binascii.Error, TypeError, ValueError) as exc:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "invalid native interactive stderr encoding",
                    ) from exc
                if len(chunk) <= _MAX_OUTPUT_CHUNK:
                    text = chunk.decode("utf-8", errors="replace").strip()
                    if text:
                        self.stderr_lines.append(text)
                continue
            if frame_type == "completed" and nonce == self._open_nonce:
                self._completed = True
                return None
            if frame_type == "error":
                raise NativeRuntimeError(
                    _runtime_error_code(frame.get("code")),
                    str(frame.get("message") or "native interactive runtime failed"),
                )
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "invalid native interactive event",
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.stdin and not self.process.stdin.is_closing():
            self.process.stdin.close()
        await self._finish_helper()

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client._terminate_tree(self.process)
        await self._finish_helper()

    async def _send_request(self, payload: dict[str, Any], *, nonce: str | None = None) -> None:
        if not self.process.stdin or self.process.stdin.is_closing():
            raise NativeRuntimeError(RuntimeErrorCode.RUNTIME_CRASHED, "native interactive stdin is closed")
        token = self._client._startup_token
        nonce = nonce or secrets.token_urlsafe(24)
        request = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "token": token,
            "nonce": nonce,
            "request": payload,
        }
        frame = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(frame) > _MAX_REQUEST_FRAME:
            raise ValueError("native interactive request exceeds the size limit")
        async with self._write_lock:
            try:
                self.process.stdin.write(frame)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_CRASHED,
                    "native interactive runtime terminated unexpectedly",
                ) from exc

    async def _finish_helper(self) -> None:
        if self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                await self._client._terminate_tree(self.process)
        await self._stderr_task


class NativeRuntimeClient:
    """Launch a fresh authenticated helper and exchange one NDJSON request."""

    def __init__(self, helper_argv: Sequence[str | Path], *, startup_timeout: float = 5.0) -> None:
        argv = tuple(str(part) for part in helper_argv)
        if not argv:
            raise ValueError("helper_argv cannot be empty")
        self._helper_argv = argv
        self._startup_timeout = startup_timeout
        self._startup_token = ""

    async def classify_shell(
        self,
        *,
        shell_kind: str,
        executable: str,
        raw_command: str,
        timeout: float = 5.0,
    ) -> ShellClassification:
        """Classify without executing; helper failures conservatively become ASK."""
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        request = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "token": token,
            "nonce": nonce,
            "request": {
                "op": "classify_shell",
                "shell_kind": str(shell_kind),
                "executable": str(executable),
                "raw_command": str(raw_command),
            },
        }
        frame = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(frame) > _MAX_REQUEST_FRAME:
            return _ask_classification(shell_kind, raw_command, "classification_request_too_large")
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            process = await self._spawn(token)
            stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
            self._validate_ready(
                await self._read_frame(process, min(self._startup_timeout, _remaining(deadline)))
            )
            assert process.stdin is not None
            process.stdin.write(frame)
            await process.stdin.drain()
            process.stdin.close()
            result = await self._read_frame(process, _remaining(deadline))
            if (
                result.get("type") != "classified"
                or result.get("version") != RUNTIME_PROTOCOL_VERSION
                or result.get("nonce") != nonce
                or result.get("seq") != 0
            ):
                return _ask_classification(shell_kind, raw_command, "classification_protocol_mismatch")
            await self._validate_protocol_eof(process, deadline)
            await asyncio.wait_for(process.wait(), timeout=_remaining(deadline))
            return _parse_classification(result.get("classification"), shell_kind, raw_command)
        except (asyncio.TimeoutError, NativeRuntimeError, OSError):
            if process is not None:
                await self._terminate_tree(process)
            return _ask_classification(shell_kind, raw_command, "classifier_unavailable")
        finally:
            if stderr_task is not None:
                await stderr_task

    async def execute(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        writable_roots: Sequence[Path] = (),
        readable_roots: Sequence[Path] = (),
        denied_roots: Sequence[Path] = (),
        network_enabled: bool = False,
        network_rules: Sequence[Mapping[str, Any]] = (),
        allow_local_binding: bool = False,
        timeout: float = 30.0,
        max_output_bytes: int = 2 * 1024 * 1024,
        stdin: bytes | None = None,
        home_files: Mapping[str, bytes] | None = None,
        env_overrides: Mapping[str, str] | None = None,
        on_started: Callable[[int | None], None] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
    ) -> RuntimeCommandResult:
        """Execute a command through the helper; never fall back to a host spawn."""
        if not command:
            raise ValueError("command cannot be empty")
        validated_env, encoded_home_files = _validate_request_inputs(
            stdin, env_overrides, home_files
        )
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        payload = {
            "op": "run",
            "command": list(command),
            "cwd": str(cwd.resolve(strict=True)),
            "writable_roots": [str(path.resolve(strict=True)) for path in writable_roots],
            "readable_roots": [str(path.resolve(strict=True)) for path in readable_roots],
            "denied_roots": [str(path.resolve(strict=False)) for path in denied_roots],
            "network_enabled": network_enabled,
            "network_rules": [dict(rule) for rule in network_rules],
            "allow_local_binding": allow_local_binding,
            "max_output_bytes": max_output_bytes,
            "env_overrides": validated_env,
            "home_files": encoded_home_files,
        }
        if stdin is not None:
            payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")
        request = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "token": token,
            "nonce": nonce,
            "request": payload,
        }
        request_frame = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(request_frame) > _MAX_REQUEST_FRAME:
            raise ValueError("native runtime request exceeds the size limit")

        process = await self._spawn(token)
        stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            ready = await self._read_frame(
                process,
                min(self._startup_timeout, _remaining(deadline)),
            )
            self._validate_ready(ready)
            assert process.stdin is not None
            process.stdin.write(request_frame)
            await process.stdin.drain()
            process.stdin.close()
            result = await self._collect_result(
                process,
                nonce=nonce,
                deadline=deadline,
                max_output_bytes=max_output_bytes,
                network_enabled=network_enabled,
                allow_local_binding=allow_local_binding,
                on_started=on_started,
                on_output=on_output,
            )
            await asyncio.wait_for(process.wait(), timeout=_remaining(deadline))
        except asyncio.TimeoutError as exc:
            await self._terminate_tree(process)
            raise NativeRuntimeError(RuntimeErrorCode.TIMEOUT, "native runtime timed out") from exc
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise
        except NativeRuntimeError:
            await self._terminate_tree(process)
            raise
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            await self._terminate_tree(process)
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED, "native runtime terminated unexpectedly"
            ) from exc
        finally:
            await stderr_task

        return result

    async def open_interactive(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        writable_roots: Sequence[Path] = (),
        readable_roots: Sequence[Path] = (),
        denied_roots: Sequence[Path] = (),
        network_enabled: bool = False,
        network_rules: Sequence[Mapping[str, Any]] = (),
        allow_local_binding: bool = False,
        timeout: float = 120.0,
        max_output_bytes: int = 64 * 1024 * 1024,
        home_files: Mapping[str, bytes] | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> NativeInteractiveSession:
        """Open one managed child whose stdin/stdout remain bidirectional."""
        if not command:
            raise ValueError("command cannot be empty")
        validated_env, encoded_home_files = _validate_request_inputs(
            None, env_overrides, home_files
        )
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        payload = {
            "op": "interactive_open",
            "command": list(command),
            "cwd": str(cwd.resolve(strict=True)),
            "writable_roots": [str(path.resolve(strict=True)) for path in writable_roots],
            "readable_roots": [str(path.resolve(strict=True)) for path in readable_roots],
            "denied_roots": [str(path.resolve(strict=False)) for path in denied_roots],
            "network_enabled": network_enabled,
            "network_rules": [dict(rule) for rule in network_rules],
            "allow_local_binding": allow_local_binding,
            "max_output_bytes": max_output_bytes,
            "env_overrides": validated_env,
            "home_files": encoded_home_files,
        }
        request_frame = json.dumps(
            {
                "version": RUNTIME_PROTOCOL_VERSION,
                "token": token,
                "nonce": nonce,
                "request": payload,
            },
            separators=(",", ":"),
        ).encode() + b"\n"
        if len(request_frame) > _MAX_REQUEST_FRAME:
            raise ValueError("native interactive request exceeds the size limit")

        process = await self._spawn(token)
        stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
        deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout or 0.0))
        try:
            ready = await self._read_frame(
                process,
                min(self._startup_timeout, _remaining(deadline)),
            )
            self._validate_ready(
                ready,
                required_capabilities=_INTERACTIVE_READY_CAPABILITIES,
            )
            session = NativeInteractiveSession(
                self,
                process,
                open_nonce=nonce,
                stderr_task=stderr_task,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
            assert process.stdin is not None
            process.stdin.write(request_frame)
            await process.stdin.drain()
            return session
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            await stderr_task
            raise
        except Exception:
            await self._terminate_tree(process)
            await stderr_task
            raise

    async def _collect_result(
        self,
        process: asyncio.subprocess.Process,
        *,
        nonce: str,
        deadline: float,
        max_output_bytes: int,
        network_enabled: bool,
        allow_local_binding: bool,
        on_started: Callable[[int | None], None] | None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None,
    ) -> RuntimeCommandResult:
        stdout = bytearray()
        stderr = bytearray()
        active_streams: set[str] = set()
        capabilities: RuntimeCapabilities | None = None
        expected_seq = 0
        while True:
            try:
                frame = await self._read_frame(process, _remaining(deadline))
            except NativeRuntimeError as exc:
                if (
                    exc.code is RuntimeErrorCode.RUNTIME_CRASHED
                    and capabilities is not None
                ):
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "native runtime closed before a terminal event",
                    ) from exc
                raise
            if (
                frame.get("version") != RUNTIME_PROTOCOL_VERSION
                or frame.get("nonce") != nonce
                or frame.get("seq") != expected_seq
            ):
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "native runtime response mismatch",
                )
            expected_seq += 1
            frame_type = frame.get("type")
            if frame_type == "error":
                code = _runtime_error_code(frame.get("code"))
                await self._validate_protocol_eof(process, deadline)
                raise NativeRuntimeError(code, str(frame.get("message") or code.value))
            if capabilities is None:
                if frame_type != "started":
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "native runtime output arrived before start",
                    )
                payload = frame.get("capabilities")
                capabilities = RuntimeCapabilities.from_payload(
                    payload if isinstance(payload, dict) else {}
                )
                if not capabilities.filesystem_sandbox or not capabilities.process_tree_cleanup:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                        "native runtime lacks required managed capabilities",
                    )
                if capabilities.is_windows_backend and not all(
                    (
                        capabilities.windows_restricted_token,
                        capabilities.windows_acl,
                        capabilities.windows_job,
                        capabilities.explicit_handle_inheritance,
                    )
                ):
                    raise NativeRuntimeError(
                        RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                        "Windows runtime lacks required token/ACL/Job/handle capabilities",
                    )
                if network_enabled and (
                    not capabilities.managed_network
                    or (capabilities.is_windows_backend and not capabilities.windows_wfp)
                ):
                    raise NativeRuntimeError(
                        RuntimeErrorCode.NETWORK_UNAVAILABLE,
                        "native runtime lacks required managed-network capability",
                    )
                if allow_local_binding and not capabilities.local_binding_control:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.NETWORK_UNAVAILABLE,
                        "native runtime lacks local-binding control",
                    )
                _safe_callback(on_started, _optional_int(frame.get("pid")))
                continue
            if frame_type in {"stdout", "stderr"}:
                try:
                    chunk = base64.b64decode(frame.get("data_b64"), validate=True)
                except (binascii.Error, TypeError, ValueError) as exc:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "invalid native runtime output encoding",
                    ) from exc
                if len(chunk) > _MAX_OUTPUT_CHUNK or len(stdout) + len(stderr) + len(chunk) > max_output_bytes:
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "native runtime output exceeds configured limits",
                    )
                (stdout if frame_type == "stdout" else stderr).extend(chunk)
                if frame_type not in active_streams:
                    active_streams.add(frame_type)
                    _safe_callback(on_output, frame_type)
                continue
            if frame_type == "completed":
                exit_code = frame.get("exit_code")
                if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                    raise NativeRuntimeError(
                        RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                        "invalid native runtime completion event",
                    )
                result = RuntimeCommandResult(
                    exit_code=exit_code,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                    capabilities=capabilities,
                )
                await self._validate_protocol_eof(process, deadline)
                return result
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "invalid native runtime event type",
            )

    async def _spawn(self, token: str) -> asyncio.subprocess.Process:
        # Verify helper integrity before spawn: a binary swapped in the install dir
        # (or pointed at by ACE_SECURITY_RUNTIME) that no longer matches its
        # manifest digest must fail-closed, not execute. A missing manifest in
        # an unbuilt source tree is allowed; a present manifest is strict.
        try:
            from crew.security.launch import verify_helper_integrity
            verify_helper_integrity(self._helper_argv[0])
        except NativeRuntimeError:
            raise
        except Exception as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                f"native security runtime integrity check failed: {exc}",
            ) from exc
        env = dict(os.environ)
        env["ACE_SECURITY_RUNTIME_TOKEN"] = token
        self._startup_token = token
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return await asyncio.create_subprocess_exec(
                *self._helper_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_MAX_PROTOCOL_FRAME + 1,
                **kwargs,
            )
        except (FileNotFoundError, OSError) as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_UNAVAILABLE, "native security runtime is unavailable"
            ) from exc

    @staticmethod
    async def _drain_helper_stderr(process: asyncio.subprocess.Process) -> bytes:
        """Drain helper diagnostics without allowing pipe deadlock or unbounded memory."""
        if process.stderr is None:
            return b""
        captured = bytearray()
        while True:
            chunk = await process.stderr.read(16 * 1024)
            if not chunk:
                return bytes(captured)
            if len(captured) < _MAX_HELPER_STDERR:
                captured.extend(chunk[: _MAX_HELPER_STDERR - len(captured)])

    @staticmethod
    async def _read_frame(process: asyncio.subprocess.Process, timeout: float) -> dict[str, Any]:
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except ValueError as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "native runtime frame is too large",
            ) from exc
        if not line:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED, "native runtime closed the protocol stream"
            )
        if len(line) > _MAX_PROTOCOL_FRAME:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH, "native runtime frame is too large"
            )
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH, "invalid native runtime frame"
            ) from exc
        if not isinstance(value, dict):
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH, "invalid native runtime frame type"
            )
        return value

    @staticmethod
    async def _validate_protocol_eof(
        process: asyncio.subprocess.Process,
        deadline: float,
    ) -> None:
        assert process.stdout is not None
        try:
            extra = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=_remaining(deadline),
            )
        except ValueError as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "native runtime frame is too large",
            ) from exc
        if extra:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "native runtime emitted data after the terminal event",
            )

    @staticmethod
    def _validate_ready(
        frame: Mapping[str, Any],
        *,
        required_capabilities: frozenset[str] = _REQUIRED_READY_CAPABILITIES,
    ) -> None:
        capabilities = frame.get("capabilities")
        if (
            frame.get("type") != "ready"
            or frame.get("version") != RUNTIME_PROTOCOL_VERSION
            or not isinstance(capabilities, list)
            or not required_capabilities.issubset(capabilities)
        ):
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH, "native runtime handshake mismatch"
            )

    @staticmethod
    async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


def _ask_classification(shell_kind: str, raw_command: str, reason: str) -> ShellClassification:
    return ShellClassification(
        shell_kind=str(shell_kind),
        raw_command=str(raw_command),
        parsed_commands=(),
        canonical_digest="",
        verdict=ShellVerdict.ASK,
        reason=reason,
    )


def _parse_classification(
    value: Any,
    shell_kind: str,
    raw_command: str,
) -> ShellClassification:
    if not isinstance(value, dict):
        return _ask_classification(shell_kind, raw_command, "invalid_classification")
    commands = value.get("parsed_commands")
    if not isinstance(commands, list) or not all(
        isinstance(command, list)
        and command
        and all(isinstance(token, str) and token for token in command)
        for command in commands
    ):
        commands = []
    try:
        verdict = ShellVerdict(str(value.get("verdict")))
    except ValueError:
        verdict = ShellVerdict.ASK
    digest = str(value.get("canonical_digest") or "")
    if verdict is ShellVerdict.ALLOW_READ_ONLY and (not commands or len(digest) != 64):
        verdict = ShellVerdict.ASK
    return ShellClassification(
        shell_kind=str(value.get("shell_kind") or shell_kind),
        raw_command=str(value.get("raw_command") or raw_command),
        parsed_commands=tuple(tuple(command) for command in commands),
        canonical_digest=digest,
        verdict=verdict,
        reason=str(value.get("reason") or "invalid_classification"),
    )


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _validate_request_inputs(
    stdin: bytes | None,
    env_overrides: Mapping[str, str] | None,
    home_files: Mapping[str, bytes] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    if stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > _MAX_STDIN_BYTES):
        raise ValueError("native runtime stdin exceeds the size limit")

    validated = dict(env_overrides or {})
    encoded_size = 0
    for name, value in validated.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("native runtime environment must contain strings")
        normalized_name = name.upper()
        if (
            not _ENV_NAME_PATTERN.fullmatch(name)
            or "\x00" in value
            or normalized_name in _RESERVED_ENV_NAMES
            or normalized_name.startswith(_RESERVED_ENV_PREFIXES)
        ):
            raise ValueError("native runtime environment contains a disallowed entry")
        encoded_size += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if encoded_size > _MAX_ENV_BYTES:
            raise ValueError("native runtime environment exceeds the size limit")
    encoded_home_files: dict[str, str] = {}
    total_home_bytes = 0
    for relative_path, content in dict(home_files or {}).items():
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or relative_path.startswith(("/", "\\"))
            or ":" in relative_path
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise ValueError("native runtime projected HOME path must be relative")
        if not isinstance(content, bytes) or len(content) > _MAX_HOME_FILE_BYTES:
            raise ValueError("native runtime projected HOME file exceeds the size limit")
        if len(encoded_home_files) >= _MAX_HOME_FILES:
            raise ValueError("native runtime projected HOME has too many files")
        total_home_bytes += len(content)
        if total_home_bytes > _MAX_HOME_TOTAL_BYTES:
            raise ValueError("native runtime projected HOME exceeds the size limit")
        encoded_home_files[relative_path] = base64.b64encode(content).decode("ascii")
    return validated, encoded_home_files


def _safe_callback(callback: Callable[[Any], None] | None, value: Any) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:
        _LOGGER.warning("native runtime activity callback failed")


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining


def _runtime_error_code(value: Any) -> RuntimeErrorCode:
    try:
        return RuntimeErrorCode(str(value))
    except ValueError:
        return RuntimeErrorCode.SANDBOX_DENIED
