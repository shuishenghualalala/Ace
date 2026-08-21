import asyncio
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.security.audit import SQLiteSecurityAudit
from crew.security.context import SecurityContext
from crew.security.launch import (
    HelperIntegrityError,
    ProcessLaunch,
    _initialized_windows_security_state_dir,
    compile_process_launch,
    current_process_launch,
    execute_captured,
    host_stream_launch_block_reason,
    issue_process_launch,
    minimal_inherited_environment,
    minimal_native_helper_environment,
    packaged_runtime_argv,
    packaged_runtime_candidates,
    runtime_platform_key,
    runtime_source_stale,
    shell_argv,
    trusted_helper_environment,
    use_process_launch,
    verify_helper_integrity,
)
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    PermissionProfile,
    PermissionProfileKind,
    SandboxPermissions,
)
from crew.security.runtime_client import (
    NativeRuntimeError,
    RuntimeCapabilities,
    RuntimeErrorCode,
)
from crew.tools.process_registry import _BACKGROUND_BRIDGE_LAUNCHER, ProcessRegistry


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


def _disabled(tmp_path: Path) -> ProcessLaunch:
    return issue_process_launch(
        _context(tmp_path),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )


def _managed(
    tmp_path: Path,
    *,
    approved_action=None,
) -> ProcessLaunch:
    runtime_skills = tmp_path / "runtime-skills"
    runtime_skills.mkdir(exist_ok=True)
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"test-runtime")
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
    return issue_process_launch(
        _context(tmp_path),
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        helper_argv=(str(runtime),),
        trusted_readable_roots=(runtime_skills,),
        approved_action=approved_action,
    )


def _bin_runtime_manifest_unresolved() -> bool:
    """True while the committed runtime manifest merge is deferred to L5."""
    manifest = (
        Path(__file__).resolve().parents[2]
        / "security-runtime"
        / "bin"
        / "runtime-manifest.json"
    )
    return not manifest.is_file() or "<<<<<<<" in manifest.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_approved_outside_write_reaches_native_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    if _bin_runtime_manifest_unresolved():
        pytest.skip("committed runtime manifest merge is deferred to L5")
    target = outside / "approved.txt"
    context = SecurityContext(
        os_user="tester",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=workspace,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=workspace,
    )
    if os.name == "nt":
        escaped = str(target).replace("'", "''")
        command = f"Set-Content -LiteralPath '{escaped}' -Value approved -NoNewline"
    else:
        command = f"printf approved > {shlex.quote(str(target))}"

    denied_launch = compile_process_launch(
        context,
        ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
    )
    with use_process_launch(denied_launch):
        denied = await execute_captured(shell_argv(command), cwd=workspace, timeout=10)
    assert denied.returncode != 0
    assert not target.exists()

    permissions = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    approved_launch = compile_process_launch(
        context,
        ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
        additional_permissions=permissions,
    )
    with use_process_launch(approved_launch):
        approved = await execute_captured(shell_argv(command), cwd=workspace, timeout=10)
    assert approved.returncode == 0
    assert target.read_text(encoding="utf-8") == "approved"


@pytest.mark.asyncio
async def test_default_workspace_inside_runtime_home_remains_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_home = tmp_path / "runtime-home"
    workspace = runtime_home / "accounts" / "owner" / "task_workspaces" / "default"
    workspace.mkdir(parents=True)
    if _bin_runtime_manifest_unresolved():
        pytest.skip("committed runtime manifest merge is deferred to L5")
    monkeypatch.setenv("CREW_HOME", str(runtime_home))
    context = SecurityContext(
        os_user="tester",
        owner_account_id="owner",
        workspace_id="default",
        workspace_root=workspace,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=workspace,
    )
    target = workspace / "result.txt"
    if os.name == "nt":
        escaped = str(target).replace("'", "''")
        command = f"Set-Content -LiteralPath '{escaped}' -Value ok -NoNewline"
    else:
        command = f"printf ok > {shlex.quote(str(target))}"
    launch = compile_process_launch(
        context,
        ConversationPermissionMode.AUTO_REVIEW,
        db_path=runtime_home / "crew.db",
    )

    with use_process_launch(launch):
        result = await execute_captured(shell_argv(command), cwd=workspace, timeout=10)

    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_captured_execution_records_native_result_fields(tmp_path, monkeypatch):
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    context = SecurityContext(
        os_user="tester",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )
    base = _managed(tmp_path)
    launch = issue_process_launch(
        context,
        base.profile,
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        security_context=context,
        audit=audit,
    )

    async def managed_execute(self, request, **kwargs):
        return SimpleNamespace(
            exit_code=7,
            stdout="",
            stderr="failed",
            capabilities=RuntimeCapabilities(
                backend="macos_seatbelt",
                filesystem_sandbox=True,
                process_tree_cleanup=True,
                managed_network=False,
            ),
        )

    monkeypatch.setattr("crew.security.broker.SecurityExecutionBroker.execute", managed_execute)
    token = current_process_launch.set(launch)
    try:
        result = await execute_captured(
            ("tool", "--check"), cwd=tmp_path, timeout=1, tool_name="contract_test"
        )
    finally:
        current_process_launch.reset(token)
    records = audit.query(owner_account_id="owner")
    audit.close()

    assert result.returncode == 7
    assert len(records) == 1
    record = records[0]
    assert record.action_type == "exec_result"
    assert record.sandbox_backend == "macos_seatbelt"
    assert record.capabilities == ("filesystem_sandbox", "process_tree_cleanup")
    assert record.exit_code == 7
    assert record.stable_error_code == ""
    assert record.tool_name == "contract_test"


def test_host_stream_launch_policy_never_falls_back_from_managed_to_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ACE_STRICT_SECURITY", raising=False)
    token = current_process_launch.set(None)
    try:
        assert "missing" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_managed(tmp_path))
        assert "managed" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_disabled(tmp_path))
        assert host_stream_launch_block_reason() is None
        monkeypatch.setenv("ACE_STRICT_SECURITY", "0")
        current_process_launch.set(None)
        assert "missing" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_managed(tmp_path))
        assert "managed" in (host_stream_launch_block_reason() or "")
        current_process_launch.set(_disabled(tmp_path))
        assert host_stream_launch_block_reason() is None
    finally:
        current_process_launch.reset(token)


