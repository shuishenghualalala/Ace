"""Central mapping and precedence rules for conversation security modes."""

from __future__ import annotations

from pathlib import Path

from crew.state.home import get_crew_home

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
        # “完全访问”是宽权限受管模式，不再等同宿主裸跑。开放 workspace 与
        # 当前用户 home，随后由更具体、不可升级的 deny_entries 覆盖 Ace
        # DB/proof/identity/审计和 protected metadata；home 外仍需精确批准。
        user_home = Path.home().resolve(strict=False)
        broad_roots = {entry.root for entry in filesystem}
        if user_home != get_crew_home().resolve(strict=False) and user_home not in broad_roots:
            filesystem = (*filesystem, FilesystemEntry(user_home, FilesystemAccess.READ_WRITE))
        return SecurityModeSettings(
            profile=PermissionProfile(
                kind=PermissionProfileKind.MANAGED,
                filesystem=(*filesystem, *deny_entries),
                network=NetworkPolicy.RESTRICTED,
            ),
            approval_policy=ApprovalPolicy.REQUEST,
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
    if any(entry.access is FilesystemAccess.DENY and not entry.escalatable for entry in base_matches):
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
