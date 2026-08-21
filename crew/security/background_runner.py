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
from pathlib import Path
from dataclasses import asdict, dataclass

from crew.security.runtime_client import (
    NativeRuntimeClient,
    NativeRuntimeError,
    is_likely_sandbox_denied,
)
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
    "result_nonce",
    "result_path",
    "snapshot",
    "snapshot_digest",
    "snapshot_mac",
    "snapshot_nonce",
    "timeout",
    "version",
}
# The result sidecar is host-created metadata plumbing, not an authorization
# fact; it is optional so minimal bridge payloads stay protocol-clean.
_OPTIONAL_BRIDGE_FIELDS = {"result_nonce", "result_path"}


@dataclass(frozen=True)
class ParsedBridgePayload:
    authorization: SignedAuthorizationSnapshot
    environment: dict[str, str]
    timeout: float
    max_output_bytes: int
    verification_key: bytes
    result_path: str = ""
    result_nonce: str = ""


def parse_bridge_payload(
    value: object,
    *,
    verification_key: bytes,
) -> ParsedBridgePayload:
    """Strictly authenticate one fixed bridge request before using any launch fact."""
    if not isinstance(value, Mapping):
        raise AuthorizationSnapshotError("bridge payload must be an object")
    unknown = set(value) - _BRIDGE_FIELDS
    missing = (_BRIDGE_FIELDS - _OPTIONAL_BRIDGE_FIELDS) - set(value)
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
    result_path_value = value.get("result_path")
    result_nonce_value = value.get("result_nonce")
    if (result_path_value is None) != (result_nonce_value is None):
        raise AuthorizationSnapshotError(
            "bridge result sidecar fields must be paired"
        )
    result_path = ""
    result_nonce = ""
    if result_path_value is not None:
        if not isinstance(result_path_value, str) or not result_path_value:
            raise AuthorizationSnapshotError("bridge result path is invalid")
        if not isinstance(result_nonce_value, str) or len(result_nonce_value) < 32:
            raise AuthorizationSnapshotError("bridge result nonce is invalid")
        result_path = result_path_value
        result_nonce = result_nonce_value
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
        result_path=result_path,
        result_nonce=result_nonce,
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
        _write_result(
            payload,
            {
                "stable_error_code": error.code.value,
            },
        )
        print(json.dumps({"error_code": error.code.value, "error": str(error)}), flush=True)
        return 125
    except (OSError, TypeError, ValueError) as error:
        _write_result(payload, {"stable_error_code": "runtime_crashed"})
        print(
            json.dumps(
                {
                    "error_code": "runtime_crashed",
                    "error": f"managed runtime launch failed: {type(error).__name__}",
                }
            ),
            flush=True,
        )
        return 125
    capabilities = asdict(result.capabilities)
    sandbox_denied = is_likely_sandbox_denied(
        result.exit_code,
        result.stdout,
        result.stderr,
        backend=str(result.capabilities.backend),
    )
    metadata = {
        "sandbox_backend": str(capabilities.pop("backend", "")),
        "capabilities": [
            key
            for key, value in capabilities.items()
            if value is True or key == "wsl_version" and value is not None
        ],
        "exit_code": result.exit_code,
    }
    if sandbox_denied:
        metadata["stable_error_code"] = "sandbox_denied"
    _write_result(payload, metadata)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _write_result(payload: ParsedBridgePayload, result: dict) -> None:
    """Return trusted runtime metadata through a host-only sidecar."""
    if not payload.result_path or len(payload.result_nonce) < 32:
        return
    Path(payload.result_path).write_text(
        json.dumps({"nonce": payload.result_nonce, **result}, separators=(",", ":")),
        encoding="utf-8",
    )


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
