"""Compile trusted conversation state into one process-launch boundary."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Sequence

from crew.security.actions import (
    NormalizedAction,
    normalize_exec_action,
    serialize_normalized_action,
)
from crew.security.context import SecurityContext
from crew.security.file_policy import (
    _discovered_sensitive_entries,
    _protected_entries,
    _protected_globs,
)
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    PermissionProfile,
    PermissionProfileKind,
    SandboxablePreference,
    resolve_sandboxable_preference,
    SandboxPermissions,
    merge_additional_permissions,
)
from crew.security.policy import settings_for_mode
from crew.security.process_lifecycle import isolated_process_kwargs, terminate_process_tree
from crew.security.snapshot import SignedAuthorizationSnapshot
from crew.tools.redact import argv_contains_sensitive_value, sensitive_env_values

_LOGGER = logging.getLogger(__name__)
PROCESS_LAUNCH_AUTHORITY_VERSION = 2
_PROCESS_LAUNCH_MAC_CONTEXT = b"ace-process-launch-v2\x00"

# Only these non-secret runtime variables cross into a host-side helper or
# compatibility subprocess. Explicit task/MCP environment values are carried
# separately through the security boundary and never sourced from this allowlist.
INHERITED_ENV_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATHEXT",
        "PATH",
        "PROGRAMDATA",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)

NATIVE_HELPER_ENV_NAMES = frozenset(
    {
        "ACE_SECURITY_STATE_DIR",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


def _initialized_windows_security_state_dir(home: Path) -> Path | None:
    candidates = [
        home / "AppData" / "Roaming" / application / "security"
        for application in ("crew-desktop", "crew-desktop-ui")
    ]
    candidates = [
        directory
        for directory in candidates
        if (directory / "windows-sandbox-identity.json").is_file()
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            (item / "windows-sandbox-identity.json").stat().st_mtime_ns,
            item.parent.name == "crew-desktop-ui",
        ),
    )


def default_windows_security_state_dir(
    home: Path | None = None,
) -> Path | None:
    """Return an initialized Desktop security state directory without ambient overrides."""
    if os.name != "nt":
        return None
    return _initialized_windows_security_state_dir(home or Path.home())


def configure_default_security_state_dir() -> bool:
    """Bind standalone Windows Gateway startup to an existing Desktop state directory."""
    if os.name != "nt":
        return True
    if os.environ.get("ACE_SECURITY_STATE_DIR", "").strip():
        return True
    directory = default_windows_security_state_dir()
    if directory is None:
        return False
    os.environ["ACE_SECURITY_STATE_DIR"] = str(directory)
    return True


def minimal_inherited_environment() -> dict[str, str]:
    """Return the non-secret host environment needed to start a child."""
    return {name: os.environ[name] for name in INHERITED_ENV_NAMES if name in os.environ}


def minimal_native_helper_environment() -> dict[str, str]:
    """Bootstrap the trusted helper without ambient search or user state."""
    environment = {name: os.environ[name] for name in NATIVE_HELPER_ENV_NAMES if name in os.environ}
    if any("\x00" in name + value for name, value in environment.items()):
        raise ValueError("native helper bootstrap environment is invalid")
    return environment


@dataclass(frozen=True)
class ProcessLaunch:
    """Host-owned launch decision passed to ProcessRegistry, never model input."""

    profile: PermissionProfile
    sandbox_preference: SandboxablePreference = SandboxablePreference.AUTO
    sandboxed: bool = False
    sandbox_system_surface: str = ""
    helper_argv: tuple[str, ...] = ()
    trusted_readable_roots: tuple[Path, ...] = ()
    # External runtimes stay on the legacy host path unless Config explicitly
    # enables the managed security boundary. Built-in tools remain managed
    # according to ``profile`` and do not use this flag.
    external_security_enabled: bool = False
    security_context: SecurityContext | None = None
    audit: Any | None = None
    approval_service: Any | None = None
    additional_permissions: AdditionalPermissionProfile = field(
        default_factory=AdditionalPermissionProfile
    )
    os_user: str = ''
    owner_account_id: str = ''
    workspace_id: str = ''
    session_id: str = ''
    task_id: str = ''
    approved_action: NormalizedAction | None = None
    authority_version: int = 0
    authority_nonce: str = ''
    authority_digest: str = ''
    authority_mac: str = ''

    @property
    def managed(self) -> bool:
        return self.sandboxed

    @property
    def external_managed(self) -> bool:
        """Whether external runtimes must cross the native managed boundary."""
        return self.managed and self.external_security_enabled


current_process_launch: ContextVar[ProcessLaunch | None] = ContextVar(
    "current_process_launch", default=None
)


@contextmanager
def use_process_launch(launch: ProcessLaunch | None) -> Iterator[None]:
    """Install one trusted launch decision for a bounded host call path."""
    token = current_process_launch.set(launch)
    try:
        yield
    finally:
        current_process_launch.reset(token)


def host_stream_launch_block_reason(*, external: bool = False) -> str | None:
    """Return why a bidirectional host subprocess must be refused, if any.

    Long-lived stdio adapters cannot currently cross the native runtime transport.
    A missing or managed launch boundary therefore fails closed for built-in
    execution. External adapters may explicitly opt into the legacy host path
    through the trusted ``Config`` switch; that exception does not affect built-ins.
    """
    launch = current_process_launch.get()
    if launch is None:
        return "security launch context missing"
    if external and not launch.external_security_enabled:
        return None
    try:
        validate_process_launch(launch)
    except Exception:  # noqa: BLE001 - any malformed launch state must fail closed
        return "security launch context invalid"
    if launch.managed:
        return "managed launch requires native bidirectional stdio transport"
    return None


@dataclass(frozen=True)
class CapturedProcessResult:
    returncode: int
    stdout: str
    stderr: str


async def execute_captured(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    home_files: Mapping[str, bytes] | None = None,
    additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
    env_overrides: Mapping[str, str] | None = None,
    max_output_bytes: int = 2 * 1024 * 1024,
    on_started: Callable[[int | None], None] | None = None,
    on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
    external: bool = False,
    tool_name: str = "captured_process",
) -> CapturedProcessResult:
    """Run an adapter under the current conversation boundary.

    Fail-closed on a missing launch decision. ``CrewApp.handle`` compiles a
    ``ProcessLaunch`` (managed or disabled) for every conversation, so a ``None``
    contextvar means we are outside any security-wired runtime — e.g. in a thread or
    a fresh event loop that did not inherit the contextvar. In that state we refuse
    host execution rather than silently run with full OS-user authority under what the
    user believes is a managed conversation.
    """
    launch = current_process_launch.get()
    if launch is None:
        from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "security launch context missing; refused host execution without an explicit decision",
        )
    # Precise injected-secret redaction (always-on) + generic forced redaction on all
    # captured output, so a secret value echoed by the child cannot reach the model or
    # task log (spec §7.3/§109). secret_values come from the env this task injects.
    from crew.tools.redact import redact_secret_values, redact_sensitive_text, sensitive_env_values

    secret_values = sensitive_env_values(dict(env_overrides) if env_overrides is not None else env)

    def _redact(text: str) -> str:
        return redact_sensitive_text(redact_secret_values(text, secret_values), force=True)

    if external and not launch.external_security_enabled:
        launch = _resign_process_launch(
            replace(
                launch,
                profile=PermissionProfile(PermissionProfileKind.DISABLED),
                sandboxed=False,
                helper_argv=(),
                trusted_readable_roots=(),
                external_security_enabled=False,
            )
        )
    host_env = env
    if env_overrides:
        host_env = {**(env if env is not None else os.environ), **env_overrides}

    action = _execution_action(argv, cwd)
    effective_permissions = merge_additional_permissions(
        launch.additional_permissions,
        additional_permissions,
    )
    if launch.managed:
        from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
        from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError

        managed_environment = dict(env_overrides or {})
        authorization = finalize_process_launch(
            launch,
            argv=argv,
            cwd=cwd,
            environment=managed_environment,
        )
        try:
            result = await SecurityExecutionBroker(
                NativeRuntimeClient(authorization.snapshot.helper_argv)
            ).execute(
                ExecutionRequest(
                    authorization_snapshot=authorization,
                    stdin=stdin,
                    env_overrides=managed_environment,
                    timeout_seconds=timeout,
                    max_output_bytes=max_output_bytes,
                ),
                on_started=on_started,
                on_output=on_output,
            )
        except NativeRuntimeError as exc:
            audit_execution_result(
                launch,
                action,
                tool_name=tool_name,
                decision="error",
                stable_error_code=exc.code.value,
            )
            raise
        runtime_capabilities = getattr(result, "capabilities", None)
        audit_execution_result(
            launch,
            action,
            tool_name=tool_name,
            decision="completed" if result.exit_code == 0 else "failed",
            sandbox_backend=(
                str(runtime_capabilities.backend) if runtime_capabilities is not None else ""
            ),
            capabilities=(
                _enabled_capabilities(runtime_capabilities)
                if runtime_capabilities is not None
                else ()
            ),
            exit_code=result.exit_code,
        )
        return CapturedProcessResult(
            result.exit_code, _redact(result.stdout), _redact(result.stderr)
        )
    host_environment = dict(env) if env is not None else minimal_inherited_environment()
    authorization = finalize_process_launch(
        launch,
        argv=argv,
        cwd=cwd,
        environment=host_environment,
    )
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
    from crew.security.snapshot import (
        AuthorizationSnapshotError,
        consume_authorization_snapshot,
    )

    try:
        host_snapshot = consume_authorization_snapshot(
            authorization,
            environment=host_environment,
            expected_owner_account_id=launch.owner_account_id,
            expected_workspace_id=launch.workspace_id,
            expected_session_id=launch.session_id,
            expected_task_id=launch.task_id,
        )
    except AuthorizationSnapshotError as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"host process authorization snapshot rejected: {exc}",
        ) from exc
    # Shield the spawn itself: on Windows process creation can outlive cancellation.
    # Waiting for the handle before propagating cancellation lets us terminate the
    # process tree instead of losing ownership of a child that was already created.
    process: asyncio.subprocess.Process | None = None
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *host_snapshot.argv,
            cwd=host_snapshot.cwd,
            env=host_environment,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **isolated_process_kwargs(),
        )
    )
    try:
        process = await asyncio.shield(spawn_task)
        _safe_activity_callback(on_started, process.pid)
        stdout, stderr = await asyncio.wait_for(
            _collect_host_output(
                process,
                stdin=stdin,
                max_output_bytes=max_output_bytes,
                on_output=on_output,
            ),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        if process is None:
            try:
                process = await asyncio.shield(spawn_task)
            except Exception:  # noqa: BLE001, S110 - preserve caller cancellation
                # The caller's cancellation remains the public outcome; a failed
                # spawn produced no process handle that needs cleanup.
                pass
        if process is not None:
            await terminate_process_tree(process)
        audit_execution_result(
            launch,
            action,
            tool_name=tool_name,
            decision="cancelled",
            sandbox_backend="host_unconfined",
            stable_error_code="cancelled",
        )
        raise
    except TimeoutError:
        if process is not None:
            await terminate_process_tree(process)
        audit_execution_result(
            launch,
            action,
            tool_name=tool_name,
            decision="error",
            sandbox_backend="host_unconfined",
            stable_error_code="timeout",
        )
        raise
    except Exception:
        if process is not None:
            await terminate_process_tree(process)
        audit_execution_result(
            launch,
            action,
            tool_name=tool_name,
            decision="error",
            sandbox_backend="host_unconfined",
            stable_error_code="host_spawn_failed",
        )
        raise
    completed = CapturedProcessResult(
        int(process.returncode or 0),
        _redact(stdout.decode("utf-8", errors="replace")),
        _redact(stderr.decode("utf-8", errors="replace")),
    )
    audit_execution_result(
        launch,
        action,
        tool_name=tool_name,
        decision="completed" if completed.returncode == 0 else "failed",
        sandbox_backend="host_unconfined",
        exit_code=completed.returncode,
    )
    return completed


def execute_captured_sync(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
    tool_name: str = "captured_process",
) -> CapturedProcessResult:
    """Run a captured adapter from a worker thread while preserving its context."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            execute_captured(
                argv,
                cwd=cwd,
                timeout=timeout,
                env=env,
                env_overrides=env_overrides,
                tool_name=tool_name,
            )
        )
    raise RuntimeError("execute_captured_sync must run outside an active event loop")


