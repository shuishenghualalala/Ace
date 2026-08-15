from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action, normalize_file_action, serialize_normalized_action
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.tools.redact import argv_contains_sensitive_value


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


@pytest.mark.parametrize(
    "argv,secret",
    [
        (("tool", "--api-key", "plain-option-secret"), "plain-option-secret"),
        (
            ("tool", "https://user:url-secret@example.test/path"),
            "url-secret",
        ),
        (
            ("tool", "https://example.test/path?access_token=query-secret"),
            "query-secret",
        ),
        (
            ("tool", "https://example.test/path#access_token=fragment-secret"),
            "fragment-secret",
        ),
        (
            (
                "powershell",
                "-Command",
                'curl -H "Authorization: Bearer header-secret" https://example.test',
            ),
            "header-secret",
        ),
    ],
)
def test_process_launch_rejects_credential_bearing_argv(
    tmp_path: Path,
    argv: tuple[str, ...],
    secret: str,
) -> None:
    from crew.security import launch as launch_module
    from crew.security.runtime_client import NativeRuntimeError

    launch = launch_module.issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )

    with pytest.raises(NativeRuntimeError) as denied:
        launch_module.finalize_process_launch(
            launch,
            argv=argv,
            cwd=tmp_path,
            environment={},
        )

    assert "credential-bearing argv is forbidden" in str(denied.value)
    assert secret not in str(denied.value)


def test_argv_secret_detector_does_not_block_noncredential_metrics() -> None:
    assert not argv_contains_sensitive_value(("tool", "--token-count", "100"))


@pytest.mark.parametrize(
    "environment,secret",
    [
        ({"SERVICE_TOKEN": "plain-environment-secret"}, "plain-environment-secret"),
        (
            {"ENDPOINT": "https://example.test/path?access_token=query-secret"},
            "query-secret",
        ),
    ],
)
def test_process_launch_rejects_credential_bearing_environment(
    tmp_path: Path,
    environment: dict[str, str],
    secret: str,
) -> None:
    from crew.security import launch as launch_module
    from crew.security.runtime_client import NativeRuntimeError

    launch = launch_module.issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )

    with pytest.raises(NativeRuntimeError) as denied:
        launch_module.finalize_process_launch(
            launch,
            argv=("tool",),
            cwd=tmp_path,
            environment=environment,
        )

    assert "credential-bearing environment is forbidden" in str(denied.value)
    assert secret not in str(denied.value)


def test_process_launch_binds_exact_declared_credential_environment(
    tmp_path: Path,
) -> None:
    from crew.security import launch as launch_module

    launch = launch_module.issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )
    secret = "bound-environment-secret"
    signed = launch_module.finalize_process_launch(
        launch,
        argv=("tool",),
        cwd=tmp_path,
        environment={"SERVICE_TOKEN": secret},
        credential_environment_names=frozenset({"SERVICE_TOKEN"}),
    )

    assert signed.snapshot.owner_account_id == "owner-a"
    assert signed.snapshot.session_id == "session-a"
    assert signed.snapshot.task_id == "task-a"
    assert secret not in repr(signed)


def test_process_launch_rejects_inexact_credential_environment_declaration(
    tmp_path: Path,
) -> None:
    from crew.security import launch as launch_module
    from crew.security.runtime_client import NativeRuntimeError

    launch = launch_module.issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )
    with pytest.raises(NativeRuntimeError, match="credential-bearing environment"):
        launch_module.finalize_process_launch(
            launch,
            argv=("tool",),
            cwd=tmp_path,
            environment={
                "SERVICE_TOKEN": "undeclared-environment-secret",
                "SAFE_VALUE": "public",
            },
            credential_environment_names=frozenset({"SAFE_VALUE"}),
        )


