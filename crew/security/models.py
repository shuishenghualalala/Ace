"""Immutable inputs shared by Ace security policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


PERMISSION_PROFILE_SCHEMA_VERSION = "ace.security.profile.v1"
RULE_SCHEMA_VERSION = "ace.security.rule.v1"


class ConversationPermissionMode(StrEnum):
    """User-facing security mode selected when a conversation is created."""

    READ_ONLY = "read_only"
    REQUEST_APPROVAL = "request_approval"
    AUTO_REVIEW = "auto_review"
    FULL_ACCESS = "full_access"


class ApprovalPolicy(StrEnum):
    """Host policy for deciding whether an approval prompt is shown."""

    REQUEST = "request"
    AUTO_REVIEW = "auto_review"
    NEVER = "never"


class ApprovalChannel(StrEnum):
    EXEC = "exec"
    FILE = "file"
    NETWORK = "network"
    PERMISSION = "permission"


@dataclass(frozen=True)
class GranularApprovalConfig:
    """Host-owned switches for the approval channels Ace actually exposes."""

    exec: bool = True
    file: bool = True
    network: bool = True
    permission: bool = True

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (self.exec, self.file, self.network, self.permission)
        ):
            raise ValueError("granular approval 配置必须是布尔值")

    def allows(self, channel: ApprovalChannel) -> bool:
        if not isinstance(channel, ApprovalChannel):
            raise ValueError("未知审批通道")
        return {
            ApprovalChannel.EXEC: self.exec,
            ApprovalChannel.FILE: self.file,
            ApprovalChannel.NETWORK: self.network,
            ApprovalChannel.PERMISSION: self.permission,
        }[channel]


class PermissionProfileKind(StrEnum):
    MANAGED = "managed"
    DISABLED = "disabled"


class SandboxablePreference(StrEnum):
    """Host-owned sandbox intent for one concrete process surface."""

    FORBID = "forbid"
    REQUIRE = "require"
    AUTO = "auto"


# FORBID is intentionally a closed host registry, not a caller-provided label.
# Each entry names a fixed control-plane surface whose commands are selected by
# host code and whose use is bound into the signed launch/snapshot audit facts.
HOST_FIXED_SANDBOX_FORBID_SURFACES: frozenset[str] = frozenset(
    {
        "cua-driver-admin",
        "external-runtime-discovery",
    }
)


def resolve_sandboxable_preference(
    profile_kind: PermissionProfileKind,
    preference: SandboxablePreference,
    *,
    system_surface: str = "",
) -> bool:
    """Resolve one preference exactly once, rejecting inconsistent authority."""

    if not isinstance(profile_kind, PermissionProfileKind):
        raise TypeError("sandbox permission profile kind is invalid")
    if not isinstance(preference, SandboxablePreference):
        raise TypeError("sandbox preference must be host-owned")
    if not isinstance(system_surface, str):
        raise TypeError("sandbox system surface must be host-owned text")
    surface = system_surface
    if "\x00" in surface:
        raise ValueError("sandbox system surface is invalid")

    if preference is SandboxablePreference.FORBID:
        if surface not in HOST_FIXED_SANDBOX_FORBID_SURFACES:
            raise ValueError("sandbox FORBID is limited to registered host-fixed system surfaces")
        if profile_kind is not PermissionProfileKind.DISABLED:
            raise ValueError("sandbox FORBID requires an explicit disabled profile")
        return False

    if surface:
        raise ValueError("sandbox system surface is only valid with the FORBID preference")
    if preference is SandboxablePreference.REQUIRE:
        if profile_kind is not PermissionProfileKind.MANAGED:
            raise ValueError("sandbox REQUIRE cannot resolve to host execution")
        return True
    if preference is SandboxablePreference.AUTO:
        return profile_kind is PermissionProfileKind.MANAGED
    raise ValueError("sandbox preference is unsupported")


class NetworkPolicy(StrEnum):
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class NetworkAccess(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionGrantScope(StrEnum):
    """Lifetime of a capability grant, independent from an action rule."""

    TURN = "turn"
    SESSION = "session"


class FilesystemAccess(StrEnum):
    READ = "read"
    READ_WRITE = "read_write"
    DENY = "deny"


class FilesystemOperation(StrEnum):
    READ = "read"
    WRITE = "write"


class FilesystemGlobAccess(StrEnum):
    """The only supported glob capability is a negative read filter."""

    DENY_READ = "deny_read"


def _validate_filesystem_glob_pattern(pattern: str) -> None:
    """Accept one portable, unambiguous globset/pathlib pattern."""
    parts = pattern.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or any(token in pattern for token in ("{", "}"))
        or any("**" in part and part != "**" for part in parts)
    ):
        raise ValueError("filesystem glob pattern 语法无效")

    in_class = False
    class_size = 0
    for character in pattern:
        if character == "[":
            if in_class:
                raise ValueError("filesystem glob pattern 语法无效")
            in_class = True
            class_size = 0
        elif character == "]":
            if not in_class or class_size == 0:
                raise ValueError("filesystem glob pattern 语法无效")
            in_class = False
        elif in_class:
            if character == "/":
                raise ValueError("filesystem glob pattern 语法无效")
            class_size += 1
    if in_class:
        raise ValueError("filesystem glob pattern 语法无效")


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
class FilesystemGlobEntry:
    """A canonical-root-relative deny-read pattern.

    Glob entries deliberately cannot express allow/write authority. Positive
    capabilities must use exact canonical roots so a pattern cannot widen access.
    """

    root: Path
    pattern: str
    access: FilesystemGlobAccess = FilesystemGlobAccess.DENY_READ

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve(strict=False)
        if self.access is not FilesystemGlobAccess.DENY_READ:
            raise ValueError("filesystem glob 只支持 deny_read")
        if not isinstance(self.pattern, str):
            raise TypeError("filesystem glob pattern 必须是字符串")
        normalized = self.pattern.replace("\\", "/")
        raw_parts = normalized.split("/")
        parsed = PurePosixPath(normalized)
        if (
            not normalized
            or "\x00" in normalized
            or parsed.is_absolute()
            or ":" in normalized
            or any(part == ".." for part in raw_parts)
        ):
            raise ValueError("filesystem glob pattern 必须是安全的相对模式")
        _validate_filesystem_glob_pattern(normalized)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "pattern", parsed.as_posix())


@dataclass(frozen=True)
class PermissionProfile:
    """Canonical sandbox permissions; disabled means current OS-user authority."""

    kind: PermissionProfileKind
    filesystem: tuple[FilesystemEntry, ...] = ()
    filesystem_globs: tuple[FilesystemGlobEntry, ...] = ()
    network: NetworkPolicy = NetworkPolicy.RESTRICTED
    network_entries: tuple[NetworkEntry, ...] = ()
    allow_local_binding: bool = False


@dataclass(frozen=True)
class AdditionalPermissionProfile:
    """Exact permissions approved in addition to a managed base profile."""

    filesystem: tuple[FilesystemEntry, ...] = ()
    network: tuple[NetworkEntry, ...] = ()
    allow_local_binding: bool = False

    def is_empty(self) -> bool:
        return not (self.filesystem or self.network or self.allow_local_binding)


@dataclass(frozen=True)
class SecurityModeSettings:
    """The two independent axes selected by one Desktop mode."""

    profile: PermissionProfile
    approval_policy: ApprovalPolicy