def _execution_action(argv: tuple[str, ...], cwd: Path):
    from crew.security.actions import normalize_exec_action

    return normalize_exec_action(argv, cwd)


def _enabled_capabilities(capabilities: object) -> tuple[str, ...]:
    values = asdict(capabilities)  # RuntimeCapabilities is a frozen dataclass.
    return tuple(
        key
        for key, value in values.items()
        if key != "backend" and (value is True or key == "wsl_version" and value is not None)
    )


def audit_execution_result(
    launch: ProcessLaunch,
    action: object,
    *,
    tool_name: str,
    decision: str,
    sandbox_backend: str = "",
    capabilities: tuple[str, ...] = (),
    exit_code: int | None = None,
    stable_error_code: str = "",
) -> None:
    """Persist one execution outcome using the action authorized for this launch."""
    if launch.security_context is None or launch.audit is None:
        return
    from crew.security.audit import AuditEvent

    try:
        launch.audit.record(
            AuditEvent.for_action(
                launch.security_context,
                action,
                action_type="exec_result",
                decision=decision,
                decision_source="native_runtime" if launch.managed else "compatibility_host",
                sandbox_backend=sandbox_backend,
                capabilities=capabilities,
                exit_code=exit_code,
                stable_error_code=stable_error_code,
                tool_name=tool_name,
                additional_permissions_summary=_additional_permissions_summary(launch),
            )
        )
    except Exception:
        # exec_result is operational evidence, not the authorization decision itself.
        # A saturated optional audit buffer must not strand an already-completed child.
        _LOGGER.warning("execution result audit write failed", exc_info=True)


