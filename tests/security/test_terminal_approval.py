from __future__ import annotations

import json
from pathlib import Path

import pytest

from crew.core.errors import ToolError
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    SandboxPermissions,
)
from crew.tools.builtin import (
    _parse_additional_permissions,
    _validate_destructive_terminal_targets,
    handle_terminal,
)


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
        **kwargs,
    ):
        self.actions.append((context, action, tool_name, risk_class, kwargs))
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
async def test_every_managed_terminal_command_crosses_host_authorization(
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

    assert '"error_code": "approval_expired"' in result
    assert len(service.actions) == 1
    _ctx, action, tool_name, risk_class, kwargs = service.actions[0]
    assert tool_name == "terminal"
    assert risk_class in {"shell_command", "dangerous_command"}
    assert kwargs["requires_approval"] is (risk_class == "dangerous_command")
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
    payload = json.loads(result)
    assert payload["execution_boundary"] == "host"
    assert payload["effective_home"] == str(Path.home().resolve())
    assert payload["applied_permissions"]["sandbox_permissions"] == "use_default"


def test_terminal_permission_request_is_exact_and_project_metadata_is_escalatable(
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

    metadata_permission = _parse_additional_permissions(
        {"filesystem": [{"root": str(metadata), "access": "read_write"}]},
        cwd=workspace,
        security_context=context,
        mode=ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )
    assert metadata_permission.filesystem == (
        FilesystemEntry(metadata, FilesystemAccess.READ_WRITE),
    )

    metadata_child = metadata / "index"
    metadata_child.write_text("index", encoding="utf-8")
    promoted = _parse_additional_permissions(
        {"filesystem": [{"root": str(metadata_child), "access": "read_write"}]},
        cwd=workspace,
        security_context=context,
        mode=ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )
    assert promoted.filesystem == metadata_permission.filesystem
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


def test_destructive_terminal_requires_absolute_target(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="必须使用宿主绝对路径"):
        _validate_destructive_terminal_targets(
            (("rm", "screenshot.png"),),
            shell_kind="bash",
            raw_command="rm screenshot.png",
            workspace_root=tmp_path,
            requested_permissions=AdditionalPermissionProfile(),
        )

    with pytest.raises(ToolError, match="必须使用宿主绝对路径"):
        _validate_destructive_terminal_targets(
            (("sudo", "rm", "screenshot.png"),),
            shell_kind="bash",
            raw_command="sudo rm screenshot.png",
            workspace_root=tmp_path,
            requested_permissions=AdditionalPermissionProfile(),
        )

    with pytest.raises(ToolError, match="无法把删除命令静态绑定"):
        _validate_destructive_terminal_targets(
            (),
            shell_kind="bash",
            raw_command='rm "$HOME/Desktop/screenshot.png"',
            workspace_root=tmp_path,
            requested_permissions=AdditionalPermissionProfile(),
        )


def test_external_delete_target_must_match_read_write_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    desktop = tmp_path / "Desktop"
    workspace.mkdir()
    desktop.mkdir()
    target = desktop / "screenshot.png"
    target.write_text("image", encoding="utf-8")

    with pytest.raises(ToolError, match="必须为实际目标申请 read_write"):
        _validate_destructive_terminal_targets(
            (("rm", str(target)),),
            shell_kind="bash",
            raw_command=f"rm {target}",
            workspace_root=workspace,
            requested_permissions=AdditionalPermissionProfile(),
        )

    _validate_destructive_terminal_targets(
        (("rm", str(target)),),
        shell_kind="bash",
        raw_command=f"rm {target}",
        workspace_root=workspace,
        requested_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(target, FilesystemAccess.READ_WRITE),),
            sandbox_permissions=SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS,
        ),
    )
