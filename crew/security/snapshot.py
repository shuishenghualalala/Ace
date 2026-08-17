"""Host-signed, immutable authorization facts consumed by process launch boundaries."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.security.actions import (
    ActionScope,
    NormalizedAction,
    security_context_digest,
    serialize_normalized_action,
)
from crew.security.context import SecurityContext
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    NetworkAccess,
    PermissionProfile,
    PermissionProfileKind,
    SandboxablePreference,
    resolve_sandboxable_preference,
)

AUTHORIZATION_SNAPSHOT_VERSION = 2
_SNAPSHOT_MAC_CONTEXT = b"ace-authorization-snapshot-v2\x00"
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_HOST_AUTHORITY_SECRET = secrets.token_bytes(32)
_MAX_CONSUMED_SNAPSHOTS = 100_000
_MAX_HELPER_IDENTITY_BYTES = 512 * 1024 * 1024
_CONSUMED_SNAPSHOT_NONCES: set[str] = set()
_REPLAY_LOCK = threading.Lock()


class AuthorizationSnapshotError(RuntimeError):
    """Raised when authorization facts are malformed, forged, stale, or mutated."""


@dataclass(frozen=True, order=True)
class SnapshotNetworkRule:
    host: str
    port: int
    protocol: str
    allow: bool
    allow_private: bool
    escalatable: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "allow": self.allow,
            "allow_private": self.allow_private,
            "escalatable": self.escalatable,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
        }

    @classmethod
    def from_payload(cls, value: object) -> SnapshotNetworkRule:
        expected = {
            "allow",
            "allow_private",
            "escalatable",
            "host",
            "port",
            "protocol",
        }
        payload = _strict_mapping(value, expected, "network rule")
        host = _required_text(payload.get("host"), "network rule host")
        protocol = _required_text(payload.get("protocol"), "network rule protocol")
        port = payload.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise AuthorizationSnapshotError("network rule port is invalid")
        for name in ("allow", "allow_private", "escalatable"):
            if not isinstance(payload.get(name), bool):
                raise AuthorizationSnapshotError(f"network rule {name} is invalid")
        return cls(
            host=host,
            port=port,
            protocol=protocol,
            allow=payload["allow"],
            allow_private=payload["allow_private"],
            escalatable=payload["escalatable"],
        )


@dataclass(frozen=True)
class AuthorizationSnapshot:
    """Complete immutable authority for one exact process execution."""

    version: int
    nonce: str
    action_digest: str
    action_payload: str
    profile_kind: str
    profile_payload: str
    sandbox_preference: str
    sandboxed: bool
    sandbox_system_surface: str
    additional_permissions_payload: str
    owner_account_id: str
    workspace_id: str
    session_id: str
    task_id: str
    argv: tuple[str, ...]
    cwd: str
    environment_digest: str
    helper_argv: tuple[str, ...]
    helper_path: str
    helper_digest: str
    readable_roots: tuple[str, ...]
    writable_roots: tuple[str, ...]
    denied_roots: tuple[str, ...]
    network_rules: tuple[SnapshotNetworkRule, ...]
    allow_local_binding: bool
    action_scope_digest: str = ""
    turn_digest: str = ""
    context_digest: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action_digest": self.action_digest,
            "action_payload": self.action_payload,
            "additional_permissions_payload": self.additional_permissions_payload,
            "allow_local_binding": self.allow_local_binding,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "denied_roots": list(self.denied_roots),
            "environment_digest": self.environment_digest,
            "helper_argv": list(self.helper_argv),
            "helper_digest": self.helper_digest,
            "helper_path": self.helper_path,
            "network_rules": [rule.to_payload() for rule in self.network_rules],
            "nonce": self.nonce,
            "owner_account_id": self.owner_account_id,
            "profile_kind": self.profile_kind,
            "profile_payload": self.profile_payload,
            "readable_roots": list(self.readable_roots),
            "sandbox_preference": self.sandbox_preference,
            "sandbox_system_surface": self.sandbox_system_surface,
            "sandboxed": self.sandboxed,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "version": self.version,
            "workspace_id": self.workspace_id,
            "writable_roots": list(self.writable_roots),
        }
        if self.action_scope_digest:
            payload["action_scope_digest"] = self.action_scope_digest
        if self.turn_digest:
            payload["turn_digest"] = self.turn_digest
        if self.context_digest:
            payload["context_digest"] = self.context_digest
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, value: object) -> AuthorizationSnapshot:
        expected = {
            "action_digest",
            "action_scope_digest",
            "action_payload",
            "additional_permissions_payload",
            "allow_local_binding",
            "argv",
            "cwd",
            "denied_roots",
            "environment_digest",
            "helper_argv",
            "helper_digest",
            "helper_path",
            "network_rules",
            "nonce",
            "owner_account_id",
            "profile_kind",
            "profile_payload",
            "readable_roots",
            "sandbox_preference",
            "sandbox_system_surface",
            "sandboxed",
            "session_id",
            "task_id",
            "turn_digest",
            "version",
            "workspace_id",
            "writable_roots",
            "context_digest",
        }
        payload = _strict_mapping(
            value,
            expected,
            "authorization snapshot",
            optional={"action_scope_digest", "turn_digest", "context_digest"},
        )
        version = payload.get("version")
        if version != AUTHORIZATION_SNAPSHOT_VERSION:
            raise AuthorizationSnapshotError("authorization snapshot version is unsupported")
        allow_local_binding = payload.get("allow_local_binding")
        if not isinstance(allow_local_binding, bool):
            raise AuthorizationSnapshotError("authorization snapshot local-binding flag is invalid")
        sandboxed = payload.get("sandboxed")
        if not isinstance(sandboxed, bool):
            raise AuthorizationSnapshotError(
                "authorization snapshot sandbox choice is invalid"
            )
        sandbox_preference = _required_text(
            payload.get("sandbox_preference"),
            "sandbox preference",
        )
        try:
            SandboxablePreference(sandbox_preference)
        except ValueError as exc:
            raise AuthorizationSnapshotError(
                "authorization snapshot sandbox preference is invalid"
            ) from exc
        raw_network = payload.get("network_rules")
        if not isinstance(raw_network, list):
            raise AuthorizationSnapshotError("authorization snapshot network rules are invalid")
        return cls(
            version=version,
            nonce=_required_text(payload.get("nonce"), "snapshot nonce"),
            action_digest=_required_text(payload.get("action_digest"), "action digest"),
            action_scope_digest=_text(payload.get("action_scope_digest", ""), "action scope digest"),
            action_payload=_required_text(payload.get("action_payload"), "action payload"),
            profile_kind=_required_text(payload.get("profile_kind"), "profile kind"),
            profile_payload=_required_text(payload.get("profile_payload"), "profile payload"),
            sandbox_preference=sandbox_preference,
            sandboxed=sandboxed,
            sandbox_system_surface=_text(
                payload.get("sandbox_system_surface"),
                "sandbox system surface",
            ),
            additional_permissions_payload=_required_text(
                payload.get("additional_permissions_payload"),
                "additional permissions payload",
            ),
            owner_account_id=_required_text(payload.get("owner_account_id"), "owner"),
            workspace_id=_required_text(payload.get("workspace_id"), "workspace"),
            session_id=_text(payload.get("session_id"), "session"),
            task_id=_text(payload.get("task_id"), "task"),
            turn_digest=_text(payload.get("turn_digest", ""), "turn digest"),
            argv=_string_tuple(payload.get("argv"), "argv", allow_empty=False),
            cwd=_required_text(payload.get("cwd"), "cwd"),
            environment_digest=_required_text(
                payload.get("environment_digest"),
                "environment digest",
            ),
            helper_argv=_string_tuple(payload.get("helper_argv"), "helper argv"),
            helper_path=_text(payload.get("helper_path"), "helper path"),
            helper_digest=_text(payload.get("helper_digest"), "helper digest"),
            readable_roots=_string_tuple(payload.get("readable_roots"), "readable roots"),
            writable_roots=_string_tuple(payload.get("writable_roots"), "writable roots"),
            denied_roots=_string_tuple(payload.get("denied_roots"), "denied roots"),
            network_rules=tuple(
                SnapshotNetworkRule.from_payload(rule) for rule in raw_network
            ),
            allow_local_binding=allow_local_binding,
            context_digest=_text(payload.get("context_digest", ""), "context digest"),
        )


@dataclass(frozen=True)
class SignedAuthorizationSnapshot:
    """A snapshot plus its canonical digest and host HMAC."""

    snapshot: AuthorizationSnapshot
    digest: str
    mac: str

    def to_payload(self) -> dict[str, object]:
        return {
            "snapshot": self.snapshot.to_payload(),
            "snapshot_digest": self.digest,
            "snapshot_mac": self.mac,
        }

    @classmethod
    def from_payload(cls, value: object) -> SignedAuthorizationSnapshot:
        payload = _strict_mapping(
            value,
            {"snapshot", "snapshot_digest", "snapshot_mac"},
            "signed authorization snapshot",
        )
        return cls(
            snapshot=AuthorizationSnapshot.from_payload(payload.get("snapshot")),
            digest=_required_text(payload.get("snapshot_digest"), "snapshot digest"),
            mac=_required_text(payload.get("snapshot_mac"), "snapshot MAC"),
        )


def canonical_json_bytes(value: object) -> bytes:
    """Serialize canonical protocol data with stable UTF-8 JSON rules."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AuthorizationSnapshotError("authorization data is not canonical JSON") from exc
    return encoded.encode("utf-8")


