"""文件工具公共辅助函数。

本文件从 Hermes 的 tools/file_tools.py 和 tools/file_operations.py
复制/改造关键独立函数，用于增强 Crew 的 file_read / file_write / patch / glob / grep。
"""

from __future__ import annotations

import codecs
import errno
import hashlib
import os
import secrets
import stat
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Optional

from crew.core.errors import ToolError
from crew.core.runctx import current_agent_workdir


# 统一整读上限：单次整读超过该值直接抛 ValueError，
# 避免把超大文件整体搬进内存（OOM / 拖垮事件循环）。
MAX_READ_FILE_BYTES = 512 * 1024 * 1024


class FileConflictError(RuntimeError):
    """Raised when a structured write target changed after inspection."""


@dataclass(frozen=True)
class FileVersion:
    path: Path
    exists: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    digest: str = ""
    mode: int = 0
    data: bytes = b""


def snapshot_file(path: Path, *, max_bytes: int | None = None) -> FileVersion:
    """Capture identity and content hash, rejecting ambiguous hard-link writes.

    max_bytes 为整读上限：超出即抛 ValueError，避免无界读取整文件。
    """
    canonical = _lexical_absolute(path)
    try:
        info, data = _read_verified_file(canonical, max_bytes=max_bytes)
    except FileNotFoundError:
        return FileVersion(path=canonical, exists=False)
    return FileVersion(
        path=canonical,
        exists=True,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        digest=hashlib.sha256(data).hexdigest(),
        mode=info.st_mode,
        data=data,
    )


def atomic_replace_bytes(path: Path, data: bytes, expected: FileVersion) -> None:
    """Replace one file in-place only if path identity and content are unchanged."""
    canonical = _lexical_absolute(path)
    if canonical != expected.path:
        raise FileConflictError("文件在写入前已被修改或替换")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    with _pinned_parent(canonical) as parent_descriptor:
        match_descriptor = None if os.name == "nt" else parent_descriptor
        if not _file_version_matches(expected, match_descriptor):
            raise FileConflictError("文件在原子替换前已被修改或替换")
        if os.name == "nt":
            _atomic_replace_windows(canonical, data, expected, parent_descriptor)
        else:
            _atomic_replace_posix(canonical.name, data, expected, parent_descriptor)


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
    handle = create_file(
        str(temporary),
        delete_access | generic_write,
        share_all,
        None,
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
        _rename_windows_handle(descriptor, parent_handle, path.name)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _rename_windows_handle(descriptor: int, parent_handle: int, destination_name: str) -> None:
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
    information.ReplaceIfExists = True
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
        if expected.exists:
            os.fchmod(descriptor, stat.S_IMODE(expected.mode))
        if not _file_version_matches(expected, parent_descriptor):
            raise FileConflictError("文件在原子替换前已被修改或替换")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _file_version_matches(expected: FileVersion, parent_descriptor: int | None = None) -> bool:
    try:
        info, data = (
            _read_verified_path(expected.path)
            if parent_descriptor is None
            else _read_verified_at(parent_descriptor, expected.path.name)
        )
    except FileNotFoundError:
        return not expected.exists
    except (FileConflictError, OSError):
        return False
    if not expected.exists:
        return False
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        expected.device,
        expected.inode,
        expected.size,
        expected.mtime_ns,
    ):
        return False
    return hashlib.sha256(data).hexdigest() == expected.digest


def read_verified_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read one regular, single-link file through the identity-checked handle."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("文件读取上限不能为负数")
    _info, data = _read_verified_file(_lexical_absolute(path), max_bytes=max_bytes)
    return data


def stat_verified_file(path: Path) -> os.stat_result:
    """Stat one regular, single-link file through an identity-checked handle."""

    return _stat_verified_file(_lexical_absolute(path))


