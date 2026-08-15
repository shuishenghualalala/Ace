"""Central mapping and precedence rules for conversation security modes."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from crew.security.actions import ActionKind, NormalizedAction
from crew.security.models import (
    AdditionalPermissionProfile,
    ApprovalPolicy,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemGlobEntry,
    FilesystemOperation,
    NetworkAccess,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
    SecurityModeSettings,
)
from crew.state.home import get_crew_home


def settings_for_mode(
    mode: ConversationPermissionMode,
    workspace_root: Path | None,
    *,
    deny_entries: tuple[FilesystemEntry, ...] = (),
    deny_globs: tuple[FilesystemGlobEntry, ...] = (),
) -> SecurityModeSettings:
    """Compile one user-facing mode into sandbox permissions and approval policy."""
    if mode not in {
        ConversationPermissionMode.READ_ONLY,
        ConversationPermissionMode.REQUEST_APPROVAL,
        ConversationPermissionMode.AUTO_REVIEW,
        ConversationPermissionMode.FULL_ACCESS,
    }:
        raise ValueError(f"未知对话安全模式: {mode!r}")
    filesystem = (
        (
            FilesystemEntry(
                workspace_root,
                FilesystemAccess.READ
                if mode is ConversationPermissionMode.READ_ONLY
                else FilesystemAccess.READ_WRITE,
            ),
        )
        if workspace_root
        else ()
    )
    if mode is ConversationPermissionMode.FULL_ACCESS:
        # “完全访问”是宽权限受管模式，不再等同宿主裸跑。开放 workspace 与
        # 当前用户 home，随后由更具体、不可升级的 deny_entries 覆盖 Ace
        # DB/proof/identity/审计和 protected metadata；home 外仍需精确批准。
        user_home = Path.home().resolve(strict=False)
        broad_roots = {entry.root for entry in filesystem}
        if user_home != get_crew_home().resolve(strict=False) and user_home not in broad_roots:
            filesystem = (*filesystem, FilesystemEntry(user_home, FilesystemAccess.READ_WRITE))
        return SecurityModeSettings(
            profile=PermissionProfile(
                kind=PermissionProfileKind.MANAGED,
                filesystem=(*filesystem, *deny_entries),
                filesystem_globs=deny_globs,
                network=NetworkPolicy.RESTRICTED,
            ),
            approval_policy=ApprovalPolicy.REQUEST,
        )

    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(*filesystem, *deny_entries),
        filesystem_globs=deny_globs,
        network=NetworkPolicy.RESTRICTED,
    )
    approval = (
        ApprovalPolicy.REQUEST
        if mode
        in {
            ConversationPermissionMode.READ_ONLY,
            ConversationPermissionMode.REQUEST_APPROVAL,
        }
        else ApprovalPolicy.AUTO_REVIEW
    )
    return SecurityModeSettings(profile=profile, approval_policy=approval)


def filesystem_operation_allowed(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile,
    target: Path,
    operation: FilesystemOperation,
) -> bool:
    """Apply deny and specificity precedence to one canonical filesystem target."""
    if profile.kind is PermissionProfileKind.DISABLED:
        return True

    resolved = target.expanduser().resolve(strict=False)
    if operation is FilesystemOperation.READ and any(
        _glob_denies_read(entry, resolved) for entry in profile.filesystem_globs
    ):
        return False
    base_matches = [entry for entry in profile.filesystem if _contains(entry.root, resolved)]
    # A base-profile deny is a ceiling, not an invitation to add a narrower
    # capability. Additional permissions are positive grants and can never erase
    # an explicit deny, regardless of relative path specificity.
    if any(entry.access is FilesystemAccess.DENY for entry in base_matches):
        return False

    matches = [
        entry
        for entry in (*profile.filesystem, *additional.filesystem)
        if _contains(entry.root, resolved)
    ]
    if not matches:
        return False
    specificity = max(len(entry.root.parts) for entry in matches)
    selected = [entry for entry in matches if len(entry.root.parts) == specificity]
    if any(entry.access is FilesystemAccess.DENY for entry in selected):
        return False
    if operation is FilesystemOperation.READ:
        return any(
            entry.access in {FilesystemAccess.READ, FilesystemAccess.READ_WRITE}
            for entry in selected
        )
    return any(entry.access is FilesystemAccess.READ_WRITE for entry in selected)


def additional_permissions_for_file_action(action: NormalizedAction) -> AdditionalPermissionProfile:
    """Return the narrow root needed for one structured file action.

    Writes/deletes need the existing parent directory because Windows DELETE is
    controlled by the parent ACL.  Missing ancestors are never expanded to a
    filesystem root; the caller will fail closed instead of granting a broad root.
    """
    if action.kind is not ActionKind.FILE:
        return AdditionalPermissionProfile()
    target = Path(action.path).expanduser().resolve(strict=False)
    if action.operation == "read":
        root = target if target.exists() else _nearest_existing_directory(target.parent)
        access = FilesystemAccess.READ
    else:
        root = _nearest_existing_directory(target.parent)
        access = FilesystemAccess.READ_WRITE
    if root is None or _is_filesystem_root(root):
        return AdditionalPermissionProfile()
    return AdditionalPermissionProfile(filesystem=(FilesystemEntry(root, access),))


def permissions_needed_for_action(
    profile: PermissionProfile,
    action: NormalizedAction,
) -> AdditionalPermissionProfile:
    """Compute only the part of a structured action outside the base profile."""
    requested = additional_permissions_for_file_action(action)
    if not requested.filesystem:
        return requested
    target = Path(action.path).expanduser().resolve(strict=False)
    operation = (
        FilesystemOperation.READ if action.operation == "read" else FilesystemOperation.WRITE
    )
    if filesystem_operation_allowed(profile, AdditionalPermissionProfile(), target, operation):
        return AdditionalPermissionProfile()
    return requested


def exec_permissions_needed_for_action(
    profile: PermissionProfile,
    action: NormalizedAction,
) -> AdditionalPermissionProfile:
    requested = additional_permissions_for_exec_action(action)
    roots = [
        entry
        for entry in requested.filesystem
        if not _path_is_covered_by_write(profile, entry.root)
    ]
    return AdditionalPermissionProfile(filesystem=tuple(roots))


def network_operation_allowed(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile,
    action: NormalizedAction,
) -> bool:
    """Apply exact network allow/deny precedence to one normalized target."""
    if action.kind is not ActionKind.NETWORK:
        return False
    if profile.kind is PermissionProfileKind.DISABLED:
        return True
    matches = [
        entry
        for entry in (*profile.network_entries, *additional.network)
        if (
            entry.host == action.host
            and entry.port == action.port
            and entry.protocol == action.protocol
        )
    ]
    if any(entry.access is NetworkAccess.DENY for entry in matches):
        return False
    return any(entry.access is NetworkAccess.ALLOW for entry in matches)


def network_operation_explicitly_denied(
    profile: PermissionProfile,
    action: NormalizedAction,
) -> bool:
    """Return whether the managed base profile has an exact immutable deny."""
    if profile.kind is PermissionProfileKind.DISABLED or action.kind is not ActionKind.NETWORK:
        return False
    return any(
        entry.access is NetworkAccess.DENY
        and entry.host == action.host
        and entry.port == action.port
        and entry.protocol == action.protocol
        for entry in profile.network_entries
    )


def network_permissions_needed_for_action(
    profile: PermissionProfile,
    action: NormalizedAction,
) -> AdditionalPermissionProfile:
    """Return the one exact network capability missing from the base profile."""
    if action.kind is not ActionKind.NETWORK or network_operation_allowed(
        profile, AdditionalPermissionProfile(), action
    ):
        return AdditionalPermissionProfile()
    return AdditionalPermissionProfile(
        network=(NetworkEntry(action.host, action.port, action.protocol),)
    )


def exec_mutation_permissions_ungrantable(
    profile: PermissionProfile,
    mutation_targets: tuple[Path, ...],
) -> bool:
    """Return whether a literal delete target has no safe writable parent root."""
    return any(not _path_is_covered_by_write(profile, target.parent) for target in mutation_targets)


def merge_additional_permissions(
    *profiles: AdditionalPermissionProfile,
) -> AdditionalPermissionProfile:
    """Union exact capability entries while preserving their access modes."""
    filesystem: list[FilesystemEntry] = []
    network = []
    allow_local_binding = False
    for profile in profiles:
        for entry in profile.filesystem:
            if entry not in filesystem:
                filesystem.append(entry)
        for entry in profile.network:
            if entry not in network:
                network.append(entry)
        allow_local_binding = allow_local_binding or profile.allow_local_binding
    return AdditionalPermissionProfile(
        filesystem=tuple(filesystem),
        network=tuple(network),
        allow_local_binding=allow_local_binding,
    )


def normalize_additional_permissions(
    value: AdditionalPermissionProfile,
) -> AdditionalPermissionProfile:
    """Normalize a capability request at the host trust boundary.

    Permission requests are capability descriptions, not policy decisions.  They
    may contain only positive, exact entries; protected-path ceilings are applied
    later by the security service with the authenticated context.
    """
    if not isinstance(value, AdditionalPermissionProfile):
        raise ValueError("额外权限必须是 AdditionalPermissionProfile")
    if not isinstance(value.filesystem, tuple) or not all(
        isinstance(entry, FilesystemEntry) for entry in value.filesystem
    ):
        raise ValueError("额外文件权限必须是 FilesystemEntry 元组")
    if not isinstance(value.network, tuple) or not all(
        isinstance(entry, NetworkEntry) for entry in value.network
    ):
        raise ValueError("额外网络权限必须是 NetworkEntry 元组")
    if not isinstance(value.allow_local_binding, bool):
        raise ValueError("allow_local_binding 必须是布尔值")

    filesystem: list[FilesystemEntry] = []
    for entry in value.filesystem:
        if entry.access is FilesystemAccess.DENY:
            raise ValueError("额外权限请求不支持 deny 文件系统条目")
        normalized = FilesystemEntry(entry.root, entry.access, escalatable=True)
        if normalized not in filesystem:
            filesystem.append(normalized)

    network = []
    for entry in value.network:
        if entry.access is NetworkAccess.DENY:
            raise ValueError("额外权限请求不支持 deny 网络条目")
        if entry not in network:
            network.append(entry)

    return AdditionalPermissionProfile(
        filesystem=tuple(filesystem),
        network=tuple(network),
        allow_local_binding=bool(value.allow_local_binding),
    )


def deserialize_additional_permissions(value: object) -> AdditionalPermissionProfile:
    """Parse an untrusted API/UI capability payload with a strict schema."""
    if not isinstance(value, Mapping):
        raise ValueError("额外权限必须是对象")
    unknown = set(value) - {"filesystem", "network", "allow_local_binding"}
    if unknown:
        raise ValueError(f"额外权限包含未知字段: {sorted(map(str, unknown))}")

    filesystem: list[FilesystemEntry] = []
    raw_filesystem = value.get("filesystem", [])
    if not isinstance(raw_filesystem, (list, tuple)):
        raise ValueError("filesystem 必须是数组")
    for raw_entry in raw_filesystem:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("filesystem 条目必须是对象")
        if set(raw_entry) - {"root", "access", "escalatable"}:
            raise ValueError("filesystem 条目包含未知字段")
        if "escalatable" in raw_entry and raw_entry["escalatable"] is not True:
            raise ValueError("filesystem escalatable 只能为 true")
        raw_root = raw_entry.get("root")
        raw_access = raw_entry.get("access")
        if (
            not isinstance(raw_root, str)
            or not raw_root
            or "\x00" in raw_root
            or not isinstance(raw_access, str)
        ):
            raise ValueError("filesystem root/access 类型无效")
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise ValueError("filesystem root 必须是绝对路径")
        try:
            access = FilesystemAccess(raw_access)
        except ValueError as exc:
            raise ValueError("filesystem access 无效") from exc
        filesystem.append(FilesystemEntry(root, access))

    network: list[NetworkEntry] = []
    raw_network = value.get("network", [])
    if not isinstance(raw_network, (list, tuple)):
        raise ValueError("network 必须是数组")
    for raw_entry in raw_network:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("network 条目必须是对象")
        if set(raw_entry) - {
            "host",
            "port",
            "protocol",
            "access",
            "allow_private",
            "escalatable",
        }:
            raise ValueError("network 条目包含未知字段")
        if "escalatable" in raw_entry and raw_entry["escalatable"] is not True:
            raise ValueError("network escalatable 只能为 true")
        allow_private = raw_entry.get("allow_private", False)
        if not isinstance(allow_private, bool):
            raise ValueError("allow_private 必须是布尔值")
        raw_host = raw_entry.get("host")
        raw_protocol = raw_entry.get("protocol")
        raw_access = raw_entry.get("access", NetworkAccess.ALLOW.value)
        raw_port = raw_entry.get("port")
        if (
            not isinstance(raw_host, str)
            or not isinstance(raw_protocol, str)
            or not isinstance(raw_access, str)
            or not isinstance(raw_port, int)
            or isinstance(raw_port, bool)
        ):
            raise ValueError("network port 必须是整数")
        try:
            access = NetworkAccess(raw_access)
            if access is NetworkAccess.DENY:
                raise ValueError("network deny 不可作为额外权限")
            network.append(
                NetworkEntry(
                    raw_host,
                    raw_port,
                    raw_protocol,
                    access=access,
                    allow_private=allow_private,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("network 条目无效") from exc

    local_binding = value.get("allow_local_binding", False)
    if not isinstance(local_binding, bool):
        raise ValueError("allow_local_binding 必须是布尔值")
    return normalize_additional_permissions(
        AdditionalPermissionProfile(
            filesystem=tuple(filesystem),
            network=tuple(network),
            allow_local_binding=local_binding,
        )
    )


def intersect_additional_permissions(
    requested: AdditionalPermissionProfile,
    granted: AdditionalPermissionProfile,
) -> AdditionalPermissionProfile:
    """Return only capability entries that are contained in the request.

    This is deliberately an intersection, never a union: a renderer, plugin, or
    model may submit a broader grant than the pending request, but that broader
    grant must not become authority.
    """
    requested = normalize_additional_permissions(requested)
    granted = normalize_additional_permissions(granted)
    filesystem: list[FilesystemEntry] = []
    for candidate in granted.filesystem:
        if (
            any(
                candidate.root == asked.root or _contains(asked.root, candidate.root)
                for asked in requested.filesystem
                if _filesystem_access_covers(asked.access, candidate.access)
            )
            and candidate not in filesystem
        ):
            filesystem.append(candidate)
    network = [
        candidate
        for candidate in granted.network
        if any(
            candidate.host == asked.host
            and candidate.port == asked.port
            and candidate.protocol == asked.protocol
            and (not candidate.allow_private or asked.allow_private)
            and _filesystem_access_covers(asked.access, candidate.access)
            for asked in requested.network
        )
    ]
    return AdditionalPermissionProfile(
        filesystem=tuple(filesystem),
        network=tuple(network),
        allow_local_binding=(requested.allow_local_binding and granted.allow_local_binding),
    )


def _filesystem_access_covers(
    requested: FilesystemAccess | NetworkAccess,
    granted: FilesystemAccess | NetworkAccess,
) -> bool:
    if isinstance(requested, FilesystemAccess) and isinstance(granted, FilesystemAccess):
        return (
            granted is FilesystemAccess.READ
            and requested
            in {
                FilesystemAccess.READ,
                FilesystemAccess.READ_WRITE,
            }
            or granted is FilesystemAccess.READ_WRITE
            and requested is FilesystemAccess.READ_WRITE
        )
    if isinstance(requested, NetworkAccess) and isinstance(granted, NetworkAccess):
        return requested is NetworkAccess.ALLOW and granted is NetworkAccess.ALLOW
    return False


def inferred_exec_mutation_targets(action: NormalizedAction) -> tuple[Path, ...]:
    """Extract only literal delete targets from a classified shell action.

    Arbitrary scripts, variables, globs, redirections, and command substitutions
    intentionally produce no inferred capability.  They still require command
    approval, but remain inside the base sandbox unless an explicit future
    capability request is added.
    """
    if action.kind is not ActionKind.EXEC:
        return ()
    targets: list[Path] = []
    for command in action.parsed_commands:
        for candidate in _delete_command_candidates(command):
            _append_literal_delete_targets(candidate, action, targets)
    return tuple(targets[:16])


def _delete_command_candidates(command: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Include the literal command inside a safe ``cmd /c`` wrapper."""
    candidates = [command]
    if not command:
        return ()
    name = Path(command[0]).name.lower()
    name = name.removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    if name == "cmd":
        for index, token in enumerate(command[1:], start=1):
            if token.lower() in {"/c", "/k"} and index + 1 < len(command):
                nested = command[index + 1 :]
                if len(nested) > 1 or not any(char.isspace() for char in nested[0]):
                    candidates.append(nested)
                break
    return tuple(candidates)


