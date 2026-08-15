"""Single fail-closed application boundary for managed process execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from crew.security.models import (
    AdditionalPermissionProfile,
    PermissionProfile,
)
from crew.security.runtime_client import NativeRuntimeClient, RuntimeCommandResult
from crew.security.snapshot import SignedAuthorizationSnapshot


@dataclass(frozen=True)
class ExecutionRequest:
    """Normalized command and permissions supplied to the native runtime."""

    authorization_snapshot: SignedAuthorizationSnapshot | None = None
    command: tuple[str, ...] = ()
    cwd: Path | None = None
    permission_profile: PermissionProfile | None = None
    additional_permissions: AdditionalPermissionProfile = field(
        default_factory=AdditionalPermissionProfile
    )
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
        if request.authorization_snapshot is None:
            raise ValueError("managed execution requires an authorization snapshot")
        return await self._runtime.execute_authorized(
            authorization=request.authorization_snapshot,
            stdin=request.stdin,
            env_overrides=dict(request.env_overrides or {}),
            timeout=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            on_started=on_started,
            on_output=on_output,
        )


def packaged_runtime_argv(executable: str | Path) -> Sequence[str]:
    """Return an explicit argv without PATH or shell resolution."""
    return (str(Path(executable).expanduser().resolve(strict=False)),)
