"""Immutable inputs shared by Ace security policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ConversationPermissionMode(StrEnum):
    """User-facing security mode selected when a conversation is created."""

    REQUEST_APPROVAL = "request_approval"
    AUTO_REVIEW = "auto_review"
    FULL_ACCESS = "full_access"


class ApprovalPolicy(StrEnum):
    """Host policy for deciding whether an approval prompt is shown."""

    REQUEST = "request"
    AUTO_REVIEW = "auto_review"
    NEVER = "never"


class PermissionProfileKind(StrEnum):
    MANAGED = "managed"
    DISABLED = "disabled"


class NetworkPolicy(StrEnum):
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class NetworkAccess(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class FilesystemAccess(StrEnum):
    READ = "read"
    READ_WRITE = "read_write"
    DENY = "deny"


class FilesystemOperation(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class NetworkEntry:
    """One exact proxy destination; wildcards and URL paths are never permissions."""

    host: str
    port: int
    protocol: str
    access: NetworkAccess = NetworkAccess.ALLOW
    allow_private: bool = False
    escalatable: bool = True

    def __post_init__(self) -> None:
        from crew.security.actions import normalize_network_action

        action = normalize_network_action(self.host, self.port, self.protocol)
        object.__setattr__(self, "host", action.host)
        object.__setattr__(self, "port", action.port)
        object.__setattr__(self, "protocol", action.protocol)


@dataclass(frozen=True)
class FilesystemEntry:
    """Access rooted at one canonical host path."""

    root: Path
    access: FilesystemAccess
    escalatable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve(strict=False))


@dataclass(frozen=True)
class PermissionProfile:
    """Canonical sandbox permissions; disabled means current OS-user authority."""

    kind: PermissionProfileKind
    filesystem: tuple[FilesystemEntry, ...] = ()
    network: NetworkPolicy = NetworkPolicy.RESTRICTED
    network_entries: tuple[NetworkEntry, ...] = ()
    allow_local_binding: bool = False


@dataclass(frozen=True)
class AdditionalPermissionProfile:
    """Exact permissions approved in addition to a managed base profile."""

    filesystem: tuple[FilesystemEntry, ...] = ()
    network: tuple[NetworkEntry, ...] = ()
    allow_local_binding: bool = False


@dataclass(frozen=True)
class SecurityModeSettings:
    """The two independent axes selected by one Desktop mode."""

    profile: PermissionProfile
    approval_policy: ApprovalPolicy