def test_packaged_runtime_ignores_environment_path_override(monkeypatch, tmp_path):
    attacker_runtime = tmp_path / "attacker" / "ace-security-runtime"
    attacker_runtime.parent.mkdir()
    attacker_runtime.write_bytes(b"attacker")
    trusted_runtime = tmp_path / "installed" / "ace-security-runtime"
    trusted_runtime.parent.mkdir()
    trusted_runtime.write_bytes(b"trusted")
    monkeypatch.setenv("ACE_SECURITY_RUNTIME", str(attacker_runtime))
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_candidates",
        lambda _root, _name: (trusted_runtime,),
    )

    assert packaged_runtime_argv() == (str(trusted_runtime.resolve()),)


def test_external_stream_policy_can_be_explicitly_disabled_without_disabling_builtins(
    tmp_path: Path,
) -> None:
    base = _managed(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        base.profile,
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        external_security_enabled=False,
    )
    token = current_process_launch.set(launch)
    try:
        assert "managed" in (host_stream_launch_block_reason() or "")
        assert host_stream_launch_block_reason(external=True) is None
    finally:
        current_process_launch.reset(token)


def test_process_launch_defaults_external_security_to_disabled(tmp_path: Path) -> None:
    launch = _managed(tmp_path)

    assert launch.external_security_enabled is False
    assert launch.external_managed is False


def test_compile_process_launch_uses_explicit_external_security_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from crew.security.context import SecurityContext
    from crew.security.models import ConversationPermissionMode

    context = SecurityContext(
        os_user="test-user",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_id="session-a",
        task_id="task-a",
        request_id="request-a",
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(
        "crew.security.launch._protected_entries",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "crew.security.launch.packaged_runtime_argv",
        lambda: ("ace-security-runtime",),
    )
    monkeypatch.setattr(
        "crew.agent.skills.get_builtin_skills_dir",
        lambda: tmp_path / "missing-skills",
    )
    monkeypatch.setattr("crew.state.home.bundled_runtime_roots", lambda: [])

    launch = compile_process_launch(
        context,
        ConversationPermissionMode.FULL_ACCESS,
        db_path=tmp_path / "security.db",
        external_security_enabled=False,
    )

    assert launch.managed is True
    assert launch.external_security_enabled is False
    assert launch.external_managed is False


def test_require_escalated_uses_host_boundary_after_explicit_approval(tmp_path: Path) -> None:
    context = SecurityContext(
        os_user="test-user",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_id="session-a",
        task_id="task-a",
        request_id="request-a",
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    launch = compile_process_launch(
        context,
        ConversationPermissionMode.REQUEST_APPROVAL,
        db_path=tmp_path / "security.db",
        additional_permissions=AdditionalPermissionProfile(
            sandbox_permissions=SandboxPermissions.REQUIRE_ESCALATED
        ),
    )
    assert launch.managed is False
    assert (
        launch.additional_permissions.sandbox_permissions
        is SandboxPermissions.REQUIRE_ESCALATED
    )


@pytest.mark.asyncio
async def test_external_captured_execution_uses_host_only_when_switch_is_off(tmp_path, monkeypatch):
    base = _managed(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        base.profile,
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        external_security_enabled=False,
    )
    monkeypatch.setattr(
        "crew.security.broker.SecurityExecutionBroker.execute",
        lambda *args, **kwargs: pytest.fail("disabled external security must not use native runtime"),
    )
    token = current_process_launch.set(launch)
    try:
        result = await execute_captured(
            (sys.executable, "-c", "print('external-ok')"),
            cwd=tmp_path,
            timeout=3,
            external=True,
        )
    finally:
        current_process_launch.reset(token)
    assert result.stdout.strip() == "external-ok"


def test_bundled_bwrap_authority_comes_from_runtime_manifest(
    monkeypatch,
    tmp_path,
):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    bwrap = tmp_path / "bwrap"
    bwrap.write_bytes(b"trusted bwrap")
    digest = hashlib.sha256(bwrap.read_bytes()).hexdigest()
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime.name,
                "binary_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                "files": [{"name": "bwrap", "sha256": digest}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ACE_BUNDLED_BWRAP", "/attacker/bwrap")
    monkeypatch.setenv("ACE_BUNDLED_BWRAP_SHA256", "0" * 64)

    assert trusted_helper_environment(runtime) == {
        "ACE_BUNDLED_BWRAP": str(bwrap),
        "ACE_BUNDLED_BWRAP_SHA256": digest,
    }

    bwrap.write_bytes(b"attacker replacement")
    with pytest.raises(HelperIntegrityError, match="does not match"):
        trusted_helper_environment(runtime)


@pytest.mark.parametrize(
    ("system_name", "machine_name", "expected"),
    [
        ("darwin", "arm64", "darwin-arm64"),
        ("macos", "aarch64", "darwin-arm64"),
        ("linux", "x86_64", "linux-x64"),
        ("windows", "AMD64", "win32-x64"),
    ],
)
def test_runtime_platform_key_is_stable(system_name, machine_name, expected):
    assert runtime_platform_key(system_name, machine_name) == expected


def test_packaged_runtime_candidates_include_host_specific_prebuilt(tmp_path):
    candidates = packaged_runtime_candidates(tmp_path, "ace-security-runtime")

    assert candidates[0] == tmp_path / "desktop" / "security-runtime-bin" / "ace-security-runtime"
    assert any(path.parent.parent.name == "prebuilt" for path in candidates)
    assert all(path.parent.name != "bin" for path in candidates)


def test_helper_integrity_rejects_another_platform(tmp_path):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    current = runtime_platform_key()
    wrong_platform = "win32" if not current or not current.startswith("win32-") else "darwin"
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "platform": wrong_platform,
                "arch": "arm64",
                "binary_name": runtime.name,
                "binary_sha256": digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HelperIntegrityError, match="different platform"):
        verify_helper_integrity(runtime)


def test_helper_integrity_rejects_missing_manifest(tmp_path):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")

    with pytest.raises(HelperIntegrityError, match="manifest is missing"):
        verify_helper_integrity(runtime)


