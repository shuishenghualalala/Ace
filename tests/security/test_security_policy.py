from pathlib import Path

import pytest

from crew.security.models import (
    AdditionalPermissionProfile,
    ApprovalPolicy,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemOperation,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfileKind,
)
from crew.security.policy import filesystem_operation_allowed, settings_for_mode


def test_ui_modes_map_to_two_independent_security_axes(tmp_path: Path) -> None:
    request = settings_for_mode(ConversationPermissionMode.REQUEST_APPROVAL, tmp_path)
    auto = settings_for_mode(ConversationPermissionMode.AUTO_REVIEW, tmp_path)
    full = settings_for_mode(ConversationPermissionMode.FULL_ACCESS, tmp_path)

    assert request.profile.kind is PermissionProfileKind.MANAGED
    assert request.approval_policy is ApprovalPolicy.REQUEST
    assert auto.profile.kind is PermissionProfileKind.MANAGED
    assert auto.approval_policy is ApprovalPolicy.AUTO_REVIEW
    assert request.profile == auto.profile
    assert full.profile.kind is PermissionProfileKind.DISABLED
    assert full.profile.network is NetworkPolicy.UNRESTRICTED
    assert full.approval_policy is ApprovalPolicy.NEVER
    assert full.profile.filesystem == ()


def test_unknown_ui_mode_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知对话安全模式"):
        settings_for_mode("unknown", tmp_path)  # type: ignore[arg-type]


def test_managed_profile_defaults_to_workspace_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = settings_for_mode(ConversationPermissionMode.REQUEST_APPROVAL, workspace).profile

    assert filesystem_operation_allowed(
        profile, AdditionalPermissionProfile(), workspace / "src" / "app.py", FilesystemOperation.WRITE
    )
    assert not filesystem_operation_allowed(
        profile, AdditionalPermissionProfile(), tmp_path / "outside.txt", FilesystemOperation.READ
    )


def test_non_escalatable_deny_cannot_be_overridden(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / ".crew"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(
            FilesystemEntry(protected, FilesystemAccess.DENY, escalatable=False),
        ),
    ).profile
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(protected / "audit.db", FilesystemAccess.READ_WRITE),)
    )

    assert not filesystem_operation_allowed(
        profile, additional, protected / "audit.db", FilesystemOperation.READ
    )


def test_more_specific_base_workspace_can_be_carved_from_runtime_home(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    workspace = runtime_home / "accounts" / "owner" / "task_workspaces" / "default"
    workspace.mkdir(parents=True)
    sibling = runtime_home / "accounts" / "owner" / "identity.json"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(
            FilesystemEntry(runtime_home, FilesystemAccess.DENY, escalatable=False),
        ),
    ).profile

    assert filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(),
        workspace / "artifact.txt",
        FilesystemOperation.WRITE,
    )
    assert not filesystem_operation_allowed(
        profile,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(sibling, FilesystemAccess.READ_WRITE),)
        ),
        sibling,
        FilesystemOperation.WRITE,
    )


def test_non_escalatable_read_only_root_cannot_be_upgraded_to_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / ".git"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(FilesystemEntry(protected, FilesystemAccess.READ, escalatable=False),),
    ).profile
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(protected, FilesystemAccess.READ_WRITE),)
    )

    assert not filesystem_operation_allowed(
        profile,
        additional,
        protected,
        FilesystemOperation.WRITE,
    )


def test_specific_addition_can_override_escalatable_parent_deny(tmp_path: Path) -> None:
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

    assert filesystem_operation_allowed(profile, additional, approved, FilesystemOperation.READ)
    assert not filesystem_operation_allowed(profile, additional, approved, FilesystemOperation.WRITE)


def test_network_entry_is_exact_and_canonical() -> None:
    entry = NetworkEntry("EXAMPLE.com.", 443, "HTTPS")
    assert (entry.host, entry.port, entry.protocol) == ("example.com", 443, "https")
    with pytest.raises(ValueError, match="wildcard"):
        NetworkEntry("*.example.com", 443, "https")
