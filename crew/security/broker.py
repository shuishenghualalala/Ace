"""Single fail-closed application boundary for managed process execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from crew.security.models import (
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
    additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile()
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
        kwargs = self._runtime_kwargs(request)
        return await self._runtime.execute(
            **kwargs,
            on_started=on_started,
            on_output=on_output,
        )

    async def open_interactive(self, request: ExecutionRequest):
        """Open a managed bidirectional child through the native runtime."""
        kwargs = self._runtime_kwargs(request)
        kwargs.pop("stdin", None)
        return await self._runtime.open_interactive(**kwargs)

    @staticmethod
    def _runtime_kwargs(request: ExecutionRequest) -> dict:
        if request.permission_profile.kind is not PermissionProfileKind.MANAGED:
            raise ValueError("host execution is outside the managed security broker")
        writable: list[Path] = []
        readable = list(request.trusted_readable_roots)
        denied: list[Path] = []
        filesystem = (*request.permission_profile.filesystem, *request.additional_permissions.filesystem)
        for entry in filesystem:
            if entry.access is FilesystemAccess.READ_WRITE:
                writable.append(entry.root)
            elif entry.access is FilesystemAccess.READ:
                # Immutable project-metadata guards are runtime-owned carveouts
                # below writable roots. Forwarding their often-missing paths as
                # ordinary reads makes NativeRuntimeClient.resolve(strict=True)
                # reject every normal workspace before either backend starts.
                if entry.escalatable:
                    if entry.root not in readable:
                        readable.append(entry.root)
            elif entry.access is FilesystemAccess.DENY:
                denied.append(entry.root)
        # A trusted runtime root already below a writable root is visible through
        # that bind. Forwarding both would make the Linux plan reject an overlap.
        readable = [
            root
            for root in readable
            if not any(root == write or write in root.parents for write in writable)
        ]
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
        return {
            "command": request.command,
            "cwd": request.cwd,
            "writable_roots": writable,
            "readable_roots": readable,
            "denied_roots": denied,
            "network_enabled": bool(network_rules),
            "network_rules": network_rules,
            "allow_local_binding": (
                request.permission_profile.allow_local_binding
                or request.additional_permissions.allow_local_binding
            ),
            "timeout": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
            "stdin": request.stdin,
            "env_overrides": request.env_overrides,
        }


def packaged_runtime_argv(executable: str | Path) -> Sequence[str]:
    """Return an explicit argv without PATH or shell resolution."""
    return (str(Path(executable).expanduser().resolve(strict=False)),)