def _read_verified_file(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[os.stat_result, bytes]:
    with _pinned_parent(path) as parent_descriptor:
        if os.name == "nt":
            return _read_verified_path(path, max_bytes=max_bytes)
        return _read_verified_at(parent_descriptor, path.name, max_bytes=max_bytes)


def _stat_verified_file(path: Path) -> os.stat_result:
    with _pinned_parent(path) as parent_descriptor:
        if os.name == "nt":
            return _stat_verified_path(path)
        return _stat_verified_at(parent_descriptor, path.name)


def _read_verified_path(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[os.stat_result, bytes]:
    return _read_verified_open(
        path.lstat(),
        lambda flags: os.open(path, flags),
        max_bytes=max_bytes,
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
) -> tuple[os.stat_result, bytes]:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    return _read_verified_open(
        before,
        lambda flags: os.open(name, flags, dir_fd=parent_descriptor),
        max_bytes=max_bytes,
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
) -> tuple[os.stat_result, bytes]:
    descriptor, opened = _open_verified(before, opener)
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
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise FileConflictError("结构化文件在读取时已变化")
        return after, data
    finally:
        os.close(descriptor)


def _stat_verified_open(before: os.stat_result, opener) -> os.stat_result:
    descriptor, opened = _open_verified(before, opener)
    os.close(descriptor)
    return opened


def _open_verified(before: os.stat_result, opener) -> tuple[int, os.stat_result]:
    if stat.S_ISLNK(before.st_mode):
        raise FileConflictError("结构化文件目标是符号链接")
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
        if opened.st_nlink > 1:
            raise FileConflictError("结构化写入拒绝多硬链接目标")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise FileConflictError("结构化文件在打开时身份已变化")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


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
                raise FileConflictError(
                    f"无法锁定结构化文件父目录: WinError {ctypes.get_last_error()}"
                )
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

_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.expanduser(path)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/environ", "/cmdline", "/maps")):
        return True
    return False


def _is_blocked_device(filepath: str) -> bool:
    """检查路径是否是会挂起或泄露敏感信息的设备文件。"""
    normalized = os.path.expanduser(filepath)
    if _is_blocked_device_path(normalized):
        return True
    try:
        resolved = os.path.realpath(normalized)
    except (OSError, ValueError):
        return False
    if resolved != normalized and _is_blocked_device_path(resolved):
        return True
    return False


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
        from crew.state.config import _get_user_config_dir, ROOT
        # 用户配置目录优先（冻结态 get_crew_home()，开发态 ROOT/config）
        user_yaml = _get_user_config_dir() / "config.yaml"
        if user_yaml.is_file():
            return str(user_yaml.resolve())
        return str((ROOT / "config" / "config.yaml").resolve())
    except Exception:
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
    if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return True
    return False


# ---------------------------------------------------------------------------
# 行尾符与 BOM 处理（复制自 Hermes tools/file_operations.py）
# ---------------------------------------------------------------------------

def _detect_line_ending(sample: str) -> Optional[str]:
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
        return text[len(_UTF8_BOM):], True
    return text, False


# ---------------------------------------------------------------------------
# 分页规范化（复制自 Hermes tools/file_operations.py）
# ---------------------------------------------------------------------------

def _normalize_read_pagination(
    total_lines: int,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
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


# ---------------------------------------------------------------------------
# 二进制扩展名检测（复制自 Hermes tools/binary_extensions.py 的子集）
# ---------------------------------------------------------------------------

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svgz",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov", ".mkv",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".pyc", ".pyo", ".o", ".a", ".class", ".jar",
})


def _has_binary_extension(path: Path) -> bool:
    """True if the file extension suggests binary content."""
    return path.suffix.lower() in _BINARY_EXTENSIONS


# ---------------------------------------------------------------------------
# 读取上限（改造自 Hermes）
# ---------------------------------------------------------------------------

_DEFAULT_MAX_READ_CHARS = 100_000


def _get_max_read_chars() -> int:
    """Return configured max characters per file read."""
    try:
        from crew.state.config import load_config
        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("file", {}).get("read_max_chars")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    except Exception:
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
    shown_lines: int | None = None,
    truncated: bool = False,
    hint: str = "",
) -> str:
    """把读取结果格式化为 JSON，与 Hermes 返回结构接近。"""
    import json

    if shown_lines is None:
        shown_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    payload: dict = {
        "success": True,
        "content": content,
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
# file_read 读取引擎：头部嗅探 → 编码/流式路由 → 行窗口 → 行号渲染
# 窗口语义对齐 dsh tool-fs read（默认行数/行长截断/整行取舍预算/流式行缓冲），
# 尾读与编码策略对齐 kimi agent-core Read（负 offset 尾窗口、UTF-16 转码、
# 非 UTF-8 显式报错提示 iconv）。
# ---------------------------------------------------------------------------

#: 单次读取默认且最大的返回行数（tools.file.read_max_lines 可覆盖默认值）。
FILE_READ_MAX_LINES = 2000
#: 单行渲染上限；行缓冲封顶为该值 +1 字符，巨型单行内存恒定。
FILE_READ_LINE_LENGTH = 2000
#: 文件达到该大小改走流式读取（不整读进内存）。
FILE_READ_STREAM_MIN_BYTES = 10 * 1024 * 1024
#: 头部嗅探的采样字节数。
TEXT_SAMPLE_BYTES = 8192
#: UTF-16 自动转码的大小上限，超过要求先 iconv 转换。
UTF16_TRANSCODE_MAX_BYTES = 10 * 1024 * 1024
#: 流式读取的单块字节数。
_READ_CHUNK_BYTES = 1024 * 1024

#: 无 BOM UTF-16 奇偶 NUL 启发式只看前 512 字节（ASCII 的 UTF-16 高字节为 NUL，落单侧下标）。
_UTF16_SNIFF_BYTES = 512
_UTF16_MIN_NUL_RUNS = 2

#: 魔数表只收 ≥4 字节无歧义前缀；MZ/BMP/FLV 等短前缀由 NUL 检测兜底，避免误伤正文文本。
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),  # JPEG（第三字节固定 Fx，文本不可能合法出现）
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"II*\x00", "image"),  # TIFF
    (b"MM\x00*", "image"),
    (b"\x00\x00\x01\x00", "image"),  # ICO
    (b"\x1a\x45\xdf\xa3", "video"),  # EBML（webm/mkv）
    (b"%PDF", "binary"),
    (b"PK\x03\x04", "binary"),  # zip 家族（docx/xlsx/jar）
    (b"PK\x05\x06", "binary"),
    (b"PK\x07\x08", "binary"),
    (b"\xd0\xcf\x11\xe0", "binary"),  # OLE（doc/xls/ppt）
    (b"\x7fELF", "binary"),
    (b"\xfd7zXZ\x00", "binary"),
    (b"7z\xbc\xaf\x27\x1c", "binary"),
    (b"Rar!\x1a\x07", "binary"),
    (b"SQLite format 3\x00", "binary"),
)