async def _collect_host_output(
    process: asyncio.subprocess.Process,
    *,
    stdin: bytes | None,
    max_output_bytes: int,
    on_output: Callable[[Literal["stdout", "stderr"]], None] | None,
) -> tuple[bytes, bytes]:
    """Write one stdin payload while concurrently draining both output streams."""
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

    stdout = bytearray()
    stderr = bytearray()
    active_streams: set[str] = set()
    total_output = 0

    async def write_stdin() -> None:
        if process.stdin is None:
            return
        try:
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def read_stream(
        stream: asyncio.StreamReader | None,
        target: bytearray,
        stream_name: Literal["stdout", "stderr"],
    ) -> None:
        nonlocal total_output
        if stream is None:
            return
        while chunk := await stream.read(64 * 1024):
            if total_output + len(chunk) > max_output_bytes:
                raise NativeRuntimeError(
                    RuntimeErrorCode.OUTPUT_TRUNCATED,
                    "captured process output exceeds the configured limit",
                )
            total_output += len(chunk)
            target.extend(chunk)
            if stream_name not in active_streams:
                active_streams.add(stream_name)
                _safe_activity_callback(on_output, stream_name)

    await asyncio.gather(
        write_stdin(),
        read_stream(process.stdout, stdout, "stdout"),
        read_stream(process.stderr, stderr, "stderr"),
        process.wait(),
    )
    return bytes(stdout), bytes(stderr)


def _safe_activity_callback(callback: Callable[[object], None] | None, value: object) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001 - observer failures cannot alter execution
        _LOGGER.warning("captured process activity callback failed")


def issue_process_launch(
    context: SecurityContext,
    profile: PermissionProfile,
    *,
    sandbox_preference: SandboxablePreference = SandboxablePreference.AUTO,
    sandbox_system_surface: str = "",
    helper_argv: tuple[str, ...] = (),
    trusted_readable_roots: tuple[Path, ...] = (),
    additional_permissions: AdditionalPermissionProfile | None = None,
    approved_action: NormalizedAction | None = None,
    external_security_enabled: bool = False,
    security_context: SecurityContext | None = None,
    audit: Any | None = None,
    approval_service: Any | None = None,
) -> ProcessLaunch:
    """Issue a host-authenticated launch capability; direct dataclass construction has no authority."""
    if not isinstance(context, SecurityContext):
        raise TypeError("security context is invalid")
    if not isinstance(profile, PermissionProfile):
        raise TypeError("permission profile is invalid")
    if not str(context.owner_account_id).strip():
        raise ValueError("security context owner is missing")
    if not str(context.workspace_id).strip():
        raise ValueError("security context workspace is missing")
    sandboxed = resolve_sandboxable_preference(
        profile.kind,
        sandbox_preference,
        system_surface=sandbox_system_surface,
    )
    if additional_permissions is None:
        additional_permissions = AdditionalPermissionProfile()
    elif not isinstance(additional_permissions, AdditionalPermissionProfile):
        raise TypeError("additional permissions are invalid")
    normalized_helper = tuple(str(part) for part in helper_argv)
    if normalized_helper:
        normalized_helper = (
            str(Path(normalized_helper[0]).expanduser().resolve(strict=False)),
            *normalized_helper[1:],
        )
    if sandboxed and not normalized_helper:
        raise ValueError("sandboxed process launch is missing its native backend")
    if not sandboxed and normalized_helper:
        raise ValueError("unsandboxed process launch cannot name a native backend")
    normalized_roots = tuple(
        sorted(
            {Path(root).expanduser().resolve(strict=False) for root in trusted_readable_roots},
            key=str,
        )
    )
    launch = ProcessLaunch(
        profile=profile,
        sandbox_preference=sandbox_preference,
        sandboxed=sandboxed,
        sandbox_system_surface=str(sandbox_system_surface),
        helper_argv=normalized_helper,
        trusted_readable_roots=normalized_roots,
        additional_permissions=additional_permissions,
        os_user=str(context.os_user),
        owner_account_id=str(context.owner_account_id).strip(),
        workspace_id=str(context.workspace_id),
        session_id=str(context.session_id),
        task_id=str(context.task_id),
        approved_action=approved_action,
        external_security_enabled=external_security_enabled if sandboxed else False,
        security_context=security_context,
        audit=audit,
        approval_service=approval_service,
        authority_version=PROCESS_LAUNCH_AUTHORITY_VERSION,
        authority_nonce=secrets.token_hex(16),
    )
    if sandbox_preference is SandboxablePreference.FORBID:
        _LOGGER.warning(
            "issuing audited sandbox FORBID launch for host-fixed surface %s",
            launch.sandbox_system_surface,
        )
    digest = hashlib.sha256(_process_launch_authority_bytes(launch)).hexdigest()
    mac = _process_launch_mac(digest)
    return ProcessLaunch(
        **{
            **launch.__dict__,
            "authority_digest": digest,
            "authority_mac": mac,
        }
    )


def _resign_process_launch(launch: ProcessLaunch) -> ProcessLaunch:
    """Re-sign trusted host-side field replacement of one launch decision."""
    unsigned = replace(
        launch,
        authority_version=PROCESS_LAUNCH_AUTHORITY_VERSION,
        authority_nonce=secrets.token_hex(16),
        authority_digest="",
        authority_mac="",
    )
    digest = hashlib.sha256(_process_launch_authority_bytes(unsigned)).hexdigest()
    return replace(unsigned, authority_digest=digest, authority_mac=_process_launch_mac(digest))


