from pathlib import Path

import pytest

from crew.security.models import (
    AdditionalPermissionProfile,
    ApprovalPolicy,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemGlobAccess,
    FilesystemGlobEntry,
    FilesystemOperation,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.policy import filesystem_operation_allowed, settings_for_mode
from crew.security.models import SandboxPermissions, merge_additional_permissions


def test_ui_modes_map_to_two_independent_security_axes(tmp_path: Path) -> None:
    read_only = settings_for_mode(ConversationPermissionMode.READ_ONLY, tmp_path)
    request = settings_for_mode(ConversationPermissionMode.REQUEST_APPROVAL, tmp_path)
    auto = settings_for_mode(ConversationPermissionMode.AUTO_REVIEW, tmp_path)
    full = settings_for_mode(ConversationPermissionMode.FULL_ACCESS, tmp_path)

    assert read_only.profile.kind is PermissionProfileKind.MANAGED
    assert read_only.approval_policy is ApprovalPolicy.REQUEST
    assert filesystem_operation_allowed(
        read_only.profile,
        AdditionalPermissionProfile(),
        tmp_path / "README.md",
        FilesystemOperation.READ,
    )
    assert not filesystem_operation_allowed(
        read_only.profile,
        AdditionalPermissionProfile(),
        tmp_path / "README.md",
        FilesystemOperation.WRITE,
    )
    assert request.profile.kind is PermissionProfileKind.MANAGED
    assert request.approval_policy is ApprovalPolicy.REQUEST
    assert auto.profile.kind is PermissionProfileKind.MANAGED
    assert auto.approval_policy is ApprovalPolicy.AUTO_REVIEW
    assert request.profile == auto.profile
    assert full.profile.kind is PermissionProfileKind.MANAGED
    assert full.profile.network is NetworkPolicy.RESTRICTED
    # 合并后安全语义：FULL_ACCESS 不再注入宿主 home，审批策略为 NEVER，
    # 越权只能走获批的 require_escalated。
    assert full.approval_policy is ApprovalPolicy.NEVER
    roots = {entry.root for entry in full.profile.filesystem}
    assert tmp_path.resolve() in roots


def test_unknown_ui_mode_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知对话安全模式"):
        settings_for_mode("unknown", tmp_path)  # type: ignore[arg-type]




def test_non_escalatable_deny_cannot_be_overridden(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / ".crew"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(FilesystemEntry(protected, FilesystemAccess.DENY, escalatable=False),),
    ).profile
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(protected / "audit.db", FilesystemAccess.READ_WRITE),)
    )

    assert not filesystem_operation_allowed(
        profile, additional, protected / "audit.db", FilesystemOperation.READ
    )


def test_additional_permission_cannot_override_any_base_deny(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(FilesystemEntry(outside, FilesystemAccess.DENY),),
    ).profile
    approved = outside / "approved.txt"
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(approved, FilesystemAccess.READ),)
    )

    assert not filesystem_operation_allowed(
        profile,
        additional,
        approved,
        FilesystemOperation.READ,
    )
    assert not filesystem_operation_allowed(
        profile, additional, approved, FilesystemOperation.WRITE
    )


def test_network_entry_is_exact_and_canonical() -> None:
    entry = NetworkEntry("EXAMPLE.com.", 443, "HTTPS")
    assert (entry.host, entry.port, entry.protocol) == ("example.com", 443, "https")
    with pytest.raises(ValueError, match="wildcard"):
        NetworkEntry("*.example.com", 443, "https")


def test_filesystem_glob_can_only_deny_reads_on_canonical_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),),
        filesystem_globs=(
            FilesystemGlobEntry(
                workspace,
                "**/*.pem",
                FilesystemGlobAccess.DENY_READ,
            ),
        ),
    )
    target = workspace / "nested" / ".." / "secret.pem"

    assert not filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(),
        target,
        FilesystemOperation.READ,
    )
    assert filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(),
        target,
        FilesystemOperation.WRITE,
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "../*.pem",
        "/absolute/*.pem",
        "",
        "safe\x00*.pem",
        "[unterminated",
        "foo/**bar",
        "{one,two}.pem",
        "foo//bar.pem",
    ],
)
def test_filesystem_glob_rejects_ambiguous_patterns(tmp_path: Path, pattern: str) -> None:
    with pytest.raises(ValueError):
        FilesystemGlobEntry(tmp_path, pattern)


def test_filesystem_glob_rejects_non_deny_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deny_read"):
        FilesystemGlobEntry(tmp_path, "*.pem", "allow")  # type: ignore[arg-type]


def test_permission_merge_keeps_strongest_same_root_access(tmp_path: Path) -> None:
    root = tmp_path / "approved"

    merged = merge_additional_permissions(
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(root, FilesystemAccess.READ),)
        ),
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(root, FilesystemAccess.READ_WRITE),)
        ),
    )

    assert merged.filesystem == (FilesystemEntry(root, FilesystemAccess.READ_WRITE),)
    assert (
        merged.sandbox_permissions
        is SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS
    )


def test_managed_profile_is_broadly_read_only_and_workspace_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = settings_for_mode(ConversationPermissionMode.REQUEST_APPROVAL, workspace).profile

    assert filesystem_operation_allowed(
        profile, AdditionalPermissionProfile(), workspace / "src" / "app.py", FilesystemOperation.WRITE
    )
    assert filesystem_operation_allowed(
        profile, AdditionalPermissionProfile(), tmp_path / "outside.txt", FilesystemOperation.READ
    )
    assert not filesystem_operation_allowed(
        profile, AdditionalPermissionProfile(), tmp_path / "outside.txt", FilesystemOperation.WRITE
    )






# 合并取舍：dev 的 escalatable 标记覆盖与 runtime-home carve-out 语义与
# 本分支的 specificity + deny-ceiling 模型冲突，按“安全以本分支为主”不保留
# （test_more_specific_base_workspace_carved / non_escalatable_upgrade /
# escalatable_parent_deny 三个 dev 用例随之移除）。
def test_escalatable_read_only_root_requires_exact_write_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / ".git"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(FilesystemEntry(protected, FilesystemAccess.READ, escalatable=True),),
    ).profile

    assert not filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(),
        protected,
        FilesystemOperation.WRITE,
    )
    assert filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(protected, FilesystemAccess.READ_WRITE),)
        ),
        protected,
        FilesystemOperation.WRITE,
    )