#: 二进制文档扩展名：错误消息里给出 wiki/skills 通道引导。
_DOC_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".epub",
})


@dataclass(frozen=True)
class HeadSniff:
    """头部字节嗅探结果：encoding 为文本编码推断，media_kind 非空表示二进制。"""

    encoding: str = "utf-8"  # utf-8 / utf-8-sig / utf-16-le / utf-16-be / utf-32
    media_kind: str = ""  # image / video / binary；空串表示文本


@dataclass(frozen=True)
class WindowLine:
    number: int  # 文件内 1-based 绝对行号
    text: str  # 已剥 \r、已做行长截断的行文本


@dataclass(frozen=True)
class TextWindow:
    lines: list[WindowLine]
    total_lines: int  # 全文件精确行数（窗口集满后仍继续扫描统计）
    truncated_by_budget: bool = False  # 输出预算整行取舍触发
    truncated_line_numbers: list[int] = field(default_factory=list)  # 超长被截断的行号


@dataclass(frozen=True)
class FileReadOutcome:
    content: str
    total_lines: int
    file_size: int
    shown_lines: int
    truncated: bool
    hint: str
    version: FileVersion  # stat-only：观察记录只消费 (device, inode, size, mtime_ns)


def _sniff_head(head: bytes) -> HeadSniff:
    """按 BOM → 魔数 → 无 BOM UTF-16 启发式 → NUL → 控制字符比例 判定头部。"""
    if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        return HeadSniff(encoding="utf-32")
    if head.startswith(b"\xff\xfe"):
        return HeadSniff(encoding="utf-16-le")
    if head.startswith(b"\xfe\xff"):
        return HeadSniff(encoding="utf-16-be")
    if head.startswith(b"\xef\xbb\xbf"):
        return HeadSniff(encoding="utf-8-sig")

    if head[4:8] == b"ftyp":
        return HeadSniff(media_kind="video")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return HeadSniff(media_kind="image")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return HeadSniff(media_kind="video")
    for prefix, kind in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            return HeadSniff(media_kind=kind)

    sample = head[:_UTF16_SNIFF_BYTES]
    odd_nul = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
    even_nul = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
    if odd_nul >= _UTF16_MIN_NUL_RUNS and even_nul == 0:
        return HeadSniff(encoding="utf-16-le")
    if even_nul >= _UTF16_MIN_NUL_RUNS and odd_nul == 0:
        return HeadSniff(encoding="utf-16-be")

    if b"\x00" in head:
        return HeadSniff(media_kind="binary")

    # 控制字符比例：ANSI 转义序列在日志文本里常见，阈值取宽（>30%）避免误伤。
    decoded = head.decode("utf-8", errors="ignore")
    if decoded:
        controls = sum(
            1
            for ch in decoded
            if (ord(ch) < 0x20 and ch not in "\t\n\r\f\b") or 0x7F <= ord(ch) <= 0x9F
        )
        if controls / len(decoded) > 0.3:
            return HeadSniff(media_kind="binary")
    return HeadSniff()


