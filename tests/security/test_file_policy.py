from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from crew.security.context import SecurityContext
from crew.security.file_policy import (
    FilePolicyResult,
    FilesystemAccess,
    _discovered_sensitive_entries,
    _protected_entries,
    assess_file_action,
)
from crew.security.actions import ActionKind, NormalizedAction, normalize_file_action
from crew.security.models import ConversationPermissionMode


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux discovery path")
def test_unreadable_workspace_directory_is_denied_instead_of_aborting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    real_iterdir = Path.iterdir

    def iterdir(self: Path):
        if self == locked:
            raise PermissionError(5, "Access is denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )

    entries = _discovered_sensitive_entries(context)

    assert any(
        entry.root == locked
        and entry.access is FilesystemAccess.DENY
        and not entry.escalatable
        for entry in entries
    )
    assert any(entry.root == tmp_path / ".env" for entry in entries)


def test_non_linux_sensitive_discovery_is_part_of_file_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    secret = nested / ".env.local"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    monkeypatch.setattr("crew.security.file_policy.sys.platform", "win32")
    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )

    protected = _protected_entries(context, tmp_path / "crew.db")

    assert any(entry.root == secret and entry.access is FilesystemAccess.DENY for entry in protected)


def test_file_policy_denies_link_and_special_file_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("safe", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")

    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=workspace,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=workspace,
    )
    link_assessment = assess_file_action(
        context,
        NormalizedAction(kind=ActionKind.FILE, path=str(link), operation="read"),
        ConversationPermissionMode.READ_ONLY,
        db_path=tmp_path / "crew.db",
    )
    assert link_assessment.result is FilePolicyResult.DENY

    if hasattr(os, "mkfifo"):
        fifo = workspace / "pipe"
        os.mkfifo(fifo)
        try:
            special_assessment = assess_file_action(
                context,
                NormalizedAction(kind=ActionKind.FILE, path=str(fifo), operation="read"),
                ConversationPermissionMode.READ_ONLY,
                db_path=tmp_path / "crew.db",
            )
            assert special_assessment.result is FilePolicyResult.DENY
        finally:
            fifo.unlink()
