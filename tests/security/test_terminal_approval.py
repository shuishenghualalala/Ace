from __future__ import annotations

import json

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.core.errors import ToolError
from crew.security.context import SecurityContext
from crew.security.launch import ProcessLaunch, current_process_launch, issue_process_launch
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    PermissionProfile,
    PermissionProfileKind,
    SandboxPermissions,
)
from crew.security.runtime_client import ShellClassification, ShellVerdict
from crew.tools.builtin import (
    _classification_auto_allows,
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



class _ImmediateAllowService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.grants = SimpleNamespace(
            additional_permissions=lambda _context: AdditionalPermissionProfile()
        )

    @staticmethod
    def mode_for(_context):
        return ConversationPermissionMode.REQUEST_APPROVAL

    @staticmethod
    def authorize_exec_action(
        _context,
        _action,
        *,
        tool_name,
        risk_class,
        auto_allow=False,
        additional_permissions=None,
        preview="",
        proposed_argv_prefix=None,
    ):
        del tool_name, risk_class, auto_allow, preview, proposed_argv_prefix
        del additional_permissions
        return True, None


def _record_terminal_spawns(monkeypatch: pytest.MonkeyPatch):
    from crew.tools.process_registry import process_registry

    calls = []

    def record(kind):
        def spawn(command, **kwargs):
            calls.append((kind, command, kwargs))
            return SimpleNamespace(
                id="proc-test",
                pid=123,
                exited=True,
                exit_code=0,
                output_buffer="",
                stable_error_code="",
            )

        return spawn

    monkeypatch.setattr(process_registry, "_task_runtime", None)
    monkeypatch.setattr(process_registry, "spawn_local", record("local"))
    monkeypatch.setattr(process_registry, "spawn_security", record("security"))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.parametrize(
    ("workspace_store", "security_service"),
    [
        (None, object()),
        (object(), None),
        (None, None),
    ],
    ids=["missing-workspace-store", "missing-security-service", "missing-both"],
)
async def test_terminal_missing_security_dependencies_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    background: bool,
    workspace_store,
    security_service,
) -> None:
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    result = json.loads(
        await handle_terminal(
            {"command": "echo must-not-run", "background": background},
            workspace_store=workspace_store,
            security_service=security_service,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "security_context_missing"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
async def test_terminal_missing_compiled_process_launch_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    background: bool,
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
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.security.launch.compile_process_launch", lambda *args, **kwargs: None)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    async def classify_shell(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell",
        classify_shell,
    )

    result = json.loads(
        await handle_terminal(
            {"command": "echo must-not-run", "background": background},
            workspace_store=object(),
            security_service=_ImmediateAllowService(tmp_path / "crew.db"),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "security_launch_missing"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.parametrize("failure", ["missing-helper", "bad-manifest"])
async def test_terminal_managed_runtime_failure_never_reaches_host_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    background: bool,
    failure: str,
) -> None:
    from crew.tools.process_registry import process_registry

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
    helper = tmp_path / "ace-security-runtime"
    if failure == "bad-manifest":
        helper.write_bytes(b"runtime")
        helper.with_name("runtime-manifest.json").write_text("{broken", encoding="utf-8")
    launch = issue_process_launch(
        context,
        PermissionProfile(PermissionProfileKind.MANAGED),
        helper_argv=(str(helper),),
    )
    host_calls = []
    monkeypatch.setattr(process_registry, "_task_runtime", None)
    monkeypatch.setattr(
        process_registry,
        "spawn_local",
        lambda *args, **kwargs: host_calls.append(("local", args, kwargs)),
    )
    monkeypatch.setattr(
        process_registry,
        "_spawn_managed_bridge",
        lambda *args, **kwargs: host_calls.append(("bridge", args, kwargs)),
    )
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr(
        "crew.security.launch.compile_process_launch",
        lambda *args, **kwargs: launch,
    )
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    async def classify_shell(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell",
        classify_shell,
    )

    result = json.loads(
        await handle_terminal(
            {"command": "echo must-not-run", "background": background},
            workspace_store=object(),
            security_service=_ImmediateAllowService(tmp_path / "crew.db"),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "sandbox_unavailable"
    assert host_calls == []


@pytest.mark.asyncio
async def test_terminal_audits_post_approval_boundary_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
    from crew.tools.process_registry import process_registry

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

    class AuditedService(_ImmediateAllowService):
        def __init__(self, db_path: Path) -> None:
            super().__init__(db_path)
            self.denials = []

        def _audit_exec(self, audit_context, action, decision, source, tool_name):
            self.denials.append((audit_context, action, decision, source, tool_name))

    service = AuditedService(tmp_path / "crew.db")

    def compile_launch(launch_context, _mode, **kwargs):
        return issue_process_launch(
            launch_context,
            PermissionProfile(PermissionProfileKind.DISABLED),
            approved_action=kwargs["approved_action"],
        )

    def reject_spawn(*_args, **_kwargs):
        raise NativeRuntimeError(
            RuntimeErrorCode.SANDBOX_DENIED,
            "authorization snapshot rejected",
        )

    async def classify_shell(*_args, **_kwargs):
        return None

    monkeypatch.setattr(process_registry, "_task_runtime", None)
    monkeypatch.setattr(process_registry, "spawn_security", reject_spawn)
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.security.launch.compile_process_launch", compile_launch)
    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell",
        classify_shell,
    )
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    result = json.loads(
        await handle_terminal(
            {"command": "echo rejected"},
            workspace_store=object(),
            security_service=service,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "sandbox_denied"
    assert len(service.denials) == 1
    assert service.denials[0][2:] == (
        "deny",
        "post_approval_boundary_sandbox_denied",
        "terminal",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
async def test_terminal_runtime_import_failure_never_reaches_any_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    background: bool,
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
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    class BrokenRuntimeClient:
        def __init__(self, _argv):
            raise ImportError("runtime client import failed")

    monkeypatch.setattr("crew.security.runtime_client.NativeRuntimeClient", BrokenRuntimeClient)

    result = json.loads(
        await handle_terminal(
            {"command": "echo must-not-run", "background": background},
            workspace_store=object(),
            security_service=_ImmediateAllowService(tmp_path / "crew.db"),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "security_unavailable"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
async def test_explicit_disabled_launch_is_not_a_missing_dependency_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    background: bool,
) -> None:
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)
    disabled = issue_process_launch(
        SecurityContext(
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )
    token = current_process_launch.set(disabled)
    try:
        result = json.loads(
            await handle_terminal(
                {"command": "echo explicit-disabled", "background": background},
                workspace_store=None,
                security_service=None,
            )
        )
    finally:
        current_process_launch.reset(token)

    assert result["success"] is True
    assert [kind for kind, _command, _kwargs in calls] == ["security"]
    assert calls[0][2]["launch"] is disabled


@pytest.mark.asyncio
async def test_terminal_direct_argv_never_passes_through_a_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)
    disabled = issue_process_launch(
        SecurityContext(
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )
    argv = [sys.executable, "-c", "print('literal ; && $HOME')"]
    token = current_process_launch.set(disabled)
    try:
        result = json.loads(
            await handle_terminal(
                {"argv": argv},
                workspace_store=None,
                security_service=None,
            )
        )
    finally:
        current_process_launch.reset(token)

    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0][2]["launch_argv"] == (
        str(Path(sys.executable).resolve(strict=True)),
        *argv[1:],
    )


@pytest.mark.asyncio
async def test_terminal_binds_launch_to_the_spawned_runtime_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from crew.tools.process_registry import process_registry

    context = SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="parent-task",
        cwd=tmp_path,
    )

    class Runtime:
        defaults = {"shell_execution": 30.0, "shell_inactivity": 600.0}
        auto_background_after = 15.0
        cancel_callback = None

        @staticmethod
        def create_runtime(**_kwargs):
            return {"task_id": "spawned-task"}

        @staticmethod
        def update(*_args, **_kwargs):
            return None

        @staticmethod
        def mark_running(*_args, **_kwargs):
            return None

        @staticmethod
        def get(*_args, **_kwargs):
            return {"status": "running"}

        @staticmethod
        def touch_activity(*_args, **_kwargs):
            return None

        @classmethod
        def attach_worker(cls, *_args, **kwargs):
            cls.cancel_callback = kwargs["cancel"]

    seen = {}

    def compile_launch(launch_context, _mode, **kwargs):
        return issue_process_launch(
            launch_context,
            PermissionProfile(PermissionProfileKind.DISABLED),
            approved_action=kwargs["approved_action"],
        )

    def spawn(_command, **kwargs):
        from crew.security.launch import validate_process_launch

        validate_process_launch(
            kwargs["launch"],
            expected_task_id=kwargs["task_id"],
        )
        seen.update(kwargs)
        return SimpleNamespace(
            id="proc-test",
            pid=123,
            owner_account_id="owner-a",
            exited=True,
            exit_code=0,
            output_buffer="",
            stable_error_code="",
        )

    async def classify_shell(*_args, **_kwargs):
        return None

    monkeypatch.setattr(process_registry, "_task_runtime", Runtime())
    monkeypatch.setattr(process_registry, "spawn_security", spawn)
    killed = []
    monkeypatch.setattr(
        process_registry,
        "kill_process",
        lambda session_id, *, owner_account_id: killed.append((session_id, owner_account_id)),
    )
    monkeypatch.setattr("crew.security.context.build_security_context", lambda _store: context)
    monkeypatch.setattr("crew.security.launch.compile_process_launch", compile_launch)
    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell",
        classify_shell,
    )
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)

    result = json.loads(
        await handle_terminal(
            {"command": "echo task-bound"},
            workspace_store=object(),
            security_service=_ImmediateAllowService(tmp_path / "crew.db"),
        )
    )

    assert result["success"] is True
    assert seen["task_id"] == "spawned-task"
    assert seen["launch"].task_id == "spawned-task"
    assert Runtime.cancel_callback is not None
    Runtime.cancel_callback("session revoked")
    assert killed == [("proc-test", "owner-a")]


@pytest.mark.asyncio
async def test_forged_disabled_launch_is_rejected_before_terminal_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _record_terminal_spawns(monkeypatch)
    monkeypatch.setattr("crew.tools.builtin._resolve_base_dir", lambda: tmp_path)
    forged = ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))
    token = current_process_launch.set(forged)
    try:
        result = json.loads(
            await handle_terminal(
                {"command": "echo forged"},
                workspace_store=None,
                security_service=None,
            )
        )
    finally:
        current_process_launch.reset(token)

    assert result["success"] is False
    assert result["error_code"] == "security_unavailable"
    assert calls == []

class _FullAccessService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.audit = None
        self.grants = SimpleNamespace(
            additional_permissions=lambda _context: AdditionalPermissionProfile()
        )

    @staticmethod
    def mode_for(_context):
        return ConversationPermissionMode.FULL_ACCESS

    @staticmethod
    def authorize_exec_action(*_args, **_kwargs):
        from crew.security.models import (
            AdditionalPermissionProfile,
            SandboxPermissions,
        )
        from crew.security.service import ExecAuthorization

        return ExecAuthorization(
            True,
            additional_permissions=AdditionalPermissionProfile(
                sandbox_permissions=SandboxPermissions.REQUIRE_ESCALATED
            ),
        )


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

    async def unparsed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "crew.security.runtime_client.NativeRuntimeClient.classify_shell", unparsed
    )

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
    assert kwargs["auto_allow"] is False
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
    assert payload["applied_permissions"]["sandbox_permissions"] == "require_escalated"


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
