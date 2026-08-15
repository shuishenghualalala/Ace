"""Structured file access classification before any host file is opened."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sys

from crew.security.actions import NormalizedAction
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemGlobEntry,
    FilesystemOperation,
)
from crew.security.policy import filesystem_operation_allowed, settings_for_mode
from crew.state.home import get_crew_home


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
    target = Path(action.path).expanduser().resolve(strict=False)
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

    protected = _protected_entries(context, db_path)
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


def _protected_entries(
    context: SecurityContext, db_path: str | Path
) -> tuple[FilesystemEntry, ...]:
    denied = [
        FilesystemEntry(Path(db_path), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-wal"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-shm"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-journal"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(get_crew_home(), FilesystemAccess.DENY, escalatable=False),
    ]
    if context.workspace_root is not None:
        workspace_root = context.workspace_root.resolve(strict=False)
        denied.extend(
            FilesystemEntry(
                workspace_root / name,
                FilesystemAccess.READ,
                escalatable=False,
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
        except OSError:
            dynamic_sensitive = []
        denied.extend(
            FilesystemEntry(path, FilesystemAccess.DENY, escalatable=False)
            for path in dynamic_sensitive
        )
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
    root = context.workspace_root.resolve(strict=True)
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
                if (
                    not child.is_symlink()
                    and not getattr(child, "is_junction", lambda: False)()
                    and child.is_dir()
                ):
                    if depth >= max_depth:
                        raise RuntimeError(
                            "sensitive workspace path discovery exceeded depth budget"
                        )
                    queue.append((child, depth + 1))
        except OSError as exc:
            raise RuntimeError("sensitive workspace path discovery failed") from exc
    return tuple(protected)


def _is_sensitive_workspace_path(target: Path, workspace_root: Path) -> bool:
    try:
        relative = target.relative_to(workspace_root.resolve(strict=False))
    except ValueError:
        return False
    parts = [part.casefold() for part in relative.parts]
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


def _is_filesystem_root(path: Path) -> bool:
    return path == Path(path.anchor) if path.anchor else False


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True
