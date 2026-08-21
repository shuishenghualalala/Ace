"""Immutable inputs shared by Ace security policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


PERMISSION_PROFILE_SCHEMA_VERSION = "ace.security.profile.v1"
RULE_SCHEMA_VERSION = "ace.security.rule.v1"
from pathlib import Path
from typing import Any


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
class SandboxPermissions(StrEnum):
    """Per-command relationship with the conversation sandbox."""

    USE_DEFAULT = "use_default"
    WITH_ADDITIONAL_PERMISSIONS = "with_additional_permissions"
    REQUIRE_ESCALATED = "require_escalated"


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
    full_disk_read: bool = False


@dataclass(frozen=True)
class AdditionalPermissionProfile:
    """Exact permissions approved in addition to a managed base profile."""

    filesystem: tuple[FilesystemEntry, ...] = ()
    network: tuple[NetworkEntry, ...] = ()
    allow_local_binding: bool = False
    sandbox_permissions: SandboxPermissions = SandboxPermissions.USE_DEFAULT

    @property
    def empty(self) -> bool:
        return (
            not self.filesystem
            and not self.network
            and not self.allow_local_binding
            and self.sandbox_permissions is SandboxPermissions.USE_DEFAULT
        )

    def is_empty(self) -> bool:
        return not (self.filesystem or self.network or self.allow_local_binding)


EMPTY_ADDITIONAL_PERMISSIONS = AdditionalPermissionProfile()


def merge_additional_permissions(
    *profiles: AdditionalPermissionProfile,
) -> AdditionalPermissionProfile:
    """Merge effective sandbox permissions without weakening an explicit boundary.

    Session permissions are capabilities, not command approvals.  They are merged
    into later sandbox launches independently from the command that originally
    requested them.  ``require_escalated`` is deliberately not made sticky by
    this helper; callers must keep unsandboxed authority bound to the exact
    approved command.
    """
    filesystem: list[FilesystemEntry] = []
    network: list[NetworkEntry] = []
    allow_local_binding = False
    uses_additional_permissions = False
    for profile in profiles:
        for entry in profile.filesystem:
            if entry not in filesystem:
                filesystem.append(entry)
        for entry in profile.network:
            if entry not in network:
                network.append(entry)
        allow_local_binding = allow_local_binding or profile.allow_local_binding
        uses_additional_permissions = uses_additional_permissions or bool(
            profile.filesystem or profile.network or profile.allow_local_binding
        )
    strongest_by_root: dict[Path, FilesystemEntry] = {}
    for entry in filesystem:
        existing = strongest_by_root.get(entry.root)
        if existing is None or (
            existing.access is FilesystemAccess.READ
            and entry.access is FilesystemAccess.READ_WRITE
        ):
            strongest_by_root[entry.root] = entry
    normalized_filesystem: list[FilesystemEntry] = []
    for entry in sorted(strongest_by_root.values(), key=lambda item: len(item.root.parts)):
        covered = False
        for existing in normalized_filesystem:
            try:
                entry.root.relative_to(existing.root)
            except ValueError:
                continue
            if existing.access is FilesystemAccess.READ_WRITE or (
                existing.access is FilesystemAccess.READ
                and entry.access is FilesystemAccess.READ
            ):
                covered = True
                break
        if not covered:
            normalized_filesystem.append(entry)
    return AdditionalPermissionProfile(
        filesystem=tuple(normalized_filesystem),
        network=tuple(network),
        allow_local_binding=allow_local_binding,
        sandbox_permissions=(
            SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS
            if uses_additional_permissions
            else SandboxPermissions.USE_DEFAULT
        ),
    )


def additional_permissions_cover(
    granted: AdditionalPermissionProfile,
    requested: AdditionalPermissionProfile,
) -> bool:
    """Return whether one approved sandbox profile covers a requested subset."""
    if (
        requested.sandbox_permissions is SandboxPermissions.REQUIRE_ESCALATED
        or granted.sandbox_permissions is SandboxPermissions.REQUIRE_ESCALATED
    ):
        return granted == requested
    if requested.allow_local_binding and not granted.allow_local_binding:
        return False
    if any(entry not in granted.network for entry in requested.network):
        return False
    for requested_entry in requested.filesystem:
        covered = False
        for granted_entry in granted.filesystem:
            try:
                requested_entry.root.relative_to(granted_entry.root)
            except ValueError:
                continue
            if requested_entry.access is FilesystemAccess.READ_WRITE:
                covered = granted_entry.access is FilesystemAccess.READ_WRITE
            else:
                covered = granted_entry.access in {
                    FilesystemAccess.READ,
                    FilesystemAccess.READ_WRITE,
                }
            if covered:
                break
        if not covered:
            return False
    return True


def serialize_additional_permissions(profile: AdditionalPermissionProfile) -> dict[str, Any]:
    """Serialize an immutable permission overlay for approval, persistence, and runtime IO."""
    return {
        "filesystem": [
            {
                "root": str(entry.root),
                "access": entry.access.value,
                "escalatable": entry.escalatable,
            }
            for entry in profile.filesystem
        ],
        "network": [
            {
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
                "access": entry.access.value,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
            }
            for entry in profile.network
        ],
        "allow_local_binding": profile.allow_local_binding,
        "sandbox_permissions": profile.sandbox_permissions.value,
    }


def deserialize_additional_permissions(payload: object) -> AdditionalPermissionProfile:
    """Decode a host-created permission overlay and reject malformed persisted state."""
    if payload is None:
        return AdditionalPermissionProfile()
    if not isinstance(payload, dict):
        raise ValueError("additional_permissions 必须是对象")
    filesystem_payload = payload.get("filesystem", [])
    network_payload = payload.get("network", [])
    if not isinstance(filesystem_payload, list) or not isinstance(network_payload, list):
        raise ValueError("additional_permissions 条目必须是数组")
    filesystem = tuple(
        FilesystemEntry(
            root=Path(_required_string(entry, "root")),
            access=FilesystemAccess(_required_string(entry, "access")),
            escalatable=_optional_bool(entry, "escalatable", True),
        )
        for entry in filesystem_payload
    )
    network = tuple(
        NetworkEntry(
            host=_required_string(entry, "host"),
            port=_required_port(entry),
            protocol=_required_string(entry, "protocol"),
            access=NetworkAccess(_required_string(entry, "access", NetworkAccess.ALLOW.value)),
            allow_private=_optional_bool(entry, "allow_private", False),
            escalatable=_optional_bool(entry, "escalatable", True),
        )
        for entry in network_payload
    )
    return AdditionalPermissionProfile(
        filesystem=filesystem,
        network=network,
        allow_local_binding=_optional_bool(payload, "allow_local_binding", False),
        sandbox_permissions=SandboxPermissions(
            str(payload.get("sandbox_permissions", SandboxPermissions.USE_DEFAULT.value))
        ),
    )


def _required_string(payload: object, key: str, default: str | None = None) -> str:
    if not isinstance(payload, dict):
        raise ValueError("permission entry 必须是对象")
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_bool(payload: object, key: str, default: bool) -> bool:
    if not isinstance(payload, dict):
        raise ValueError("permission entry 必须是对象")
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _required_port(payload: object) -> int:
    if not isinstance(payload, dict):
        raise ValueError("permission entry 必须是对象")
    value = payload.get("port")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("port 必须在 1..65535")
    return value

@dataclass(frozen=True)
class SecurityModeSettings:
    """The two independent axes selected by one Desktop mode."""

    profile: PermissionProfile
    approval_policy: ApprovalPolicy