def environment_digest(environment: Mapping[str, str]) -> str:
    """Hash an exact environment without placing values in the snapshot."""
    normalized: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value:
            raise AuthorizationSnapshotError("authorization environment must contain strings")
        normalized[key] = value
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def issue_authorization_snapshot(
    *,
    context: SecurityContext,
    action: NormalizedAction,
    profile: PermissionProfile,
    sandbox_preference: SandboxablePreference = SandboxablePreference.AUTO,
    sandboxed: bool | None = None,
    sandbox_system_surface: str = "",
    additional_permissions: AdditionalPermissionProfile,
    action_scope: ActionScope | None = None,
    turn_digest: str = "",
    context_digest: str = "",
    argv: Sequence[str],
    cwd: str | Path,
    environment: Mapping[str, str],
    helper_argv: Sequence[str | Path],
    trusted_readable_roots: Sequence[str | Path] = (),
) -> SignedAuthorizationSnapshot:
    """Create and sign the sole launch authority after an action is authorized."""
    if not isinstance(context, SecurityContext):
        raise AuthorizationSnapshotError("security context is invalid")
    if not isinstance(action, NormalizedAction):
        raise AuthorizationSnapshotError("normalized action is invalid")
    if not isinstance(profile, PermissionProfile):
        raise AuthorizationSnapshotError("permission profile is invalid")
    try:
        resolved_sandboxed = resolve_sandboxable_preference(
            profile.kind,
            sandbox_preference,
            system_surface=sandbox_system_surface,
        )
    except (TypeError, ValueError) as exc:
        raise AuthorizationSnapshotError(
            f"sandbox preference is invalid: {exc}"
        ) from exc
    if sandboxed is None:
        sandboxed = resolved_sandboxed
    if not isinstance(sandboxed, bool) or sandboxed is not resolved_sandboxed:
        raise AuthorizationSnapshotError(
            "sandbox preference and final choice are inconsistent"
        )
    if not isinstance(additional_permissions, AdditionalPermissionProfile):
        raise AuthorizationSnapshotError("additional permissions are invalid")
    if action_scope is not None and (
        not isinstance(action_scope, ActionScope) or action_scope.action.digest != action.digest
    ):
        raise AuthorizationSnapshotError("authorization action scope does not match action")
    owner = str(context.owner_account_id).strip()
    if not owner:
        raise AuthorizationSnapshotError("authorization owner is missing")
    workspace = str(context.workspace_id).strip()
    if not workspace:
        raise AuthorizationSnapshotError("authorization workspace is missing")
    normalized_argv = tuple(_required_text(part, "argv token") for part in argv)
    if not normalized_argv:
        raise AuthorizationSnapshotError("authorization argv is empty")
    normalized_cwd = str(Path(cwd).expanduser().resolve(strict=True))
    if tuple(action.argv) != normalized_argv or action.cwd != normalized_cwd:
        raise AuthorizationSnapshotError("authorized action does not match final argv/cwd")

    helper_parts = tuple(str(Path(part).expanduser()) for part in helper_argv)
    helper_path = ""
    helper_digest = ""
    if sandboxed:
        if profile.kind is not PermissionProfileKind.MANAGED:
            raise AuthorizationSnapshotError(
                "sandboxed authorization requires a managed profile"
            )
        if not helper_parts:
            raise AuthorizationSnapshotError("managed authorization is missing its helper")
        helper = Path(helper_parts[0]).resolve(strict=True)
        if not helper.is_file():
            raise AuthorizationSnapshotError("managed authorization helper is not a file")
        helper_path = str(helper)
        helper_parts = (helper_path, *helper_parts[1:])
        helper_digest = _verified_file_digest(helper)
    elif profile.kind is PermissionProfileKind.DISABLED:
        if helper_parts:
            raise AuthorizationSnapshotError("disabled authorization cannot name a helper")
    else:
        raise AuthorizationSnapshotError("permission profile kind is unsupported")

    readable, writable, denied = _filesystem_roots(
        profile,
        additional_permissions,
        trusted_readable_roots,
    )
    network_rules = _network_rules(profile, additional_permissions)
    profile_payload = canonical_json_bytes(_serialize_profile(profile)).decode("utf-8")
    additional_payload = canonical_json_bytes(
        _serialize_additional_permissions(additional_permissions)
    ).decode("utf-8")
    action_payload = canonical_json_bytes(serialize_normalized_action(action)).decode("utf-8")
    nonce = secrets.token_hex(16)
    if not _NONCE.fullmatch(nonce):
        raise AuthorizationSnapshotError("authorization snapshot nonce generation failed")
    snapshot = AuthorizationSnapshot(
        version=AUTHORIZATION_SNAPSHOT_VERSION,
        nonce=nonce,
        action_digest=action.digest,
        action_scope_digest=action_scope.digest if action_scope is not None else "",
        action_payload=action_payload,
        profile_kind=profile.kind.value,
        profile_payload=profile_payload,
        sandbox_preference=sandbox_preference.value,
        sandboxed=sandboxed,
        sandbox_system_surface=str(sandbox_system_surface),
        additional_permissions_payload=additional_payload,
        owner_account_id=owner,
        workspace_id=workspace,
        session_id=str(context.session_id),
        task_id=str(context.task_id),
        turn_digest=turn_digest or (action_scope.turn_digest if action_scope is not None else ""),
        argv=normalized_argv,
        cwd=normalized_cwd,
        environment_digest=environment_digest(environment),
        helper_argv=helper_parts,
        helper_path=helper_path,
        helper_digest=helper_digest,
        readable_roots=readable,
        writable_roots=writable,
        denied_roots=denied,
        network_rules=network_rules,
        allow_local_binding=(
            profile.allow_local_binding or additional_permissions.allow_local_binding
        ),
        context_digest=context_digest or security_context_digest(context),
    )
    digest = hashlib.sha256(snapshot.canonical_bytes()).hexdigest()
    mac = _snapshot_mac(digest)
    return SignedAuthorizationSnapshot(snapshot=snapshot, digest=digest, mac=mac)


