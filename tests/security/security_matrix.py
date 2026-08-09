"""Real-runner release smoke test; platform mocks are intentionally not accepted."""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading
import tempfile

from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
from crew.security.models import (
    FilesystemAccess,
    FilesystemEntry,
    NetworkAccess,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeClient


async def run_matrix(runtime: Path, platform_name: str) -> None:
    if not runtime.is_file():
        raise SystemExit(f"native runtime missing: {runtime}")
    with tempfile.TemporaryDirectory(prefix="ace-security-matrix-") as raw:
        root = Path(raw).resolve()
        workspace = root / "workspace"
        outside = root / "outside"
        workspace.mkdir()
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("MATRIX_SECRET_MUST_NOT_LEAK", encoding="utf-8")
        profile = PermissionProfile(
            kind=PermissionProfileKind.MANAGED,
            filesystem=(
                FilesystemEntry(
                    root=workspace,
                    access=FilesystemAccess.READ_WRITE,
                ),
            ),
            network=NetworkPolicy.RESTRICTED,
        )
        broker = SecurityExecutionBroker(NativeRuntimeClient((str(runtime.resolve()),)))
        write_command = (
            ("cmd.exe", "/d", "/c", "echo ok>ok.txt")
            if platform_name == "windows"
            else ("/bin/sh", "-c", "printf ok > ok.txt")
        )
        allowed = await broker.execute(
            ExecutionRequest(
                command=write_command,
                cwd=workspace,
                permission_profile=profile,
                timeout_seconds=15,
            )
        )
        if allowed.exit_code != 0 or (workspace / "ok.txt").read_text().strip() != "ok":
            raise SystemExit(f"SMX-FS-001 workspace write failed: {allowed.stderr}")
        denied = await broker.execute(
            ExecutionRequest(
                command=(
                    ("cmd.exe", "/d", "/c", "type", str(secret))
                    if platform_name == "windows"
                    else ("/bin/cat", str(secret))
                ),
                cwd=workspace,
                permission_profile=profile,
                timeout_seconds=15,
            )
        )
        combined = denied.stdout + denied.stderr
        if denied.exit_code == 0 or "MATRIX_SECRET_MUST_NOT_LEAK" in combined:
            raise SystemExit("SMX-FS-002 outside read was not denied or leaked secret output")

        curl = shutil.which("curl.exe" if platform_name == "windows" else "curl")
        if not curl:
            raise SystemExit("SMX-NET-001 requires packaged/system curl on the real runner")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"matrix-network-ok")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            direct = await broker.execute(
                ExecutionRequest(
                    command=(curl, "--fail", "--max-time", "3", f"http://127.0.0.1:{port}/"),
                    cwd=workspace,
                    permission_profile=profile,
                    timeout_seconds=10,
                )
            )
            if direct.exit_code == 0:
                raise SystemExit("SMX-NET-001 offline sandbox reached a host listener directly")
            online_profile = PermissionProfile(
                kind=PermissionProfileKind.MANAGED,
                filesystem=profile.filesystem,
                network=NetworkPolicy.RESTRICTED,
                network_entries=(
                    NetworkEntry(
                        host="127.0.0.1",
                        port=port,
                        protocol="http",
                        access=NetworkAccess.ALLOW,
                        allow_private=True,
                    ),
                ),
            )
            proxied = await broker.execute(
                ExecutionRequest(
                    command=(curl, "--fail", "--max-time", "5", f"http://127.0.0.1:{port}/"),
                    cwd=workspace,
                    permission_profile=online_profile,
                    timeout_seconds=15,
                )
            )
            if proxied.exit_code != 0 or "matrix-network-ok" not in proxied.stdout:
                raise SystemExit(f"SMX-NET-002 allowed proxy route failed: {proxied.stderr}")
        finally:
            server.shutdown()
            server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=("linux", "macos", "windows"))
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run_matrix(args.runtime, args.platform))
    print(f"native security smoke passed on {args.platform}")


if __name__ == "__main__":
    main()
