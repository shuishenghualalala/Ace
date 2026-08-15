"""Race-resistant, append-only recording trace writes on Windows.

The POSIX implementation in :mod:`crew.browser.manager` is intentionally based
on ``openat(2)`` directory descriptors.  Windows has no equivalent pathname
API, so this module keeps non-delete-share directory handles open, validates the
kernel-resolved path of every handle, and writes through a handle whose only
data permission is ``FILE_APPEND_DATA``.

The orchestration is separated from the ctypes facade so the security contract
can be exercised on non-Windows CI without pretending POSIX symlinks have
Windows junction semantics.
"""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import re
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Protocol


_TRACE_NAME = "trace.jsonl"
_INCOMPLETE_NAME = "INCOMPLETE"
_SESSION_COMPONENT = re.compile(r"[0-9a-f]{16}")
_RECORDING_COMPONENT = re.compile(r"[0-9a-f]{8,32}")


@dataclass(frozen=True)
class Win32FileIdentity:
    """Stable identity and security-relevant metadata for one open handle."""

    volume_serial: int
    file_index: int
    size: int
    link_count: int
    is_directory: bool
    is_reparse_point: bool
    is_disk_file: bool
    # FILE_APPEND_DATA without FILE_WRITE_DATA has an overwrite-prevention
    # guarantee only for local files. Mapped/UNC filesystems fail closed.
    is_local_fixed_disk: bool


class Win32RecordingAPI(Protocol):
    """Small injectable Win32 surface used by the append algorithm."""

    def create_private_directory(self, path: str) -> None: ...

    def open_directory(self, path: str) -> Any: ...

    def open_append_file(self, path: str) -> tuple[Any, bool]: ...

    def open_file_audit(self, path: str) -> Any: ...

    def final_path(self, handle: Any) -> str: ...

    def identity(self, handle: Any) -> Win32FileIdentity: ...

    def security_is_current_user_only(self, handle: Any) -> bool: ...

    def write(self, handle: Any, payload: memoryview) -> int: ...

    def flush(self, handle: Any) -> None: ...

    def close(self, handle: Any) -> None: ...


def _canonical_windows_path(path: str | os.PathLike[str]) -> str:
    """Canonical comparison form for a kernel-resolved Windows path."""
    value = os.fspath(path).replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    value = ntpath.normpath(value)
    if not ntpath.isabs(value):
        raise OSError(errno.EINVAL, "Windows 录制路径必须是绝对路径")
    return ntpath.normcase(value)


def _validated_recording_paths(
    owner_home: str | os.PathLike[str],
    directory: str | os.PathLike[str],
) -> tuple[str, tuple[str, str, str], str]:
    raw_owner = os.fspath(owner_home).replace("/", "\\")
    raw_directory = os.fspath(directory).replace("/", "\\")
    if any(
        component in {".", ".."} for component in ntpath.splitdrive(raw_directory)[1].split("\\")
    ):
        raise OSError(errno.EINVAL, "录制目录不能包含相对路径组件")
    try:
        raw_relative = ntpath.relpath(
            ntpath.normpath(raw_directory),
            ntpath.normpath(raw_owner),
        )
    except ValueError as exc:
        raise OSError(errno.EPERM, "录制目录越过 owner 私有根目录") from exc
    raw_parts = tuple(part for part in raw_relative.split("\\") if part)
    if (
        len(raw_parts) != 3
        or raw_parts[0] != "recordings"
        or _SESSION_COMPONENT.fullmatch(raw_parts[1]) is None
        or _RECORDING_COMPONENT.fullmatch(raw_parts[2]) is None
    ):
        raise OSError(errno.EINVAL, "录制目录名称不是规范形式")
    owner = _canonical_windows_path(owner_home)
    destination = _canonical_windows_path(directory)
    try:
        relative = ntpath.relpath(destination, owner)
    except ValueError as exc:
        raise OSError(errno.EPERM, "录制目录越过 owner 私有根目录") from exc
    parts = tuple(part for part in relative.split("\\") if part)
    if (
        len(parts) != 3
        or parts[0].casefold() != "recordings"
        or _SESSION_COMPONENT.fullmatch(parts[1]) is None
        or _RECORDING_COMPONENT.fullmatch(parts[2]) is None
        or relative == ".."
        or relative.startswith("..\\")
    ):
        raise OSError(errno.EINVAL, "录制目录结构无效")
    # Component spelling is part of the capability.  In particular, do not let
    # case-insensitive aliases turn an unexpected directory into the trace root.
    parts = ("recordings", parts[1], parts[2])
    exact_directory = ntpath.join(owner, *parts)
    if destination != ntpath.normcase(exact_directory):
        raise OSError(errno.EINVAL, "录制目录名称不是规范形式")
    return owner, parts, ntpath.join(exact_directory, _TRACE_NAME)