def verify_authorization_snapshot(
    signed: SignedAuthorizationSnapshot,
    *,
    environment: Mapping[str, str] | None = None,
    expected_owner_account_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_session_id: str | None = None,
    expected_task_id: str | None = None,
    expected_nonce: str | None = None,
    expected_action_scope_digest: str | None = None,
    expected_turn_digest: str | None = None,
    expected_context_digest: str | None = None,
    verification_key: bytes | None = None,
) -> AuthorizationSnapshot:
    """Verify HMAC, identity, helper identity, and optional final environment."""
    if not isinstance(signed, SignedAuthorizationSnapshot):
        raise AuthorizationSnapshotError("signed authorization snapshot is invalid")
    snapshot = signed.snapshot
    if snapshot.version != AUTHORIZATION_SNAPSHOT_VERSION:
        raise AuthorizationSnapshotError("authorization snapshot version is unsupported")
    if not _NONCE.fullmatch(snapshot.nonce):
        raise AuthorizationSnapshotError("authorization snapshot nonce is invalid")
    if not _HEX_256.fullmatch(signed.digest) or not _HEX_256.fullmatch(signed.mac):
        raise AuthorizationSnapshotError("authorization snapshot signature is invalid")
    actual_digest = hashlib.sha256(snapshot.canonical_bytes()).hexdigest()
    if not hmac.compare_digest(actual_digest, signed.digest):
        raise AuthorizationSnapshotError("authorization snapshot digest mismatch")
    if not hmac.compare_digest(
        _snapshot_mac(actual_digest, verification_key=verification_key),
        signed.mac,
    ):
        raise AuthorizationSnapshotError("authorization snapshot MAC mismatch")
    if expected_nonce is not None and not hmac.compare_digest(snapshot.nonce, expected_nonce):
        raise AuthorizationSnapshotError("authorization snapshot nonce mismatch")
    for actual, expected, label in (
        (snapshot.owner_account_id, expected_owner_account_id, "owner"),
        (snapshot.workspace_id, expected_workspace_id, "workspace"),
        (snapshot.session_id, expected_session_id, "session"),
        (snapshot.task_id, expected_task_id, "task"),
        (snapshot.action_scope_digest, expected_action_scope_digest, "action scope"),
        (snapshot.turn_digest, expected_turn_digest, "turn"),
        (snapshot.context_digest, expected_context_digest, "context"),
    ):
        if expected is not None and actual != str(expected):
            raise AuthorizationSnapshotError(f"authorization snapshot {label} mismatch")
    if environment is not None and not hmac.compare_digest(
        snapshot.environment_digest,
        environment_digest(environment),
    ):
        raise AuthorizationSnapshotError("authorization snapshot environment mismatch")
    _verify_internal_consistency(snapshot)
    _verify_helper_identity(snapshot)
    return snapshot