def binary_reject_message(path: Path) -> str:
    """二进制内容的拒绝消息：按扩展名给出替代通道引导。"""
    suffix = path.suffix.lower()
    if suffix in _DOC_EXTENSIONS:
        return (
            f'"{path}" 是二进制文档（{suffix}），file_read 仅读取 UTF-8 文本；'
            "可用 wiki_capture_attachment 摄取为知识库页面，或用 skills_list 加载对应文档技能处理。"
        )
    return (
        f'"{path}" 不是可读的 UTF-8 文本（检测到二进制内容）。'
        "图片请改用 vision_analyze；文档可用 wiki_capture_attachment 摄取。"
    )


def _iter_capped_lines(chunks: Iterable[str], cap: int) -> Iterator[str]:
    """把文本 chunk 流切成行；行缓冲封顶 cap+1 字符，巨型单行内存恒定。

    行尾 \\r 剥离（CRLF 显示按 LF）；文件不以换行结尾时最后一段也作为一行。
    """
    buffer_cap = cap + 1
    buffer = ""

    def append(segment: str) -> None:
        nonlocal buffer
        if len(buffer) >= buffer_cap:
            return
        buffer += segment
        if len(buffer) > buffer_cap:
            buffer = buffer[:buffer_cap]

    for chunk in chunks:
        start = 0
        while True:
            newline = chunk.find("\n", start)
            if newline == -1:
                break
            append(chunk[start:newline])
            line, buffer = buffer, ""
            yield line[:-1] if line.endswith("\r") else line
            start = newline + 1
        append(chunk[start:])
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def _clip_line(text: str, max_line_length: int, truncated_numbers: list[int], number: int) -> str:
    if len(text) <= max_line_length:
        return text
    truncated_numbers.append(number)
    return text[:max_line_length] + f"...（行已截断至 {max_line_length} 字符）"


def build_text_window(
    chunks: Iterable[str],
    *,
    offset: int,
    limit: int,
    max_line_length: int = FILE_READ_LINE_LENGTH,
    max_chars: int = 100_000,
) -> TextWindow:
    """在文本 chunk 流上构建行窗口：offset≥1 为前向读取，offset<0 为尾读。"""
    if offset >= 1:
        return _build_window_forward(
            chunks, offset=offset, limit=limit, max_line_length=max_line_length, max_chars=max_chars
        )
    return _build_window_tail(
        chunks, offset=offset, limit=limit, max_line_length=max_line_length, max_chars=max_chars
    )


