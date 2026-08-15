from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.context import SecurityContext
from crew.security.launch import (
    compile_process_launch,
    finalize_process_launch,
    issue_process_launch,
    validate_process_launch,
)
from crew.security.models import (
    ConversationPermissionMode,
    PermissionProfile,
    PermissionProfileKind,
    SandboxablePreference,
)
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode
from crew.security.snapshot import AuthorizationSnapshotError, verify_authorization_snapshot
from crew.tools.process_registry import ProcessRegistry, ProcessSession


def _context(tmp_path: Path) -> SecurityContext:
    return SecurityContext(
        os_user="host-user",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _managed_helper(tmp_path: Path) -> Path:
    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"test-runtime")
    helper.with_name("runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": helper.name,
                "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return helper


def test_require_fails_closed_when_native_backend_is_unavailable(
    tmp_path: Path,
) -> None:
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.MANAGED),
        sandbox_preference=SandboxablePreference.REQUIRE,
        helper_argv=(str(tmp_path / "missing-runtime"),),
    )

    with pytest.raises(NativeRuntimeError) as denied:
        validate_process_launch(launch)

    assert denied.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert launch.sandboxed is True


def test_auto_managed_choice_does_not_fallback_when_backend_is_unavailable(
    tmp_path: Path,
) -> None:
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.MANAGED),
        sandbox_preference=SandboxablePreference.AUTO,
        helper_argv=(str(tmp_path / "missing-runtime"),),
    )

    with pytest.raises(NativeRuntimeError) as denied:
        validate_process_launch(launch)

    assert denied.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert launch.sandboxed is True


def test_require_binds_verified_backend_and_managed_network_policy(
    tmp_path: Path,
) -> None:
    helper = _managed_helper(tmp_path)
    argv = (sys.executable, "-V")
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.MANAGED),
        sandbox_preference=SandboxablePreference.REQUIRE,
        helper_argv=(str(helper),),
        approved_action=normalize_exec_action(argv, tmp_path),
    )

    validate_process_launch(launch)
    signed = finalize_process_launch(
        launch,
        argv=argv,
        cwd=tmp_path,
        environment={},
    )

    assert signed.snapshot.sandbox_preference == "require"
    assert signed.snapshot.sandboxed is True
    assert json.loads(signed.snapshot.profile_payload)["network"] == "restricted"


