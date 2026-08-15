"""Shared fail-closed quota for filesystem capability discovery.

Plugin and Skill discovery both inspect attacker-influenced directory trees.  They
share one non-blocking slot so concurrent requests cannot multiply that work and
turn otherwise-bounded scans into aggregate resource exhaustion.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

MAX_CAPABILITY_DISCOVERY_CONCURRENCY = 1
_DISCOVERY_SLOTS = threading.BoundedSemaphore(MAX_CAPABILITY_DISCOVERY_CONCURRENCY)


class CapabilityDiscoveryBusy(RuntimeError):
    """Another capability discovery owns the process-wide scan budget."""


@contextmanager
def capability_discovery_slot() -> Iterator[None]:
    """Acquire the process-wide discovery slot without creating an unbounded queue."""

    if not _DISCOVERY_SLOTS.acquire(blocking=False):
        raise CapabilityDiscoveryBusy("capability discovery concurrency budget exceeded")
    try:
        yield
    finally:
        _DISCOVERY_SLOTS.release()


__all__ = [
    "MAX_CAPABILITY_DISCOVERY_CONCURRENCY",
    "CapabilityDiscoveryBusy",
    "capability_discovery_slot",
]