def consume_authorization_snapshot(
    signed: SignedAuthorizationSnapshot,
    *,
    environment: Mapping[str, str] | None = None,
    expected_owner_account_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_session_id: str | None = None,
    expected_task_id: str | None = None,
    expected_nonce: str | None = None,
    expected_action_scope_digest: str | None = None,
    expected_turn_digest: str | None = None,
    expected_context_digest: str | None = None,
    verification_key: bytes | None = None,
) -> AuthorizationSnapshot:
    """Atomically verify and consume a one-shot process authorization."""
    snapshot = verify_authorization_snapshot(
        signed,
        environment=environment,
        expected_owner_account_id=expected_owner_account_id,
        expected_workspace_id=expected_workspace_id,
        expected_session_id=expected_session_id,
        expected_task_id=expected_task_id,
        expected_nonce=expected_nonce,
        expected_action_scope_digest=expected_action_scope_digest,
        expected_turn_digest=expected_turn_digest,
        expected_context_digest=expected_context_digest,
        verification_key=verification_key,
    )
    try:
        with _REPLAY_LOCK:
            if not isinstance(_CONSUMED_SNAPSHOT_NONCES, set):
                raise AuthorizationSnapshotError(
                    "authorization snapshot replay state is unavailable"
                )
            if snapshot.nonce in _CONSUMED_SNAPSHOT_NONCES:
                raise AuthorizationSnapshotError("authorization snapshot replay detected")
            if len(_CONSUMED_SNAPSHOT_NONCES) >= _MAX_CONSUMED_SNAPSHOTS:
                raise AuthorizationSnapshotError(
                    "authorization snapshot replay state is exhausted"
                )
            _CONSUMED_SNAPSHOT_NONCES.add(snapshot.nonce)
    except AuthorizationSnapshotError:
        raise
    except Exception as exc:
        raise AuthorizationSnapshotError(
            "authorization snapshot replay state is unavailable"
        ) from exc
    return snapshot