def _require_directory(
    api: Win32RecordingAPI,
    handle: Any,
    expected_path: str,
) -> Win32FileIdentity:
    identity = api.identity(handle)
    if (
        not identity.is_disk_file
        or not identity.is_local_fixed_disk
        or not identity.is_directory
        or identity.is_reparse_point
        or identity.link_count < 1
        or _canonical_windows_path(api.final_path(handle)) != _canonical_windows_path(expected_path)
        or not api.security_is_current_user_only(handle)
    ):
        raise OSError(errno.EPERM, "Windows 录制目录不是当前用户私有的稳定目录")
    return identity


def _require_owner_anchor(
    api: Win32RecordingAPI,
    handle: Any,
    expected_path: str,
) -> Win32FileIdentity:
    """Validate and pin owner_home without rewriting its broader ACL.

    Every child directory is private, but the owner root can predate this
    feature and contain other Crew state. Holding its no-delete-share handle is
    what closes the ancestor-rename race; changing its DACL here would be an
    unrelated, potentially destructive migration.
    """
    identity = api.identity(handle)
    if (
        not identity.is_disk_file
        or not identity.is_local_fixed_disk
        or not identity.is_directory
        or identity.is_reparse_point
        or identity.link_count < 1
        or _canonical_windows_path(api.final_path(handle))
        != _canonical_windows_path(expected_path)
    ):
        raise OSError(errno.EPERM, "Windows owner 录制根不是稳定目录（须为本地固定磁盘）")
    return identity


def _require_trace_file(
    api: Win32RecordingAPI,
    append_handle: Any,
    audit_handle: Any,
    expected_path: str,
    *,
    label: str = "录制轨迹",
) -> Win32FileIdentity:
    append_identity = api.identity(append_handle)
    audit_identity = api.identity(audit_handle)
    if (
        append_identity != audit_identity
        or not append_identity.is_disk_file
        or not append_identity.is_local_fixed_disk
        or append_identity.is_directory
        or append_identity.is_reparse_point
        or append_identity.link_count != 1
        or _canonical_windows_path(api.final_path(append_handle))
        != _canonical_windows_path(expected_path)
        or _canonical_windows_path(api.final_path(audit_handle))
        != _canonical_windows_path(expected_path)
        or not api.security_is_current_user_only(audit_handle)
    ):
        raise OSError(errno.EPERM, f"Windows {label}不是当前用户私有的稳定普通文件")
    return append_identity


def secure_append_recording_line(
    owner_home: str | os.PathLike[str],
    directory: str | os.PathLike[str],
    payload: bytes,
    max_bytes: int,
    *,
    api: Win32RecordingAPI | None = None,
) -> None:
    """Append one complete JSONL record using validated Win32 handles.

    Directory handles deny delete sharing and remain open through the final
    validation.  The append handle asks for no arbitrary-offset write right.
    An independent read-control handle audits the DACL without broadening the
    writer's access mask.
    """
    _secure_append_recording_file(
        owner_home,
        directory,
        payload,
        max_bytes,
        leaf_name=_TRACE_NAME,
        api=api,
        only_if_empty=False,
    )