def test_forbid_requires_registered_host_fixed_surface(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered host-fixed"):
        issue_process_launch(
            _context(tmp_path),
            PermissionProfile(PermissionProfileKind.DISABLED),
            sandbox_preference=SandboxablePreference.FORBID,
        )
    with pytest.raises(ValueError, match="registered host-fixed"):
        issue_process_launch(
            _context(tmp_path),
            PermissionProfile(PermissionProfileKind.DISABLED),
            sandbox_preference=SandboxablePreference.FORBID,
            sandbox_system_surface="model-selected-surface",
        )
    with pytest.raises(TypeError, match="host-owned"):
        issue_process_launch(
            _context(tmp_path),
            PermissionProfile(PermissionProfileKind.DISABLED),
            sandbox_preference="forbid",  # type: ignore[arg-type]
            sandbox_system_surface="external-runtime-discovery",
        )
    with pytest.raises(ValueError, match="unsandboxed"):
        issue_process_launch(
            _context(tmp_path),
            PermissionProfile(PermissionProfileKind.DISABLED),
            sandbox_preference=SandboxablePreference.FORBID,
            sandbox_system_surface="external-runtime-discovery",
            helper_argv=(str(tmp_path / "runtime"),),
        )


def test_forbid_choice_is_bound_into_launch_and_authorization_snapshot(
    tmp_path: Path,
) -> None:
    argv = (sys.executable, "-V")
    action = normalize_exec_action(argv, tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
        sandbox_preference=SandboxablePreference.FORBID,
        sandbox_system_surface="external-runtime-discovery",
        approved_action=action,
    )

    signed = finalize_process_launch(
        launch,
        argv=argv,
        cwd=tmp_path,
        environment={},
    )

    assert launch.sandbox_preference is SandboxablePreference.FORBID
    assert launch.sandboxed is False
    assert launch.sandbox_system_surface == "external-runtime-discovery"
    assert signed.snapshot.sandbox_preference == "forbid"
    assert signed.snapshot.sandboxed is False
    assert signed.snapshot.sandbox_system_surface == "external-runtime-discovery"
    assert verify_authorization_snapshot(signed) is signed.snapshot
    with pytest.raises(FrozenInstanceError):
        signed.snapshot.sandboxed = True  # type: ignore[misc]


def test_sandbox_preference_and_final_choice_cannot_be_forged(
    tmp_path: Path,
) -> None:
    helper = _managed_helper(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.MANAGED),
        sandbox_preference=SandboxablePreference.AUTO,
        helper_argv=(str(helper),),
    )
    assert launch.sandboxed is True

    for forged in (
        replace(launch, sandboxed=False),
        replace(launch, sandbox_preference=SandboxablePreference.FORBID),
        replace(launch, sandbox_system_surface="external-runtime-discovery"),
    ):
        with pytest.raises(NativeRuntimeError) as denied:
            validate_process_launch(forged)
        assert denied.value.code is RuntimeErrorCode.SANDBOX_DENIED

    signed = finalize_process_launch(
        launch,
        argv=(sys.executable, "-V"),
        cwd=tmp_path,
        environment={},
    )
    forged_signed = replace(
        signed,
        snapshot=replace(signed.snapshot, sandboxed=False),
    )
    with pytest.raises(AuthorizationSnapshotError):
        verify_authorization_snapshot(forged_signed)


def test_auto_resolves_once_to_immutable_final_choice(tmp_path: Path) -> None:
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
        sandbox_preference=SandboxablePreference.AUTO,
    )

    assert launch.sandbox_preference is SandboxablePreference.AUTO
    assert launch.sandboxed is False
    with pytest.raises(FrozenInstanceError):
        launch.sandboxed = True  # type: ignore[misc]


def test_model_process_compiler_is_host_fixed_to_require(tmp_path: Path) -> None:
    launch = compile_process_launch(
        _context(tmp_path),
        ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )

    assert launch.profile.kind is PermissionProfileKind.MANAGED
    assert launch.sandbox_preference is SandboxablePreference.REQUIRE
    assert launch.sandboxed is True


def test_external_runtime_probe_uses_registered_forbid_surface(
    tmp_path: Path,
) -> None:
    from crew.agent.external.process_lifecycle import _trusted_probe_launch

    launch = _trusted_probe_launch((sys.executable, "-V"), tmp_path)

    assert launch.sandbox_preference is SandboxablePreference.FORBID
    assert launch.sandboxed is False
    assert launch.sandbox_system_surface == "external-runtime-discovery"
    validate_process_launch(launch)


def test_process_lifecycle_audit_carries_forbid_surface() -> None:
    recorded: list[dict] = []
    registry = ProcessRegistry()
    registry._audit_recorder = recorded.append
    session = ProcessSession(
        id="process-a",
        command="[redacted]",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        task_id="task-a",
        sandbox_preference="forbid",
        sandboxed=False,
        sandbox_system_surface="external-runtime-discovery",
    )

    registry._audit_lifecycle(
        "process_spawn_checkpointed",
        session,
        decision="allow",
    )

    assert recorded == [
        {
            "decision": "allow",
            "event_type": "process_spawn_checkpointed",
            "owner_account_id": "owner-a",
            "pid": 0,
            "reason": "",
            "sandbox_preference": "forbid",
            "sandbox_system_surface": "external-runtime-discovery",
            "sandboxed": False,
            "session_id": "process-a",
            "session_key": "session-a",
            "task_id": "task-a",
            "workspace_id": "workspace-a",
        }
    ]
