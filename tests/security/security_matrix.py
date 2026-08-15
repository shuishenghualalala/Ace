"""Real-runner release smoke test; platform mocks are intentionally not accepted."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
from crew.security.context import SecurityContext
from crew.security.launch import finalize_process_launch, issue_process_launch
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    NetworkAccess,
    NetworkEntry,
    NetworkPolicy,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.security.runtime_client import NativeRuntimeClient


def _authorized_request(
    *,
    runtime: Path,
    profile: PermissionProfile,
    command: tuple[str, ...],
    cwd: Path,
    additional_permissions: AdditionalPermissionProfile | None = None,
    timeout_seconds: float,
) -> ExecutionRequest:
    additional = additional_permissions or AdditionalPermissionProfile()
    context = SecurityContext(
        os_user="security-matrix-runner",
        owner_account_id="security-matrix-owner",
        workspace_id="security-matrix-workspace",
        workspace_root=cwd,
        session_id="security-matrix-session",
        request_id="security-matrix-request",
        task_id="security-matrix-task",
        cwd=cwd,
    )
    launch = issue_process_launch(
        context,
        profile,
        helper_argv=(str(runtime.resolve(strict=True)),),
        additional_permissions=additional,
    )
    authorization = finalize_process_launch(
        launch,
        argv=command,
        cwd=cwd,
        environment={},
        expected_owner_account_id=context.owner_account_id,
        expected_workspace_id=context.workspace_id,
        expected_session_id=context.session_id,
        expected_task_id=context.task_id,
    )
    return ExecutionRequest(
        authorization_snapshot=authorization,
        timeout_seconds=timeout_seconds,
    )


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
            _authorized_request(
                runtime=runtime,
                profile=profile,
                command=write_command,
                cwd=workspace,
                timeout_seconds=15,
            )
        )
        if allowed.exit_code != 0 or (workspace / "ok.txt").read_text().strip() != "ok":
            raise SystemExit(f"SMX-FS-001 workspace write failed: {allowed.stderr}")
        delete_target = outside / "approved-delete.txt"
        delete_target.write_text("delete-me", encoding="utf-8")
        approved_delete = await broker.execute(
            _authorized_request(
                runtime=runtime,
                profile=profile,
                command=(
                    ("cmd.exe", "/d", "/c", "del", "/q", str(delete_target))
                    if platform_name == "windows"
                    else ("/bin/rm", "-f", str(delete_target))
                ),
                cwd=workspace,
                additional_permissions=AdditionalPermissionProfile(
                    filesystem=(
                        FilesystemEntry(
                            root=outside,
                            access=FilesystemAccess.READ_WRITE,
                        ),
                    ),
                ),
                timeout_seconds=15,
            )
        )
        if approved_delete.exit_code != 0 or delete_target.exists():
            raise SystemExit(
                f"SMX-FS-003 approved external delete failed: {approved_delete.stderr}"
            )
        denied = await broker.execute(
            _authorized_request(
                runtime=runtime,
                profile=profile,
                command=(
                    ("cmd.exe", "/d", "/c", "type", str(secret))
                    if platform_name == "windows"
                    else ("/bin/cat", str(secret))
                ),
                cwd=workspace,
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
            def do_GET(self) -> None:
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
                _authorized_request(
                    runtime=runtime,
                    profile=profile,
                    command=(curl, "--fail", "--max-time", "3", f"http://127.0.0.1:{port}/"),
                    cwd=workspace,
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
            if platform_name == "windows":
                direct_online = await broker.execute(
                    _authorized_request(
                        runtime=runtime,
                        profile=online_profile,
                        command=(
                            curl,
                            "--noproxy",
                            "*",
                            "--fail",
                            "--max-time",
                            "3",
                            f"http://127.0.0.1:{port}/",
                        ),
                        cwd=workspace,
                        timeout_seconds=10,
                    )
                )
                if direct_online.exit_code == 0:
                    raise SystemExit(
                        "SMX-NET-003 online sandbox bypassed the proxy-only WFP route"
                    )
            proxied = await broker.execute(
                _authorized_request(
                    runtime=runtime,
                    profile=online_profile,
                    command=(curl, "--fail", "--max-time", "5", f"http://127.0.0.1:{port}/"),
                    cwd=workspace,
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
    parser.add_argument("--platform", required=True, choices=("linux", "windows", "macos"))
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run_matrix(args.runtime, args.platform))
    print(f"native security smoke passed on {args.platform}")


if __name__ == "__main__":
    main()