def bind_process_launch_task(launch: ProcessLaunch, task_id: str) -> ProcessLaunch:
    """Reissue verified launch facts for the concrete host runtime task."""
    validate_process_launch(launch)
    bound_task_id = str(task_id).strip()
    if not bound_task_id:
        raise ValueError("process launch task is missing")
    if launch.task_id == bound_task_id:
        return launch
    return issue_process_launch(
        SecurityContext(
            os_user=launch.os_user,
            owner_account_id=launch.owner_account_id,
            workspace_id=launch.workspace_id,
            workspace_root=None,
            session_id=launch.session_id,
            request_id="",
            task_id=bound_task_id,
            cwd=None,
        ),
        launch.profile,
        sandbox_preference=launch.sandbox_preference,
        sandbox_system_surface=launch.sandbox_system_surface,
        helper_argv=launch.helper_argv,
        trusted_readable_roots=launch.trusted_readable_roots,
        additional_permissions=launch.additional_permissions,
        approved_action=launch.approved_action,
        external_security_enabled=launch.external_security_enabled,
        security_context=launch.security_context,
        audit=launch.audit,
        approval_service=launch.approval_service,
    )


def delegate_process_launch_to_private_directory(
    launch: ProcessLaunch,
    directory: str | Path,
    *,
    trusted_readable_roots: tuple[str | Path, ...] = (),
) -> ProcessLaunch:
    """Delegate one verified launch to a host-created private scratch directory.

    This is intentionally narrower than a general filesystem grant: production
    helpers may use it only for a fresh, owner-private directory whose contents
    were materialized by the host. User-selected paths still require the normal
    file-capability approval flow.
    """
    from crew.security.models import (
        FilesystemAccess,
        FilesystemEntry,
        FilesystemOperation,
        NetworkAccess,
        NetworkPolicy,
    )
    from crew.security.policy import (
        filesystem_operation_allowed,
        merge_additional_permissions,
    )
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

    validate_process_launch(launch)
    if not launch.managed:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "private helper delegation requires managed native enforcement",
        )
    candidate = Path(directory).expanduser()
    try:
        info = candidate.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag:
            raise ValueError("scratch directory is a link or reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("scratch path is not a directory")
        if os.name != "nt":
            getuid = getattr(os, "getuid", None)
            if getuid is not None and info.st_uid != getuid():
                raise ValueError("scratch directory is not owned by the current user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("scratch directory is accessible by another user")
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"private process scratch directory rejected: {exc}",
        ) from exc

    # A document converter receives only the host-created scratch tree. It
    # must not inherit the conversation's workspace/home grants or network
    # approvals merely because parsing happened during that conversation.
    delegated_profile = PermissionProfile(
        kind=launch.profile.kind,
        filesystem=tuple(
            entry for entry in launch.profile.filesystem if entry.access is FilesystemAccess.DENY
        ),
        filesystem_globs=launch.profile.filesystem_globs,
        network=NetworkPolicy.RESTRICTED,
        network_entries=tuple(
            entry for entry in launch.profile.network_entries if entry.access is NetworkAccess.DENY
        ),
        allow_local_binding=False,
    )
    try:
        delegated_trusted_roots = tuple(
            sorted(
                {
                    Path(readable_root).expanduser().resolve(strict=True)
                    for readable_root in trusted_readable_roots
                },
                key=str,
            )
        )
        if any(not readable_root.is_dir() for readable_root in delegated_trusted_roots):
            raise ValueError("trusted helper read root is not a directory")
        for readable_root in delegated_trusted_roots:
            try:
                root.relative_to(readable_root)
            except ValueError:
                pass
            else:
                raise ValueError("trusted helper read root contains the writable scratch")
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"trusted helper read root rejected: {exc}",
        ) from exc

    delegated_permissions = merge_additional_permissions(
        AdditionalPermissionProfile(),
        AdditionalPermissionProfile(
            filesystem=(
                FilesystemEntry(
                    root,
                    FilesystemAccess.READ_WRITE,
                    escalatable=False,
                ),
            )
        ),
    )
    if not filesystem_operation_allowed(
        delegated_profile,
        delegated_permissions,
        root,
        FilesystemOperation.WRITE,
    ):
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            "private process scratch directory conflicts with an immutable deny",
        )
    return issue_process_launch(
        SecurityContext(
            os_user=launch.os_user,
            owner_account_id=launch.owner_account_id,
            workspace_id=launch.workspace_id,
            workspace_root=None,
            session_id=launch.session_id,
            request_id="",
            task_id=launch.task_id,
            cwd=root,
        ),
        delegated_profile,
        sandbox_preference=launch.sandbox_preference,
        sandbox_system_surface=launch.sandbox_system_surface,
        helper_argv=launch.helper_argv,
        trusted_readable_roots=delegated_trusted_roots,
        additional_permissions=delegated_permissions,
        # The private helper action is bound to its exact argv/cwd by
        # execute_captured; it must never inherit an unrelated terminal approval.
        approved_action=None,
        external_security_enabled=launch.external_security_enabled,
        security_context=launch.security_context,
        audit=launch.audit,
        approval_service=launch.approval_service,
    )