def _build_window_forward(
    chunks: Iterable[str],
    *,
    offset: int,
    limit: int,
    max_line_length: int,
    max_chars: int,
) -> TextWindow:
    lines: list[WindowLine] = []
    truncated_line_numbers: list[int] = []
    total = 0
    used = 0
    capped = False

    def consume(raw: str) -> None:
        nonlocal total, used, capped
        total += 1
        if capped or total < offset or len(lines) >= limit:
            return
        text = _clip_line(raw, max_line_length, truncated_line_numbers, total)
        cost = len(text) + (1 if lines else 0)
        if used + cost > max_chars:
            capped = True
            return
        used += cost
        lines.append(WindowLine(number=total, text=text))

    for line in _iter_capped_lines(chunks, max_line_length):
        consume(line)

    if not capped and offset > total and not (total == 0 and offset == 1):
        raise ToolError(f"offset {offset} 超出文件范围（共 {total} 行）")
    shown = {line.number for line in lines}
    return TextWindow(
        lines=lines,
        total_lines=total,
        truncated_by_budget=capped,
        truncated_line_numbers=[n for n in truncated_line_numbers if n in shown],
    )


def _build_window_tail(
    chunks: Iterable[str],
    *,
    offset: int,
    limit: int,
    max_line_length: int,
    max_chars: int,
) -> TextWindow:
    tail_count = -offset
    truncated_line_numbers: list[int] = []
    total = 0
    window: deque[WindowLine] = deque(maxlen=tail_count)
    for line in _iter_capped_lines(chunks, max_line_length):
        total += 1
        window.append(WindowLine(number=total, text=_clip_line(line, max_line_length, truncated_line_numbers, total)))

    # limit 从尾窗口头部截取；预算超限时从头部丢行，保留最接近文件末尾的内容。
    entries = list(window)[:limit]
    kept: list[WindowLine] = []
    used = 0
    capped = False
    for entry in reversed(entries):
        cost = len(entry.text) + (1 if kept else 0)
        if used + cost > max_chars:
            capped = True
            break
        used += cost
        kept.append(entry)
    kept.reverse()
    shown = {entry.number for entry in kept}
    return TextWindow(
        lines=kept,
        total_lines=total,
        truncated_by_budget=capped,
        truncated_line_numbers=[n for n in truncated_line_numbers if n in shown],
    )


def render_read_window(window: TextWindow, *, offset: int) -> str:
    """把窗口渲染为「行号: 内容」正文 + 续读提示 footer。"""
    body = [f"{line.number}: {line.text}" for line in window.lines]
    count = len(window.lines)
    if offset >= 1:
        end = window.lines[-1].number if count else max(0, offset - 1)
        if window.truncated_by_budget:
            footer = f"(输出已达上限。已显示第 {offset}-{end} 行。用 offset={end + 1} 继续读取。)"
        elif end < window.total_lines:
            footer = f"(已显示第 {offset}-{end} 行，共 {window.total_lines} 行。用 offset={end + 1} 继续读取。)"
        else:
            footer = f"(文件结束，共 {window.total_lines} 行)"
    else:
        if count:
            start, end = window.lines[0].number, window.lines[-1].number
            footer = f"(已读取 {count} 行（第 {start}-{end} 行），文件共 {window.total_lines} 行)"
        else:
            footer = f"(文件结束，共 {window.total_lines} 行)"
        if window.truncated_by_budget:
            footer = f"{footer} 输出已达字符上限，已保留最接近文件末尾的行；可用更小的 limit 分段读取。"
    return "\n".join(body + ["", footer]) if body else footer