def delegate_authorization_snapshot(
    signed: SignedAuthorizationSnapshot,
    *,
    verification_key: bytes,
) -> SignedAuthorizationSnapshot:
    """Re-sign a verified snapshot for one isolated child verifier."""
    verify_authorization_snapshot(signed)
    if not isinstance(verification_key, bytes) or len(verification_key) != 32:
        raise AuthorizationSnapshotError(
            "delegated authorization verification key is invalid"
        )
    return SignedAuthorizationSnapshot(
        snapshot=signed.snapshot,
        digest=signed.digest,
        mac=_snapshot_mac(
            signed.digest,
            verification_key=verification_key,
        ),
    )


# Shared T02 contract names; launch callers keep their existing API.
PermissionSnapshot = AuthorizationSnapshot
SignedPermissionSnapshot = SignedAuthorizationSnapshot
issue_permission_snapshot = issue_authorization_snapshot
verify_permission_snapshot = verify_authorization_snapshot
consume_permission_snapshot = consume_authorization_snapshot


def _verify_internal_consistency(snapshot: AuthorizationSnapshot) -> None:
    if not _HEX_256.fullmatch(snapshot.action_digest):
        raise AuthorizationSnapshotError("authorization action digest is invalid")
    if not _HEX_256.fullmatch(snapshot.environment_digest):
        raise AuthorizationSnapshotError("authorization environment digest is invalid")
    for value, label in (
        (snapshot.action_scope_digest, "action scope digest"),
        (snapshot.turn_digest, "turn digest"),
        (snapshot.context_digest, "context digest"),
    ):
        if value and not _HEX_256.fullmatch(value):
            raise AuthorizationSnapshotError(f"authorization {label} is invalid")
    try:
        action = json.loads(snapshot.action_payload)
        profile = json.loads(snapshot.profile_payload)
        additional = json.loads(snapshot.additional_permissions_payload)
    except json.JSONDecodeError as exc:
        raise AuthorizationSnapshotError("authorization canonical payload is invalid") from exc
    if not isinstance(action, dict) or not isinstance(profile, dict) or not isinstance(additional, dict):
        raise AuthorizationSnapshotError("authorization canonical payload has an invalid type")
    if action.get("argv") != list(snapshot.argv) or action.get("cwd") != snapshot.cwd:
        raise AuthorizationSnapshotError("authorization action and spawn facts differ")
    executable_digest = action.get("executable_digest") or ""
    command_identities = action.get("command_identities", [])
    if not isinstance(executable_digest, str) or (
        executable_digest and not _HEX_256.fullmatch(executable_digest)
    ):
        raise AuthorizationSnapshotError("authorization executable identity is invalid")
    if not isinstance(command_identities, list):
        raise AuthorizationSnapshotError("authorization command identities are invalid")
    if executable_digest:
        executable = action.get("executable")
        if not isinstance(executable, str) or action.get("argv", [None])[0] != executable:
            raise AuthorizationSnapshotError(
                "authorization executable identity does not match argv"
            )
        _verify_bound_file_identity(executable, executable_digest, "authorization executable")
    for identity in command_identities:
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or not isinstance(identity[0], str)
            or not isinstance(identity[1], str)
            or not _HEX_256.fullmatch(identity[1])
        ):
            raise AuthorizationSnapshotError("authorization command identity is invalid")
        _verify_bound_file_identity(identity[0], identity[1], "authorization command")
    if profile.get("kind") != snapshot.profile_kind:
        raise AuthorizationSnapshotError("authorization profile facts differ")
    try:
        kind = PermissionProfileKind(snapshot.profile_kind)
    except ValueError as exc:
        raise AuthorizationSnapshotError("authorization profile kind is invalid") from exc
    try:
        preference = SandboxablePreference(snapshot.sandbox_preference)
        expected_sandboxed = resolve_sandboxable_preference(
            kind,
            preference,
            system_surface=snapshot.sandbox_system_surface,
        )
    except (TypeError, ValueError) as exc:
        raise AuthorizationSnapshotError(
            "authorization sandbox preference is invalid"
        ) from exc
    if (
        not isinstance(snapshot.sandboxed, bool)
        or snapshot.sandboxed is not expected_sandboxed
    ):
        raise AuthorizationSnapshotError(
            "authorization sandbox choice is inconsistent"
        )
    if snapshot.sandboxed and (
        not snapshot.helper_argv
        or snapshot.helper_argv[0] != snapshot.helper_path
        or not _HEX_256.fullmatch(snapshot.helper_digest)
    ):
        raise AuthorizationSnapshotError("managed authorization helper facts are invalid")
    if not snapshot.sandboxed and (
        snapshot.helper_argv or snapshot.helper_path or snapshot.helper_digest
    ):
        raise AuthorizationSnapshotError("disabled authorization contains helper authority")
    if tuple(sorted(set(snapshot.readable_roots))) != snapshot.readable_roots:
        raise AuthorizationSnapshotError("authorization readable roots are not canonical")
    if tuple(sorted(set(snapshot.writable_roots))) != snapshot.writable_roots:
        raise AuthorizationSnapshotError("authorization writable roots are not canonical")
    if tuple(sorted(set(snapshot.denied_roots))) != snapshot.denied_roots:
        raise AuthorizationSnapshotError("authorization denied roots are not canonical")
    if tuple(sorted(set(snapshot.network_rules))) != snapshot.network_rules:
        raise AuthorizationSnapshotError("authorization network rules are not canonical")