def finalize_process_launch(
    launch: ProcessLaunch,
    *,
    argv: tuple[str, ...],
    cwd: str | Path,
    environment: Mapping[str, str],
    expected_owner_account_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_session_id: str | None = None,
    expected_task_id: str | None = None,
    credential_environment_names: frozenset[str] = frozenset(),
) -> SignedAuthorizationSnapshot:
    """Bind an issued launch to exact spawn facts and return its sole signed snapshot."""
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
    from crew.security.snapshot import (
        AuthorizationSnapshotError,
        issue_authorization_snapshot,
    )

    try:
        validate_process_launch(
            launch,
            expected_owner_account_id=expected_owner_account_id,
            expected_workspace_id=expected_workspace_id,
            expected_session_id=expected_session_id,
            expected_task_id=expected_task_id,
        )
        if argv_contains_sensitive_value(argv):
            raise AuthorizationSnapshotError(
                "credential-bearing argv is forbidden; use a managed credential channel"
            )
        normalized_credential_names = {
            str(name).casefold() for name in credential_environment_names
        }
        if len(normalized_credential_names) != len(credential_environment_names) or any(
            not isinstance(name, str)
            or not name
            or name.casefold() not in {str(key).casefold() for key in environment}
            for name in credential_environment_names
        ):
            raise AuthorizationSnapshotError(
                "managed credential environment declaration is invalid"
            )
        sensitive_names = {
            str(name).casefold()
            for name, value in environment.items()
            if sensitive_env_values({str(name): str(value)})
            or argv_contains_sensitive_value((str(value),))
        }
        if not sensitive_names.issubset(normalized_credential_names):
            raise AuthorizationSnapshotError(
                "credential-bearing environment is forbidden; use a managed credential channel"
            )
        normalized_cwd = Path(cwd).expanduser().resolve(strict=True)
        action = launch.approved_action or normalize_exec_action(argv, normalized_cwd)
        if action.executable_digest:
            if action.executable != str(argv[0]):
                raise AuthorizationSnapshotError(
                    "authorized executable identity does not match final argv"
                )
            _verify_bound_executable_identity(
                action.executable,
                action.executable_digest,
                "authorized executable",
            )
        elif action.command_identities:
            raise AuthorizationSnapshotError(
                "command identities require a bound shell executable identity"
            )
        for path, digest in action.command_identities:
            _verify_bound_executable_identity(path, digest, "authorized command")
        return issue_authorization_snapshot(
            context=SecurityContext(
                os_user=launch.os_user,
                owner_account_id=launch.owner_account_id,
                workspace_id=launch.workspace_id,
                workspace_root=None,
                session_id=launch.session_id,
                request_id="",
                task_id=launch.task_id,
                cwd=normalized_cwd,
            ),
            action=action,
            profile=launch.profile,
            sandbox_preference=launch.sandbox_preference,
            sandboxed=launch.sandboxed,
            sandbox_system_surface=launch.sandbox_system_surface,
            additional_permissions=launch.additional_permissions,
            argv=argv,
            cwd=normalized_cwd,
            environment=environment,
            helper_argv=launch.helper_argv,
            trusted_readable_roots=launch.trusted_readable_roots,
        )
    except NativeRuntimeError:
        raise
    except (AuthorizationSnapshotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"authorization snapshot rejected: {exc}",
        ) from exc


def _verify_bound_executable_identity(path: str, expected_digest: str, label: str) -> None:
    from crew.security.snapshot import AuthorizationSnapshotError, _verified_file_digest

    try:
        canonical = Path(path).expanduser().resolve(strict=True)
        actual_digest = _verified_file_digest(canonical)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorizationSnapshotError(f"{label} is unavailable") from exc
    if str(canonical) != str(path) or not hmac.compare_digest(
        actual_digest,
        expected_digest,
    ):
        raise AuthorizationSnapshotError(f"{label} identity changed")


def compile_process_launch(
    context: SecurityContext,
    mode: ConversationPermissionMode,
    *,
    db_path: Path,
    sandbox_preference: SandboxablePreference = SandboxablePreference.REQUIRE,
    approved_action: NormalizedAction | None = None,
    external_security_enabled: bool = False,
    audit: Any | None = None,
    approval_service: Any | None = None,
    additional_permissions: AdditionalPermissionProfile | None = None,
    trusted_readable_roots: Sequence[str | Path] = (),
) -> ProcessLaunch:
    """Build the host launch decision from trusted config and security state.

    ``external_security_enabled`` is supplied by ``Config`` for Gateway requests.
    Lower-level callers default external runtimes to the legacy host path.
    Built-in tools remain managed whenever ``profile`` is managed.
    """
    protected = (
        *_protected_entries(context, db_path),
        *_discovered_sensitive_entries(context),
    )
    profile = settings_for_mode(
        mode,
        context.workspace_root,
        deny_entries=protected,
        deny_globs=_protected_globs(context),
    ).profile
    if (
        additional_permissions is not None
        and additional_permissions.sandbox_permissions
        is SandboxPermissions.REQUIRE_ESCALATED
    ):
        profile = PermissionProfile(
            kind=PermissionProfileKind.DISABLED,
            network=profile.network,
        )
        # The approved escalation is host-bound; a REQUIRE preference would
        # contradict the disabled profile and always fail resolution.
        sandbox_preference = SandboxablePreference.AUTO
    from crew.agent.skills import get_builtin_skills_dir
    from crew.state.home import managed_runtime_read_roots

    builtin_skills = get_builtin_skills_dir()
    trusted_roots = [
        *((builtin_skills.resolve(strict=True),) if builtin_skills.is_dir() else ()),
        *managed_runtime_read_roots(),
        *(
            root.resolve(strict=True)
            for value in trusted_readable_roots
            if (root := Path(value).expanduser()).exists()
        ),
    ]
    if os.name == "nt" and profile.full_disk_read:
        trusted_roots.extend(_windows_full_disk_read_roots())
    trusted_roots = list(dict.fromkeys(trusted_roots))
    return issue_process_launch(
        context,
        profile,
        sandbox_preference=sandbox_preference,
        additional_permissions=additional_permissions,
        approved_action=approved_action,
        helper_argv=(
            packaged_runtime_argv() if profile.kind is PermissionProfileKind.MANAGED else ()
        ),
        trusted_readable_roots=(
            tuple(trusted_roots) if profile.kind is PermissionProfileKind.MANAGED else ()
        ),
        external_security_enabled=(
            external_security_enabled
            if profile.kind is PermissionProfileKind.MANAGED
            else False
        ),
        security_context=context,
        audit=audit,
        approval_service=approval_service,
    )


_WINDOWS_SENSITIVE_TOP_LEVEL = frozenset(
    {
        ".ssh",
        ".tsh",
        ".brev",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".config",
        ".npm",
        ".pki",
        ".terraform.d",
        ".crew",
        ".ace",
    }
)


