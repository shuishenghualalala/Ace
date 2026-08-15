"""H-1 regression: the managed background bridge must not import ``crew`` from cwd.

The bridge launches as a host subprocess whose ``cwd`` is the task workspace. With a
naive ``python -m crew.security.background_runner`` the interpreter puts ``cwd`` on
``sys.path[0]``; a workspace-dropped ``crew/security/background_runner.py`` (and a
matching fake ``runtime_client``) would then execute on the host *before* the native
helper, the shell classifier, or the sandbox can intervene. The launcher rebuilds
``sys.path`` under ``-I`` so only the installed crew package root is trusted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


def test_background_bridge_ignores_workspace_fake_crew(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    fake_security = workspace / "crew" / "security"
    fake_security.mkdir(parents=True)
    (workspace / "crew" / "__init__.py").write_text("", encoding="utf-8")
    (fake_security / "__init__.py").write_text("", encoding="utf-8")
    # A harmless canary: if this module ever loads, it prints PWNED and exits 0.
    (fake_security / "background_runner.py").write_text(
        "print('PWNED', flush=True)\nraise SystemExit(0)\n",
        encoding="utf-8",
    )

    from crew.tools.process_registry import _BACKGROUND_BRIDGE_LAUNCHER

    # The real runner reads stdin; send an empty line so it returns cleanly (126)
    # without ever touching the workspace's fake package.
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _BACKGROUND_BRIDGE_LAUNCHER],
        cwd=workspace,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert "PWNED" not in proc.stdout, (
        f"managed bridge loaded a workspace-dropped fake crew package "
        f"(stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )


def _signed_bridge_payload(tmp_path: Path) -> tuple[dict, bytes]:
    from crew.security.actions import normalize_exec_action
    from crew.security.context import SecurityContext
    from crew.security.models import PermissionProfile, PermissionProfileKind
    from crew.security.snapshot import (
        delegate_authorization_snapshot,
        issue_authorization_snapshot,
    )

    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"runtime")
    action = normalize_exec_action(("python", "-V"), tmp_path)
    environment = {"SAFE": "1"}
    signed = issue_authorization_snapshot(
        context=SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        action=action,
        profile=PermissionProfile(PermissionProfileKind.MANAGED),
        additional_permissions=__import__(
            "crew.security.models",
            fromlist=["AdditionalPermissionProfile"],
        ).AdditionalPermissionProfile(),
        argv=action.argv,
        cwd=tmp_path,
        environment=environment,
        helper_argv=(str(helper),),
    )
    key = b"k" * 32
    delegated = delegate_authorization_snapshot(
        signed,
        verification_key=key,
    )
    return (
        {
            "version": 2,
            **delegated.to_payload(),
            "snapshot_nonce": signed.snapshot.nonce,
            "env_overrides": environment,
            "timeout": 30,
            "max_output_bytes": 1024,
        },
        key,
    )


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "nested_unknown", "nonce", "environment", "argv", "mac"],
)
def test_background_bridge_strictly_rejects_tampered_snapshot_payload(
    tmp_path: Path,
    mutation: str,
) -> None:
    from crew.security import background_runner
    from crew.security.snapshot import AuthorizationSnapshotError

    payload, key = _signed_bridge_payload(tmp_path)
    candidate = deepcopy(payload)
    if mutation == "unknown":
        candidate["unexpected"] = True
    elif mutation == "nested_unknown":
        candidate["snapshot"]["unexpected"] = True
    elif mutation == "nonce":
        candidate["snapshot_nonce"] = "00" * 16
    elif mutation == "environment":
        candidate["env_overrides"]["SAFE"] = "tampered"
    elif mutation == "argv":
        candidate["snapshot"]["argv"] = ["python", "-c", "bad"]
    elif mutation == "mac":
        candidate["snapshot_mac"] = "0" * 64

    with pytest.raises(AuthorizationSnapshotError):
        background_runner.parse_bridge_payload(candidate, verification_key=key)


def test_background_bridge_accepts_only_the_complete_signed_schema(tmp_path: Path) -> None:
    from crew.security import background_runner

    payload, key = _signed_bridge_payload(tmp_path)
    parsed = background_runner.parse_bridge_payload(payload, verification_key=key)

    assert parsed.authorization.snapshot.nonce == payload["snapshot_nonce"]
    assert parsed.environment == {"SAFE": "1"}
    assert parsed.timeout == 30
    assert parsed.max_output_bytes == 1024


def test_background_bridge_bootstrap_carries_key_only_in_private_stdin() -> None:
    from crew.security import background_runner

    payload = {"request": "opaque"}
    parsed_payload, key = background_runner.parse_bridge_bootstrap(
        {
            "authorization_key": (b"k" * 32).hex(),
            "parent_pid": os.getppid(),
            "payload": payload,
            "version": background_runner.BRIDGE_BOOTSTRAP_VERSION,
        }
    )

    assert parsed_payload is payload
    assert key == b"k" * 32
    assert not hasattr(background_runner, "BRIDGE_AUTH_KEY_ENV")


def test_background_bridge_bootstrap_is_bound_to_the_spawning_parent() -> None:
    from crew.security import background_runner
    from crew.security.snapshot import AuthorizationSnapshotError

    with pytest.raises(AuthorizationSnapshotError, match="parent identity"):
        background_runner.parse_bridge_bootstrap(
            {
                "authorization_key": (b"k" * 32).hex(),
                "parent_pid": os.getppid() + 1,
                "payload": {},
                "version": background_runner.BRIDGE_BOOTSTRAP_VERSION,
            }
        )


@pytest.mark.parametrize(
    "bootstrap",
    [
        {},
        {"authorization_key": "00", "payload": {}, "version": 1},
        {"authorization_key": "00" * 32, "payload": {}, "version": 2},
        {
            "authorization_key": "00" * 32,
            "parent_pid": os.getppid(),
            "payload": {},
            "unexpected": True,
            "version": 1,
        },
    ],
)
def test_background_bridge_bootstrap_rejects_incomplete_or_unknown_schema(
    bootstrap: dict,
) -> None:
    from crew.security import background_runner
    from crew.security.snapshot import AuthorizationSnapshotError

    with pytest.raises(AuthorizationSnapshotError):
        background_runner.parse_bridge_bootstrap(bootstrap)