def _verify_helper_identity(snapshot: AuthorizationSnapshot) -> None:
    if not snapshot.sandboxed:
        return
    try:
        helper = Path(snapshot.helper_path).resolve(strict=True)
        digest = _verified_file_digest(helper)
    except OSError as exc:
        raise AuthorizationSnapshotError("authorization helper is unavailable") from exc
    if str(helper) != snapshot.helper_path or not hmac.compare_digest(
        digest,
        snapshot.helper_digest,
    ):
        raise AuthorizationSnapshotError("authorization helper identity mismatch")


def _verify_bound_file_identity(path: str, expected_digest: str, label: str) -> None:
    try:
        target = Path(path).expanduser().resolve(strict=True)
        actual_digest = _verified_file_digest(target)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorizationSnapshotError(f"{label} is unavailable") from exc
    if str(target) != path or not hmac.compare_digest(actual_digest, expected_digest):
        raise AuthorizationSnapshotError(f"{label} identity changed")


def _verified_file_digest(path: str | Path) -> str:
    """Hash one regular file through an identity-checked handle."""
    target = Path(path)
    before = os.lstat(target)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & reparse_flag
        or not stat.S_ISREG(before.st_mode)
        or before.st_size < 0
        or before.st_size > _MAX_HELPER_IDENTITY_BYTES
    ):
        raise AuthorizationSnapshotError("authorization helper identity is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_file_attributes", 0) & reparse_flag
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > _MAX_HELPER_IDENTITY_BYTES
        ):
            raise AuthorizationSnapshotError(
                "authorization helper identity changed while opening"
            )
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_HELPER_IDENTITY_BYTES:
                raise AuthorizationSnapshotError(
                    "authorization helper exceeds identity byte limit"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AuthorizationSnapshotError(
                "authorization helper changed while hashing"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _snapshot_mac(digest: str, *, verification_key: bytes | None = None) -> str:
    key = (
        _host_signing_key("authorization-snapshot")
        if verification_key is None
        else verification_key
    )
    if not isinstance(key, bytes) or len(key) != 32:
        raise AuthorizationSnapshotError("authorization snapshot signing key is invalid")
    return hmac.new(
        key,
        _SNAPSHOT_MAC_CONTEXT + digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _host_signing_key(purpose: str) -> bytes:
    """Derive a process-local purpose key; bridge keys cross only through inherited memory."""
    if (
        not isinstance(_HOST_AUTHORITY_SECRET, bytes)
        or len(_HOST_AUTHORITY_SECRET) != 32
        or not isinstance(purpose, str)
        or not purpose
        or "\x00" in purpose
    ):
        raise AuthorizationSnapshotError("host authorization signing key is unavailable")
    return hmac.new(
        _HOST_AUTHORITY_SECRET,
        b"ace-host-authority-v1\x00" + str(purpose).encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _filesystem_roots(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile,
    trusted_readable_roots: Sequence[str | Path],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    readable = {
        str(Path(root).expanduser().resolve(strict=False))
        for root in trusted_readable_roots
    }
    writable: set[str] = set()
    denied: set[str] = set()
    for entry in (*profile.filesystem, *additional.filesystem):
        root = str(entry.root.expanduser().resolve(strict=False))
        if entry.access is FilesystemAccess.READ_WRITE:
            writable.add(root)
        elif entry.access is FilesystemAccess.READ and entry.escalatable:
            readable.add(root)
        elif entry.access is FilesystemAccess.DENY:
            denied.add(root)
    return tuple(sorted(readable)), tuple(sorted(writable)), tuple(sorted(denied))


def _network_rules(
    profile: PermissionProfile,
    additional: AdditionalPermissionProfile,
) -> tuple[SnapshotNetworkRule, ...]:
    rules = {
        SnapshotNetworkRule(
            host=entry.host,
            port=entry.port,
            protocol=entry.protocol,
            allow=entry.access is NetworkAccess.ALLOW,
            allow_private=entry.allow_private,
            escalatable=entry.escalatable,
        )
        for entry in (*profile.network_entries, *additional.network)
    }
    return tuple(sorted(rules))


def _serialize_profile(profile: PermissionProfile) -> dict[str, object]:
    filesystem = sorted(
        (
            {
                "access": entry.access.value,
                "escalatable": entry.escalatable,
                "root": str(entry.root),
            }
            for entry in profile.filesystem
        ),
        key=lambda entry: (
            str(entry["root"]),
            str(entry["access"]),
            bool(entry["escalatable"]),
        ),
    )
    filesystem_globs = sorted(
        (
            {
                "access": entry.access.value,
                "pattern": entry.pattern,
                "root": str(entry.root),
            }
            for entry in profile.filesystem_globs
        ),
        key=lambda entry: (
            str(entry["root"]),
            str(entry["pattern"]),
            str(entry["access"]),
        ),
    )
    network_entries = sorted(
        (
            {
                "access": entry.access.value,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
            }
            for entry in profile.network_entries
        ),
        key=lambda entry: (
            str(entry["host"]),
            int(entry["port"]),
            str(entry["protocol"]),
            str(entry["access"]),
        ),
    )
    return {
        "allow_local_binding": profile.allow_local_binding,
        "filesystem": filesystem,
        "filesystem_globs": filesystem_globs,
        "kind": profile.kind.value,
        "network": profile.network.value,
        "network_entries": network_entries,
    }


def _serialize_additional_permissions(
    profile: AdditionalPermissionProfile,
) -> dict[str, object]:
    filesystem = sorted(
        (
            {
                "access": entry.access.value,
                "escalatable": entry.escalatable,
                "root": str(entry.root),
            }
            for entry in profile.filesystem
        ),
        key=lambda entry: (
            str(entry["root"]),
            str(entry["access"]),
            bool(entry["escalatable"]),
        ),
    )
    network = sorted(
        (
            {
                "access": entry.access.value,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
            }
            for entry in profile.network
        ),
        key=lambda entry: (
            str(entry["host"]),
            int(entry["port"]),
            str(entry["protocol"]),
            str(entry["access"]),
        ),
    )
    return {
        "allow_local_binding": profile.allow_local_binding,
        "filesystem": filesystem,
        "network": network,
    }


def _strict_mapping(
    value: object,
    expected: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationSnapshotError(f"{label} must be an object")
    optional = optional or set()
    unknown = set(value) - expected
    missing = (expected - optional) - set(value)
    if unknown:
        raise AuthorizationSnapshotError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise AuthorizationSnapshotError(f"{label} is missing fields: {sorted(missing)}")
    if not all(isinstance(key, str) for key in value):
        raise AuthorizationSnapshotError(f"{label} contains an invalid field name")
    return value


def _string_tuple(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AuthorizationSnapshotError(f"{label} must be an array")
    result = tuple(_required_text(item, f"{label} entry") for item in value)
    if not allow_empty and not result:
        raise AuthorizationSnapshotError(f"{label} cannot be empty")
    return result


def _required_text(value: object, label: str) -> str:
    result = _text(value, label)
    if not result:
        raise AuthorizationSnapshotError(f"{label} cannot be empty")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise AuthorizationSnapshotError(f"{label} must be text")
    return value
