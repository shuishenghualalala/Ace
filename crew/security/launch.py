"""Compile trusted conversation state into one process-launch boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from crew.security.context import SecurityContext
from crew.security.file_policy import _protected_entries
from crew.security.models import (
    ConversationPermissionMode,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.policy import settings_for_mode
from crew.security.process_lifecycle import isolated_process_kwargs, terminate_process_tree


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessLaunch:
    """Host-owned launch decision passed to ProcessRegistry, never model input."""

    profile: PermissionProfile
    helper_argv: tuple[str, ...] = ()
    trusted_readable_roots: tuple[Path, ...] = ()
    security_context: SecurityContext | None = None
    audit: Any | None = None

    @property
    def managed(self) -> bool:
        return self.profile.kind is PermissionProfileKind.MANAGED


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


def host_stream_launch_block_reason() -> str | None:
    """Return why a bidirectional host subprocess must be refused, if any.

    Long-lived stdio adapters cannot currently cross the native runtime transport.
    A missing or managed launch boundary therefore always fails closed. Compatibility
    mode may relax transport and approval policy, but it cannot turn managed execution
    into an unconfined host subprocess.
    """
    launch = current_process_launch.get()
    if launch is None:
        return "security launch context missing"
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
    env_overrides: Mapping[str, str] | None = None,
    max_output_bytes: int = 2 * 1024 * 1024,
    on_started: Callable[[int | None], None] | None = None,
    on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
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

    host_env = env
    if env_overrides:
        host_env = {**(env if env is not None else os.environ), **env_overrides}

    action = _execution_action(argv, cwd)
    if launch.managed:
        from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
        from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError

        try:
            result = await SecurityExecutionBroker(NativeRuntimeClient(launch.helper_argv)).execute(
                ExecutionRequest(
                    command=argv,
                    cwd=cwd,
                    permission_profile=launch.profile,
                    trusted_readable_roots=launch.trusted_readable_roots,
                    stdin=stdin,
                    env_overrides=env_overrides,
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
        return CapturedProcessResult(result.exit_code, _redact(result.stdout), _redact(result.stderr))
    # Shield the spawn itself: on Windows process creation can outlive cancellation.
    # Waiting for the handle before propagating cancellation lets us terminate the
    # process tree instead of losing ownership of a child that was already created.
    process: asyncio.subprocess.Process | None = None
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=host_env,
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
            except Exception:
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
    except asyncio.TimeoutError:
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
    except Exception:
        _LOGGER.warning("captured process activity callback failed")


def compile_process_launch(
    context: SecurityContext,
    mode: ConversationPermissionMode,
    *,
    db_path: Path,
    audit: Any | None = None,
) -> ProcessLaunch:
    """Build filesystem/profile facts and locate only the packaged native helper."""
    protected = _protected_entries(context, db_path)
    profile = settings_for_mode(mode, context.workspace_root, deny_entries=protected).profile
    from crew.agent.skills import get_builtin_skills_dir
    from crew.state.home import bundled_runtime_roots

    builtin_skills = get_builtin_skills_dir()
    trusted_roots = [
        *((builtin_skills.resolve(strict=True),) if builtin_skills.is_dir() else ()),
        *bundled_runtime_roots(),
    ]
    trusted_roots = list(dict.fromkeys(trusted_roots))
    return ProcessLaunch(
        profile=profile,
        helper_argv=packaged_runtime_argv() if profile.kind is PermissionProfileKind.MANAGED else (),
        trusted_readable_roots=(
            tuple(trusted_roots) if profile.kind is PermissionProfileKind.MANAGED else ()
        ),
        security_context=context,
        audit=audit,
    )


def shell_argv(command: str) -> tuple[str, ...]:
    """Represent one terminal string as an explicit platform shell argv."""
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise RuntimeError("PowerShell is unavailable")
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$OutputEncoding=[System.Text.Encoding]::UTF8;"
            "chcp 65001|Out-Null;"
            f"{command}"
        )
        return (str(Path(executable).resolve()), "-NoProfile", "-Command", script)
    executable = shutil.which("bash") or "/bin/sh"
    return (str(Path(executable).resolve()), "-lc", command)


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


def packaged_runtime_argv() -> tuple[str, ...]:
    """Resolve a trusted installed helper without searching the task cwd.

    Priority: ACE_SECURITY_RUNTIME env (absolute) → Desktop 本地 staging 目录 → 当前
    平台/架构的仓库预编译产物。所有候选都固定在
    仓库根目录，不搜索任务 cwd。
    """
    configured = os.environ.get("ACE_SECURITY_RUNTIME", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve(strict=False)
    else:
        name = "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
        # crew/security/launch.py → parents[2] = 仓库根
        repo_root = Path(__file__).resolve().parents[2]
        candidates = packaged_runtime_candidates(repo_root, name)
        candidate = next((path for path in candidates if path.is_file()), candidates[0])
    if not candidate.is_absolute():
        raise RuntimeError("native security runtime path must be absolute")
    return (str(candidate),)


class HelperIntegrityError(RuntimeError):
    """Raised when the native helper binary fails manifest/digest verification."""


def _manifest_for(helper_path: Path) -> dict | None:
    """Load the runtime manifest sitting next to the helper, or None if absent.

    The manifest is co-located with the binary (security-runtime/bin/ or the
    packaged resources dir). A missing manifest is not itself an error in dev
    (source tree without a build), but a *present* manifest with a mismatched
    binary_sha256 is fail-closed.
    """
    manifest_path = helper_path.with_name("runtime-manifest.json")
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_helper_integrity(helper_path: str | Path) -> None:
    """Fail-closed when the helper binary exists but does not match its manifest digest.

    Spec §19: an attacker with write access to the install directory could
    replace both the binary and the manifest; a signed package is the real fix.
    This check still raises the bar for the *common* case (binary swapped, stale
    manifest left behind) and aligns Python with the Desktop sha256 gate, so the
    `ACE_SECURITY_RUNTIME` env path no longer bypasses integrity entirely.
    A missing binary is NOT raised here--the subsequent spawn's FileNotFoundError
    already maps to SANDBOX_UNAVAILABLE, and tests assert that spawn path is
    reached. A missing manifest is allowed in an unbuilt development tree.
    Once a manifest is present it must be schema 2, name the selected helper,
    and include its binary digest; otherwise the managed path fails closed.
    """
    path = Path(helper_path)
    if not path.is_file():
        return
    manifest = _manifest_for(path)
    if manifest is None:
        return
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
    if expected_name != path.name:
        raise HelperIntegrityError("native security runtime manifest names a different binary")
    expected = str(manifest.get("binary_sha256", "")).strip()
    if not expected:
        raise HelperIntegrityError("native security runtime manifest is missing binary digest")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise HelperIntegrityError(
            "native security runtime binary digest mismatch: manifest and binary are out of sync"
        )


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
    manifest_path = Path(helper_path).expanduser().resolve(strict=False).with_name(
        "runtime-manifest.json"
    )
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
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
    for entry in value["network_entries"]:
        entry["access"] = entry["access"].value
    return value
