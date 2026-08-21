"""Structured file access classification before any host file is opened."""

from __future__ import annotations

import stat
import sys
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from crew.security.actions import NormalizedAction
from crew.security.context import SecurityContext
from crew.security.local_path import LocalPathReference, LocalPathReferenceError
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemGlobEntry,
    FilesystemOperation,
)
from crew.security.policy import filesystem_operation_allowed, settings_for_mode
from crew.state.home import get_crew_home, get_owner_runtime_home


class FilePolicyResult(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class FilePolicyAssessment:
    result: FilePolicyResult
    reason: str


def assess_file_action(
    context: SecurityContext,
    action: NormalizedAction,
    mode: ConversationPermissionMode,
    *,
    db_path: str | Path,
    additional: AdditionalPermissionProfile = AdditionalPermissionProfile(),
) -> FilePolicyAssessment:
    """Classify one canonical path with immutable protected entries."""
    try:
        target = _resolve_policy_target(action.path)
        _validate_policy_target(target, operation=action.operation)
    except (LocalPathReferenceError, OSError, RuntimeError, ValueError):
        return FilePolicyAssessment(FilePolicyResult.DENY, "文件目标无法安全验证")
    operation = (
        FilesystemOperation.READ if action.operation == "read" else FilesystemOperation.WRITE
    )
    if operation is FilesystemOperation.WRITE and _is_filesystem_root(target):
        return FilePolicyAssessment(FilePolicyResult.DENY, "永久拒绝写入文件系统根")
    if context.workspace_root is not None and _is_sensitive_workspace_path(
        target, context.workspace_root
    ):
        return FilePolicyAssessment(
            FilePolicyResult.DENY,
            "目标属于不可升级的环境或凭据文件",
        )

    try:
        protected = _protected_entries(context, db_path)
    except (OSError, RuntimeError, ValueError):
        return FilePolicyAssessment(FilePolicyResult.DENY, "受保护路径清单不可用")
    matching = [entry for entry in protected if _contains(entry.root, target)]
    if any(entry.access is FilesystemAccess.DENY and not entry.escalatable for entry in matching):
        return FilePolicyAssessment(FilePolicyResult.DENY, "目标属于不可升级的运行时或凭据路径")
    if operation is FilesystemOperation.WRITE and any(
        entry.access is FilesystemAccess.READ and not entry.escalatable for entry in matching
    ):
        return FilePolicyAssessment(FilePolicyResult.DENY, "受保护项目元数据只读")

    settings = settings_for_mode(
        mode,
        context.workspace_root,
        deny_entries=protected,
        deny_globs=_protected_globs(context),
    )
    if filesystem_operation_allowed(
        settings.profile,
        additional,
        target,
        operation,
    ):
        return FilePolicyAssessment(FilePolicyResult.ALLOW, "base_profile")
    return FilePolicyAssessment(FilePolicyResult.REQUIRE_APPROVAL, "项目外路径需要额外授权")


def approvable_file_permission_root(
    context: SecurityContext,
    target: str | Path,
    *,
    db_path: str | Path,
) -> Path:
    """Return the native permission boundary that must be shown to the user.

    Protected project metadata is enforced as a directory-level read-only
    carve-out on every native backend.  A write to one child therefore needs an
    explicit grant for that displayed directory; silently approving a child and
    opening the parent would make the UI scope dishonest.
    """
    resolved = Path(target).expanduser().resolve(strict=False)
    candidates = [
        entry.root
        for entry in _protected_entries(context, db_path)
        if entry.access is FilesystemAccess.READ
        and entry.escalatable
        and _contains(entry.root, resolved)
    ]
    if not candidates:
        return resolved
    return max(candidates, key=lambda root: len(root.parts))


def _protected_entries(
    context: SecurityContext, db_path: str | Path
) -> tuple[FilesystemEntry, ...]:
    denied = [
        FilesystemEntry(Path(db_path), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-wal"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-shm"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-journal"), FilesystemAccess.DENY, escalatable=False),
    ]
    # CREW_HOME also contains the explicitly writable owner task workspaces.
    # Denying the whole parent makes macOS Seatbelt reject getcwd() even when
    # the task root itself is present in writable_roots. Protect only the
    # host-owned stores and credentials that must never be exposed to a child.
    crew_home = get_crew_home().expanduser().resolve(strict=False)
    protected_home_paths = (
        crew_home / ".auth",
        crew_home / ".env",
        crew_home / ".gateway-instance",
        crew_home / "config.yaml",
        crew_home / "crew_data",
        crew_home / "logs",
    )
    denied.extend(
        FilesystemEntry(path, FilesystemAccess.DENY, escalatable=False)
        for path in protected_home_paths
    )
    try:
        owner_home = get_owner_runtime_home(context.owner_account_id, create=False)
    except (OSError, RuntimeError, ValueError):
        owner_home = None
    if owner_home is not None:
        denied.extend(
            FilesystemEntry(owner_home / name, FilesystemAccess.DENY, escalatable=False)
            for name in (".env", "config.yaml")
        )
    if context.workspace_root is not None:
        workspace_root = context.workspace_root.resolve(strict=False)
        denied.extend(
            FilesystemEntry(
                workspace_root / name,
                FilesystemAccess.READ,
                escalatable=True,
            )
            for name in (".git", ".agents", ".crew")
        )
        denied.extend(
            FilesystemEntry(
                workspace_root / name,
                FilesystemAccess.DENY,
                escalatable=False,
            )
            for name in (*_SENSITIVE_DIRECTORY_NAMES, *_SENSITIVE_FILE_NAMES)
        )
        try:
            dynamic_sensitive = []
            for index, child in enumerate(workspace_root.iterdir()):
                if index >= 256:
                    break
                if _is_sensitive_workspace_path(child, workspace_root):
                    dynamic_sensitive.append(child)
        except OSError as exc:
            raise RuntimeError("sensitive workspace path discovery failed") from exc
        denied.extend(
            FilesystemEntry(path, FilesystemAccess.DENY, escalatable=False)
            for path in dynamic_sensitive
        )
        if not sys.platform.startswith("linux"):
            # Windows/macOS do not have the Linux deny-read glob contract.  Bind
            # the exact discovered paths into the same immutable protected set.
            denied.extend(_discovered_sensitive_entries(context))
    return tuple(denied)


_SENSITIVE_DIRECTORY_NAMES = frozenset({".ssh", ".aws", ".azure", ".gnupg"})
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SENSITIVE_FILE_SUFFIXES = (".key", ".p12", ".pfx", ".pem")
_SENSITIVE_READ_GLOBS = (
    "**/.env",
    "**/.env.*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.azure/**",
    "**/.gnupg/**",
    "**/*credentials*.json",
    "**/application_default_credentials.json",
    "**/id_dsa",
    "**/id_ecdsa",
    "**/id_ed25519",
    "**/id_rsa",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.pem",
)


def _protected_globs(context: SecurityContext) -> tuple[FilesystemGlobEntry, ...]:
    if context.workspace_root is None or not sys.platform.startswith("linux"):
        return ()
    return tuple(
        FilesystemGlobEntry(context.workspace_root, pattern) for pattern in _SENSITIVE_READ_GLOBS
    )


def _discovered_sensitive_entries(
    context: SecurityContext,
    *,
    max_entries: int = 250_000,
    max_directories: int = 25_000,
    max_depth: int = 64,
) -> tuple[FilesystemEntry, ...]:
    """Enumerate exact secret paths for platforms without deny-read glob support."""
    if context.workspace_root is None or sys.platform.startswith("linux"):
        return ()
    root = LocalPathReference.from_host_path(context.workspace_root).resolve_at_boundary(
        strict=True
    )
    queue: list[tuple[Path, int]] = [(root, 0)]
    protected: list[FilesystemEntry] = []
    seen_entries = 0
    seen_directories = 0
    while queue:
        directory, depth = queue.pop()
        seen_directories += 1
        if seen_directories > max_directories:
            raise RuntimeError("sensitive workspace path discovery exceeded directory budget")
        try:
            for child in directory.iterdir():
                seen_entries += 1
                if seen_entries > max_entries:
                    raise RuntimeError("sensitive workspace path discovery exceeded entry budget")
                name = child.name.casefold()
                if name in _SENSITIVE_DIRECTORY_NAMES:
                    protected.append(
                        FilesystemEntry(child, FilesystemAccess.DENY, escalatable=False)
                    )
                    continue
                if _is_sensitive_workspace_path(child, root):
                    protected.append(
                        FilesystemEntry(child, FilesystemAccess.DENY, escalatable=False)
                    )
                try:
                    child_info = child.lstat()
                except OSError as exc:
                    raise RuntimeError("sensitive workspace path discovery failed") from exc
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    not stat.S_ISLNK(child_info.st_mode)
                    and not getattr(child_info, "st_file_attributes", 0) & reparse_flag
                    and stat.S_ISDIR(child_info.st_mode)
                ):
                    if depth >= max_depth:
                        raise RuntimeError(
                            "sensitive workspace path discovery exceeded depth budget"
                        )
                    queue.append((child, depth + 1))
        except OSError:
            # If a directory cannot be inspected, deny that whole subtree rather than
            # guessing that it contains no credentials.
            protected.append(
                FilesystemEntry(directory, FilesystemAccess.DENY, escalatable=False)
            )
    return tuple(protected)


def _is_sensitive_workspace_path(target: Path, workspace_root: Path) -> bool:
    try:
        relative = target.relative_to(workspace_root.resolve(strict=False))
    except ValueError:
        return False
    parts = [_portable_component(part) for part in relative.parts]
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in parts):
        return True
    if not parts:
        return False
    name = parts[-1]
    return (
        name in _SENSITIVE_FILE_NAMES
        or name.startswith(".env.")
        or name.endswith(_SENSITIVE_FILE_SUFFIXES)
    )


def is_protected_workspace_path(target: Path, workspace_root: Path) -> bool:
    """Share the metadata deny predicate with bounded search callers."""

    return _is_sensitive_workspace_path(target, workspace_root)


def _resolve_policy_target(raw_path: str | Path) -> Path:
    reference = LocalPathReference.parse(str(raw_path))
    if reference.kind.value != "plain_path":
        raise LocalPathReferenceError("file policy accepts only a local path")
    return reference.resolve_at_boundary(strict=False)


def _validate_policy_target(target: Path, *, operation: str) -> None:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise LocalPathReferenceError("文件目标是符号链接")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(info, "st_file_attributes", 0) & reparse_flag:
        raise LocalPathReferenceError("文件目标是 reparse point")
    if stat.S_ISDIR(info.st_mode):
        if operation != "read":
            raise LocalPathReferenceError("写入目标不是普通文件")
        return
    if not stat.S_ISREG(info.st_mode):
        raise LocalPathReferenceError("文件目标不是普通文件")
    if info.st_nlink > 1:
        raise LocalPathReferenceError("文件目标存在多个硬链接")


def _portable_component(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _is_filesystem_root(path: Path) -> bool:
    return path == Path(path.anchor) if path.anchor else False


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True
