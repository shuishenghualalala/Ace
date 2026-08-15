"""Fixed host bridge that turns a ProcessRegistry session into one native runtime call."""

from __future__ import annotations

if __name__ == "__main__":
    from crew.process_hardening import harden_main_process

    harden_main_process("managed-background-bridge")

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError
from crew.security.snapshot import (
    AuthorizationSnapshotError,
    SignedAuthorizationSnapshot,
    verify_authorization_snapshot,
)

BRIDGE_PROTOCOL_VERSION = 2
BRIDGE_BOOTSTRAP_VERSION = 1
_BRIDGE_BOOTSTRAP_FIELDS = {
    "authorization_key",
    "parent_pid",
    "payload",
    "version",
}
_BRIDGE_FIELDS = {
    "env_overrides",
    "max_output_bytes",
    "snapshot",
    "snapshot_digest",
    "snapshot_mac",
    "snapshot_nonce",
    "timeout",
    "version",
}


@dataclass(frozen=True)
class ParsedBridgePayload:
    authorization: SignedAuthorizationSnapshot
    environment: dict[str, str]
    timeout: float
    max_output_bytes: int
    verification_key: bytes


def parse_bridge_payload(
    value: object,
    *,
    verification_key: bytes,
) -> ParsedBridgePayload:
    """Strictly authenticate one fixed bridge request before using any launch fact."""
    if not isinstance(value, Mapping):
        raise AuthorizationSnapshotError("bridge payload must be an object")
    unknown = set(value) - _BRIDGE_FIELDS
    missing = _BRIDGE_FIELDS - set(value)
    if unknown:
        raise AuthorizationSnapshotError(f"bridge payload contains unknown fields: {sorted(unknown)}")
    if missing:
        raise AuthorizationSnapshotError(f"bridge payload is missing fields: {sorted(missing)}")
    if value.get("version") != BRIDGE_PROTOCOL_VERSION:
        raise AuthorizationSnapshotError("bridge payload version is unsupported")
    if not isinstance(verification_key, bytes) or len(verification_key) != 32:
        raise AuthorizationSnapshotError("bridge verification key is invalid")
    environment_value = value.get("env_overrides")
    if not isinstance(environment_value, Mapping):
        raise AuthorizationSnapshotError("bridge environment must be an object")
    environment: dict[str, str] = {}
    for name, content in environment_value.items():
        if (
            not isinstance(name, str)
            or not isinstance(content, str)
            or "\x00" in name + content
        ):
            raise AuthorizationSnapshotError("bridge environment contains an invalid entry")
        environment[name] = content
    timeout = value.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise AuthorizationSnapshotError("bridge timeout is invalid")
    max_output_bytes = value.get("max_output_bytes")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 <= max_output_bytes <= 2 * 1024 * 1024
    ):
        raise AuthorizationSnapshotError("bridge output limit is invalid")
    signed = SignedAuthorizationSnapshot.from_payload(
        {
            "snapshot": value.get("snapshot"),
            "snapshot_digest": value.get("snapshot_digest"),
            "snapshot_mac": value.get("snapshot_mac"),
        }
    )
    nonce = value.get("snapshot_nonce")
    if not isinstance(nonce, str):
        raise AuthorizationSnapshotError("bridge snapshot nonce is invalid")
    verify_authorization_snapshot(
        signed,
        environment=environment,
        expected_nonce=nonce,
        verification_key=verification_key,
    )
    return ParsedBridgePayload(
        authorization=signed,
        environment=environment,
        timeout=float(timeout),
        max_output_bytes=max_output_bytes,
        verification_key=verification_key,
    )


def parse_bridge_bootstrap(
    value: object,
    *,
    actual_parent_pid: int | None = None,
) -> tuple[object, bytes]:
    """Read the one-shot verification key from the private stdin pipe."""
    if not isinstance(value, Mapping):
        raise AuthorizationSnapshotError("bridge bootstrap must be an object")
    if set(value) != _BRIDGE_BOOTSTRAP_FIELDS:
        raise AuthorizationSnapshotError("bridge bootstrap schema is invalid")
    if value.get("version") != BRIDGE_BOOTSTRAP_VERSION:
        raise AuthorizationSnapshotError("bridge bootstrap version is unsupported")
    parent_pid = value.get("parent_pid")
    observed_parent_pid = (
        os.getppid() if actual_parent_pid is None else actual_parent_pid
    )
    if (
        isinstance(parent_pid, bool)
        or not isinstance(parent_pid, int)
        or parent_pid <= 0
        or parent_pid != observed_parent_pid
    ):
        raise AuthorizationSnapshotError("bridge bootstrap parent identity is invalid")
    encoded = value.get("authorization_key")
    if not isinstance(encoded, str):
        raise AuthorizationSnapshotError("bridge verification key is malformed")
    try:
        key = bytes.fromhex(encoded)
    except ValueError as exc:
        raise AuthorizationSnapshotError("bridge verification key is malformed") from exc
    if len(key) != 32:
        raise AuthorizationSnapshotError("bridge verification key is unavailable")
    return value.get("payload"), key


async def _run(payload: ParsedBridgePayload) -> int:
    if not isinstance(payload, ParsedBridgePayload):
        raise AuthorizationSnapshotError("bridge payload was not authenticated")
    snapshot = payload.authorization.snapshot
    client = NativeRuntimeClient(snapshot.helper_argv)
    try:
        result = await client.execute_authorized(
            authorization=payload.authorization,
            env_overrides=payload.environment,
            timeout=payload.timeout,
            max_output_bytes=payload.max_output_bytes,
            verification_key=payload.verification_key,
        )
    except NativeRuntimeError as error:
        print(json.dumps({"error_code": error.code.value, "error": str(error)}), flush=True)
        return 125
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main() -> int:
    line = sys.stdin.buffer.readline(8 * 1024 * 1024 + 1)
    if not line or len(line) > 8 * 1024 * 1024:
        return 126
    try:
        bootstrap = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 126
    try:
        payload, verification_key = parse_bridge_bootstrap(bootstrap)
        parsed = parse_bridge_payload(
            payload,
            verification_key=verification_key,
        )
    except AuthorizationSnapshotError:
        return 126
    return asyncio.run(_run(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
