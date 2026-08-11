"""Central mapping and precedence rules for conversation security modes."""

from __future__ import annotations

from pathlib import Path

from crew.security.models import (
    AdditionalPermissionProfile,
    ApprovalPolicy,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemOperation,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
    SecurityModeSettings,
)


def settings_for_mode(
    mode: ConversationPermissionMode,
    workspace_root: Path | None,
    *,
    deny_entries: tuple[FilesystemEntry, ...] = (),
) -> SecurityModeSettings:
    """Compile one user-facing mode into sandbox permissions and approval policy."""
    if mode not in {
        ConversationPermissionMode.REQUEST_APPROVAL,
        ConversationPermissionMode.AUTO_REVIEW,
        ConversationPermissionMode.FULL_ACCESS,
    }:
        raise ValueError(f"未知对话安全模式: {mode!r}")
    filesystem = (
        (FilesystemEntry(workspace_root, FilesystemAccess.READ_WRITE),) if workspace_root else ()
    )
    if mode is ConversationPermissionMode.FULL_ACCESS:
        # 完全访问权限明确选择宿主用户权限，不再编译文件或网络沙箱。
        # 不可逾越的破坏性红线仍由终端 hardline 与结构化文件根目录检查负责。
        return SecurityModeSettings(
            profile=PermissionProfile(
                kind=PermissionProfileKind.DISABLED,
                network=NetworkPolicy.UNRESTRICTED,
            ),
            approval_policy=ApprovalPolicy.NEVER,
        )

    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(*filesystem, *deny_entries),
        network=NetworkPolicy.RESTRICTED,
    )
    approval = (
        ApprovalPolicy.REQUEST
        if mode is ConversationPermissionMode.REQUEST_APPROVAL
        else ApprovalPolicy.AUTO_REVIEW
    )
    return SecurityModeSettings(profile=profile, approval_policy=approval)


def filesystem_operation_allowed(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile,
    target: Path,
    operation: FilesystemOperation,
) -> bool:
    """Apply deny and specificity precedence to one canonical filesystem target."""
    if profile.kind is PermissionProfileKind.DISABLED:
        return True

    resolved = target.expanduser().resolve(strict=False)
    base_matches = [entry for entry in profile.filesystem if _contains(entry.root, resolved)]
    if base_matches:
        base_specificity = max(len(entry.root.parts) for entry in base_matches)
        selected_base = [
            entry for entry in base_matches if len(entry.root.parts) == base_specificity
        ]
        # Immutable denies/read-only entries cannot be overridden by an
        # approval overlay.  A more-specific host-owned base entry can still
        # carve the task workspace out of a broad runtime-home deny.
        if any(
            entry.access is FilesystemAccess.DENY and not entry.escalatable
            for entry in selected_base
        ):
            return False
        if operation is FilesystemOperation.WRITE and any(
            entry.access is FilesystemAccess.READ and not entry.escalatable
            for entry in selected_base
        ):
            return False

    matches = [
        entry
        for entry in (*profile.filesystem, *additional.filesystem)
        if _contains(entry.root, resolved)
    ]
    if not matches:
        return False
    specificity = max(len(entry.root.parts) for entry in matches)
    selected = [entry for entry in matches if len(entry.root.parts) == specificity]
    if any(entry.access is FilesystemAccess.DENY for entry in selected):
        return False
    if operation is FilesystemOperation.READ:
        return any(
            entry.access in {FilesystemAccess.READ, FilesystemAccess.READ_WRITE}
            for entry in selected
        )
    return any(entry.access is FilesystemAccess.READ_WRITE for entry in selected)


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True