def test_production_helper_rejects_system_temp_directory(tmp_path, monkeypatch):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    monkeypatch.setattr(
        "crew.security.launch._runtime_requires_hardened_directory",
        lambda _path: True,
    )
    monkeypatch.setattr("crew.security.launch.tempfile.gettempdir", lambda: str(tmp_path))

    with pytest.raises(HelperIntegrityError, match="temp directory"):
        verify_helper_integrity(runtime)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are unavailable on Windows")
def test_production_helper_rejects_non_private_directory(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime = runtime_dir / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
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
    runtime_dir.chmod(0o755)
    monkeypatch.setattr(
        "crew.security.launch._runtime_requires_hardened_directory",
        lambda _path: True,
    )

    with pytest.raises(HelperIntegrityError, match="owner-only"):
        verify_helper_integrity(runtime)


def test_packaged_desktop_binding_rejects_replaced_runtime_and_manifest(tmp_path, monkeypatch):
    runtime_name = "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
    runtime = tmp_path / runtime_name
    runtime.write_bytes(b"trusted-runtime")
    bwrap = tmp_path / "bwrap"
    bwrap.write_bytes(b"trusted-bwrap")
    runtime_digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    bwrap_digest = hashlib.sha256(bwrap.read_bytes()).hexdigest()
    manifest = tmp_path / "runtime-manifest.json"
    manifest_bytes = json.dumps(
        {
            "schema": 2,
            "binary_name": runtime.name,
            "binary_sha256": runtime_digest,
            "files": [
                {"name": runtime.name, "sha256": runtime_digest},
                {"name": "bwrap", "sha256": bwrap_digest},
            ],
        },
        sort_keys=True,
    ).encode()
    manifest.write_bytes(manifest_bytes)

    monkeypatch.setenv("ACE_SECURITY_RELEASE_MODE", "1")
    monkeypatch.setenv("ACE_DESKTOP_SECURITY_RUNTIME", str(runtime.resolve()))
    monkeypatch.setenv("ACE_DESKTOP_SECURITY_RUNTIME_SHA256", runtime_digest)
    monkeypatch.setenv(
        "ACE_DESKTOP_SECURITY_RUNTIME_MANIFEST_SHA256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    monkeypatch.setenv("ACE_DESKTOP_BUNDLED_BWRAP_SHA256", bwrap_digest)
    monkeypatch.setattr(
        "crew.security.launch._runtime_requires_hardened_directory",
        lambda _path: False,
    )

    assert packaged_runtime_argv() == (str(runtime.resolve()),)
    verify_helper_integrity(runtime)
    assert trusted_helper_environment(runtime) == {
        "ACE_BUNDLED_BWRAP": str(bwrap),
        "ACE_BUNDLED_BWRAP_SHA256": bwrap_digest,
    }

    attacker_digest = hashlib.sha256(b"attacker-runtime").hexdigest()
    runtime.write_bytes(b"attacker-runtime")
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime.name,
                "binary_sha256": attacker_digest,
                "files": [{"name": runtime.name, "sha256": attacker_digest}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HelperIntegrityError, match="Desktop trust root"):
        verify_helper_integrity(runtime)


@pytest.mark.skipif(os.name != "nt", reason="committed Windows runtime artifact")
def test_committed_windows_runtime_artifact_matches_source():
    root = Path(__file__).resolve().parents[2]
    helper = root / "security-runtime" / "bin" / "ace-security-runtime.exe"
    if _bin_runtime_manifest_unresolved():
        pytest.skip("committed runtime manifest merge is deferred to L5")

    assert runtime_source_stale(helper) is False
    verify_helper_integrity(helper)


def test_windows_security_state_default_only_accepts_initialized_crew_desktop(tmp_path):
    old_dir = tmp_path / "AppData" / "Roaming" / "crew-desktop" / "security"
    new_dir = tmp_path / "AppData" / "Roaming" / "crew-desktop-ui" / "security"
    old_dir.mkdir(parents=True)
    old_marker = old_dir / "windows-sandbox-identity.json"
    old_marker.write_text("old", encoding="utf-8")
    new_dir.mkdir(parents=True)
    marker = new_dir / "windows-sandbox-identity.json"
    marker.write_text("new", encoding="utf-8")
    same_mtime_ns = marker.stat().st_mtime_ns
    os.utime(old_marker, ns=(same_mtime_ns, same_mtime_ns))
    os.utime(marker, ns=(same_mtime_ns, same_mtime_ns))
    os.utime(old_dir, ns=(same_mtime_ns, same_mtime_ns))
    os.utime(new_dir, ns=(same_mtime_ns, same_mtime_ns))

    assert _initialized_windows_security_state_dir(tmp_path) == new_dir

    marker.unlink()
    assert _initialized_windows_security_state_dir(tmp_path) == old_dir

    old_marker.unlink()
    assert _initialized_windows_security_state_dir(tmp_path) is None


def test_native_helper_environment_carries_security_state_dir(monkeypatch):
    monkeypatch.setenv("ACE_SECURITY_STATE_DIR", r"C:\ProgramData\does-not-exist")

    assert minimal_native_helper_environment()[
        "ACE_SECURITY_STATE_DIR"
    ] == r"C:\ProgramData\does-not-exist"


def test_runtime_source_stale_uses_manifest_next_to_selected_helper(tmp_path):
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"runtime")
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime.name,
                "binary_sha256": "not-used-by-source-check",
            }
        ),
        encoding="utf-8",
    )

    # Desktop staging manifests intentionally omit source_hash because the source tree
    # is not shipped with the application; they must not be reported as stale.
    assert runtime_source_stale(runtime) is None


def test_helper_integrity_rejects_source_stale_manifest(tmp_path, monkeypatch):
    runtime_name = (
        "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
    )
    runtime = tmp_path / runtime_name
    runtime.write_bytes(b"runtime")
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": runtime_name,
                "binary_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                "source_hash": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "crew.security.launch._runtime_requires_hardened_directory",
        lambda _path: False,
    )

    with pytest.raises(HelperIntegrityError, match="source is stale"):
        verify_helper_integrity(runtime)