def _append_literal_delete_targets(
    command: tuple[str, ...], action: NormalizedAction, targets: list[Path]
) -> None:
    if not command:
        return
    name = Path(command[0]).name.lower()
    name = name.removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    if name not in {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri"}:
        return
    for raw in _literal_delete_arguments(command):
        if any(marker in raw for marker in ("*", "?", "[", "]", "$", "`", "\x00")):
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(action.cwd) / path
        resolved = Path(os.path.abspath(str(path)))
        if _has_reparse_component(resolved):
            continue
        if resolved not in targets:
            targets.append(resolved)


def additional_permissions_for_exec_action(action: NormalizedAction) -> AdditionalPermissionProfile:
    """Return parent-directory write roots for statically classified deletes."""
    roots: list[FilesystemEntry] = []
    for target in inferred_exec_mutation_targets(action):
        root = _nearest_existing_directory(target.parent)
        if root is None or _is_filesystem_root(root):
            continue
        entry = FilesystemEntry(root, FilesystemAccess.READ_WRITE)
        if entry not in roots:
            roots.append(entry)
    return AdditionalPermissionProfile(filesystem=tuple(roots))


def serialize_additional_permissions(value: AdditionalPermissionProfile) -> dict:
    """Serialize the capability request for the approval UI without enum objects."""
    return {
        "filesystem": [
            {
                "root": str(entry.root),
                "access": entry.access.value,
                "escalatable": entry.escalatable,
            }
            for entry in value.filesystem
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
            for entry in value.network
        ],
        "allow_local_binding": value.allow_local_binding,
    }


def _literal_delete_arguments(command: tuple[str, ...]) -> tuple[str, ...]:
    path_options = {"-path", "-literalpath"}
    value_options = {
        "-filter",
        "-include",
        "-exclude",
        "-erroraction",
        "-warningaction",
        "-informationaction",
        "-outvariable",
        "-outbuffer",
        "-pipelinevariable",
        "--context",
    }
    paths: list[str] = []
    index = 1
    while index < len(command):
        token = command[index]
        lower = token.lower()
        if token == "--":
            paths.extend(command[index + 1 :])
            break
        if lower in path_options:
            if index + 1 < len(command):
                paths.append(command[index + 1])
            index += 2
            continue
        if lower in value_options:
            index += 2
            continue
        if token.startswith("-") or (os.name == "nt" and token.startswith("/")):
            index += 1
            continue
        paths.append(token)
        index += 1
    return tuple(paths)


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path.resolve(strict=False)
    if not candidate.exists() or not candidate.is_dir():
        return None
    return candidate


def _has_reparse_component(path: Path) -> bool:
    """Reject symlink/reparse traversal when inferring a shell capability."""
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            return True
    return False


def _is_filesystem_root(path: Path) -> bool:
    return bool(path.anchor) and path == Path(path.anchor)


def _path_is_covered_by_write(profile: PermissionProfile, target: Path) -> bool:
    return any(
        entry.access is FilesystemAccess.READ_WRITE and _contains(entry.root, target)
        for entry in profile.filesystem
    )


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _glob_denies_read(entry: FilesystemGlobEntry, target: Path) -> bool:
    try:
        relative = target.relative_to(entry.root).as_posix()
    except ValueError:
        return False
    candidate = relative.casefold() if os.name == "nt" else relative
    pattern = entry.pattern.casefold() if os.name == "nt" else entry.pattern
    path = PurePosixPath(candidate)
    if path.match(pattern):
        return True
    # pathlib requires at least one segment for a leading ``**/``. Treat it as
    # zero-or-more segments so ``**/*.pem`` also protects ``root/secret.pem``.
    while pattern.startswith("**/"):
        pattern = pattern[3:]
        if path.match(pattern):
            return True
    return False
