"""文件工具公共辅助函数。

本文件从 Hermes 的 tools/file_tools.py 和 tools/file_operations.py
复制/改造关键独立函数，用于增强 Crew 的 file_read / file_write / patch / glob / grep。
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_task_runtime_id,
)
from crew.security.local_path import (
    LocalPathReference,
    LocalPathReferenceKind,
    decode_file_uri_path,
)

_MAX_CONCURRENT_WRITES = 16
_MAX_IN_FLIGHT_WRITE_BYTES = 64 * 1024 * 1024
_MAX_IN_FLIGHT_READ_BYTES = 128 * 1024 * 1024
_MIN_FREE_SPACE_AFTER_WRITE = 16 * 1024 * 1024
_WRITE_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_WRITES)


class FileConflictError(RuntimeError):
    """Raised when a structured write target changed after inspection."""


_WriteBudgetKey = tuple[str, str]


class _WriteBudgetLease:
    """One released-once reservation in an owner/task write budget."""

    def __init__(
        self,
        registry: _WriteBudgetRegistry,
        key: _WriteBudgetKey,
        byte_count: int,
    ) -> None:
        self._registry = registry
        self._key = key
        self._byte_count = byte_count
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._registry._release(self._key, self._byte_count)
            self._released = True


class _WriteBudgetRegistry:
    """Thread-safe aggregate in-flight byte reservations by owner/task."""

    def __init__(self, *, max_bytes: int = _MAX_IN_FLIGHT_WRITE_BYTES) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("聚合写入上限无效")
        self._max_bytes = max_bytes
        # ponytail: process-local lock; use a shared broker only if writers span processes.
        self._lock = threading.Lock()
        self._usage: dict[_WriteBudgetKey, int] = {}

    def reserve(
        self,
        key: _WriteBudgetKey | None,
        byte_count: int,
    ) -> _WriteBudgetLease | None:
        if key is None:
            return None
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ValueError("聚合写入字节数无效")
        with self._lock:
            used = self._usage.get(key, 0)
            if used + byte_count > self._max_bytes:
                raise FileConflictError("owner/task 聚合在途写入超过安全上限")
            self._usage[key] = used + byte_count
        return _WriteBudgetLease(self, key, byte_count)

    def _release(self, key: _WriteBudgetKey, byte_count: int) -> None:
        with self._lock:
            used = self._usage.get(key)
            if used is None or used < byte_count:
                raise RuntimeError("聚合写入预算释放不匹配")
            remaining = used - byte_count
            if remaining:
                self._usage[key] = remaining
            else:
                self._usage.pop(key, None)


_WRITE_BUDGET = _WriteBudgetRegistry()


class _ReadBudgetRegistry(_WriteBudgetRegistry):
    """Thread-safe aggregate in-flight byte reservations for verified reads."""

    def __init__(self, *, max_bytes: int = _MAX_IN_FLIGHT_READ_BYTES) -> None:
        super().__init__(max_bytes=max_bytes)

    def reserve(
        self,
        key: _WriteBudgetKey | None,
        byte_count: int,
    ) -> _WriteBudgetLease | None:
        try:
            return super().reserve(key, byte_count)
        except FileConflictError as exc:
            raise FileConflictError("owner/task 聚合在途读取超过安全上限") from exc


_READ_BUDGET = _ReadBudgetRegistry()


@dataclass(frozen=True)
class FileIdentity:
    """Canonical leaf identity captured at the authorization boundary."""

    path: Path
    exists: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0


@dataclass(frozen=True)
class FileVersion:
    path: Path
    exists: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0
    digest: str = ""
    mode: int = 0
    data: bytes = b""


def capture_file_identity(path: Path) -> FileIdentity:
    """Capture a regular leaf without following a leaf link or reading content."""
    canonical = _lexical_absolute(path)
    try:
        info = canonical.lstat()
    except FileNotFoundError:
        return FileIdentity(path=canonical, exists=False)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode):
        raise FileConflictError("授权文件目标是符号链接")
    if getattr(info, "st_file_attributes", 0) & reparse_flag:
        raise FileConflictError("授权文件目标是 reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise FileConflictError("授权文件目标不是普通文件")
    if _is_sparse_file(info):
        raise FileConflictError("授权文件目标是稀疏或压缩文件")
    if info.st_nlink > 1:
        raise FileConflictError("授权文件目标存在多个硬链接")
    return FileIdentity(
        path=canonical,
        exists=True,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
    )


def snapshot_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    expected_identity: FileIdentity | None = None,
) -> FileVersion:
    """Capture identity and content hash, rejecting ambiguous hard-link writes."""
    canonical = _lexical_absolute(path)
    if expected_identity is not None and expected_identity.path != canonical:
        raise FileConflictError("文件路径与授权身份不一致")
    byte_limit = _DEFAULT_MAX_FILE_BYTES if max_bytes is None else max_bytes
    if byte_limit < 0:
        raise ValueError("文件读取上限不能为负数")
    budget_lease = _READ_BUDGET.reserve(_current_owner_task_key(), byte_limit)
    try:
        try:
            info, data = _read_verified_file(canonical, max_bytes=byte_limit)
        except FileNotFoundError:
            if expected_identity is not None and expected_identity.exists:
                raise FileConflictError("文件在授权后已被修改或替换") from None
            return FileVersion(path=canonical, exists=False)
        if expected_identity is not None and not _identity_matches_info(
            expected_identity,
            canonical,
            info,
        ):
            raise FileConflictError("文件在授权后身份已变化")
        return FileVersion(
            path=canonical,
            exists=True,
            device=info.st_dev,
            inode=info.st_ino,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
            digest=hashlib.sha256(data).hexdigest(),
            mode=info.st_mode,
            data=data,
        )
    finally:
        if budget_lease is not None:
            budget_lease.release()


def atomic_replace_bytes(
    path: Path,
    data: bytes,
    expected: FileVersion,
    *,
    max_bytes: int | None = None,
) -> None:
    """Replace one file in-place only if path identity and content are unchanged."""
    byte_limit = _DEFAULT_MAX_WRITE_BYTES if max_bytes is None else max_bytes
    if byte_limit < 0:
        raise ValueError("写入上限不能为负数")
    if len(data) > byte_limit:
        raise ValueError(f"文件大小 {len(data)} 字节超过写入上限 {byte_limit} 字节")
    budget_lease = _WRITE_BUDGET.reserve(_current_write_budget_key(), len(data))
    slots = _WRITE_SLOTS
    try:
        if not slots.acquire(blocking=False):
            raise FileConflictError("并发写入数量超过安全上限")
        try:
            canonical = _lexical_absolute(path)
            if canonical != expected.path:
                raise FileConflictError("文件在写入前已被修改或替换")
            _ensure_private_directory(canonical.parent)
            free_bytes = shutil.disk_usage(canonical.parent).free
            if free_bytes - len(data) < _MIN_FREE_SPACE_AFTER_WRITE:
                raise OSError(errno.ENOSPC, "剩余磁盘空间低于安全写入保留量")
            with _pinned_parent(canonical) as parent_descriptor:
                match_descriptor = None if os.name == "nt" else parent_descriptor
                if not _file_version_matches(expected, match_descriptor):
                    raise FileConflictError("文件在原子替换前已被修改或替换")
                if os.name == "nt":
                    _atomic_replace_windows(canonical, data, expected, parent_descriptor)
                else:
                    _atomic_replace_posix(canonical.name, data, expected, parent_descriptor)
        finally:
            slots.release()
    finally:
        if budget_lease is not None:
            budget_lease.release()


def _current_owner_task_key() -> _WriteBudgetKey | None:
    try:
        owner = str(current_owner_account_id.get() or "").strip()
        task = str(current_task_runtime_id.get() or "").strip()
    except Exception as exc:
        raise FileConflictError("文件预算上下文不可用") from exc
    if not owner or not task:
        return None
    return owner, task


def _current_write_budget_key() -> _WriteBudgetKey | None:
    return _current_owner_task_key()


def _atomic_replace_windows(
    path: Path,
    data: bytes,
    expected: FileVersion,
    parent_handle: int,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}"
    delete_access = 0x00010000
    generic_write = 0x40000000
    share_all = 0x0001 | 0x0002 | 0x0004
    create_new = 1
    temporary_attribute = 0x0100
    invalid_handle = ctypes.c_void_p(-1).value
    with _windows_private_security_attributes() as security_attributes:
        handle = create_file(
            str(temporary),
            delete_access | generic_write,
            share_all,
            security_attributes,
            create_new,
            temporary_attribute,
            None,
        )
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "无法创建结构化写入临时文件")
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        close_handle(handle)
        raise
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected.exists:
            os.chmod(temporary, stat.S_IMODE(expected.mode))
        if not _file_version_matches(expected, None):
            raise FileConflictError("文件在原子替换前已被修改或替换")
        _rename_windows_handle(
            descriptor,
            parent_handle,
            path.name,
            replace_if_exists=expected.exists,
        )
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _windows_private_security_attributes():
    """Create protected owner/SYSTEM-only security attributes."""

    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    descriptor = wintypes.LPVOID()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    if not convert(
        "D:P(A;;FA;;;SY)(A;;FA;;;OW)",
        1,  # SDDL_REVISION_1
        ctypes.byref(descriptor),
        None,
    ):
        raise FileConflictError(f"无法创建私有文件系统对象 ACL: WinError {ctypes.get_last_error()}")
    attributes = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes),
        descriptor,
        False,
    )
    try:
        yield ctypes.byref(attributes)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.LocalFree(descriptor)


def _rename_windows_handle(
    descriptor: int,
    parent_handle: int,
    destination_name: str,
    *,
    replace_if_exists: bool,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * len(destination_name)),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("StatusOrPointer", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    information = FileRenameInformation()
    information.ReplaceIfExists = replace_if_exists
    information.RootDirectory = parent_handle
    information.FileNameLength = len(destination_name.encode("utf-16-le"))
    information.FileName = destination_name
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    set_information.restype = ctypes.c_long
    status_block = IoStatusBlock()
    status = set_information(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(status_block),
        ctypes.byref(information),
        ctypes.sizeof(information),
        10,  # FileRenameInformation
    )
    if status < 0:
        to_win_error = ntdll.RtlNtStatusToDosError
        to_win_error.argtypes = [ctypes.c_long]
        to_win_error.restype = wintypes.ULONG
        raise OSError(to_win_error(status), "无法以父目录对象替换结构化文件")


def _atomic_replace_posix(
    name: str,
    data: bytes,
    expected: FileVersion,
    parent_descriptor: int,
) -> None:
    temporary_name = f".{name}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not _file_version_matches(expected, parent_descriptor):
            raise FileConflictError("文件在原子替换前已被修改或替换")
        if expected.exists:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise FileConflictError("文件在原子发布前已被创建或替换") from exc
        if expected.exists:
            os.fchmod(descriptor, stat.S_IMODE(expected.mode))
            os.fsync(descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _file_version_matches(expected: FileVersion, parent_descriptor: int | None = None) -> bool:
    try:
        info, data = (
            _read_verified_path(expected.path, max_bytes=expected.size)
            if parent_descriptor is None
            else _read_verified_at(
                parent_descriptor,
                expected.path.name,
                max_bytes=expected.size,
            )
        )
    except FileNotFoundError:
        return not expected.exists
    except (FileConflictError, OSError, ValueError):
        return False
    if not expected.exists:
        return False
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        expected.device,
        expected.inode,
        expected.size,
        expected.mtime_ns,
        expected.ctime_ns,
    ):
        return False
    return hashlib.sha256(data).hexdigest() == expected.digest


def read_verified_bytes(
    path: Path,
    *,
    max_bytes: int | None = None,
    expected_digest: str | None = None,
    expected_identity: FileIdentity | None = None,
    reject_hard_links: bool = True,
) -> bytes:
    """Read one regular file through the identity-checked handle."""

    byte_limit = _DEFAULT_MAX_FILE_BYTES if max_bytes is None else max_bytes
    if byte_limit < 0:
        raise ValueError("文件读取上限不能为负数")
    budget_lease = _READ_BUDGET.reserve(_current_owner_task_key(), byte_limit)
    canonical = _lexical_absolute(path)
    try:
        if expected_identity is not None and expected_identity.path != canonical:
            raise FileConflictError("文件路径与授权身份不一致")
        info, data = _read_verified_file(
            canonical,
            max_bytes=byte_limit,
            reject_hard_links=reject_hard_links,
        )
        if expected_identity is not None and not _identity_matches_info(
            expected_identity,
            canonical,
            info,
        ):
            raise FileConflictError("文件在授权后身份已变化")
        if expected_digest is not None:
            if not isinstance(expected_digest, str):
                raise ValueError("预期内容摘要格式无效")
            normalized_digest = expected_digest.casefold()
            if len(normalized_digest) != 64 or any(
                char not in "0123456789abcdef" for char in normalized_digest
            ):
                raise ValueError("预期内容摘要格式无效")
            actual_digest = hashlib.sha256(data).hexdigest()
            if not secrets.compare_digest(actual_digest, normalized_digest):
                raise FileConflictError("结构化文件内容摘要与已授权内容不一致")
        return data
    finally:
        if budget_lease is not None:
            budget_lease.release()


def _identity_matches_info(
    expected: FileIdentity,
    canonical: Path,
    actual: os.stat_result,
) -> bool:
    if not expected.exists or expected.path != canonical:
        return False
    stable_identity = (
        actual.st_dev,
        actual.st_ino,
        actual.st_size,
        actual.st_mtime_ns,
    ) == (
        expected.device,
        expected.inode,
        expected.size,
        expected.mtime_ns,
    )
    if not stable_identity:
        return False
    # On Windows ``st_ctime`` is creation time and can be normalized lazily
    # between a path stat and the first handle open. File index + volume,
    # size, and write time remain the stable authorization identity there.
    return os.name == "nt" or actual.st_ctime_ns == expected.ctime_ns


def stat_verified_file(path: Path) -> os.stat_result:
    """Stat one regular, single-link file through an identity-checked handle."""

    return _stat_verified_file(_lexical_absolute(path))


def _read_verified_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    reject_hard_links: bool = True,
) -> tuple[os.stat_result, bytes]:
    with _pinned_parent(path) as parent_descriptor:
        if os.name == "nt":
            return _read_verified_path(
                path,
                max_bytes=max_bytes,
                reject_hard_links=reject_hard_links,
            )
        return _read_verified_at(
            parent_descriptor,
            path.name,
            max_bytes=max_bytes,
            reject_hard_links=reject_hard_links,
        )


def _stat_verified_file(path: Path) -> os.stat_result:
    with _pinned_parent(path) as parent_descriptor:
        if os.name == "nt":
            return _stat_verified_path(path)
        return _stat_verified_at(parent_descriptor, path.name)


def _read_verified_path(
    path: Path,
    *,
    max_bytes: int | None = None,
    reject_hard_links: bool = True,
) -> tuple[os.stat_result, bytes]:
    return _read_verified_open(
        path.lstat(),
        lambda flags: os.open(path, flags),
        max_bytes=max_bytes,
        reject_hard_links=reject_hard_links,
    )


def _stat_verified_path(path: Path) -> os.stat_result:
    return _stat_verified_open(
        path.lstat(),
        lambda flags: os.open(path, flags),
    )


def _read_verified_at(
    parent_descriptor: int,
    name: str,
    *,
    max_bytes: int | None = None,
    reject_hard_links: bool = True,
) -> tuple[os.stat_result, bytes]:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    return _read_verified_open(
        before,
        lambda flags: os.open(name, flags, dir_fd=parent_descriptor),
        max_bytes=max_bytes,
        reject_hard_links=reject_hard_links,
    )


def _stat_verified_at(parent_descriptor: int, name: str) -> os.stat_result:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    return _stat_verified_open(
        before,
        lambda flags: os.open(name, flags, dir_fd=parent_descriptor),
    )


def _read_verified_open(
    before: os.stat_result,
    opener,
    *,
    max_bytes: int | None = None,
    reject_hard_links: bool = True,
) -> tuple[os.stat_result, bytes]:
    descriptor, opened = _open_verified(
        before,
        opener,
        reject_hard_links=reject_hard_links,
    )
    try:
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError("文件超过读取上限")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("文件超过读取上限")
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
            raise FileConflictError("结构化文件在读取时已变化")
        return after, data
    finally:
        os.close(descriptor)


def _stat_verified_open(before: os.stat_result, opener) -> os.stat_result:
    descriptor, opened = _open_verified(before, opener)
    os.close(descriptor)
    return opened


def _open_verified(
    before: os.stat_result,
    opener,
    *,
    reject_hard_links: bool = True,
) -> tuple[int, os.stat_result]:
    if stat.S_ISLNK(before.st_mode):
        raise FileConflictError("结构化文件目标是符号链接")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(before, "st_file_attributes", 0) & reparse_flag:
        raise FileConflictError("结构化文件目标是 reparse point")
    if not stat.S_ISREG(before.st_mode):
        raise FileConflictError("结构化文件目标不是普通文件")
    if _is_sparse_file(before):
        raise FileConflictError("结构化文件目标是稀疏或压缩文件")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = opener(flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise FileConflictError("结构化文件目标是符号链接") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileConflictError("结构化文件目标不是普通文件")
        if getattr(opened, "st_file_attributes", 0) & reparse_flag:
            raise FileConflictError("结构化文件目标是 reparse point")
        if _is_sparse_file(opened):
            raise FileConflictError("结构化文件目标是稀疏或压缩文件")
        if reject_hard_links and opened.st_nlink > 1:
            raise FileConflictError("结构化写入拒绝多硬链接目标")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise FileConflictError("结构化文件在打开时身份已变化")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _is_sparse_file(info: os.stat_result) -> bool:
    """Fail closed for sparse/compressed allocation that can hide huge logical size."""

    if info.st_size <= 0:
        return False
    allocation_flags = getattr(stat, "FILE_ATTRIBUTE_SPARSE_FILE", 0x200) | getattr(
        stat, "FILE_ATTRIBUTE_COMPRESSED", 0x800
    )
    if getattr(info, "st_file_attributes", 0) & allocation_flags:
        return True
    blocks = getattr(info, "st_blocks", None)
    return isinstance(blocks, int) and blocks * 512 < info.st_size


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def decode_local_file_uri(raw_uri: str) -> str:
    """Decode one strict file URI for legacy callers at an I/O seam."""

    reference = LocalPathReference.parse(raw_uri)
    if reference.kind is not LocalPathReferenceKind.FILE_URI:
        raise ValueError("本地文件 URI scheme 无效")
    return decode_file_uri_path(reference)


def _ensure_private_directory(directory: Path) -> None:
    """Create a directory chain without traversing links or reparse points."""

    absolute = _lexical_absolute(directory)
    missing: list[Path] = []
    cursor = absolute
    while True:
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            parent = cursor.parent
            if parent == cursor:
                raise FileConflictError("无法找到可安全创建目录的现有父路径")
            missing.append(cursor)
            cursor = parent
            continue
        _validate_directory_metadata(info)
        break

    for candidate in reversed(missing):
        with _pinned_parent(candidate) as parent_descriptor:
            created = _mkdir_private_at(candidate, parent_descriptor)
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise FileConflictError("无法验证新建目录身份") from exc
            _validate_directory_metadata(info)
            if not created:
                # A concurrent creator is acceptable only when it published a
                # real directory beneath the still-pinned parent.
                continue

    # Validate every existing component even when no directory needed creation.
    with _pinned_parent(absolute / ".ace-directory-probe"):
        pass


def _validate_directory_metadata(info: os.stat_result) -> None:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag:
        raise FileConflictError("目录路径组件是链接或 reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise FileConflictError("目录路径组件不是目录")


def _mkdir_private_at(candidate: Path, parent_descriptor: int) -> bool:
    if os.name != "nt":
        try:
            os.mkdir(candidate.name, mode=0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return True
        except FileExistsError:
            return False

    import ctypes
    from ctypes import wintypes

    create_directory = ctypes.WinDLL("kernel32", use_last_error=True).CreateDirectoryW
    create_directory.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    create_directory.restype = wintypes.BOOL
    with _windows_private_security_attributes() as security_attributes:
        if create_directory(str(candidate), security_attributes):
            return True
    error = ctypes.get_last_error()
    if error == 183:  # ERROR_ALREADY_EXISTS
        return False
    raise OSError(error, "无法创建私有目录")


@contextmanager
def _pinned_parent(path: Path) -> Iterator[int]:
    """Pin the authorized parent object while the structured operation uses it."""

    if os.name == "nt":
        handles, close_handle = _pin_windows_directory_chain(path.parent)
        try:
            yield int(handles[-1])
        finally:
            for handle in reversed(handles):
                close_handle(handle)
        return
    descriptor = _open_posix_directory_chain(path.parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_posix_directory_chain(directory: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    absolute = _lexical_absolute(directory)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _pin_windows_directory_chain(directory: Path):
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    file_share_read_write = 0x0001 | 0x0002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    attribute_directory = 0x0010
    attribute_reparse = 0x0400
    invalid_handle = ctypes.c_void_p(-1).value

    absolute = _lexical_absolute(directory)
    current = Path(absolute.anchor)
    components = [current]
    for component in absolute.parts[1:]:
        current /= component
        components.append(current)

    handles = []
    try:
        for component in components:
            handle = create_file(
                str(component),
                file_read_attributes,
                file_share_read_write,
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            if handle == invalid_handle:
                error = ctypes.get_last_error()
                if error in {2, 3}:  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                    raise FileNotFoundError(error, os.strerror(error), str(component))
                raise FileConflictError(f"无法锁定结构化文件父目录: WinError {error}")
            handles.append(handle)
            information = ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                raise FileConflictError(
                    f"无法验证结构化文件父目录: WinError {ctypes.get_last_error()}"
                )
            if not information.dwFileAttributes & attribute_directory:
                raise FileConflictError("结构化文件父路径组件不是目录")
            if information.dwFileAttributes & attribute_reparse:
                raise FileConflictError("结构化文件父路径组件是 reparse point")
        return handles, close_handle
    except Exception:
        for handle in reversed(handles):
            close_handle(handle)
        raise


# ---------------------------------------------------------------------------
# 路径解析（对齐 Hermes _resolve_base_dir / _resolve_path_for_task）
# ---------------------------------------------------------------------------


def _resolve_base_dir() -> Path:
    """返回解析相对路径时的绝对基目录。

    优先使用 current_agent_workdir ContextVar；否则使用进程 cwd。
    与 Hermes 的区别：Hermes 用 task_id + TERMINAL_CWD；Crew 用 ContextVar。
    """
    raw = current_agent_workdir.get()
    base = Path(raw).expanduser() if raw else Path(os.getcwd())
    if not base.is_absolute():
        base = Path(os.getcwd()) / base
    return base.resolve()


def _resolve_path(filepath: str) -> Path:
    """把 filepath 解析为绝对 Path。"""
    p = Path(filepath).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (_resolve_base_dir() / p).resolve()


# ---------------------------------------------------------------------------
# 设备路径黑名单（复制自 Hermes）
# ---------------------------------------------------------------------------

_BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)


def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.expanduser(path)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    return normalized.startswith("/proc/") and normalized.endswith(
        ("/environ", "/cmdline", "/maps")
    )


def _is_blocked_device(filepath: str) -> bool:
    """检查路径是否是会挂起或泄露敏感信息的设备文件。"""
    normalized = os.path.expanduser(filepath)
    if _is_blocked_device_path(normalized):
        return True
    try:
        resolved = os.path.realpath(normalized)
    except (OSError, ValueError):
        return False
    return resolved != normalized and _is_blocked_device_path(resolved)


# ---------------------------------------------------------------------------
# 敏感写入路径保护（改造自 Hermes）
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PREFIXES = (
    "/etc/",
    "/boot/",
    "/usr/lib/systemd/",
    "/private/etc/",
    "/private/boot/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}


def _get_crew_config_resolved() -> str | None:
    """返回 Crew 主配置文件的绝对路径（用于防止 agent 修改自身配置）。"""
    try:
        from crew.state.config import ROOT, _get_user_config_dir

        # 用户配置目录优先（冻结态 get_crew_home()，开发态 ROOT/config）
        user_yaml = _get_user_config_dir() / "config.yaml"
        if user_yaml.is_file():
            return str(user_yaml.resolve())
        return str((ROOT / "config" / "config.yaml").resolve())
    except Exception:  # noqa: BLE001 - optional config discovery fails closed to no path
        return None


def _check_sensitive_path(filepath: str) -> str | None:
    """Return error message if path targets a sensitive system location."""
    try:
        resolved = str(_resolve_path(filepath))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(os.path.expanduser(filepath))
    raw_posix = os.path.expanduser(filepath).replace("\\", "/")
    resolved_posix = resolved.replace("\\", "/")
    normalized_posix = normalized.replace("\\", "/")
    err = (
        f"Refusing to write to sensitive system path: {filepath}\n"
        "Use the terminal tool with sudo if you need to modify system files."
    )
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if (
            resolved_posix.startswith(prefix)
            or normalized_posix.startswith(prefix)
            or raw_posix.startswith(prefix)
        ):
            return err
    if (
        resolved_posix in _SENSITIVE_EXACT_PATHS
        or normalized_posix in _SENSITIVE_EXACT_PATHS
        or raw_posix in _SENSITIVE_EXACT_PATHS
    ):
        return err
    crew_config = _get_crew_config_resolved()
    if crew_config and (resolved == crew_config or normalized == crew_config):
        return (
            f"Refusing to write to Crew config file: {filepath}\n"
            "Agent cannot modify security-sensitive configuration."
        )
    return None


def _is_expected_write_exception(exc: Exception) -> bool:
    """True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
    }