def test_authorization_snapshot_is_deterministic_immutable_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.security import snapshot as snapshot_module

    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"trusted helper")
    readable = tmp_path / "readable"
    writable = tmp_path / "writable"
    denied = tmp_path / "denied"
    for path in (readable, writable):
        path.mkdir()
    action = normalize_exec_action(("python", "-V"), tmp_path)
    profile = PermissionProfile(
        PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(writable, FilesystemAccess.READ_WRITE),
            FilesystemEntry(denied, FilesystemAccess.DENY, escalatable=False),
        ),
        network_entries=(NetworkEntry("example.com", 443, "https"),),
    )
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(readable, FilesystemAccess.READ),),
    )
    monkeypatch.setattr(snapshot_module, "_host_signing_key", lambda _purpose: b"k" * 32)
    nonces = iter(("ab" * 16, "cd" * 16))
    monkeypatch.setattr(snapshot_module.secrets, "token_hex", lambda _size: next(nonces))

    first = snapshot_module.issue_authorization_snapshot(
        context=_context(tmp_path),
        action=action,
        profile=profile,
        additional_permissions=additional,
        argv=action.argv,
        cwd=tmp_path,
        environment={"Z": "last", "A": "first"},
        helper_argv=(str(helper),),
    )
    second = snapshot_module.issue_authorization_snapshot(
        context=_context(tmp_path),
        action=action,
        profile=profile,
        additional_permissions=additional,
        argv=action.argv,
        cwd=tmp_path,
        environment={"A": "first", "Z": "last"},
        helper_argv=(str(helper),),
    )

    first_facts = first.snapshot.to_payload()
    second_facts = second.snapshot.to_payload()
    first_facts.pop("nonce")
    second_facts.pop("nonce")
    assert first_facts == second_facts
    assert first.digest != second.digest
    assert first.mac != second.mac
    assert first.snapshot.version == snapshot_module.AUTHORIZATION_SNAPSHOT_VERSION
    assert first.snapshot.action_digest == action.digest
    assert first.snapshot.argv == action.argv
    assert first.snapshot.cwd == str(tmp_path.resolve())
    assert first.snapshot.owner_account_id == "owner-a"
    assert first.snapshot.workspace_id == "workspace-a"
    assert first.snapshot.session_id == "session-a"
    assert first.snapshot.task_id == "task-a"
    assert first.snapshot.helper_path == str(helper.resolve())
    assert len(first.snapshot.helper_digest) == 64
    assert first.snapshot.writable_roots == (str(writable.resolve()),)
    assert first.snapshot.readable_roots == (str(readable.resolve()),)
    assert first.snapshot.denied_roots == (str(denied.resolve()),)
    assert first.snapshot.network_rules[0].host == "example.com"
    assert snapshot_module.verify_authorization_snapshot(
        first,
        environment={"A": "first", "Z": "last"},
        expected_owner_account_id="owner-a",
        expected_workspace_id="workspace-a",
        expected_session_id="session-a",
        expected_task_id="task-a",
    ) is first.snapshot
    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="workspace"):
        snapshot_module.verify_authorization_snapshot(
            first,
            expected_workspace_id="workspace-b",
        )
    with pytest.raises(FrozenInstanceError):
        first.snapshot.cwd = str(tmp_path / "other")  # type: ignore[misc]


def test_normalized_action_reparse_is_immutable_and_does_not_hidden_decode(tmp_path: Path) -> None:
    first = normalize_exec_action(("python", "-V"), tmp_path)
    reparse = normalize_exec_action(first.argv, first.cwd, raw_command=first.raw_command)

    assert first.digest == reparse.digest
    assert serialize_normalized_action(first) == serialize_normalized_action(reparse)
    with pytest.raises(FrozenInstanceError):
        first.argv = ()

    encoded = normalize_file_action(tmp_path / "dir%2Ffile.txt", "read")
    literal = normalize_file_action(tmp_path / "dir" / "file.txt", "read")
    double_encoded = normalize_file_action(tmp_path / "dir%252Ffile.txt", "read")
    assert encoded.digest != literal.digest
    assert encoded.digest != double_encoded.digest
    assert encoded.path.endswith("dir%2Ffile.txt")


@pytest.mark.parametrize("field", ["argv", "cwd", "environment", "helper", "roots", "profile"])
def test_authorization_snapshot_rejects_every_post_approval_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from crew.security import snapshot as snapshot_module

    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"trusted helper")
    root = tmp_path / "root"
    root.mkdir()
    context = _context(tmp_path)
    action = normalize_exec_action(("python", "-V"), tmp_path)
    profile = PermissionProfile(
        PermissionProfileKind.MANAGED,
        filesystem=(FilesystemEntry(root, FilesystemAccess.READ_WRITE),),
    )
    monkeypatch.setattr(snapshot_module, "_host_signing_key", lambda _purpose: b"k" * 32)
    signed = snapshot_module.issue_authorization_snapshot(
        context=context,
        action=action,
        profile=profile,
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={"SAFE": "1"},
        helper_argv=(str(helper),),
    )

    environment = {"SAFE": "1"}
    candidate = signed
    if field == "argv":
        candidate = replace(signed, snapshot=replace(signed.snapshot, argv=("python", "-c", "bad")))
    elif field == "cwd":
        candidate = replace(
            signed,
            snapshot=replace(signed.snapshot, cwd=str((tmp_path / "other").resolve())),
        )
    elif field == "environment":
        environment = {"SAFE": "tampered"}
    elif field == "helper":
        helper.write_bytes(b"replaced helper")
    elif field == "roots":
        candidate = replace(
            signed,
            snapshot=replace(
                signed.snapshot,
                writable_roots=(*signed.snapshot.writable_roots, str(tmp_path.parent)),
            ),
        )
    elif field == "profile":
        candidate = replace(
            signed,
            snapshot=replace(signed.snapshot, profile_kind=PermissionProfileKind.DISABLED.value),
        )

    with pytest.raises(snapshot_module.AuthorizationSnapshotError):
        snapshot_module.verify_authorization_snapshot(candidate, environment=environment)


