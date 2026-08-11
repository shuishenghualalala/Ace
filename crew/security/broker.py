"""Single fail-closed application boundary for managed process execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    FilesystemAccess,
    NetworkAccess,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeClient, RuntimeCommandResult


@dataclass(frozen=True)
class ExecutionRequest:
    """Normalized command and permissions supplied to the native runtime."""

    command: tuple[str, ...]
    cwd: Path
    permission_profile: PermissionProfile
    additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS
    trusted_readable_roots: tuple[Path, ...] = ()
    stdin: bytes | None = None
    env_overrides: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0
    max_output_bytes: int = 2 * 1024 * 1024


class SecurityExecutionBroker:
    """Translate a managed permission profile into one native runtime call."""

    def __init__(self, runtime: NativeRuntimeClient) -> None:
        self._runtime = runtime

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        on_started: Callable[[int | None], None] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"]], None] | None = None,
    ) -> RuntimeCommandResult:
        """Run a managed request; disabled profiles are deliberately unsupported here."""
        if request.permission_profile.kind is not PermissionProfileKind.MANAGED:
            raise ValueError("host execution is outside the managed security broker")
        writable, readable, readonly, denied = compile_runtime_filesystem_roots(
            request.permission_profile,
            request.additional_permissions,
            request.trusted_readable_roots,
        )
        network_entries = (
            *request.permission_profile.network_entries,
            *request.additional_permissions.network,
        )
        network_rules = [
            {
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
                "allow": entry.access is NetworkAccess.ALLOW,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
            }
            for entry in network_entries
        ]
        return await self._runtime.execute(
            command=request.command,
            cwd=request.cwd,
            writable_roots=writable,
            readable_roots=readable,
            readonly_roots=readonly,
            denied_roots=denied,
            network_enabled=bool(network_rules),
            network_rules=network_rules,
            allow_local_binding=(
                request.permission_profile.allow_local_binding
                or request.additional_permissions.allow_local_binding
            ),
            timeout=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            stdin=request.stdin,
            env_overrides=request.env_overrides,
            on_started=on_started,
            on_output=on_output,
        )


def compile_runtime_filesystem_roots(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
    trusted_readable_roots: Sequence[Path] = (),
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Compile one consistent native filesystem plan for foreground/background runs.

    A task workspace can intentionally be a more-specific child of the protected
    runtime home. Native backends cannot enforce both a writable child and a deny
    mount/ACE on its ancestor: the ancestor deny wins and makes a valid approval
    unusable. Drop only those strict ancestor denies. The native default-deny
    boundary still hides every sibling, while exact and descendant denies remain.
    """
    writable: list[Path] = []
    readable = list(dict.fromkeys(trusted_readable_roots))
    readonly: list[Path] = []
    denied: list[Path] = []
    for entry in (*profile.filesystem, *additional.filesystem):
        if entry.access is FilesystemAccess.READ_WRITE:
            if entry.root not in writable:
                writable.append(entry.root)
        elif entry.access is FilesystemAccess.READ:
            # Immutable entries below writable roots use the native read-only
            # carve-out contract. Missing metadata paths remain valid so the
            # runtime can prevent their later creation.
            target = readonly if not entry.escalatable else readable
            if entry.root not in target:
                target.append(entry.root)
        elif entry.access is FilesystemAccess.DENY and entry.root not in denied:
            denied.append(entry.root)

    # A trusted/runtime root below a writable root is already visible through the
    # write bind. Forwarding both makes Linux reject an overlapping mount plan.
    readable = [
        root
        for root in readable
        if not any(root == write or write in root.parents for write in writable)
    ]
    allowed_roots = (*writable, *readable)
    denied = [
        root
        for root in denied
        if not any(root != allowed and root in allowed.parents for allowed in allowed_roots)
    ]
    return writable, readable, readonly, denied


def packaged_runtime_argv(executable: str | Path) -> Sequence[str]:
    """Return an explicit argv without PATH or shell resolution."""
    return (str(Path(executable).expanduser().resolve(strict=False)),)
