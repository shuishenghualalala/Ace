"""MCP Client 有界队列、截止时间与关闭语义。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from crew.core.observability import bind_observation, install_sink
from crew.state.observability import ObservationRecorder
from crew.state.trace_store import TraceStore
from crew.tools.mcp_client import MCPClientManager, _ServerWorker
from crew.tools.registry import Registry


@pytest.fixture(autouse=True)
def clear_observation_sink():
    yield
    install_sink(None)


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
async def test_remote_handler_fails_closed_without_conversation_security_context():
    worker = _ServerWorker(
        "remote",
        {"url": "https://mcp.example.com/api"},
        Registry(),
    )
    worker._task = asyncio.create_task(asyncio.Event().wait())

    result = await worker._make_handler("mutate")({"value": 1})

    assert "缺少当前会话安全上下文" in _error(result)
    assert worker._queue.empty()
    worker.force_abort("test cleanup")
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
async def test_mcp_worker_attaches_queue_context_and_records_payload_refs(monkeypatch, tmp_path):
    worker, session = await _started_worker(monkeypatch, call_timeout=0.5)
    store = TraceStore(tmp_path / "observability.sqlite3")
    recorder = ObservationRecorder(store)
    install_sink(recorder)
    with bind_observation(owner_account_id="owner", request_id="request"):
        pending = asyncio.create_task(worker._make_handler("echo")({"query": "safe"}))
    await session.call_started.wait()
    session.release.set()
    result = await pending
    assert "echo" in result
    await worker.stop()
    assert recorder.flush(2)
    traces = store.list_traces(owner_account_id="owner")["items"]
    assert len(traces) == 1
    spans = store.list_spans(owner_account_id="owner", trace_id=traces[0]["trace_id"])
    assert any(item["module"] == "mcp" and item["operation"] == "mcp.call" for item in spans)
    events = store.list_events(owner_account_id="owner", trace_id=traces[0]["trace_id"])
    completed = next(item for item in events if item["name"] == "mcp.call.completed")
    assert completed["attributes"]["request_payload_id"]
    assert completed["attributes"]["response_payload_id"]
    recorder.close()
    store.close()


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


# --------------------------------------------------------------------- #
# 断线重连 supervisor：指数退避、共享预算、稳定重置、耗尽显式失败
# --------------------------------------------------------------------- #


class _ScriptedSession:
    """按脚本表现的可编程假 session。

    call_behavior: 每次 call_tool 调用的协程，签名为 async (name) -> result。
    ping: None=未配置 send_ping；协程则按脚本执行。
    """

    def __init__(self, tools=("t1",), call_behavior=None, ping=None):
        self._tools = [SimpleNamespace(name=n, description="", input_schema={}) for n in tools]
        self._call_behavior = call_behavior
        self._ping = ping

    async def list_tools(self):
        return SimpleNamespace(tools=list(self._tools))

    async def call_tool(self, name, _args):
        if self._call_behavior is None:
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"ok:{name}")], is_error=False
            )
        return await self._call_behavior(name)


async def _dead_call(name):
    raise RuntimeError(f"boom:{name}")


async def _dead_ping():
    raise ConnectionError("transport lost")


async def _ok_call(name):
    return SimpleNamespace(content=[SimpleNamespace(text=f"ok:{name}")], is_error=False)


def _attach_ping(session, ping):
    session.send_ping = ping
    return session


async def _wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


async def _scripted_worker(monkeypatch, cfg, sessions, delays):
    """造一个 _open 按脚本吐 session、_sleep 记录退避时长的 worker。"""
    from crew.tools import mcp_client as mcp_client_module

    worker = _ServerWorker(
        "flaky",
        cfg,
        Registry(),
        call_timeout=1.0,
        startup_timeout=0.2,
    )
    scripted = list(sessions)

    async def open_scripted(_stack):
        if not scripted:
            raise RuntimeError("脚本耗尽：不应再有连接尝试")
        session = scripted.pop(0)
        if isinstance(session, Exception):
            raise session
        return session

    async def fake_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_open", open_scripted)
    monkeypatch.setattr(mcp_client_module, "_sleep", fake_sleep)
    return worker


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_with_exponential_backoff(monkeypatch):
    delays: list[float] = []
    dying = _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping)
    healthy = _ScriptedSession(call_behavior=_ok_call)
    worker = await _scripted_worker(monkeypatch, {}, [dying, healthy], delays)

    assert await worker.start()
    first = await worker._make_handler("t1")({})
    assert "连接已断开" in _error(first)

    await _wait_for(lambda: worker.is_connected)
    second = await worker._make_handler("t1")({})
    assert "ok:t1" in second
    assert delays == [0.5]  # 首次退避 = initial_delay
    await worker.stop()


@pytest.mark.asyncio
async def test_queued_call_survives_reconnect_within_deadline(monkeypatch):
    delays: list[float] = []
    dying = _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping)
    healthy = _ScriptedSession(call_behavior=_ok_call)
    worker = await _scripted_worker(monkeypatch, {}, [dying, healthy], delays)

    assert await worker.start()
    first = asyncio.create_task(worker._make_handler("t1")({}))
    await _wait_for(lambda: "连接已断开" in (first.done() and _error(first.result()) or ""))
    # 退避期间发起的调用排队等待，重连成功后在截止时间前被服务。
    second = asyncio.create_task(worker._make_handler("t1")({}))
    assert "ok:t1" in await second
    await first
    await worker.stop()


@pytest.mark.asyncio
async def test_budget_exhaustion_unregisters_tools_and_fails_explicitly(monkeypatch):
    delays: list[float] = []
    dying = _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping)
    cfg = {"reconnect": {"initial_delay": 0.5, "max_delay": 30.0, "max_attempts": 2}}
    worker = await _scripted_worker(
        monkeypatch,
        cfg,
        [dying, RuntimeError("still down"), RuntimeError("still down")],
        delays,
    )
    registry = worker.registry

    assert await worker.start()
    assert "flaky__t1" in registry.names()
    await worker._make_handler("t1")({})  # 触发断线进入重连循环

    await _wait_for(lambda: worker._task is not None and worker._task.done())
    assert "flaky__t1" not in registry.names()
    assert "预算" in worker.error and "重载" in worker.error
    result = await worker._make_handler("t1")({})
    assert "连接已放弃" in _error(result)
    assert delays == [0.5, 1.0]  # 500ms 翻倍；第三次失败超出预算直接放弃


@pytest.mark.asyncio
async def test_stable_uptime_past_max_delay_resets_attempt_budget(monkeypatch):
    delays: list[float] = []
    cfg = {
        "reconnect": {"initial_delay": 0.01, "max_delay": 0.05, "max_attempts": 2},
        "ping_interval": 0,
    }

    async def slow_death(name):
        await asyncio.sleep(0.08)  # 存活超过 max_delay(0.05s) 后才断
        raise RuntimeError(f"boom:{name}")

    sessions = [
        _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping),
        _attach_ping(_ScriptedSession(call_behavior=slow_death), _dead_ping),
        _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping),
        _attach_ping(_ScriptedSession(call_behavior=_dead_call), _dead_ping),
        RuntimeError("still down"),
    ]
    worker = await _scripted_worker(monkeypatch, cfg, sessions, delays)

    assert await worker.start()
    for _ in range(4):
        await worker._make_handler("t1")({})

    await _wait_for(lambda: worker._task is not None and worker._task.done())
    # 第 2 代连接稳定超窗 → 预算重置：退避序列从头开始，第 4 次断线才耗尽。
    assert delays == [0.01, 0.01, 0.02]
    assert "预算" in worker.error


@pytest.mark.asyncio
async def test_fail_on_startup_error_makes_activation_fail(monkeypatch):
    delays: list[float] = []
    cfg = {"fail_on_startup_error": True}
    worker = await _scripted_worker(monkeypatch, cfg, [RuntimeError("refused")], delays)

    assert not await worker.start()
    assert "refused" in worker.error
    await worker.stop()


@pytest.mark.asyncio
async def test_idle_ping_probe_detects_dead_connection(monkeypatch):
    delays: list[float] = []
    cfg = {"ping_interval": 0.02}
    dead_idle = _attach_ping(_ScriptedSession(call_behavior=_ok_call), _dead_ping)
    healthy = _ScriptedSession(call_behavior=_ok_call)
    worker = await _scripted_worker(monkeypatch, cfg, [dead_idle, healthy], delays)

    assert await worker.start()
    await _wait_for(lambda: worker.is_connected and worker._session is healthy)
    result = await worker._make_handler("t1")({})
    assert "ok:t1" in result
    await worker.stop()


@pytest.mark.asyncio
async def test_ping_method_not_found_disables_probe(monkeypatch):
    delays: list[float] = []
    cfg = {"ping_interval": 0.02}

    class _McpMethodNotFound(Exception):
        def __init__(self):
            super().__init__("Method not found")
            self.error = SimpleNamespace(code=-32601)

    async def no_ping():
        raise _McpMethodNotFound()

    session = _attach_ping(_ScriptedSession(call_behavior=_ok_call), no_ping)
    worker = await _scripted_worker(monkeypatch, cfg, [session], delays)

    assert await worker.start()
    await asyncio.sleep(0.1)
    assert worker.is_connected
    assert worker._ping_supported is False
    assert worker._session is session  # 未触发无谓重连
    await worker.stop()


@pytest.mark.asyncio
async def test_resync_failure_keeps_last_good_tool_list(monkeypatch):
    delays: list[float] = []
    session = _ScriptedSession(tools=("t1",))
    worker = await _scripted_worker(monkeypatch, {}, [session], delays)
    assert await worker.start()
    assert "flaky__t1" in worker.registry.names()

    async def broken_list_tools():
        raise ConnectionError("gone")

    session.list_tools = broken_list_tools
    await worker._resync_tools(session)
    assert worker.tool_names == ["t1"]
    assert "flaky__t1" in worker.registry.names()

    await worker.stop()


@pytest.mark.asyncio
async def test_resync_replaces_tool_list_in_registry(monkeypatch):
    delays: list[float] = []
    session = _ScriptedSession(tools=("t1",))
    worker = await _scripted_worker(monkeypatch, {}, [session], delays)
    assert await worker.start()
    assert "flaky__t1" in worker.registry.names()

    session._tools = [SimpleNamespace(name="t2", description="", input_schema={})]
    await worker._resync_tools(session)
    assert worker.tool_names == ["t2"]
    assert "flaky__t2" in worker.registry.names()
    assert "flaky__t1" not in worker.registry.names()

    await worker.stop()