def validate_read_window_args(
    offset: object,
    limit: object,
    *,
    max_lines: int | None = None,
) -> tuple[int, int]:
    """校验 file_read 的 offset/limit：正 offset 前向、负 offset 尾读。"""
    if max_lines is None:
        max_lines = _get_max_read_lines()
    if offset is None:
        offset = 1
    else:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            raise ToolError("offset 必须是整数")
        if offset == 0:
            raise ToolError("offset 不能为 0：正数表示 1-based 起始行，负数表示从文件末尾读取")
        if offset < 0 and -offset > max_lines:
            raise ToolError(f"负 offset 的绝对值不能超过 {max_lines}")
    if limit is None:
        limit = max_lines
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ToolError("limit 必须是正整数")
        if limit < 1:
            raise ToolError("limit 必须是正整数")
        if limit > max_lines:
            raise ToolError(f"limit 不能超过 {max_lines}")
    return offset, limit


def _get_max_read_lines() -> int:
    """读取配置的单次返回行数上限（tools.file.read_max_lines）。"""
    try:
        from crew.state.config import load_config

        cfg = load_config()
        val = cfg.raw_config.get("tools", {}).get("file", {}).get("read_max_lines")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    except Exception:
        pass
    return FILE_READ_MAX_LINES


def _stream_utf8_chunks(handle: BinaryIO, *, strip_bom: bool) -> Iterator[str]:
    """从已打开句柄流式产出严格解码的 UTF-8 文本块。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    first = True
    while True:
        block = handle.read(_READ_CHUNK_BYTES)
        if not block:
            tail = decoder.decode(b"", final=True)
            if tail:
                yield tail.removeprefix("\ufeff") if first and strip_bom else tail
            return
        piece = decoder.decode(block)
        if piece:
            if first and strip_bom:
                piece = piece.removeprefix("\ufeff")
            first = False
            yield piece


@contextmanager
def open_verified_read(path: Path) -> Iterator[tuple[os.stat_result, BinaryIO]]:
    """身份校验后打开只读句柄（不整读），供流式读取大文件。

    校验规则与整读路径一致：拒绝符号链接/非常规文件/多硬链接，打开前后身份一致。
    """
    canonical = _lexical_absolute(path)
    with _pinned_parent(canonical) as parent_descriptor:
        if os.name == "nt":
            before = canonical.lstat()

            def opener(flags: int) -> int:
                return os.open(canonical, flags)

        else:
            before = os.stat(canonical.name, dir_fd=parent_descriptor, follow_symlinks=False)

            def opener(flags: int) -> int:
                return os.open(canonical.name, flags, dir_fd=parent_descriptor)

        descriptor, opened = _open_verified(before, opener)
        handle = os.fdopen(descriptor, "rb")
        try:
            yield opened, handle
        finally:
            handle.close()


def _utf16_label(encoding: str) -> str:
    return "UTF-16 LE" if encoding == "utf-16-le" else "UTF-16 BE"


def read_file_window(
    path: Path,
    *,
    offset: int,
    limit: int,
) -> FileReadOutcome:
    """file_read 主流程：嗅探 → 编码/流式路由 → 行窗口 → 行号渲染。

    成功返回带 stat-only FileVersion 的结果；写入侧的 stale 校验只消费
    stat 四元组，因此这里不整读、不计算内容摘要。
    """
    max_chars = _get_max_read_chars()
    hints: list[str] = []

    with open_verified_read(path) as (info, handle):
        head = handle.read(TEXT_SAMPLE_BYTES)
        handle.seek(0)
        sniff = _sniff_head(head)

        if sniff.encoding == "utf-32":
            raise ToolError(
                f'"{path}" 是 UTF-32 编码，file_read 不支持；请先用 terminal 执行 iconv 转为 UTF-8 后再读取。'
            )
        if sniff.media_kind == "image":
            raise ToolError(f'"{path}" 是图片文件，file_read 仅读取文本；请改用 vision_analyze 分析图片。')
        if sniff.media_kind == "video":
            raise ToolError(f'"{path}" 是视频文件，file_read 仅读取文本。')
        if sniff.media_kind == "binary":
            raise ToolError(binary_reject_message(path))

        def build(chunks: Iterable[str]) -> TextWindow:
            return build_text_window(
                chunks, offset=offset, limit=limit, max_chars=max_chars
            )

        if sniff.encoding == "utf-8-sig":
            hints.append("文件包含 UTF-8 BOM，已自动剥离显示。")

        try:
            if sniff.encoding in ("utf-16-le", "utf-16-be"):
                if info.st_size > UTF16_TRANSCODE_MAX_BYTES:
                    raise ToolError(
                        f'"{path}" 是 {_utf16_label(sniff.encoding)} 文本但超过转码上限'
                        f"（{info.st_size} 字节 > {UTF16_TRANSCODE_MAX_BYTES}）；"
                        "请先用 terminal 执行 iconv 转为 UTF-8 后再读取。"
                    )
                text = handle.read().decode(sniff.encoding, errors="replace")
                window = build([text.removeprefix("\ufeff")])
                hints.append(
                    f"检测到 {_utf16_label(sniff.encoding)} 编码，已转码为 UTF-8 显示；"
                    "编辑/写入要求 UTF-8，请先用 terminal 执行 iconv 转换文件编码。"
                )
            elif info.st_size >= FILE_READ_STREAM_MIN_BYTES:
                window = build(_stream_utf8_chunks(handle, strip_bom=sniff.encoding == "utf-8-sig"))
            else:
                text = handle.read().decode("utf-8")
                window = build([text.removeprefix("\ufeff") if sniff.encoding == "utf-8-sig" else text])
        except UnicodeDecodeError as exc:
            raise ToolError(
                f'"{path}" 不是有效的 UTF-8 文本。'
                "GBK 等其他编码请先用 terminal 执行 iconv 转换后再读取。"
            ) from exc

        after = os.fstat(handle.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise FileConflictError("文件在读取期间已变化，请重试")

    if window.truncated_line_numbers:
        shown_numbers = "、".join(str(n) for n in window.truncated_line_numbers[:20])
        suffix = " 等" if len(window.truncated_line_numbers) > 20 else ""
        hints.append(
            f"第 {shown_numbers}{suffix} 行超过 {FILE_READ_LINE_LENGTH} 字符已截断；"
            "可用 terminal 的 cut 或 sed 查看完整行。"
        )

    content = render_read_window(window, offset=offset)
    end = window.lines[-1].number if window.lines else 0
    truncated = end < window.total_lines or window.truncated_by_budget or bool(window.truncated_line_numbers)
    version = FileVersion(
        path=_lexical_absolute(path),
        exists=True,
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        mode=after.st_mode,
    )
    return FileReadOutcome(
        content=content,
        total_lines=window.total_lines,
        file_size=info.st_size,
        shown_lines=len(window.lines),
        truncated=truncated,
        hint="\n".join(hints),
        version=version,
    )


def slice_text_window(
    text: str,
    offset: object,
    limit: object,
    *,
    max_lines: int | None = None,
) -> tuple[str, int, int]:
    """把整段文本按 file_read 的 offset/limit 语义切片（负 offset = 尾读）。

    宽容归一（不抛错）：供压缩恢复等只需要纯文本窗口的场景复用。
    返回 (切片文本, 起始行号 ≥1, 总行数)。
    """
    if max_lines is None:
        max_lines = _get_max_read_lines()
    try:
        offset_i = int(offset) if offset is not None else 1
    except (TypeError, ValueError):
        offset_i = 1
    if offset_i == 0:
        offset_i = 1
    offset_i = max(offset_i, -max_lines)
    try:
        limit_i = int(limit) if limit is not None else max_lines
    except (TypeError, ValueError):
        limit_i = max_lines
    limit_i = max(1, min(limit_i, max_lines))

    lines = text.splitlines(keepends=True)
    total = len(lines)
    if offset_i < 0:
        start = max(0, total + offset_i)
    else:
        start = min(offset_i - 1, total)
    window = lines[start : start + limit_i]
    return "".join(window), start + 1, total


# ---------------------------------------------------------------------------
# 通用输出截断（多个工具 handler 共用）
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 12000) -> str:
    """截断超长文本，避免 tool result 超出模型上下文。"""
    return text if len(text) <= limit else text[:limit] + "\n...[内容已截断]"
