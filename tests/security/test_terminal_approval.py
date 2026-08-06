from __future__ import annotations

from pathlib import Path

import pytest

from crew.security.context import SecurityContext
from crew.security.models import ConversationPermissionMode
from crew.security.runtime_client import ShellClassification, ShellVerdict
from crew.tools.builtin import _classification_auto_allows, handle_terminal


class _ApprovalOnlyService:
    def __init__(self) -> None:
        self.actions = []
        self.db_path = Path("crew.db")

    @staticmethod
    def mode_for(_context):
        from crew.security.models import ConversationPermissionMode

        return ConversationPermissionMode.REQUEST_APPROVAL

    def authorize_exec_action(self, context, action, *, tool_name, risk_class, auto_allow=False):
        self.actions.append((context, action, tool_name, risk_class))
        if auto_allow:
            return True, None
        return False, {"request_id": "approval-1"}

    async def await_decision(self, request_id):
        assert request_id == "approval-1"
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "echo safe-looking",
        "Remove-Item -Recurse -Force C:\\\\Users",
        "a=rm; $a -rf /tmp/example",
    ],
)
async def test_every_managed_terminal_command_requires_host_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    """Unknown/PowerShell/indirect commands must not bypass approval by missing a regex."""
    context = SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )
    service = _ApprovalOnlyService()
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    result = await handle_terminal(
        {"command": command},
        workspace_store=object(),
        security_service=service,
    )

    assert '"error_code": "approval_rejected"' in result
    assert len(service.actions) == 1
    _ctx, action, tool_name, risk_class = service.actions[0]
    assert tool_name == "terminal"
    assert risk_class in {"shell_command", "dangerous_command"}
    assert action.raw_command == command
    assert command in action.argv[-1]


def test_auto_review_only_trusts_verified_read_only_classification() -> None:
    # ``whoami`` resolves to a real system binary on both platforms
    # (/usr/bin/whoami on POSIX, C:\Windows\System32\whoami.exe on Windows), so it
    # passes executable provenance and may be auto-allowed. PowerShell built-in
    # cmdlets like Write-Output/Get-Content have no on-disk binary, so ``which``
    # cannot pin them to a trusted bin dir — they now fall back to ASK rather than
    # being trusted by bare basename (H-3).
    safe = ShellClassification(
        shell_kind="bash",
        raw_command="whoami",
        parsed_commands=(("whoami",),),
        canonical_digest="a" * 64,
        verdict=ShellVerdict.ALLOW_READ_ONLY,
        reason="all_commands_proven_read_only",
    )
    ask = ShellClassification(
        shell_kind="powershell",
        raw_command="Remove-Item x",
        parsed_commands=(("Remove-Item", "x"),),
        canonical_digest="b" * 64,
        verdict=ShellVerdict.ASK,
        reason="command_not_in_read_only_policy",
    )
    sensitive_read = ShellClassification(
        shell_kind="powershell",
        raw_command="Get-Content ~/.ssh/id_rsa",
        parsed_commands=(("Get-Content", "~/.ssh/id_rsa"),),
        canonical_digest="c" * 64,
        verdict=ShellVerdict.ALLOW_READ_ONLY,
        reason="all_commands_proven_read_only",
    )
    assert not _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, safe)
    assert not _classification_auto_allows(ConversationPermissionMode.REQUEST_APPROVAL, safe)
    assert not _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, ask)
    assert not _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, sensitive_read)
    assert not _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, None)
