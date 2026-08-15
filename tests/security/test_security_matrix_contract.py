import hashlib
import json
from pathlib import Path


def test_real_runner_matrix_uses_current_authorization_snapshot_contract(
    tmp_path: Path,
) -> None:
    from crew.security.models import (
        FilesystemAccess,
        FilesystemEntry,
        PermissionProfile,
        PermissionProfileKind,
    )
    from crew.security.snapshot import verify_authorization_snapshot
    from tests.security.security_matrix import _authorized_request

    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"test runtime")
    runtime.with_name("runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime.name,
                "binary_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = (str((tmp_path / "tool").resolve()), "--version")
    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(
                root=workspace,
                access=FilesystemAccess.READ_WRITE,
            ),
        ),
    )

    request = _authorized_request(
        runtime=runtime,
        profile=profile,
        command=command,
        cwd=workspace,
        timeout_seconds=5,
    )

    assert request.authorization_snapshot is not None
    snapshot = verify_authorization_snapshot(
        request.authorization_snapshot,
        environment={},
        expected_owner_account_id="security-matrix-owner",
        expected_workspace_id="security-matrix-workspace",
        expected_session_id="security-matrix-session",
        expected_task_id="security-matrix-task",
    )
    assert snapshot.argv == command
    assert snapshot.cwd == str(workspace.resolve())
    assert snapshot.helper_path == str(runtime.resolve())