def test_managed_background_command_is_protocol_data_not_host_argv(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    monkeypatch.setattr(
        registry,
        "spawn_local",
        lambda *args, **kwargs: pytest.fail("managed execution used host spawn_local"),
    )

    result = registry.spawn_security("echo secret", launch=_managed(tmp_path), cwd=str(tmp_path))

    assert result == "session"
    payload = captured["payload"]
    assert set(payload) == {
        "version",
        "snapshot",
        "snapshot_digest",
        "snapshot_mac",
        "snapshot_nonce",
        "env_overrides",
        "timeout",
        "max_output_bytes",
    }
    assert payload["snapshot"]["argv"][-1].endswith("echo secret")
    assert payload["snapshot"]["helper_path"] == str(tmp_path / "ace-security-runtime")
    assert payload["snapshot"]["readable_roots"] == [str(tmp_path / "runtime-skills")]
    assert payload["snapshot_nonce"] == payload["snapshot"]["nonce"]


def test_managed_background_keeps_base_read_only_carve_out(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}
    protected = tmp_path / ".git"
    base = _managed(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(
                *base.profile.filesystem,
                FilesystemEntry(protected, FilesystemAccess.READ, escalatable=True),
            ),
        ),
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
    )

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    registry.spawn_security("git status", launch=launch, cwd=str(tmp_path))

    # The merged bridge carries escalation-eligible READ entries as snapshot
    # readable roots; protected metadata stays enforced by each runtime.
    assert captured["payload"]["snapshot"]["readable_roots"] == [
        str(protected.resolve()),
        str((tmp_path / "runtime-skills").resolve()),
    ]


def test_approved_metadata_write_removes_read_only_carve_out(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}
    protected = tmp_path / ".git"
    protected.mkdir()
    base = _managed(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(
                *base.profile.filesystem,
                FilesystemEntry(protected, FilesystemAccess.READ, escalatable=True),
            ),
        ),
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(protected, FilesystemAccess.READ_WRITE),)
        ),
    )

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    registry.spawn_security("git commit", launch=launch, cwd=str(tmp_path))

    # The approved READ_WRITE entry wins the escalation; the snapshot keeps the
    # readable twin, which execute_authorized drops as read-inside-writable.
    assert captured["payload"]["snapshot"]["writable_roots"] == [
        str(tmp_path.resolve()),
        str(protected.resolve()),
    ]


def test_managed_background_preserves_parent_deny_around_workspace(tmp_path, monkeypatch):
    """The signed snapshot keeps the ancestor deny; the bridge does not carve.

    Foreground execution may resolve workspace-in-denied-parent carve-outs at
    policy level, but the background snapshot protocol stays fail-closed and
    forwards the deny unchanged.
    """
    registry = ProcessRegistry()
    captured = {}
    runtime_home = tmp_path / "runtime-home"
    workspace = runtime_home / "task-workspaces" / "default"
    workspace.mkdir(parents=True)
    runtime = tmp_path / "ace-security-runtime"
    runtime.write_bytes(b"test-runtime")
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
    launch = issue_process_launch(
        _context(tmp_path),
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(
                FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),
                FilesystemEntry(runtime_home, FilesystemAccess.DENY, escalatable=False),
            ),
        ),
        helper_argv=(str(runtime),),
    )

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    registry.spawn_security("echo ok", launch=launch, cwd=str(workspace))

    assert captured["payload"]["snapshot"]["writable_roots"] == [str(workspace.resolve())]
    assert captured["payload"]["snapshot"]["denied_roots"] == [
        str(runtime_home.resolve())
    ]


def test_managed_background_forwards_approved_permission_overlay(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}
    outside = tmp_path / "outside"
    outside.mkdir()
    base = _managed(tmp_path)
    launch = issue_process_launch(
        _context(tmp_path),
        base.profile,
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),),
            network=(NetworkEntry("uploads.example.com", 443, "https"),),
            allow_local_binding=True,
        ),
    )

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    registry.spawn_security("upload artifact", launch=launch, cwd=str(tmp_path))

    snapshot = captured["payload"]["snapshot"]
    assert str(outside.resolve()) in snapshot["writable_roots"]
    assert snapshot["network_rules"] == [{
        "host": "uploads.example.com",
        "port": 443,
        "protocol": "https",
        "allow": True,
        "allow_private": False,
        "escalatable": True,
    }]
    assert snapshot["allow_local_binding"] is True


def test_managed_bridge_rejects_a_replayed_snapshot_before_popen(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as process_registry_module
    from crew.security.launch import finalize_process_launch
    from crew.security.snapshot import consume_authorization_snapshot

    launch = _managed(tmp_path)
    environment = {"SAFE": "1"}
    signed = finalize_process_launch(
        launch,
        argv=("python", "-V"),
        cwd=tmp_path,
        environment=environment,
    )
    consume_authorization_snapshot(signed, environment=environment)
    payload = {
        "version": 2,
        **signed.to_payload(),
        "snapshot_nonce": signed.snapshot.nonce,
        "env_overrides": environment,
        "timeout": 30,
        "max_output_bytes": 1024,
    }
    registry = ProcessRegistry()
    monkeypatch.setattr(
        process_registry_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("replayed snapshot reached bridge Popen"),
    )

    with pytest.raises(NativeRuntimeError) as caught:
        registry._spawn_managed_bridge(
            "display only",
            payload,
            launch=launch,
            cwd=str(tmp_path),
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            session_key="session-a",
            task_id="task-a",
            authorization_snapshot=signed,
        )

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


def test_managed_bridge_passes_snapshot_key_through_stdin_not_environment(
    tmp_path,
    monkeypatch,
):
    import json

    import crew.tools.process_registry as process_registry_module
    from crew.security import background_runner
    from crew.security.launch import finalize_process_launch

    environment = {"SAFE": "1"}
    launch = _managed(tmp_path)
    signed = finalize_process_launch(
        launch,
        argv=("python", "-V"),
        cwd=tmp_path,
        environment=environment,
    )
    payload = {
        "version": 2,
        **signed.to_payload(),
        "snapshot_nonce": signed.snapshot.nonce,
        "env_overrides": environment,
        "timeout": 30,
        "max_output_bytes": 1024,
    }
    captured: dict[str, object] = {}

    class CaptureInput:
        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> None:
            self.value += value

        def close(self) -> None:
            return None

    class FakeProcess:
        pid = 321
        stdin = CaptureInput()
        stdout = None

        def poll(self):
            return None

    class NoopThread:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self):
            return None

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    identity = process_registry_module.ProcessIdentity(
        create_time=1.0,
        executable=sys.executable,
        executable_digest="a" * 64,
        os_owner="host-user",
    )
    registry = ProcessRegistry()
    monkeypatch.setattr(process_registry_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process_registry_module, "_process_identity", lambda _pid: identity)
    monkeypatch.setattr(process_registry_module.threading, "Thread", NoopThread)

    def checkpoint_before_payload(**_kwargs):
        assert FakeProcess.stdin.value == ""
        captured["checkpoint_before_payload"] = True

    monkeypatch.setattr(registry, "_write_checkpoint", checkpoint_before_payload)

    registry._spawn_managed_bridge(
        "display only",
        payload,
        launch=launch,
        cwd=str(tmp_path),
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        task_id="task-a",
        authorization_snapshot=signed,
    )

    bootstrap = json.loads(FakeProcess.stdin.value)
    parsed_payload, key = background_runner.parse_bridge_bootstrap(
        bootstrap,
        actual_parent_pid=os.getpid(),
    )
    parsed = background_runner.parse_bridge_payload(
        parsed_payload,
        verification_key=key,
    )
    assert parsed.authorization.digest == signed.digest
    assert parsed.environment == environment
    assert len(key) == 32
    assert parsed_payload["snapshot_mac"] != payload["snapshot_mac"]
    assert "ACE_SECURITY_BRIDGE_AUTH_KEY" not in captured["kwargs"]["env"]
    assert captured["checkpoint_before_payload"] is True


