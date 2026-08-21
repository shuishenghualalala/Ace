"""MCP Client 有界队列、截止时间与关闭语义。"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from types import ModuleType
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from crew.browser.security import (
    BrowserNetworkDenied,
    BrowserNetworkPolicy,
    LoopbackPolicyProxy,
)
from crew.core.errors import ToolError
from crew.tools.mcp_client import (
    MCP_NETWORK_MAX_BYTES,
    MCP_RESULT_MAX_BYTES,
    MCPClientManager,
    _ServerWorker,
    _extract_text,
    _mcp_headers,
    _sanitize_tool_descriptor,
)
from crew.tools.registry import Registry


def _error(result: str) -> str:
    return str(json.loads(result)["error"])


def test_mcp_network_headers_are_bounded_before_transport_creation() -> None:
    with pytest.raises(ValueError, match="headers are too large"):
        _mcp_headers({"X-Oversized": "x" * (64 * 1024)})


def test_mcp_tool_result_is_bounded_before_serialization() -> None:
    result = SimpleNamespace(
        content=[
            {
                "type": "text",
                "text": "x" * (MCP_RESULT_MAX_BYTES + 1),
            }
        ],
        is_error=False,
    )

    extracted = json.loads(_extract_text(result))

    assert "size limit" in extracted["error"]


def test_mcp_tool_name_collision_does_not_override_existing_tool() -> None:
    registry = Registry()
    registry.register(
        name="fake__danger",
        toolset="builtin",
        schema={"name": "fake__danger", "parameters": {"type": "object"}},
        handler=lambda _args: "original",
    )
    worker = _ServerWorker("fake", {}, registry)
    worker._tools = [
        SimpleNamespace(
            name="danger",
            description="untrusted",
            input_schema={"type": "object", "properties": {}},
        )
    ]

    with pytest.raises(ValueError, match="collides with an existing tool"):
        worker._register_tools()
    assert registry.get("fake__danger").handler({}) == "original"


def _descriptor(name="ok_tool", **kwargs) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "name": name,
        "description": "untrusted",
        "input_schema": {"type": "object", "properties": {}},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_mcp_tool_schema_nesting_is_bounded() -> None:
    nested: dict[str, object] = {"type": "object"}
    node = nested
    for _index in range(34):
        node["properties"] = {"child": {}}
        node = node["properties"]["child"]

    with pytest.raises(ValueError, match="nesting exceeds"):
        _sanitize_tool_descriptor(
            _descriptor(input_schema=nested),
            secret_values=(),
        )


def test_mcp_tool_schema_key_cannot_embed_a_secret_value() -> None:
    secret = "schema-secret-canary-123456"
    with pytest.raises(ValueError, match="key is invalid"):
        _sanitize_tool_descriptor(
            _descriptor(input_schema={"type": "object", secret: "x"}),
            secret_values=(secret,),
        )


def test_mcp_tool_schema_size_is_bounded() -> None:
    oversized = {
        "type": "object",
        "properties": {
            "k": {"type": "string", "default": "x" * (300 * 1024)},
        },
    }
    with pytest.raises(ValueError, match="size limit"):
        _sanitize_tool_descriptor(
            _descriptor(input_schema=oversized),
            secret_values=(),
        )


def test_mcp_server_duplicate_tool_names_cannot_both_register() -> None:
    registry = Registry()
    worker = _ServerWorker("dup", {}, registry)
    worker._tools = [
        _descriptor(name="same"),
        _descriptor(name="same"),
    ]

    with pytest.raises(ToolError, match="已注册"):
        worker._register_tools()
    assert registry.get("dup__same") is not None


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
    await asyncio.sleep(0)

    assert session.calls == ["mutate"]
    assert "状态可能未知" in _error(result)
    assert "不会自动重试" in _error(result)
    assert worker._closing is True
    assert worker._task is not None
    assert worker._task.done()
    await worker.stop()


@pytest.mark.asyncio
async def test_inflight_cancellation_aborts_the_transport(monkeypatch):
    worker, session = await _started_worker(monkeypatch, call_timeout=1.0)
    call = asyncio.create_task(worker._make_handler("mutate")({}))
    await session.call_started.wait()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.sleep(0)

    assert session.calls == ["mutate"]
    assert worker._closing is True
    assert worker._task is not None
    assert worker._task.done()
    await worker.stop()


@pytest.mark.asyncio
async def test_mcp_transport_exception_does_not_expose_proxy_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LeakySession(_FakeSession):
        async def call_tool(self, name: str, _args: dict) -> SimpleNamespace:
            raise RuntimeError(
                "failed via http://crew:must-not-leak@127.0.0.1:1234"
            )

    worker = _ServerWorker(
        "leaky",
        {},
        Registry(),
        call_timeout=0.2,
        startup_timeout=0.1,
    )
    session = LeakySession()

    async def open_fake(_stack):
        return session

    monkeypatch.setattr(worker, "_open", open_fake)
    assert await worker.start()
    result = await worker._make_handler("probe")({})

    assert "must-not-leak" not in _error(result)
    assert "RuntimeError" in _error(result)
    await worker.stop()


@pytest.mark.asyncio
async def test_mcp_error_body_is_not_replayed_as_tool_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LeakyErrorSession(_FakeSession):
        async def call_tool(self, name: str, _args: dict) -> SimpleNamespace:
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=r"C:\private\mcp\access_token=must-not-leak",
                    )
                ],
                is_error=True,
            )

    worker = _ServerWorker("leaky", {}, Registry(), call_timeout=0.2, startup_timeout=0.1)
    session = LeakyErrorSession()

    async def open_fake(_stack):
        return session

    monkeypatch.setattr(worker, "_open", open_fake)
    assert await worker.start()
    with pytest.raises(ToolError, match="MCP 工具返回错误") as caught:
        await worker._make_handler("probe")({})
    assert "must-not-leak" not in str(caught.value)
    assert r"C:\private\mcp" not in str(caught.value)
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
async def test_quiesce_server_revokes_old_handler_before_reconnect() -> None:
    registry = Registry()
    manager = MCPClientManager({}, shutdown_timeout=0.1)
    await manager.start(registry)

    worker = _ServerWorker("fake", {}, registry)
    worker._tools = [
        SimpleNamespace(
            name="echo",
            description="",
            input_schema={"type": "object", "properties": {}},
        )
    ]
    worker._task = asyncio.create_task(asyncio.Event().wait())
    worker._register_tools()
    manager._workers.append(worker)
    old_handler = worker._make_handler("echo")

    assert await manager.quiesce_server("fake") is True
    result = await old_handler({})

    assert "连接已断开" in _error(result)
    assert manager._worker_for("fake") is None
    assert "fake__echo" not in registry.names()
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


@pytest.mark.asyncio
async def test_mcp_http_private_endpoint_is_rejected_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mcp.Client", object, raising=False)
    worker = _ServerWorker(
        "private",
        {"url": "http://169.254.169.254/latest/meta-data", "transport": "http"},
        Registry(),
    )
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="SECURITY_OUTBOUND_DENIED"):
            await worker._open(stack)


@pytest.mark.asyncio
async def test_mcp_proxy_initialization_failure_creates_no_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = False

    async def fail_proxy(_proxy) -> str:
        raise OSError("proxy credential=must-not-leak")

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal created
            created = True

    fake_httpx2 = ModuleType("httpx2")
    fake_httpx2.AsyncClient = ForbiddenClient
    monkeypatch.setitem(sys.modules, "httpx2", fake_httpx2)
    monkeypatch.setattr("mcp.Client", object, raising=False)
    monkeypatch.setattr(
        "crew.tools.mcp_client.LoopbackPolicyProxy.start",
        fail_proxy,
    )
    worker = _ServerWorker(
        "public",
        {"url": "https://93.184.216.34/mcp", "transport": "http"},
        Registry(),
    )
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="proxy_unavailable") as denied:
            await worker._open(stack)
    assert "must-not-leak" not in str(denied.value)
    assert created is False


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_name", ["http", "sse"])
async def test_mcp_network_transport_uses_authenticated_policy_proxy_and_ignores_env(
    transport_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    original_proxy_start = LoopbackPolicyProxy.start

    async def capture_proxy_policy(proxy: LoopbackPolicyProxy) -> str:
        captured["proxy_policy"] = proxy.policy
        return await original_proxy_start(proxy)

    class FakeHttpClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeProxyConfig:
        def __init__(self, url: str, *, auth: tuple[str, str]) -> None:
            self.url = url
            self.auth = auth

        def __repr__(self) -> str:
            return f"FakeProxyConfig(url={self.url!r}, auth=('crew', '********'))"

    class FakeMcpClient:
        def __init__(self, transport, **_kwargs) -> None:
            captured["transport"] = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    fake_httpx2 = ModuleType("httpx2")
    fake_httpx2.AsyncClient = FakeHttpClient
    fake_httpx2.Proxy = FakeProxyConfig
    fake_httpx2.Timeout = lambda *args, **kwargs: ("timeout", args, kwargs)
    fake_streamable = ModuleType("mcp.client.streamable_http")
    fake_streamable.streamable_http_client = (
        lambda endpoint, **kwargs: ("http", endpoint, kwargs)
    )
    fake_sse = ModuleType("mcp.client.sse")

    def fake_sse_client(endpoint, **kwargs):
        captured["sse_kwargs"] = kwargs
        return ("sse", endpoint, kwargs)

    fake_sse.sse_client = fake_sse_client
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker:secret@127.0.0.1:9")
    monkeypatch.setitem(sys.modules, "httpx2", fake_httpx2)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", fake_streamable)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", fake_sse)
    monkeypatch.setattr("mcp.Client", FakeMcpClient, raising=False)
    monkeypatch.setattr(LoopbackPolicyProxy, "start", capture_proxy_policy)

    worker = _ServerWorker(
        "public",
        {"url": "https://93.184.216.34/mcp", "transport": transport_name},
        Registry(),
    )
    async with AsyncExitStack() as stack:
        await worker._open(stack)

    if transport_name == "http":
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
    else:
        factory = captured["sse_kwargs"]["httpx_client_factory"]
        factory()
    proxy_config = captured["proxy"]
    assert isinstance(proxy_config, FakeProxyConfig)
    assert proxy_config.url.startswith("http://127.0.0.1:")
    assert "@" not in proxy_config.url
    assert proxy_config.auth[0] == "crew"
    assert len(proxy_config.auth[1]) >= 32
    assert proxy_config.auth[1] not in repr(proxy_config)
    assert "attacker" not in repr(proxy_config)
    request_hook = captured["event_hooks"]["request"][0]
    await request_hook(
        SimpleNamespace(url="https://93.184.216.34/resource", method="POST")
    )
    with pytest.raises(ValueError, match="mcp_origin_mismatch"):
        await request_hook(
            SimpleNamespace(url="https://93.184.216.35/resource", method="POST")
        )
    proxy_policy = captured["proxy_policy"]
    assert isinstance(proxy_policy, BrowserNetworkPolicy)
    assert proxy_policy.config.max_transfer_bytes == MCP_NETWORK_MAX_BYTES
    with pytest.raises(BrowserNetworkDenied, match="destination_not_authorized"):
        await proxy_policy.plan_url(
            "https://93.184.216.35/resource",
            method="POST",
        )


@pytest.mark.asyncio
async def test_stdio_mcp_fails_closed_without_managed_network_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = False

    def resolve_forbidden_secret(*_args, **_kwargs):
        nonlocal resolved
        resolved = True
        raise AssertionError("stdio credentials must not be resolved before denial")

    monkeypatch.setenv("ACE_ALLOW_HOST_MCP_STDIO", "1")
    monkeypatch.setattr("mcp.Client", object, raising=False)
    monkeypatch.setattr(
        "crew.tools.mcp_client.resolve_mcp_server_secrets",
        resolve_forbidden_secret,
    )
    worker = _ServerWorker(
        "stdio",
        {
            "command": sys.executable,
            "args": ["server.py"],
            "env": {"MCP_TOKEN": "@ace-secret:v1:not-resolved"},
        },
        Registry(),
    )
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="authenticated managed launch"):
            await worker._open(stack)
    assert resolved is False