def secure_ensure_recording_marker(
    owner_home: str | os.PathLike[str],
    directory: str | os.PathLike[str],
    *,
    api: Win32RecordingAPI | None = None,
) -> None:
    """Create the fixed-content incomplete marker through the same safe handles.

    The marker is integrity state, not user content, but a pathname-only create
    would still let a reparse point turn a failed recording into a misleading
    success.  Existing non-empty markers are left untouched and revalidated.
    """
    _secure_append_recording_file(
        owner_home,
        directory,
        b"recording-incomplete\n",
        128,
        leaf_name=_INCOMPLETE_NAME,
        api=api,
        only_if_empty=True,
    )


def _secure_append_recording_file(
    owner_home: str | os.PathLike[str],
    directory: str | os.PathLike[str],
    payload: bytes,
    max_bytes: int,
    *,
    leaf_name: str,
    api: Win32RecordingAPI | None,
    only_if_empty: bool,
) -> None:
    if leaf_name not in {_TRACE_NAME, _INCOMPLETE_NAME}:
        raise ValueError("Windows 录制文件名无效")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("Windows 录制 payload 必须是非空 bytes")
    if len(payload) > max_bytes:
        raise OSError(errno.EFBIG, "录制文件超过大小上限")
    owner, parts, trace_path = _validated_recording_paths(owner_home, directory)
    target_path = ntpath.join(ntpath.dirname(trace_path), leaf_name)
    winapi: Win32RecordingAPI = api if api is not None else CtypesWin32RecordingAPI()
    directory_handles: list[tuple[Any, str, Win32FileIdentity]] = []
    append_handle: Any | None = None
    audit_handle: Any | None = None
    try:
        current = owner
        # Anchor the full capability chain, not only recordings/session/id.
        # Otherwise another process could rename owner_home while all three
        # descendant handles still resolve to internally consistent objects.
        winapi.create_private_directory(owner)
        owner_handle = winapi.open_directory(owner)
        try:
            owner_identity = _require_owner_anchor(winapi, owner_handle, owner)
        except BaseException:
            winapi.close(owner_handle)
            raise
        directory_handles.append((owner_handle, owner, owner_identity))
        for component in parts:
            current = ntpath.join(current, component)
            # CreateDirectoryW is atomic for the named component.  An existing
            # object is accepted only after the no-follow handle validation.
            winapi.create_private_directory(current)
            handle = winapi.open_directory(current)
            try:
                identity = _require_directory(winapi, handle, current)
            except BaseException:
                winapi.close(handle)
                raise
            directory_handles.append((handle, current, identity))

        append_handle, _created = winapi.open_append_file(target_path)
        audit_handle = winapi.open_file_audit(target_path)
        before = _require_trace_file(
            winapi,
            append_handle,
            audit_handle,
            target_path,
            label=("录制轨迹" if leaf_name == _TRACE_NAME else "录制完整性标记"),
        )
        write_payload = not (only_if_empty and before.size > 0)
        if before.size > max_bytes or (
            write_payload and before.size > max_bytes - len(payload)
        ):
            raise OSError(errno.EFBIG, "录制轨迹超过大小上限")
        # A parent can be renamed only if every open handle allowed delete
        # sharing.  The real facade denies it; this second validation also makes
        # that invariant explicit and fail-closed for alternate filesystems.
        for index, (handle, expected, initial) in enumerate(directory_handles):
            current_identity = (
                _require_owner_anchor(winapi, handle, expected)
                if index == 0
                else _require_directory(winapi, handle, expected)
            )
            if (
                current_identity.volume_serial,
                current_identity.file_index,
            ) != (initial.volume_serial, initial.file_index):
                raise OSError(errno.EPERM, "Windows 录制目录在追加前被替换")

        if write_payload:
            view = memoryview(payload)
            while view:
                # Do not seek.  FILE_APPEND_DATA without FILE_WRITE_DATA is the
                # kernel-enforced append primitive for local files; SetFilePointerEx
                # would require broader GENERIC_READ/GENERIC_WRITE authority and
                # would reintroduce an arbitrary-offset write surface.
                written = winapi.write(append_handle, view)
                if written <= 0 or written > len(view):
                    raise OSError(errno.EIO, "Windows 录制文件追加失败")
                view = view[written:]
            winapi.flush(append_handle)

        after = _require_trace_file(
            winapi,
            append_handle,
            audit_handle,
            target_path,
            label=("录制轨迹" if leaf_name == _TRACE_NAME else "录制完整性标记"),
        )
        expected_size = before.size + len(payload) if write_payload else before.size
        if after.size != expected_size or after.size > max_bytes:
            raise OSError(errno.EIO, "Windows 录制文件写后大小校验失败")

        for index, (handle, expected, initial) in enumerate(directory_handles):
            current_identity = (
                _require_owner_anchor(winapi, handle, expected)
                if index == 0
                else _require_directory(winapi, handle, expected)
            )
            if (
                current_identity.volume_serial,
                current_identity.file_index,
            ) != (initial.volume_serial, initial.file_index):
                raise OSError(errno.EPERM, "Windows 录制目录在追加期间被替换")
    finally:
        if audit_handle is not None:
            with suppress(Exception):
                winapi.close(audit_handle)
        if append_handle is not None:
            with suppress(Exception):
                winapi.close(append_handle)
        for handle, _expected, _identity in reversed(directory_handles):
            with suppress(Exception):
                winapi.close(handle)


