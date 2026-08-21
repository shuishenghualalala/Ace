"""AUTO_REVIEW requires executable identity binding end to end."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.context import SecurityContext
from crew.security.launch import finalize_process_launch, issue_process_launch
from crew.security.models import (
    ConversationPermissionMode,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import (
    NativeRuntimeError,
    ShellClassification,
    ShellVerdict,
    _executable_identity,
)
from crew.tools.builtin import _classification_auto_allows


def test_auto_review_rejects_workspace_same_named_executable() -> None:
    fake = ShellClassification(
        shell_kind="bash",
        raw_command="./echo hi",
        parsed_commands=(("./echo", "hi"),),
        canonical_digest="d" * 64,
        verdict=ShellVerdict.ALLOW_READ_ONLY,
        reason="basename says echo",
    )
    assert _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, fake) is False


def test_auto_review_accepts_only_current_shell_and_command_identities() -> None:
    executable = str(Path(sys.executable).resolve(strict=True))
    _path, digest = _executable_identity(executable)
    safe = ShellClassification(
        shell_kind="bash",
        raw_command=executable,
        parsed_commands=((executable,),),
        canonical_digest="a" * 64,
        verdict=ShellVerdict.ALLOW_READ_ONLY,
        reason="all_commands_proven_read_only",
        executable=executable,
        executable_digest=digest,
        command_identities=((executable, digest),),
    )
    assert _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, safe)


def test_final_launch_rejects_replaced_bound_executable(tmp_path: Path) -> None:
    target = tmp_path / "tool"
    target.write_bytes(b"original")
    executable, digest = _executable_identity(str(target))
    action = normalize_exec_action(
        (executable,),
        tmp_path,
        executable_digest=digest,
        command_identities=((executable, digest),),
    )
    context = SecurityContext(
        os_user="user-a",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )
    launch = issue_process_launch(
        context,
        PermissionProfile(PermissionProfileKind.DISABLED),
        approved_action=action,
    )
    target.write_bytes(b"replacement")

    with pytest.raises(NativeRuntimeError, match="identity changed"):
        finalize_process_launch(
            launch,
            argv=action.argv,
            cwd=tmp_path,
            environment={},
        )


def test_classifier_evidence_does_not_expand_shell_action_authority(tmp_path: Path) -> None:
    plain = normalize_exec_action(
        ["bash", "-lc", "tool status"],
        tmp_path,
        raw_command="tool status",
    )
    classified = normalize_exec_action(
        ["bash", "-lc", "tool status"],
        tmp_path,
        raw_command="tool status",
        shell_kind="bash",
        parsed_commands=(("tool", "status"),),
        canonical_digest="a" * 64,
    )

    assert classified.digest == plain.digest
