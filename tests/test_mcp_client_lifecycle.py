"""MCP Client 有界队列、截止时间与关闭语义。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from crew.tools.mcp_client import MCPClientManager, _ServerWorker
from crew.tools.registry import Registry


def _error(result: str) -> str:
    return str(json.loads(result)["error"])


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.call_started = asyncio.Event()
        self.release = asyncio.Event()

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, _args: dict) -> SimpleNamespace:
        self.calls.append(name)
        self.call_started.set()
        await self.release.wait()
        return SimpleNamespace(content=[SimpleNamespace(text=name)], is_error=False)


async def _started_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_timeout: float = 0.2,
) -> tuple[_ServerWorker, _FakeSession]:
    worker = _ServerWorker(
        "fake",
        {},
        Registry(),
        call_timeout=call_timeout,
        startup_timeout=0.1,
    )
    session = _FakeSession()

    async def open_fake(_stack):
        return session

    monkeypatch.setattr(worker, "_open", open_fake)
    assert await worker.start()
    return worker, session


@pytest.mark.asyncio
async def test_queue_full_fails_immediately_without_displacing_existing_requests():
    worker = _ServerWorker(
        "full",
        {},
        Registry(),
        queue_capacity=2,
        call_timeout=1.0,
    )
    blocker = asyncio.Event()
    worker._task = asyncio.create_task(blocker.wait())
    handler = worker._make_handler("echo")

    first = asyncio.create_task(handler({"n": 1}))
    second = asyncio.create_task(handler({"n": 2}))
    while worker._queue.qsize() < 2:
        await asyncio.sleep(0)

    result = await handler({"n": 3})
    assert "队列已满" in _error(result)
    assert worker._queue.qsize() == 2

    worker.force_abort("test cleanup")
    await asyncio.gather(first, second)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_expired_queued_request_never_calls_remote(monkeypatch):
    worker, session = await _started_worker(monkeypatch, call_timeout=0.2)
    first = asyncio.create_task(worker._make_handler("first")({}))
    await session.call_started.wait()

    worker._call_timeout = 0.02
    second_result = await worker._make_handler("second")({})
    assert "执行前" in _error(second_result)
    assert "远端未被调用" in _error(second_result)
    assert session.calls == ["first"]

    await worker.stop()
    assert "状态可能未知" in _error(await first)


@pytest.mark.asyncio
async def test_inflight_timeout_is_not_retried_and_reports_unknown_state(monkeypatch):
    worker, session = await _started_worker(monkeypatch, call_timeout=0.02)

    result = await worker._make_handler("mutate")({})

    assert session.calls == ["mutate"]
    assert "状态可能未知" in _error(result)
    assert "不会自动重试" in _error(result)
    await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disconnect", "first_error"),
    [
        pytest.param("stop", "正在关闭", id="stop"),
        pytest.param("exit", "连接已断开", id="unexpected_exit"),
    ],
)
async def test_disconnect_completes_inflight_and_queued_futures(monkeypatch, disconnect, first_error):
    worker, session = await _started_worker(monkeypatch, call_timeout=1.0)
    first = asyncio.create_task(worker._make_handler("first")({}))
    await session.call_started.wait()
    second = asyncio.create_task(worker._make_handler("second")({}))
    while worker._queue.empty():
        await asyncio.sleep(0)

    if disconnect == "exit":
        assert worker._task is not None
        worker._task.cancel()
    else:
        await worker.stop()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_error in _error(first_result)
    assert "状态可能未知" in _error(first_result)
    assert "排队请求未调用远端" in _error(second_result)
    assert session.calls == ["first"]


@pytest.mark.asyncio
async def test_startup_has_total_deadline(monkeypatch):
    worker = _ServerWorker(
        "slow",
        {},
        Registry(),
        startup_timeout=0.02,
    )
    never = asyncio.Event()

    async def open_forever(_stack):
        await never.wait()

    monkeypatch.setattr(worker, "_open", open_forever)
    assert not await worker.start()
    assert isinstance(worker._error, TimeoutError)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_servers_start_in_parallel_and_fail_independently(monkeypatch):
    completed_at: dict[str, float] = {}

    async def fake_start(self):
        await asyncio.sleep(0.01 if self.name == "good" else 0.05)
        completed_at[self.name] = asyncio.get_running_loop().time()
        return self.name == "good"

    monkeypatch.setattr(_ServerWorker, "start", fake_start)
    manager = MCPClientManager({"bad": {}, "good": {}}, shutdown_timeout=0.1)

    await manager._start_blocking(Registry())

    assert completed_at["good"] < completed_at["bad"]
    assert {worker.name for worker in manager._workers} == {"bad", "good"}
    await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_incomplete_start_with_one_total_budget(monkeypatch):
    manager = MCPClientManager({"slow": {}}, shutdown_timeout=0.02)
    never = asyncio.Event()

    async def start_forever(_registry):
        await never.wait()

    monkeypatch.setattr(manager, "_start_blocking", start_forever)
    await manager.start(Registry())
    await asyncio.sleep(0)

    started = asyncio.get_running_loop().time()
    await manager.aclose()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1
    assert manager._start_task is None