def _windows_full_disk_read_roots() -> tuple[Path, ...]:
    """Enumerate precise Windows read roots without recursive drive ACL changes."""
    roots: list[Path] = []
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(name)
        if value:
            root = Path(value).expanduser().resolve(strict=False)
            if root.exists() and root not in roots:
                roots.append(root)
    home = Path.home().expanduser().resolve(strict=False)
    try:
        children = tuple(home.iterdir())
    except OSError:
        children = ()
    for child in children:
        if child.name.lower() in _WINDOWS_SENSITIVE_TOP_LEVEL:
            continue
        resolved = child.resolve(strict=False)
        if resolved.exists() and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _additional_permissions_summary(launch: ProcessLaunch) -> str:
    if launch.additional_permissions.empty:
        return ""
    from crew.security.models import serialize_additional_permissions

    return json.dumps(
        serialize_additional_permissions(launch.additional_permissions),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _windows_shell_executable() -> Path | None:
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            candidate = Path(executable)
            candidate_info = candidate.lstat()
            if not stat.S_ISREG(candidate_info.st_mode):
                continue
            resolved = candidate.resolve(strict=True)
            resolved_info = resolved.stat()
            if stat.S_ISREG(resolved_info.st_mode):
                return resolved
        except (OSError, RuntimeError):
            # WindowsApps aliases and broken PATH entries can resolve to an
            # inaccessible reparse point. Try the next real PowerShell.
            continue
    return None


def shell_argv(command: str) -> tuple[str, ...]:
    """Represent one terminal string as an explicit platform shell argv."""
    if os.name == "nt":
        executable = _windows_shell_executable()
        if not executable:
            raise RuntimeError("PowerShell is unavailable")
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "chcp 65001 | Out-Null; "
            "$PSDefaultParameterValues['Out-File:Encoding']='utf8NoBOM'; "
            "Remove-Item Alias:pwd -Force -ErrorAction SilentlyContinue; "
            "function global:pwd { (Get-Location).Path }; "
            f"{command}"
        )
        return (
            str(executable),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        )
    executable = shutil.which("bash") or "/bin/sh"
    # Managed runtimes intentionally set HOME to the host user directory so
    # paths such as ~/Desktop resolve correctly. A non-login shell prevents
    # that UX improvement from implicitly sourcing host profile scripts.
    return (str(Path(executable).resolve()), "-c", command)


def runtime_platform_key(
    system_name: str | None = None,
    machine_name: str | None = None,
) -> str | None:
    """Return the repository prebuilt directory key for the current host."""
    system = (system_name or sys.platform).strip().lower()
    system = {
        "macos": "darwin",
        "windows": "win32",
        "linux2": "linux",
    }.get(system, system)
    machine = (machine_name or platform.machine()).strip().lower()
    arch = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x64": "x64",
        "x86_64": "x64",
    }.get(machine)
    if system not in {"darwin", "linux", "win32"} or arch is None:
        return None
    return f"{system}-{arch}"


def packaged_runtime_candidates(repo_root: Path, name: str) -> tuple[Path, ...]:
    """Return fixed, host-specific runtime locations in development priority order."""
    candidates = [repo_root / "desktop" / "security-runtime-bin" / name]
    platform_key = runtime_platform_key()
    if platform_key:
        candidates.append(repo_root / "security-runtime" / "prebuilt" / platform_key / name)
    return tuple(candidates)


@dataclass(frozen=True)
class _DesktopRuntimeBinding:
    path: Path
    binary_sha256: str
    manifest_sha256: str
    bwrap_sha256: str = ""


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _desktop_runtime_binding() -> _DesktopRuntimeBinding | None:
    """Read the packaged Desktop runtime binding supplied by protected main code."""
    raw_path = os.environ.get("ACE_DESKTOP_SECURITY_RUNTIME", "").strip()
    binary_sha256 = os.environ.get("ACE_DESKTOP_SECURITY_RUNTIME_SHA256", "").strip().casefold()
    manifest_sha256 = (
        os.environ.get("ACE_DESKTOP_SECURITY_RUNTIME_MANIFEST_SHA256", "").strip().casefold()
    )
    bwrap_sha256 = os.environ.get("ACE_DESKTOP_BUNDLED_BWRAP_SHA256", "").strip().casefold()
    configured = any((raw_path, binary_sha256, manifest_sha256, bwrap_sha256))
    if not configured:
        return None
    if os.environ.get("ACE_SECURITY_RELEASE_MODE") != "1":
        raise RuntimeError("Desktop runtime binding is valid only in release mode")
    if not raw_path or not _valid_sha256(binary_sha256) or not _valid_sha256(manifest_sha256):
        raise RuntimeError("Desktop runtime binding is incomplete or malformed")
    if bwrap_sha256 and not _valid_sha256(bwrap_sha256):
        raise RuntimeError("Desktop bundled bwrap binding is malformed")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise RuntimeError("Desktop runtime binding path must be absolute")
    absolute = candidate.absolute()
    resolved = candidate.resolve(strict=False)
    if candidate.exists() and resolved != absolute:
        raise RuntimeError("Desktop runtime binding path must not contain symlinks")
    return _DesktopRuntimeBinding(
        path=resolved,
        binary_sha256=binary_sha256,
        manifest_sha256=manifest_sha256,
        bwrap_sha256=bwrap_sha256,
    )


def packaged_runtime_argv() -> tuple[str, ...]:
    """Resolve a trusted installed helper without searching the task cwd.

    A packaged Desktop may provide an ASAR-bound path plus digests. Otherwise
    priority is Desktop staging → platform prebuilt → legacy bin. Arbitrary
    ambient helper overrides remain unsupported.
    """
    name = "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
    desktop_binding = _desktop_runtime_binding()
    if desktop_binding is not None:
        candidate = desktop_binding.path
        if candidate.name != name:
            raise RuntimeError("Desktop runtime binding names the wrong platform helper")
    else:
        # crew/security/launch.py → parents[2] = 仓库根
        repo_root = Path(__file__).resolve().parents[2]
        candidates = packaged_runtime_candidates(repo_root, name)
        candidate = next((path for path in candidates if path.is_file()), candidates[0])
    if not candidate.is_absolute():
        raise RuntimeError("native security runtime path must be absolute")
    absolute = candidate.absolute()
    resolved = candidate.resolve(strict=False)
    if candidate.exists() and resolved != absolute:
        raise RuntimeError("native security runtime path must not contain symlinks")
    return (str(resolved),)


class HelperIntegrityError(RuntimeError):
    """Raised when the native helper binary fails manifest/digest verification."""


