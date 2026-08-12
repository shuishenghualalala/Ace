"""Single fail-closed application boundary for managed process execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import shlex
import shutil
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    FilesystemAccess,
    NetworkAccess,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeClient, RuntimeCommandResult


def _existing_directory(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _declared_python_root(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        module_path = Path(f"{path}.py")
        try:
            resolved = module_path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
    if resolved.is_dir():
        return resolved
    return resolved.parent if resolved.is_file() else None


def _editable_python_package_roots(environment_root: Path) -> tuple[Path, ...]:
    """Read package roots from static Python editable-install metadata."""
    site_packages: list[Path] = []
    for library_parent in (environment_root / "lib", environment_root / "Lib"):
        try:
            candidates = tuple(library_parent.iterdir())
        except OSError:
            candidates = ()
        for candidate in candidates:
            if not candidate.is_dir() or not candidate.name.lower().startswith("python"):
                continue
            for name in ("site-packages", "dist-packages"):
                package_root = _existing_directory(candidate / name)
                if package_root is not None:
                    site_packages.append(package_root)

    roots: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = _declared_python_root(path)
        if resolved is not None and resolved not in seen:
            roots.append(resolved)
            seen.add(resolved)

    for package_root in site_packages:
        for metadata_path in package_root.glob("*.pth"):
            try:
                lines = metadata_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                value = line.strip()
                if not value or value.startswith("import "):
                    continue
                candidate = Path(value)
                add(candidate if candidate.is_absolute() else package_root / candidate)
        for finder_path in package_root.glob("__editable__*.py"):
            try:
                tree = ast.parse(finder_path.read_text(encoding="utf-8"), filename=str(finder_path))
            except (OSError, SyntaxError):
                continue
            # Editable-install finders contain MAPPING/NAMESPACES string paths.
            # AST inspection avoids importing or executing third-party metadata.
            for statement in ast.walk(tree):
                if isinstance(statement, ast.Assign):
                    target_names = {
                        target.id
                        for target in statement.targets
                        if isinstance(target, ast.Name)
                    }
                elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    target_names = {statement.target.id}
                else:
                    continue
                if not target_names.intersection({"MAPPING", "NAMESPACES"}):
                    continue
                try:
                    literal = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                values = [literal]
                while values:
                    value = values.pop()
                    if isinstance(value, str):
                        value_path = Path(value)
                        if value_path.is_absolute():
                            add(value_path)
                    elif isinstance(value, dict):
                        values.extend(value.values())
                    elif isinstance(value, (list, tuple, set)):
                        values.extend(value)
    return tuple(roots)


def _venv_runtime_readable_roots(
    command: Sequence[str],
    *,
    writable_roots: Sequence[Path],
) -> tuple[Path, ...]:
    """Derive minimum read-only roots needed by a host-installed script entrypoint.

    Managed execution intentionally does not expose the user's home directory. A
    Python venv entrypoint is the one common case where the executable can start
    successfully but its interpreter still needs files outside the task root.
    Read only the shebang and ``pyvenv.cfg``; never execute or trust model-provided
    metadata. A script inside a writable workspace cannot use this helper to turn
    its shebang into a read grant.
    """
    if not command:
        return ()
    try:
        entrypoint = Path(str(command[0])).expanduser().resolve(strict=True)
        if not entrypoint.is_file() or any(
            entrypoint == root or root in entrypoint.parents for root in writable_roots
        ):
            return ()
        with entrypoint.open("rb") as stream:
            first_line = stream.readline(4096)
    except (OSError, RuntimeError):
        return ()
    if not first_line.startswith(b"#!"):
        return ()
    try:
        shebang = shlex.split(first_line[2:].decode("utf-8", errors="replace"))
    except ValueError:
        return ()
    if not shebang:
        return ()

    interpreter_text = shebang[0]
    if Path(interpreter_text).name.lower().removesuffix(".exe") == "env":
        candidates = [
            part for part in shebang[1:]
            if not part.startswith("-") and "=" not in part
        ]
        if not candidates:
            return ()
        interpreter_text = shutil.which(candidates[0]) or ""
    if not interpreter_text or not Path(interpreter_text).expanduser().is_absolute():
        return ()

    try:
        interpreter = Path(interpreter_text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return ()

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in (Path(interpreter_text).expanduser(), interpreter):
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        environment_candidates: list[Path] = []
        for interpreter_path in (candidate, resolved_candidate):
            if interpreter_path.parent.name.lower() in {"bin", "scripts"}:
                environment_candidates.append(interpreter_path.parent.parent)
        for environment_root in environment_candidates:
            config_path = environment_root / "pyvenv.cfg"
            if not config_path.is_file():
                continue
            if environment_root not in seen:
                roots.append(environment_root)
                seen.add(environment_root)
            for package_root in _editable_python_package_roots(environment_root):
                if package_root not in seen:
                    roots.append(package_root)
                    seen.add(package_root)
            try:
                config_lines = config_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            home_value = next(
                (
                    value.strip()
                    for line in config_lines
                    if line.partition("=")[0].strip().lower() == "home"
                    for value in (line.partition("=")[2],)
                    if value.strip()
                ),
                "",
            )
            if not home_value:
                continue
            base_home = Path(home_value).expanduser()
            if base_home.name.lower() in {"bin", "scripts"}:
                base_home = base_home.parent
            try:
                base_home = base_home.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            library_parent = base_home / "lib"
            base_library_root = _existing_directory(library_parent)
            if base_library_root is None:
                base_library_root = _existing_directory(base_home / "Lib")
            if base_library_root is not None and base_library_root not in seen:
                roots.append(base_library_root)
                seen.add(base_library_root)
            try:
                library_roots = tuple(
                    child.resolve(strict=True)
                    for child in library_parent.iterdir()
                    if child.is_dir() and child.name.lower().startswith("python")
                )
            except OSError:
                library_roots = ()
            if not library_roots:
                windows_library = base_home / "Lib"
                library_roots = (
                    (windows_library.resolve(strict=True),)
                    if windows_library.is_dir()
                    else ()
                )
            for library_root in library_roots:
                if library_root not in seen:
                    roots.append(library_root)
                    seen.add(library_root)
    return tuple(roots)


@dataclass(frozen=True)
class ExecutionRequest:
    """Normalized command and permissions supplied to the native runtime."""

    command: tuple[str, ...]
    cwd: Path
    permission_profile: PermissionProfile
    additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS
    trusted_readable_roots: tuple[Path, ...] = ()
    stdin: bytes | None = None
    home_files: Mapping[str, bytes] | None = None
    env_overrides: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0
    max_output_bytes: int = 2 * 1024 * 1024


class SecurityExecutionBroker:
    """Translate a managed permission profile into one native runtime call."""

    def __init__(self, runtime: NativeRuntimeClient) -> None:
        self._runtime = runtime

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        on_started: Callable[[int | None], None] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
    ) -> RuntimeCommandResult:
        """Run a managed request; disabled profiles are deliberately unsupported here."""
        kwargs = self._runtime_kwargs(request)
        return await self._runtime.execute(
            **kwargs,
            on_started=on_started,
            on_output=on_output,
        )

    async def open_interactive(self, request: ExecutionRequest):
        """Open a managed bidirectional child through the native runtime."""
        kwargs = self._runtime_kwargs(request)
        kwargs.pop("stdin", None)
        return await self._runtime.open_interactive(**kwargs)

    @staticmethod
    def _runtime_kwargs(request: ExecutionRequest) -> dict:
        if request.permission_profile.kind is not PermissionProfileKind.MANAGED:
            raise ValueError("host execution is outside the managed security broker")
        writable, readable, readonly, denied = compile_runtime_filesystem_roots(
            request.permission_profile,
            request.additional_permissions,
            request.trusted_readable_roots,
        )
        for root in _venv_runtime_readable_roots(request.command, writable_roots=writable):
            if root not in readable:
                readable.append(root)
        # A trusted runtime root already below a writable root is visible through
        # that bind. Forwarding both would make the Linux plan reject an overlap.
        readable = [
            root
            for root in readable
            if not any(root == write or write in root.parents for write in writable)
        ]
        network_entries = (
            *request.permission_profile.network_entries,
            *request.additional_permissions.network,
        )
        network_rules = [
            {
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
                "allow": entry.access is NetworkAccess.ALLOW,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
            }
            for entry in network_entries
        ]
        return {
            "command": request.command,
            "cwd": request.cwd,
            "writable_roots": writable,
            "readable_roots": readable,
            "readonly_roots": readonly,
            "denied_roots": denied,
            "full_disk_read": request.permission_profile.full_disk_read,
            "network_enabled": bool(network_rules),
            "network_rules": network_rules,
            "allow_local_binding": (
                request.permission_profile.allow_local_binding
                or request.additional_permissions.allow_local_binding
            ),
            "timeout": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
            "stdin": request.stdin,
            "home_files": request.home_files,
            "env_overrides": request.env_overrides,
        }


def compile_runtime_filesystem_roots(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
    trusted_readable_roots: Sequence[Path] = (),
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Compile one consistent native filesystem plan for foreground/background runs.

    A task workspace can intentionally be a more-specific child of the protected
    runtime home. Native backends cannot enforce both a writable child and a deny
    mount/ACE on its ancestor: the ancestor deny wins and makes a valid approval
    unusable. Drop only those strict ancestor denies. The native default-deny
    boundary still hides every sibling, while exact and descendant denies remain.
    """
    writable: list[Path] = []
    readable = list(dict.fromkeys(trusted_readable_roots))
    readonly: list[Path] = []
    denied: list[Path] = []
    for entry in (*profile.filesystem, *additional.filesystem):
        if entry.access is FilesystemAccess.READ_WRITE:
            if entry.root not in writable:
                writable.append(entry.root)
        elif entry.access is FilesystemAccess.READ:
            # Immutable entries below writable roots use the native read-only
            # carve-out contract. Missing metadata paths remain valid so the
            # runtime can prevent their later creation.
            target = readonly if not entry.escalatable else readable
            if entry.root not in target:
                target.append(entry.root)
        elif entry.access is FilesystemAccess.DENY and entry.root not in denied:
            denied.append(entry.root)

    # A trusted/runtime root below a writable root is already visible through the
    # write bind. Forwarding both makes Linux reject an overlapping mount plan.
    readable = [
        root
        for root in readable
        if not any(root == write or write in root.parents for write in writable)
    ]
    allowed_roots = (*writable, *readable)
    denied = [
        root
        for root in denied
        if not any(root != allowed and root in allowed.parents for allowed in allowed_roots)
    ]
    return writable, readable, readonly, denied


def packaged_runtime_argv(executable: str | Path) -> Sequence[str]:
    """Return an explicit argv without PATH or shell resolution."""
    return (str(Path(executable).expanduser().resolve(strict=False)),)
