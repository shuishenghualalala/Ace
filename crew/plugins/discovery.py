"""Bounded, immutable plugin discovery snapshots.

Discovery is deliberately separate from import/registration.  Every recognized
plugin bundle is represented by the exact directory and file identities plus
content digests that were inspected.  Callers can therefore reject a swapped or
stale tree before any executable entrypoint is imported.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from crew.plugins.security import (
    PLUGIN_PROVENANCE_FILE,
    PLUGIN_SIGNATURE_FILE,
    PluginSecurityError,
)
from crew.tools.file_utils import FileConflictError, FileIdentity, _pinned_parent, snapshot_file

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class PluginDiscoveryLimits:
    max_roots: int
    max_depth: int
    max_directories: int
    max_entries: int
    max_files: int
    max_bundles: int
    max_file_bytes: int
    max_aggregate_bytes: int


@dataclass(frozen=True)
class PluginPathIdentity:
    relative_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    sha256: str = ""

    @classmethod
    def from_stat(
        cls,
        relative_path: str,
        info: os.stat_result,
        *,
        sha256: str = "",
    ) -> PluginPathIdentity:
        return cls(
            relative_path=relative_path,
            device=int(info.st_dev),
            inode=int(info.st_ino),
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            ctime_ns=int(info.st_ctime_ns),
            mode=int(info.st_mode),
            sha256=sha256,
        )


@dataclass(frozen=True)
class PluginDiscoveryRoot:
    path: str
    source: str
    identity: PluginPathIdentity


@dataclass(frozen=True)
class PluginDiscoveryMember:
    root_path: str
    root_identity: PluginPathIdentity
    plugin_path: str
    relative_path: str
    key: str
    source: str
    depth: int
    manifest_relative_path: str
    manifest_bytes: bytes
    manifest_sha256: str
    tree_sha256: str
    directories: tuple[PluginPathIdentity, ...]
    files: tuple[PluginPathIdentity, ...]


@dataclass(frozen=True)
class PluginDiscoverySnapshot:
    request_scope: tuple[str, str, str] | None
    snapshot_id: str
    roots: tuple[PluginDiscoveryRoot, ...]
    members: tuple[PluginDiscoveryMember, ...]
    directories_seen: int
    entries_seen: int
    files_seen: int
    aggregate_bytes: int


@dataclass
class _Budget:
    limits: PluginDiscoveryLimits
    directories: int = 0
    entries: int = 0
    files: int = 0
    bundles: int = 0
    aggregate_bytes: int = 0

    def depth(self, value: int) -> None:
        if value > self.limits.max_depth:
            raise _limit("plugin discovery depth budget exceeded")

    def directory(self) -> None:
        self.directories += 1
        if self.directories > self.limits.max_directories:
            raise _limit("plugin discovery directory budget exceeded")

    def add_entries(self, count: int) -> None:
        self.entries += count
        if self.entries > self.limits.max_entries:
            raise _limit("plugin discovery entry budget exceeded")

    def file(self, size: int) -> None:
        if size < 0 or size > self.limits.max_file_bytes:
            raise _limit("plugin discovery per-file byte budget exceeded")
        if self.aggregate_bytes + size > self.limits.max_aggregate_bytes:
            raise _limit("plugin discovery aggregate byte budget exceeded")
        self.files += 1
        if self.files > self.limits.max_files:
            raise _limit("plugin discovery file budget exceeded")
        self.aggregate_bytes += size

    def bundle(self) -> None:
        self.bundles += 1
        if self.bundles > self.limits.max_bundles:
            raise _limit("plugin discovery bundle budget exceeded")


def _limit(message: str) -> PluginSecurityError:
    return PluginSecurityError(message, code="plugin_discovery_limit")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _same_identity(expected: PluginPathIdentity, actual: os.stat_result) -> bool:
    return (
        expected.device,
        expected.inode,
        expected.mode,
    ) == (
        int(actual.st_dev),
        int(actual.st_ino),
        int(actual.st_mode),
    )


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _directory_identity(path: Path, relative: str) -> PluginPathIdentity:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PluginSecurityError(
            f"plugin discovery directory is unavailable: {path}",
            code="plugin_discovery_root_invalid",
        ) from exc
    if _is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise PluginSecurityError(
            f"plugin discovery directory is not a real directory: {path}",
            code="plugin_discovery_root_invalid",
        )
    try:
        with _pinned_parent(path / ".ace-plugin-discovery-probe"):
            after = path.lstat()
    except (FileConflictError, OSError) as exc:
        raise PluginSecurityError(
            f"plugin discovery directory identity cannot be pinned: {path}",
            code="plugin_discovery_root_invalid",
        ) from exc
    identity = PluginPathIdentity.from_stat(relative, before)
    if not _same_identity(identity, after):
        raise PluginSecurityError(
            f"plugin discovery directory identity changed: {path}",
            code="plugin_discovery_root_changed",
        )
    return identity


def _assert_directory_identity(path: Path, expected: PluginPathIdentity) -> None:
    try:
        actual = path.lstat()
    except OSError as exc:
        raise PluginSecurityError(
            f"plugin discovery directory disappeared: {path}",
            code="plugin_discovery_snapshot_stale",
        ) from exc
    if (
        _is_link_or_reparse(actual)
        or not stat.S_ISDIR(actual.st_mode)
        or not _same_identity(expected, actual)
    ):
        raise PluginSecurityError(
            f"plugin discovery directory changed: {path}",
            code="plugin_discovery_snapshot_stale",
        )


def _sorted_entries(parent: Path, expected: PluginPathIdentity, budget: _Budget) -> list[Path]:
    _assert_directory_identity(parent, expected)
    try:
        entries = sorted(parent.iterdir(), key=lambda item: (item.name.casefold(), item.name))
    except OSError as exc:
        raise PluginSecurityError(
            f"plugin discovery directory cannot be enumerated: {parent}",
            code="plugin_discovery_root_invalid",
        ) from exc
    budget.add_entries(len(entries))
    _assert_directory_identity(parent, expected)
    return entries


def _safe_child_directories(
    parent: Path,
    parent_identity: PluginPathIdentity,
    *,
    parent_relative: str,
    depth: int,
    budget: _Budget,
) -> list[tuple[Path, PluginPathIdentity]]:
    budget.depth(depth)
    result: list[tuple[Path, PluginPathIdentity]] = []
    for entry in _sorted_entries(parent, parent_identity, budget):
        try:
            info = entry.lstat()
        except OSError:
            continue
        # Discovery roots may contain unrelated links.  They are never followed
        # and therefore cannot create a plugin candidate.
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            continue
        relative = f"{parent_relative}/{entry.name}".strip("/")
        identity = PluginPathIdentity.from_stat(relative, info)
        budget.directory()
        result.append((entry, identity))
    return result


def _manifest_name(plugin_dir: Path) -> str | None:
    found: list[str] = []
    for name in ("plugin.yaml", "plugin.yml"):
        candidate = plugin_dir / name
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PluginSecurityError(
                f"plugin manifest is unavailable: {candidate}",
                code="plugin_discovery_root_invalid",
            ) from exc
        if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise PluginSecurityError(
                f"plugin manifest is not a regular file: {candidate}",
                code="plugin_path_unsafe",
            )
        found.append(name)
    if len(found) > 1:
        raise PluginSecurityError(
            f"plugin bundle has ambiguous manifests: {plugin_dir}",
            code="manifest_schema_invalid",
        )
    return found[0] if found else None


def _snapshot_member(
    *,
    root: Path,
    root_identity: PluginPathIdentity,
    plugin_dir: Path,
    plugin_identity: PluginPathIdentity,
    relative_path: str,
    key: str,
    source: str,
    candidate_depth: int,
    manifest_name: str,
    budget: _Budget,
) -> PluginDiscoveryMember:
    member_root_identity = replace(plugin_identity, relative_path="")
    directories: list[PluginPathIdentity] = [member_root_identity]
    files: list[PluginPathIdentity] = []
    manifest_bytes = b""
    manifest_digest = ""
    tree_parts: list[tuple[str, int, str]] = []

    def walk(
        directory: Path,
        directory_identity: PluginPathIdentity,
        relative_to_plugin: str,
        depth: int,
    ) -> None:
        nonlocal manifest_bytes, manifest_digest
        absolute_depth = candidate_depth + depth
        budget.depth(absolute_depth)
        for entry in _sorted_entries(directory, directory_identity, budget):
            try:
                info = entry.lstat()
            except OSError as exc:
                raise PluginSecurityError(
                    f"plugin member disappeared during discovery: {entry}",
                    code="plugin_discovery_snapshot_stale",
                ) from exc
            member_relative = f"{relative_to_plugin}/{entry.name}".strip("/")
            if _is_link_or_reparse(info):
                raise PluginSecurityError(
                    f"plugin tree contains a link or reparse point: {entry}",
                    code="plugin_path_unsafe",
                )
            if stat.S_ISDIR(info.st_mode):
                if entry.name == "__pycache__":
                    continue
                identity = PluginPathIdentity.from_stat(member_relative, info)
                budget.directory()
                directories.append(identity)
                walk(entry, identity, member_relative, depth + 1)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PluginSecurityError(
                    f"plugin tree contains a non-regular member: {entry}",
                    code="plugin_path_unsafe",
                )
            budget.file(int(info.st_size))
            if entry.suffix == ".pyc":
                continue
            try:
                version = snapshot_file(entry, max_bytes=budget.limits.max_file_bytes)
            except (FileConflictError, OSError, ValueError) as exc:
                raise PluginSecurityError(
                    f"plugin member cannot be snapshotted: {entry}",
                    code="plugin_discovery_snapshot_stale",
                ) from exc
            if (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
            ) != (
                version.device,
                version.inode,
                version.size,
                version.mtime_ns,
            ) or (os.name != "nt" and int(info.st_ctime_ns) != version.ctime_ns):
                raise PluginSecurityError(
                    f"plugin member changed during discovery: {entry}",
                    code="plugin_discovery_snapshot_stale",
                )
            identity = PluginPathIdentity(
                relative_path=member_relative,
                device=version.device,
                inode=version.inode,
                size=version.size,
                mtime_ns=version.mtime_ns,
                ctime_ns=version.ctime_ns,
                mode=version.mode,
                sha256=version.digest,
            )
            files.append(identity)
            if member_relative == manifest_name:
                manifest_bytes = version.data
                manifest_digest = version.digest
            ignored_tree_member = (
                entry.name in {PLUGIN_SIGNATURE_FILE, PLUGIN_PROVENANCE_FILE}
                or entry.suffix == ".pyc"
                or "__pycache__" in Path(member_relative).parts
            )
            if not ignored_tree_member:
                tree_parts.append((member_relative, version.size, version.digest))
        _assert_directory_identity(directory, directory_identity)

    walk(plugin_dir, member_root_identity, "", 0)
    _assert_directory_identity(root, root_identity)
    if not manifest_bytes or not manifest_digest:
        raise PluginSecurityError(
            f"plugin manifest changed during discovery: {plugin_dir}",
            code="plugin_discovery_snapshot_stale",
        )

    tree_digest = hashlib.sha256()
    file_by_path = {item.relative_path: item for item in files}
    for relative, size, digest in sorted(tree_parts):
        relative_bytes = relative.encode("utf-8")
        tree_digest.update(len(relative_bytes).to_bytes(4, "big"))
        tree_digest.update(relative_bytes)
        tree_digest.update(size.to_bytes(8, "big"))
        # The canonical tree digest hashes bytes, not their hexadecimal digest.
        # Re-read through the captured identity so validation cannot silently use
        # a different path object.
        identity = file_by_path[relative]
        expected = FileIdentity(
            path=_lexical_absolute(plugin_dir / Path(relative)),
            exists=True,
            device=identity.device,
            inode=identity.inode,
            size=identity.size,
            mtime_ns=identity.mtime_ns,
            ctime_ns=identity.ctime_ns,
        )
        try:
            data = snapshot_file(
                plugin_dir / Path(relative),
                max_bytes=budget.limits.max_file_bytes,
                expected_identity=expected,
            ).data
        except (FileConflictError, OSError, ValueError) as exc:
            raise PluginSecurityError(
                f"plugin member changed while computing tree digest: {relative}",
                code="plugin_discovery_snapshot_stale",
            ) from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise PluginSecurityError(
                f"plugin member digest changed while computing tree digest: {relative}",
                code="plugin_discovery_snapshot_stale",
            )
        tree_digest.update(data)

    return PluginDiscoveryMember(
        root_path=str(root),
        root_identity=root_identity,
        plugin_path=str(plugin_dir),
        relative_path=relative_path,
        key=key,
        source=source,
        depth=candidate_depth,
        manifest_relative_path=manifest_name,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_digest,
        tree_sha256=tree_digest.hexdigest(),
        directories=tuple(sorted(directories, key=lambda item: item.relative_path)),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
    )


def _snapshot_digest(
    roots: Iterable[PluginDiscoveryRoot],
    members: Iterable[PluginDiscoveryMember],
) -> str:
    digest = hashlib.sha256()
    for root in roots:
        digest.update(root.path.encode("utf-8"))
        digest.update(root.source.encode("utf-8"))
        digest.update(repr(root.identity).encode("utf-8"))
    for member in members:
        digest.update(member.root_path.encode("utf-8"))
        digest.update(member.relative_path.encode("utf-8"))
        digest.update(member.source.encode("utf-8"))
        digest.update(member.manifest_sha256.encode("ascii"))
        digest.update(member.tree_sha256.encode("ascii"))
        for identity in (*member.directories, *member.files):
            digest.update(repr(identity).encode("utf-8"))
    return digest.hexdigest()


def snapshot_plugin_roots(
    roots: Iterable[tuple[Path, str]],
    *,
    limits: PluginDiscoveryLimits,
    request_scope: tuple[str, str, str] | None,
) -> PluginDiscoverySnapshot:
    """Discover all plugin bundles into one bounded immutable snapshot."""

    roots_list = [(_lexical_absolute(path), str(source)) for path, source in roots]
    unique = {os.path.normcase(str(path)) for path, _source in roots_list}
    if len(unique) > limits.max_roots:
        raise _limit("plugin discovery root budget exceeded")

    budget = _Budget(limits)
    root_snapshots: list[PluginDiscoveryRoot] = []
    candidates: list[
        tuple[
            Path,
            PluginPathIdentity,
            Path,
            PluginPathIdentity,
            str,
            str,
            str,
            int,
        ]
    ] = []
    seen_roots: set[str] = set()
    for root, source in roots_list:
        root_key = os.path.normcase(str(root))
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        root_identity = _directory_identity(root, "")
        budget.directory()
        root_snapshots.append(
            PluginDiscoveryRoot(path=str(root), source=source, identity=root_identity)
        )
        for child, child_identity in _safe_child_directories(
            root,
            root_identity,
            parent_relative="",
            depth=1,
            budget=budget,
        ):
            child_manifest = _manifest_name(child)
            if child_manifest:
                budget.bundle()
                candidates.append(
                    (
                        root,
                        root_identity,
                        child,
                        child_identity,
                        child.name,
                        child.name,
                        source,
                        1,
                    )
                )
                continue
            for grandchild, grandchild_identity in _safe_child_directories(
                child,
                child_identity,
                parent_relative=child.name,
                depth=2,
                budget=budget,
            ):
                grandchild_manifest = _manifest_name(grandchild)
                if not grandchild_manifest:
                    continue
                budget.bundle()
                candidates.append(
                    (
                        root,
                        root_identity,
                        grandchild,
                        grandchild_identity,
                        f"{child.name}/{grandchild.name}",
                        f"{child.name}/{grandchild.name}",
                        source,
                        2,
                    )
                )
        _assert_directory_identity(root, root_identity)

    members: list[PluginDiscoveryMember] = []
    for (
        root,
        root_identity,
        plugin_dir,
        plugin_identity,
        relative_path,
        key,
        source,
        depth,
    ) in candidates:
        manifest_name = _manifest_name(plugin_dir)
        if manifest_name is None:
            raise PluginSecurityError(
                f"plugin manifest disappeared during discovery: {plugin_dir}",
                code="plugin_discovery_snapshot_stale",
            )
        members.append(
            _snapshot_member(
                root=root,
                root_identity=root_identity,
                plugin_dir=plugin_dir,
                plugin_identity=plugin_identity,
                relative_path=relative_path,
                key=key,
                source=source,
                candidate_depth=depth,
                manifest_name=manifest_name,
                budget=budget,
            )
        )

    roots_tuple = tuple(root_snapshots)
    members_tuple = tuple(sorted(members, key=lambda item: (item.root_path, item.key)))
    return PluginDiscoverySnapshot(
        request_scope=request_scope,
        snapshot_id=_snapshot_digest(roots_tuple, members_tuple),
        roots=roots_tuple,
        members=members_tuple,
        directories_seen=budget.directories,
        entries_seen=budget.entries,
        files_seen=budget.files,
        aggregate_bytes=budget.aggregate_bytes,
    )


def validate_plugin_member(
    member: PluginDiscoveryMember,
    *,
    limits: PluginDiscoveryLimits,
) -> None:
    """Reject any identity, member-set, or digest change since discovery."""

    root = Path(member.root_path)
    plugin_dir = Path(member.plugin_path)
    _assert_directory_identity(root, member.root_identity)
    expected_plugin_identity = next(
        (item for item in member.directories if item.relative_path == ""),
        None,
    )
    if expected_plugin_identity is None:
        raise PluginSecurityError(
            "plugin discovery snapshot is incomplete",
            code="plugin_discovery_snapshot_stale",
        )
    _assert_directory_identity(plugin_dir, expected_plugin_identity)
    budget = _Budget(limits)
    budget.directory()
    current = _snapshot_member(
        root=root,
        root_identity=member.root_identity,
        plugin_dir=plugin_dir,
        plugin_identity=expected_plugin_identity,
        relative_path=member.relative_path,
        key=member.key,
        source=member.source,
        candidate_depth=member.depth,
        manifest_name=member.manifest_relative_path,
        budget=budget,
    )
    current_directories = tuple(
        (item.relative_path, item.device, item.inode, item.mode)
        for item in current.directories
    )
    expected_directories = tuple(
        (item.relative_path, item.device, item.inode, item.mode)
        for item in member.directories
    )
    if (
        current.root_path != member.root_path
        or current.plugin_path != member.plugin_path
        or current.relative_path != member.relative_path
        or current.key != member.key
        or current.source != member.source
        or current.depth != member.depth
        or current.manifest_relative_path != member.manifest_relative_path
        or current.manifest_bytes != member.manifest_bytes
        or current.manifest_sha256 != member.manifest_sha256
        or current.tree_sha256 != member.tree_sha256
        or current_directories != expected_directories
        or current.files != member.files
    ):
        raise PluginSecurityError(
            f"plugin discovery snapshot is stale: {plugin_dir}",
            code="plugin_discovery_snapshot_stale",
        )


__all__ = [
    "PluginDiscoveryLimits",
    "PluginDiscoveryMember",
    "PluginDiscoveryRoot",
    "PluginDiscoverySnapshot",
    "PluginPathIdentity",
    "snapshot_plugin_roots",
    "validate_plugin_member",
]
