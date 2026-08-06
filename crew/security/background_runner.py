"""Fixed host bridge that turns a ProcessRegistry session into one native runtime call."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError


async def _run(payload: dict) -> int:
    client = NativeRuntimeClient(tuple(payload["helper_argv"]))
    try:
        result = await client.execute(
            command=tuple(payload["command"]),
            cwd=Path(payload["cwd"]),
            writable_roots=tuple(Path(value) for value in payload.get("writable_roots", [])),
            readable_roots=tuple(Path(value) for value in payload.get("readable_roots", [])),
            denied_roots=tuple(Path(value) for value in payload.get("denied_roots", [])),
            network_enabled=bool(payload.get("network_rules")),
            network_rules=tuple(payload.get("network_rules", [])),
            allow_local_binding=bool(payload.get("allow_local_binding", False)),
            timeout=float(payload.get("timeout", 86400)),
            max_output_bytes=int(payload.get("max_output_bytes", 2 * 1024 * 1024)),
            env_overrides=payload.get("env_overrides"),
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
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 126
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return 126
    return asyncio.run(_run(payload))


if __name__ == "__main__":
    raise SystemExit(main())
