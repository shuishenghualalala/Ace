"""Versioned client for the per-task native security runtime."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from crew.security.process_lifecycle import isolated_process_kwargs, terminate_process_tree
from crew.security.snapshot import SignedAuthorizationSnapshot

RUNTIME_PROTOCOL_VERSION = 2
_MAX_REQUEST_FRAME = 2 * 1024 * 1024
_MAX_PROTOCOL_FRAME = 128 * 1024
_MAX_OUTPUT_CHUNK = 64 * 1024
_MAX_STDIN_BYTES = 1024 * 1024
_MAX_STDIO_INPUT_BYTES = 16 * 1024 * 1024
_MAX_STDIO_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_ENV_BYTES = 256 * 1024
_MAX_HELPER_STDERR = 64 * 1024
_MAX_RUNTIME_TIMEOUT_SECONDS = 24 * 60 * 60
_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_ENV_NAMES = frozenset(
    {
        "ACE_SANDBOX",
        "ALL_PROXY",
        "BASH_ENV",
        "COMSPEC",
        "ENV",
        "GIT_CONFIG_GLOBAL",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NODE_OPTIONS",
        "NO_PROXY",
        "PATH",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)
_RESERVED_ENV_PREFIXES = ("ACE_SECURITY_", "ACE_BUNDLED_", "DYLD_", "LD_")
_REQUIRED_READY_CAPABILITIES = frozenset(
    {"deny_read_glob_v1", "stdin_once", "stream_output"}
)
_DUPLEX_STDIO_CAPABILITY = "duplex_stdio_v1"
_STDIO_MAC_CONTEXT = b"ace-runtime-stdio-v1\x00"
_LOGGER = logging.getLogger(__name__)


class RuntimeErrorCode(StrEnum):
    """Stable errors exposed by the native-runtime boundary."""

    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    RUNTIME_PROTOCOL_MISMATCH = "runtime_protocol_mismatch"
    RUNTIME_CRASHED = "runtime_crashed"
    SANDBOX_DENIED = "sandbox_denied"
    NETWORK_UNAVAILABLE = "network_unavailable"
    PROCESS_LIMIT_REACHED = "process_limit_reached"
    TIMEOUT = "timeout"
    OUTPUT_TRUNCATED = "output_truncated"


class NativeRuntimeError(RuntimeError):
    """A fail-closed native-runtime failure with a stable public code."""

    def __init__(self, code: RuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


_RUNTIME_DIAGNOSTIC_AUDITOR: Callable[..., None] | None = None


def set_runtime_diagnostic_auditor(auditor: Callable[..., None] | None) -> None:
    """Install the host-side durable sink for sanitized runtime diagnostics."""
    global _RUNTIME_DIAGNOSTIC_AUDITOR
    _RUNTIME_DIAGNOSTIC_AUDITOR = auditor if callable(auditor) else None


def _emit_runtime_diagnostic(
    *,
    status: str,
    backend: str = "",
    version: str = "",
    manifest_digest: str = "",
    capabilities: Sequence[str] = (),
    failure_code: str = "",
    failure_detail: str = "",
) -> None:
    """Forward one diagnostic to the durable audit sink without corrupting runtime flow."""
    auditor = _RUNTIME_DIAGNOSTIC_AUDITOR
    if auditor is None:
        return
    try:
        auditor(
            status=str(status),
            backend=str(backend),
            version=str(version),
            manifest_digest=str(manifest_digest),
            capabilities=tuple(str(item) for item in capabilities),
            failure_code=str(failure_code),
            failure_detail=str(failure_detail),
        )
    except Exception:  # noqa: BLE001 - host sink owns its fail-closed logging
        return


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
    executable: str = ""
    executable_digest: str = ""
    command_identities: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RuntimeCommandResult:
    """Captured command result returned only by the sandbox helper."""

    exit_code: int
    stdout: str
    stderr: str
    capabilities: RuntimeCapabilities


class NativeRuntimeStdioProcess:
    """Authenticated full-duplex byte stream owned by one native helper."""

    def __init__(
        self,
        *,
        client: NativeRuntimeClient,
        process: asyncio.subprocess.Process,
        stderr_task: asyncio.Task[bytes],
        token: str,
        nonce: str,
        deadline: float,
        inactivity_timeout: float | None,
        max_input_bytes: int,
        max_output_bytes: int,
        capabilities: RuntimeCapabilities,
    ) -> None:
        self._client = client
        self._process = process
        self._stderr_task = stderr_task
        self._token = token
        self._nonce = nonce
        self._deadline = deadline
        self._inactivity_timeout = inactivity_timeout
        self._last_activity = asyncio.get_running_loop().time()
        self._max_input_bytes = max_input_bytes
        self._max_output_bytes = max_output_bytes
        self._input_bytes = 0
        self._output_bytes = 0
        self._input_seq = 0
        self._output_seq = 1
        self._write_lock = asyncio.Lock()
        self._stdin_closed = False
        self._terminal = False
        self._terminated = False
        self.capabilities = capabilities

    async def send(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not data:
            raise ValueError("native stdio input frame must contain bytes")
        if len(data) > _MAX_STDIN_BYTES:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native stdio input frame exceeds the size limit",
            )
        async with self._write_lock:
            if self._stdin_closed or self._terminal or self._terminated:
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_CRASHED,
                    "native stdio input is closed",
                )
            if self._input_bytes + len(data) > self._max_input_bytes:
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_DENIED,
                    "native stdio input exceeds the configured limit",
                )
            encoded = base64.b64encode(data).decode("ascii")
            await self._write_input_frame("stdin", encoded)
            self._input_bytes += len(data)
            self._last_activity = asyncio.get_running_loop().time()

    async def close_stdin(self) -> None:
        async with self._write_lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            if self._terminal or self._terminated:
                return
            try:
                await self._write_input_frame("stdin_close", "")
            finally:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                self._last_activity = asyncio.get_running_loop().time()

    async def receive(self) -> tuple[str, bytes | int]:
        if self._terminal or self._terminated:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED,
                "native stdio output is closed",
            )
        try:
            frame = await self._client._read_frame(
                self._process,
                _activity_timeout(
                    self._deadline,
                    self._last_activity,
                    self._inactivity_timeout,
                ),
            )
        except asyncio.CancelledError:
            await self.terminate()
            raise
        except TimeoutError as exc:
            await self.terminate()
            raise NativeRuntimeError(
                RuntimeErrorCode.TIMEOUT,
                "native stdio runtime timed out",
            ) from exc
        self._last_activity = asyncio.get_running_loop().time()
        if (
            frame.get("version") != RUNTIME_PROTOCOL_VERSION
            or frame.get("nonce") != self._nonce
            or frame.get("seq") != self._output_seq
        ):
            await self.terminate()
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                "native stdio response mismatch",
            )
        self._output_seq += 1
        frame_type = frame.get("type")
        if frame_type in {"stdout", "stderr"}:
            try:
                chunk = base64.b64decode(frame.get("data_b64"), validate=True)
            except (binascii.Error, TypeError, ValueError) as exc:
                await self.terminate()
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "invalid native stdio output encoding",
                ) from exc
            if (
                len(chunk) > _MAX_OUTPUT_CHUNK
                or self._output_bytes + len(chunk) > self._max_output_bytes
            ):
                await self.terminate()
                raise NativeRuntimeError(
                    RuntimeErrorCode.OUTPUT_TRUNCATED,
                    "native stdio output exceeds the configured limit",
                )
            self._output_bytes += len(chunk)
            return str(frame_type), chunk
        if frame_type == "error":
            self._terminal = True
            code = _runtime_error_code(frame.get("code"))
            await self._finish_helper()
            raise NativeRuntimeError(code, str(frame.get("message") or code.value))
        if frame_type == "completed":
            exit_code = frame.get("exit_code")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                await self.terminate()
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "invalid native stdio completion event",
                )
            self._terminal = True
            await self._finish_helper()
            return "completed", exit_code
        await self.terminate()
        raise NativeRuntimeError(
            RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
            "invalid native stdio event type",
        )

    async def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        if self._process.stdin is not None:
            self._process.stdin.close()
        await self._client._terminate_tree(self._process)
        await self._stderr_task

    async def _write_input_frame(self, frame_type: str, data_b64: str) -> None:
        seq = self._input_seq
        canonical = (
            _STDIO_MAC_CONTEXT
            + self._nonce.encode()
            + b"\x00"
            + str(seq).encode("ascii")
            + b"\x00"
            + frame_type.encode("ascii")
            + b"\x00"
            + data_b64.encode("ascii")
        )
        frame = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "nonce": self._nonce,
            "seq": seq,
            "type": frame_type,
            "data_b64": data_b64,
            "mac": hmac.new(self._token.encode(), canonical, hashlib.sha256).hexdigest(),
        }
        encoded = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > _MAX_REQUEST_FRAME:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native stdio protocol frame exceeds the size limit",
            )
        if self._process.stdin is None:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED,
                "native stdio helper input is unavailable",
            )
        try:
            self._process.stdin.write(encoded)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED,
                "native stdio helper terminated unexpectedly",
            ) from exc
        self._input_seq += 1

    async def _finish_helper(self) -> None:
        try:
            await self._client._validate_protocol_eof(self._process, self._deadline)
            await asyncio.wait_for(
                self._process.wait(),
                timeout=_remaining(self._deadline),
            )
        except (TimeoutError, NativeRuntimeError, OSError):
            await self._client._terminate_tree(self._process)
            raise
        finally:
            await self._stderr_task


class NativeRuntimeClient:
    """Launch a fresh authenticated helper and exchange one NDJSON request."""

    def __init__(self, helper_argv: Sequence[str | Path], *, startup_timeout: float = 5.0) -> None:
        argv = tuple(str(part) for part in helper_argv)
        if not argv:
            raise ValueError("helper_argv cannot be empty")
        self._helper_argv = argv
        self._startup_timeout = _validate_timeout(
            startup_timeout,
            "native runtime startup timeout",
        )

    async def classify_shell(
        self,
        *,
        shell_kind: str,
        executable: str,
        raw_command: str,
        timeout: float = 5.0,
    ) -> ShellClassification:
        """Classify without executing; helper failures conservatively become ASK."""
        timeout = _validate_timeout(timeout, "classification timeout")
        executable_path, executable_digest = _executable_identity(executable)
        if not executable_path or not executable_digest:
            return _ask_classification(
                shell_kind,
                raw_command,
                "executable_identity_unavailable",
            )
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        request = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "token": token,
            "nonce": nonce,
            "request": {
                "op": "classify_shell",
                "shell_kind": str(shell_kind),
                "executable": executable_path,
                "raw_command": str(raw_command),
            },
        }
        frame = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(frame) > _MAX_REQUEST_FRAME:
            return _ask_classification(
                shell_kind,
                raw_command,
                "classification_request_too_large",
                executable=executable_path,
                executable_digest=executable_digest,
            )
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            process = await self._spawn(token)
            stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
            ready = await self._read_frame(
                process,
                min(self._startup_timeout, _remaining(deadline)),
            )
            self._validate_ready(ready)
            ready_capabilities = ready.get("capabilities")
            _emit_runtime_diagnostic(
                status="ready",
                version=str(RUNTIME_PROTOCOL_VERSION),
                capabilities=(
                    tuple(str(item) for item in ready_capabilities)
                    if isinstance(ready_capabilities, list)
                    else ()
                ),
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
                return _ask_classification(
                    shell_kind,
                    raw_command,
                    "classification_protocol_mismatch",
                    executable=executable_path,
                    executable_digest=executable_digest,
                )
            await self._validate_protocol_eof(process, deadline)
            await asyncio.wait_for(process.wait(), timeout=_remaining(deadline))
            parsed = _parse_classification(
                result.get("classification"),
                shell_kind,
                raw_command,
                executable=executable_path,
                executable_digest=executable_digest,
            )
            return replace(
                parsed,
                command_identities=_command_identities(parsed.parsed_commands),
            )
        except (TimeoutError, NativeRuntimeError, OSError, RuntimeError, ValueError) as exc:
            if process is not None:
                await self._terminate_tree(process)
            code = getattr(exc, "code", None)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=(
                    str(code) if code is not None else "classifier_unavailable"
                ),
                failure_detail=str(exc)[:256],
            )
            return _ask_classification(
                shell_kind,
                raw_command,
                "classifier_unavailable",
                executable=executable_path,
                executable_digest=executable_digest,
            )
        finally:
            if stderr_task is not None:
                await stderr_task

    async def execute_authorized(
        self,
        *,
        authorization: SignedAuthorizationSnapshot,
        env_overrides: Mapping[str, str],
        timeout: float = 30.0,
        inactivity_timeout: float | None = None,
        max_output_bytes: int = 2 * 1024 * 1024,
        stdin: bytes | None = None,
        on_started: Callable[[int | None], None] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
        verification_key: bytes | None = None,
    ) -> RuntimeCommandResult:
        """Execute only facts authenticated by one immutable authorization snapshot."""
        from crew.security.models import PermissionProfileKind
        from crew.security.snapshot import (
            AuthorizationSnapshotError,
            consume_authorization_snapshot,
        )

        if not isinstance(authorization, SignedAuthorizationSnapshot):
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native runtime authorization snapshot is missing",
            )
        timeout = _validate_timeout(timeout, "native runtime timeout")
        inactivity_timeout = _validate_optional_timeout(
            inactivity_timeout,
            "native runtime inactivity timeout",
        )
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 0 < max_output_bytes <= _MAX_STDIO_OUTPUT_BYTES
        ):
            raise ValueError("native runtime output budget is invalid")
        try:
            frozen_environment = dict(env_overrides)
            snapshot = consume_authorization_snapshot(
                authorization,
                environment=frozen_environment,
                verification_key=verification_key,
            )
            filesystem_globs = _filesystem_globs_from_profile_payload(
                snapshot.profile_payload
            )
        except (AuthorizationSnapshotError, TypeError, ValueError) as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                f"native runtime authorization snapshot rejected: {exc}",
            ) from exc
        if (
            not snapshot.sandboxed
            or snapshot.profile_kind != PermissionProfileKind.MANAGED.value
        ):
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native runtime requires a managed authorization snapshot",
            )
        if tuple(self._helper_argv) != snapshot.helper_argv:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native runtime helper differs from the authorization snapshot",
            )
        cwd = _authorized_canonical_path(snapshot.cwd, label="cwd", strict=True)
        writable = tuple(
            _authorized_canonical_path(value, label="writable root", strict=False)
            for value in snapshot.writable_roots
        )
        readable_roots = tuple(
            _authorized_canonical_path(value, label="readable root", strict=False)
            for value in snapshot.readable_roots
        )
        readable = tuple(
            value
            for value in readable_roots
            if not any(
                value == root or root in value.parents
                for root in writable
            )
        )
        denied = tuple(
            _authorized_canonical_path(value, label="denied root", strict=False)
            for value in snapshot.denied_roots
        )
        network_rules = tuple(rule.to_payload() for rule in snapshot.network_rules)
        return await self.execute(
            command=snapshot.argv,
            cwd=cwd,
            writable_roots=writable,
            readable_roots=readable,
            denied_roots=denied,
            filesystem_globs=filesystem_globs,
            network_enabled=bool(network_rules),
            network_rules=network_rules,
            allow_local_binding=snapshot.allow_local_binding,
            timeout=timeout,
            inactivity_timeout=inactivity_timeout,
            max_output_bytes=max_output_bytes,
            stdin=stdin,
            env_overrides=frozen_environment,
            on_started=on_started,
            on_output=on_output,
            _expected_helper_digest=snapshot.helper_digest,
            _use_exact_authorized_paths=True,
        )

    async def open_authorized_stdio(
        self,
        *,
        authorization: SignedAuthorizationSnapshot,
        env_overrides: Mapping[str, str],
        max_lifetime_seconds: float,
        max_input_bytes: int,
        max_output_bytes: int,
        inactivity_timeout_seconds: float | None = None,
        verification_key: bytes | None = None,
    ) -> NativeRuntimeStdioProcess:
        """Open one capability-negotiated duplex process without host fallback."""
        from crew.security.models import PermissionProfileKind
        from crew.security.snapshot import (
            AuthorizationSnapshotError,
            consume_authorization_snapshot,
        )

        if (
            not isinstance(max_input_bytes, int)
            or isinstance(max_input_bytes, bool)
            or not 0 < max_input_bytes <= _MAX_STDIO_INPUT_BYTES
            or not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 0 < max_output_bytes <= _MAX_STDIO_OUTPUT_BYTES
            or not isinstance(max_lifetime_seconds, (int, float))
            or isinstance(max_lifetime_seconds, bool)
            or not 0 < float(max_lifetime_seconds) <= 24 * 60 * 60
        ):
            raise ValueError("native stdio budgets are invalid")
        inactivity_timeout_seconds = _validate_optional_timeout(
            inactivity_timeout_seconds,
            "native stdio inactivity timeout",
        )
        if not isinstance(authorization, SignedAuthorizationSnapshot):
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native runtime authorization snapshot is missing",
            )
        try:
            frozen_environment = dict(env_overrides)
            snapshot = consume_authorization_snapshot(
                authorization,
                environment=frozen_environment,
                verification_key=verification_key,
            )
            filesystem_globs = _filesystem_globs_from_profile_payload(
                snapshot.profile_payload
            )
        except (AuthorizationSnapshotError, TypeError, ValueError) as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                f"native runtime authorization snapshot rejected: {exc}",
            ) from exc
        if (
            not snapshot.sandboxed
            or snapshot.profile_kind != PermissionProfileKind.MANAGED.value
        ):
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native stdio requires a managed authorization snapshot",
            )
        if tuple(self._helper_argv) != snapshot.helper_argv:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "native runtime helper differs from the authorization snapshot",
            )
        cwd = _authorized_canonical_path(snapshot.cwd, label="cwd", strict=True)
        writable = tuple(
            _authorized_canonical_path(value, label="writable root", strict=False)
            for value in snapshot.writable_roots
        )
        readable_roots = tuple(
            _authorized_canonical_path(value, label="readable root", strict=False)
            for value in snapshot.readable_roots
        )
        readable = tuple(
            value
            for value in readable_roots
            if not any(value == root or root in value.parents for root in writable)
        )
        denied = tuple(
            _authorized_canonical_path(value, label="denied root", strict=False)
            for value in snapshot.denied_roots
        )
        network_rules = tuple(rule.to_payload() for rule in snapshot.network_rules)
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        request = {
            "version": RUNTIME_PROTOCOL_VERSION,
            "token": token,
            "nonce": nonce,
            "request": {
                "op": "run_stdio",
                "command": list(snapshot.argv),
                "cwd": str(cwd),
                "writable_roots": [str(path) for path in writable],
                "readable_roots": [str(path) for path in readable],
                "denied_roots": [str(path) for path in denied],
                "filesystem_globs": _validate_filesystem_globs(
                    filesystem_globs,
                    exact_paths=True,
                ),
                "network_enabled": bool(network_rules),
                "network_rules": [dict(rule) for rule in network_rules],
                "allow_local_binding": snapshot.allow_local_binding,
                "max_output_bytes": max_output_bytes,
                "max_input_bytes": max_input_bytes,
                "env_overrides": _validate_request_inputs(
                    None,
                    frozen_environment,
                ),
            },
        }
        request_frame = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(request_frame) > _MAX_REQUEST_FRAME:
            raise ValueError("native stdio request exceeds the size limit")

        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        opened = False
        deadline = asyncio.get_running_loop().time() + float(max_lifetime_seconds)
        try:
            process = await self._spawn(
                token,
                expected_helper_digest=snapshot.helper_digest,
            )
            stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
            ready = await self._read_frame(
                process,
                min(self._startup_timeout, _remaining(deadline)),
            )
            self._validate_ready(ready)
            capabilities = ready.get("capabilities")
            if (
                not isinstance(capabilities, list)
                or _DUPLEX_STDIO_CAPABILITY not in capabilities
            ):
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                    "native runtime lacks authenticated duplex stdio",
                )
            assert process.stdin is not None
            process.stdin.write(request_frame)
            await process.stdin.drain()
            started = await self._read_frame(process, _remaining(deadline))
            if (
                started.get("version") != RUNTIME_PROTOCOL_VERSION
                or started.get("nonce") != nonce
                or started.get("seq") != 0
            ):
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "native stdio start response mismatch",
                )
            if started.get("type") == "error":
                raise NativeRuntimeError(
                    _runtime_error_code(started.get("code")),
                    str(started.get("message") or "native stdio start failed"),
                )
            if started.get("type") != "started":
                raise NativeRuntimeError(
                    RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH,
                    "native stdio output arrived before start",
                )
            payload = started.get("capabilities")
            runtime_capabilities = RuntimeCapabilities.from_payload(
                payload if isinstance(payload, dict) else {}
            )
            self._validate_started_capabilities(
                runtime_capabilities,
                network_enabled=bool(network_rules),
                allow_local_binding=snapshot.allow_local_binding,
            )
            _emit_runtime_diagnostic(
                status="started",
                backend=runtime_capabilities.backend,
                version=str(RUNTIME_PROTOCOL_VERSION),
                manifest_digest=str(snapshot.helper_digest or ""),
                capabilities=tuple(str(item) for item in capabilities or ()),
            )
            opened = True
            return NativeRuntimeStdioProcess(
                client=self,
                process=process,
                stderr_task=stderr_task,
                token=token,
                nonce=nonce,
                deadline=deadline,
                inactivity_timeout=inactivity_timeout_seconds,
                max_input_bytes=max_input_bytes,
                max_output_bytes=max_output_bytes,
                capabilities=runtime_capabilities,
            )
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate_tree(process)
            raise
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            if process is not None:
                await self._terminate_tree(process)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(RuntimeErrorCode.RUNTIME_CRASHED),
                failure_detail="native stdio runtime terminated unexpectedly",
                manifest_digest=str(snapshot.helper_digest or ""),
            )
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED,
                "native stdio runtime terminated unexpectedly",
            ) from exc
        except Exception as exc:
            if process is not None:
                await self._terminate_tree(process)
            code = getattr(exc, "code", None)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(code) if code is not None else str(RuntimeErrorCode.RUNTIME_CRASHED),
                failure_detail=str(exc)[:256],
                manifest_digest=str(snapshot.helper_digest or ""),
            )
            raise
        finally:
            if stderr_task is not None and not opened:
                await stderr_task

    async def execute(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        writable_roots: Sequence[Path] = (),
        readable_roots: Sequence[Path] = (),
        denied_roots: Sequence[Path] = (),
        filesystem_globs: Sequence[Mapping[str, Any]] = (),
        network_enabled: bool = False,
        network_rules: Sequence[Mapping[str, Any]] = (),
        allow_local_binding: bool = False,
        timeout: float = 30.0,
        inactivity_timeout: float | None = None,
        max_output_bytes: int = 2 * 1024 * 1024,
        stdin: bytes | None = None,
        env_overrides: Mapping[str, str] | None = None,
        on_started: Callable[[int | None], None] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
        _expected_helper_digest: str | None = None,
        _use_exact_authorized_paths: bool = False,
    ) -> RuntimeCommandResult:
        """Execute a command through the helper; never fall back to a host spawn."""
        if not command:
            raise ValueError("command cannot be empty")
        timeout = _validate_timeout(timeout, "native runtime timeout")
        inactivity_timeout = _validate_optional_timeout(
            inactivity_timeout,
            "native runtime inactivity timeout",
        )
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 0 < max_output_bytes <= _MAX_STDIO_OUTPUT_BYTES
        ):
            raise ValueError("native runtime output budget is invalid")
        validated_env = _validate_request_inputs(stdin, env_overrides)
        validated_filesystem_globs = _validate_filesystem_globs(
            filesystem_globs,
            exact_paths=_use_exact_authorized_paths,
        )
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        if _use_exact_authorized_paths:
            payload_cwd = str(cwd)
            payload_writable_roots = [str(path) for path in writable_roots]
            payload_readable_roots = [str(path) for path in readable_roots]
            payload_denied_roots = [str(path) for path in denied_roots]
        else:
            payload_cwd = str(cwd.resolve(strict=True))
            payload_writable_roots = [
                str(path.resolve(strict=True)) for path in writable_roots
            ]
            payload_readable_roots = [
                str(path.resolve(strict=True)) for path in readable_roots
            ]
            payload_denied_roots = [
                str(path.resolve(strict=False)) for path in denied_roots
            ]
        payload = {
            "op": "run",
            "command": list(command),
            "cwd": payload_cwd,
            "writable_roots": payload_writable_roots,
            "readable_roots": payload_readable_roots,
            "denied_roots": payload_denied_roots,
            "filesystem_globs": validated_filesystem_globs,
            "network_enabled": network_enabled,
            "network_rules": [dict(rule) for rule in network_rules],
            "allow_local_binding": allow_local_binding,
            "max_output_bytes": max_output_bytes,
            "env_overrides": validated_env,
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

        try:
            process = await self._spawn(
                token,
                expected_helper_digest=_expected_helper_digest,
            )
        except NativeRuntimeError as exc:
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(exc.code),
                failure_detail=str(exc)[:256],
                manifest_digest=str(_expected_helper_digest or ""),
            )
            raise
        stderr_task = asyncio.create_task(self._drain_helper_stderr(process))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            ready = await self._read_frame(
                process,
                min(self._startup_timeout, _remaining(deadline)),
            )
            self._validate_ready(ready)
            ready_capabilities = ready.get("capabilities")
            _emit_runtime_diagnostic(
                status="ready",
                version=str(RUNTIME_PROTOCOL_VERSION),
                manifest_digest=str(_expected_helper_digest or ""),
                capabilities=(
                    tuple(str(item) for item in ready_capabilities)
                    if isinstance(ready_capabilities, list)
                    else ()
                ),
            )
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
                inactivity_timeout=inactivity_timeout,
            )
            await asyncio.wait_for(process.wait(), timeout=_remaining(deadline))
        except TimeoutError as exc:
            await self._terminate_tree(process)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(RuntimeErrorCode.TIMEOUT),
                failure_detail="native runtime timed out",
                manifest_digest=str(_expected_helper_digest or ""),
            )
            raise NativeRuntimeError(RuntimeErrorCode.TIMEOUT, "native runtime timed out") from exc
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise
        except NativeRuntimeError as exc:
            await self._terminate_tree(process)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(exc.code),
                failure_detail=str(exc)[:256],
                manifest_digest=str(_expected_helper_digest or ""),
            )
            raise
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            await self._terminate_tree(process)
            _emit_runtime_diagnostic(
                status="failed",
                failure_code=str(RuntimeErrorCode.RUNTIME_CRASHED),
                failure_detail="native runtime terminated unexpectedly",
                manifest_digest=str(_expected_helper_digest or ""),
            )
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_CRASHED, "native runtime terminated unexpectedly"
            ) from exc
        finally:
            await stderr_task

        return result

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
        inactivity_timeout: float | None,
    ) -> RuntimeCommandResult:
        stdout = bytearray()
        stderr = bytearray()
        active_streams: set[str] = set()
        capabilities: RuntimeCapabilities | None = None
        expected_seq = 0
        last_activity = asyncio.get_running_loop().time()
        while True:
            try:
                frame = await self._read_frame(
                    process,
                    _activity_timeout(deadline, last_activity, inactivity_timeout),
                )
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
            last_activity = asyncio.get_running_loop().time()
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
                self._validate_started_capabilities(
                    capabilities,
                    network_enabled=network_enabled,
                    allow_local_binding=allow_local_binding,
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

    @staticmethod
    def _validate_started_capabilities(
        capabilities: RuntimeCapabilities,
        *,
        network_enabled: bool,
        allow_local_binding: bool,
    ) -> None:
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

    async def _spawn(
        self,
        token: str,
        *,
        expected_helper_digest: str | None = None,
    ) -> asyncio.subprocess.Process:
        if expected_helper_digest is not None:
            try:
                from crew.security.snapshot import _verified_file_digest

                actual_digest = _verified_file_digest(self._helper_argv[0])
            except Exception as exc:
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_DENIED,
                    f"authorized native runtime identity check failed: {exc}",
                ) from exc
            if not hmac.compare_digest(actual_digest, expected_helper_digest):
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_DENIED,
                    "native runtime helper differs from the authorization snapshot",
                )
        # Also bind the helper to the packaged release manifest. This is checked
        # after the per-authorization identity so a post-approval replacement is
        # reported as a denied snapshot, not merely an unavailable installation.
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
        from crew.security.launch import (
            minimal_native_helper_environment,
            trusted_helper_environment,
        )

        env = minimal_native_helper_environment()
        try:
            env.update(trusted_helper_environment(self._helper_argv[0]))
        except Exception as exc:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                f"native security runtime artifact verification failed: {exc}",
            ) from exc
        env["ACE_SECURITY_RUNTIME_TOKEN"] = token
        kwargs = isolated_process_kwargs()
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
    def _validate_ready(frame: Mapping[str, Any]) -> None:
        capabilities = frame.get("capabilities")
        if (
            frame.get("type") != "ready"
            or frame.get("version") != RUNTIME_PROTOCOL_VERSION
            or not isinstance(capabilities, list)
            or not _REQUIRED_READY_CAPABILITIES.issubset(capabilities)
        ):
            raise NativeRuntimeError(
                RuntimeErrorCode.RUNTIME_PROTOCOL_MISMATCH, "native runtime handshake mismatch"
            )

    @staticmethod
    async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
        await terminate_process_tree(process)


def _ask_classification(
    shell_kind: str,
    raw_command: str,
    reason: str,
    *,
    executable: str = "",
    executable_digest: str = "",
) -> ShellClassification:
    return ShellClassification(
        shell_kind=str(shell_kind),
        raw_command=str(raw_command),
        parsed_commands=(),
        canonical_digest="",
        verdict=ShellVerdict.ASK,
        reason=reason,
        executable=executable,
        executable_digest=executable_digest,
    )


def _parse_classification(
    value: Any,
    shell_kind: str,
    raw_command: str,
    *,
    executable: str = "",
    executable_digest: str = "",
) -> ShellClassification:
    if not isinstance(value, dict):
        return _ask_classification(
            shell_kind,
            raw_command,
            "invalid_classification",
            executable=executable,
            executable_digest=executable_digest,
        )
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
    response_shell_kind = str(value.get("shell_kind") or shell_kind)
    response_raw_command = str(value.get("raw_command") or raw_command)
    if response_shell_kind != str(shell_kind) or response_raw_command != str(raw_command):
        verdict = ShellVerdict.ASK
    if verdict is ShellVerdict.ALLOW_READ_ONLY and (
        not commands or len(digest) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        verdict = ShellVerdict.ASK
    return ShellClassification(
        shell_kind=response_shell_kind,
        raw_command=response_raw_command,
        parsed_commands=tuple(tuple(command) for command in commands),
        canonical_digest=digest,
        verdict=verdict,
        reason=str(value.get("reason") or "invalid_classification"),
        executable=executable,
        executable_digest=executable_digest,
    )


def _executable_identity(value: str) -> tuple[str, str]:
    try:
        from crew.security.snapshot import _verified_file_digest

        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            return "", ""
        return str(path), _verified_file_digest(path)
    except (OSError, RuntimeError, ValueError):
        return "", ""


def _command_identity(value: str) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if candidate.parent != Path("."):
            return None
        resolved = shutil.which(value)
        if not resolved:
            return None
        candidate = Path(resolved)
    return _executable_identity(str(candidate)) if candidate.is_absolute() else None


def _command_identities(
    commands: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    for command in commands:
        if not command:
            return ()
        identity = _command_identity(command[0])
        if identity is None:
            return ()
        identities.append(identity)
    return tuple(identities)


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _filesystem_globs_from_profile_payload(
    profile_payload: str,
) -> tuple[dict[str, str], ...]:
    try:
        profile = json.loads(profile_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("authorization filesystem glob profile is invalid") from exc
    if not isinstance(profile, dict):
        raise TypeError("authorization filesystem glob profile is invalid")
    rules = profile.get("filesystem_globs")
    if not isinstance(rules, list):
        raise TypeError("authorization filesystem glob rules are missing")
    return tuple(_validate_filesystem_globs(rules, exact_paths=True))


def _validate_filesystem_globs(
    rules: Sequence[Mapping[str, Any]],
    *,
    exact_paths: bool,
) -> list[dict[str, str]]:
    from crew.security.models import FilesystemGlobAccess, FilesystemGlobEntry

    validated: list[dict[str, str]] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise TypeError("native runtime filesystem glob rule is invalid")
        if set(rule) != {"access", "pattern", "root"}:
            raise ValueError("native runtime filesystem glob rule is invalid")
        root_value = rule.get("root")
        pattern = rule.get("pattern")
        access_value = rule.get("access")
        if not isinstance(root_value, str) or not isinstance(pattern, str):
            raise TypeError("native runtime filesystem glob rule is invalid")
        try:
            access = FilesystemGlobAccess(access_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("native runtime filesystem glob access is invalid") from exc

        if exact_paths:
            root = _authorized_canonical_path(
                root_value,
                label="filesystem glob root",
                strict=True,
            )
        else:
            try:
                root = Path(root_value).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("native runtime filesystem glob root is unavailable") from exc
        if not root.is_dir():
            raise ValueError("native runtime filesystem glob root is not a directory")

        entry = FilesystemGlobEntry(root, pattern, access)
        if entry.root != root or entry.pattern != pattern:
            raise ValueError("native runtime filesystem glob rule is not canonical")
        validated.append(
            {
                "access": entry.access.value,
                "pattern": entry.pattern,
                "root": str(entry.root),
            }
        )
    return validated


def _authorized_canonical_path(
    value: str,
    *,
    label: str,
    strict: bool,
) -> Path:
    """Keep signed paths exact and reject identity-changing resolution."""
    path = Path(value)
    try:
        resolved = path.resolve(strict=strict)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"authorized {label} path is unavailable",
        ) from exc
    if not path.is_absolute() or resolved != path:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"authorized {label} path identity changed",
        )
    return path


def _validate_request_inputs(
    stdin: bytes | None,
    env_overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    if stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > _MAX_STDIN_BYTES):
        raise ValueError("native runtime stdin exceeds the size limit")

    validated = dict(env_overrides or {})
    encoded_size = 0
    normalized_names: set[str] = set()
    for name, value in validated.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("native runtime environment must contain strings")
        normalized_name = name.upper()
        if (
            not _ENV_NAME_PATTERN.fullmatch(name)
            or "\x00" in value
            or normalized_name in _RESERVED_ENV_NAMES
            or normalized_name.startswith(_RESERVED_ENV_PREFIXES)
            or normalized_name in normalized_names
        ):
            raise ValueError("native runtime environment contains a disallowed entry")
        normalized_names.add(normalized_name)
        encoded_size += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if encoded_size > _MAX_ENV_BYTES:
            raise ValueError("native runtime environment exceeds the size limit")
    return validated


def _safe_callback(callback: Callable[[Any], None] | None, value: Any) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001 - observer failures cannot alter execution
        _LOGGER.warning("native runtime activity callback failed")


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _validate_timeout(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} is invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(normalized) or not 0 < normalized <= _MAX_RUNTIME_TIMEOUT_SECONDS:
        raise ValueError(f"{label} is invalid")
    return normalized


def _validate_optional_timeout(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    return _validate_timeout(value, label)


def _activity_timeout(
    deadline: float,
    last_activity: float,
    inactivity_timeout: float | None,
) -> float:
    remaining = _remaining(deadline)
    if inactivity_timeout is None:
        return remaining
    idle_remaining = last_activity + inactivity_timeout - asyncio.get_running_loop().time()
    if idle_remaining <= 0:
        raise TimeoutError
    return min(remaining, idle_remaining)


def _runtime_error_code(value: Any) -> RuntimeErrorCode:
    try:
        return RuntimeErrorCode(str(value))
    except ValueError:
        return RuntimeErrorCode.SANDBOX_DENIED