def _runtime_requires_hardened_directory(helper_path: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    if os.environ.get("ACE_SECURITY_RELEASE_MODE") == "1" or getattr(sys, "frozen", False):
        return True
    try:
        helper_path.resolve(strict=False).relative_to(repo_root)
    except ValueError:
        return False
    return not (repo_root / ".git").exists()


def _verify_helper_directory(helper_path: Path) -> None:
    """Reject production helper directories writable by another local principal."""
    parent = helper_path.parent
    try:
        helper_stat = helper_path.lstat()
        parent_stat = parent.lstat()
    except OSError as exc:
        raise HelperIntegrityError("native security runtime path is unavailable") from exc
    if (
        stat.S_ISLNK(helper_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
    ):
        raise HelperIntegrityError(
            "native security runtime path contains a symlink or non-directory"
        )
    if not _runtime_requires_hardened_directory(helper_path):
        return
    try:
        helper_path.resolve(strict=True).relative_to(
            Path(tempfile.gettempdir()).resolve(strict=True)
        )
    except ValueError:
        pass
    else:
        raise HelperIntegrityError("production security runtime cannot run from a temp directory")
    if os.name != "nt":
        if parent_stat.st_uid not in {0, os.getuid()}:
            raise HelperIntegrityError("native security runtime directory owner is invalid")
        if stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise HelperIntegrityError(
                "native security runtime directory must be owner-only"
            )


def _manifest_for(helper_path: Path) -> dict:
    """Load the required runtime manifest sitting next to the helper.

    The manifest is co-located with the binary (security-runtime/bin/ or the
    packaged resources dir). Managed execution never treats a source checkout
    as authority to run an unmanifested helper.
    """
    manifest_path = helper_path.with_name("runtime-manifest.json")
    try:
        manifest_stat = manifest_path.lstat()
    except OSError as exc:
        raise HelperIntegrityError("native security runtime manifest is missing") from exc
    if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode):
        raise HelperIntegrityError("native security runtime manifest is not a regular file")
    if not manifest_path.is_file():
        raise HelperIntegrityError("native security runtime manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperIntegrityError(
            "native security runtime manifest is unreadable or malformed"
        ) from exc
    if not isinstance(manifest, dict):
        raise HelperIntegrityError("native security runtime manifest must be an object")
    return manifest


def verify_helper_integrity(helper_path: str | Path) -> None:
    """Fail-closed when the helper binary exists but does not match its manifest digest.

    Packaged Desktop launches additionally bind both files to digests read from
    the integrity-protected ASAR. Standalone development uses the adjacent
    manifest as its local trust source.
    A missing binary is NOT raised here--the subsequent spawn's FileNotFoundError
    maps to SANDBOX_UNAVAILABLE. Every existing helper must have a schema-2
    manifest naming the selected helper and its digest.
    """
    path = Path(helper_path)
    if not path.is_file():
        return
    _verify_helper_directory(path)
    desktop_binding = _desktop_runtime_binding()
    from crew.security.snapshot import _verified_file_digest

    if desktop_binding is not None:
        if path.resolve(strict=True) != desktop_binding.path:
            raise HelperIntegrityError(
                "native security runtime differs from the Desktop-bound helper"
            )
        manifest_digest = _verified_file_digest(path.with_name("runtime-manifest.json"))
        if manifest_digest != desktop_binding.manifest_sha256:
            raise HelperIntegrityError(
                "native security runtime manifest differs from the Desktop trust root"
            )
    manifest = _manifest_for(path)
    if manifest.get("schema") != 2:
        raise HelperIntegrityError("native security runtime manifest schema is unsupported")
    declared_platform = str(manifest.get("platform", "")).strip()
    declared_arch = str(manifest.get("arch", "")).strip()
    if bool(declared_platform) != bool(declared_arch):
        raise HelperIntegrityError("native security runtime manifest target is incomplete")
    if declared_platform and declared_arch:
        declared_key = runtime_platform_key(declared_platform, declared_arch)
        current_key = runtime_platform_key()
        if declared_key is None or current_key is None or declared_key != current_key:
            raise HelperIntegrityError("native security runtime targets a different platform")
    expected_name = str(manifest.get("binary_name", "")).strip()
    if expected_name and expected_name != path.name:
        raise HelperIntegrityError("native security runtime manifest names a different binary")
    expected = str(manifest.get("binary_sha256", "")).strip()
    if not expected or len(expected) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in expected
    ):
        raise HelperIntegrityError("native security runtime manifest is missing binary digest")
    digest = _verified_file_digest(path)
    if digest != expected:
        raise HelperIntegrityError(
            "native security runtime binary digest mismatch: manifest and binary are out of sync"
        )
    if desktop_binding is not None and digest != desktop_binding.binary_sha256:
        raise HelperIntegrityError(
            "native security runtime binary differs from the Desktop trust root"
        )
    source_hash = str(manifest.get("source_hash", "")).strip()
    if source_hash and (
        len(source_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in source_hash)
    ):
        raise HelperIntegrityError("native security runtime manifest source digest is invalid")
    source_state = runtime_source_stale(path) if source_hash else None
    if source_state is True:
        raise HelperIntegrityError("native security runtime source is stale")
    if (
        source_state is None
        and _runtime_requires_hardened_directory(path)
        and desktop_binding is None
    ):
        raise HelperIntegrityError(
            "native security runtime manifest lacks verifiable source provenance"
        )


def trusted_helper_environment(helper_path: str | Path) -> dict[str, str]:
    """Derive optional helper artifacts only from the verified runtime manifest."""
    path = Path(helper_path).resolve(strict=True)
    desktop_binding = _desktop_runtime_binding()
    if desktop_binding is not None:
        verify_helper_integrity(path)
        if not desktop_binding.bwrap_sha256:
            return {}
        bundled = path.with_name("bwrap")
        from crew.security.snapshot import _verified_file_digest

        if _verified_file_digest(bundled) != desktop_binding.bwrap_sha256:
            raise HelperIntegrityError("bundled bwrap differs from the Desktop trust root")
        return {
            "ACE_BUNDLED_BWRAP": str(bundled),
            "ACE_BUNDLED_BWRAP_SHA256": desktop_binding.bwrap_sha256,
        }
    manifest = _manifest_for(path)
    records = manifest.get("files", [])
    if not isinstance(records, list):
        raise HelperIntegrityError("native security runtime manifest files are invalid")
    bwrap_records = [
        record for record in records if isinstance(record, dict) and record.get("name") == "bwrap"
    ]
    if not bwrap_records:
        return {}
    if len(bwrap_records) != 1:
        raise HelperIntegrityError("native security runtime manifest repeats bundled bwrap")
    expected = str(bwrap_records[0].get("sha256", "")).strip().casefold()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise HelperIntegrityError("bundled bwrap manifest digest is invalid")
    bundled = path.with_name("bwrap")
    from crew.security.snapshot import _verified_file_digest

    if _verified_file_digest(bundled) != expected:
        raise HelperIntegrityError("bundled bwrap does not match runtime manifest")
    return {
        "ACE_BUNDLED_BWRAP": str(bundled),
        "ACE_BUNDLED_BWRAP_SHA256": expected,
    }


