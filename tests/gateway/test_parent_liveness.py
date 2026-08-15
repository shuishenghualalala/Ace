"""Desktop-to-Gateway parent-liveness lease tests."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

from crew.gateway.app import _start_desktop_parent_monitor


def test_gateway_requests_shutdown_only_after_parent_pipe_closes() -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    server = SimpleNamespace(should_exit=False)
    try:
        thread = _start_desktop_parent_monitor(stream, server)
        time.sleep(0.05)
        assert thread.is_alive()
        assert server.should_exit is False

        os.close(write_fd)
        write_fd = -1
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert server.should_exit is True
    finally:
        stream.close()
        if write_fd >= 0:
            os.close(write_fd)
