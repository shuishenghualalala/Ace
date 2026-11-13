"""Tests for the synchronous-to-asynchronous bridge contract."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

from crew.core import asyncio_bridge


async def _value(value: str) -> str:
    await asyncio.sleep(0)
    return value


def test_run_sync_from_sync_context() -> None:
    assert asyncio_bridge.run_sync(_value("sync")) == "sync"


def test_run_sync_from_foreign_running_loop() -> None:
    async def caller() -> str:
        return asyncio_bridge.run_sync(_value("foreign-loop"))

    assert asyncio.run(caller()) == "foreign-loop"


def test_run_sync_rejects_the_bridge_loop_thread() -> None:
    bridge_loop = asyncio_bridge._bridge_loop()

    async def caller() -> None:
        with pytest.raises(RuntimeError, match="bridge loop thread"):
            asyncio_bridge.run_sync(_value("must-not-run"))

    future = asyncio.run_coroutine_threadsafe(caller(), bridge_loop)
    try:
        future.result(timeout=2)
    except FutureTimeoutError as exc:  # pragma: no cover - protects against a deadlock
        raise AssertionError("bridge-loop guard did not return") from exc