def _process_launch_authority_bytes(launch: ProcessLaunch) -> bytes:
    from crew.security.policy import serialize_additional_permissions
    from crew.security.snapshot import canonical_json_bytes

    payload = {
        "additional_permissions": serialize_additional_permissions(launch.additional_permissions),
        "approved_action": (
            serialize_normalized_action(launch.approved_action)
            if launch.approved_action is not None
            else None
        ),
        "authority_nonce": launch.authority_nonce,
        "authority_version": launch.authority_version,
        "helper_argv": list(launch.helper_argv),
        "os_user": launch.os_user,
        "owner_account_id": launch.owner_account_id,
        "profile": serialize_profile(launch.profile),
        "sandbox_preference": launch.sandbox_preference.value,
        "sandbox_system_surface": launch.sandbox_system_surface,
        "sandboxed": launch.sandboxed,
        "session_id": launch.session_id,
        "task_id": launch.task_id,
        "trusted_readable_roots": [str(root) for root in launch.trusted_readable_roots],
        "workspace_id": launch.workspace_id,
    }
    return canonical_json_bytes(payload)


def _process_launch_mac(digest: str) -> str:
    from crew.security.snapshot import _host_signing_key

    return hmac.new(
        _host_signing_key("process-launch"),
        _PROCESS_LAUNCH_MAC_CONTEXT + digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def validate_process_launch(
    launch: ProcessLaunch | None,
    *,
    expected_owner_account_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_session_id: str | None = None,
    expected_task_id: str | None = None,
) -> None:
    """Validate an explicit host-owned launch decision before any process starts."""
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

    if not isinstance(launch, ProcessLaunch):
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "security process launch decision is missing or invalid",
        )
    try:
        digest = hashlib.sha256(_process_launch_authority_bytes(launch)).hexdigest()
        authority_valid = (
            launch.authority_version == PROCESS_LAUNCH_AUTHORITY_VERSION
            and len(launch.authority_nonce) == 32
            and len(launch.authority_digest) == 64
            and len(launch.authority_mac) == 64
            and hmac.compare_digest(digest, launch.authority_digest)
            and hmac.compare_digest(_process_launch_mac(digest), launch.authority_mac)
        )
    except Exception:  # noqa: BLE001 - malformed authority state must fail closed
        authority_valid = False
    if not authority_valid:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            "security process launch authority is missing or forged",
        )
    try:
        resolved_sandboxed = resolve_sandboxable_preference(
            launch.profile.kind,
            launch.sandbox_preference,
            system_surface=launch.sandbox_system_surface,
        )
    except (TypeError, ValueError) as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"security process sandbox preference is invalid: {exc}",
        ) from exc
    if not isinstance(launch.sandboxed, bool) or launch.sandboxed is not resolved_sandboxed:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            "security process sandbox choice is inconsistent",
        )
    for actual, expected, label in (
        (launch.owner_account_id, expected_owner_account_id, "owner"),
        (launch.workspace_id, expected_workspace_id, "workspace"),
        (launch.session_id, expected_session_id, "session"),
        (launch.task_id, expected_task_id, "task"),
    ):
        if expected is not None and actual != str(expected):
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                f"security process launch {label} mismatch",
            )
    kind = launch.profile.kind
    if not launch.sandboxed:
        if kind is not PermissionProfileKind.DISABLED or launch.helper_argv:
            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "unsandboxed process launch contains managed authority",
            )
        return
    if kind is not PermissionProfileKind.MANAGED:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "sandboxed process launch does not have a managed profile",
        )
    if not launch.helper_argv:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "managed launch is missing the native security runtime",
        )
    helper = Path(launch.helper_argv[0])
    if not helper.is_absolute() or not helper.is_file():
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            "native security runtime is unavailable",
        )
    try:
        verify_helper_integrity(helper)
    except NativeRuntimeError:
        raise
    except Exception as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_UNAVAILABLE,
            f"native security runtime integrity check failed: {exc}",
        ) from exc


def runtime_source_stale(helper_path: str | Path | None = None) -> bool | None:
    """检测提交进 bin/ 的二进制是否落后于 Rust 源码。

    对 helper 旁边 manifest 里记录的 source_hash 与当前 src/+Cargo.toml+tests 的实时哈希
    比对。打包态 manifest 只包含二进制文件 hash，没有 source_hash，因此返回 None。
    返回 True=过期、False=一致、None=无法判定（缺 manifest/source_hash 或源码未随包）。
    """
    repo_root = Path(__file__).resolve().parents[2]
    sec_root = repo_root / "security-runtime"
    if helper_path is None:
        helper_path = packaged_runtime_argv()[0]
    manifest_path = (
        Path(helper_path).expanduser().resolve(strict=False).with_name("runtime-manifest.json")
    )
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    runtime_name = "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
    expected = ""
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and entry.get("name") == runtime_name:
            expected = str(entry.get("source_hash", ""))
            break
    if not expected and manifest.get("binary_name") == runtime_name:
        expected = str(manifest.get("source_hash", ""))
    if not expected:
        return None
    files = sorted(
        p
        for p in [
            *sec_root.glob("src/**/*"),
            *sec_root.glob("tests/**/*.rs"),
            sec_root / "Cargo.toml",
            sec_root / "Cargo.lock",
        ]
        if p.is_file()
    )
    if not files:
        return None
    # IMPORTANT: this file set MUST stay identical to the manifest generator in
    # scripts/build-security-runtime.{sh,ps1}. Diverging here (e.g. globbing only
    # ``src/**/*.rs`` or dropping Cargo.lock) makes a freshly rebuilt runtime report
    # stale even though the committed manifest matches the build script — the two
    # algorithms drifted before and masked real source/binary drift.
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(sec_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(p.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest() != expected


def serialize_profile(profile: PermissionProfile) -> dict:
    """Serialize a profile for the fixed background bridge protocol."""
    value = asdict(profile)
    value["kind"] = profile.kind.value
    value["network"] = profile.network.value
    for entry in value["filesystem"]:
        entry["root"] = str(entry["root"])
        entry["access"] = entry["access"].value
    for entry in value["filesystem_globs"]:
        entry["root"] = str(entry["root"])
        entry["access"] = entry["access"].value
    for entry in value["network_entries"]:
        entry["access"] = entry["access"].value
    return value