# ---------------------------------------------------------------------------
# 行尾符与 BOM 处理（复制自 Hermes tools/file_operations.py）
# ---------------------------------------------------------------------------


def _detect_line_ending(sample: str) -> str | None:
    """Return the dominant line ending in ``sample`` or None."""
    if not sample:
        return None
    head = sample[:4096]
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    """Convert all line endings in ``text`` to ``target`` (``\\n`` or ``\\r\\n``)."""
    lf_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return lf_normalized
    if target == "\r\n":
        return lf_normalized.replace("\n", "\r\n")
    return text


_UTF8_BOM = "﻿"


def _strip_bom(text: str) -> tuple[str, bool]:
    """Return (text-without-leading-BOM, had_bom)."""
    if text and text.startswith(_UTF8_BOM):
        return text[len(_UTF8_BOM) :], True
    return text, False


def _has_bom(text: str | None) -> bool:
    """True if ``text`` begins with a UTF-8 BOM."""
    return bool(text) and text.startswith(_UTF8_BOM)


# ---------------------------------------------------------------------------
# 分页规范化（复制自 Hermes tools/file_operations.py）
# ---------------------------------------------------------------------------


def _normalize_read_pagination(
    total_lines: int,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Return validated (offset, limit) for read_file pagination.

    Offset is 1-based for model-facing parameters (matches Hermes).
    """
    if offset is None:
        offset = 1
    else:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 1
    if limit is None:
        limit = total_lines
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = total_lines
    offset = max(1, min(offset, total_lines + 1))
    limit = max(0, limit)
    return offset, limit


def _normalize_search_pagination(
    total_hits: int,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Return validated (offset, limit) for glob/grep pagination."""
    if offset is None:
        offset = 1
    else:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 1
    if limit is None:
        limit = total_hits
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = total_hits
    offset = max(1, offset)
    limit = max(0, limit)
    return offset, limit


# ---------------------------------------------------------------------------
# 二进制扩展名检测（复制自 Hermes tools/binary_extensions.py 的子集）
# ---------------------------------------------------------------------------

_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".svgz",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".webm",
        ".avi",
        ".mov",
        ".mkv",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".rar",
        ".7z",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".dat",
        ".pyc",
        ".pyo",
        ".o",
        ".a",
        ".class",
        ".jar",
    }
)


