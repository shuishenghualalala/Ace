from __future__ import annotations

from pathlib import Path

import pytest

from crew.core.errors import ToolError
from crew.security.context import SecurityContext
from crew.security.models import ConversationPermissionMode, SandboxPermissions
from crew.tools.builtin import _parse_additional_permissions, handle_terminal


class _ApprovalOnlyService:
    def __init__(self) -> None:
        self.actions = []
        self.db_path = Path("crew.db")

    @staticmethod
    def mode_for(_context):
        from crew.security.models import ConversationPermissionMode

        return ConversationPermissionMode.REQUEST_APPROVAL

    def authorize_exec_action(
        self,
        context,
        action,
        *,
        tool_name,
        risk_class,
        **_kwargs,
    ):
        self.actions.append((context, action, tool_name, risk_class))
        return False, {"request_id": "approval-1"}

    async def await_decision(self, request_id):
        assert request_id == "approval-1"
        return None


class _FullAccessService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.audit = None

    @staticmethod
    def mode_for(_context):
        return ConversationPermissionMode.FULL_ACCESS

    @staticmethod
    def authorize_exec_action(*_args, **_kwargs):
        return True, None


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


@pytest.mark.asyncio
async def test_full_access_does_not_depend_on_native_classifier_or_permission_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("full access must not require the native classifier")

    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell",
        fail_if_called,
    )
    result = await handle_terminal(
        {
            "command": "echo full-access-ok",
            "additional_permissions": {
                "filesystem": [{"root": str(tmp_path.anchor), "access": "read_write"}],
            },
        },
        workspace_store=object(),
        security_service=_FullAccessService(tmp_path / "crew.db"),
    )

    assert '"success": true' in result
    assert "full-access-ok" in result


def test_terminal_permission_request_is_exact_and_protected_metadata_stays_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    metadata = workspace / ".git"
    workspace.mkdir()
    outside.mkdir()
    metadata.mkdir()
    monkeypatch.setattr("crew.security.policy.tempfile.gettempdir", lambda: str(workspace))
    context = SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
        workspace_root=workspace,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=workspace,
    )

    profile = _parse_additional_permissions(
        {
            "filesystem": [{"root": str(outside), "access": "read_write"}],
            "network": [{
                "host": "Uploads.Example.COM.",
                "port": 443,
                "protocol": "HTTPS",
            }],
        },
        cwd=workspace,
        security_context=context,
        mode=ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )
    assert profile.filesystem[0].root == outside.resolve()
    assert profile.network[0].host == "uploads.example.com"
    redundant = _parse_additional_permissions(
        {"filesystem": [{"root": str(workspace), "access": "read_write"}]},
        cwd=workspace,
        security_context=context,
        mode=ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )
    assert redundant.empty

    with pytest.raises(ToolError, match="受保护路径"):
        _parse_additional_permissions(
            {"filesystem": [{"root": str(metadata), "access": "read_write"}]},
            cwd=workspace,
            security_context=context,
            mode=ConversationPermissionMode.AUTO_REVIEW,
            db_path=tmp_path / "crew.db",
        )
    with pytest.raises(ToolError, match="已存在路径"):
        _parse_additional_permissions(
            {"filesystem": [{"root": str(tmp_path / "missing"), "access": "read"}]},
            cwd=workspace,
            security_context=context,
            mode=ConversationPermissionMode.AUTO_REVIEW,
            db_path=tmp_path / "crew.db",
        )


def test_terminal_sandbox_override_validation(tmp_path: Path) -> None:
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
    escalated = _parse_additional_permissions(
        None,
        cwd=tmp_path,
        security_context=context,
        mode=ConversationPermissionMode.REQUEST_APPROVAL,
        db_path=tmp_path / "crew.db",
        sandbox_permissions="require_escalated",
    )
    assert escalated.sandbox_permissions is SandboxPermissions.REQUIRE_ESCALATED
    with pytest.raises(ToolError, match="sandbox_permissions"):
        _parse_additional_permissions(
            None,
            cwd=tmp_path,
            security_context=context,
            mode=ConversationPermissionMode.REQUEST_APPROVAL,
            db_path=tmp_path / "crew.db",
            sandbox_permissions="unknown",
        )