def test_process_registry_requires_an_explicit_disabled_launch_for_host_execution(
    tmp_path,
    monkeypatch,
):
    registry = ProcessRegistry()
    host_calls = []

    def host_spawn(command, **kwargs):
        host_calls.append((command, kwargs))
        return "host-session"

    monkeypatch.setattr(registry, "spawn_local", host_spawn)
    disabled = _disabled(tmp_path)

    assert (
        registry.spawn_security("echo allowed", launch=disabled, cwd=str(tmp_path))
        == "host-session"
    )
    assert host_calls[0][1]["launch"] is disabled

    unknown = ProcessLaunch(PermissionProfile("unknown"))  # type: ignore[arg-type]
    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_security("echo must-not-run", launch=unknown, cwd=str(tmp_path))
    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED
    assert len(host_calls) == 1


def test_direct_host_spawn_without_disabled_launch_fails_before_popen(
    monkeypatch,
):
    registry = ProcessRegistry()
    monkeypatch.setattr(
        "crew.tools.process_registry.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("host Popen must not run without disabled launch"),
    )

    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_local("echo must-not-run")

    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


def test_forged_disabled_process_launch_cannot_reach_host_popen(
    tmp_path,
    monkeypatch,
):
    registry = ProcessRegistry()
    monkeypatch.setattr(
        "crew.tools.process_registry.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("forged ProcessLaunch reached host Popen"),
    )
    forged = ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))

    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_security("echo forged", launch=forged, cwd=str(tmp_path))

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