def _has_binary_extension(path: Path) -> bool:
    """True if the file extension suggests binary content."""
    return path.suffix.lower() in _BINARY_EXTENSIONS


# ---------------------------------------------------------------------------
# 读取上限（改造自 Hermes）
# ---------------------------------------------------------------------------

_DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_WRITE_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_READ_CHARS = 100_000


def _get_max_read_chars() -> int:
    """Return configured max characters per file read."""
    try:
        from crew.state.config import load_config

        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("file", {}).get("read_max_chars")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    except Exception:  # noqa: BLE001, S110 - optional configuration uses safe default
        pass
    return _DEFAULT_MAX_READ_CHARS


# ---------------------------------------------------------------------------
# 行号/片段辅助
# ---------------------------------------------------------------------------


def _apply_line_pagination(text: str, offset: int, limit: int) -> str:
    """Return the requested slice of lines (1-based offset)."""
    lines = text.splitlines(keepends=True)
    start = offset - 1
    end = start + limit
    return "".join(lines[start:end])


def _format_read_result(
    content: str,
    *,
    total_lines: int,
    file_size: int,
    offset: int = 1,
    limit: int | None = None,
    truncated: bool = False,
    hint: str = "",
) -> str:
    """把读取结果格式化为 JSON，与 Hermes 返回结构接近。"""
    import json

    shown_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    payload: dict = {
        "success": True,
        "content": content,
        "content_trust": "untrusted",
        "content_source": "file",
        "total_lines": total_lines,
        "file_size": file_size,
        "offset": offset,
        "limit": limit if limit is not None else total_lines,
        "shown_lines": shown_lines,
    }
    if truncated:
        payload["truncated"] = True
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 通用输出截断（多个工具 handler 共用）
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 12000) -> str:
    """截断超长文本，避免 tool result 超出模型上下文。"""
    return text if len(text) <= limit else text[:limit] + "\n...[内容已截断]"
