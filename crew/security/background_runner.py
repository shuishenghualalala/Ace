"""Fixed host bridge that turns a ProcessRegistry session into one native runtime call."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from crew.security.runtime_client import (
    NativeRuntimeClient,
    NativeRuntimeError,
    is_likely_sandbox_denied,
)


async def _run(payload: dict) -> int:
    client = NativeRuntimeClient(tuple(payload["helper_argv"]))
    env_overrides = dict(payload.get("env_overrides") or {})
    trusted_path = env_overrides.pop("PATH", None)
    try:
        result = await client.execute(
            command=tuple(payload["command"]),
            cwd=Path(payload["cwd"]),
            writable_roots=tuple(Path(value) for value in payload.get("writable_roots", [])),
            readable_roots=tuple(Path(value) for value in payload.get("readable_roots", [])),
            readonly_roots=tuple(Path(value) for value in payload.get("readonly_roots", [])),
            denied_roots=tuple(Path(value) for value in payload.get("denied_roots", [])),
            full_disk_read=bool(payload.get("full_disk_read", False)),
            network_enabled=bool(payload.get("network_rules")),
            network_rules=tuple(payload.get("network_rules", [])),
            allow_local_binding=bool(payload.get("allow_local_binding", False)),
            timeout=float(payload.get("timeout", 86400)),
            max_output_bytes=int(payload.get("max_output_bytes", 2 * 1024 * 1024)),
            env_overrides=env_overrides,
            trusted_path=trusted_path,
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


def _write_result(payload: dict, result: dict) -> None:
    """Return trusted runtime metadata through a host-only sidecar."""
    result_path = str(payload.get("result_path") or "")
    nonce = str(payload.get("result_nonce") or "")
    if not result_path or len(nonce) < 32:
        return
    Path(result_path).write_text(
        json.dumps({"nonce": nonce, **result}, separators=(",", ":")),
        encoding="utf-8",
    )


def main() -> int:
    line = sys.stdin.buffer.readline(8 * 1024 * 1024 + 1)
    if not line or len(line) > 8 * 1024 * 1024:
        return 126
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 126
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return 126
    return asyncio.run(_run(payload))


if __name__ == "__main__":
    raise SystemExit(main())
