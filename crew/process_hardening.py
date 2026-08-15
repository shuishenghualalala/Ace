"""Early, dependency-free hardening for long-lived product processes.

This module intentionally imports only the Python standard library.  Production
entrypoints call :func:`harden_main_process` before importing the CLI, Gateway,
or native-runtime bridge implementation.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Protocol

_DANGEROUS_ENV_NAMES = frozenset(
    {
        # Dynamic-loader injection and diagnostics.
        "GCONV_PATH",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH_64",
        "LD_PRELOAD",
        "LOCPATH",
        # Python startup/import injection.
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "PSMODULEPATH",
        # Node/Electron startup injection.
        "ELECTRON_NO_ASAR",
        "ELECTRON_RUN_AS_NODE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_REPL_EXTERNAL_MODULE",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        # Shell/language startup hooks inherited by descendants.
        "BASH_ENV",
        "ENV",
        "PERL5OPT",
        "RUBYOPT",
        "_JAVA_OPTIONS",
    }
)
_DANGEROUS_ENV_PREFIXES = (
    "COMPLUS_",
    "CORECLR_",
    "COR_",
    "DOTNET_",
    "DYLD_",
    "LD_",
)
# no_new_privs is irreversible and inherited. The CLI/Gateway intentionally
# support explicitly approved elevated descendants (for example Linux CUA
# dependency setup), so only leaf roles that never provide that feature opt in.
_NO_NEW_PRIVS_SAFE_ROLES = frozenset(
    {
        "cli-weixin-login",
        "managed-background-bridge",
    }
)


def _is_injection_environment_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _DANGEROUS_ENV_NAMES or normalized.startswith(
        _DANGEROUS_ENV_PREFIXES
    )


def sanitize_injection_environment(environ: MutableMapping[str, str]) -> tuple[str, ...]:
    """Remove ambient loader/runtime hooks in place and return names only.

    Matching is case-insensitive so the same policy applies to Windows'
    case-insensitive process environment.  Values are deliberately never returned
    or logged because they can contain secrets or attacker-controlled paths.
    """

    removed: list[str] = []
    for name in tuple(environ):
        if not _is_injection_environment_name(name):
            continue
        try:
            del environ[name]
        except KeyError:
            continue
        if name in environ:
            raise OSError(f"failed to remove unsafe environment variable {name}")
        removed.append(name)
    return tuple(removed)


class ProcessHardeningError(RuntimeError):
    """A required process hardening control could not be established."""


@dataclass(frozen=True)
class HardeningReport:
    role: str
    platform: str
    applied: tuple[str, ...]
    warnings: tuple[str, ...]
    removed_environment: tuple[str, ...]


class HardeningOperations(Protocol):
    def disable_core_dumps(self) -> None: ...

    def disable_linux_dumpability(self) -> None: ...

    def enable_linux_no_new_privs(self) -> None: ...

    def deny_macos_debug_attach(self) -> None: ...

    def secure_windows_dll_search(self) -> None: ...

    def configure_windows_error_mode(self) -> None: ...

    def disable_windows_standard_handle_inheritance(self) -> None: ...


class NativeHardeningOperations:
    """Small native calls used by :class:`ProcessHardener`."""

    _PR_GET_DUMPABLE = 3
    _PR_SET_DUMPABLE = 4
    _PR_SET_NO_NEW_PRIVS = 38
    _PR_GET_NO_NEW_PRIVS = 39
    _PT_DENY_ATTACH = 31

    _LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000
    _SEM_FAILCRITICALERRORS = 0x0001
    _SEM_NOGPFAULTERRORBOX = 0x0002
    _SEM_NOOPENFILEERRORBOX = 0x8000

    def __init__(self) -> None:
        self._windows_dll_directory_cookies: list[int] = []

    def disable_core_dumps(self) -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise OSError("RLIMIT_CORE verification failed")

    @staticmethod
    def _linux_prctl(option: int, value: int) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        ctypes.set_errno(0)
        if prctl(option, value, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    @staticmethod
    def _linux_prctl_get(option: int) -> int:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = prctl(option, 0, 0, 0, 0)
        if result < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return result

    def disable_linux_dumpability(self) -> None:
        self._linux_prctl(self._PR_SET_DUMPABLE, 0)
        if self._linux_prctl_get(self._PR_GET_DUMPABLE) != 0:
            raise OSError("PR_SET_DUMPABLE verification failed")

    def enable_linux_no_new_privs(self) -> None:
        self._linux_prctl(self._PR_SET_NO_NEW_PRIVS, 1)
        if self._linux_prctl_get(self._PR_GET_NO_NEW_PRIVS) != 1:
            raise OSError("PR_SET_NO_NEW_PRIVS verification failed")

    def deny_macos_debug_attach(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        ptrace = libc.ptrace
        ptrace.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        ptrace.restype = ctypes.c_int
        ctypes.set_errno(0)
        if ptrace(self._PT_DENY_ATTACH, 0, None, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    @staticmethod
    def _kernel32():
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Win32 API unavailable")
        return ctypes.WinDLL("kernel32", use_last_error=True)

    @staticmethod
    def _windows_error(message: str) -> OSError:
        error_number = ctypes.get_last_error()
        if error_number:
            return ctypes.WinError(error_number)
        return OSError(message)

    def secure_windows_dll_search(self) -> None:
        from ctypes import wintypes

        kernel32 = self._kernel32()
        try:
            add_dll_directory = kernel32.AddDllDirectory
        except AttributeError as exc:
            raise OSError("AddDllDirectory unavailable") from exc
        add_dll_directory.argtypes = [wintypes.LPCWSTR]
        add_dll_directory.restype = ctypes.c_void_p

        executable_directory = os.path.dirname(os.path.realpath(sys.executable))
        trusted_directories = {
            os.path.realpath(os.path.join(sys.prefix, "DLLs")),
            os.path.realpath(os.path.join(sys.prefix, "Library", "bin")),
        }
        bundle_directory = getattr(sys, "_MEIPASS", "")
        if bundle_directory:
            resolved_bundle = os.path.realpath(str(bundle_directory))
            try:
                inside_executable_directory = (
                    os.path.commonpath([resolved_bundle, executable_directory])
                    == executable_directory
                )
            except ValueError:
                inside_executable_directory = False
            if not inside_executable_directory:
                raise OSError("PyInstaller DLL directory is outside the application directory")
            trusted_directories.add(resolved_bundle)
        for directory in sorted(trusted_directories):
            if not os.path.isdir(directory):
                continue
            cookie = add_dll_directory(directory)
            if not cookie:
                raise self._windows_error(f"AddDllDirectory failed for {directory}")
            self._windows_dll_directory_cookies.append(int(cookie))

        try:
            set_default_dll_directories = kernel32.SetDefaultDllDirectories
        except AttributeError as exc:
            raise OSError("SetDefaultDllDirectories unavailable") from exc
        set_default_dll_directories.argtypes = [wintypes.DWORD]
        set_default_dll_directories.restype = wintypes.BOOL
        if not set_default_dll_directories(self._LOAD_LIBRARY_SEARCH_DEFAULT_DIRS):
            raise self._windows_error("SetDefaultDllDirectories failed")

        set_dll_directory = kernel32.SetDllDirectoryW
        set_dll_directory.argtypes = [wintypes.LPCWSTR]
        set_dll_directory.restype = wintypes.BOOL
        if not set_dll_directory(""):
            raise self._windows_error("SetDllDirectoryW failed")

    def configure_windows_error_mode(self) -> None:
        from ctypes import wintypes

        flags = (
            self._SEM_FAILCRITICALERRORS
            | self._SEM_NOGPFAULTERRORBOX
            | self._SEM_NOOPENFILEERRORBOX
        )
        kernel32 = self._kernel32()
        set_error_mode = kernel32.SetErrorMode
        set_error_mode.argtypes = [wintypes.UINT]
        set_error_mode.restype = wintypes.UINT
        set_error_mode(flags)
        try:
            get_error_mode = kernel32.GetErrorMode
        except AttributeError as exc:
            raise OSError("GetErrorMode unavailable") from exc
        get_error_mode.argtypes = []
        get_error_mode.restype = wintypes.UINT
        if get_error_mode() & flags != flags:
            raise OSError("SetErrorMode verification failed")

        try:
            set_thread_error_mode = kernel32.SetThreadErrorMode
        except AttributeError as exc:
            raise OSError("SetThreadErrorMode unavailable") from exc
        previous = wintypes.DWORD()
        set_thread_error_mode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        set_thread_error_mode.restype = wintypes.BOOL
        if not set_thread_error_mode(flags, ctypes.byref(previous)):
            raise self._windows_error("SetThreadErrorMode failed")
        try:
            get_thread_error_mode = kernel32.GetThreadErrorMode
        except AttributeError as exc:
            raise OSError("GetThreadErrorMode unavailable") from exc
        get_thread_error_mode.argtypes = []
        get_thread_error_mode.restype = wintypes.DWORD
        if get_thread_error_mode() & flags != flags:
            raise OSError("SetThreadErrorMode verification failed")

    def disable_windows_standard_handle_inheritance(self) -> None:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream is None:
                continue
            try:
                descriptor = stream.fileno()
            except (AttributeError, OSError, ValueError):
                continue
            if descriptor < 0:
                continue
            try:
                import msvcrt

                handle = msvcrt.get_osfhandle(descriptor)
            except (ImportError, OSError):
                continue
            if handle == -1:
                continue
            os.set_handle_inheritable(handle, False)
            if os.get_handle_inheritable(handle):
                raise OSError(f"standard handle {descriptor} remains inheritable")


def _stderr_warning(message: str) -> None:
    try:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        # Failure policy is carried by the exception/report; a detached GUI stderr
        # must not turn a successful native control into a startup failure.
        return


class ProcessHardener:
    """Apply one platform policy once while re-sanitizing env on every call."""

    def __init__(
        self,
        *,
        environ: MutableMapping[str, str],
        platform_name: str,
        os_name: str,
        operations: HardeningOperations,
        warning_sink: Callable[[str], None] = _stderr_warning,
    ) -> None:
        self._environ = environ
        self._platform = platform_name
        self._os_name = os_name
        self._operations = operations
        self._warning_sink = warning_sink
        self._lock = threading.Lock()
        self._completed = False
        self._applied: tuple[str, ...] = ()
        self._warnings: list[str] = []
        self._role = ""

    def _required(self, role: str, label: str, operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception as exc:
            message = (
                "[process-hardening] FAIL-CLOSED "
                f"role={role} platform={self._platform} control={label} "
                f"error={type(exc).__name__}: {exc}"
            )
            self._warning_sink(message)
            raise ProcessHardeningError(
                f"required process hardening failed: {label}"
            ) from exc

    def apply(self, role: str) -> HardeningReport:
        normalized_role = role.strip()
        if not normalized_role:
            raise ValueError("process hardening role must not be empty")

        with self._lock:
            try:
                removed = sanitize_injection_environment(self._environ)
            except Exception as exc:
                self._warning_sink(
                    "[process-hardening] FAIL-CLOSED "
                    f"role={normalized_role} platform={self._platform} "
                    f"control=environment-sanitization error={type(exc).__name__}"
                )
                raise ProcessHardeningError(
                    "required process hardening failed: environment sanitization"
                ) from exc
            if removed:
                self._warning_sink(
                    "[process-hardening] removed ambient startup hooks "
                    f"role={normalized_role} names={','.join(sorted(removed, key=str.upper))}"
                )

            if not self._completed:
                applied: list[str] = []
                if self._platform.startswith("linux") and self._os_name == "posix":
                    self._required(
                        normalized_role,
                        "rlimit_core=0",
                        self._operations.disable_core_dumps,
                    )
                    applied.append("rlimit_core=0")
                    self._required(
                        normalized_role,
                        "pr_set_dumpable=0",
                        self._operations.disable_linux_dumpability,
                    )
                    applied.append("pr_set_dumpable=0")
                    if normalized_role in _NO_NEW_PRIVS_SAFE_ROLES:
                        self._required(
                            normalized_role,
                            "pr_set_no_new_privs=1",
                            self._operations.enable_linux_no_new_privs,
                        )
                        applied.append("pr_set_no_new_privs=1")
                    else:
                        message = (
                            "[process-hardening] SKIPPED "
                            f"role={normalized_role} platform={self._platform} "
                            "control=PR_SET_NO_NEW_PRIVS "
                            "reason=role supports approved elevated descendants"
                        )
                        self._warnings.append(message)
                        self._warning_sink(message)
                elif self._platform == "darwin" and self._os_name == "posix":
                    self._required(
                        normalized_role,
                        "rlimit_core=0",
                        self._operations.disable_core_dumps,
                    )
                    applied.append("rlimit_core=0")
                    self._required(
                        normalized_role,
                        "PT_DENY_ATTACH",
                        self._operations.deny_macos_debug_attach,
                    )
                    applied.append("pt_deny_attach")
                elif self._platform == "win32" and self._os_name == "nt":
                    self._required(
                        normalized_role,
                        "secure_dll_search",
                        self._operations.secure_windows_dll_search,
                    )
                    applied.append("secure_dll_search")
                    self._required(
                        normalized_role,
                        "noninteractive_error_mode",
                        self._operations.configure_windows_error_mode,
                    )
                    applied.append("noninteractive_error_mode")
                    self._required(
                        normalized_role,
                        "noninheritable_standard_handles",
                        self._operations.disable_windows_standard_handle_inheritance,
                    )
                    applied.append("noninheritable_standard_handles")
                else:
                    message = (
                        "[process-hardening] FAIL-CLOSED "
                        f"role={normalized_role} platform={self._platform} "
                        "control=platform-support error=unsupported platform"
                    )
                    self._warning_sink(message)
                    raise ProcessHardeningError(
                        f"unsupported platform for process hardening: {self._platform}"
                    )
                self._role = normalized_role
                self._applied = tuple(applied)
                self._completed = True

            return HardeningReport(
                role=self._role,
                platform=self._platform,
                applied=self._applied,
                warnings=tuple(self._warnings),
                removed_environment=removed,
            )


_CURRENT_PROCESS_HARDENER = ProcessHardener(
    environ=os.environ,
    platform_name=sys.platform,
    os_name=os.name,
    operations=NativeHardeningOperations(),
)


def harden_main_process(role: str) -> HardeningReport:
    """Harden this product process before importing its application stack."""

    return _CURRENT_PROCESS_HARDENER.apply(role)
