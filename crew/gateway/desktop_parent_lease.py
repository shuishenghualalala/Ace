"""A private parent pipe for Gateway processes started by Desktop."""

from __future__ import annotations

import atexit
import hmac
import os
import re
import stat
import sys
import threading
from collections.abc import Sequence

DESKTOP_PARENT_LEASE_ARG = "--desktop-parent-lease"
DESKTOP_PARENT_LEASE_KEY_ENV = "CREW_DESKTOP_GATEWAY_LEASE_KEY"
_DESKTOP_LAUNCH_CONTEXT = b"crew-gateway-desktop-launch-v1\x00"
_SHUTDOWN_GRACE_SECONDS = 30.0
_EMERGENCY_CLEANUP_SECONDS = 2.0


def _terminate_registered_children() -> None:
    # The deadline also covers failures during import. Never wait for the
    # import lock if the shutdown code has yet to be loaded.
    lifecycle = sys.modules.get("crew.security.process_lifecycle")
    terminate = getattr(lifecycle, "terminate_all_for_host_exit", None)
    if callable(terminate):
        terminate()


class DesktopParentLease:
    """Watch an inherited read endpoint without depending on a parent PID."""

    def __init__(
        self,
        key: bytes,
        pipe_fd: int,
        *,
        shutdown_grace: float = _SHUTDOWN_GRACE_SECONDS,
        cleanup_grace: float = _EMERGENCY_CLEANUP_SECONDS,
    ) -> None:
        self._key = key
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._shutdown_grace = shutdown_grace
        self._cleanup_grace = cleanup_grace
        threading.Thread(target=self._monitor, args=(pipe_fd,), name="desktop-parent-lease", daemon=True).start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def finish(self) -> None:
        self._finished.set()

    def launch_proof(self, challenge: bytes) -> str:
        # Retain ownership identity while draining. A still-serving listener
        # must not look like a standalone Gateway once its parent disappears.
        return hmac.digest(self._key, _DESKTOP_LAUNCH_CONTEXT + challenge, "sha256").hex()

    def _monitor(self, pipe_fd: int) -> None:
        try:
            # Bypass buffered stdin so this daemon cannot hold an I/O lock
            # that CPython needs during its own shutdown.
            while os.read(pipe_fd, 1):
                pass
        except (OSError, ValueError):
            pass  # A broken or closed pipe also loses the parent's lease.
        self._closed.set()
        if self._finished.wait(self._shutdown_grace):
            return
        cleanup = threading.Thread(target=_terminate_registered_children, name="desktop-parent-cleanup", daemon=True)
        try:
            cleanup.start()
            cleanup.join(self._cleanup_grace)
        except RuntimeError:
            # Python can reject new threads during interpreter shutdown.
            pass
        if not self._finished.is_set():
            os._exit(1)


_active_lease: DesktopParentLease | None = None


def bootstrap_desktop_parent_lease(argv: Sequence[str]) -> DesktopParentLease | None:
    """Scrub the startup secret and start watching only for an opt-in launch."""
    global _active_lease
    raw_key = os.environ.pop(DESKTOP_PARENT_LEASE_KEY_ENV, "")
    if DESKTOP_PARENT_LEASE_ARG not in argv:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", raw_key) is None:
        raise SystemExit("Invalid Desktop Gateway parent lease key")
    try:
        pipe_fd = sys.stdin.fileno()
        mode = os.fstat(pipe_fd).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise ValueError("stdin is not a pipe")
    except (AttributeError, OSError, ValueError) as exc:
        raise SystemExit("Desktop Gateway parent lease requires an inherited stdin pipe") from exc
    lease = DesktopParentLease(bytes.fromhex(raw_key), pipe_fd)
    _active_lease = lease
    # Register before loading Gateway so its other exit hooks finish first.
    atexit.register(lease.finish)
    return lease


def create_desktop_launch_proof(challenge: bytes) -> str | None:
    lease = _active_lease
    return lease.launch_proof(challenge) if lease is not None else None
