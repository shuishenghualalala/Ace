"""Structured file access classification before any host file is opened."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from crew.security.actions import NormalizedAction
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
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
) -> FilePolicyAssessment:
    """Classify one canonical path with immutable protected entries."""
    target = Path(action.path).expanduser().resolve(strict=False)
    operation = (
        FilesystemOperation.READ if action.operation == "read" else FilesystemOperation.WRITE
    )
    if operation is FilesystemOperation.WRITE and _is_filesystem_root(target):
        return FilePolicyAssessment(FilePolicyResult.DENY, "永久拒绝写入文件系统根")

    protected = _protected_entries(context, db_path)
    matching = [entry for entry in protected if _contains(entry.root, target)]
    if any(
        entry.access is FilesystemAccess.DENY and not entry.escalatable
        for entry in matching
    ):
        return FilePolicyAssessment(FilePolicyResult.DENY, "目标属于不可升级的运行时或凭据路径")
    if operation is FilesystemOperation.WRITE and any(
        entry.access is FilesystemAccess.READ and not entry.escalatable
        for entry in matching
    ):
        return FilePolicyAssessment(FilePolicyResult.DENY, "受保护项目元数据只读")

    settings = settings_for_mode(mode, context.workspace_root, deny_entries=protected)
    if filesystem_operation_allowed(
        settings.profile,
        AdditionalPermissionProfile(),
        target,
        operation,
    ):
        return FilePolicyAssessment(FilePolicyResult.ALLOW, "base_profile")
    return FilePolicyAssessment(FilePolicyResult.REQUIRE_APPROVAL, "项目外路径需要额外授权")


def _protected_entries(context: SecurityContext, db_path: str | Path) -> tuple[FilesystemEntry, ...]:
    denied = [
        FilesystemEntry(Path(db_path), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-wal"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-shm"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(Path(f"{db_path}-journal"), FilesystemAccess.DENY, escalatable=False),
        FilesystemEntry(get_crew_home(), FilesystemAccess.DENY, escalatable=False),
    ]
    if context.workspace_root is not None:
        denied.extend(
            FilesystemEntry(
                context.workspace_root / name,
                FilesystemAccess.READ,
                escalatable=False,
            )
            for name in (".git", ".agents", ".crew")
        )
    return tuple(denied)


def _is_filesystem_root(path: Path) -> bool:
    return path == Path(path.anchor) if path.anchor else False


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True