def test_host_issued_launch_binds_approved_argv_cwd_and_identity(
    tmp_path,
    monkeypatch,
):
    from crew.security.actions import normalize_exec_action
    from crew.security.context import SecurityContext
    from crew.security.launch import issue_process_launch

    action = normalize_exec_action(("trusted-shell", "-c", "echo approved"), tmp_path)
    context = SecurityContext(
        os_user="host-user",
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
    registry = ProcessRegistry()
    monkeypatch.setattr(
        "crew.tools.process_registry.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("mutated launch reached host Popen"),
    )

    for argv, cwd, owner, session, task in (
        (
            ("trusted-shell", "-c", "echo changed"),
            str(tmp_path),
            "owner-a",
            "session-a",
            "task-a",
        ),
        (action.argv, str(tmp_path / "other"), "owner-a", "session-a", "task-a"),
        (action.argv, str(tmp_path), "owner-b", "session-a", "task-a"),
        (action.argv, str(tmp_path), "owner-a", "session-b", "task-a"),
        (action.argv, str(tmp_path), "owner-a", "session-a", "task-b"),
    ):
        with pytest.raises(NativeRuntimeError) as caught:
            registry.spawn_security(
                "display text is not authority",
                launch=launch,
                launch_argv=argv,
                cwd=cwd,
                owner_account_id=owner,
                session_key=session,
                task_id=task,
            )
        assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


@pytest.mark.parametrize("failure", ["missing-helper", "bad-manifest", "integrity-import"])
def test_managed_background_runtime_failures_stop_before_any_host_bridge_or_shell(
    tmp_path,
    monkeypatch,
    failure,
):
    registry = ProcessRegistry()
    launch = _managed(tmp_path)
    helper = Path(launch.helper_argv[0])
    if failure == "missing-helper":
        helper.unlink()
    elif failure == "bad-manifest":
        helper.with_name("runtime-manifest.json").write_text("{broken", encoding="utf-8")
    else:
        monkeypatch.setattr(
            "crew.security.launch.verify_helper_integrity",
            lambda _path: (_ for _ in ()).throw(ImportError("integrity import failed")),
        )

    host_calls = []
    monkeypatch.setattr(
        registry,
        "spawn_local",
        lambda *args, **kwargs: host_calls.append(("local", args, kwargs)),
    )
    monkeypatch.setattr(
        registry,
        "_spawn_managed_bridge",
        lambda *args, **kwargs: host_calls.append(("bridge", args, kwargs)),
    )

    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_security("echo must-not-run", launch=launch, cwd=str(tmp_path))

    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert host_calls == []


def test_explicit_disabled_posix_host_path_uses_argv_without_shell_true(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as process_registry_module

    registry = ProcessRegistry()
    captured = {}

    class FakePopen:
        pid = 321

    class NoopThread:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self):
            return None

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(process_registry_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(process_registry_module.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(
        process_registry_module,
        "shell_argv",
        lambda command: ("/trusted/bash", "-lc", command),
    )
    monkeypatch.setattr(process_registry_module, "minimal_inherited_environment", dict)
    monkeypatch.setattr(process_registry_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        process_registry_module,
        "_process_identity",
        lambda _pid: process_registry_module.ProcessIdentity(
            create_time=1.0,
            executable="/trusted/bash",
            executable_digest="a" * 64,
            os_owner="host-user",
        ),
    )
    monkeypatch.setattr(process_registry_module.threading, "Thread", NoopThread)
    monkeypatch.setattr(registry, "_child_env", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(registry, "_write_checkpoint", lambda **_kwargs: None)

    disabled = _disabled(tmp_path)
    session = registry.spawn_security(
        "printf model-controlled",
        launch=disabled,
        cwd=str(tmp_path),
    )

    assert session.pid == 321
    assert tuple(captured["args"][0]) == (
        "/trusted/bash",
        "-lc",
        "printf model-controlled",
    )
    assert captured["kwargs"]["shell"] is False


def test_disabled_host_spawn_rejects_a_replayed_snapshot_before_popen(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as process_registry_module
    from crew.security.actions import normalize_exec_action
    from crew.security.models import AdditionalPermissionProfile
    from crew.security.snapshot import (
        consume_authorization_snapshot,
        issue_authorization_snapshot,
    )

    action = normalize_exec_action(("trusted-shell", "-c", "echo approved"), tmp_path)
    signed = issue_authorization_snapshot(
        context=_context(tmp_path),
        action=action,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(),
    )
    consume_authorization_snapshot(signed)
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_child_env", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(process_registry_module, "minimal_inherited_environment", dict)
    monkeypatch.setattr(
        process_registry_module,
        "finalize_process_launch",
        lambda *args, **kwargs: signed,
    )
    monkeypatch.setattr(
        process_registry_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("replayed snapshot reached host Popen"),
    )

    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_local(
            "display only",
            launch=_disabled(tmp_path),
            launch_argv=action.argv,
            cwd=str(tmp_path),
            owner_account_id="owner-a",
            session_key="session-a",
            task_id="task-a",
        )

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


def test_managed_launch_uses_the_argv_that_was_approved(tmp_path, monkeypatch):
    from crew.security.actions import normalize_exec_action

    registry = ProcessRegistry()
    captured = {}
    approved_argv = ("pwsh", "-NoProfile", "-Command", "Remove-Item", "outside.txt")
    approved_action = normalize_exec_action(approved_argv, tmp_path)

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    registry.spawn_security(
        "this command is deliberately different",
        launch=_managed(tmp_path, approved_action=approved_action),
        launch_argv=approved_argv,
        cwd=str(tmp_path),
    )
    assert captured["payload"]["snapshot"]["argv"] == [
        "pwsh",
        "-NoProfile",
        "-Command",
        "Remove-Item",
        "outside.txt",
    ]


def test_managed_bridge_removes_absolute_cwd_from_python_import_path() -> None:
    assert "os.path.abspath(os.getcwd())" in _BACKGROUND_BRIDGE_LAUNCHER
    assert "os.path.abspath(p) != _cwd" in _BACKGROUND_BRIDGE_LAUNCHER


def test_managed_background_receives_minimal_runtime_env(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    captured = {}

    monkeypatch.setattr(
        "crew.state.home.managed_runtime_env_overrides",
        lambda **kwargs: {"CREW_RUNTIME_HOME": str(tmp_path / "runtime"), "PYTHONUTF8": "1"},
    )

    def record(command, payload, **kwargs):
        captured.update(command=command, payload=payload, kwargs=kwargs)
        return "session"

    monkeypatch.setattr(registry, "_spawn_managed_bridge", record)
    result = registry.spawn_security(
        "python unified_search.py",
        launch=_managed(tmp_path),
        cwd=str(tmp_path),
        owner_account_id="owner-a",
        session_key="session-a",
    )

    assert result == "session"
    assert captured["payload"]["env_overrides"] == {
        "CREW_RUNTIME_HOME": str(tmp_path / "runtime"),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    assert captured["kwargs"]["secret_values"] == ()


@pytest.mark.asyncio
async def test_background_bridge_forwards_explicit_env_overrides(tmp_path, monkeypatch):
    from crew.security import background_runner
    from crew.security.launch import finalize_process_launch
    from crew.security.snapshot import _host_signing_key

    captured = {}
    environment = {"SAFE_MARKER": "bound-value"}
    signed = finalize_process_launch(
        _managed(tmp_path),
        argv=("python", "skill.py"),
        cwd=tmp_path,
        environment=environment,
    )
    payload = {
        "version": 2,
        **signed.to_payload(),
        "snapshot_nonce": signed.snapshot.nonce,
        "env_overrides": environment,
        "timeout": 30,
        "max_output_bytes": 1024,
    }

    class _Runtime:
        def __init__(self, helper_argv):
            captured["helper_argv"] = helper_argv

        async def execute_authorized(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                exit_code=0,
                stdout="",
                stderr="",
                capabilities=RuntimeCapabilities(
                    backend="macos_seatbelt",
                    filesystem_sandbox=True,
                    process_tree_cleanup=True,
                    managed_network=False,
                ),
            )

    monkeypatch.setattr(background_runner, "NativeRuntimeClient", _Runtime)
    parsed = background_runner.parse_bridge_payload(
        payload,
        verification_key=_host_signing_key("authorization-snapshot"),
    )
    exit_code = await background_runner._run(parsed)

    assert exit_code == 0
    assert captured["env_overrides"] == {"SAFE_MARKER": "bound-value"}
    assert captured["authorization"] is not None


@pytest.mark.asyncio
async def test_background_bridge_writes_audited_result_metadata(tmp_path, monkeypatch):
    from crew.security import background_runner
    from crew.security.launch import finalize_process_launch
    from crew.security.snapshot import _host_signing_key

    captured = {}
    environment = {"CREW_SEARCH_MARKER": "one-time-marker-9876"}
    result_path = tmp_path / "result.json"
    signed = finalize_process_launch(
        _managed(tmp_path),
        argv=("python", "skill.py"),
        cwd=tmp_path,
        environment=environment,
    )
    payload = {
        "version": 2,
        **signed.to_payload(),
        "snapshot_nonce": signed.snapshot.nonce,
        "env_overrides": environment,
        "result_path": str(result_path),
        "result_nonce": "a" * 32,
        "timeout": 30,
        "max_output_bytes": 1024,
    }

    class _Runtime:
        def __init__(self, _helper_argv):
            pass

        async def execute_authorized(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                exit_code=0,
                stdout="",
                stderr="",
                capabilities=RuntimeCapabilities(
                    backend="macos_seatbelt",
                    filesystem_sandbox=True,
                    process_tree_cleanup=True,
                    managed_network=False,
                ),
            )

    monkeypatch.setattr(background_runner, "NativeRuntimeClient", _Runtime)
    parsed = background_runner.parse_bridge_payload(
        payload,
        verification_key=_host_signing_key("authorization-snapshot"),
    )
    exit_code = await background_runner._run(parsed)

    assert exit_code == 0
    assert captured["env_overrides"] == {
        "CREW_SEARCH_MARKER": "one-time-marker-9876"
    }
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "nonce": "a" * 32,
        "sandbox_backend": "macos_seatbelt",
        "capabilities": ["filesystem_sandbox", "process_tree_cleanup"],
        "exit_code": 0,
    }


def test_background_process_result_is_audited(tmp_path):
    from crew.security.actions import normalize_exec_action
    from crew.tools.process_registry import ProcessSession

    audit = SQLiteSecurityAudit(tmp_path / "background-audit.db")
    context = SecurityContext(
        os_user="tester",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )
    base = _managed(tmp_path)
    launch = issue_process_launch(
        context,
        base.profile,
        helper_argv=base.helper_argv,
        trusted_readable_roots=base.trusted_readable_roots,
        security_context=context,
        audit=audit,
    )
    result_path = tmp_path / "native-result.json"
    result_path.write_text(
        json.dumps(
            {
                "nonce": "b" * 32,
                "sandbox_backend": "macos_seatbelt",
                "capabilities": ["filesystem_sandbox", "process_tree_cleanup"],
                "exit_code": 3,
            }
        ),
        encoding="utf-8",
    )
    session = ProcessSession(
        id="proc-test",
        command="false",
        exit_code=3,
        _security_launch=launch,
        _security_action=normalize_exec_action(("/usr/bin/false",), tmp_path),
        _security_result_path=result_path,
        _security_result_nonce="b" * 32,
    )

    ProcessRegistry._audit_process_result(session)
    records = audit.query(owner_account_id="owner")
    audit.close()

    assert not result_path.exists()
    assert len(records) == 1
    assert records[0].tool_name == "terminal"
    assert records[0].decision == "failed"
    assert records[0].sandbox_backend == "macos_seatbelt"
    assert records[0].capabilities == ("filesystem_sandbox", "process_tree_cleanup")
    assert records[0].exit_code == 3


@pytest.mark.asyncio
async def test_managed_captured_failure_never_falls_back_to_host(tmp_path, monkeypatch):
    launch = _managed(tmp_path)
    token = current_process_launch.set(launch)
    spawned_argv = None

    async def host_spawn(*args, **kwargs):
        nonlocal spawned_argv
        spawned_argv = args
        raise FileNotFoundError(args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await execute_captured(("ignored",), cwd=tmp_path, timeout=0.1)
    finally:
        current_process_launch.reset(token)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert spawned_argv == launch.helper_argv
    assert "ignored" not in spawned_argv


@pytest.mark.asyncio
async def test_managed_captured_passes_process_data_without_host_spawn(tmp_path, monkeypatch):
    launch = _managed(tmp_path)
    captured = {}
    started = [].append
    output = [].append

    async def managed_execute(self, request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    async def host_spawn(*args, **kwargs):
        raise AssertionError("managed execution must not use host spawn")

    monkeypatch.setattr(
        "crew.security.broker.SecurityExecutionBroker.execute",
        managed_execute,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    token = current_process_launch.set(launch)
    try:
        result = await execute_captured(
            ("ignored",),
            cwd=tmp_path,
            timeout=3.5,
            stdin=b"prompt",
            env_overrides={"SAFE_VALUE": "bound"},
            max_output_bytes=1234,
            on_started=started,
            on_output=output,
        )
    finally:
        current_process_launch.reset(token)

    assert result.stdout == "ok"
    assert captured["request"].stdin == b"prompt"
    assert captured["request"].env_overrides == {"SAFE_VALUE": "bound"}
    assert captured["request"].timeout_seconds == 3.5
    assert captured["request"].max_output_bytes == 1234
    snapshot = captured["request"].authorization_snapshot.snapshot
    assert snapshot.argv == ("ignored",)
    assert snapshot.readable_roots == (str(tmp_path / "runtime-skills"),)
    assert captured["kwargs"] == {"on_started": started, "on_output": output}


@pytest.mark.asyncio
async def test_captured_execution_without_launch_context_refuses_host(tmp_path, monkeypatch):
    """A missing launch contextvar (e.g. a non-inheriting task) must NOT host-exec.

    Regression guard for the fail-open defect: ``execute_captured`` used to fall back
    to ``create_subprocess_exec`` when the contextvar was unset, conflating "full
    access" with "lost security context". It must now fail closed.
    """
    token = current_process_launch.set(None)

    async def host_spawn(*args, **kwargs):
        raise AssertionError("host subprocess must not start without a launch decision")

    monkeypatch.setattr("asyncio.create_subprocess_exec", host_spawn)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await execute_captured(("echo", "secret"), cwd=tmp_path, timeout=0.1)
    finally:
        current_process_launch.reset(token)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_host_captured_rejects_a_replayed_snapshot_before_spawn(
    tmp_path,
    monkeypatch,
):
    from crew.security.actions import normalize_exec_action
    from crew.security.models import AdditionalPermissionProfile
    from crew.security.snapshot import (
        consume_authorization_snapshot,
        issue_authorization_snapshot,
    )

    action = normalize_exec_action(("trusted-shell", "-c", "echo approved"), tmp_path)
    signed = issue_authorization_snapshot(
        context=_context(tmp_path),
        action=action,
        profile=PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(),
    )
    consume_authorization_snapshot(signed)
    monkeypatch.setattr(
        "crew.security.launch.finalize_process_launch",
        lambda *args, **kwargs: signed,
    )
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        lambda *args, **kwargs: pytest.fail("replayed snapshot reached host spawn"),
    )
    token = current_process_launch.set(_disabled(tmp_path))
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await execute_captured(
                action.argv,
                cwd=tmp_path,
                timeout=1,
                env={},
            )
    finally:
        current_process_launch.reset(token)

    assert caught.value.code is RuntimeErrorCode.SANDBOX_DENIED


def test_managed_acp_has_a_native_transport_path() -> None:
    source = Path("crew/agent/external/acp_adapter.py").read_text(encoding="utf-8")
    assert "SecurityExecutionBroker" in source
    assert "open_interactive" in source
    assert "_NativeAcpTransport" in source
    assert "managed 模式拒绝宿主启动" not in source


def test_sensitive_env_values_and_precise_redaction() -> None:
    """H-3 unit: sensitive-key env values are extracted and exact-redacted (always-on)."""
    from crew.tools.redact import redact_secret_values, sensitive_env_values

    values = sensitive_env_values(
        {
            "DB_PASSWORD": "Hunter2-Blue-77!",
            "CREW_SEARCH_TICKET": "one-time-ticket-9876",
            "PATH": "/usr/bin",
            "API_KEY": "k",
            "USER": "bob",
            "AUTHOR": "Jane Doe",
            "AUTHORITY": "x",
        }
    )
    assert "Hunter2-Blue-77!" in values
    assert "one-time-ticket-9876" in values
    assert "/usr/bin" not in values  # PATH is not a secret key
    assert "k" not in values  # too short (<4)
    assert "Jane Doe" not in values  # AUTHOR must not be over-redacted (substring 'auth')
    assert "x" not in values  # AUTHORITY likewise
    assert redact_secret_values(f"echo {values[0]}", values) == "echo ***REDACTED***"
    # non-matching text passes through untouched
    assert redact_secret_values("plain text", values) == "plain text"


def test_private_output_truncation_never_exposes_a_partial_secret() -> None:
    secret = "ABCDEFGHIJ"
    # A rolling buffer may begin in the middle of a value, while a complete
    # occurrence may independently cross the nominal visible-output boundary.
    output = "DEFGHIJxx" + secret + " safe"

    result = ProcessRegistry._redact_private_output(
        output,
        (secret,),
        truncated=True,
        max_output_chars=16,
    )

    assert "DEFGHIJ" not in result
    assert secret not in result
    assert result.endswith(" safe")


@pytest.mark.asyncio
async def test_cua_run_command_requires_an_explicit_launch_context(tmp_path, monkeypatch):
    """CUA lifecycle commands must not become an unbrokered host-execution escape."""
    from crew.tools.cua_setup import _run_command

    token = current_process_launch.set(None)
    starts: list[tuple[object, ...]] = []

    async def fake_exec(*argv, **kwargs):
        del kwargs
        starts.append(argv)
        raise AssertionError("CUA command reached an unbrokered host process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    try:
        with pytest.raises(NativeRuntimeError) as caught:
            await _run_command(["cua-driver", "--version"], timeout=5)
    finally:
        current_process_launch.reset(token)
    assert caught.value.code is RuntimeErrorCode.SANDBOX_UNAVAILABLE
    assert starts == []


@pytest.mark.asyncio
async def test_captured_execution_rejects_sensitive_env_before_spawn(tmp_path, monkeypatch):
    secret = "Hunter2-Blue-77!"  # no shape the generic regex matches
    disabled_launch = _disabled(tmp_path)
    spawned = False

    async def fake_exec(*argv, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("credential-bearing environment reached spawn")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    token = current_process_launch.set(disabled_launch)
    try:
        with pytest.raises(NativeRuntimeError, match="credential-bearing environment"):
            await execute_captured(
                ("dbcli",),
                cwd=tmp_path,
                timeout=1.0,
                env={"DB_PASSWORD": secret, "PATH": "/bin"},
            )
    finally:
        current_process_launch.reset(token)
    assert spawned is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("stdin", "expected"), [(None, "0"), (b"prompt", "6")])
async def test_host_captured_uses_one_shot_stdin_and_reports_activity(tmp_path, stdin, expected):
    safe_value = "Split-Public-9876"
    script = (
        "import os,sys;"
        "data=sys.stdin.buffer.read();"
        "value=os.environ['SAFE_VALUE'];"
        "sys.stdout.write(str(len(data))+':'+value[:7]);sys.stdout.flush();"
        "sys.stdout.write(value[7:]);"
        "sys.stderr.write('notice')"
    )
    env = minimal_inherited_environment()
    env["SAFE_VALUE"] = safe_value
    started = []
    streams = []
    token = current_process_launch.set(_disabled(tmp_path))
    try:
        result = await execute_captured(
            (sys.executable, "-c", script),
            cwd=tmp_path,
            timeout=2,
            env=env,
            stdin=stdin,
            on_started=started.append,
            on_output=streams.append,
        )
    finally:
        current_process_launch.reset(token)

    assert result.returncode == 0
    assert result.stdout.startswith(f"{expected}:")
    assert safe_value in result.stdout
    assert result.stderr == "notice"
    assert len(started) == 1
    assert set(streams) == {"stdout", "stderr"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_host_captured_timeout_and_cancel_terminate_process_tree(
    tmp_path, monkeypatch, cancel
):
    from crew.security import process_lifecycle

    terminated = 0
    real_terminate = process_lifecycle.terminate_process_tree
    real_spawn = asyncio.create_subprocess_exec

    if cancel:
        first_spawn = True

        async def delayed_first_spawn(*args, **kwargs):
            nonlocal first_spawn
            if first_spawn:
                first_spawn = False
                await asyncio.sleep(0.1)
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(
            "crew.security.launch.asyncio.create_subprocess_exec",
            delayed_first_spawn,
        )

    async def record_terminate(process):
        nonlocal terminated
        terminated += 1
        await real_terminate(process)

    monkeypatch.setattr("crew.security.launch.terminate_process_tree", record_terminate)
    token = current_process_launch.set(_disabled(tmp_path))
    try:
        task = asyncio.create_task(
            execute_captured(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=0.05 if not cancel else 30,
            )
        )
        if cancel:
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(asyncio.TimeoutError):
                await task
    finally:
        current_process_launch.reset(token)

    assert terminated == 1
