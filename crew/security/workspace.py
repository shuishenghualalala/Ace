"""Host access to files created in Ace-owned workspaces."""

from __future__ import annotations

import os
from pathlib import Path


def prepare_new_workspace(path: Path) -> None:
    """Let the Windows host user access files the sandbox account creates.

    Call only for a newly created Ace-owned directory. Windows can inherit an
    OWNER RIGHTS ACE that applies to each file's owner, so it does not preserve
    host access when the sandbox account owns the new file.
    """
    if os.name == "nt":
        _grant_windows_host_inheritance(path)


def _grant_windows_host_inheritance(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class Trustee(ctypes.Structure):
        _fields_ = [
            ("multiple_trustee", ctypes.c_void_p),
            ("multiple_operation", ctypes.c_int),
            ("form", ctypes.c_int),
            ("type", ctypes.c_int),
            ("name", ctypes.c_void_p),
        ]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [
            ("permissions", wintypes.DWORD),
            ("mode", ctypes.c_int),
            ("inheritance", wintypes.DWORD),
            ("trustee", Trustee),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    security.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    security.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    security.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    security.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    )
    security.SetEntriesInAclW.argtypes = (
        wintypes.ULONG, ctypes.POINTER(ExplicitAccess), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(token)

    # TOKEN_USER begins with its SID pointer. Preserve the existing DACL and
    # grant the actual host SID, with inheritance, to this Ace-owned directory.
    sid = ctypes.c_void_p.from_buffer(user).value
    # FILE_ALL_ACCESS, GRANT_ACCESS, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,
    # TRUSTEE_IS_SID.
    entry = ExplicitAccess(0x1F01FF, 1, 3, Trustee(None, 0, 0, 0, sid))
    old_dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    updated = ctypes.c_void_p()
    path_text = str(path.resolve(strict=True))
    file_object = 1
    dacl_information = 4
    result = security.GetNamedSecurityInfoW(
        path_text, file_object, dacl_information, None, None, ctypes.byref(old_dacl),
        None, ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        result = security.SetEntriesInAclW(
            1, ctypes.byref(entry), old_dacl, ctypes.byref(updated)
        )
        if result:
            raise ctypes.WinError(result)
        result = security.SetNamedSecurityInfoW(
            ctypes.create_unicode_buffer(path_text), file_object, dacl_information,
            None, None, updated, None,
        )
        if result:
            raise ctypes.WinError(result)
    finally:
        if updated:
            kernel.LocalFree(updated)
        kernel.LocalFree(descriptor)