def test_snapshot_payload_rejects_unknown_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.security import snapshot as snapshot_module

    context = _context(tmp_path)
    action = normalize_exec_action(("python", "-V"), tmp_path)
    monkeypatch.setattr(snapshot_module, "_host_signing_key", lambda _purpose: b"k" * 32)
    signed = snapshot_module.issue_authorization_snapshot(
        context=context,
        action=action,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(),
    )
    payload = signed.to_payload()
    payload["unexpected"] = True

    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="unknown"):
        snapshot_module.SignedAuthorizationSnapshot.from_payload(payload)


def test_snapshot_key_nonce_and_replay_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.security import snapshot as snapshot_module

    context = _context(tmp_path)
    action = normalize_exec_action(("python", "-V"), tmp_path)
    signed = snapshot_module.issue_authorization_snapshot(
        context=context,
        action=action,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(),
    )

    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="key"):
        snapshot_module.verify_authorization_snapshot(signed, verification_key=b"")

    assert snapshot_module.consume_authorization_snapshot(
        signed,
        expected_owner_account_id="owner-a",
        expected_workspace_id="workspace-a",
        expected_session_id="session-a",
        expected_task_id="task-a",
    ) is signed.snapshot
    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="replay"):
        snapshot_module.consume_authorization_snapshot(signed)

    monkeypatch.setattr(snapshot_module.secrets, "token_hex", lambda _size: "not-a-nonce")
    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="nonce"):
        snapshot_module.issue_authorization_snapshot(
            context=context,
            action=action,
            profile=PermissionProfile(PermissionProfileKind.DISABLED),
            additional_permissions=AdditionalPermissionProfile(),
            argv=action.argv,
            cwd=tmp_path,
            environment={},
            helper_argv=(),
        )


@pytest.mark.parametrize("state_failure", ["missing", "exhausted", "lock"])
def test_snapshot_replay_state_failures_are_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_failure: str,
) -> None:
    from crew.security import snapshot as snapshot_module

    context = _context(tmp_path)
    action = normalize_exec_action(("python", "-V"), tmp_path)
    signed = snapshot_module.issue_authorization_snapshot(
        context=context,
        action=action,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(),
    )

    if state_failure == "missing":
        monkeypatch.setattr(snapshot_module, "_CONSUMED_SNAPSHOT_NONCES", None)
    elif state_failure == "exhausted":
        monkeypatch.setattr(snapshot_module, "_CONSUMED_SNAPSHOT_NONCES", set())
        monkeypatch.setattr(snapshot_module, "_MAX_CONSUMED_SNAPSHOTS", 0)
    else:
        class BrokenLock:
            def __enter__(self):
                raise RuntimeError("lock unavailable")

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(snapshot_module, "_REPLAY_LOCK", BrokenLock())

    with pytest.raises(
        snapshot_module.AuthorizationSnapshotError,
        match="replay state",
    ):
        snapshot_module.consume_authorization_snapshot(signed)


def test_host_snapshot_key_state_failure_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.security import snapshot as snapshot_module

    monkeypatch.setattr(snapshot_module, "_HOST_AUTHORITY_SECRET", b"")
    action = normalize_exec_action(("python", "-V"), tmp_path)
    with pytest.raises(snapshot_module.AuthorizationSnapshotError, match="key"):
        snapshot_module.issue_authorization_snapshot(
            context=_context(tmp_path),
            action=action,
            profile=PermissionProfile(PermissionProfileKind.DISABLED),
            additional_permissions=AdditionalPermissionProfile(),
            argv=action.argv,
            cwd=tmp_path,
            environment={},
            helper_argv=(),
        )
