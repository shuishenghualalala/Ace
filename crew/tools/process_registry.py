"""后台进程注册表 —— 管理 terminal(background=true) 启动的进程。

对照 Hermes tools/process_registry.py 裁剪而来。Crew 是本地单机场景，故砍掉
Hermes 的 PTY 交互 / Docker·SSH·Modal sandbox 后端 / gateway watcher 路由 /
跨 session 全局熔断器，只保留本地进程真正需要的核心：

  - 输出捕获（reader 线程 + 200KB 滚动缓冲，替代原来的 DEVNULL 丢弃）
  - 状态轮询 / 日志读取 / 阻塞等待 / 杀进程
  - 带完整性和进程身份校验的 crash-recovery checkpoint
  - watch_patterns：输出命中模式时排队通知（单 session 限流 + strike 自动降级）
  - notify_on_complete：进程退出时排队一次完成通知

通知投递复用 Crew 既有通路：reader 线程把事件写入 per-session 待通知队列，
app.handle() 每轮 drain_for_session() 取出 → envelope.params → runtime 拼进
<system-reminder>，与后台子 agent 通知保持一致。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

import crew as _crew_pkg
from crew.core.runctx import current_owner_account_id
from crew.security.launch import (
    ProcessLaunch,
    finalize_process_launch,
    minimal_inherited_environment,
    shell_argv,
    validate_process_launch,
)
from crew.security.models import (
    HOST_FIXED_SANDBOX_FORBID_SURFACES,
    PermissionProfileKind,
    SandboxablePreference,
)
from crew.security.process_lifecycle import windows_system_executable
from crew.tools.output_filters import strip_ansi
from crew.tools.registry import tool_error

SessionKey = tuple[str, str]
log = logging.getLogger(__name__)

# Trust root for the managed background bridge subprocess. The bridge must import
# ``crew`` only from the installed package location, never from the task ``cwd``:
# ``python -m crew.security.background_runner`` puts ``cwd`` on ``sys.path[0]`` and a
# workspace-dropped ``crew/security/background_runner.py`` (plus a fake
# ``crew/security/runtime_client.py``) would execute on the host *before* the native
# helper, classifier, or sandbox ever run (H-1). The launcher below runs under
# ``-I`` (no PYTHONPATH / user site) and rebuilds ``sys.path`` to drop ``cwd`` and
# prepend only this trusted root.
_CREW_TRUST_ROOT = str(Path(_crew_pkg.__file__).resolve().parent.parent)
_BACKGROUND_BRIDGE_LAUNCHER = (
    "import os,sys; "
    "_cwd=os.path.abspath(os.getcwd()); "
    "sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != _cwd]; "
    f"sys.path.insert(0, {_CREW_TRUST_ROOT!r}); "
    "from crew.process_hardening import harden_main_process; "
    'harden_main_process("managed-background-bridge"); '
    "from crew.security.background_runner import main; "
    "raise SystemExit(main())"
)

# ---- 限制项 ----
MAX_OUTPUT_CHARS = 200_000      # 200KB 滚动输出缓冲
MAX_OUTPUT_REF_BYTES = 200_000  # 持久输出同样必须有硬上限
_OUTPUT_REF_TRUNCATED = b"[ACE_OUTPUT_TRUNCATED]\n"
FINISHED_TTL_SECONDS = 1800     # 已结束进程保留 30 分钟
MAX_PROCESSES = 64              # 最大并发跟踪进程数（超出按最旧 LRU 淘汰）
MAX_PENDING_PER_SESSION = 20    # 每 session 待注入通知上限，防无限堆积
MAX_RUNNING_PROCESSES_PER_OWNER = 8
MAX_RUNNING_PROCESSES_GLOBAL = 32
PROCESS_CHECKPOINT_VERSION = 5
PROCESS_CHECKPOINT_SCHEMA = "ace.process-checkpoint.v5"
_CHECKPOINT_MAC_CONTEXT = b"ace-process-checkpoint-v5\x00"
_CHECKPOINT_MAX_BYTES = 2 * 1024 * 1024
_MAX_EXECUTABLE_IDENTITY_BYTES = 512 * 1024 * 1024
_DEFAULT_STANDALONE_AUTHORITY_TTL_SECONDS = 7 * 24 * 60 * 60
_CHECKPOINT_WRITE_LOCK = threading.RLock()
_HEX_256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_RECOVERY_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_RECOVERY_STATES = frozenset({"live", "cleanup_pending"})


class ProcessCheckpointError(RuntimeError):
    """A durable recovery checkpoint could not be proven."""


class ProcessCleanupError(ProcessCheckpointError):
    """One or more identity-pinned processes remain behind a durable fence."""

# ---- watch_patterns 限流（单 session）----
# 硬规则：每 WATCH_MIN_INTERVAL_SECONDS 至多发一条 watch 命中通知。冷却窗口内到达的
# 命中被丢弃并记一次 strike；连续 WATCH_STRIKE_LIMIT 个 strike 窗口后，永久禁用该
# session 的 watch_patterns 并降级为 notify_on_complete（进程真正退出时发一条）。
WATCH_MIN_INTERVAL_SECONDS = 15
WATCH_STRIKE_LIMIT = 3

_IS_WINDOWS = os.name == "nt"
_WINDOWS_PROCESS_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable host identity that distinguishes a process from a reused PID."""

    create_time: float
    executable: str
    executable_digest: str
    os_owner: str

    def to_payload(self) -> dict[str, object]:
        return {
            "create_time": self.create_time,
            "executable": self.executable,
            "executable_digest": self.executable_digest,
            "os_owner": self.os_owner,
        }

    @classmethod
    def from_payload(cls, value: object) -> ProcessIdentity:
        if not isinstance(value, dict) or set(value) != {
            "create_time",
            "executable",
            "executable_digest",
            "os_owner",
        }:
            raise ValueError("process identity schema is invalid")
        create_time = value.get("create_time")
        executable = value.get("executable")
        executable_digest = value.get("executable_digest")
        os_owner = value.get("os_owner")
        if (
            isinstance(create_time, bool)
            or not isinstance(create_time, (int, float))
            or float(create_time) <= 0
            or not isinstance(executable, str)
            or not executable
            or "\x00" in executable
            or not isinstance(executable_digest, str)
            or len(executable_digest) != 64
            or any(character not in "0123456789abcdef" for character in executable_digest)
            or not isinstance(os_owner, str)
            or not os_owner
            or "\x00" in os_owner
        ):
            raise ValueError("process identity values are invalid")
        return cls(float(create_time), executable, executable_digest, os_owner)


