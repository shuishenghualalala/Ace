"""Minimal zero-dependency Windows ACL protection for Gateway key material.

The policy mirrors the native security runtime's host-state policy: the current
interactive user owns the object and the protected DACL contains exactly three
full-control allow ACEs (current user, LOCAL SYSTEM, and Administrators).
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from ctypes import wintypes
from functools import lru_cache

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_WIN_LOCAL_SYSTEM_SID = 22
_WIN_BUILTIN_ADMINISTRATORS_SID = 26

_SET_ACCESS = 2
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_UNKNOWN = 0
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACL_SIZE_INFORMATION = 2
_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", wintypes.LPVOID),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", wintypes.LPWSTR),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TrusteeW),
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


class _WindowsAclApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError(errno.ENOTSUP, "Windows ACL API 仅能在 Windows 上运行")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure_functions()
        self._sids = (
            self._current_user_sid(),
            self._well_known_sid(_WIN_LOCAL_SYSTEM_SID),
            self._well_known_sid(_WIN_BUILTIN_ADMINISTRATORS_SID),
        )

    def _configure_functions(self) -> None:
        pointer = ctypes.POINTER
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

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
        self._advapi32.CreateWellKnownSid.argtypes = [
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            pointer(wintypes.DWORD),
        ]
        self._advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self._advapi32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG,
            pointer(_ExplicitAccessW),
            wintypes.LPVOID,
            pointer(wintypes.LPVOID),
        ]
        self._advapi32.SetEntriesInAclW.restype = wintypes.DWORD
        self._advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        self._advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self._advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self._advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self._advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self._advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self._advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
        self._advapi32.EqualSid.restype = wintypes.BOOL
        self._advapi32.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
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
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self._advapi32.GetAce.restype = wintypes.BOOL

    @staticmethod
    def _raise_last_error(message: str) -> None:
        code = ctypes.get_last_error()
        raise OSError(code, message)

    def _current_user_sid(self) -> bytes:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            self._raise_last_error("无法打开当前用户 token")
        try:
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token,
                _TOKEN_USER,
                None,
                0,
                ctypes.byref(required),
            )
            if not required.value:
                self._raise_last_error("无法读取当前用户 SID 大小")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_USER,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                self._raise_last_error("无法读取当前用户 SID")
            sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
            length = self._advapi32.GetLengthSid(sid)
            if not length:
                self._raise_last_error("当前用户 SID 无效")
            return ctypes.string_at(sid, length)
        finally:
            self._kernel32.CloseHandle(token)

    def _well_known_sid(self, kind: int) -> bytes:
        required = wintypes.DWORD()
        self._advapi32.CreateWellKnownSid(kind, None, None, ctypes.byref(required))
        if not required.value:
            self._raise_last_error(f"无法读取 well-known SID {kind} 大小")
        buffer = ctypes.create_string_buffer(required.value)
        if not self._advapi32.CreateWellKnownSid(
            kind,
            None,
            buffer,
            ctypes.byref(required),
        ):
            self._raise_last_error(f"无法创建 well-known SID {kind}")
        return bytes(buffer.raw[: required.value])

    def _sid_pointers(self) -> tuple[list[ctypes.Array], list[wintypes.LPVOID]]:
        buffers = [ctypes.create_string_buffer(sid) for sid in self._sids]
        pointers = [ctypes.cast(buffer, wintypes.LPVOID) for buffer in buffers]
        return buffers, pointers

    def _new_dacl(self, *, directory: bool) -> tuple[wintypes.LPVOID, list[ctypes.Array]]:
        sid_buffers, sid_pointers = self._sid_pointers()
        inheritance = (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        )
        entries = (_ExplicitAccessW * len(sid_pointers))()
        for index, sid in enumerate(sid_pointers):
            entries[index] = _ExplicitAccessW(
                _FILE_ALL_ACCESS,
                _SET_ACCESS,
                inheritance,
                _TrusteeW(
                    None,
                    0,
                    _TRUSTEE_IS_SID,
                    _TRUSTEE_IS_UNKNOWN,
                    ctypes.cast(sid, wintypes.LPWSTR),
                ),
            )
        dacl = wintypes.LPVOID()
        status = self._advapi32.SetEntriesInAclW(
            len(entries),
            entries,
            None,
            ctypes.byref(dacl),
        )
        if status != 0 or not dacl:
            if dacl:
                self._kernel32.LocalFree(dacl)
            raise OSError(status, "无法创建 Gateway 密钥 DACL")
        # The entries and SID buffers must stay alive until Set*SecurityInfo returns.
        return dacl, [*sid_buffers, entries]

    def protect_path(self, path: str, *, directory: bool) -> None:
        dacl, keepalive = self._new_dacl(directory=directory)
        owner_buffer = ctypes.create_string_buffer(self._sids[0])
        owner = ctypes.cast(owner_buffer, wintypes.LPVOID)
        path_buffer = ctypes.create_unicode_buffer(path)
        try:
            status = self._advapi32.SetNamedSecurityInfoW(
                path_buffer,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION
                | _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                owner,
                None,
                dacl,
                None,
            )
            if status == 5:  # ERROR_ACCESS_DENIED：卷继承 ACL 只给 Modify（无 WRITE_OWNER）
                # 时无法显式设置 owner。创建者本就是 owner，回退为仅写 DACL；
                # 随后 path_is_secure 仍会独立校验 owner SID，保证同等安全语义。
                status = self._advapi32.SetNamedSecurityInfoW(
                    path_buffer,
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )
            _ = keepalive
        finally:
            self._kernel32.LocalFree(dacl)
        if status != 0:
            raise OSError(status, f"无法保护 Windows Gateway 密钥对象: {path}")

    def path_is_secure(self, path: str, *, directory: bool) -> bool:
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        path_buffer = ctypes.create_unicode_buffer(path)
        status = self._advapi32.GetNamedSecurityInfoW(
            path_buffer,
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
            raise OSError(status, f"无法读取 Windows Gateway 密钥 DACL: {path}")
        try:
            return self._security_is_expected(
                owner,
                dacl,
                descriptor,
                directory=directory,
            )
        finally:
            self._kernel32.LocalFree(descriptor)

    def handle_is_secure(self, handle: wintypes.HANDLE, *, directory: bool) -> bool:
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
            raise OSError(status, "无法读取 Windows Gateway 密钥 handle DACL")
        try:
            return self._security_is_expected(
                owner,
                dacl,
                descriptor,
                directory=directory,
            )
        finally:
            self._kernel32.LocalFree(descriptor)

    def _security_is_expected(
        self,
        owner: wintypes.LPVOID,
        dacl: wintypes.LPVOID,
        descriptor: wintypes.LPVOID,
        *,
        directory: bool,
    ) -> bool:
        sid_buffers, expected = self._sid_pointers()
        _ = sid_buffers
        if not owner or not self._advapi32.EqualSid(owner, expected[0]):
            return False
        if not dacl or not descriptor:
            return False

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not self._advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            self._raise_last_error("无法读取 Windows Gateway 密钥 DACL 控制标志")
        if not control.value & _SE_DACL_PROTECTED:
            return False

        info = _AclSizeInformation()
        if not self._advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            _ACL_SIZE_INFORMATION,
        ):
            self._raise_last_error("无法读取 Windows Gateway 密钥 DACL")
        if info.AceCount != len(expected):
            return False

        expected_flags = (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        )
        seen = [False] * len(expected)
        for index in range(info.AceCount):
            ace = wintypes.LPVOID()
            if not self._advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                self._raise_last_error("无法读取 Windows Gateway 密钥 DACL ACE")
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            if (
                header.AceType != _ACCESS_ALLOWED_ACE_TYPE
                or header.AceSize < 12
                or header.AceFlags != expected_flags
            ):
                return False
            address = int(ace.value)
            mask = ctypes.cast(
                address + ctypes.sizeof(_AceHeader),
                ctypes.POINTER(wintypes.DWORD),
            ).contents.value
            if mask != _FILE_ALL_ACCESS:
                return False
            ace_sid = wintypes.LPVOID(address + ctypes.sizeof(_AceHeader) + 4)
            match = next(
                (
                    position
                    for position, expected_sid in enumerate(expected)
                    if self._advapi32.EqualSid(ace_sid, expected_sid)
                ),
                None,
            )
            if match is None or seen[match]:
                return False
            seen[match] = True
        return all(seen)


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.fspath(path))


def _path_has_expected_kind(path: str, *, directory: bool) -> bool:
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return False
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    attributes = int(getattr(info, "st_file_attributes", 0))
    return expected and not attributes & _FILE_ATTRIBUTE_REPARSE_POINT


def _windows_handle(fd: int) -> wintypes.HANDLE:
    if os.name != "nt":
        raise OSError(errno.ENOTSUP, "Windows ACL API 仅能在 Windows 上运行")
    import msvcrt

    raw = msvcrt.get_osfhandle(fd)
    if raw == -1:
        raise OSError(errno.EBADF, "Gateway 密钥文件描述符无效")
    return wintypes.HANDLE(raw)


@lru_cache(maxsize=1)
def _api() -> _WindowsAclApi:
    return _WindowsAclApi()


def protect_path(path: str | os.PathLike[str], *, directory: bool) -> None:
    """Apply and verify the protected host-only DACL on a path."""

    normalized = _normalized_path(path)
    if not _path_has_expected_kind(normalized, directory=directory):
        raise OSError(errno.EINVAL, "Windows Gateway 密钥对象类型无效或是 reparse point")
    _api().protect_path(normalized, directory=directory)
    if (
        not _path_has_expected_kind(normalized, directory=directory)
        or not _api().path_is_secure(normalized, directory=directory)
    ):
        raise OSError(errno.EACCES, "Windows Gateway 密钥 DACL 验证失败")


def path_is_secure(path: str | os.PathLike[str], *, directory: bool) -> bool:
    """Return whether owner and DACL exactly match the host-only policy."""

    normalized = _normalized_path(path)
    if not _path_has_expected_kind(normalized, directory=directory):
        return False
    try:
        return _api().path_is_secure(normalized, directory=directory)
    except (OSError, ValueError):
        return False


def fd_is_secure(fd: int, *, directory: bool = False) -> bool:
    """Return whether an open descriptor has the expected owner and DACL."""

    try:
        return _api().handle_is_secure(_windows_handle(fd), directory=directory)
    except (OSError, ValueError):
        return False


__all__ = ["fd_is_secure", "path_is_secure", "protect_path"]