# Win32 constants are repeated here rather than imported from pywin32 so the
# desktop distribution keeps a zero-dependency security boundary.
_FILE_APPEND_DATA = 0x00000004
_FILE_READ_ATTRIBUTES = 0x00000080
_READ_CONTROL = 0x00020000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_TYPE_DISK = 0x0001
_DRIVE_FIXED = 3
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_FILE_ALL_ACCESS = 0x001F01FF
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
# ``_ensure_private_directory`` uses a protected SYSTEM + OWNER RIGHTS DACL;
# accept those two maintenance principals while still requiring the current
# process owner and rejecting every other ACE.
_SYSTEM_SID = b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00"
_OWNER_RIGHTS_SID = b"\x01\x01\x00\x00\x00\x00\x00\x03\x04\x00\x00\x00"


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _ByHandleFileInformation(ctypes.Structure):
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


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class CtypesWin32RecordingAPI:
    """Minimal kernel32/advapi32 implementation of :class:`Win32RecordingAPI`."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError(errno.ENOTSUP, "Win32 安全追加仅能在 Windows 上运行")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure_functions()
        self._current_sid = self._read_current_user_sid()

    def _configure_functions(self) -> None:
        """Declare pointer-sized signatures; ctypes' ``c_int`` default is unsafe."""
        pointer = ctypes.POINTER
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL
        self._kernel32.CreateDirectoryW.argtypes = [
            wintypes.LPCWSTR,
            pointer(_SecurityAttributes),
        ]
        self._kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            pointer(_SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            pointer(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        self._kernel32.GetFileType.restype = wintypes.DWORD
        self._kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetDriveTypeW.restype = wintypes.UINT
        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            pointer(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            pointer(wintypes.HANDLE),
        ]
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            pointer(wintypes.DWORD),
        ]
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]
        self._advapi32.GetLengthSid.restype = wintypes.DWORD
        self._advapi32.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            pointer(wintypes.LPWSTR),
        ]
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            pointer(wintypes.LPVOID),
            pointer(wintypes.DWORD),
        ]
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self._advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            pointer(wintypes.LPVOID),
            pointer(wintypes.LPVOID),
            pointer(wintypes.LPVOID),
            pointer(wintypes.LPVOID),
            pointer(wintypes.LPVOID),
        ]
        self._advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self._advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
        self._advapi32.EqualSid.restype = wintypes.BOOL
        self._advapi32.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID,
            pointer(wintypes.WORD),
            pointer(wintypes.DWORD),
        ]
        self._advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self._advapi32.GetAclInformation.argtypes = [
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.GetAclInformation.restype = wintypes.BOOL
        self._advapi32.GetAce.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            pointer(wintypes.LPVOID),
        ]
        self._advapi32.GetAce.restype = wintypes.BOOL

    @staticmethod
    def _raise_last_error(message: str) -> None:
        code = ctypes.get_last_error()
        raise OSError(code, message)

    def _read_current_user_sid(self) -> bytes:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            self._raise_last_error("无法打开当前用户 token")
        try:
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
            if not required.value:
                self._raise_last_error("无法读取当前用户 SID 大小")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                self._raise_last_error("无法读取当前用户 SID")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_length = self._advapi32.GetLengthSid(sid_pointer)
            if not sid_length:
                self._raise_last_error("当前用户 SID 无效")
            return ctypes.string_at(sid_pointer, sid_length)
        finally:
            self._kernel32.CloseHandle(token)

    def _sid_pointer(self) -> tuple[Any, Any]:
        buffer = ctypes.create_string_buffer(self._current_sid)
        return buffer, ctypes.cast(buffer, wintypes.LPVOID)

    def _private_security_attributes(self) -> tuple[_SecurityAttributes, Any]:
        sid_buffer, sid_pointer = self._sid_pointer()
        sid_text = wintypes.LPWSTR()
        if not self._advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            self._raise_last_error("无法格式化当前用户 SID")
        try:
            sddl = f"D:P(A;;FA;;;{sid_text.value})"
        finally:
            self._kernel32.LocalFree(sid_text)
        descriptor = wintypes.LPVOID()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            None,
        ):
            if descriptor:
                self._kernel32.LocalFree(descriptor)
            self._raise_last_error("无法创建私有安全描述符")
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            descriptor,
            False,
        )
        # Keep the SID buffer alive until conversion has finished (the
        # descriptor itself is self-contained).
        _ = sid_buffer
        return attributes, descriptor

    def create_private_directory(self, path: str) -> None:
        attributes, descriptor = self._private_security_attributes()
        try:
            if self._kernel32.CreateDirectoryW(path, ctypes.byref(attributes)):
                return
            code = ctypes.get_last_error()
            if code not in (_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS):
                raise OSError(code, "无法创建 Windows 私有录制目录")
        finally:
            self._kernel32.LocalFree(descriptor)

    def _create_file(
        self,
        path: str,
        access: int,
        share: int,
        disposition: int,
        flags: int,
        *,
        private_on_create: bool = False,
    ) -> tuple[Any, bool]:
        descriptor = None
        attributes_pointer = None
        if private_on_create:
            attributes, descriptor = self._private_security_attributes()
            attributes_pointer = ctypes.byref(attributes)
        try:
            ctypes.set_last_error(0)
            handle = self._kernel32.CreateFileW(
                path,
                access,
                share,
                attributes_pointer,
                disposition,
                flags,
                None,
            )
            invalid = wintypes.HANDLE(-1).value
            if handle == invalid:
                self._raise_last_error("无法打开 Windows 录制对象")
            created = disposition == _OPEN_ALWAYS and (
                ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
            )
            return handle, created
        finally:
            if descriptor is not None:
                self._kernel32.LocalFree(descriptor)

    def open_directory(self, path: str) -> Any:
        handle, _ = self._create_file(
            path,
            _FILE_READ_ATTRIBUTES | _READ_CONTROL,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        return handle

    def open_append_file(self, path: str) -> tuple[Any, bool]:
        return self._create_file(
            path,
            _FILE_APPEND_DATA,
            _FILE_SHARE_READ,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            private_on_create=True,
        )

    def open_file_audit(self, path: str) -> Any:
        handle, _ = self._create_file(
            path,
            _FILE_READ_ATTRIBUTES | _READ_CONTROL,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        return handle

    def final_path(self, handle: Any) -> str:
        required = self._kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not required:
            self._raise_last_error("无法解析 Windows 录制 handle 最终路径")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = self._kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            self._raise_last_error("Windows 录制 handle 最终路径被截断")
        return buffer.value

    def identity(self, handle: Any) -> Win32FileIdentity:
        info = _ByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            self._raise_last_error("无法读取 Windows 录制 handle 元数据")
        file_type = self._kernel32.GetFileType(handle)
        resolved = _canonical_windows_path(self.final_path(handle))
        drive, _tail = ntpath.splitdrive(resolved)
        drive_root = drive + "\\" if drive else ""
        is_local_fixed_disk = bool(
            drive_root
            and self._kernel32.GetDriveTypeW(drive_root) == _DRIVE_FIXED
        )
        return Win32FileIdentity(
            volume_serial=int(info.dwVolumeSerialNumber),
            file_index=(int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
            size=(int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
            link_count=int(info.nNumberOfLinks),
            is_directory=bool(info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY),
            is_reparse_point=bool(info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            is_disk_file=file_type == _FILE_TYPE_DISK,
            is_local_fixed_disk=is_local_fixed_disk,
        )

    def security_is_current_user_only(self, handle: Any) -> bool:
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        status = self._advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            if descriptor:
                self._kernel32.LocalFree(descriptor)
            raise OSError(status, "无法读取 Windows 录制对象安全描述符")
        try:
            sid_buffer, current_sid = self._sid_pointer()
            _ = sid_buffer
            if not owner or not self._advapi32.EqualSid(owner, current_sid):
                return False
            if not dacl:
                return False
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not self._advapi32.GetSecurityDescriptorControl(
                descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                self._raise_last_error("无法读取 Windows DACL 控制标志")
            if not control.value & _SE_DACL_PROTECTED:
                return False
            acl_info = _AclSizeInformation()
            if not self._advapi32.GetAclInformation(
                dacl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                _ACL_SIZE_INFORMATION,
            ):
                self._raise_last_error("无法读取 Windows DACL")
            if acl_info.AceCount < 1:
                return False
            system_buffer = ctypes.create_string_buffer(_SYSTEM_SID)
            owner_rights_buffer = ctypes.create_string_buffer(_OWNER_RIGHTS_SID)
            system_sid = ctypes.cast(system_buffer, wintypes.LPVOID)
            owner_rights_sid = ctypes.cast(owner_rights_buffer, wintypes.LPVOID)
            for index in range(acl_info.AceCount):
                ace = wintypes.LPVOID()
                if not self._advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    self._raise_last_error("无法读取 Windows DACL ACE")
                header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if header.AceType != _ACCESS_ALLOWED_ACE_TYPE or header.AceSize < 12:
                    return False
                mask = ctypes.cast(
                    int(ace.value) + 4, ctypes.POINTER(wintypes.DWORD)
                ).contents.value
                ace_sid = wintypes.LPVOID(int(ace.value) + 8)
                allowed_principal = (
                    self._advapi32.EqualSid(ace_sid, current_sid)
                    or self._advapi32.EqualSid(ace_sid, system_sid)
                    or self._advapi32.EqualSid(ace_sid, owner_rights_sid)
                )
                if mask & _FILE_ALL_ACCESS != _FILE_ALL_ACCESS or not allowed_principal:
                    return False
            return True
        finally:
            self._kernel32.LocalFree(descriptor)

    def write(self, handle: Any, payload: memoryview) -> int:
        data = bytes(payload)
        buffer = ctypes.create_string_buffer(data)
        written = wintypes.DWORD()
        if not self._kernel32.WriteFile(
            handle,
            buffer,
            len(data),
            ctypes.byref(written),
            None,
        ):
            self._raise_last_error("Windows 录制轨迹 WriteFile 失败")
        return int(written.value)

    def flush(self, handle: Any) -> None:
        if not self._kernel32.FlushFileBuffers(handle):
            self._raise_last_error("Windows 录制轨迹 FlushFileBuffers 失败")

    def close(self, handle: Any) -> None:
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("无法关闭 Windows 录制 handle")