@dataclass(frozen=True)
class RecoveryProcessIdentity:
    """Secret-free process identity persisted for crash recovery.

    The executable path is intentionally omitted. PID creation time, executable
    bytes, and OS owner are sufficient to reject PID reuse while avoiding an
    absolute host path in the checkpoint.
    """

    create_time: float
    executable_digest: str
    os_owner: str

    @classmethod
    def from_process_identity(cls, identity: ProcessIdentity) -> RecoveryProcessIdentity:
        return cls(
            create_time=identity.create_time,
            executable_digest=identity.executable_digest,
            os_owner=identity.os_owner,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "create_time": self.create_time,
            "executable_digest": self.executable_digest,
            "os_owner": self.os_owner,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecoveryProcessIdentity:
        if not isinstance(value, dict) or set(value) != {
            "create_time",
            "executable_digest",
            "os_owner",
        }:
            raise ValueError("process recovery identity schema is invalid")
        create_time = value.get("create_time")
        executable_digest = value.get("executable_digest")
        os_owner = value.get("os_owner")
        if (
            isinstance(create_time, bool)
            or not isinstance(create_time, (int, float))
            or not math.isfinite(float(create_time))
            or float(create_time) <= 0
            or not isinstance(executable_digest, str)
            or not _HEX_256_RE.fullmatch(executable_digest)
            or not isinstance(os_owner, str)
            or not os_owner
            or len(os_owner) > 512
            or "\x00" in os_owner
        ):
            raise ValueError("process recovery identity values are invalid")
        return cls(float(create_time), executable_digest, os_owner)


@dataclass(frozen=True)
class OwnerProcessAuthority:
    """Authenticated owner generation currently allowed to launch/control processes."""

    generation: str
    expires_at: float


def terminate_process_tree(pid: int) -> None:
    """Best-effort terminate one process and its complete child process tree.

    Windows ``Popen.terminate()`` only stops the top-level process, so command
    chains such as PowerShell → Node → LibreOffice can otherwise leave orphaned
    children. POSIX processes are started in their own group and terminate as a
    group; Windows uses ``taskkill /T /F``.
    """
    if pid <= 0:
        return
    try:
        if _IS_WINDOWS:
            taskkill = windows_system_executable("taskkill.exe")
            if taskkill is None:
                raise OSError("trusted taskkill executable is unavailable")
            subprocess.run(
                [taskkill, "/PID", str(pid), "/T", "/F"],
                env=minimal_inherited_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        return


def _terminate_verified_process_tree(
    pid: int,
    expected_identity: ProcessIdentity,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Terminate only the expected process and prove that identity is gone."""
    actual = _process_identity(pid)
    if actual is None:
        return
    if not _process_identity_matches(expected_identity, actual):
        raise ProcessCheckpointError(
            "uncheckpointed process identity changed before cleanup"
        )
    terminate_process_tree(pid)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        actual = _process_identity(pid)
        if actual is None or not _process_identity_matches(expected_identity, actual):
            return
        time.sleep(0.05)
    raise ProcessCheckpointError(
        "uncheckpointed process tree could not be terminated"
    )


def _checkpoint_path() -> Path:
    """崩溃恢复 checkpoint 文件路径（懒解析，随 CREW_HOME 变化）。"""
    from crew.state.home import get_crew_home

    return get_crew_home() / "processes.json"


def _checkpoint_signing_key() -> bytes:
    """Derive checkpoint integrity authority from the existing protected host key."""
    from crew.gateway.auth import _load_or_create_session_key

    return hmac.new(
        _load_or_create_session_key(),
        _CHECKPOINT_MAC_CONTEXT,
        hashlib.sha256,
    ).digest()


def _checkpoint_mac(payload: dict[str, Any]) -> str:
    from crew.security.snapshot import canonical_json_bytes

    return hmac.new(
        _checkpoint_signing_key(),
        _CHECKPOINT_MAC_CONTEXT + canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _protect_checkpoint_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _IS_WINDOWS:
        from crew.gateway.windows_acl import protect_path

        protect_path(path.parent, directory=True)
    else:
        path.parent.chmod(0o700)


def _checkpoint_path_is_secure(path: Path) -> bool:
    """Require owner-only metadata on both checkpoint file and its parent."""
    try:
        parent = os.lstat(path.parent)
        info = os.lstat(path)
    except OSError:
        return False
    if (
        stat.S_ISLNK(parent.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or not stat.S_ISREG(info.st_mode)
    ):
        return False
    if _IS_WINDOWS:
        try:
            from crew.gateway.windows_acl import path_is_secure

            return path_is_secure(path.parent, directory=True) and path_is_secure(
                path,
                directory=False,
            )
        except (OSError, ValueError):
            return False
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and (parent.st_uid != getuid() or info.st_uid != getuid()):
        return False
    return stat.S_IMODE(parent.st_mode) == 0o700 and stat.S_IMODE(info.st_mode) == 0o600


def _write_checkpoint_document(path: Path, document: dict[str, Any]) -> None:
    from crew.security.snapshot import canonical_json_bytes
    from crew.tools.file_utils import atomic_replace_bytes, snapshot_file

    with _CHECKPOINT_WRITE_LOCK:
        _protect_checkpoint_parent(path)
        data = canonical_json_bytes(document)
        if len(data) > _CHECKPOINT_MAX_BYTES:
            raise ValueError("process checkpoint exceeds size limit")
        expected = snapshot_file(path, max_bytes=_CHECKPOINT_MAX_BYTES)
        atomic_replace_bytes(path, data, expected, max_bytes=_CHECKPOINT_MAX_BYTES)
        if _IS_WINDOWS:
            from crew.gateway.windows_acl import protect_path

            protect_path(path, directory=False)
        else:
            path.chmod(0o600)
        if not _checkpoint_path_is_secure(path):
            raise PermissionError("process checkpoint permissions are not owner-only")
        descriptor = os.open(
            path,
            os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            parent_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _read_checkpoint_document(path: Path) -> object:
    from crew.tools.file_utils import read_verified_bytes

    if not _checkpoint_path_is_secure(path):
        raise PermissionError("process checkpoint permissions are invalid")
    data = read_verified_bytes(path, max_bytes=_CHECKPOINT_MAX_BYTES)
    return json.loads(data.decode("utf-8"))


def _owner_matches(session: ProcessSession, owner_account_id: str = "") -> bool:
    owner = str(owner_account_id or "").strip()
    return not owner or str(session.owner_account_id or "") == owner


def _executable_digest(path: str | Path) -> str:
    """Hash one regular executable through a stable handle with a hard byte cap."""
    executable = Path(path)
    before = os.lstat(executable)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & reparse_flag
        or not stat.S_ISREG(before.st_mode)
        or before.st_size < 0
        or before.st_size > _MAX_EXECUTABLE_IDENTITY_BYTES
    ):
        raise ValueError("process executable identity is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(executable, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_file_attributes", 0) & reparse_flag
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > _MAX_EXECUTABLE_IDENTITY_BYTES
        ):
            raise ValueError("process executable identity changed while opening")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_EXECUTABLE_IDENTITY_BYTES:
                raise ValueError("process executable exceeds identity byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("process executable changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _process_identity(pid: int | None) -> ProcessIdentity | None:
    """Capture PID creation, executable path/digest, and OS owner."""
    if not pid or pid <= 0:
        return None
    try:
        process = psutil.Process(int(pid))
        with process.oneshot():
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            create_time = float(process.create_time())
            executable = str(Path(process.exe()).expanduser().resolve(strict=False))
            owner = str(process.username()).strip()
        executable_digest = _executable_digest(executable)
        if not process.is_running() or float(process.create_time()) != create_time:
            return None
    except (psutil.Error, OSError, RuntimeError, ValueError):
        return None
    if create_time <= 0 or not executable or not owner:
        return None
    return ProcessIdentity(create_time, executable, executable_digest, owner)


def _process_identity_matches(
    expected: ProcessIdentity | None,
    actual: ProcessIdentity | None,
) -> bool:
    if expected is None or actual is None:
        return False
    return (
        abs(expected.create_time - actual.create_time) <= 0.001
        and os.path.normcase(expected.executable) == os.path.normcase(actual.executable)
        and hmac.compare_digest(expected.executable_digest, actual.executable_digest)
        and expected.os_owner.casefold() == actual.os_owner.casefold()
    )


def _recovery_identity_matches(
    expected: RecoveryProcessIdentity | None,
    actual: ProcessIdentity | None,
) -> bool:
    if expected is None or actual is None:
        return False
    return (
        abs(expected.create_time - actual.create_time) <= 0.001
        and hmac.compare_digest(expected.executable_digest, actual.executable_digest)
        and expected.os_owner.casefold() == actual.os_owner.casefold()
    )


def _host_boot_id() -> str:
    """Return a non-secret boot fingerprint for explicit reboot handling."""
    boot_time = int(psutil.boot_time())
    return hashlib.sha256(f"ace-host-boot:{boot_time}".encode("ascii")).hexdigest()


def _pid_alive(pid: int | None) -> bool:
    """best-effort 检测 host PID 是否存活。"""
    return _process_identity(pid) is not None


def _consume_spawn_authorization(
    signed_snapshot: Any,
    *,
    environment: dict[str, str],
    owner_account_id: str,
    workspace_id: str,
    session_id: str,
    task_id: str,
) -> None:
    """Consume one final host-spawn authority or return a stable denial."""
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
    from crew.security.snapshot import (
        AuthorizationSnapshotError,
        consume_authorization_snapshot,
    )

    try:
        consume_authorization_snapshot(
            signed_snapshot,
            environment=environment,
            expected_owner_account_id=owner_account_id,
            expected_workspace_id=workspace_id,
            expected_session_id=session_id,
            expected_task_id=task_id,
        )
    except AuthorizationSnapshotError as exc:
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            f"process authorization snapshot rejected: {exc}",
        ) from exc


def _parse_checkpoint_entry(
    value: object,
) -> tuple[dict[str, Any], RecoveryProcessIdentity]:
    expected = {
        "authorization_digest",
        "authorization_expires_at",
        "authorization_generation",
        "authorization_policy_digest",
        "authorization_revalidatable",
        "cleanup_attempts",
        "cleanup_reason",
        "notify_on_complete",
        "owner_account_id",
        "pid",
        "process_identity",
        "recovery_state",
        "sandbox_preference",
        "sandbox_system_surface",
        "sandboxed",
        "session_id",
        "session_key",
        "started_at",
        "task_id",
        "workspace_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("process checkpoint entry schema is invalid")
    for name in (
        "authorization_digest",
        "authorization_generation",
        "authorization_policy_digest",
        "cleanup_reason",
        "owner_account_id",
        "recovery_state",
        "sandbox_preference",
        "sandbox_system_surface",
        "session_id",
        "session_key",
        "task_id",
        "workspace_id",
    ):
        item = value.get(name)
        if not isinstance(item, str) or len(item) > 512 or "\x00" in item:
            raise ValueError(f"process checkpoint {name} is invalid")
    if (
        not value["owner_account_id"]
        or not value["workspace_id"]
        or not value["session_id"]
        or not value["session_key"]
    ):
        raise ValueError("process checkpoint ownership is missing")
    for name in (
        "authorization_digest",
        "authorization_generation",
        "authorization_policy_digest",
    ):
        if not _HEX_256_RE.fullmatch(value[name]):
            raise ValueError(f"process checkpoint {name} is invalid")
    if value["recovery_state"] not in _RECOVERY_STATES:
        raise ValueError("process checkpoint recovery state is invalid")
    reason = value["cleanup_reason"]
    if reason and not _SAFE_RECOVERY_CODE_RE.fullmatch(reason):
        raise ValueError("process checkpoint cleanup reason is invalid")
    attempts = value.get("cleanup_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 1_000_000:
        raise ValueError("process checkpoint cleanup attempts are invalid")
    if value["recovery_state"] == "live" and (reason or attempts):
        raise ValueError("live process checkpoint cannot contain cleanup state")
    if value["recovery_state"] == "cleanup_pending" and not reason:
        raise ValueError("cleanup tombstone is missing its reason")
    pid = value.get("pid")
    started_at = value.get("started_at")
    expires_at = value.get("authorization_expires_at")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("process checkpoint PID is invalid")
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(float(started_at))
        or float(started_at) <= 0
    ):
        raise ValueError("process checkpoint start time is invalid")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(float(expires_at))
        or float(expires_at) <= 0
    ):
        raise ValueError("process checkpoint authorization expiry is invalid")
    if not isinstance(value.get("notify_on_complete"), bool):
        raise TypeError("process checkpoint notification flag is invalid")
    if not isinstance(value.get("authorization_revalidatable"), bool):
        raise TypeError("process checkpoint revalidation flag is invalid")
    if not isinstance(value.get("sandboxed"), bool):
        raise TypeError("process checkpoint sandbox choice is invalid")
    try:
        preference = SandboxablePreference(value["sandbox_preference"])
    except ValueError as exc:
        raise ValueError("process checkpoint sandbox preference is invalid") from exc
    surface = value["sandbox_system_surface"]
    if preference is SandboxablePreference.FORBID:
        if (
            value["sandboxed"]
            or surface not in HOST_FIXED_SANDBOX_FORBID_SURFACES
        ):
            raise ValueError("process checkpoint FORBID surface is invalid")
    elif (
        surface
        or (
            preference is SandboxablePreference.REQUIRE
            and not value["sandboxed"]
        )
    ):
        raise ValueError("process checkpoint sandbox authority is invalid")
    identity = RecoveryProcessIdentity.from_payload(value.get("process_identity"))
    return value, identity


@dataclass
class ProcessSession:
    """一个被跟踪的后台进程及其输出缓冲。"""

    id: str
    command: str
    session_key: str = ""                         # 归属会话（用于通知路由）
    owner_account_id: str = ""
    workspace_id: str = ""
    pid: int | None = None
    process: subprocess.Popen | None = None
    cwd: str | None = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: int | None = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                        # 崩溃恢复认领的进程：可查状态/可 kill，但无输出历史
    notify_on_complete: bool = False
    watch_patterns: list[str] = field(default_factory=list)
    # watch 限流状态（单 session）
    _watch_disabled: bool = field(default=False, repr=False)
    _watch_suppressed: int = field(default=0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _heartbeat_thread: threading.Thread | None = field(default=None, repr=False)
    _secret_values: tuple[str, ...] = field(default=(), repr=False)
    task_id: str = ""
    authorization_digest: str = ""
    output_ref: str = ""
    _output_ref_disabled: bool = field(default=False, repr=False)
    authorization_snapshot: Any = field(default=None, repr=False)
    process_identity: ProcessIdentity | None = None
    identity_mismatch: bool = False
    authorization_generation: str = ""
    authorization_expires_at: float = 0.0
    authorization_policy_digest: str = ""
    authorization_revalidatable: bool = True
    sandbox_preference: str = ""
    sandboxed: bool = False
    sandbox_system_surface: str = ""
    recovery_state: str = "live"
    cleanup_reason: str = ""
    cleanup_attempts: int = 0
    frozen: bool = False


class ProcessRegistry:
    """运行中 / 已结束后台进程的内存注册表。线程安全。"""

    def __init__(self) -> None:
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._frozen: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        # 待注入通知：(owner_account_id, session_key) -> [event, ...]。reader 线程写入，handle() 取出。
        self._pending: dict[SessionKey, list[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._task_runtime: Any = None
        self._reserved_global = 0
        self._reserved_by_owner: dict[str, int] = {}
        self._strict_lifecycle = False
        self._workspace_root_resolver: Callable[[str, str], str | Path | None] | None = None
        self._output_root_resolver: Callable[[str], str | Path | None] | None = None
        self._session_validator: Callable[[str, str, str, str], bool] | None = None
        self._policy_digest_resolver: Callable[[str, str, str, str], str] | None = None
        self._audit_recorder: Callable[[dict[str, Any]], None] | None = None
        self._owner_authorities: dict[str, OwnerProcessAuthority] = {}
        self._checkpoint_fenced_reason = ""

    def configure_task_runtime(self, runtime: Any) -> None:
        """Attach the unified task runtime without making tools import app."""
        self._task_runtime = runtime

    def configure_lifecycle(
        self,
        *,
        workspace_root_resolver: Callable[[str, str], str | Path | None],
        output_root_resolver: Callable[[str], str | Path | None],
        session_validator: Callable[[str, str, str, str], bool],
        policy_digest_resolver: Callable[[str, str, str, str], str],
        audit_recorder: Callable[[dict[str, Any]], None],
    ) -> None:
        """Install production owner/session/policy authorities before recovery."""
        if not all(
            callable(item)
            for item in (
                workspace_root_resolver,
                output_root_resolver,
                session_validator,
                policy_digest_resolver,
                audit_recorder,
            )
        ):
            raise TypeError("process lifecycle validators must be callable")
        with self._lock:
            self._workspace_root_resolver = workspace_root_resolver
            self._output_root_resolver = output_root_resolver
            self._session_validator = session_validator
            self._policy_digest_resolver = policy_digest_resolver
            self._audit_recorder = audit_recorder
            self._strict_lifecycle = True

    def reset_lifecycle_configuration(self) -> None:
        """Drop App-bound callbacks after orderly App shutdown."""
        with self._lock:
            self._strict_lifecycle = False
            self._workspace_root_resolver = None
            self._output_root_resolver = None
            self._session_validator = None
            self._policy_digest_resolver = None
            self._audit_recorder = None
            self._owner_authorities.clear()

    def _audit_lifecycle(
        self,
        event_type: str,
        session: ProcessSession | None = None,
        *,
        owner_account_id: str = "",
        reason: str = "",
        decision: str = "",
    ) -> None:
        recorder = self._audit_recorder
        if recorder is None:
            if self._strict_lifecycle:
                raise ProcessCheckpointError("process lifecycle audit authority is unavailable")
            return
        stable_reason = str(reason or "")
        if stable_reason and not _SAFE_RECOVERY_CODE_RE.fullmatch(stable_reason):
            stable_reason = "INTERNAL_ERROR"
        payload = {
            "decision": str(decision or ""),
            "event_type": str(event_type),
            "owner_account_id": (
                session.owner_account_id if session is not None else str(owner_account_id or "")
            ),
            "pid": int(session.pid or 0) if session is not None else 0,
            "reason": stable_reason,
            "sandbox_preference": (
                session.sandbox_preference if session is not None else ""
            ),
            "sandbox_system_surface": (
                session.sandbox_system_surface if session is not None else ""
            ),
            "sandboxed": bool(session.sandboxed) if session is not None else False,
            "session_id": session.id if session is not None else "",
            "session_key": session.session_key if session is not None else "",
            "task_id": session.task_id if session is not None else "",
            "workspace_id": session.workspace_id if session is not None else "",
        }
        recorder(payload)

    # ----- 通知队列 -----

    @staticmethod
    def _key(session_key: str, owner_account_id: str = "") -> SessionKey:
        return owner_account_id or "", session_key

    def _enqueue(self, session: ProcessSession, event: dict[str, Any]) -> None:
        with self._pending_lock:
            queue = self._pending.setdefault(self._key(session.session_key, session.owner_account_id), [])
            queue.append(event)
            if len(queue) > MAX_PENDING_PER_SESSION:
                del queue[:-MAX_PENDING_PER_SESSION]

    def drain_for_session(self, session_key: str, owner_account_id: str = "") -> list[dict[str, Any]]:
        """弹出某 session 的全部待通知事件（线程安全）。"""
        with self._pending_lock:
            if not str(owner_account_id or "").strip():
                events: list[dict[str, Any]] = []
                for key in [
                    key for key in self._pending if key[1] == session_key
                ]:
                    events.extend(self._pending.pop(key, []))
                return events
            return self._pending.pop(self._key(session_key, owner_account_id), []) or []

    # ----- 启动 -----

    @staticmethod
    def _child_env(
        owner_account_id: str,
        *,
        managed: bool = False,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the explicit child environment and identify injected secrets."""
        from crew.state.home import managed_runtime_env_overrides, runtime_env_overrides
        from crew.tools.redact import sensitive_env_values

        build_env = managed_runtime_env_overrides if managed else runtime_env_overrides
        values = build_env(owner_account_id=owner_account_id)
        values["PYTHONUNBUFFERED"] = "1"
        return values, tuple(sensitive_env_values(values))

    def _reserve_process_slot(self, owner_account_id: str) -> None:
        """Atomically reserve one running slot so concurrent spawns cannot overrun quotas."""
        from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

        owner = str(owner_account_id).strip()
        with self._lock:
            running = [session for session in self._running.values() if not session.exited]
            global_count = len(running) + self._reserved_global
            owner_count = sum(
                1 for session in running if session.owner_account_id == owner
            ) + self._reserved_by_owner.get(owner, 0)
            if global_count >= MAX_RUNNING_PROCESSES_GLOBAL:
                raise NativeRuntimeError(
                    RuntimeErrorCode.PROCESS_LIMIT_REACHED,
                    "global running process quota reached; retry after a process exits",
                )
            if owner_count >= MAX_RUNNING_PROCESSES_PER_OWNER:
                raise NativeRuntimeError(
                    RuntimeErrorCode.PROCESS_LIMIT_REACHED,
                    "owner running process quota reached; retry after a process exits",
                )
            self._reserved_global += 1
            self._reserved_by_owner[owner] = self._reserved_by_owner.get(owner, 0) + 1

    def _release_process_slot(self, owner_account_id: str) -> None:
        owner = str(owner_account_id).strip()
        with self._lock:
            self._reserved_global = max(0, self._reserved_global - 1)
            remaining = self._reserved_by_owner.get(owner, 0) - 1
            if remaining > 0:
                self._reserved_by_owner[owner] = remaining
            else:
                self._reserved_by_owner.pop(owner, None)

    @staticmethod
    def _standalone_policy_digest(launch: ProcessLaunch) -> str:
        """Hash policy facts without persisting command, environment, or paths."""
        from crew.security.launch import serialize_profile
        from crew.security.policy import serialize_additional_permissions
        from crew.security.snapshot import canonical_json_bytes

        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "additional_permissions": serialize_additional_permissions(
                        launch.additional_permissions
                    ),
                    "profile": serialize_profile(launch.profile),
                    "sandbox_preference": launch.sandbox_preference.value,
                    "sandbox_system_surface": launch.sandbox_system_surface,
                    "sandboxed": launch.sandboxed,
                }
            )
        ).hexdigest()

    def _resolve_workspace_root(self, owner_account_id: str, workspace_id: str) -> Path | None:
        resolver = self._workspace_root_resolver
        if resolver is None:
            return None
        raw = resolver(owner_account_id, workspace_id)
        if raw is None or not str(raw).strip():
            return None
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ProcessCheckpointError("authenticated workspace root is not a directory")
        return root

    def _resolve_output_root(self, owner_account_id: str) -> Path | None:
        resolver = self._output_root_resolver
        if resolver is None:
            return None
        raw = resolver(owner_account_id)
        if raw is None or not str(raw).strip():
            return None
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ProcessCheckpointError("authenticated process output root is not a directory")
        return root

    @staticmethod
    def _path_within_root(path: str | Path, root: Path, *, directory: bool) -> Path:
        candidate = Path(path).expanduser().resolve(strict=directory)
        if directory and not candidate.is_dir():
            raise ProcessCheckpointError("process cwd is not an existing directory")
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ProcessCheckpointError(
                "process path is outside the authenticated workspace"
            ) from exc
        return candidate

    def _validate_checkpoint_scope(
        self,
        *,
        owner_account_id: str,
        workspace_id: str,
        session_key: str,
        task_id: str,
        cwd: str | Path,
        output_ref: str,
    ) -> None:
        if not self._strict_lifecycle:
            return
        validator = self._session_validator
        if validator is None or not validator(
            owner_account_id,
            session_key,
            workspace_id,
            task_id,
        ):
            raise ProcessCheckpointError(
                "process session/workspace ownership cannot be verified"
            )
        root = self._resolve_workspace_root(owner_account_id, workspace_id)
        if root is None:
            raise ProcessCheckpointError(
                "process recovery requires an owner-scoped workspace root"
            )
        self._path_within_root(cwd, root, directory=True)
        if output_ref:
            output_path = Path(output_ref).expanduser().resolve(strict=False)
            output_root = self._resolve_output_root(owner_account_id)
            allowed_roots = tuple(
                candidate for candidate in (root, output_root) if candidate is not None
            )
            allowed = False
            for candidate in allowed_roots:
                try:
                    self._path_within_root(output_path, candidate, directory=False)
                except ProcessCheckpointError:
                    continue
                allowed = True
                break
            if not allowed:
                raise ProcessCheckpointError(
                    "process output reference leaves owner-scoped storage"
                )

    def _policy_digest(
        self,
        launch: ProcessLaunch | None,
        *,
        owner_account_id: str,
        workspace_id: str,
        session_key: str,
        task_id: str,
    ) -> str:
        resolver = self._policy_digest_resolver
        if resolver is not None:
            digest = str(
                resolver(
                    owner_account_id,
                    workspace_id,
                    session_key,
                    task_id,
                )
            ).strip().lower()
        elif launch is not None:
            digest = self._standalone_policy_digest(launch)
        else:
            digest = ""
        if not _HEX_256_RE.fullmatch(digest):
            raise ProcessCheckpointError(
                "process authorization policy generation cannot be verified"
            )
        return digest

    def _authority_for_spawn(self, owner_account_id: str) -> OwnerProcessAuthority:
        owner = str(owner_account_id or "").strip()
        now = time.time()
        with self._lock:
            authority = self._owner_authorities.get(owner)
            if authority is None and not self._strict_lifecycle:
                authority = OwnerProcessAuthority(
                    generation=hashlib.sha256(
                        f"standalone-process-authority:{owner}".encode()
                    ).hexdigest(),
                    expires_at=now + _DEFAULT_STANDALONE_AUTHORITY_TTL_SECONDS,
                )
                self._owner_authorities[owner] = authority
        if (
            authority is None
            or not _HEX_256_RE.fullmatch(authority.generation)
            or not math.isfinite(authority.expires_at)
            or authority.expires_at <= now
        ):
            raise ProcessCheckpointError(
                "authenticated process authorization generation is unavailable or expired"
            )
        return authority

    def _prepare_recovery_metadata(
        self,
        launch: ProcessLaunch | None,
        *,
        owner_account_id: str,
        workspace_id: str,
        session_key: str,
        task_id: str,
        cwd: str | Path,
        output_ref: str,
        authorization_snapshot: Any = None,
    ) -> tuple[OwnerProcessAuthority, str, bool]:
        self._validate_checkpoint_scope(
            owner_account_id=owner_account_id,
            workspace_id=workspace_id,
            session_key=session_key,
            task_id=task_id,
            cwd=cwd,
            output_ref=output_ref,
        )
        authority = self._authority_for_spawn(owner_account_id)
        if launch is None and self._policy_digest_resolver is None:
            snapshot = getattr(authorization_snapshot, "snapshot", None)
            policy_material = {
                "additional_permissions_payload": str(
                    getattr(snapshot, "additional_permissions_payload", "")
                ),
                "profile_payload": str(getattr(snapshot, "profile_payload", "")),
            }
            from crew.security.snapshot import canonical_json_bytes

            policy_digest = hashlib.sha256(
                canonical_json_bytes(policy_material)
            ).hexdigest()
        else:
            policy_digest = self._policy_digest(
                launch,
                owner_account_id=owner_account_id,
                workspace_id=workspace_id,
                session_key=session_key,
                task_id=task_id,
            )
        revalidatable = bool(
            launch is not None
            and launch.additional_permissions.is_empty()
        )
        return authority, policy_digest, revalidatable

    def spawn_local(
        self,
        command: str,
        *,
        launch: ProcessLaunch | None = None,
        launch_argv: tuple[str, ...] | None = None,
        cwd: str | None = None,
        session_key: str = "",
        owner_account_id: str = "",
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = False,
        task_id: str = "",
        output_ref: str = "",
        explicit_environment: dict[str, str] | None = None,
    ) -> ProcessSession:
        """本地后台启动一条命令，立即返回（非阻塞）。

        输出由 daemon reader 线程读入滚动缓冲，可经 poll/log/wait 取回。
        """
        validate_process_launch(launch)
        if (
            launch is None
            or launch.sandboxed
            or launch.profile.kind is not PermissionProfileKind.DISABLED
        ):
            from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                "host execution requires an explicit disabled security launch",
            )
        resolved_owner = str(owner_account_id).strip() or launch.owner_account_id
        resolved_session = str(session_key).strip() or launch.session_id
        resolved_task = str(task_id).strip() or launch.task_id
        try:
            resolved_cwd = str(
                Path(cwd or os.getcwd()).expanduser().resolve(strict=True)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "process cwd is unavailable",
            ) from exc
        (
            authority,
            policy_digest,
            authorization_revalidatable,
        ) = self._prepare_recovery_metadata(
            launch,
            owner_account_id=resolved_owner,
            workspace_id=launch.workspace_id,
            session_key=resolved_session,
            task_id=resolved_task,
            cwd=resolved_cwd,
            output_ref=str(output_ref or ""),
        )
        if explicit_environment is None:
            child_env, secret_values = self._child_env(resolved_owner)
            env = minimal_inherited_environment()
            env.update(child_env)
        else:
            from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
            from crew.tools.redact import sensitive_env_values

            if not isinstance(explicit_environment, dict) or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or "\x00" in name + value
                or "=" in name
                for name, value in explicit_environment.items()
            ):
                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_DENIED,
                    "explicit child environment is invalid",
                )
            env = dict(explicit_environment)
            secret_values = tuple(sensitive_env_values(env))
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            session_key=resolved_session,
            owner_account_id=resolved_owner,
            workspace_id=launch.workspace_id,
            cwd=resolved_cwd,
            started_at=time.time(),
            notify_on_complete=notify_on_complete,
            watch_patterns=list(watch_patterns or []),
            task_id=resolved_task,
            output_ref=output_ref,
            _secret_values=secret_values,
            authorization_generation=authority.generation,
            authorization_expires_at=authority.expires_at,
            authorization_policy_digest=policy_digest,
            authorization_revalidatable=authorization_revalidatable,
            sandbox_preference=launch.sandbox_preference.value,
            sandboxed=launch.sandboxed,
            sandbox_system_surface=launch.sandbox_system_surface,
        )

        popen_args = tuple(launch_argv or shell_argv(command))
        signed_snapshot = finalize_process_launch(
            launch,
            argv=popen_args,
            cwd=session.cwd,
            environment=env,
            expected_owner_account_id=resolved_owner,
            expected_workspace_id=launch.workspace_id,
            expected_session_id=resolved_session,
            expected_task_id=resolved_task,
        )
        snapshot = signed_snapshot.snapshot
        _consume_spawn_authorization(
            signed_snapshot,
            environment=env,
            owner_account_id=resolved_owner,
            workspace_id=launch.workspace_id,
            session_id=resolved_session,
            task_id=resolved_task,
        )
        self._reserve_process_slot(resolved_owner)
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                snapshot.argv,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=snapshot.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=not _IS_WINDOWS,  # 自成进程组，便于整树 kill
                # Windows 进程组仍共享父控制台；taskkill /T /F 可能把控制信号回传给
                # Gateway。后台 shell 无交互控制台需求，创建无窗口独立进程组才能安全整树终止。
                creationflags=_WINDOWS_PROCESS_FLAGS if _IS_WINDOWS else 0,
            )
            identity = _process_identity(proc.pid)
            poll = getattr(proc, "poll", None)
            already_exited = callable(poll) and poll() is not None
            if identity is None and not already_exited:
                terminate_process_tree(proc.pid)
                from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                    "spawned process identity could not be verified",
                )
            session.process = proc
            session.pid = proc.pid
            session.process_identity = identity
            session.authorization_snapshot = signed_snapshot
            session.authorization_digest = signed_snapshot.digest
            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session
        finally:
            self._release_process_slot(resolved_owner)

        try:
            self._write_checkpoint(required=True)
        except ProcessCheckpointError:
            self._abort_uncheckpointed_spawn(session)
            raise
        try:
            self._audit_lifecycle(
                "process_spawn_checkpointed",
                session,
                decision="allow",
            )
        except Exception as exc:
            self._abort_uncheckpointed_spawn(session)
            raise ProcessCheckpointError(
                "process spawn audit could not be persisted"
            ) from exc

        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-reader-{session.id}",
        )
        session._reader_thread = reader
        reader.start()
        if self._task_runtime is not None and resolved_task:
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(session,),
                daemon=True,
                name=f"proc-heartbeat-{session.id}",
            )
            session._heartbeat_thread = heartbeat
            heartbeat.start()

        return session

    def spawn_security(
        self,
        command: str,
        *,
        launch: ProcessLaunch | None = None,
        cwd: str | None = None,
        launch_argv: tuple[str, ...] | None = None,
        **session_options: Any,
    ) -> ProcessSession:
        """Start through the host-owned security decision; managed never uses a user argv."""
        validate_process_launch(launch)
        if not launch.sandboxed:
            return self.spawn_local(
                command,
                launch=launch,
                launch_argv=launch_argv,
                cwd=cwd,
                **session_options,
            )
        owner_account_id = (
            str(session_options.get("owner_account_id") or "").strip()
            or launch.owner_account_id
        )
        session_key = (
            str(session_options.get("session_key") or "").strip()
            or launch.session_id
        )
        session_options["owner_account_id"] = owner_account_id
        session_options["session_key"] = session_key
        if not str(session_options.get("task_id") or "").strip():
            session_options["task_id"] = launch.task_id
        child_env, secret_values = self._child_env(
            owner_account_id,
            managed=True,
        )
        command_argv = tuple(launch_argv or shell_argv(command))
        signed_snapshot = finalize_process_launch(
            launch,
            argv=command_argv,
            cwd=cwd or os.getcwd(),
            environment=child_env,
            expected_owner_account_id=owner_account_id,
            expected_workspace_id=launch.workspace_id,
            expected_session_id=session_key,
            expected_task_id=str(session_options["task_id"]),
        )
        payload = {
            "version": 2,
            **signed_snapshot.to_payload(),
            "snapshot_nonce": signed_snapshot.snapshot.nonce,
            "env_overrides": child_env,
            "timeout": 86400,
            "max_output_bytes": 2 * 1024 * 1024,
        }
        return self._spawn_managed_bridge(
            command,
            payload,
            launch=launch,
            cwd=cwd,
            secret_values=secret_values,
            authorization_snapshot=signed_snapshot,
            workspace_id=launch.workspace_id,
            **session_options,
        )

    def _spawn_managed_bridge(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        launch: ProcessLaunch,
        cwd: str | None,
        session_key: str = "",
        owner_account_id: str = "",
        workspace_id: str = "",
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = False,
        task_id: str = "",
        output_ref: str = "",
        secret_values: tuple[str, ...] = (),
        authorization_snapshot: Any = None,
    ) -> ProcessSession:
        """Launch only the fixed Ace bridge; command is sent through stdin."""
        environment = payload.get("env_overrides")
        if not isinstance(environment, dict):
            from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "managed bridge environment is invalid",
            )
        _consume_spawn_authorization(
            authorization_snapshot,
            environment=environment,
            owner_account_id=owner_account_id,
            workspace_id=workspace_id,
            session_id=session_key,
            task_id=task_id,
        )
        try:
            resolved_cwd = str(
                Path(cwd or os.getcwd()).expanduser().resolve(strict=True)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

            raise NativeRuntimeError(
                RuntimeErrorCode.SANDBOX_DENIED,
                "managed process cwd is unavailable",
            ) from exc
        (
            authority,
            policy_digest,
            authorization_revalidatable,
        ) = self._prepare_recovery_metadata(
            launch,
            owner_account_id=owner_account_id,
            workspace_id=workspace_id,
            session_key=session_key,
            task_id=task_id,
            cwd=resolved_cwd,
            output_ref=str(output_ref or ""),
            authorization_snapshot=authorization_snapshot,
        )
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}", command=command, session_key=session_key,
            owner_account_id=owner_account_id, workspace_id=workspace_id,
            cwd=resolved_cwd, started_at=time.time(),
            notify_on_complete=notify_on_complete, watch_patterns=list(watch_patterns or []),
            task_id=task_id, authorization_digest=str(
                getattr(authorization_snapshot, "digest", "") or ""
            ), output_ref=output_ref, _secret_values=secret_values,
            authorization_snapshot=authorization_snapshot,
            authorization_generation=authority.generation,
            authorization_expires_at=authority.expires_at,
            authorization_policy_digest=policy_digest,
            authorization_revalidatable=authorization_revalidatable,
            sandbox_preference=launch.sandbox_preference.value,
            sandboxed=launch.sandboxed,
            sandbox_system_surface=launch.sandbox_system_surface,
        )
        flags = _WINDOWS_PROCESS_FLAGS if _IS_WINDOWS else 0
        from crew.security.background_runner import BRIDGE_BOOTSTRAP_VERSION
        from crew.security.snapshot import delegate_authorization_snapshot

        bridge_env = {**minimal_inherited_environment(), "PYTHONUNBUFFERED": "1"}
        bridge_key = secrets.token_bytes(32)
        delegated = delegate_authorization_snapshot(
            authorization_snapshot,
            verification_key=bridge_key,
        )
        bridge_payload = {**payload, **delegated.to_payload()}
        bootstrap = {
            "authorization_key": bridge_key.hex(),
            "parent_pid": os.getpid(),
            "payload": bridge_payload,
            "version": BRIDGE_BOOTSTRAP_VERSION,
        }
        self._reserve_process_slot(owner_account_id)
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _BACKGROUND_BRIDGE_LAUNCHER],
                shell=False, text=True, encoding="utf-8", errors="replace", cwd=session.cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=bridge_env,
                start_new_session=not _IS_WINDOWS, creationflags=flags,
            )
            identity = _process_identity(proc.pid)
            poll = getattr(proc, "poll", None)
            already_exited = callable(poll) and poll() is not None
            if identity is None and not already_exited:
                terminate_process_tree(proc.pid)
                from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

                raise NativeRuntimeError(
                    RuntimeErrorCode.SANDBOX_UNAVAILABLE,
                    "managed bridge identity could not be verified",
                )
            session.process = proc
            session.pid = proc.pid
            session.process_identity = identity
            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session
        except BaseException:
            if proc is not None and getattr(proc, "poll", lambda: None)() is None:
                terminate_process_tree(proc.pid)
            raise
        finally:
            self._release_process_slot(owner_account_id)
        try:
            self._write_checkpoint(required=True)
        except ProcessCheckpointError:
            self._abort_uncheckpointed_spawn(session)
            raise
        try:
            self._audit_lifecycle(
                "process_spawn_checkpointed",
                session,
                decision="allow",
            )
        except Exception as exc:
            self._abort_uncheckpointed_spawn(session)
            raise ProcessCheckpointError(
                "managed process spawn audit could not be persisted"
            ) from exc
        try:
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(json.dumps(bootstrap, separators=(",", ":")) + "\n")
            proc.stdin.close()
        except BaseException:
            self._abort_uncheckpointed_spawn(session)
            self._write_checkpoint()
            raise
        reader = threading.Thread(target=self._reader_loop, args=(session,), daemon=True, name=f"proc-reader-{session.id}")
        session._reader_thread = reader
        reader.start()
        return session

    def _heartbeat_loop(self, session: ProcessSession) -> None:
        interval = float(getattr(self._task_runtime, "heartbeat_interval", 10.0))
        while not session.exited:
            time.sleep(max(0.1, interval))
            if session.exited:
                return
            try:
                self._task_runtime.heartbeat(session.task_id)
            except Exception:  # noqa: BLE001 - heartbeat backend failure ends this loop
                return

    # ----- reader 线程 -----

    @staticmethod
    def _redact_private_output(
        output: str,
        secret_values: tuple[str, ...],
        *,
        truncated: bool,
        max_output_chars: int,
    ) -> str:
        """Expose only complete, redacted output after a secret-bearing child exits."""
        from crew.tools.redact import redact_secret_values, redact_sensitive_text

        if truncated:
            # The rolling buffer can begin inside a secret. Drop one maximum-secret
            # width, then move past any complete secret crossing that cut.
            start = max(len(value) for value in secret_values) - 1
            while True:
                advanced = start
                for value in secret_values:
                    offset = output.find(value)
                    while offset >= 0:
                        end = offset + len(value)
                        if offset < start < end:
                            advanced = max(advanced, end)
                        offset = output.find(value, offset + 1)
                if advanced == start:
                    break
                start = advanced
            output = output[start:]
        redacted = redact_secret_values(output, secret_values)
        return redact_sensitive_text(redacted, force=True)[-max_output_chars:]

    @staticmethod
    def _safe_display(value: object, secret_values: tuple[str, ...] = ()) -> str:
        """Redact explicit task secrets and generic credentials at display boundaries."""
        from crew.tools.redact import redact_secret_values, redact_sensitive_text

        text = redact_secret_values(str(value), secret_values) or ""
        return redact_sensitive_text(text, force=True)

    def _safe_session_text(self, session: ProcessSession, value: object) -> str:
        return self._safe_display(value, session._secret_values)

    def _publish_output(self, session: ProcessSession, chunk: str) -> None:
        """Publish already-safe output to every observer of a process session."""
        with session._lock:
            session.output_buffer += chunk
            if len(session.output_buffer) > session.max_output_chars:
                session.output_buffer = session.output_buffer[-session.max_output_chars:]
            output_chars = len(session.output_buffer)
            output_tail = strip_ansi(session.output_buffer[-1000:])
        if session.output_ref and not session._output_ref_disabled:
            try:
                from crew.tools.file_utils import atomic_replace_bytes, snapshot_file

                path = Path(session.output_ref)
                expected = snapshot_file(path, max_bytes=MAX_OUTPUT_REF_BYTES)
                combined = expected.data + chunk.encode("utf-8")
                if len(combined) > MAX_OUTPUT_REF_BYTES:
                    tail_budget = MAX_OUTPUT_REF_BYTES - len(_OUTPUT_REF_TRUNCATED)
                    tail = combined[-tail_budget:].decode("utf-8", errors="ignore").encode("utf-8")
                    combined = _OUTPUT_REF_TRUNCATED + tail
                atomic_replace_bytes(
                    path,
                    combined,
                    expected,
                    max_bytes=MAX_OUTPUT_REF_BYTES,
                )
            except Exception:  # noqa: BLE001 - unsafe output target disables persistence
                session._output_ref_disabled = True
                with session._lock:
                    marker = "\n[ACE_OUTPUT_REF_DISABLED]\n"
                    session.output_buffer = (
                        session.output_buffer + marker
                    )[-session.max_output_chars:]
        if self._task_runtime is not None and session.task_id:
            try:
                self._task_runtime.touch_activity(
                    session.task_id,
                    {
                        "pid": session.pid,
                        "output_chars": output_chars,
                        "output_tail": output_tail,
                    },
                )
            except Exception:  # noqa: BLE001, S110 - observer failure cannot stop capture
                pass
        self._check_watch_patterns(session, chunk)

    def _reader_loop(self, session: ProcessSession) -> None:
        """daemon 线程：持续读取 Popen stdout 到滚动缓冲。

        用 buffer.read1(4096) + 增量 UTF-8 解码器，而不是文本模式 read(4096)——后者会
        阻塞到填满 4096 字符或 EOF，小增量输出（如 `for i ...; do echo $i; sleep`）期间
        reader 一直阻塞、output_buffer 不增长，前台 terminal 的 onProgress 进度流就只能
        在进程退出时一次性吐全部（对齐 OCC Stage5 onProgress 需要 line-level 实时增量）。
        read1 有数据即返回，增量解码器正确拼接跨 chunk 的多字节字符（含 emoji）。
        """
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        private_output = ""
        private_output_truncated = False
        private_limit = session.max_output_chars
        if session._secret_values:
            private_limit += max(len(value) for value in session._secret_values) - 1
        try:
            while True:
                # buffer 在 text-mode Popen 上仍可用（底层 BufferedReader）；非 text 模式直接 read1
                buf = getattr(session.process.stdout, "buffer", None)
                if buf is not None:
                    raw = buf.read1(4096)
                else:  # 兜底：非 text 模式（理论不会走到，Popen 用了 text=True）
                    raw = session.process.stdout.read1(4096)
                if not raw:
                    # 输入未就绪也可能返回空，需检查 EOF
                    if session.process.stdout.at_eof() if hasattr(session.process.stdout, "at_eof") else True:
                        break
                    # 部分 BufferedReader.read1 在无数据时返回 b'' 表示 EOF
                    try:
                        if session.process.stdout.closed:
                            break
                    except Exception:  # noqa: BLE001, S110 - optional stream probe
                        pass
                    # 真正的 EOF：再读一次确认
                    if not raw:
                        break
                    continue
                chunk = decoder.decode(raw)
                if not chunk:
                    continue  # 多字节字符跨 chunk，等下一片再输出
                if session._secret_values:
                    private_output += chunk
                    if len(private_output) > private_limit:
                        private_output = private_output[-private_limit:]
                        private_output_truncated = True
                else:
                    self._publish_output(session, chunk)
        except Exception:  # noqa: BLE001, S110 - reader 异常按读取结束处理
            pass
        finally:
            try:
                # flush 增量解码器尾部（无尾字节，防御性）
                tail = decoder.decode(b"", final=True)
                if tail:
                    if session._secret_values:
                        private_output += tail
                        if len(private_output) > private_limit:
                            private_output = private_output[-private_limit:]
                            private_output_truncated = True
                    else:
                        self._publish_output(session, tail)
            except Exception:  # noqa: BLE001, S110 - defensive decoder flush
                pass
            if session._secret_values:
                safe_output = self._redact_private_output(
                    private_output,
                    session._secret_values,
                    truncated=private_output_truncated,
                    max_output_chars=session.max_output_chars,
                )
                if safe_output:
                    self._publish_output(session, safe_output)
            try:
                session.process.wait(timeout=5)
            except Exception:  # noqa: BLE001, S110 - process may already be reaped
                pass
            session.exited = True
            session.exit_code = session.process.returncode
            self._move_to_finished(session)

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """扫描新输出中的 watch_patterns 并按限流排队通知。

        单 session 限流：每 WATCH_MIN_INTERVAL_SECONDS 至多发一条。冷却窗口内的命中
        丢弃并对该窗口记一次 strike；连续 WATCH_STRIKE_LIMIT 个 strike 后禁用 watch
        并降级为 notify_on_complete。
        """
        if not session.watch_patterns or session._watch_disabled or session.exited:
            return

        matched_lines: list[str] = []
        matched_pattern: str | None = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break
        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        with session._lock:
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                # 冷却窗口内：丢弃 + 记 strike（每窗口仅记一次）
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        session.notify_on_complete = True
                        should_disable = True
                emit = False
                suppressed = 0
            else:
                # 冷却已过：上一个窗口若无丢弃则重置 strike 计数
                if session._watch_cooldown_until and not session._watch_strike_candidate:
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                emit = True

        if not emit:
            if should_disable:
                self._enqueue(session, {
                    "type": "watch_disabled",
                    "session_id": session.id,
                    "command": session.command,
                    "message": (
                        f"进程 {session.id} 的 watch_patterns 已禁用："
                        f"连续 {WATCH_STRIKE_LIMIT} 个限流窗口触发（最小间隔 "
                        f"{WATCH_MIN_INTERVAL_SECONDS}s）。已降级为 notify_on_complete，"
                        f"进程退出时会发一条完成通知。"
                    ),
                })
            return

        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"
        self._enqueue(session, {
            "type": "watch_match",
            "session_id": session.id,
            "command": self._safe_session_text(session, session.command),
            "pattern": self._safe_session_text(session, matched_pattern),
            "output": self._safe_session_text(session, output),
            "suppressed": suppressed,
        })

    def _move_to_finished(self, session: ProcessSession) -> None:
        """把 session 从 running 移到 finished（幂等：重复调用不重复发通知）。"""
        with self._lock:
            was_running = session.id in self._running
            if was_running:
                try:
                    self._audit_lifecycle(
                        "process_exited",
                        session,
                        decision="complete",
                    )
                except Exception as exc:  # noqa: BLE001 - callback failure is fenced
                    audit_error: Exception | None = exc
                    session.exited = False
                    session.recovery_state = "cleanup_pending"
                    session.cleanup_reason = "AUDIT_WRITE_FAILED"
                    session.cleanup_attempts += 1
                else:
                    audit_error = None
                    self._running.pop(session.id, None)
                    self._finished[session.id] = session
            else:
                audit_error = None
        if audit_error is not None:
            log.error(
                "process exit audit failed; retaining durable tombstone: %s",
                type(audit_error).__name__,
            )
            try:
                self._write_checkpoint(required=True)
            except ProcessCheckpointError:
                log.exception("process exit audit tombstone could not be persisted")
            return
        if not was_running:
            return
        self._write_checkpoint()

        if session.notify_on_complete:
            output_tail = strip_ansi(session.output_buffer[-2000:]) if session.output_buffer else ""
            self._enqueue(session, {
                "type": "completion",
                "session_id": session.id,
                "command": self._safe_session_text(session, session.command),
                "exit_code": session.exit_code,
                "output": self._safe_session_text(session, output_tail),
            })
        if self._task_runtime is not None and session.task_id:
            try:
                status = "completed" if session.exit_code == 0 else "failed"
                self._task_runtime.finish(
                    session.task_id,
                    owner_account_id=session.owner_account_id,
                    status=status,
                    result=strip_ansi(session.output_buffer[-2000:]),
                    error="" if status == "completed" else f"进程退出码 {session.exit_code}",
                    progress={
                        "pid": session.pid,
                        "exit_code": session.exit_code,
                        "output_chars": len(session.output_buffer),
                        "output_tail": strip_ansi(session.output_buffer[-1000:]),
                    },
                )
            except Exception:  # noqa: BLE001, S110 - observer failure is non-fatal
                pass

    # ----- 查询 -----

    def get(self, session_id: str, owner_account_id: str = "") -> ProcessSession | None:
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        session = self._refresh_session(session)
        if session is None or not _owner_matches(session, owner_account_id):
            return None
        if session.recovery_state == "cleanup_pending" and not session.exited:
            return None
        if (
            self._strict_lifecycle
            and not session.exited
            and session.authorization_expires_at <= time.time()
        ):
            self._attempt_cleanup(session, "AUTHORIZATION_EXPIRED")
            return None
        return session

    def _refresh_session(self, session: ProcessSession | None) -> ProcessSession | None:
        if session is None or session.exited:
            return session
        if session.detached:
            return self._refresh_detached(session)
        process = session.process
        if process is None or process.poll() is None:
            return session
        reader = session._reader_thread
        if (
            reader is not None
            and reader is not threading.current_thread()
            and reader.is_alive()
        ):
            reader.join(timeout=0.1)
        with session._lock:
            if session.exited:
                return session
            session.exited = True
            session.exit_code = process.returncode
        self._move_to_finished(session)
        return session

    def _refresh_detached(self, session: ProcessSession | None) -> ProcessSession | None:
        """崩溃恢复认领的进程没有 Popen 句柄，靠 host PID 存活检测判定是否已退出。"""
        if session is None or session.exited or not session.detached:
            return session
        actual_identity = _process_identity(session.pid)
        if _process_identity_matches(session.process_identity, actual_identity):
            return session
        if actual_identity is not None:
            session.identity_mismatch = True
            return session
        with session._lock:
            if session.exited:
                return session
            session.exited = True
            session.exit_code = None  # 无句柄可 wait，真实退出码不可得
        self._move_to_finished(session)
        return session

    def poll(self, session_id: str, owner_account_id: str = "") -> dict[str, Any]:
        """查看状态 + 最近输出预览。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        with session._lock:
            preview = strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""
        result: dict[str, Any] = {
            "session_id": session.id,
            "command": self._safe_session_text(session, session.command),
            "status": (
                "exited"
                if session.exited
                else "cleanup_pending"
                if session.recovery_state == "cleanup_pending"
                else "identity_mismatch"
                if session.identity_mismatch
                else "running"
            ),
            "pid": session.pid,
            "task_id": session.task_id,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": self._safe_session_text(session, preview),
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        if session.detached:
            result["detached"] = True
            result["note"] = "进程为重启后认领，无输出历史，仅能查状态/kill"
        return result

    def read_log(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 200,
        *,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """读取完整输出日志（按行分页）。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        with session._lock:
            full_output = strip_ansi(session.output_buffer)
        full_output = self._safe_session_text(session, full_output)
        lines = full_output.splitlines()
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset:offset + limit]
        result: dict[str, Any] = {
            "session_id": session.id,
            "task_id": session.task_id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": len(lines),
            "showing": f"{len(selected)} lines",
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        return result

    def wait(self, session_id: str, timeout: int | None = None, *, owner_account_id: str = "") -> dict[str, Any]:
        """阻塞直到进程退出或超时。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        effective_timeout = float(timeout) if timeout and timeout > 0 else 180.0
        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            self._refresh_session(session)
            if session.exited:
                return {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": self._safe_session_text(
                        session,
                        strip_ansi(session.output_buffer[-2000:]),
                    ),
                }
            time.sleep(0.5)
        return {
            "status": "timeout",
            "output": self._safe_session_text(
                session,
                strip_ansi(session.output_buffer[-1000:]),
            ),
            "timeout_note": f"已等待 {int(effective_timeout)}s，进程仍在运行",
        }

    def kill_process(self, session_id: str, owner_account_id: str = "") -> dict[str, Any]:
        """Terminate through a durable write-ahead tombstone and identity check."""
        owner = str(owner_account_id or "").strip()
        if not owner:
            return {
                "status": "forbidden",
                "error": "process termination requires an explicit owner scope",
            }
        session = self.get(session_id, owner_account_id=owner)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}
        actual_identity = _process_identity(session.pid)
        if not _process_identity_matches(session.process_identity, actual_identity):
            if actual_identity is not None:
                self._attempt_cleanup(
                    session,
                    "PID_IDENTITY_CHANGED",
                    expose_finished=False,
                )
                return {
                    "status": "identity_mismatch",
                    "session_id": session.id,
                    "error": "PID identity changed; process was not signalled",
                }
            self._attempt_cleanup(
                session,
                "PROCESS_ALREADY_EXITED",
                expose_finished=True,
            )
            return {"status": "already_exited", "exit_code": None}
        if self._attempt_cleanup(
            session,
            "USER_TERMINATED",
            expose_finished=True,
        ):
            return {"status": "killed", "session_id": session.id}
        return {
            "status": "cleanup_pending",
            "session_id": session.id,
            "error": "process termination remains durably fenced for retry",
        }

    def list_sessions(self, owner_account_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())
        all_sessions = [
            refreshed
            for s in all_sessions
            if (refreshed := self._refresh_session(s)) is not None
            and _owner_matches(refreshed, owner_account_id)
        ]
        result = []
        for s in all_sessions:
            entry: dict[str, Any] = {
                "session_id": s.id,
                "command": self._safe_session_text(s, s.command[:200]),
                "cwd": self._safe_session_text(s, s.cwd or ""),
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": (
                    "exited"
                    if s.exited
                    else "cleanup_pending"
                    if s.recovery_state == "cleanup_pending"
                    else "running"
                ),
                "output_preview": self._safe_session_text(
                    s,
                    s.output_buffer[-200:] if s.output_buffer else "",
                ),
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    def count_running(self) -> int:
        try:
            return len(self._running)
        except Exception:  # noqa: BLE001
            return 0

    # ----- 清理 -----

    def _prune_if_needed(self) -> None:
        """淘汰过期 / 超量的已结束 session。须持有 self._lock。"""
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest]

    # ----- 崩溃恢复 checkpoint -----

    def _abort_uncheckpointed_spawn(self, session: ProcessSession) -> None:
        """Stop a just-created process that cannot be made crash-recoverable."""
        try:
            if session.process_identity is None or not session.pid:
                process = session.process
                if process is not None and process.poll() is None:
                    raise ProcessCheckpointError(
                        "uncheckpointed process has no verifiable identity"
                    )
            else:
                _terminate_verified_process_tree(
                    session.pid,
                    session.process_identity,
                )
        except Exception as exc:
            with self._lock:
                session.recovery_state = "cleanup_pending"
                session.cleanup_reason = "CHECKPOINT_WRITE_FAILED"
                session.cleanup_attempts += 1
                self._running[session.id] = session
            try:
                self._write_checkpoint(required=True)
            except ProcessCheckpointError:
                pass
            raise ProcessCleanupError(
                "uncheckpointed process could not be safely terminated"
            ) from exc
        with self._lock:
            self._running.pop(session.id, None)
            self._frozen.pop(session.id, None)
            session.exited = True
            session.exit_code = -1
        try:
            self._write_checkpoint(required=True)
        except ProcessCheckpointError:
            # The target identity is already gone. A stale signed entry remains
            # fail-closed and will be retired as dead on the next recovery.
            log.exception("terminated uncheckpointed spawn index could not be retired")

    @staticmethod
    def _checkpoint_entry(session: ProcessSession) -> dict[str, Any]:
        if session.process_identity is None or not session.pid:
            raise ProcessCheckpointError("live process lacks complete recovery identity")
        if (
            not session.owner_account_id
            or not session.workspace_id
            or not session.session_key
            or not _HEX_256_RE.fullmatch(session.authorization_digest)
            or not _HEX_256_RE.fullmatch(session.authorization_generation)
            or not _HEX_256_RE.fullmatch(session.authorization_policy_digest)
            or not math.isfinite(float(session.authorization_expires_at))
            or session.authorization_expires_at <= 0
            or not isinstance(session.authorization_revalidatable, bool)
            or not isinstance(session.sandboxed, bool)
            or session.sandbox_preference
            not in {preference.value for preference in SandboxablePreference}
            or session.recovery_state not in _RECOVERY_STATES
        ):
            raise ProcessCheckpointError("live process lacks complete recovery authority")
        if session.sandbox_preference == SandboxablePreference.FORBID.value:
            if (
                session.sandboxed
                or session.sandbox_system_surface
                not in HOST_FIXED_SANDBOX_FORBID_SURFACES
            ):
                raise ProcessCheckpointError(
                    "live process has invalid sandbox FORBID authority"
                )
        elif (
            session.sandbox_system_surface
            or (
                session.sandbox_preference
                == SandboxablePreference.REQUIRE.value
                and not session.sandboxed
            )
        ):
            raise ProcessCheckpointError("live process has invalid sandbox authority")
        reason = str(session.cleanup_reason or "")
        attempts = int(session.cleanup_attempts)
        if session.recovery_state == "live":
            reason = ""
            attempts = 0
        elif not _SAFE_RECOVERY_CODE_RE.fullmatch(reason):
            raise ProcessCheckpointError("cleanup tombstone reason is invalid")
        return {
            "authorization_digest": session.authorization_digest,
            "authorization_expires_at": float(session.authorization_expires_at),
            "authorization_generation": session.authorization_generation,
            "authorization_policy_digest": session.authorization_policy_digest,
            "authorization_revalidatable": session.authorization_revalidatable,
            "cleanup_attempts": attempts,
            "cleanup_reason": reason,
            "notify_on_complete": bool(session.notify_on_complete),
            "owner_account_id": str(session.owner_account_id),
            "pid": int(session.pid),
            "process_identity": RecoveryProcessIdentity.from_process_identity(
                session.process_identity
            ).to_payload(),
            "recovery_state": session.recovery_state,
            "sandbox_preference": session.sandbox_preference,
            "sandbox_system_surface": session.sandbox_system_surface,
            "sandboxed": session.sandboxed,
            "session_id": str(session.id),
            "session_key": str(session.session_key),
            "started_at": float(session.started_at),
            "task_id": str(session.task_id),
            "workspace_id": str(session.workspace_id),
        }

    def _write_checkpoint(self, *, required: bool = False) -> None:
        """Serialize one exact, signed, atomic and fsynced lifecycle snapshot."""
        try:
            with _CHECKPOINT_WRITE_LOCK:
                if self._checkpoint_fenced_reason:
                    raise ProcessCheckpointError(
                        "process checkpoint is fenced after unverifiable state"
                    )
                with self._lock:
                    sessions_by_id = {
                        **self._frozen,
                        **self._running,
                    }
                    sessions = sorted(
                        (
                            session
                            for session in sessions_by_id.values()
                            if not session.exited
                        ),
                        key=lambda item: item.id,
                    )
                    entries: list[dict[str, Any]] = []
                    for session in sessions:
                        try:
                            entries.append(self._checkpoint_entry(session))
                        except ProcessCheckpointError:
                            process = session.process
                            poll = getattr(process, "poll", None)
                            already_exited = callable(poll) and poll() is not None
                            if required and not already_exited:
                                raise
                payload = {
                    "boot_id": _host_boot_id(),
                    "entries": entries,
                    "schema": PROCESS_CHECKPOINT_SCHEMA,
                    "version": PROCESS_CHECKPOINT_VERSION,
                }
                document = {**payload, "mac": _checkpoint_mac(payload)}
                _write_checkpoint_document(_checkpoint_path(), document)
        except ProcessCheckpointError:
            log.error("process checkpoint update failed: ProcessCheckpointError")
            if required:
                raise
        except Exception as exc:
            log.error("process checkpoint update failed: %s", type(exc).__name__)
            if required:
                raise ProcessCheckpointError(
                    "process recovery checkpoint could not be persisted"
                ) from exc

    def _fence_checkpoint(self, reason: str) -> None:
        stable_reason = (
            reason if _SAFE_RECOVERY_CODE_RE.fullmatch(reason) else "CHECKPOINT_INVALID"
        )
        self._checkpoint_fenced_reason = stable_reason
        try:
            self._audit_lifecycle(
                "process_checkpoint_rejected",
                owner_account_id="",
                reason=stable_reason,
                decision="deny",
            )
        except Exception as exc:
            log.exception("process checkpoint rejection audit failed")
            if self._strict_lifecycle:
                raise ProcessCheckpointError(
                    "process checkpoint rejection could not be durably audited"
                ) from exc

    def _begin_cleanup(self, session: ProcessSession, reason: str) -> None:
        if not _SAFE_RECOVERY_CODE_RE.fullmatch(reason):
            raise ValueError("process cleanup reason must be a stable code")
        with self._lock:
            session.recovery_state = "cleanup_pending"
            session.cleanup_reason = reason
            session.cleanup_attempts += 1
        self._write_checkpoint(required=True)
        self._audit_lifecycle(
            "process_cleanup_fenced",
            session,
            reason=reason,
            decision="deny",
        )

    def _finish_cleanup_index(
        self,
        session: ProcessSession,
        *,
        expose_finished: bool,
    ) -> None:
        with self._lock:
            self._running.pop(session.id, None)
            self._frozen.pop(session.id, None)
            session.exited = True
            session.exit_code = -15
            session.frozen = False
            if expose_finished:
                self._finished[session.id] = session
        try:
            self._write_checkpoint(required=True)
        except ProcessCheckpointError:
            # The write-ahead tombstone remains durable and will be retired on
            # the next retry/startup after the now-dead identity is observed.
            log.error("terminated process cleanup tombstone could not be retired")

    def _attempt_cleanup(
        self,
        session: ProcessSession,
        reason: str,
        *,
        expose_finished: bool = False,
    ) -> bool:
        try:
            self._begin_cleanup(session, reason)
        except Exception:
            log.exception("process cleanup fence could not be persisted")
            return False
        actual = _process_identity(session.pid)
        if actual is not None and not _process_identity_matches(
            session.process_identity,
            actual,
        ):
            session.identity_mismatch = True
            try:
                self._audit_lifecycle(
                    "process_cleanup_identity_changed",
                    session,
                    reason="PID_IDENTITY_CHANGED",
                    decision="deny",
                )
            except Exception:
                log.exception("process identity-change audit failed")
                return False
            self._finish_cleanup_index(session, expose_finished=False)
            return True
        try:
            if actual is not None:
                if session.process_identity is None or not session.pid:
                    raise ProcessCheckpointError(
                        "cleanup target has no verifiable process identity"
                    )
                _terminate_verified_process_tree(
                    session.pid,
                    session.process_identity,
                )
            if session.cleanup_reason == "AUDIT_WRITE_FAILED":
                self._audit_lifecycle(
                    "process_exited",
                    session,
                    decision="complete",
                )
            elif not session.exited:
                self._audit_lifecycle(
                    "process_exited",
                    session,
                    decision="terminated",
                )
            self._audit_lifecycle(
                "process_cleanup_completed",
                session,
                reason=reason,
                decision="deny",
            )
        except Exception:
            log.exception("identity-pinned process cleanup remains pending")
            try:
                self._audit_lifecycle(
                    "process_cleanup_failed",
                    session,
                    reason=reason,
                    decision="deny",
                )
            except Exception:
                log.exception("process cleanup failure audit failed")
            return False
        self._finish_cleanup_index(session, expose_finished=expose_finished)
        return True

    def retry_pending_cleanup(self) -> int:
        """Retry every durable cleanup tombstone, retaining failures."""
        with self._lock:
            pending = [
                session
                for session in {**self._frozen, **self._running}.values()
                if session.recovery_state == "cleanup_pending"
            ]
        cleaned = 0
        for session in pending:
            if self._attempt_cleanup(
                session,
                session.cleanup_reason or "CLEANUP_RETRY",
            ):
                cleaned += 1
        return cleaned

    def reap_expired_authorizations(self, *, now: float | None = None) -> int:
        """Terminate expired live/frozen generations through write-ahead tombstones."""
        current = time.time() if now is None else float(now)
        with self._lock:
            expired = [
                session
                for session in {**self._frozen, **self._running}.values()
                if not session.exited and session.authorization_expires_at <= current
            ]
        cleaned = 0
        for session in expired:
            if self._attempt_cleanup(session, "AUTHORIZATION_EXPIRED"):
                cleaned += 1
        return cleaned

    def revoke_owner(self, owner_account_id: str, *, reason: str = "OWNER_REVOKED") -> int:
        """Fence one owner generation and terminate every identity-pinned process."""
        owner = str(owner_account_id or "").strip()
        if not owner:
            return 0
        with self._lock:
            self._owner_authorities.pop(owner, None)
            sessions = [
                session
                for session in {**self._frozen, **self._running}.values()
                if session.owner_account_id == owner and not session.exited
            ]
        cleaned = 0
        for session in sessions:
            if self._attempt_cleanup(session, reason):
                cleaned += 1
        pending = len(sessions) - cleaned
        if pending:
            raise ProcessCleanupError(
                f"{pending} process cleanup tombstone(s) remain for revoked owner"
            )
        return cleaned

    def revoke_session(
        self,
        owner_account_id: str,
        session_key: str,
        *,
        reason: str = "SESSION_REVOKED",
    ) -> int:
        """Terminate one owner/session scope while retaining failed tombstones."""
        owner = str(owner_account_id or "").strip()
        session = str(session_key or "").strip()
        if not owner or not session:
            return 0
        with self._lock:
            sessions = [
                item
                for item in {**self._frozen, **self._running}.values()
                if item.owner_account_id == owner
                and item.session_key == session
                and not item.exited
            ]
        cleaned = 0
        for item in sessions:
            if self._attempt_cleanup(item, reason):
                cleaned += 1
        pending = len(sessions) - cleaned
        if pending:
            raise ProcessCleanupError(
                f"{pending} process cleanup tombstone(s) remain for revoked session"
            )
        return cleaned

    def shutdown_processes(self) -> int:
        """Explicit graceful-quit policy: terminate all indexed live processes."""
        with self._lock:
            owners = {
                session.owner_account_id
                for session in {**self._frozen, **self._running}.values()
                if session.owner_account_id and not session.exited
            }
        cleaned = 0
        failures = 0
        for owner in owners:
            try:
                cleaned += self.revoke_owner(owner, reason="GATEWAY_SHUTDOWN")
            except ProcessCleanupError:
                failures += 1
        if failures:
            raise ProcessCleanupError(
                "process cleanup remains fenced after Gateway shutdown"
            )
        return cleaned

    def _revalidation_result(
        self,
        session: ProcessSession,
        *,
        owner_account_id: str,
        authorization_generation: str,
        now: float,
        recovered: bool,
    ) -> tuple[str, Path | None]:
        """Return a stable denial and current workspace root for one live identity."""
        if session.authorization_expires_at <= now:
            return "AUTHORIZATION_EXPIRED", None
        if session.authorization_generation != authorization_generation:
            return "AUTHORIZATION_GENERATION_REVOKED", None

        validator = self._session_validator
        if self._strict_lifecycle:
            try:
                valid_session = bool(
                    validator is not None
                    and validator(
                        owner_account_id,
                        session.session_key,
                        session.workspace_id,
                        session.task_id,
                    )
                )
            except Exception:  # noqa: BLE001 - callback failure is fail-closed
                return "SESSION_UNVERIFIABLE", None
            if not valid_session:
                return "SESSION_OWNERSHIP_REVOKED", None
        try:
            root = self._resolve_workspace_root(
                owner_account_id,
                session.workspace_id,
            )
        except Exception:  # noqa: BLE001 - resolver failure is fail-closed
            return "WORKSPACE_UNVERIFIABLE", None
        if self._strict_lifecycle and root is None:
            return "WORKSPACE_UNVERIFIABLE", None
        if not recovered and root is not None:
            try:
                if session.cwd is None:
                    raise ProcessCheckpointError("process cwd is unavailable")
                self._path_within_root(session.cwd, root, directory=True)
                if session.output_ref:
                    output_root = self._resolve_output_root(owner_account_id)
                    output_allowed = False
                    for candidate in tuple(
                        item for item in (root, output_root) if item is not None
                    ):
                        try:
                            self._path_within_root(
                                session.output_ref,
                                candidate,
                                directory=False,
                            )
                        except ProcessCheckpointError:
                            continue
                        output_allowed = True
                        break
                    if not output_allowed:
                        raise ProcessCheckpointError(
                            "process output reference is no longer owner-scoped"
                        )
            except Exception:  # noqa: BLE001 - any scope ambiguity revokes authority
                return "WORKSPACE_SCOPE_REVOKED", root
        try:
            policy_digest = (
                session.authorization_policy_digest
                if not self._strict_lifecycle
                and self._policy_digest_resolver is None
                else self._policy_digest(
                    None,
                    owner_account_id=owner_account_id,
                    workspace_id=session.workspace_id,
                    session_key=session.session_key,
                    task_id=session.task_id,
                )
            )
        except Exception:  # noqa: BLE001 - policy resolver failure is fail-closed
            return "POLICY_UNVERIFIABLE", root
        if not hmac.compare_digest(
            session.authorization_policy_digest,
            policy_digest,
        ):
            return "POLICY_GENERATION_REVOKED", root
        if not _process_identity_matches(
            session.process_identity,
            _process_identity(session.pid),
        ):
            return "PID_IDENTITY_CHANGED", root
        return "", root

    def activate_owner(
        self,
        owner_account_id: str,
        *,
        authorization_generation: str,
        authorization_expires_at: float,
        now: float | None = None,
    ) -> dict[str, int]:
        """Activate only frozen entries proven against current authenticated facts."""
        owner = str(owner_account_id or "").strip()
        generation = str(authorization_generation or "").strip().lower()
        try:
            expires_at = float(authorization_expires_at)
        except (TypeError, ValueError):
            expires_at = float("nan")
        current = time.time() if now is None else float(now)
        if (
            not owner
            or not _HEX_256_RE.fullmatch(generation)
            or not math.isfinite(expires_at)
            or expires_at <= current
        ):
            with self._lock:
                candidates = [
                    session
                    for session in self._frozen.values()
                    if session.owner_account_id == owner
                ]
            cleaned = 0
            for session in candidates:
                if self._attempt_cleanup(session, "AUTHENTICATION_EXPIRED"):
                    cleaned += 1
            return {
                "activated": 0,
                "cleaned": cleaned,
                "frozen": len(candidates) - cleaned,
            }

        authority = OwnerProcessAuthority(generation, expires_at)
        with self._lock:
            self._owner_authorities[owner] = authority
            active = [
                session
                for session in self._running.values()
                if session.owner_account_id == owner and not session.exited
            ]
            frozen = [
                session
                for session in self._frozen.values()
                if session.owner_account_id == owner and not session.exited
            ]
            foreign = [
                session
                for session in {**self._frozen, **self._running}.values()
                if session.owner_account_id != owner and not session.exited
            ]
        if not active and not frozen and not foreign:
            return {"activated": 0, "cleaned": 0, "frozen": 0}

        cleaned = 0
        for session in foreign:
            if self._attempt_cleanup(session, "OWNER_NOT_ACTIVE"):
                cleaned += 1
        for session in active:
            reason, _root = self._revalidation_result(
                session,
                owner_account_id=owner,
                authorization_generation=generation,
                now=current,
                recovered=False,
            )
            if reason:
                if self._attempt_cleanup(session, reason):
                    cleaned += 1
                continue
            session.authorization_expires_at = min(
                session.authorization_expires_at,
                expires_at,
            )

        activated = 0
        for session in frozen:
            reason, root = self._revalidation_result(
                session,
                owner_account_id=owner,
                authorization_generation=generation,
                now=current,
                recovered=True,
            )
            if reason:
                if self._attempt_cleanup(session, reason):
                    cleaned += 1
                continue

            with self._lock:
                running = [
                    item for item in self._running.values() if not item.exited
                ]
                quota_available = (
                    len(running) < MAX_RUNNING_PROCESSES_GLOBAL
                    and sum(item.owner_account_id == owner for item in running)
                    < MAX_RUNNING_PROCESSES_PER_OWNER
                )
            if not quota_available:
                continue
            session.cwd = str(root) if root is not None else None
            session.output_ref = ""
            session.authorization_expires_at = min(
                session.authorization_expires_at,
                expires_at,
            )
            try:
                self._audit_lifecycle(
                    "process_recovery_activated",
                    session,
                    decision="allow",
                )
            except Exception:  # noqa: BLE001 - audit durability gates activation
                if self._attempt_cleanup(session, "AUDIT_WRITE_FAILED"):
                    cleaned += 1
                continue
            with self._lock:
                self._frozen.pop(session.id, None)
                session.frozen = False
                self._running[session.id] = session
            activated += 1

        try:
            self._write_checkpoint(required=True)
        except ProcessCheckpointError:
            # Activated sessions remain generation-fenced in memory; the old
            # signed checkpoint is conservative and expires no later.
            log.exception("activated process authority refresh was not checkpointed")
        with self._lock:
            remaining = sum(
                session.owner_account_id == owner for session in self._frozen.values()
            )
        return {"activated": activated, "cleaned": cleaned, "frozen": remaining}

    def recover_from_checkpoint(
        self,
        owner_account_id: str = "",
        *,
        session_key: str = "",
        workspace_id: str = "",
    ) -> int:
        """Stage signed live entries as frozen; never expose control before auth."""
        path = _checkpoint_path()
        if not path.exists():
            return 0
        rejection_reason = "CHECKPOINT_INVALID"
        try:
            with _CHECKPOINT_WRITE_LOCK:
                document = _read_checkpoint_document(path)
                if (
                    isinstance(document, dict)
                    and (
                        document.get("schema") != PROCESS_CHECKPOINT_SCHEMA
                        or document.get("version") != PROCESS_CHECKPOINT_VERSION
                    )
                ):
                    rejection_reason = "CHECKPOINT_VERSION_UNSUPPORTED"
                    raise ValueError("checkpoint schema/version is unsupported")
                if (
                    not isinstance(document, dict)
                    or set(document)
                    != {"boot_id", "entries", "mac", "schema", "version"}
                    or document.get("schema") != PROCESS_CHECKPOINT_SCHEMA
                    or document.get("version") != PROCESS_CHECKPOINT_VERSION
                    or not isinstance(document.get("boot_id"), str)
                    or not _HEX_256_RE.fullmatch(document["boot_id"])
                    or not isinstance(document.get("entries"), list)
                    or len(document["entries"]) > MAX_PROCESSES
                    or not isinstance(document.get("mac"), str)
                    or not _HEX_256_RE.fullmatch(document["mac"])
                ):
                    raise ValueError("checkpoint schema/version is unsupported")
                payload = {
                    "boot_id": document["boot_id"],
                    "entries": document["entries"],
                    "schema": document["schema"],
                    "version": document["version"],
                }
                if not hmac.compare_digest(
                    document["mac"],
                    _checkpoint_mac(payload),
                ):
                    raise ValueError("checkpoint MAC is invalid")
                parsed_entries = [
                    _parse_checkpoint_entry(entry) for entry in document["entries"]
                ]
                identifiers = [entry["session_id"] for entry, _identity in parsed_entries]
                if len(set(identifiers)) != len(identifiers):
                    raise ValueError("checkpoint repeats a process session")
        except Exception:  # noqa: BLE001
            self._fence_checkpoint(rejection_reason)
            return 0

        requested_owner = str(owner_account_id or "").strip()
        requested_session = str(session_key or "").strip()
        requested_workspace = str(workspace_id or "").strip()
        same_boot = document["boot_id"] == _host_boot_id()
        staged: list[ProcessSession] = []
        dropped_identity = False
        for entry, identity in parsed_entries:
            owner = entry["owner_account_id"]
            pid = entry["pid"]
            actual_identity = _process_identity(pid)
            if not _recovery_identity_matches(identity, actual_identity):
                dropped_identity = True
                try:
                    self._audit_lifecycle(
                        "process_recovery_identity_rejected",
                        owner_account_id=owner,
                        reason="PID_IDENTITY_CHANGED",
                        decision="deny",
                    )
                except Exception:
                    log.exception("process recovery identity rejection audit failed")
                continue
            assert actual_identity is not None
            session = ProcessSession(
                id=entry["session_id"],
                command="[recovered process]",
                session_key=entry["session_key"],
                owner_account_id=owner,
                workspace_id=entry["workspace_id"],
                pid=pid,
                cwd=None,
                started_at=entry["started_at"],
                detached=True,
                notify_on_complete=entry["notify_on_complete"],
                task_id=entry["task_id"],
                authorization_digest=entry["authorization_digest"],
                output_ref="",
                process_identity=actual_identity,
                authorization_generation=entry["authorization_generation"],
                authorization_expires_at=entry["authorization_expires_at"],
                authorization_policy_digest=entry["authorization_policy_digest"],
                authorization_revalidatable=entry["authorization_revalidatable"],
                sandbox_preference=entry["sandbox_preference"],
                sandboxed=entry["sandboxed"],
                sandbox_system_surface=entry["sandbox_system_surface"],
                recovery_state=entry["recovery_state"],
                cleanup_reason=entry["cleanup_reason"],
                cleanup_attempts=entry["cleanup_attempts"],
                frozen=True,
            )
            with self._lock:
                self._frozen[session.id] = session
            staged.append(session)

        current = time.time()
        visible = 0
        for session in staged:
            cleanup_reason = ""
            if not same_boot:
                cleanup_reason = "HOST_REBOOT"
            elif session.authorization_expires_at <= current:
                cleanup_reason = "AUTHORIZATION_EXPIRED"
            elif session.recovery_state == "cleanup_pending":
                cleanup_reason = session.cleanup_reason or "CLEANUP_RETRY"
            elif not session.authorization_revalidatable:
                cleanup_reason = "TRANSIENT_AUTHORITY_UNVERIFIABLE"
            if cleanup_reason:
                self._attempt_cleanup(session, cleanup_reason)
                continue
            try:
                self._audit_lifecycle(
                    "process_recovery_staged",
                    session,
                    decision="frozen",
                )
            except Exception:  # noqa: BLE001 - audit durability gates recovery
                self._attempt_cleanup(session, "AUDIT_WRITE_FAILED")
                continue
            if (
                (not requested_owner or session.owner_account_id == requested_owner)
                and (not requested_session or session.session_key == requested_session)
                and (
                    not requested_workspace
                    or session.workspace_id == requested_workspace
                )
            ):
                visible += 1
        if dropped_identity:
            self._write_checkpoint()
        return visible


# 模块级单例
process_registry = ProcessRegistry()


def format_process_notification(evt: dict[str, Any]) -> str | None:
    """把一条进程通知事件格式化为可注入上下文的文本。"""
    evt_type = evt.get("type", "completion")
    sid = evt.get("session_id", "unknown")
    cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[重要] {evt.get('message', '')}"

    if evt_type == "watch_match":
        pat = evt.get("pattern", "?")
        out = evt.get("output", "")
        sup = evt.get("suppressed", 0)
        text = (
            f"[重要] 后台进程 {sid} 命中 watch 模式「{pat}」。\n"
            f"命令: {cmd}\n命中输出:\n{out}"
        )
        if sup:
            text += f"\n（另有 {sup} 条更早的命中被限流丢弃）"
        return text

    exit_code = evt.get("exit_code", "?")
    out = evt.get("output", "")
    return (
        f"[重要] 后台进程 {sid} 已结束（退出码 {exit_code}）。\n"
        f"命令: {cmd}\n输出:\n{out}"
    )


# ---------------------------------------------------------------------------
# process 工具：schema + handler
# ---------------------------------------------------------------------------

PROCESS_SCHEMA = {
    "name": "process",
    "description": (
        "管理 terminal(background=true) 启动的后台进程。actions: "
        "'list'（列出全部）、'poll'（查状态+最新输出）、'log'（完整输出，支持分页）、"
        "'wait'（阻塞至结束或超时）、'kill'（终止）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill"],
                "description": "对后台进程执行的操作",
            },
            "session_id": {
                "type": "string",
                "description": "后台进程的 session_id（来自 terminal 后台返回）。除 list 外均必填。",
            },
            "timeout": {
                "type": "integer",
                "description": "wait 操作最多阻塞秒数，超时返回部分输出",
                "minimum": 1,
            },
            "offset": {
                "type": "integer",
                "description": "log 操作的行偏移（默认取最后 200 行）",
            },
            "limit": {
                "type": "integer",
                "description": "log 操作最多返回行数",
                "minimum": 1,
            },
        },
        "required": ["action"],
    },
}


def _handle_process(args: dict[str, Any]) -> str:
    import json

    action = args.get("action", "")
    session_id = str(args.get("session_id", "")) if args.get("session_id") is not None else ""
    owner_account_id = str(current_owner_account_id.get() or "").strip()

    if action == "list":
        return json.dumps(
            {"processes": process_registry.list_sessions(owner_account_id=owner_account_id)},
            ensure_ascii=False,
        )
    if action not in {"poll", "log", "wait", "kill"}:
        return tool_error(f"未知 action: {action}。可用: list, poll, log, wait, kill")
    if not session_id:
        return tool_error(f"{action} 需要 session_id")
    if action == "poll":
        return json.dumps(
            process_registry.poll(session_id, owner_account_id=owner_account_id),
            ensure_ascii=False,
        )
    if action == "log":
        return json.dumps(
            process_registry.read_log(
                session_id,
                offset=args.get("offset", 0),
                limit=args.get("limit", 200),
                owner_account_id=owner_account_id,
            ),
            ensure_ascii=False,
        )
    if action == "wait":
        return json.dumps(
            process_registry.wait(
                session_id,
                timeout=args.get("timeout"),
                owner_account_id=owner_account_id,
            ),
            ensure_ascii=False,
        )
    # kill
    return json.dumps(
        process_registry.kill_process(session_id, owner_account_id=owner_account_id),
        ensure_ascii=False,
    )


def register_process_tool(registry: Any) -> None:
    """注册 process 工具到给定 registry。"""
    registry.register(
        name="process",
        toolset="terminal",
        schema=PROCESS_SCHEMA,
        handler=_handle_process,
        emoji="⚙️",
        display_name="管理进程",
        ui_label_template="进程 {action}",
        always_load=True,
        search_hint="process background job poll log wait kill list terminal",
    )
